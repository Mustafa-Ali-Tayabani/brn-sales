"""brn dynamics agentic sales pipeline: API, scheduler and dashboard."""
import base64
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
from .providers import llm, sources

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
@app.get("/api/stats")
def stats():
    stages = {r["stage"]: r["n"] for r in db.q("SELECT stage, COUNT(*) n FROM leads GROUP BY stage")}
    kinds = {r["kind"]: r["n"] for r in db.q("SELECT kind, COUNT(*) n FROM activities WHERE status IN ('ok','dry_run') GROUP BY kind")}
    since = (datetime.now(timezone.utc) - timedelta(days=13)).date().isoformat()
    daily = db.q("""SELECT substr(created_at,1,10) d,
                    SUM(kind='email_sent') emails, SUM(kind='call_completed') calls, SUM(kind='email_reply') replies
                    FROM activities WHERE created_at>=? GROUP BY d ORDER BY d""", (since,))
    total = sum(stages.values())
    contacted = db.one("SELECT COUNT(DISTINCT lead_id) n FROM activities WHERE kind IN ('email_sent','call_completed')")["n"]
    replied = stages.get("replied", 0) + stages.get("meeting_booked", 0) + stages.get("won", 0) + stages.get("nurture", 0) + stages.get("lost", 0) + stages.get("opted_out", 0)
    return {
        "mode": db.setting("mode"), "llm": llm.enabled(), "stages": stages, "kinds": kinds, "daily": daily,
        "kpis": {
            "leads": total, "qualified": total - stages.get("new", 0) - stages.get("disqualified", 0),
            "contacted": contacted, "emails": kinds.get("email_sent", 0), "calls": kinds.get("call_completed", 0),
            "replies": replied, "meetings": stages.get("meeting_booked", 0) + stages.get("won", 0),
            "pending_approvals": db.one("SELECT COUNT(*) n FROM approvals WHERE state='pending'")["n"],
            "blocked": db.one("SELECT COUNT(*) n FROM activities WHERE status='blocked'")["n"],
        },
        "funnel": [
            ["Leads", total], ["Qualified", total - stages.get("new", 0) - stages.get("disqualified", 0)],
            ["Contacted", contacted], ["Replied", replied], ["Meetings", stages.get("meeting_booked", 0) + stages.get("won", 0)],
        ],
    }


# ---------- leads ----------
@app.get("/api/leads")
def leads(stage: str = None, q: str = None):
    sql, args = "SELECT * FROM leads WHERE 1=1", []
    if stage:
        sql += " AND stage=?"; args.append(stage)
    if q:
        sql += " AND (company LIKE ? OR contact_name LIKE ? OR email LIKE ? OR city LIKE ?)"; args += [f"%{q}%"] * 4
    return db.q(sql + " ORDER BY updated_at DESC LIMIT 500", args)


@app.get("/api/leads/{lead_id}")
def lead(lead_id: int):
    l = db.one("SELECT * FROM leads WHERE id=?", (lead_id,))
    if not l:
        raise HTTPException(404)
    l["research"] = json.loads(l["research"] or "null")
    l["activities"] = db.q("SELECT * FROM activities WHERE lead_id=? ORDER BY id DESC", (lead_id,))
    l["approvals"] = db.q("SELECT * FROM approvals WHERE lead_id=? ORDER BY id DESC", (lead_id,))
    return l


def _insert(row, source="manual"):
    t = db.now()
    lid = db.ex("""INSERT INTO leads(company, contact_name, title, email, phone, website, city, country, industry, source,
                consent_basis, notes, stage, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'new', ?, ?)""",
                (row.get("company"), row.get("contact_name"), row.get("title"), row.get("email"), row.get("phone"), row.get("website"),
                 row.get("city"), (row.get("country") or "SA").upper(), row.get("industry"), row.get("source") or source,
                 row.get("consent_basis") or "legitimate_interest_b2b", row.get("notes"), t, t))
    db.log(lid, "system", f"Lead added from {row.get('source') or source}", channel="system")
    return lid


@app.post("/api/leads")
def add_lead(row: dict = Body(...)):
    if not row.get("company"):
        raise HTTPException(400, "company is required")
    return {"id": _insert(row)}


@app.post("/api/leads/import")
def import_csv(payload: dict = Body(...)):
    rows = sources.parse_csv(payload.get("csv", ""))
    return {"imported": [_insert(r, "csv") for r in rows]}


@app.post("/api/leads/places")
def import_places(payload: dict = Body(...)):
    try:
        rows = sources.google_places(payload["query"], payload.get("country", "SA"), int(payload.get("limit", 20)))
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    return {"imported": [_insert(r, "google_places") for r in rows]}


@app.patch("/api/leads/{lead_id}")
def edit_lead(lead_id: int, payload: dict = Body(...)):
    allowed = {k: v for k, v in payload.items() if k in ("stage", "notes", "consent_basis", "email", "phone", "contact_name", "title")}
    if allowed:
        db.update_lead(lead_id, **allowed)
        db.log(lead_id, "stage_change" if "stage" in allowed else "system", f"Edited by user: {', '.join(allowed)}", channel="system")
    return {"ok": True}


@app.post("/api/leads/{lead_id}/reply")
def reply(lead_id: int, payload: dict = Body(...)):
    return agent.handle_reply(lead_id, payload.get("text", ""))


# ---------- activity log ----------
@app.get("/api/activities")
def activities(limit: int = 200, kind: str = None, status: str = None):
    sql, args = "SELECT a.*, l.company FROM activities a LEFT JOIN leads l ON l.id=a.lead_id WHERE 1=1", []
    if kind:
        sql += " AND a.kind=?"; args.append(kind)
    if status:
        sql += " AND a.status=?"; args.append(status)
    return db.q(sql + " ORDER BY a.id DESC LIMIT ?", (*args, min(limit, 2000)))


@app.get("/api/export/activities.csv")
def export_activities():
    rows = activities(limit=2000)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["id", "created_at", "company", "lead_id", "kind", "channel", "status", "summary"], extrasaction="ignore")
    w.writeheader(); w.writerows(rows)
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=brn-activity-log.csv"})


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
    s["sequence"] = json.loads(s["sequence"])
    s["live_allowed"] = os.environ.get("BRN_ALLOW_LIVE") == "1"
    s["providers"] = {"claude": llm.enabled(), "email": bool(os.environ.get("RESEND_API_KEY")),
                      "voice": bool(os.environ.get("VAPI_API_KEY")), "places": bool(os.environ.get("GOOGLE_PLACES_API_KEY"))}
    return s


@app.post("/api/settings")
def save_settings(payload: dict = Body(...)):
    if payload.get("mode") == "live" and os.environ.get("BRN_ALLOW_LIVE") != "1":
        raise HTTPException(403, "Live sending is locked. Set BRN_ALLOW_LIVE=1 on the server once outreach is approved.")
    for k, v in payload.items():
        if k in db.DEFAULT_SETTINGS:
            db.set_setting(k, json.dumps(v) if k == "sequence" else v)
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


def _loop():
    while True:
        try:
            agent.tick()
        except Exception as e:
            db.log(None, "system", f"Scheduler error: {e}", channel="system", status="error")
        time.sleep(60)


if os.environ.get("BRN_SCHEDULER", "1") == "1":
    threading.Thread(target=_loop, daemon=True).start()
