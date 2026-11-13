"""Reports and forecast, tasks, email templates, the public booking page and the Resend delivery webhook."""
import base64
import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse, Response

from . import agent, auth, compliance, db
from . import main as M
from . import invites, rules

router = APIRouter()


def _iso(d):
    return d.isoformat(timespec="seconds")


def _who():
    u = db.actor.get() or {}
    return None if u.get("implicit") else u.get("name")


# ---------- reports and forecast ----------
STAGE_CHANCE = {"new": 0.02, "needs_contact": 0.02, "reply_manually": 0.05, "in_sequence": 0.05, "awaiting_approval": 0.05, "paused": 0.03, "completed": 0.03,
                "nurture": 0.05, "replied": 0.2, "meeting_booked": 0.4, "won": 1.0}   # how likely a deal at each stage is to close
POSITIVE = ("interested", "meeting", "question")
STEP_NAMES = {"intro": "First email", "follow_up_1": "Follow-up 1", "follow_up_2": "Follow-up 2", "breakup": "Last note", "reply": "Answer to a reply",
              "followup": "Meeting follow-up", "by hand": "Written by hand"}


def _rate(part, whole):
    return round(part / whole * 100, 1) if whole else None


def _action(a, p):
    if p.get("action"):
        return p["action"]
    if p.get("manual"):
        return "by hand"
    head = (a["summary"] or "").split(":", 1)[0].strip()
    return head if head in STEP_NAMES else "by hand"


