"""brn dynamics agentic sales pipeline: API, scheduler and dashboard."""
import base64
import html
import csv
import io
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

from . import agent, assistant, auth, compliance, db, email_template, invites
from urllib.parse import quote

from .providers import channels, enrich, inbox, llm, mailbox, sources

app = FastAPI(title="brn pipeline")
STATIC = os.path.join(os.path.dirname(__file__), "static")
ADMIN_PW = os.environ.get("BRN_ADMIN_PASSWORD")


PUBLIC = ("/webhooks/", "/brand/", "/login", "/api/login", "/book", "/api/book")   # reachable without signing in
OWN = ("/", "/api/me", "/api/password", "/api/logout")           # anyone signed in, whatever their role


@app.middleware("http")
async def gate(request: Request, call_next):
    """Who is asking, and are they allowed to? See auth.py for the three sign-in states and the roles."""
    path, user = request.url.path, None
    if path.startswith("/api/book"):  # public, and callable from a page on your own website
        cors = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "GET, POST, OPTIONS"}
        if request.method == "OPTIONS":
            return Response(status_code=204, headers=cors)
        response = await call_next(request)
        response.headers.update(cors)
        return response
    if not path.startswith(PUBLIC):
        mode = auth.mode()
        if mode == "users":
            user = auth.session_user(request.cookies.get(auth.COOKIE))
            if not user:
                if path.startswith("/api/"):
                    return JSONResponse({"detail": "Please sign in again."}, status_code=401)
                return RedirectResponse("/login", status_code=303)
        elif mode == "basic":
            h, ok = request.headers.get("authorization", ""), False
            if h.startswith("Basic "):
                try:
                    _, pw = base64.b64decode(h[6:]).decode().split(":", 1)
                    ok = secrets.compare_digest(pw, ADMIN_PW)
                except Exception:
                    ok = False
            if not ok:
                return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="brn"'})
        user = user or auth.IMPLICIT_ADMIN
        need = auth.required(request.method, path)
        if path not in OWN and need not in auth.perms(user):
            return JSONResponse({"detail": f"Your role ({auth.ROLES[user['role']]['label']}) can't do this. It needs permission to: {auth.PERMS[need].lower()}."}, status_code=403)
        if path.startswith("/api/mailbox/") and path != "/api/mailbox/folders" and _mailbox_locked(request):
            return JSONResponse({"detail": MAILBOX_LOCKED}, status_code=403)
    mark = db.actor.set(user)
    try:
        return await call_next(request)
    finally:
        db.actor.reset(mark)


MAILBOX_LOCKED = "The full mailbox is locked because nobody has to sign in to this dashboard. Add your account in Settings > People, or set BRN_ADMIN_PASSWORD on the server, to open it."


def _mailbox_locked(request):
    """With no sign-in at all, anyone with the link could read and send your mail: only allow it on this computer."""
    return auth.mode() == "open" and (request.client.host if request.client else "") not in ("127.0.0.1", "::1", "localhost")


def _need(perm):
    if not auth.can(perm):
        raise HTTPException(403, f"Your role can't do this. It needs permission to: {auth.PERMS[perm].lower()}.")


def _public_base(request):
    """This server's public address, for links other services call back to."""
    base = os.environ.get("BRN_PUBLIC_URL") or os.environ.get("RAILWAY_PUBLIC_DOMAIN") or ""
    if base:
        return (base if base.startswith("http") else f"https://{base}").rstrip("/")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    return f"{proto}://{host}"


# ---------- signing in, people and roles ----------
@app.get("/login")
def login_page():
    if auth.mode() != "users":
        return RedirectResponse("/", status_code=303)
    return FileResponse(os.path.join(STATIC, "login.html"))


def _signed_in(response, request, user_id):
    secure = _public_base(request).startswith("https")
    response.set_cookie(auth.COOKIE, auth.start_session(user_id), max_age=auth.SESSION_DAYS * 86400, httponly=True, samesite="lax", secure=secure, path="/")
    return response


@app.post("/api/login")
def login(request: Request, payload: dict = Body(...)):
    try:
        u = auth.login(payload.get("email"), payload.get("password"), request.client.host if request.client else "")
    except ValueError as e:
        raise HTTPException(401, str(e))
    return _signed_in(JSONResponse({"ok": True}), request, u["id"])


@app.post("/api/logout")
def logout(request: Request):
    auth.end_session(request.cookies.get(auth.COOKIE))
    r = JSONResponse({"ok": True})
    r.delete_cookie(auth.COOKIE, path="/")
    return r


@app.get("/api/me")
def me():
    u = db.actor.get() or auth.IMPLICIT_ADMIN
    return {"user": {k: u.get(k) for k in ("id", "name", "email", "role")}, "perms": auth.perms(u), "mode": auth.mode(),
            "role_label": auth.ROLES[u["role"]]["label"], "currency": db.setting("currency") or "USD"}


