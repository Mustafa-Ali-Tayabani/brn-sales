"""brn dynamics agentic sales pipeline: API, scheduler and dashboard."""
import base64
import html
import csv
import io
import json
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response

from . import agent, compliance, db
from .providers import channels, inbox, llm, sources

app = FastAPI(title="brn pipeline")
STATIC = os.path.join(os.path.dirname(__file__), "static")
ADMIN_PW = os.environ.get("BRN_ADMIN_PASSWORD")


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if ADMIN_PW and not request.url.path.startswith("/webhooks/"):
        h = request.headers.get("authorization", "")
        ok = False
        if h.startswith("Basic "):
            try:
                _, pw = base64.b64decode(h[6:]).decode().split(":", 1)
                ok = secrets.compare_digest(pw, ADMIN_PW)
            except Exception:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="brn"'})
    return await call_next(request)


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


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
         "contacted": "l.last_contacted_at DESC", "next": "l.next_action_at IS NULL, l.next_action_at"}


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
        "mode": db.setting("mode"), "llm": llm.enabled(), "llm_status": llm.status(), "agent_enabled": db.setting("agent_enabled") == "1",
        "stages": stages, "daily": daily, "by_product": by_product,
        "kpis": {
            "leads": total, "qualified": qualified, "contacted": agg["contacted"] or 0, "emails": agg["emails"] or 0, "calls": calls,
            "replies": agg["replies"] or 0, "meetings": meetings,
            "pending_approvals": db.one("SELECT COUNT(*) n FROM approvals WHERE state='pending'")["n"],
            "blocked": db.one("SELECT COUNT(*) n FROM activities WHERE status='blocked'")["n"],
            "errors": db.one("SELECT COUNT(*) n FROM activities WHERE status='error' AND created_at>=?", (since,))["n"],
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
                    l.last_contacted_at, l.replied_at, l.created_at, l.updated_at
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
    lid = _insert(row)
    if not lid:
        raise HTTPException(409, "a lead with this email already exists")
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


EDITABLE = ("stage", "notes", "consent_basis", "email", "phone", "contact_name", "title", "product", "segment", "country", "city", "website")


@app.patch("/api/leads/{lead_id}")
def edit_lead(lead_id: int, payload: dict = Body(...)):
    allowed = {k: v for k, v in payload.items() if k in EDITABLE}
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
    for val, col in ((kind, "a.kind"), (status, "a.status"), (channel, "a.channel"), (product, "l.product")):
        if val:
            sql += f" AND {col}=?"; args.append(val)
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
                               prod["name"] if prod else None), media_type="text/html")
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
    return Response(render(content["subject"], content["body"], s["booking_link"], content.get("cta"), prod["name"] if prod else None),
                    media_type="text/html")


# ---------- approvals ----------
@app.get("/api/approvals")
def approvals(state: str = "pending"):
    rows = db.q("SELECT a.*, l.company, l.contact_name, l.email, l.phone FROM approvals a JOIN leads l ON l.id=a.lead_id WHERE a.state=? ORDER BY a.id", (state,))
    for r in rows:
        r["content"] = json.loads(r["content"])
    return rows


@app.post("/api/approvals/{approval_id}")
def decide(approval_id: int, payload: dict = Body(...)):
    if not agent.decide(approval_id, bool(payload.get("approve")), payload.get("content")):
        raise HTTPException(409, "already decided")
    return {"ok": True}


# ---------- settings & compliance ----------
@app.get("/api/settings")
def get_settings():
    s = {r["key"]: r["value"] for r in db.q("SELECT * FROM settings")}
    for k in ("sequence", "products", "discovery_queries"):
        try:
            s[k] = json.loads(s[k])
        except Exception:
            pass
    s["live_allowed"] = os.environ.get("BRN_ALLOW_LIVE") == "1"
    s["email_via"] = channels.email_provider()
    s["providers"] = {"claude": llm.enabled(), "email": bool(channels.email_provider()), "inbox": inbox.enabled(),
                      "voice": bool(os.environ.get("VAPI_API_KEY")), "places": bool(os.environ.get("GOOGLE_PLACES_API_KEY"))}
    return s


@app.post("/api/settings")
def save_settings(payload: dict = Body(...)):
    if payload.get("mode") == "live" and os.environ.get("BRN_ALLOW_LIVE") != "1":
        raise HTTPException(403, "Live sending is locked. Set BRN_ALLOW_LIVE=1 on the server once outreach is approved.")
    for k, v in payload.items():
        if k in db.DEFAULT_SETTINGS:
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
@app.post("/api/agent/tick")
def tick():
    return {"processed": agent.tick()}


@app.post("/api/agent/simulate")
def simulate(payload: dict = Body(default={})):
    if db.setting("mode") == "live":
        raise HTTPException(400, "simulation is disabled in live mode")
    return {"processed": agent.simulate(int(payload.get("days", 14)))}


@app.post("/webhooks/voice")
def voice_webhook(payload: dict = Body(...)):
    """Vapi end-of-call report. Put lead_id in the call's metadata."""
    msg = payload.get("message", payload)
    lead_id = (msg.get("call", {}).get("metadata") or {}).get("lead_id")
    if not lead_id:
        return {"ignored": True}
    outcome = (msg.get("analysis") or {}).get("structuredData", {}).get("outcome") or msg.get("endedReason")
    db.log(int(lead_id), "call_completed", f"Call report: {outcome}", channel="phone", status="ok",
           payload={"transcript": msg.get("transcript"), "recording": msg.get("recordingUrl"), "outcome": outcome})
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
    return {"replies": inbox.poll(agent.handle_reply)}


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
    if not places and not src.get("reddit"):
        out["notes"].append("This pipeline has no sources yet. Add Places searches or Reddit phrases in Settings > Pipelines.")
    ps = db.setting("pipeline_runs", json.loads) or {}
    ps[key] = {"at": db.now(), "imported": out["imported"], "errors": len(out["errors"])}
    db.set_setting("pipeline_runs", json.dumps(ps))
    db.log(None, "system", f"Lead discovery for {p['name']}: {out['imported']} new leads"
           + (f", {len(out['errors'])} errors ({out['errors'][0][:120]})" if out["errors"] else ""),
           channel="system", status="error" if out["errors"] and not out["imported"] else "ok")
    return out


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
            agent.tick()
            if n % 5 == 0 and inbox.enabled():  # read replies every 5 minutes
                inbox.poll(agent.handle_reply)
            if n % 60 == 0:  # hourly: auto-discovery runs at most once a day
                _auto_discover()
        except Exception as e:
            db.log(None, "system", f"Scheduler error: {e}", channel="system", status="error")
        n += 1
        time.sleep(60)


if os.environ.get("BRN_SCHEDULER", "1") == "1":
    threading.Thread(target=_loop, daemon=True).start()