@router.get("/api/reports")
def reports(days: int = 30, product: str = None):
    """What the outreach is producing. Rates are by cohort: of the emails sent in the period, how many were answered."""
    days = max(1, min(int(days), 365))
    since = _iso(datetime.now(timezone.utc) - timedelta(days=days))
    prods = db.products()
    lw, largs = ("AND l.product=?", [product]) if product else ("", [])
    rows = db.q(f"""SELECT a.id, a.lead_id, a.kind, a.summary, a.payload, a.created_at, a.actor, l.product FROM activities a JOIN leads l ON l.id=a.lead_id
                    WHERE ((a.kind='email_sent' AND a.status='ok') OR a.kind IN ('email_reply','email_bounce')) AND a.created_at>=? {lw} ORDER BY a.id""", (since, *largs))
    blank = lambda: {"sent": 0, "replies": 0, "positive": 0}
    step, variant, pipe, person = {}, {}, {}, {}
    totals = {"sent": 0, "replies": 0, "positive": 0, "bounced": 0, "leads_emailed": set(), "leads_replied": set()}
    last = {}   # lead -> the email a reply would be answering
    for a in rows:
        p = json.loads(a["payload"] or "{}")
        if a["kind"] == "email_sent":
            act = _action(a, p)
            keys = [(step, act), (pipe, a["product"] or ""), (person, a["actor"] or "Agent (sent automatically)")]
            if p.get("variant") in ("A", "B"):
                keys.append((variant, (act, p["variant"])))
            for table, key in keys:
                table.setdefault(key, blank())["sent"] += 1
            totals["sent"] += 1; totals["leads_emailed"].add(a["lead_id"])
            last[a["lead_id"]] = {"keys": keys, "answered": False}
        elif a["kind"] == "email_reply":
            totals["leads_replied"].add(a["lead_id"])
            e = last.get(a["lead_id"])
            if e and not e["answered"]:   # one answer per email
                e["answered"] = True
                good = p.get("intent") in POSITIVE
                totals["replies"] += 1; totals["positive"] += good
                for table, key in e["keys"]:
                    table[key]["replies"] += 1; table[key]["positive"] += good
        elif p.get("hard", True):
            totals["bounced"] += 1
    meetings = db.q(f"""SELECT m.created_by, l.product FROM meetings m LEFT JOIN leads l ON l.id=m.lead_id
                        WHERE m.created_at>=? AND m.status<>'cancelled' {lw.replace('AND', 'AND', 1)}""", (since, *largs))
    won = db.q(f"SELECT l.product, l.deal_value FROM leads l WHERE l.stage='won' AND l.closed_at>=? {lw}", (since, *largs))
    for m in meetings:
        person.setdefault(m["created_by"] or "Booking page / unknown", blank()).setdefault("meetings", 0)
        person[m["created_by"] or "Booking page / unknown"]["meetings"] = person[m["created_by"] or "Booking page / unknown"].get("meetings", 0) + 1
    out_pipe = []
    for key in sorted(set(pipe) | {m["product"] or "" for m in meetings} | {w["product"] or "" for w in won}, key=lambda k: -pipe.get(k, blank())["sent"]):
        r = pipe.get(key, blank())
        out_pipe.append({"product": key, "name": prods.get(key, {}).get("name", "No pipeline"), **r, "rate": _rate(r["replies"], r["sent"]),
                         "meetings": sum(1 for m in meetings if (m["product"] or "") == key), "won": sum(1 for w in won if (w["product"] or "") == key),
                         "won_value": sum(w["deal_value"] or 0 for w in won if (w["product"] or "") == key)})
    order = list(STEP_NAMES)
    return {
        "days": days, "currency": db.setting("currency") or "USD",
        "totals": {"sent": totals["sent"], "leads_emailed": len(totals["leads_emailed"]), "replies": totals["replies"], "rate": _rate(totals["replies"], totals["sent"]),
                   "positive": totals["positive"], "positive_rate": _rate(totals["positive"], totals["sent"]), "bounced": totals["bounced"],
                   "bounce_rate": _rate(totals["bounced"], totals["sent"]), "meetings": len(meetings), "won": len(won), "won_value": sum(w["deal_value"] or 0 for w in won)},
        "by_pipeline": out_pipe,
        "by_step": [{"action": k, "name": STEP_NAMES.get(k, k), **v, "rate": _rate(v["replies"], v["sent"])} for k, v in sorted(step.items(), key=lambda x: order.index(x[0]) if x[0] in order else 99)],
        "by_variant": [{"action": k[0], "name": STEP_NAMES.get(k[0], k[0]), "variant": k[1], **v, "rate": _rate(v["replies"], v["sent"])}
                       for k, v in sorted(variant.items(), key=lambda x: (order.index(x[0][0]) if x[0][0] in order else 99, x[0][1]))],
        "by_person": [{"name": k, "sent": v["sent"], "replies": v["replies"], "rate": _rate(v["replies"], v["sent"]), "meetings": v.get("meetings", 0)}
                      for k, v in sorted(person.items(), key=lambda x: -(x[1]["sent"] + x[1].get("meetings", 0)))],
        "forecast": forecast(product),
    }


def forecast(product=None):
    """Open deals, what they are likely to bring in (value x the chance for their stage), and when."""
    lw, largs = ("AND product=?", [product]) if product else ("", [])
    deals = db.q(f"""SELECT id, company, stage, product, deal_value, expected_close FROM leads
                     WHERE deal_value IS NOT NULL AND deal_value>0 AND stage NOT IN ('lost','disqualified','opted_out') {lw}""", largs)
    open_ = [d for d in deals if d["stage"] != "won"]
    months = {}
    for d in open_:
        key = (d["expected_close"] or "")[:7] or "no date"
        m = months.setdefault(key, {"month": key, "deals": 0, "value": 0, "weighted": 0})
        m["deals"] += 1; m["value"] += d["deal_value"]; m["weighted"] += d["deal_value"] * STAGE_CHANCE.get(d["stage"], 0)
    return {"open_deals": len(open_), "open_value": sum(d["deal_value"] for d in open_),
            "weighted": round(sum(d["deal_value"] * STAGE_CHANCE.get(d["stage"], 0) for d in open_), 2),
            "won_value": sum(d["deal_value"] for d in deals if d["stage"] == "won"), "won_deals": sum(1 for d in deals if d["stage"] == "won"),
            "by_month": [{**m, "weighted": round(m["weighted"], 2)} for _, m in sorted(months.items(), key=lambda x: (x[0] == "no date", x[0]))],
            "top": [{**d, "chance": STAGE_CHANCE.get(d["stage"], 0)} for d in sorted(open_, key=lambda d: -d["deal_value"])[:8]],
            "chances": STAGE_CHANCE, "no_value": db.one(f"SELECT COUNT(*) n FROM leads WHERE stage IN ('replied','meeting_booked') AND (deal_value IS NULL OR deal_value=0) {lw}", largs)["n"]}


