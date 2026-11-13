"""Small background jobs, run once a minute whether or not the agent is on:
  * a brief emailed to whoever booked a meeting, shortly before it starts
  * after a meeting: mark it done, draft a follow-up email for approval, and add a task to record the outcome
  * an email when a task assigned to someone falls due
Nothing here contacts a lead by itself: follow-ups wait in Approvals like every other email."""
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import agent, db
from .providers import channels, llm, mailbox


def _iso(d):
    return d.isoformat(timespec="seconds")


def _when(iso, tz):
    """A meeting time written out in the time zone it was booked in."""
    d = datetime.fromisoformat(iso)
    try:
        d = d.astimezone(ZoneInfo(tz or "UTC"))
    except Exception:
        tz = "UTC"
    return d.strftime("%A %d %B, %H:%M") + f" ({tz or 'UTC'})"


def _person(name):
    """The account behind a name on a meeting or task, if they still have one."""
    return db.one("SELECT id, name, email FROM users WHERE name=? AND active=1", (name,)) if name else None


def _notify(to, subject, body):
    """An internal email to a teammate. Returns False when email is not connected or it could not be sent."""
    if not to or not channels.email_provider():
        return False
    try:
        channels.send_message(mailbox.build(db.setting("sender_name"), channels._sender() or mailbox.address(), {"to": to, "subject": subject, "body": body}))
        return True
    except Exception as e:
        db.log(None, "system", f"Could not email a reminder to {to}: {e}", channel="email", status="error")
        return False


def meeting_reminders(now):
    lead_min = int(db.setting("meeting_reminder_min") or 0)
    if lead_min <= 0:
        return 0
    due = db.q("SELECT * FROM meetings WHERE status='scheduled' AND reminded_at IS NULL AND starts_at>? AND starts_at<=?", (_iso(now), _iso(now + timedelta(minutes=lead_min))))
    for m in due:
        db.ex("UPDATE meetings SET reminded_at=? WHERE id=?", (_iso(now), m["id"]))  # first, so a failure can never repeat it every minute
        who = _person(m["created_by"])
        to = (who or {}).get("email") or db.setting("sender_email")
        lead = db.one("SELECT * FROM leads WHERE id=?", (m["lead_id"],)) if m["lead_id"] else None
        lines = [f"{m['title']}", f"When: {_when(m['starts_at'], m['timezone'])}", f"Join: {m['meet_link'] or 'no Meet link on this meeting'}",
                 "With: " + (", ".join(a["email"] for a in json.loads(m["attendees"] or "[]")) or "nobody invited")]
        if lead:
            r = json.loads(lead.get("research") or "{}") or {}
            reply = db.q("SELECT payload FROM activities WHERE lead_id=? AND kind='email_reply' ORDER BY id DESC LIMIT 1", (lead["id"],))
            lines += ["", f"About {lead['company']}" + (f" ({lead.get('contact_name')}, {lead.get('title')})" if lead.get("contact_name") and lead.get("title") else f" ({lead['contact_name']})" if lead.get("contact_name") else ""),
                      r.get("summary") or "", ("What may matter to them: " + "; ".join(r.get("pain_points") or [])) if r.get("pain_points") else "",
                      ("Their last message: " + (json.loads(reply[0]["payload"] or "{}").get("text") or "")[:600]) if reply else ""]
        if m.get("description"):
            lines += ["", "Notes on the invitation:", m["description"]]
        if _notify(to, f"In {lead_min} min: {m['title']}", "\n".join(x for x in lines if x is not None)):
            db.log(m["lead_id"], "meeting", f"Reminder sent to {to} for: {m['title']}", channel="calendar")
    return len(due)


def meeting_followups(now):
    done = db.q("SELECT * FROM meetings WHERE status='scheduled' AND followup_at IS NULL AND ends_at<=?", (_iso(now - timedelta(minutes=15)),))
    for m in done:
        db.ex("UPDATE meetings SET status='done', followup_at=? WHERE id=?", (_iso(now), m["id"]))
        lead = db.one("SELECT * FROM leads WHERE id=?", (m["lead_id"],)) if m["lead_id"] else None
        if not lead:
            continue
        who = _person(m["created_by"])
        db.ex("INSERT INTO tasks(lead_id, title, due_at, assigned_to, created_by, created_at) VALUES (?,?,?,?,?,?)",
              (lead["id"], f"Record how the meeting with {lead['company']} went, and the next step", _iso(now), who and who["id"], "Agent", db.now()))
        if db.setting("meeting_followup") != "1" or not lead.get("email") or lead["stage"] in ("opted_out", "lost"):
            continue
        try:
            draft = llm.write_followup(lead, json.loads(lead.get("research") or "{}") or {}, agent._settings(lead), m)
        except Exception as e:
            db.log(lead["id"], "system", f"Could not draft the meeting follow-up: {e}", status="error")
            continue
        content = {"subject": draft["subject"], "body": draft["body"], "to": lead["email"], "meeting_id": m["id"], "meeting_title": m["title"], "meeting_at": m["starts_at"]}
        db.ex("DELETE FROM approvals WHERE lead_id=? AND state='pending' AND action='followup'", (lead["id"],))
        db.ex("INSERT INTO approvals(lead_id, channel, action, content, created_at) VALUES (?,?,?,?,?)", (lead["id"], "email", "followup", json.dumps(content, ensure_ascii=False), db.now()))
        db.log(lead["id"], "approval", f"Meeting over: drafted a follow-up email, waiting in Approvals > Replies ({m['title']})", channel="email", status="pending_approval", payload=content)
    return len(done)


def task_reminders(now):
    due = db.q("""SELECT t.*, u.email, u.name assignee, l.company FROM tasks t JOIN users u ON u.id=t.assigned_to LEFT JOIN leads l ON l.id=t.lead_id
                  WHERE t.done_at IS NULL AND t.notified_at IS NULL AND t.due_at IS NOT NULL AND t.due_at<=? AND u.active=1""", (_iso(now),))
    for t in due:
        db.ex("UPDATE tasks SET notified_at=? WHERE id=?", (_iso(now), t["id"]))
        _notify(t["email"], f"Task due: {t['title']}", "\n".join(x for x in [t["title"], f"Lead: {t['company']}" if t.get("company") else "", t.get("notes") or "",
                                                                             "", "Open the Tasks page in brn. pipeline to tick it off."] if x is not None))
    return len(due)


def run(now=None):
    now = now or datetime.now(timezone.utc)
    return {"reminders": meeting_reminders(now), "followups": meeting_followups(now), "tasks": task_reminders(now)}
