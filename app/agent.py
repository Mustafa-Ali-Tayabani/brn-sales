"""The orchestrator: moves every lead through research -> scoring -> multi-channel sequence -> handoff.

Stages: new, researched, disqualified, in_sequence, awaiting_approval, replied, meeting_booked,
nurture, opted_out, completed, won, lost.
"""
import json
from datetime import datetime, timedelta, timezone

from . import compliance, db
from .providers import channels, llm

TERMINAL_BLOCKS = ("on suppression list", "no lawful basis", "GDPR", "calls need opt-in", "lead is ")


def _settings():
    return {k: db.setting(k) for k in ("offer", "icp", "sender_name", "booking_link")}


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def research_lead(lead):
    s = _settings()
    try:
        r = llm.research(lead, s["offer"], s["icp"])
    except Exception as e:  # keep the pipeline moving; log the failure
        db.log(lead["id"], "research", f"Research failed: {e}", status="error")
        return
    score = int(r.get("score") or 0)
    db.update_lead(lead["id"], research=json.dumps(r, ensure_ascii=False), score=score)
    db.log(lead["id"], "research", r.get("summary", ""), payload=r)
    if score < int(db.setting("min_score")):
        db.update_lead(lead["id"], stage="disqualified", next_action=None, next_action_at=None)
        db.log(lead["id"], "stage_change", f"Disqualified (score {score}): {r.get('score_reason', '')}", status="ok")
    else:
        db.update_lead(lead["id"], stage="in_sequence", step=0, next_action="step_0", next_action_at=_iso(datetime.now(timezone.utc)))
        db.log(lead["id"], "stage_change", f"Qualified (score {score}), sequence started")


def _draft(lead, step):
    s = _settings()
    r = json.loads(lead["research"] or "{}")
    if step["channel"] == "email":
        return llm.write_email(lead, r, step["action"], s)
    return llm.call_script(lead, r, s)


def _advance(lead, seq, at):
    nxt = lead["step"] + 1
    if nxt >= len(seq):
        db.update_lead(lead["id"], stage="completed", step=nxt, next_action=None, next_action_at=None)
        db.log(lead["id"], "stage_change", "Sequence finished with no reply")
        return
    gap = seq[nxt]["day"] - seq[lead["step"]]["day"]
    db.update_lead(lead["id"], stage="in_sequence", step=nxt, next_action=f"step_{nxt}",
                   next_action_at=_iso(at + timedelta(days=gap)))


def deliver(lead, step, content, at):
    """Actually send (or dry-run) one step, log it, apply outcomes, and advance."""
    if step["channel"] == "email":
        res = channels.send_email(lead, content["subject"], content["body"])
        db.log(lead["id"], "email_sent", f"{step['action']}: {content['subject']}", channel="email", status=res["status"], payload=res)
    else:
        res = channels.place_call(lead, content)
        db.log(lead["id"], "call_completed", f"Call outcome: {res.get('outcome')}", channel="phone", status=res["status"], payload={**res, "script": content})
        if apply_outcome(lead["id"], res.get("outcome")):
            return
    _advance(db.one("SELECT * FROM leads WHERE id=?", (lead["id"],)), json.loads(db.setting("sequence")), at)


def apply_outcome(lead_id, outcome):
    """Map a call outcome or reply intent to a stage. Returns True when the sequence should stop."""
    stop = {"meeting": "meeting_booked", "interested": "replied", "not_now": "nurture",
            "not_interested": "lost", "unsubscribe": "opted_out", "wrong_person": "replied", "question": "replied"}
    if outcome not in stop:
        return False
    stage = stop[outcome]
    db.update_lead(lead_id, stage=stage, next_action=("human_followup" if stage in ("replied", "meeting_booked") else None), next_action_at=None)
    db.log(lead_id, "stage_change", f"Moved to {stage} ({outcome})")
    if outcome == "unsubscribe":
        lead = db.one("SELECT * FROM leads WHERE id=?", (lead_id,))
        for v in (lead.get("email"), lead.get("phone")):
            if v:
                compliance.suppress(v, "opted out")
    return True