# ---------- rules ----------
@router.get("/api/rules")
def get_rules():
    st = rules.state()
    return {"builtin": [{**r, **st["builtin"][r["key"]]} for r in rules.BUILTIN], "custom": st["custom"],
            "fields": rules.FIELDS, "tests": rules.TESTS, "outcomes": rules.OUTCOMES}


@router.post("/api/rules")
def save_rules(p: dict = Body(...)):
    rules.save(p)
    db.log(None, "system", "Lead rules changed", channel="system")
    return {"ok": True}


@router.post("/api/rules/preview")
def preview_rules(p: dict = Body(default={})):
    """Which leads the agent is working on break a rule now. With apply: move them accordingly."""
    out = rules.preview(bool(p.get("apply")))
    if out["moved"]:
        db.log(None, "system", f"Rules applied to current leads: {out['moved']} moved", channel="system")
    return out


# ---------- tasks ----------
TASK_SQL = """SELECT t.*, l.company, u.name assignee FROM tasks t LEFT JOIN leads l ON l.id=t.lead_id LEFT JOIN users u ON u.id=t.assigned_to"""


@router.get("/api/tasks")
def tasks(scope: str = "open", lead_id: int = None, mine: int = 0):
    w, args = ["1=1"], []
    if scope == "open":
        w.append("t.done_at IS NULL")
    elif scope == "done":
        w.append("t.done_at IS NOT NULL")
    if lead_id:
        w.append("t.lead_id=?"); args.append(lead_id)
    me = db.actor.get() or {}
    if mine and me.get("id"):
        w.append("(t.assigned_to=? OR t.assigned_to IS NULL)"); args.append(me["id"])
    order = "t.done_at DESC" if scope == "done" else "t.due_at IS NULL, t.due_at, t.id"
    return {"tasks": db.q(f"{TASK_SQL} WHERE {' AND '.join(w)} ORDER BY {order} LIMIT 500", args),
            "people": [{"id": u["id"], "name": u["name"]} for u in db.q("SELECT id, name FROM users WHERE active=1 ORDER BY name")], "me": me.get("id")}


def _due(value):
    if not value:
        return None
    try:
        return M._utc(value)
    except ValueError:
        raise HTTPException(400, "That due date is not a date.")


@router.post("/api/tasks")
def add_task(p: dict = Body(...)):
    title = (p.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "Write what needs doing.")
    me = db.actor.get() or {}
    tid = db.ex("INSERT INTO tasks(lead_id, title, notes, due_at, assigned_to, created_by, created_at) VALUES (?,?,?,?,?,?,?)",
                (p.get("lead_id") or None, title, p.get("notes") or None, _due(p.get("due_at")), p.get("assigned_to") or me.get("id"), _who(), db.now()))
    if p.get("lead_id"):
        db.log(int(p["lead_id"]), "task", f"Task added: {title}", channel="system")
    return {"id": tid}


