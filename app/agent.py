"""The orchestrator: moves every lead through research -> scoring -> multi-channel sequence -> handoff.

Stages: new, researched, disqualified, in_sequence, awaiting_approval, replied, meeting_booked,
nurture, opted_out, completed, won, lost.
"""
import json
from datetime import datetime, timedelta, timezone

from . import compliance, db
from .providers import channels, llm, site_scan

TERMINAL_BLOCKS = ("on suppression list", "no lawful basis", "GDPR", "calls need opt-in", "lead is ")


def _settings(lead=None):
    s = {k: db.setting(k) for k in ("offer", "icp", "sender_name", "booking_link")}
    prods = db.products()
    key = (lead or {}).get("product") or db.setting("default_product")
    p = prods.get(key)
    if p:
        s["offer"], s["icp"], s["product_name"] = p["pitch"], p.get("icp", s["icp"]), p["name"]
        if p.get("booking_link"):
            s["booking_link"] = p["booking_link"]
    s["product_key"] = key
    return s


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def scan_website(lead):
    """Fixify leads: scan the lead's website once and store the confirmed problems in lead.extra.issues, so the
    research and email steps can use real, specific findings. Skipped if the lead already has issues (imported
    from a spreadsheet) or was scanned before. Never raises: a failed scan just leaves the lead as it was."""
    if not lead.get("website") or lead.get("product") not in (None, "", "fixify"):
        return lead
    try:
        extra = json.loads(lead.get("extra") or "{}") or {}
    except Exception:
        extra = {}
    if extra.get("issues") or extra.get("scanned_at"):
        return lead
    try:
        site_scan.BUDGET, site_scan.TIMEOUT, site_scan.WAITS = 25, 8, (1, 3)  # one scan must not stall the 1-minute tick
        R = site_scan.scan(lead["website"], 5)
        rows = [x for x in R.rows if x["severity"] != "low"
                and not any(w in x["problem"].lower() for w in ("confirm", "unconfirmed", "could not"))]
        extra.update(issues=[{k: x[k] for k in ("severity", "category", "problem", "fix")} for x in rows[:10]],
                     health_score=site_scan.score(R.rows), scanned_at=_iso(datetime.now(timezone.utc)))
        db.update_lead(lead["id"], extra=json.dumps(extra, ensure_ascii=False))
        db.log(lead["id"], "research", f"Website scan: score {extra['health_score']}, {len(rows)} confirmed issue(s)",
               payload={"issues": extra["issues"]})
        return {**lead, "extra": json.dumps(extra, ensure_ascii=False)}
    except Exception as e:
        db.log(lead["id"], "research", f"Website scan failed: {e}", status="error")
        return lead