@app.post("/api/password")
def change_password(payload: dict = Body(...)):
    u = db.actor.get()
    if not u or u.get("implicit"):
        raise HTTPException(400, "There is no account to change yet. Add yours in Settings > People.")
    full = db.one("SELECT * FROM users WHERE id=?", (u["id"],))
    if not auth.check_pw(payload.get("current") or "", full["password"]):
        raise HTTPException(400, "The current password is not right.")
    try:
        auth.check_new_password(payload.get("new"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.ex("UPDATE users SET password=? WHERE id=?", (auth.hash_pw(payload["new"]), u["id"]))
    return {"ok": True}


@app.get("/api/users")
def list_users():
    return {"users": auth.users(), "mode": auth.mode(), "perms": auth.PERMS,
            "roles": [{"key": k, **v} for k, v in auth.ROLES.items()]}


@app.post("/api/users")
def add_user(request: Request, payload: dict = Body(...)):
    first = auth.mode() != "users"
    try:
        uid = auth.create(payload.get("name"), payload.get("email"), payload.get("role"), payload.get("password"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.log(None, "system", f"Account added: {payload.get('name')} ({auth.ROLES[payload['role']]['label']})", channel="system")
    r = JSONResponse({"id": uid, "first": first})
    return _signed_in(r, request, uid) if first else r  # from now on everyone signs in: keep the person who set it up signed in


@app.patch("/api/users/{user_id}")
def edit_user(user_id: int, payload: dict = Body(...)):
    try:
        u = auth.update(user_id, {k: payload[k] for k in ("name", "role", "active", "password") if k in payload}, db.actor.get())
    except ValueError as e:
        raise HTTPException(400, str(e))
    what = [f"role {auth.ROLES[payload['role']]['label']}" if "role" in payload and payload["role"] != u["role"] else "",
            ("reactivated" if payload["active"] else "deactivated") if "active" in payload and bool(payload["active"]) != bool(u["active"]) else "",
            "password reset" if payload.get("password") else ""]
    db.log(None, "system", f"Account changed: {u['name']} ({', '.join(w for w in what if w) or 'details'})", channel="system")
    return {"ok": True}


@app.delete("/api/users/{user_id}")
def remove_user(user_id: int):
    try:
        u = auth.delete(user_id, db.actor.get())
    except ValueError as e:
        raise HTTPException(400, str(e))
    if u:
        db.log(None, "system", f"Account deleted: {u['name']}", channel="system")
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/brand/{name}")
def brand(name: str):
    """Public brand images (the logo in emails is loaded from here, so it needs no login)."""
    path = os.path.join(STATIC, "brand", os.path.basename(name))
    if not name.endswith(".png") or not os.path.isfile(path):
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=604800"})


# ---------- dashboard data ----------
REPLIED_STAGES = ("replied", "meeting_booked", "won", "nurture", "lost", "opted_out")


def _lead_filters(f):
    """Shared WHERE clause for lead lists, stats and exports. f is a dict of query params."""
    w, a = ["1=1"], []
    def multi(col, val):
        vals = [v for v in str(val).split(",") if v]
        if vals:
            w.append(f"{col} IN ({','.join('?' * len(vals))})"); a.extend(vals)
    if f.get("q"):
        w.append("(l.company LIKE ? OR l.contact_name LIKE ? OR l.email LIKE ? OR l.city LIKE ? OR l.website LIKE ? OR l.notes LIKE ?)")
        a += [f"%{f['q']}%"] * 6
    for key, col in (("stage", "l.stage"), ("product", "l.product"), ("segment", "l.segment"), ("country", "l.country"), ("source", "l.source")):
        if f.get(key):
            if key == "product" and f[key] == "__none":
                w.append("l.product IS NULL")
            else:
                multi(col, f[key])
    es = f.get("email_status")
    if es == "never":
        w.append("l.emails_sent=0 AND l.email IS NOT NULL")
    elif es == "sent":
        w.append("l.emails_sent>0")
    elif es == "replied":
        w.append("l.replied_at IS NOT NULL")
    elif es == "no_reply":
        w.append("l.emails_sent>0 AND l.replied_at IS NULL")
    elif es == "no_email":
        w.append("(l.email IS NULL OR l.email='')")
    if f.get("has") == "email":
        w.append("l.email IS NOT NULL AND l.email<>''")
    elif f.get("has") == "phone":
        w.append("l.phone IS NOT NULL AND l.phone<>''")
    if f.get("score_min") not in (None, ""):
        w.append("l.score>=?"); a.append(int(f["score_min"]))
    if f.get("score_max") not in (None, ""):
        w.append("l.score<=?"); a.append(int(f["score_max"]))
    for key, col, op in (("created_from", "l.created_at", ">="), ("created_to", "l.created_at", "<="),
                         ("contacted_from", "l.last_contacted_at", ">="), ("contacted_to", "l.last_contacted_at", "<="),
                         ("replied_from", "l.replied_at", ">="), ("replied_to", "l.replied_at", "<=")):
        if f.get(key):
            w.append(f"{col}{op}?"); a.append(f[key] + ("T23:59:59" if op == "<=" and len(f[key]) == 10 else ""))
    if f.get("ids"):
        multi("l.id", f["ids"])
    return " AND ".join(w), a


SORTS = {"updated": "l.updated_at DESC", "created": "l.created_at DESC", "score": "l.score DESC", "company": "l.company COLLATE NOCASE",
         "contacted": "l.last_contacted_at DESC", "value": "COALESCE(l.deal_value,0) DESC", "next": "l.next_action_at IS NULL, l.next_action_at"}


@app.get("/api/stats")
def stats(request: Request):
    f = dict(request.query_params)
    where, args = _lead_filters(f)
    base = f"FROM leads l WHERE {where}"
    stages = {r["stage"]: r["n"] for r in db.q(f"SELECT l.stage, COUNT(*) n {base} GROUP BY l.stage", args)}
    total = sum(stages.values())
    agg = db.one(f"""SELECT COALESCE(SUM(l.emails_sent),0) emails, SUM(l.last_contacted_at IS NOT NULL) contacted,
                     SUM(l.replied_at IS NOT NULL) replies {base}""", args)
    act_where = f"a.lead_id IN (SELECT l.id {base})" if any(v for k, v in f.items()) else "1=1"
    a_args = args if act_where != "1=1" else []
    calls = db.one(f"SELECT COUNT(*) n FROM activities a WHERE a.kind='call_completed' AND a.status IN ('ok','dry_run') AND {act_where}", a_args)["n"]
    since = (datetime.now(timezone.utc) - timedelta(days=13)).date().isoformat()
    daily = db.q(f"""SELECT substr(a.created_at,1,10) d, SUM(a.kind='email_sent' AND a.status IN ('ok','dry_run')) emails,
                    SUM(a.kind='call_completed') calls, SUM(a.kind='email_reply') replies
                    FROM activities a WHERE a.created_at>=? AND {act_where} GROUP BY d ORDER BY d""", (since, *a_args))
    meetings = stages.get("meeting_booked", 0) + stages.get("won", 0)
    qualified = total - stages.get("new", 0) - stages.get("disqualified", 0) - stages.get("needs_contact", 0)
    prods = db.products()
    by_product = db.q(f"""SELECT COALESCE(l.product,'') product, COUNT(*) leads, SUM(l.emails_sent>0) emailed,
                          SUM(l.replied_at IS NOT NULL) replied, SUM(l.stage IN ('meeting_booked','won')) meetings,
                          SUM(l.stage='in_sequence') active {base} GROUP BY l.product ORDER BY leads DESC""", args)
    for r in by_product:
        r["name"] = prods.get(r["product"], {}).get("name", "No product")
    return {
        "mode": db.setting("mode"), "llm": llm.enabled(), "llm_status": llm.status(), "agent_enabled": db.setting("agent_enabled") == "1", "agent": agent.schedule_state(),
        "stages": stages, "daily": daily, "by_product": by_product,
        "kpis": {
            "leads": total, "qualified": qualified, "contacted": agg["contacted"] or 0, "emails": agg["emails"] or 0, "calls": calls,
            "replies": agg["replies"] or 0, "meetings": meetings,
            "pending_approvals": db.one("SELECT COUNT(*) n FROM approvals WHERE state='pending'")["n"],
            "blocked": db.one("SELECT COUNT(*) n FROM activities WHERE status='blocked'")["n"],
            "errors": db.one("SELECT COUNT(*) n FROM activities WHERE status='error' AND created_at>=?", (since,))["n"],
            "unread_mail": db.one(f"SELECT COUNT(DISTINCT a.lead_id) n FROM activities a JOIN leads l ON l.id=a.lead_id WHERE {UNREAD}")["n"],
            "inbox_unread": None if _mailbox_locked(request) or not auth.can("mailbox") else mailbox.inbox_unread(),
            "meetings_upcoming": db.one("SELECT COUNT(*) n FROM meetings WHERE status='scheduled' AND ends_at>=?", (db.now(),))["n"],
            "tasks_due": db.one("SELECT COUNT(*) n FROM tasks WHERE done_at IS NULL AND due_at IS NOT NULL AND due_at<=?",
                                ((datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(timespec="seconds"),))["n"],
        },
        "funnel": [["Leads", total], ["Qualified", qualified], ["Contacted", agg["contacted"] or 0],
                   ["Replied", agg["replies"] or 0], ["Meetings", meetings]],
    }


# ---------- leads ----------
@app.get("/api/leads")
def leads(request: Request):
    f = dict(request.query_params)
    where, args = _lead_filters(f)
    total = db.one(f"SELECT COUNT(*) n FROM leads l WHERE {where}", args)["n"]
    limit = min(int(f.get("limit", 100)), 1000)
    offset = int(f.get("offset", 0))
    rows = db.q(f"""SELECT l.id, l.company, l.contact_name, l.title, l.email, l.phone, l.website, l.city, l.country, l.industry,
                    l.segment, l.product, l.source, l.stage, l.score, l.step, l.next_action, l.next_action_at, l.emails_sent,
                    l.last_contacted_at, l.replied_at, l.created_at, l.updated_at, l.deal_value, l.expected_close
                    FROM leads l WHERE {where} ORDER BY {SORTS.get(f.get('sort'), SORTS['updated'])} LIMIT ? OFFSET ?""",
                (*args, limit, offset))
    return {"total": total, "rows": rows}


@app.get("/api/leads/facets")
def facets():
    def counts(col):
        return db.q(f"SELECT {col} value, COUNT(*) n FROM leads WHERE {col} IS NOT NULL AND {col}<>'' GROUP BY {col} ORDER BY n DESC")
    return {"stage": counts("stage"), "product": counts("product"), "segment": counts("segment"),
            "country": counts("country"), "source": counts("source"),
            "products": list(db.products().values())}


@app.get("/api/leads/{lead_id}")
def lead(lead_id: int):
    l = db.one("SELECT * FROM leads WHERE id=?", (lead_id,))
    if not l:
        raise HTTPException(404)
    l["research"] = json.loads(l["research"] or "null")
    l["extra"] = json.loads(l["extra"] or "null")
    l["activities"] = db.q("SELECT * FROM activities WHERE lead_id=? ORDER BY id DESC", (lead_id,))
    l["approvals"] = db.q("SELECT * FROM approvals WHERE lead_id=? ORDER BY id DESC", (lead_id,))
    l["tasks"] = db.q("SELECT t.*, u.name assignee FROM tasks t LEFT JOIN users u ON u.id=t.assigned_to WHERE t.lead_id=? ORDER BY t.done_at IS NOT NULL, t.due_at IS NULL, t.due_at, t.id DESC LIMIT 30", (lead_id,))
    l["meetings"] = [_meeting(m) for m in db.q("SELECT m.*, NULL company FROM meetings m WHERE m.lead_id=? ORDER BY m.starts_at DESC", (lead_id,))]
    return l


STATUS_MAP = {"new": "new", "contacted": "completed", "replied": "replied", "interested": "replied", "meeting": "meeting_booked",
              "won": "won", "lost": "lost", "closed": "lost", "unsubscribed": "opted_out", "do not contact": "opted_out"}


def _insert(row, source="manual", defaults=None):
    d = defaults or {}
    email = (row.get("email") or "").strip().lower() or None
    if email and db.one("SELECT 1 x FROM leads WHERE lower(email)=?", (email,)):
        return None  # duplicate
    if not email:  # discovered leads often have no email: dedupe by website, phone or place
        site = (row.get("website") or "").strip().lower().rstrip("/")
        if site and db.one("SELECT 1 x FROM leads WHERE lower(rtrim(website,'/'))=?", (site,)):
            return None
        if row.get("phone") and db.one("SELECT 1 x FROM leads WHERE phone=?", (row["phone"],)):
            return None
        pid = (row.get("extra") or {}).get("place_id")
        if pid and db.one("SELECT 1 x FROM leads WHERE extra LIKE ?", (f'%"place_id": "{pid}"%',)):
            return None
    t = db.now()
    stage = STATUS_MAP.get(str(row.get("status") or "new").strip().lower(), "new")
    if row.get("date_contacted") and stage == "new":
        stage = "completed"
    extra = row.get("extra") or {}
    if row.get("status") and stage != "new":
        extra["imported_status"] = row["status"]
    product = row.get("product") or d.get("product") or None
    if product and product not in db.products():
        product = next((k for k, p in db.products().items() if p["name"].lower() == str(product).lower()), product)
    lid = db.ex("""INSERT INTO leads(company, contact_name, title, email, phone, website, city, country, industry, segment, product, extra,
                source, consent_basis, notes, stage, last_contacted_at, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row.get("company"), row.get("contact_name"), row.get("title"), email, row.get("phone"), row.get("website"),
                 row.get("city"), (row.get("country") or d.get("country") or "SA").upper()[:2], row.get("industry"),
                 row.get("segment") or d.get("segment"), product, json.dumps(extra, ensure_ascii=False, default=str) if extra else None,
                 row.get("source") or source, row.get("consent_basis") or d.get("consent_basis") or "legitimate_interest_b2b",
                 row.get("notes"), stage, row.get("date_contacted"), t, t))
    db.log(lid, "system", f"Lead added from {row.get('source') or source}" + (f" ({stage})" if stage != "new" else ""), channel="system")
    return lid


def _import(rows, source, payload):
    defaults = {k: payload.get(k) for k in ("product", "country", "consent_basis", "segment") if payload.get(k)}
    ids = [_insert(r, source, defaults) for r in rows]
    added = [i for i in ids if i]
    db.log(None, "system", f"Imported {len(added)} leads from {source} ({len(ids) - len(added)} duplicates skipped)", channel="system")
    return {"imported": added, "skipped": len(ids) - len(added), "rows": len(ids)}


@app.post("/api/leads")
def add_lead(row: dict = Body(...)):
    if not row.get("company"):
        raise HTTPException(400, "company is required")
    if row.get("linkedin"):
        row["extra"] = {**(row.get("extra") or {}), "linkedin": enrich.linkedin_url(row["linkedin"]) or row["linkedin"]}
    lid = _insert(row)
    if not lid:
        raise HTTPException(409, "This lead is already in the system (same email, or the same website or phone with no email).")
    return {"id": lid}


@app.post("/api/leads/import")
def import_file(payload: dict = Body(...)):
    """CSV text in `csv`, or an Excel/CSV file as base64 in `file` with its `filename`."""
    if payload.get("file"):
        data = base64.b64decode(payload["file"].split(",")[-1])
        name = (payload.get("filename") or "").lower()
        try:
            rows = sources.parse_xlsx(data) if name.endswith((".xlsx", ".xlsm")) else sources.parse_csv(data.decode("utf-8-sig"))
        except Exception as e:
            raise HTTPException(400, f"Could not read the file: {e}")
        return _import(rows, payload.get("filename") or "file", payload)
    return _import(sources.parse_csv(payload.get("csv", "")), "csv", payload)


@app.post("/api/leads/places")
def import_places(payload: dict = Body(...)):
    try:
        rows = sources.google_places(payload["query"], payload.get("country", "SA"), int(payload.get("limit", 20)))
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    return _import(rows, "google_places", payload)


# ---------- contact finder: name + LinkedIn profile -> email and phone ----------
def _fill_phone(lead_id, numbers):
    """A mobile number arrived for a lead: keep it (replacing a company switchboard number, never one a person typed in)."""
    lead = db.one("SELECT phone, stage, extra FROM leads WHERE id=?", (lead_id,))
    if not lead:
        return
    extra = json.loads(lead["extra"] or "{}")
    extra.pop("phone_request", None)
    fields = {"extra": json.dumps(extra, ensure_ascii=False)}
    if numbers and (not lead["phone"] or extra.pop("company_line", None)):
        fields["phone"] = numbers[0]
        fields["extra"] = json.dumps({**extra, **({"other_phones": numbers[1:4]} if numbers[1:] else {})}, ensure_ascii=False)
        if lead["stage"] == "needs_contact":
            fields["stage"] = "new"
        db.log(lead_id, "system", f"Contact finder: phone number found ({numbers[0]})", channel="system")
    elif not numbers:
        db.log(lead_id, "system", "Contact finder: no phone number on record for this person", channel="system")
    db.update_lead(lead_id, **fields)


def _await_phone(lead_id, request_id):
    """Apollo takes a few minutes to return mobile numbers: check back until it has, for up to 20 minutes."""
    deadline, wait = time.time() + 1200, 20
    while time.time() < deadline:
        time.sleep(min(max(wait, 10), 120))
        try:
            state, value = enrich.phone_result(request_id)
        except Exception:
            continue
        if state == "pending":
            wait = value
            continue
        if state == "done":
            _fill_phone(lead_id, value)
        return


def _known_profile(url):
    handle = url.rsplit("/", 1)[-1].lower()
    return db.one("SELECT id, company, contact_name, email, phone FROM leads WHERE lower(extra) LIKE ?", (f'%linkedin.com/in/{handle}"%',))


def _find_one(name, linkedin, company, defaults, want_phone):
    """Look one person up and save them as a lead. Returns a result row for the page."""
    out = {"name": name, "linkedin": linkedin}
    known = _known_profile(linkedin) if linkedin else None
    if known:  # no credits spent on someone already here
        return {**out, "status": "exists", "lead_id": known["id"], "email": known["email"], "phone": known["phone"], "company": known["company"]}
    try:
        p = enrich.find_person(name, linkedin, company, want_phone=want_phone)
    except RuntimeError as e:
        return {**out, "status": "error", "detail": str(e)}
    p = p or {}
    extra = {k: v for k, v in {"linkedin": p.get("linkedin") or linkedin, "email_status": p.get("email_status"), "match_confidence": p.get("confidence"),
                               "phone_request": p.get("phone_request"), "company_line": p.get("company_line") or None, "found_by": "contact finder"}.items() if v}
    row = {"company": p.get("company") or company or name or (linkedin or "").rsplit("/", 1)[-1] or "Unknown", "contact_name": p.get("contact_name") or name, "title": p.get("title"),
           "email": p.get("email"), "phone": p.get("phone"), "website": p.get("website"), "city": p.get("city"), "industry": p.get("industry"),
           "country": p.get("country"), "extra": extra, "source": "linkedin"}
    lid = _insert(row, "linkedin", defaults)
    if not lid and not row["email"]:
        # no email, so the only match was the company's website or switchboard: a colleague is already a lead.
        # This is a different person, so keep them, with the shared details set aside.
        row["extra"].update({k: v for k, v in {"company_phone": row["phone"] if p.get("company_line") else None, "company_website": row["website"]}.items() if v})
        row["website"] = None
        if p.get("company_line"):
            row["phone"] = None; row["extra"].pop("company_line", None)
        lid = _insert(row, "linkedin", defaults)
    if not lid:
        dup = db.one("SELECT id FROM leads WHERE lower(email)=?", (row["email"] or "",)) or {}
        return {**out, "status": "exists", "lead_id": dup.get("id"), "email": row["email"], "phone": row["phone"], "company": row["company"]}
    if p.get("phone_request"):
        threading.Thread(target=_await_phone, args=(lid, p["phone_request"]), daemon=True).start()
    return {**out, "status": "found" if p else "not_found", "lead_id": lid, "name": row["contact_name"], "title": row["title"], "company": row["company"],
            "email": row["email"], "phone": row["phone"], "phone_pending": bool(p.get("phone_request")), "company_line": bool(row["phone"] and p.get("company_line")), "note": p.get("note")}


@app.post("/api/leads/find")
def find_people(payload: dict = Body(...)):
    """`text`: one person per line, as "Name, LinkedIn link, Company (optional)". Each becomes a lead with whatever was found."""
    if not enrich.enabled():
        raise HTTPException(400, "The contact finder is not connected. Add APOLLO_API_KEY (an Apollo.io API key) to the server's variables.")
    people = []
    for line in (payload.get("text") or "").splitlines():
        link = enrich.linkedin_url(line)
        rest = [x.strip() for x in re.split(r"[,\t;|]", re.sub(r"\S*linkedin\.com/\S*", "", line, flags=re.I)) if x.strip()]
        if link or len(rest) >= 2:
            people.append((rest[0] if rest else None, link, rest[1] if len(rest) > 1 else None))
    if not people:
        raise HTTPException(400, "Nothing to look up. Write one person per line: name, LinkedIn link, and optionally the company.")
    if len(people) > 25:
        raise HTTPException(400, "That's more than 25 people. Send them in smaller batches.")
    defaults = {k: payload.get(k) for k in ("product", "country", "consent_basis", "segment") if payload.get(k)}
    results = [_find_one(n, l, c, defaults, payload.get("phone", True)) for n, l, c in people]
    found = sum(r["status"] == "found" for r in results)
    db.log(None, "system", f"Contact finder: looked up {len(results)} people, {found} found", channel="system")
    return {"results": results}


@app.post("/api/leads/{lead_id}/find-contact")
def find_contact(lead_id: int):
    """Fill in a missing email or phone for an existing lead, from its LinkedIn profile or its name and company."""
    lead = db.one("SELECT * FROM leads WHERE id=?", (lead_id,))
    if not lead:
        raise HTTPException(404)
    if not enrich.enabled():
        raise HTTPException(400, "The contact finder is not connected. Add APOLLO_API_KEY (an Apollo.io API key) to the server's variables.")
    extra = json.loads(lead["extra"] or "{}")
    try:
        if extra.get("phone_request"):  # a lookup is already under way: just collect it
            state, value = enrich.phone_result(extra["phone_request"])
            if state == "pending":
                return {"status": "pending", "detail": "Apollo is still looking for the phone number. Try again in a few minutes."}
            _fill_phone(lead_id, value or [])
            return {"status": "done", "phone": (value or [None])[0]}
        domain = re.sub(r"^https?://(www\.)?", "", lead.get("website") or "").split("/")[0] or None
        p = enrich.find_person(lead.get("contact_name"), enrich.linkedin_url(extra.get("linkedin") or lead.get("notes") or ""),
                               lead.get("company"), domain, want_phone=not lead.get("phone"))
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    if not p:
        db.log(lead_id, "system", "Contact finder: this person was not found", channel="system")
        return {"status": "not_found", "detail": "Apollo has no record of this person."}
    fields = {k: p[k] for k in ("email", "phone", "title", "contact_name", "city") if p.get(k) and not lead.get(k)}
    if fields.get("email") and db.one("SELECT 1 x FROM leads WHERE lower(email)=? AND id<>?", (fields["email"], lead_id)):
        fields.pop("email")
    extra.update({k: v for k, v in {"linkedin": p.get("linkedin"), "email_status": p.get("email_status"), "phone_request": p.get("phone_request"),
                                    "company_line": (p.get("company_line") and "phone" in fields) or None}.items() if v})
    if lead["stage"] == "needs_contact" and (fields.get("email") or fields.get("phone")):
        fields["stage"] = "new"
    db.update_lead(lead_id, extra=json.dumps(extra, ensure_ascii=False), **fields)
    db.log(lead_id, "system", "Contact finder: " + (", ".join(f"{k} found" for k in fields if k in ("email", "phone", "title")) or "nothing new found")
           + (", phone number still being looked up" if p.get("phone_request") else ""), channel="system")
    if p.get("phone_request"):
        threading.Thread(target=_await_phone, args=(lead_id, p["phone_request"]), daemon=True).start()
    return {"status": "found", "email": fields.get("email"), "phone": fields.get("phone"), "phone_pending": bool(p.get("phone_request")), "note": p.get("note")}


EDITABLE = ("stage", "notes", "consent_basis", "email", "phone", "contact_name", "title", "product", "segment", "country", "city", "website",
            "deal_value", "expected_close")


@app.patch("/api/leads/{lead_id}")
def edit_lead(lead_id: int, payload: dict = Body(...)):
    allowed = {k: v for k, v in payload.items() if k in EDITABLE}
    if "deal_value" in allowed:
        try:
            allowed["deal_value"] = float(str(allowed["deal_value"]).replace(",", "")) if allowed["deal_value"] not in (None, "") else None
        except ValueError:
            raise HTTPException(400, "The deal value must be a number.")
    if "expected_close" in allowed:
        allowed["expected_close"] = allowed["expected_close"] or None
    if allowed.get("stage") == "in_sequence":
        allowed["next_action_at"] = db.now()
    cur = db.one("SELECT stage FROM leads WHERE id=?", (lead_id,))
    if cur and cur["stage"] == "needs_contact" and (allowed.get("email") or allowed.get("phone")) and "stage" not in allowed:
        allowed["stage"] = "new"
    if allowed:
        db.update_lead(lead_id, **allowed)
        db.log(lead_id, "stage_change" if "stage" in allowed else "system", f"Edited by user: {', '.join(k for k in allowed if k != 'next_action_at')}", channel="system")
    return {"ok": True}


@app.post("/api/leads/bulk")
def bulk(payload: dict = Body(...)):
    """actions: set_stage, set_product, set_segment, pause, resume, restart, delete. Targets `ids`, or every lead matching `filters`."""
    if payload.get("ids"):
        ids = [int(i) for i in payload["ids"]]
    else:
        where, args = _lead_filters(payload.get("filters") or {})
        ids = [r["id"] for r in db.q(f"SELECT l.id FROM leads l WHERE {where}", args)]
    action, value = payload.get("action"), payload.get("value")
    for i in ids:
        if action == "set_stage":
            edit_lead(i, {"stage": value})
        elif action in ("set_product", "set_segment"):
            db.update_lead(i, **{action[4:]: value or None})
        elif action == "pause":
            db.update_lead(i, stage="paused", next_action=None, next_action_at=None)
        elif action == "resume":
            db.update_lead(i, stage="in_sequence", next_action_at=db.now())
        elif action == "restart":
            db.update_lead(i, stage="new", step=0, next_action=None, next_action_at=None, emails_sent=0, last_contacted_at=None)
        elif action == "delete":
            _need("leads.delete")
            db.ex("DELETE FROM meetings WHERE lead_id=? AND status<>'scheduled'", (i,)); db.ex("UPDATE meetings SET lead_id=NULL WHERE lead_id=?", (i,))
            db.ex("DELETE FROM approvals WHERE lead_id=?", (i,)); db.ex("DELETE FROM activities WHERE lead_id=?", (i,))
            db.ex("DELETE FROM leads WHERE id=?", (i,))
        else:
            raise HTTPException(400, "unknown action")
    db.log(None, "system", f"Bulk {action}{' = ' + str(value) if value else ''} on {len(ids)} leads", channel="system")
    return {"updated": len(ids)}


@app.post("/api/leads/{lead_id}/reply")
def reply(lead_id: int, payload: dict = Body(...)):
    return agent.handle_reply(lead_id, payload.get("text", ""))


def _csv_response(rows, fields, filename):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader(); w.writerows(rows)
    return PlainTextResponse(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.get("/api/export/leads.csv")
def export_leads(request: Request):
    f = dict(request.query_params); f["limit"] = 100000
    where, args = _lead_filters(f)
    rows = db.q(f"SELECT l.* FROM leads l WHERE {where} ORDER BY l.company", args)
    return _csv_response(rows, ["id", "company", "contact_name", "title", "email", "phone", "website", "city", "country", "segment",
                                "product", "industry", "stage", "score", "emails_sent", "last_contacted_at", "replied_at",
                                "source", "consent_basis", "notes", "created_at"], "brn-leads.csv")


# ---------- activity log ----------
@app.get("/api/activities")
def activities(limit: int = 200, kind: str = None, status: str = None, channel: str = None, product: str = None,
               date_from: str = None, date_to: str = None, q: str = None, lead_id: int = None):
    sql, args = "SELECT a.*, l.company, l.product FROM activities a LEFT JOIN leads l ON l.id=a.lead_id WHERE 1=1", []
    for val, col in ((kind, "a.kind"), (status, "a.status"), (channel, "a.channel")):
        if val:
            sql += f" AND {col}=?"; args.append(val)
    picked = [x for x in (product or "").split(",") if x]
    if picked:
        sql += f" AND l.product IN ({','.join('?' * len(picked))})"; args += picked
    if lead_id:
        sql += " AND a.lead_id=?"; args.append(lead_id)
    if date_from:
        sql += " AND a.created_at>=?"; args.append(date_from)
    if date_to:
        sql += " AND a.created_at<=?"; args.append(date_to + ("T23:59:59" if len(date_to) == 10 else ""))
    if q:
        sql += " AND (a.summary LIKE ? OR l.company LIKE ?)"; args += [f"%{q}%"] * 2
    return db.q(sql + " ORDER BY a.id DESC LIMIT ?", (*args, min(limit, 5000)))


@app.get("/api/export/activities.csv")
def export_activities(request: Request):
    f = {k: v for k, v in request.query_params.items() if k in ("kind", "status", "channel", "product", "date_from", "date_to", "q")}
    rows = activities(limit=5000, **f)
    return _csv_response(rows, ["id", "created_at", "company", "product", "lead_id", "kind", "channel", "status", "summary"], "brn-activity-log.csv")


# ---------- email preview ----------
@app.get("/api/email/preview")
def email_preview(product: str = None, lead_id: int = None, activity_id: int = None):
    """Renders the branded template: for a sent email (activity_id), a lead's next email, or a sample."""
    try:
        return _email_preview(product, lead_id, activity_id)
    except Exception as e:
        db.log(None, "system", f"Email preview failed: {e}", channel="system", status="error")
        return Response(f"<div style='font:15px sans-serif;padding:40px;color:#333'><b>Preview could not be built.</b><br><br>"
                        f"{html.escape(str(e))[:400]}</div>", media_type="text/html")


def _email_preview(product=None, lead_id=None, activity_id=None):
    from .email_template import render
    if activity_id:
        a = db.one("SELECT a.payload, l.* FROM activities a JOIN leads l ON l.id=a.lead_id WHERE a.id=?", (activity_id,))
        if not a:
            raise ValueError("That email is no longer in the log.")
        p = json.loads(a["payload"] or "{}")
        body = p.get("body", "").split("\n\n--\n")[0]
        cta_line = body.rsplit("\n\n", 1)[-1] if ": http" in body.rsplit("\n\n", 1)[-1] else None
        if cta_line:
            body = body.rsplit("\n\n", 1)[0]
        prod = db.products().get(a.get("product") or "")
        return Response(render(p.get("subject", ""), body, agent._settings(a)["booking_link"], p.get("cta"),
                               prod["name"] if prod else None, preview=True), media_type="text/html")
    lead = db.one("SELECT * FROM leads WHERE id=?", (lead_id,)) if lead_id else None
    lead = lead or {"company": "Acme Lavender Farm", "contact_name": "Sarah Miller", "city": "Hood River", "country": "US",
                    "product": product or db.setting("default_product"), "research": None,
                    "extra": json.dumps({"issues": [{"problem": "Home page took 7.0s to respond"}]})}
    if product:
        lead = {**lead, "product": product}
    s = agent._settings(lead)
    r = json.loads(lead.get("research") or "null") or llm.research(lead, s["offer"], s["icp"], s["product_key"])
    content = llm.write_email(lead, r or {}, "intro", s)
    prod = db.products().get(lead.get("product") or "")
    return Response(render(content["subject"], content["body"], s["booking_link"], content.get("cta"), prod["name"] if prod else None, preview=True),
                    media_type="text/html")


# ---------- mailbox: every email sent and every reply, as one conversation per lead ----------
UNREAD = "a.kind='email_reply' AND a.created_at>COALESCE(l.mail_seen_at,'')"


def _message(a):
    """One activity row as a mailbox message."""
    p = json.loads(a["payload"] or "{}")
    if a["kind"] == "email_reply":
        text = p.get("text") or ""
        subject = p.get("subject")
        if text.startswith("Subject: "):
            head, _, text = text.partition("\n\n")
            subject = subject or head[9:]
        return {"id": a["id"], "dir": "in", "at": p.get("received_at") or a["created_at"], "status": a["status"], "subject": subject or "",
                "body": text.strip(), "from": p.get("from"), "intent": p.get("intent"), "summary": p.get("summary")}
    body = p.get("body") or ""
    return {"id": a["id"], "dir": "out", "at": a["created_at"], "status": a["status"], "subject": p.get("subject") or "",
            "body": body if p.get("manual") else body.split("\n\n--\n")[0], "to": p.get("to"), "manual": bool(p.get("manual")),
            "via": p.get("provider"), "note": a["summary"] if a["status"] == "error" else None}


@app.get("/api/mail")
def mail(filter: str = "all", q: str = None, product: str = None):
    w, args = ["a.kind IN ('email_sent','email_reply')", "a.status IN ('ok','dry_run')"], []
    if q:
        w.append("""(l.company LIKE ? OR l.contact_name LIKE ? OR l.email LIKE ? OR l.id IN
                    (SELECT lead_id FROM activities WHERE kind IN ('email_sent','email_reply') AND payload LIKE ?))""")
        args += [f"%{q}%"] * 4
    if product:
        w.append("l.product=?"); args.append(product)
    having = {"unread": "unread>0", "replied": "replies>0", "waiting": "sent>0 AND replies=0", "sent": "sent>0"}.get(filter, "1=1")
    rows = db.q(f"""SELECT * FROM (SELECT l.id, l.company, l.contact_name, l.email, l.product, l.stage,
                    SUM(a.kind='email_sent' AND a.status='ok') sent, SUM(a.kind='email_sent' AND a.status='dry_run') dry_runs,
                    SUM(a.kind='email_reply') replies, SUM({UNREAD}) unread, MAX(a.id) last_id
                    FROM activities a JOIN leads l ON l.id=a.lead_id WHERE {' AND '.join(w)}
                    GROUP BY l.id, l.company, l.contact_name, l.email, l.product, l.stage) t WHERE {having} ORDER BY last_id DESC LIMIT 300""", args)
    last = {a["id"]: _message(a) for a in db.q(
        f"SELECT * FROM activities WHERE id IN ({','.join('?' * len(rows))})", [r["last_id"] for r in rows])} if rows else {}
    for r in rows:
        m = last.get(r["last_id"]) or {}
        r["last"] = {"dir": m.get("dir"), "at": m.get("at"), "status": m.get("status"), "subject": m.get("subject"),
                     "snippet": " ".join((m.get("body") or "").split())[:160]}
    return {"threads": rows, "inbox": inbox.enabled(), "email": bool(channels.email_provider()),
            "last_check": db.setting("inbox_checked_at"), "mailbox": os.environ.get("IMAP_USER") or os.environ.get("SMTP_USER") or ""}


@app.get("/api/mail/{lead_id}")
def mail_thread(lead_id: int):
    l = db.one("SELECT id, company, contact_name, title, email, product, stage, emails_sent, replied_at FROM leads WHERE id=?", (lead_id,))
    if not l:
        raise HTTPException(404)
    msgs = [_message(a) for a in db.q("""SELECT * FROM activities WHERE lead_id=? AND kind IN ('email_sent','email_reply')
                                         AND status IN ('ok','dry_run','error') ORDER BY id""", (lead_id,))]
    l["messages"] = sorted(msgs, key=lambda m: m["at"] or "")
    l["can_send"] = bool(channels.email_provider())
    db.ex("UPDATE leads SET mail_seen_at=? WHERE id=?", (db.now(), lead_id))  # opening the conversation marks it read
    return l


@app.post("/api/mail/{lead_id}/send")
def mail_send(lead_id: int, payload: dict = Body(...)):
    """A reply typed by a person in a lead's conversation. It is sent straight away, as plain text, whatever the send mode."""
    lead = db.one("SELECT * FROM leads WHERE id=?", (lead_id,))
    if not lead:
        raise HTTPException(404)
    if not (payload.get("subject") or "").strip() or not (payload.get("body") or "").strip():
        raise HTTPException(400, "Write a subject and a message first.")
    if not lead.get("email"):
        raise HTTPException(400, "This lead has no email address.")
    prev = db.q("SELECT payload FROM activities WHERE lead_id=? AND kind='email_reply' ORDER BY id DESC LIMIT 1", (lead_id,))
    mid = json.loads(prev[0]["payload"] or "{}").get("message_id") if prev else None
    return _send_composed({"to": lead["email"], "subject": payload["subject"], "body": payload["body"], "in_reply_to": mid, "references": mid})


# ---------- mailbox: the whole mail account (folders, reading, compose, drafts) ----------
def _mb(fn, *a, **k):
    try:
        return fn(*a, **k)
    except mailbox.MailboxError as e:
        raise HTTPException(502, str(e))


def _lead_map(addresses):
    addrs = sorted({a.lower() for a in addresses if a})
    if not addrs:
        return {}
    rows = db.q(f"SELECT id, company, lower(email) email FROM leads WHERE lower(email) IN ({','.join('?' * len(addrs))})", addrs)
    return {r["email"]: {"id": r["id"], "company": r["company"]} for r in rows}


@app.get("/api/mailbox/folders")
def mb_folders(request: Request):
    out = {"enabled": mailbox.enabled(), "can_send": bool(channels.email_provider()), "me": (channels._sender() or mailbox.address()).lower(),
           "last_check": db.setting("inbox_checked_at"), "folders": [], "inbox": inbox.enabled(),
           "lead_unread": db.one(f"SELECT COUNT(DISTINCT a.lead_id) n FROM activities a JOIN leads l ON l.id=a.lead_id WHERE {UNREAD}")["n"]}
    if out["enabled"] and _mailbox_locked(request):
        out.update(enabled=False, locked=MAILBOX_LOCKED)
    elif out["enabled"]:
        try:
            out["folders"] = mailbox.folders()
        except mailbox.MailboxError as e:
            out["error"] = str(e)
    return out


@app.get("/api/mailbox/messages")
def mb_messages(folder: str, page: int = 0, q: str = None, filter: str = None):
    r = _mb(mailbox.messages, folder, max(page, 0), 50, (q or "").strip() or None, filter)
    leads = _lead_map([x["from"]["email"] for x in r["rows"]] + [t["email"] for x in r["rows"] for t in x["to"]])
    for x in r["rows"]:
        x["lead"] = leads.get(x["from"]["email"].lower()) or next((leads[t["email"].lower()] for t in x["to"] if t["email"].lower() in leads), None)
    return r


@app.get("/api/mailbox/message")
def mb_message(folder: str, uid: int, seen: int = 1):
    m = _mb(mailbox.message, folder, uid, bool(seen))
    leads = _lead_map([m["from"]["email"]] + [t["email"] for t in m["to"]])
    m["lead"] = leads.get(m["from"]["email"].lower()) or next((leads[t["email"].lower()] for t in m["to"] if t["email"].lower() in leads), None)
    if seen and m["from"]["email"].lower() in leads:  # read here = read in the lead's conversation too
        db.ex("UPDATE leads SET mail_seen_at=? WHERE id=?", (db.now(), leads[m["from"]["email"].lower()]["id"]))
    return m


SANDBOX = "sandbox allow-popups allow-popups-to-escape-sandbox"


@app.get("/api/mailbox/html")
def mb_html(folder: str, uid: int):
    """An email's HTML body. Scripts, forms and frames are switched off by the browser; only images, styles and fonts load."""
    body = _mb(mailbox.html_body, folder, uid)
    if body is None:
        raise HTTPException(404)
    return Response(body, media_type="text/html", headers={
        "Content-Security-Policy": f"{mailbox.CSP}; {SANDBOX}",
        "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})


@app.get("/api/mailbox/attachment")
def mb_attachment(folder: str, uid: int, i: int):
    name, ctype, data = _mb(mailbox.attachment, folder, uid, i)
    return Response(data, media_type="application/octet-stream", headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox"})


@app.post("/api/mailbox/flags")
def mb_flags(payload: dict = Body(...)):
    for key, flag in (("seen", "\\Seen"), ("starred", "\\Flagged")):
        if key in payload:
            _mb(mailbox.set_flag, payload["folder"], payload.get("uids") or [], flag, bool(payload[key]))
    return {"ok": True}


@app.post("/api/mailbox/move")
def mb_move(payload: dict = Body(...)):
    """to: trash (from Trash itself: gone for good), spam, archive, inbox, or delete."""
    folder, uids, to = payload["folder"], payload.get("uids") or [], payload.get("to")
    if to == "inbox":
        target = "INBOX"
    elif to in ("trash", "spam", "archive"):
        target = _mb(mailbox.folder_for, to, True)
    elif to == "delete":
        target = None
    else:
        raise HTTPException(400, "unknown destination")
    if target == folder:
        if to != "trash":
            return {"moved": 0}
        target = None  # deleting from Trash removes it for good
    _mb(mailbox.move, folder, uids, target)
    return {"moved": len(uids)}


def _compose_parts(c):
    """Attachments of a composed email: new uploads, plus ones carried over from another message
    (the email being forwarded, or the earlier version of this draft)."""
    out = []
    carry = c.get("carry") or {}
    for i in (carry.get("indexes") or []) if carry.get("uid") else []:
        out.append(_mb(mailbox.attachment, carry["folder"], carry["uid"], int(i)))
    for f in c.get("files") or []:
        try:
            out.append((f.get("name") or "attachment", f.get("type") or "application/octet-stream", base64.b64decode(f["data"].split(",")[-1])))
        except Exception:
            raise HTTPException(400, f"Could not read the attachment {f.get('name')}.")
    if sum(len(x[2]) for x in out) > 20_000_000:
        raise HTTPException(400, "Attachments are over 20 MB in total. Remove some and try again.")
    return out


def _build(c, draft=False):
    fwd = c.get("forward") or {}
    parts = _mb(mailbox.forward_parts, fwd["folder"], int(fwd["uid"])) if fwd.get("uid") else None
    return mailbox.build(db.setting("sender_name"), channels._sender() or mailbox.address(), c, _compose_parts(c), parts, draft)


@app.post("/api/mailbox/draft")
def mb_draft(c: dict = Body(...)):
    """Save (or re-save) a draft in the mailbox's Drafts folder. Returns where it now lives."""
    msg = _build(c, draft=True)
    folder = _mb(mailbox.folder_for, "drafts", True)
    uid = _mb(mailbox.append, folder, msg, "\\Draft \\Seen")
    if c.get("draft_uid"):  # the old version goes only after the new one is safely stored
        _mb(mailbox.move, folder, [c["draft_uid"]], None)
    atts = _mb(mailbox.message, folder, uid, False)["attachments"] if uid else []
    return {"folder": folder, "uid": uid, "attachments": atts}


def _send_composed(c):
    """Send an email a person wrote. Goes out straight away through the connected mailbox, whatever the send mode."""
    if not channels.email_provider():
        raise HTTPException(400, "Email not connected. Set RESEND_API_KEY, or SMTP_HOST, SMTP_USER and SMTP_PASSWORD.")
    msg = _build(c)
    rcpts = mailbox.recipients(msg)
    if not rcpts:
        raise HTTPException(400, "Add at least one valid email address.")
    leads = db.q(f"SELECT id, email, stage FROM leads WHERE lower(email) IN ({','.join('?' * len(rcpts))})", rcpts)
    blocked = sorted({a for a in rcpts if compliance.is_suppressed({"email": a})} | {l["email"] for l in leads if l["stage"] == "opted_out"})
    if blocked:
        raise HTTPException(400, f"{', '.join(blocked)} is on the do-not-contact list, so nothing was sent.")
    subject = str(msg["Subject"] or "")
    try:
        res = channels.send_message(msg)
    except Exception as e:
        for l in leads:
            db.log(l["id"], "email_sent", f"Email failed to send: {e}", channel="email", status="error")
        raise HTTPException(502, f"The email was not sent: {e}")
    for l in leads:  # shows up in the lead's timeline and conversation
        db.log(l["id"], "email_sent", f"sent by hand: {subject}", channel="email", status="ok",
               payload={"status": "ok", **res, "provider_id": str(msg["Message-ID"]), "to": l["email"], "subject": subject,
                        "body": c.get("body") or "", "manual": True})
        db.ex("UPDATE leads SET emails_sent=emails_sent+1, last_contacted_at=?, mail_seen_at=? WHERE id=?", (db.now(), db.now(), l["id"]))
    for cleanup in (lambda: c.get("draft_uid") and mailbox.move(mailbox.folder_for("drafts"), [c["draft_uid"]], None),
                    lambda: (c.get("answered") or {}).get("uid") and mailbox.set_flag(c["answered"]["folder"], [c["answered"]["uid"]], "\\Answered", True)):
        try:  # the email is already out: tidying up must not turn that into an error
            cleanup()
        except Exception:
            pass
    return {"ok": True, "to": rcpts}


@app.post("/api/mailbox/send")
def mb_send(c: dict = Body(...)):
    return _send_composed(c)


# ---------- calendar: meetings, with Google Calendar and Meet when connected ----------
def _utc(value):
    """Any ISO time (with or without offset) as a UTC ISO string."""
    d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")


def _meeting(m):
    m = dict(m)
    m["attendees"] = json.loads(m.get("attendees") or "[]")
    return m


def _attendees(value):
    text = " ".join((a.get("email") or "") if isinstance(a, dict) else str(a or "") for a in value) if isinstance(value, list) else (value or "")
    out = []
    for a in re.findall(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+", text):
        if a.lower() not in out:
            out.append(a.lower())
    return out


@app.get("/api/calendar")
def calendar(start: str, end: str):
    """Meetings between two moments, and the next ones coming up."""
    start, end = _utc(start), _utc(end)
    sql = "SELECT m.*, l.company, l.contact_name FROM meetings m LEFT JOIN leads l ON l.id=m.lead_id"
    return {"meetings": [_meeting(m) for m in db.q(f"{sql} WHERE m.starts_at<? AND m.ends_at>? ORDER BY m.starts_at", (end, start))],
            "upcoming": [_meeting(m) for m in db.q(f"{sql} WHERE m.status='scheduled' AND m.ends_at>=? ORDER BY m.starts_at LIMIT 12", (db.now(),))],
            "meet_links": len(invites.meet_links()), "can_email": bool(channels.email_provider()), "can_settings": auth.can("settings")}


def _meeting_fields(p, old=None):
    old = old or {}
    title = (p.get("title") or old.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "Give the meeting a title.")
    try:
        starts = _utc(p["starts_at"]) if p.get("starts_at") else old["starts_at"]
    except (KeyError, ValueError):
        raise HTTPException(400, "Pick a date and time.")
    if p.get("duration_min") or not old:
        ends = (datetime.fromisoformat(starts) + timedelta(minutes=max(5, int(p.get("duration_min") or 30)))).isoformat(timespec="seconds")
    else:  # moved without a new length: keep how long it was
        ends = (datetime.fromisoformat(starts) + (datetime.fromisoformat(old["ends_at"]) - datetime.fromisoformat(old["starts_at"]))).isoformat(timespec="seconds")
    return {"title": title, "starts_at": starts, "ends_at": ends, "timezone": p.get("timezone") or old.get("timezone") or "UTC",
            "attendees": _attendees(p["attendees"]) if "attendees" in p else [a["email"] for a in json.loads(old.get("attendees") or "[]")],
            "description": p.get("description", old.get("description") or ""), "location": p.get("location", old.get("location") or "")}


def book_meeting(f, lead=None, meet=True, meet_link=None, by=None, invite=True):
    """Create a meeting, give it a Google Meet link from the ones saved in Settings, email the invitation, and
    update the lead it belongs to."""
    shared = False
    if not meet_link and meet:
        meet_link, shared = invites.pick_link(f["starts_at"], f["ends_at"])
    who = db.actor.get() or {}
    mid = db.ex("""INSERT INTO meetings(lead_id, title, starts_at, ends_at, timezone, attendees, description, location, meet_link, created_by, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (lead and lead["id"], f["title"], f["starts_at"], f["ends_at"], f["timezone"], json.dumps([{"email": a} for a in f["attendees"]]),
                 f["description"], f["location"], meet_link or None, by or (None if who.get("implicit") else who.get("name")), db.now()))
    m = {**f, "id": mid, "meet_link": meet_link, "lead_id": lead and lead["id"], "ics_seq": 0}
    invited = invites.send(m, "invite") if invite else []
    note = (" (invitation emailed)" if invited else "") + (f", by {by}" if by else "")
    if lead:
        db.log(lead["id"], "meeting", f"Meeting booked: {f['title']}{note}", channel="calendar",
               payload={"meeting_id": mid, "starts_at": f["starts_at"], "meet_link": meet_link, "attendees": f["attendees"], "invited": invited})
        if lead["stage"] not in ("won", "opted_out"):
            db.update_lead(lead["id"], stage="meeting_booked", next_action="meeting", next_action_at=f["starts_at"])
            db.ex("UPDATE approvals SET state='rejected', decided_at=? WHERE lead_id=? AND state='pending' AND action NOT IN ('reply','followup')", (db.now(), lead["id"]))
    else:
        db.log(None, "meeting", f"Meeting booked: {f['title']}{note}", channel="calendar", payload={"meeting_id": mid})
    return {"id": mid, "meet_link": meet_link, "invited": invited, "shared_link": shared, "can_email": bool(channels.email_provider())}


@app.post("/api/calendar")
def schedule_meeting(p: dict = Body(...)):
    """Book a meeting. With Google connected (and unless meet is false), it is created in Google Calendar with a
    Meet link and Google emails the invitation to every attendee."""
    lead = db.one("SELECT * FROM leads WHERE id=?", (int(p["lead_id"]),)) if p.get("lead_id") else None
    return book_meeting(_meeting_fields(p), lead, p.get("meet", True), (p.get("meet_link") or "").strip() or None, invite=p.get("invite", True))


@app.patch("/api/calendar/{meeting_id}")
def change_meeting(meeting_id: int, p: dict = Body(...)):
    """Move or edit a meeting, or mark it done. The people invited get an updated invitation."""
    m = db.one("SELECT * FROM meetings WHERE id=?", (meeting_id,))
    if not m:
        raise HTTPException(404)
    if p.get("status") in ("done", "scheduled") and len(p) == 1:
        db.ex("UPDATE meetings SET status=? WHERE id=?", (p["status"], meeting_id))
        return {"ok": True}
    f = _meeting_fields(p, m)
    link = (p.get("meet_link") or "").strip() or m["meet_link"]
    if not link or (f["starts_at"], f["ends_at"]) != (m["starts_at"], m["ends_at"]) and link in invites.meet_links():
        link = invites.pick_link(f["starts_at"], f["ends_at"], except_id=meeting_id)[0] or link  # keep it clear of other meetings at the new time
    seq = int(m.get("ics_seq") or 0) + 1
    db.ex("UPDATE meetings SET title=?, starts_at=?, ends_at=?, timezone=?, attendees=?, description=?, location=?, meet_link=?, ics_seq=?, status='scheduled', reminded_at=NULL WHERE id=?",
          (f["title"], f["starts_at"], f["ends_at"], f["timezone"], json.dumps([{"email": a} for a in f["attendees"]]), f["description"], f["location"], link, seq, meeting_id))
    told = invites.send({**f, "id": meeting_id, "meet_link": link, "lead_id": m["lead_id"], "ics_seq": seq}, "update") if p.get("invite", True) else []
    if m["lead_id"]:
        moved = f["starts_at"] != m["starts_at"]
        db.log(m["lead_id"], "meeting", f"Meeting {'moved' if moved else 'changed'}: {f['title']}", channel="calendar", payload={"meeting_id": meeting_id, "starts_at": f["starts_at"]})
        if moved:
            db.ex("UPDATE leads SET next_action_at=? WHERE id=? AND next_action='meeting'", (f["starts_at"], m["lead_id"]))
    return {"ok": True, "told": told}


@app.delete("/api/calendar/{meeting_id}")
def cancel_meeting(meeting_id: int):
    """Cancel a meeting. It stays on record as cancelled, and the people invited are told."""
    m = db.one("SELECT * FROM meetings WHERE id=?", (meeting_id,))
    if not m:
        raise HTTPException(404)
    told = invites.send({**_meeting(m), "ics_seq": int(m.get("ics_seq") or 0) + 1}, "cancel") if m["status"] == "scheduled" else []
    db.ex("UPDATE meetings SET status='cancelled' WHERE id=?", (meeting_id,))
    db.log(m["lead_id"], "meeting", f"Meeting cancelled: {m['title']}", channel="calendar", payload={"meeting_id": meeting_id})
    return {"ok": True, "told": told}


# ---------- approvals ----------
@app.get("/api/approvals")
def approvals(state: str = "pending"):
    rows = db.q("SELECT a.*, l.company, l.contact_name, l.email, l.phone FROM approvals a JOIN leads l ON l.id=a.lead_id WHERE a.state=? ORDER BY a.id", (state,))
    for r in rows:
        r["content"] = json.loads(r["content"])
    return rows


def _decide(approval_id, approve, content=None):
    """Approve (and really send) or skip one draft. Returns "sent", "skipped", "gone", or the reason the send failed."""
    a = db.one("SELECT lead_id, action FROM approvals WHERE id=? AND state='pending'", (approval_id,))
    if not a:
        return "gone"
    mark = db.one("SELECT COALESCE(MAX(id),0) n FROM activities")["n"]
    agent.decide(approval_id, approve, content)
    if not approve:
        return "skipped"
    err = db.one("SELECT summary FROM activities WHERE id>? AND lead_id=? AND status='error' ORDER BY id LIMIT 1", (mark, a["lead_id"]))
    if not err:
        return "sent"
    # it did not go out: put the draft (with any edits) back in Approvals instead of quietly re-drafting later
    db.ex("UPDATE approvals SET state='pending', decided_at=NULL WHERE id=?", (approval_id,))
    if a["action"] != "reply":
        db.update_lead(a["lead_id"], stage="awaiting_approval", next_action="approval", next_action_at=None)
    return err["summary"].replace(", retrying in 1 hour", "").replace(", retrying in 2 hours", "")


@app.post("/api/approvals/bulk")
def decide_bulk(payload: dict = Body(...)):
    """items: [{id, content?}] with approve true/false. Emails stop at today's cap; the rest stay waiting."""
    approve, out = bool(payload.get("approve")), {"sent": 0, "skipped": 0, "failed": [], "left": 0}
    cap = int(db.setting("daily_email_cap"))
    for it in payload.get("items") or []:
        a = db.one("SELECT a.channel, a.action, l.company FROM approvals a JOIN leads l ON l.id=a.lead_id WHERE a.id=? AND a.state='pending'", (int(it["id"]),))
        if not a:
            continue
        # the daily cap is for cold outreach: an answer to someone who wrote to us is never held back
        if approve and a["channel"] == "email" and a["action"] != "reply" and compliance.sent_today("email_sent") >= cap:
            out["left"] += 1
            continue
        res = _decide(int(it["id"]), approve, it.get("content"))
        if res in ("sent", "skipped"):
            out[res] += 1
        elif res != "gone":
            out["failed"].append(f"{a['company']}: {res}")
    db.log(None, "system", f"Bulk {'approve' if approve else 'skip'}: {out['sent']} sent, {out['skipped']} skipped, {len(out['failed'])} failed"
           + (f", {out['left']} left waiting (daily cap of {cap} reached)" if out["left"] else ""), channel="system")
    return {**out, "cap": cap}


@app.post("/api/approvals/{approval_id}")
def decide(approval_id: int, payload: dict = Body(...)):
    res = _decide(approval_id, bool(payload.get("approve")), payload.get("content"))
    if res == "gone":
        raise HTTPException(409, "already decided")
    if res not in ("sent", "skipped"):
        raise HTTPException(502, res)
    return {"ok": True}


# ---------- settings & compliance ----------
@app.get("/api/settings")
def get_settings(request: Request):
    s = {r["key"]: r["value"] for r in db.q("SELECT * FROM settings") if not r["key"].startswith("secret_")}
    for k in ("sequence", "products", "discovery_queries"):
        try:
            s[k] = json.loads(s[k])
        except Exception:
            pass
    s["live_allowed"] = os.environ.get("BRN_ALLOW_LIVE") == "1"
    s["email_via"] = channels.email_provider()
    s["logo_effective"] = email_template.logo_url()
    s["booking_url"] = _public_base(request) + "/book"
    s["app_url"] = _public_base(request)
    s["providers"] = {"claude": llm.enabled(), "email": bool(channels.email_provider()), "inbox": inbox.enabled(),
                      "voice": bool(os.environ.get("VAPI_API_KEY")), "enrich": enrich.enabled(), "meet": bool(invites.meet_links()), "places": bool(os.environ.get("GOOGLE_PLACES_API_KEY"))}
    return s


@app.post("/api/settings")
def save_settings(payload: dict = Body(...)):
    if "mode" in payload and payload["mode"] not in ("approval", "live"):
        raise HTTPException(400, "Send mode must be approval or live.")
    if payload.get("mode") == "live" and os.environ.get("BRN_ALLOW_LIVE") != "1":
        raise HTTPException(403, "Live sending is locked. Set BRN_ALLOW_LIVE=1 on the server once outreach is approved.")
    if "ai_balance" in payload and str(payload["ai_balance"]) != str(db.setting("ai_balance") or ""):
        db.set_setting("ai_balance_at", db.now())  # spend is counted from the moment the balance was entered
    for k, v in payload.items():
        if k in db.DEFAULT_SETTINGS and k != "ai_balance_at":
            db.set_setting(k, json.dumps(v) if k in ("sequence", "products", "discovery_queries") else v)
    db.log(None, "system", f"Settings changed: {', '.join(payload)}", channel="system")
    return {"ok": True}


@app.get("/api/suppression")
def get_suppression():
    return db.q("SELECT * FROM suppression ORDER BY created_at DESC")


@app.post("/api/suppression")
def add_suppression(payload: dict = Body(...)):
    compliance.suppress(payload["value"], payload.get("reason", "manual"))
    return {"ok": True}


# ---------- agent controls ----------
@app.post("/api/agent/state")
def agent_state(payload: dict = Body(...)):
    on = bool(payload.get("on"))
    db.set_setting("agent_enabled", "1" if on else "0")
    db.log(None, "system", "Agent started" if on else "Agent stopped: no research, emails, calls, inbox reading or lead searches until started again",
           channel="system")
    return {"agent_enabled": on}


@app.get("/api/ai/usage")
def ai_usage():
    return llm.usage_report()


@app.post("/api/agent/tick")
def tick():
    return {"processed": agent.tick(force=True)}  # asked for by hand: run now, whatever the schedule says


@app.post("/webhooks/voice")
def voice_webhook(payload: dict = Body(...)):
    """Vapi's end-of-call report. The lead is found from the call id saved when the call was placed
    (or from lead_id in the call's metadata)."""
    msg = payload.get("message", payload)
    if msg.get("type") not in (None, "end-of-call-report"):
        return {"ignored": True}  # status updates, transcripts while the call is running, ...
    call = msg.get("call") or {}
    meta = {**(call.get("metadata") or {}), **((call.get("assistantOverrides") or {}).get("metadata") or {}), **((msg.get("assistant") or {}).get("metadata") or {})}
    lead_id = meta.get("lead_id")
    if not lead_id and call.get("id"):
        row = db.one("SELECT lead_id FROM activities WHERE kind='call_completed' AND payload LIKE ? ORDER BY id DESC LIMIT 1", (f'%"provider_id": "{call["id"]}"%',))
        lead_id = row and row["lead_id"]
    if not lead_id or not db.one("SELECT 1 x FROM leads WHERE id=?", (int(lead_id),)):
        return {"ignored": True}
    analysis = msg.get("analysis") or call.get("analysis") or {}
    outcome = (analysis.get("structuredData") or {}).get("outcome") or msg.get("endedReason")
    artifact = msg.get("artifact") or {}
    db.log(int(lead_id), "call_completed", f"Call report: {outcome}" + (f". {analysis['summary']}" if analysis.get("summary") else ""), channel="phone", status="ok",
           payload={"transcript": msg.get("transcript") or artifact.get("transcript"), "recording": msg.get("recordingUrl") or artifact.get("recordingUrl"),
                    "outcome": outcome, "summary": analysis.get("summary")})
    agent.apply_outcome(int(lead_id), outcome)
    return {"ok": True}


@app.post("/api/email/test")
def test_email():
    """Sends one test email to your own sender address, to check the mailbox connection."""
    provider = channels.email_provider()
    if not provider:
        raise HTTPException(400, "Email not connected. Set RESEND_API_KEY, or SMTP_HOST, SMTP_USER and SMTP_PASSWORD.")
    me = channels._sender()
    try:
        channels.deliver({"email": me}, "brn pipeline: test email", f"Email sending works (via {provider}). The agent can send as {me}.")
    except Exception as e:
        db.log(None, "system", f"Test email failed: {e}", channel="email", status="error")
        raise HTTPException(502, f"Test email failed: {e}")
    db.log(None, "system", f"Test email sent to {me} via {provider}", channel="email")
    return {"sent_to": me, "via": provider}


@app.post("/api/inbox/check")
def check_inbox():
    if not inbox.enabled():
        raise HTTPException(400, "Mailbox not connected. Set IMAP_HOST, SMTP_USER and SMTP_PASSWORD.")
    try:
        return {"replies": inbox.poll(agent.handle_reply, agent.handle_bounce)}
    except Exception as e:
        db.log(None, "system", f"Inbox check failed: {e}", channel="email", status="error")
        raise HTTPException(502, f"Could not read the mailbox: {e}")


def discover_pipeline(key, sources_only=None):
    """Finds new leads for one pipeline from its sources (Google Places, Reddit) and imports them into it."""
    p = db.products().get(key)
    if not p:
        raise HTTPException(404, "unknown pipeline")
    src = p.get("sources") or {}
    out = {"pipeline": key, "imported": 0, "skipped": 0, "errors": [], "notes": []}
    places = src.get("places") or []
    if places and (sources_only in (None, "places")):
        if not os.environ.get("GOOGLE_PLACES_API_KEY"):
            out["notes"].append("Google Places key not set, so business search was skipped.")
        else:
            for qd in places:
                try:
                    rows = sources.google_places(qd["query"], qd.get("country", "SA"), int(qd.get("limit", 20)),
                                                 bool(src.get("only_without_website")))
                    res = _import(rows, f"google_places: {qd['query']}", {"product": key, "segment": qd.get("segment") or p.get("name")})
                    out["imported"] += len(res["imported"]); out["skipped"] += res["skipped"]
                except Exception as e:
                    out["errors"].append(f"{qd.get('query')}: {e}")
    for phrase in (src.get("reddit") or []) if sources_only in (None, "reddit") else []:
        try:
            rows = sources.reddit_search(phrase)
            res = _import(rows, f"reddit: {phrase}", {"product": key, "segment": "Asking online"})
            out["imported"] += len(res["imported"]); out["skipped"] += res["skipped"]
        except Exception as e:
            out["errors"].append(f"Reddit '{phrase}': {e}")
    web = src.get("web") or []
    if web and (sources_only in (None, "web")):
        if not llm.enabled():
            out["notes"].append("ANTHROPIC_API_KEY is not set, so web search was skipped.")
        else:
            for qd in web:
                try:
                    rows = llm.discover_businesses(qd["query"], qd.get("country", "US"), int(qd.get("limit", 15)))
                    rows = sources.enrich(rows, wordpress_only=bool(src.get("wordpress_only")))
                    res = _import(rows, f"web: {qd['query']}", {"product": key, "segment": qd.get("segment") or p.get("name"),
                                                                 "country": qd.get("country", "US")})
                    out["imported"] += len(res["imported"]); out["skipped"] += res["skipped"]
                except Exception as e:
                    out["errors"].append(f"{qd.get('query')}: {e}")
    if not places and not web and not src.get("reddit"):
        out["notes"].append("This pipeline has no sources yet. Add web searches, Places searches or Reddit phrases in Settings > Pipelines.")
    ps = db.setting("pipeline_runs", json.loads) or {}
    ps[key] = {"at": db.now(), "imported": out["imported"], "errors": len(out["errors"])}
    db.set_setting("pipeline_runs", json.dumps(ps))
    db.log(None, "system", f"Lead discovery for {p['name']}: {out['imported']} new leads"
           + (f", {len(out['errors'])} errors ({out['errors'][0][:120]})" if out["errors"] else ""),
           channel="system", status="error" if out["errors"] and not out["imported"] else "ok")
    return out


@app.post("/api/assistant")
def assistant_chat(payload: dict = Body(...)):
    """Chat assistant for the dashboard. It can read and edit leads but never sends or approves anything."""
    return assistant.chat(payload.get("messages") or [])


@app.post("/api/pipelines/{key}/discover")
def discover_now(key: str):
    return discover_pipeline(key)


@app.get("/api/pipelines")
def pipelines():
    runs = db.setting("pipeline_runs", json.loads) or {}
    counts = {}
    for r in db.q("SELECT COALESCE(product,'') product, stage, COUNT(*) n FROM leads GROUP BY product, stage"):
        counts.setdefault(r["product"], {})[r["stage"]] = r["n"]
    out = []
    for k, p in db.products().items():
        src = p.get("sources") or {}
        out.append({**p, "stages": counts.get(k, {}), "total": sum(counts.get(k, {}).values()), "last_run": runs.get(k),
                    "source_count": len(src.get("places") or []) + len(src.get("reddit") or [])})
    return {"pipelines": out, "auto": db.setting("auto_discovery") == "1",
            "places_key": bool(os.environ.get("GOOGLE_PLACES_API_KEY"))}


def _auto_discover():
    """Once a day: the saved global Places searches, then each pipeline's own sources (when auto discovery is on)."""
    last = db.setting("last_discovery")
    if last and datetime.fromisoformat(last) > datetime.now(timezone.utc) - timedelta(hours=23):
        return
    db.set_setting("last_discovery", db.now())
    queries = db.setting("discovery_queries", json.loads) or []
    if queries and os.environ.get("GOOGLE_PLACES_API_KEY"):
        for qd in queries:
            try:
                rows = sources.google_places(qd["query"], qd.get("country", "SA"), int(qd.get("limit", 20)))
                res = _import(rows, "google_places", qd)
                db.log(None, "system", f"Auto-discovery '{qd['query']}': {len(res['imported'])} new leads", channel="system")
            except Exception as e:
                db.log(None, "system", f"Auto-discovery '{qd.get('query')}' failed: {e}", channel="system", status="error")
    if db.setting("auto_discovery") == "1":
        for key in db.products():
            try:
                discover_pipeline(key)
            except Exception as e:
                db.log(None, "system", f"Discovery for {key} failed: {e}", channel="system", status="error")


def _loop():
    n = 0
    while True:
        try:
            jobs.run()  # meeting reminders, follow-ups and task reminders: these keep running when the agent is stopped
        except Exception as e:
            db.log(None, "system", f"Reminder error: {e}", channel="system", status="error")
        try:
            if not agent.active():  # switched off, or outside its scheduled hours: no research, outreach, inbox reading or lead searches
                n += 1
                time.sleep(60)
                continue
            agent.tick()
            if n % 5 == 0 and inbox.enabled():  # read replies every 5 minutes
                inbox.poll(agent.handle_reply, agent.handle_bounce)
            if n % 60 == 0:  # hourly: auto-discovery runs at most once a day
                _auto_discover()
        except Exception as e:
            db.log(None, "system", f"Scheduler error: {e}", channel="system", status="error")
        n += 1
        time.sleep(60)


from . import jobs, more  # noqa: E402  (they build on what is defined above)
app.include_router(more.router)

if os.environ.get("BRN_SCHEDULER", "1") == "1":
    threading.Thread(target=_loop, daemon=True).start()