@router.patch("/api/tasks/{task_id}")
def edit_task(task_id: int, p: dict = Body(...)):
    t = db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not t:
        raise HTTPException(404)
    sets = {}
    if "title" in p and (p["title"] or "").strip():
        sets["title"] = p["title"].strip()
    if "notes" in p:
        sets["notes"] = p["notes"] or None
    if "due_at" in p:
        sets["due_at"], sets["notified_at"] = _due(p["due_at"]), None
    if "assigned_to" in p:
        sets["assigned_to"], sets["notified_at"] = p["assigned_to"] or None, None
    if "done" in p:
        sets["done_at"], sets["done_by"] = (db.now(), _who()) if p["done"] else (None, None)
        if p["done"] and t["lead_id"]:
            db.log(t["lead_id"], "task", f"Task done: {t['title']}", channel="system")
    if sets:
        db.ex(f"UPDATE tasks SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), task_id))
    return {"ok": True}


@router.delete("/api/tasks/{task_id}")
def delete_task(task_id: int):
    db.ex("DELETE FROM tasks WHERE id=?", (task_id,))
    return {"ok": True}


# ---------- email templates ----------
STARTERS = [
    ("Send more details", "More on {{company}} and brn dynamics",
     "Hi {{first_name}},\n\nThanks for your interest. Here is a short overview of how we would approach this for {{company}}:\n\n[add the details]\n\nIf it is easier to talk it through, you can pick a time here: {{booking_link}}\n\nBest regards,\n{{sender_name}}"),
    ("Propose a call", "A quick call?",
     "Hi {{first_name}},\n\nWould a 15-minute call this week be useful? You can pick a time that suits you here: {{booking_link}}\n\nBest regards,\n{{sender_name}}"),
    ("Checking in", "Checking in",
     "Hi {{first_name}},\n\nI wanted to check in and see whether this is still on your radar at {{company}}. Happy to pick it up whenever the timing is right.\n\nBest regards,\n{{sender_name}}"),
]


@router.get("/api/templates")
def templates():
    if db.setting("templates_seeded") != "1":   # a few to start from, once
        db.set_setting("templates_seeded", "1")
        if not db.one("SELECT 1 x FROM templates LIMIT 1"):
            for name, subject, body in STARTERS:
                db.ex("INSERT INTO templates(name, subject, body, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?)", (name, subject, body, None, db.now(), db.now()))
    return {"templates": db.q("SELECT * FROM templates ORDER BY name"), "placeholders": ["first_name", "name", "company", "sender_name", "booking_link"],
            "values": {"sender_name": db.setting("sender_name"), "booking_link": db.setting("booking_link")}}


@router.post("/api/templates")
def save_template(p: dict = Body(...)):
    name = (p.get("name") or "").strip()
    if not name or not (p.get("body") or "").strip():
        raise HTTPException(400, "A template needs a name and some text.")
    if p.get("id"):
        db.ex("UPDATE templates SET name=?, subject=?, body=?, updated_at=? WHERE id=?", (name, p.get("subject") or "", p["body"], db.now(), int(p["id"])))
        return {"id": int(p["id"])}
    return {"id": db.ex("INSERT INTO templates(name, subject, body, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                        (name, p.get("subject") or "", p["body"], _who(), db.now(), db.now()))}


@router.delete("/api/templates/{template_id}")
def delete_template(template_id: int):
    db.ex("DELETE FROM templates WHERE id=?", (template_id,))
    return {"ok": True}


# ---------- public booking page ----------
_booked = {}   # ip -> times of recent bookings


def _zone():
    try:
        return ZoneInfo(db.setting("booking_timezone") or "UTC")
    except Exception:
        return timezone.utc


def _hm(text, default):
    try:
        h, m = (text or default).split(":")
        return int(h), int(m)
    except ValueError:
        h, m = default.split(":")
        return int(h), int(m)


def open_slots():
    """Free start times, as UTC ISO strings, from the hours set in Settings minus what is already in the calendar."""
    tz, now = _zone(), datetime.now(timezone.utc)
    length = max(10, int(db.setting("booking_slot_min") or 30))
    earliest = now + timedelta(hours=float(db.setting("booking_notice_hours") or 0))
    ahead = max(1, min(int(db.setting("booking_days_ahead") or 14), 60))
    days = {int(d) for d in (db.setting("booking_days") or "").split(",") if d.strip().isdigit()}
    (sh, sm), (eh, em) = _hm(db.setting("booking_start"), "10:00"), _hm(db.setting("booking_end"), "17:00")
    end_range = now + timedelta(days=ahead + 1)
    busy = [(datetime.fromisoformat(m["starts_at"]), datetime.fromisoformat(m["ends_at"]))
            for m in db.q("SELECT starts_at, ends_at FROM meetings WHERE status='scheduled' AND ends_at>? AND starts_at<?", (_iso(now), _iso(end_range)))]
    out, today = [], now.astimezone(tz).date()
    for i in range(ahead + 1):
        day = today + timedelta(days=i)
        if day.weekday() not in days:
            continue
        t = datetime(day.year, day.month, day.day, sh, sm, tzinfo=tz)
        stop = datetime(day.year, day.month, day.day, eh, em, tzinfo=tz)
        while t + timedelta(minutes=length) <= stop:
            s, e = t.astimezone(timezone.utc), (t + timedelta(minutes=length)).astimezone(timezone.utc)
            if s >= earliest and not any(s < b_end and e > b_start for b_start, b_end in busy):
                out.append(_iso(s))
            t += timedelta(minutes=length)
    return out, length


@router.get("/book")
def book_page():
    return FileResponse(os.path.join(M.STATIC, "book.html"))


EMBED_JS = r"""(function(){
  var me=document.currentScript, base=new URL(me.src).origin, lead=new URLSearchParams(location.search).get('l');
  var box=document.getElementById('brn-booking')||me.parentNode.insertBefore(document.createElement('div'),me);
  var f=document.createElement('iframe');
  f.src=base+'/book?embed=1'+(lead?'&l='+encodeURIComponent(lead):'');
  f.title='Book a call'; f.loading='lazy';
  f.style.cssText='width:100%;border:0;display:block;height:560px;background:transparent';
  box.appendChild(f);
  addEventListener('message',function(e){ if(e.origin===base&&e.data&&e.data.brnBookHeight) f.style.height=e.data.brnBookHeight+'px'; });
})();"""


@router.get("/book/embed.js")
def book_embed():
    """Put the booking page on your own website: <div id="brn-booking"></div><script src=".../book/embed.js" async></script>"""
    return Response(EMBED_JS, media_type="application/javascript", headers={"Cache-Control": "public, max-age=3600"})


@router.get("/api/book/info")
def book_info(l: str = None):
    if db.setting("booking_enabled") != "1":
        return {"enabled": False, "company": db.setting("sender_name")}
    slots, length = open_slots()
    lead = agent.lead_from_token(l) if l else None
    return {"enabled": True, "title": db.setting("booking_title"), "company": db.setting("sender_name"), "minutes": length, "slots": slots,
            "meet": bool(invites.meet_links()), "lead": {"name": lead.get("contact_name") or "", "email": lead.get("email") or "", "company": lead["company"]} if lead else None}


@router.post("/api/book")
def book(request: Request, p: dict = Body(...)):
    """A lead picks a time on the public page. Only a free slot from open_slots() is accepted."""
    if db.setting("booking_enabled") != "1":
        raise HTTPException(403, "Online booking is switched off.")
    if p.get("website"):   # a field people never see; only scripts fill it in
        return {"ok": True}
    ip, now = (request.client.host if request.client else ""), time.time()
    recent = [t for t in _booked.get(ip, []) if now - t < 3600]
    if len(recent) >= 5:
        raise HTTPException(429, "Too many bookings from here in the last hour. Please try again later.")
    name, email = (p.get("name") or "").strip()[:120], (p.get("email") or "").strip().lower()[:200]
    if not name or not re.fullmatch(r"[\w.+'-]+@[\w-]+(\.[\w-]+)+", email):
        raise HTTPException(400, "Please enter your name and a valid email address.")
    slots, length = open_slots()
    try:
        slot = M._utc(p.get("slot"))
    except (ValueError, TypeError):
        raise HTTPException(400, "Please pick a time.")
    if slot not in slots:
        raise HTTPException(409, "That time was just taken. Please pick another one.")
    lead = (agent.lead_from_token(p["l"]) if p.get("l") else None) or db.one("SELECT * FROM leads WHERE lower(email)=?", (email,))
    if not lead:
        company = (p.get("company") or "").strip()[:160] or name
        lid = M._insert({"company": company, "contact_name": name, "email": email, "source": "booking", "consent_basis": "opt_in",
                         "notes": (p.get("note") or "").strip()[:1000] or None}, "booking")
        lead = db.one("SELECT * FROM leads WHERE id=?", (lid,)) if lid else db.one("SELECT * FROM leads WHERE lower(email)=?", (email,))
    elif not lead.get("email"):
        db.update_lead(lead["id"], email=email)
    note = (p.get("note") or "").strip()[:1000]
    tz = (p.get("tz") or db.setting("booking_timezone") or "UTC")[:60]
    f = {"title": f"{db.setting('booking_title')}: {lead['company'] if lead else name}", "starts_at": slot,
         "ends_at": _iso(datetime.fromisoformat(slot) + timedelta(minutes=length)), "timezone": tz, "attendees": [email],
         "description": (f"Booked online by {name}." + (f"\n\n{note}" if note else "")), "location": ""}
    res = M.book_meeting(f, lead, True, None, by="Booking page")
    _booked[ip] = recent + [now]
    return {"ok": True, "starts_at": slot, "minutes": length, "meet_link": res.get("meet_link"), "invited": res.get("invited")}


# ---------- Resend delivery webhook (bounces and spam complaints) ----------
def _signed_by_resend(request_headers, body):
    """Resend signs webhooks the Svix way: HMAC-SHA256 over "id.timestamp.body" with the endpoint's secret."""
    secret = os.environ.get("RESEND_WEBHOOK_SECRET") or ""
    if not secret:
        return False
    try:
        key = base64.b64decode(secret.split("_", 1)[1] if secret.startswith("whsec_") else secret)
        msg_id, stamp = request_headers["svix-id"], request_headers["svix-timestamp"]
        if abs(time.time() - int(stamp)) > 600:
            return False
        want = base64.b64encode(hmac.new(key, f"{msg_id}.{stamp}.".encode() + body, hashlib.sha256).digest()).decode()
        return any(hmac.compare_digest(want, part.split(",", 1)[-1]) for part in request_headers["svix-signature"].split())
    except Exception:
        return False


@router.post("/webhooks/resend")
async def resend_webhook(request: Request):
    """Add this address in Resend > Webhooks (events: email.bounced, email.complained) and set RESEND_WEBHOOK_SECRET
    to the signing secret it shows. Unsigned calls are refused."""
    body = await request.body()
    if not _signed_by_resend(request.headers, body):
        raise HTTPException(403, "Not signed by Resend. Set RESEND_WEBHOOK_SECRET to the webhook's signing secret.")
    event = json.loads(body or b"{}")
    kind, data = event.get("type"), event.get("data") or {}
    to = data.get("to") or []
    to = [to] if isinstance(to, str) else to
    if kind == "email.bounced":
        b = data.get("bounce") or {}
        hard = (b.get("type") or "Permanent").lower() != "transient"
        for a in to:
            agent.handle_bounce(a, b.get("message") or b.get("subType") or "", hard)
    elif kind == "email.complained":   # they pressed "spam": never write to them again
        for a in to:
            compliance.suppress(a, "marked as spam")
            for lead in db.q("SELECT id FROM leads WHERE lower(email)=?", (a.lower(),)):
                db.log(lead["id"], "email_bounce", f"{a} marked our email as spam: added to the do-not-contact list", channel="email", status="error")
                agent.apply_outcome(lead["id"], "unsubscribe")
    return {"ok": True}