def research_lead(lead):
    if lead.get("source") == "reddit":
        s = _settings(lead)
        draft = llm.intent_reply(lead, s)
        db.update_lead(lead["id"], stage="reply_manually", score=draft.get("score", 70),
                       research=json.dumps(draft, ensure_ascii=False), next_action="reply on the post", next_action_at=None)
        db.log(lead["id"], "research", f"Someone asked for this online. Drafted a reply for you to post: {draft.get('summary', '')}", payload=draft)
        return
    if not (lead.get("email") or lead.get("phone")):
        db.update_lead(lead["id"], stage="needs_contact", next_action=None, next_action_at=None)
        db.log(lead["id"], "stage_change", "Needs contact details: no email or phone yet. Add one and move it back to 'new'.")
        return
    lead = scan_website(lead)
    s = _settings(lead)
    try:
        r = llm.research(lead, s["offer"], s["icp"], s.get("product_key"))
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
    s = _settings(lead)
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
    """Send one step for real, log it, apply outcomes, and advance."""
    if step["channel"] == "email":
        try:
            res = channels.send_email(lead, content["subject"], content["body"], cta=_settings(lead)["booking_link"], cta_label=content.get("cta"))
        except Exception as e:
            db.log(lead["id"], "email_sent", f"Send failed, retrying in 1 hour: {e}", channel="email", status="error")
            db.update_lead(lead["id"], stage="in_sequence", next_action_at=_iso(at + timedelta(hours=1)))
            return
        res.pop("html", None)
        db.log(lead["id"], "email_sent", f"{step['action']}: {content['subject']}", channel="email", status=res["status"], payload=res)
        if res["status"] == "ok":
            db.ex("UPDATE leads SET emails_sent=emails_sent+1, last_contacted_at=? WHERE id=?", (db.now(), lead["id"]))
    else:
        try:
            res = channels.place_call(lead, content)
        except Exception as e:
            db.log(lead["id"], "call_completed", f"Call failed, retrying in 2 hours: {e}", channel="phone", status="error")
            db.update_lead(lead["id"], stage="in_sequence", next_action_at=_iso(at + timedelta(hours=2)))
            return
        if res["status"] == "skipped":
            db.log(lead["id"], "call_scheduled", f"Call not placed: AI calling isn't connected. Call {lead.get('phone') or 'them'} yourself (brief below), then set the status.",
                   channel="phone", status="ok", payload={"transcript": "Call brief\n" + json.dumps(content, ensure_ascii=False, indent=2), "script": content})
        else:
            db.ex("UPDATE leads SET last_contacted_at=? WHERE id=?", (db.now(), lead["id"]))
            db.log(lead["id"], "call_completed", f"Call outcome: {res.get('outcome')}", channel="phone", status=res["status"], payload={**res, "script": content})
            if apply_outcome(lead["id"], res.get("outcome")):
                return
    fresh = db.one("SELECT * FROM leads WHERE id=?", (lead["id"],))
    _advance(fresh, db.sequence_for(fresh), at)


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
    seq = db.sequence_for(lead)
    step = seq[lead["step"]]
    ok, reason = compliance.check(lead, step["channel"], at)
    if not ok:
        if reason == "outside local business hours":
            db.update_lead(lead["id"], next_action_at=_iso(compliance.next_window(lead.get("country"), step["channel"], at)))
            return
        if "cap reached" in reason:  # wait for tomorrow's quota, quietly (not an error)
            tomorrow = (at + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
            db.update_lead(lead["id"], next_action_at=_iso(compliance.next_window(lead.get("country"), step["channel"], tomorrow)))
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

    if db.setting("mode") != "live":  # anything but live waits for a person
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
        step = db.sequence_for(lead)[lead["step"]]
        deliver(lead, step, content, now)
    else:
        _advance(lead, db.sequence_for(lead), now)
    return True


AGENT_STAGES = ("new", "researched", "in_sequence", "awaiting_approval", "completed", "paused", "nurture")


def handle_reply(lead_id, text, meta=None):
    """Log and classify one reply. meta carries mailbox details (from, subject, message_id, received_at).
    meta["already_read"]: a human read it in the mailbox before the agent saw it. It is still recorded, but a
    status a human has already chosen (replied, meeting booked, won, lost) is left alone, except for opt-outs."""
    meta = dict(meta or {})
    already_read = meta.pop("already_read", False)
    intent = llm.classify_reply(text)
    db.log(lead_id, "email_reply", f"Reply ({intent.get('intent')}): {intent.get('summary', '')}", channel="email", payload={"text": text, **meta, **intent})
    lead = db.one("SELECT stage FROM leads WHERE id=?", (lead_id,)) or {}
    db.update_lead(lead_id, replied_at=meta.get("received_at") or db.now())
    if not already_read or lead.get("stage") in AGENT_STAGES or intent.get("intent") == "unsubscribe":
        apply_outcome(lead_id, intent.get("intent"))
    return intent


def tick(at=None):
    """One pass of the agent loop. Called every minute by the scheduler, or manually."""
    at = at or datetime.now(timezone.utc)
    done = 0
    if db.setting("agent_enabled") != "1":
        return 0
    for lead in db.q("SELECT * FROM leads WHERE stage='new' LIMIT 25"):
        research_lead(lead)
        done += 1
    due = db.q("SELECT * FROM leads WHERE stage='in_sequence' AND next_action_at IS NOT NULL AND next_action_at<=? ORDER BY next_action_at LIMIT 50", (_iso(at),))
    for lead in due:
        run_step(lead, at)
        done += 1
    return done