def run_step(lead, at):
    seq = json.loads(db.setting("sequence"))
    step = seq[lead["step"]]
    ok, reason = compliance.check(lead, step["channel"], at)
    if not ok:
        if reason == "outside local business hours":
            db.update_lead(lead["id"], next_action_at=_iso(compliance.next_window(lead.get("country"), step["channel"], at)))
            return
        if "cap reached" in reason:
            db.update_lead(lead["id"], next_action_at=_iso(at + timedelta(hours=12)))
            db.log(lead["id"], "blocked", f"Deferred: {reason}", channel=step["channel"], status="blocked")
            return
        if reason.startswith(("no email", "no phone")):
            db.log(lead["id"], "blocked", f"Skipped {step['action']}: {reason}", channel=step["channel"], status="blocked")
            _advance(lead, seq, at)
            return
        db.update_lead(lead["id"], stage="disqualified" if not reason.startswith("lead is") else lead["stage"], next_action=None, next_action_at=None)
        db.log(lead["id"], "blocked", f"Blocked: {reason}", channel=step["channel"], status="blocked")
        return

    try:
        content = _draft(lead, step)
    except Exception as e:
        db.log(lead["id"], "system", f"Drafting failed: {e}", status="error")
        db.update_lead(lead["id"], next_action_at=_iso(at + timedelta(hours=1)))
        return

    if db.setting("mode") == "approval":
        db.ex("INSERT INTO approvals(lead_id, channel, action, content, created_at) VALUES (?,?,?,?,?)",
              (lead["id"], step["channel"], step["action"], json.dumps(content, ensure_ascii=False), db.now()))
        db.update_lead(lead["id"], stage="awaiting_approval", next_action="approval", next_action_at=None)
        db.log(lead["id"], "approval", f"Drafted {step['channel']} '{step['action']}', waiting for approval", channel=step["channel"], status="pending_approval", payload=content)
        return
    deliver(lead, step, content, at)


def decide(approval_id, approve, edited=None):
    a = db.one("SELECT * FROM approvals WHERE id=?", (approval_id,))
    if not a or a["state"] != "pending":
        return False
    lead = db.one("SELECT * FROM leads WHERE id=?", (a["lead_id"],))
    content = edited or json.loads(a["content"])
    db.ex("UPDATE approvals SET state=?, decided_at=?, content=? WHERE id=?",
          ("approved" if approve else "rejected", db.now(), json.dumps(content, ensure_ascii=False), approval_id))
    db.log(lead["id"], "approval", f"{'Approved' if approve else 'Rejected'} {a['channel']} '{a['action']}'", channel=a["channel"])
    now = datetime.now(timezone.utc)
    lead["stage"] = "in_sequence"
    if approve:
        step = json.loads(db.setting("sequence"))[lead["step"]]
        deliver(lead, step, content, now)
    else:
        _advance(lead, json.loads(db.setting("sequence")), now)
    return True


def handle_reply(lead_id, text):
    intent = llm.classify_reply(text)
    db.log(lead_id, "email_reply", f"Reply ({intent.get('intent')}): {intent.get('summary', '')}", channel="email", payload={"text": text, **intent})
    apply_outcome(lead_id, intent.get("intent"))
    return intent


def tick(at=None):
    """One pass of the agent loop. Called every minute by the scheduler, or manually."""
    at = at or datetime.now(timezone.utc)
    done = 0
    for lead in db.q("SELECT * FROM leads WHERE stage='new' LIMIT 25"):
        research_lead(lead)
        done += 1
    due = db.q("SELECT * FROM leads WHERE stage='in_sequence' AND next_action_at IS NOT NULL AND next_action_at<=? ORDER BY next_action_at LIMIT 50", (_iso(at),))
    for lead in due:
        run_step(lead, at)
        done += 1
    return done


def simulate(days=14):
    """Demo helper: replays the agent loop over a virtual clock (in hours) so you can watch a full sequence."""
    start = datetime.now(timezone.utc)
    total = 0
    for h in range(days * 24):
        total += tick(start + timedelta(hours=h))
    return total
