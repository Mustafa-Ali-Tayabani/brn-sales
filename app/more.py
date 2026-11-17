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
from . import invites, quality, rules
from .providers import sources, voice
from .providers import llm

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
    rows = db.q(f"""SELECT a.id, a.lead_id, a.kind, a.summary, a.payload, a.created_at, a.actor, l.product, l.source, l.extra FROM activities a JOIN leads l ON l.id=a.lead_id
                    WHERE ((a.kind='email_sent' AND a.status='ok') OR a.kind IN ('email_reply','email_bounce')) AND a.created_at>=? {lw} ORDER BY a.id""", (since, *largs))
    blank = lambda: {"sent": 0, "replies": 0, "positive": 0}
    approved = {}   # lead -> [(id of the "Approved ..." entry, who)]: an email sent right after one was approved by a person, not sent by the agent alone
    for ap in db.q("SELECT id, lead_id, actor FROM activities WHERE kind='approval' AND summary LIKE ? AND created_at>=? ORDER BY id", ("Approved %", since)):
        approved.setdefault(ap["lead_id"], []).append((ap["id"], ap["actor"]))
    def sender(a, p):
        if a["actor"]:
            return a["actor"]
        by = next((who for aid, who in reversed(approved.get(a["lead_id"], [])) if 0 < a["id"] - aid <= 6), False)
        if by is not False or p.get("manual"):
            return by or "Approved by hand (shared sign-in)"
        return "Agent (sent automatically)"
    step, variant, pipe, person, how = {}, {}, {}, {}, {}
    totals = {"sent": 0, "replies": 0, "positive": 0, "bounced": 0, "leads_emailed": set(), "leads_replied": set()}
    last = {}   # lead -> the email a reply would be answering
    for a in rows:
        p = json.loads(a["payload"] or "{}")
        if a["kind"] == "email_sent":
            act = _action(a, p)
            keys = [(step, act), (pipe, a["product"] or ""), (person, sender(a, p)),
                    (how, agent.SIGNALS.get(agent.signal(a)) or "Fits the profile (no signal)")]
            if p.get("variant") in ("A", "B"):
                keys.append((variant, (act, p["variant"])))
            for table, key in keys:
                table.setdefault(key, blank())["sent"] += 1
            totals["sent"] += 1; totals["leads_emailed"].add(a["lead_id"])
            last[a["lead_id"]] = {"keys": keys, "answered": False}
        elif a["kind"] == "email_reply":
            if p.get("intent") == "auto_reply":
                continue   # an out-of-office, a ticket robot or a survey is not a reply
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
        "by_signal": [{"name": k, **v, "rate": _rate(v["replies"], v["sent"])} for k, v in sorted(how.items(), key=lambda x: -x[1]["sent"])],
        "by_person": [{"name": k, "sent": v["sent"], "replies": v["replies"], "rate": _rate(v["replies"], v["sent"]), "meetings": v.get("meetings", 0)}
                      for k, v in sorted(person.items(), key=lambda x: -(x[1]["sent"] + x[1].get("meetings", 0)))],
        "forecast": forecast(product), "funnel": funnel(product), "countries": by_country(product),
        "pipelines": [{"key": k, "name": p["name"], "partner": bool(p.get("partner"))} for k, p in prods.items()], "product": product or "",
    }


def by_country(product=None):
    """Leads per country (all time): how many there are, how many were contacted, replied, and reached a meeting or a win."""
    out = {}
    for l in db.q("SELECT country, stage, emails_sent, last_contacted_at, replied_at FROM leads" + (" WHERE product=?" if product else ""), [product] if product else []):
        c = M.countries.code(l["country"]) or ""
        o = out.setdefault(c, {"code": c, "name": M.countries.name(c) if c else "Unknown", "leads": 0, "contacted": 0, "replied": 0, "meetings": 0, "won": 0})
        o["leads"] += 1
        o["contacted"] += bool(l["emails_sent"] or l["last_contacted_at"])
        o["replied"] += bool(l["replied_at"])
        o["meetings"] += l["stage"] in ("meeting_booked", "won")
        o["won"] += l["stage"] == "won"
    return sorted(out.values(), key=lambda o: -o["leads"])


def funnel(product=None, linkedin=False):
    """Where leads are and where they drop out, for one pipeline or all: found, worth contacting, contacted, replied,
    replied positively, meeting, won. Each step is a subset of the one before."""
    conds, largs = (["leads.product=?"], [product]) if product else ([], [])
    if linkedin:
        conds.append(f"({M.LINKEDIN_SQL})"); largs = largs + M.LINKEDIN_ARGS
    lw = "WHERE " + " AND ".join(conds) if conds else ""
    rows = db.q(f"SELECT id, stage, emails_sent, last_contacted_at, replied_at, product FROM leads {lw}", largs)
    met = {r["lead_id"] for r in db.q("SELECT DISTINCT lead_id FROM meetings WHERE lead_id IS NOT NULL AND status<>'cancelled'")}
    out_stages = ("disqualified", "needs_contact", "new")
    contacted = [r for r in rows if r["emails_sent"] or r["last_contacted_at"]]
    replied = [r for r in contacted if r["replied_at"]] + [r for r in rows if r["replied_at"] and not (r["emails_sent"] or r["last_contacted_at"])]
    good = set()
    for a in db.q("SELECT lead_id, payload FROM activities WHERE kind='email_reply'"):
        try:
            if json.loads(a["payload"] or "{}").get("intent") in POSITIVE:
                good.add(a["lead_id"])
        except ValueError:
            pass
    positive = [r for r in replied if r["id"] in good]   # the same meaning as "Positive replies" in the period figures
    meeting = [r for r in rows if r["stage"] in ("meeting_booked", "won") or r["id"] in met]
    steps = [("Leads found", len(rows)), ("Worth contacting", sum(r["stage"] not in out_stages for r in rows)), ("Contacted", len(contacted) + sum(1 for r in replied if r not in contacted)),
             ("Replied", len(replied)), ("Replied positively", len(positive)), ("Meeting", len(meeting)), ("Won", sum(r["stage"] == "won" for r in rows))]
    partners = {k for k, p in db.products().items() if p.get("partner")}
    why, ruled = {}, set()
    for a in db.q("SELECT a.lead_id, a.summary, a.payload FROM activities a JOIN leads l ON l.id=a.lead_id WHERE a.kind='blocked' AND a.summary LIKE 'Rule:%'" + "".join(" AND " + c.replace("leads.", "l.") for c in conds), largs):
        name = "moved" if "partner pipeline" in a["summary"] else a["summary"][6:].split(". ")[0]
        why[name] = why.get(name, 0) + 1
        if name != "moved":
            ruled.add(a["lead_id"])
    rest = [r for r in rows if r["id"] not in ruled]  # each lead is counted under one reason
    moved = why.pop("moved", 0)  # agencies sent to the partner pipeline are still being worked, so they are not a loss
    lost = {"Stopped by a rule: " + k: v for k, v in sorted(why.items(), key=lambda x: -x[1])[:6]}
    lost.update({k: v for k, v in {"Scored too low to contact": sum(r["stage"] == "disqualified" for r in rest) ,
                                   "No email or phone yet": sum(r["stage"] == "needs_contact" for r in rest),
                                   "Sequence finished, no reply": sum(r["stage"] == "completed" for r in rest),
                                   "Said no": sum(r["stage"] == "lost" for r in rest), "Asked us to stop": sum(r["stage"] == "opted_out" for r in rest),
                                   "Kept under Nurture, not contacted": sum(r["stage"] == "nurture" and not r["replied_at"] for r in rest)}.items() if v > 0})
    return {"steps": [{"name": n, "leads": v, "of_previous": _rate(v, steps[i - 1][1]) if i else None, "of_all": _rate(v, steps[0][1])} for i, (n, v) in enumerate(steps)],
            "lost": [{"why": k, "leads": v} for k, v in lost.items()],
            "moved_to_partners": moved,
            "kinds": {"customers": sum(r["product"] not in partners for r in rows), "partners": sum(r["product"] in partners for r in rows)}}


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


# ---------- email coach: how emails should be written ----------
def _guidance():
    try:
        g = json.loads(db.setting("email_guidance") or "{}")
    except ValueError:
        g = {}
    return {"all": g.get("all") or "", "by_pipeline": {k: v for k, v in (g.get("by_pipeline") or {}).items() if v}, "examples": (g.get("examples") or [])[:5]}


def _sent_with_outcome(days=90, limit=400):
    """Outreach emails the agent sent, each with what came back: [{id, lead_id, company, step, subject, body, at, outcome}]."""
    since = _iso(datetime.now(timezone.utc) - timedelta(days=days))
    rows = db.q("""SELECT a.id, a.lead_id, a.kind, a.summary, a.payload, a.created_at, l.company FROM activities a JOIN leads l ON l.id=a.lead_id
                   WHERE ((a.kind='email_sent' AND a.status='ok') OR a.kind='email_reply') AND a.created_at>=? ORDER BY a.id""", (since,))
    out, last = [], {}
    for a in rows:
        p = json.loads(a["payload"] or "{}")
        if a["kind"] == "email_sent":
            if p.get("manual"):
                last.pop(a["lead_id"], None)
                continue
            e = {"id": a["id"], "lead_id": a["lead_id"], "company": a["company"], "step": _action(a, p), "subject": p.get("subject") or "",
                 "body": (p.get("body") or "").split("\n\n--\n")[0], "at": a["created_at"], "outcome": "no reply"}
            out.append(e); last[a["lead_id"]] = e
        elif a["lead_id"] in last and last[a["lead_id"]]["outcome"] == "no reply":
            last[a["lead_id"]]["outcome"] = "reply: " + (p.get("intent") or "replied")
    return out[-limit:]


@router.get("/api/coach")
def coach():
    sent = _sent_with_outcome()
    subjects = {}
    for e in sent:
        s = subjects.setdefault(e["subject"], {"subject": e["subject"], "sent": 0, "replies": 0, "positive": 0})
        s["sent"] += 1; s["replies"] += e["outcome"] != "no reply"; s["positive"] += e["outcome"].split(": ")[-1] in POSITIVE
    ok, why = llm.enabled() and llm._usable("email"), ""
    if not ok:
        why = ("Claude is not connected (no ANTHROPIC_API_KEY), so emails use built-in wording and these instructions have no effect yet." if not llm.enabled()
               else "Claude is switched off or over today's budget, so emails use built-in wording and these instructions have no effect right now.")
    return {"guidance": _guidance(), "pipelines": [{"key": k, "name": p["name"]} for k, p in db.products().items()], "ai": ok, "ai_note": why,
            "sent": len(sent), "replied": sum(e["outcome"] != "no reply" for e in sent),
            "subjects": sorted((s for s in subjects.values() if s["replies"]), key=lambda s: (-s["replies"], -s["sent"]))[:8],
            "worked": [e for e in reversed(sent) if e["outcome"] != "no reply" and e["outcome"].split(": ")[-1] not in ("not_interested", "unsubscribe")][:6]}


@router.post("/api/coach")
def save_coach(p: dict = Body(...)):
    g = {"all": (p.get("all") or "").strip()[:4000], "by_pipeline": {k: str(v).strip()[:2000] for k, v in (p.get("by_pipeline") or {}).items() if k in db.products() and str(v).strip()},
         "examples": [{"subject": (e.get("subject") or "").strip()[:200], "body": (e.get("body") or "").strip()[:2000]} for e in (p.get("examples") or []) if (e.get("body") or "").strip()][:5]}
    db.set_setting("email_guidance", json.dumps(g, ensure_ascii=False))
    db.log(None, "system", "Email writing instructions changed", channel="system")
    return {"ok": True}


@router.post("/api/coach/try")
def coach_try(p: dict = Body(...)):
    """Write a sample email with the instructions as they are on the page right now (saved or not)."""
    lead = db.one("SELECT * FROM leads WHERE id=?", (int(p["lead_id"]),)) if p.get("lead_id") else None
    lead = lead or db.one("SELECT * FROM leads WHERE product=? AND research IS NOT NULL ORDER BY score DESC LIMIT 1", (p.get("product") or "",)) or \
        {"id": 0, "step": 0, "company": "Acme Fitness", "contact_name": "Sarah Miller", "city": "Riyadh", "country": "SA", "product": p.get("product"), "research": None}
    lead = {**lead, "product": p.get("product") or lead.get("product")}
    s = agent._settings(lead)
    if "guidance" in p:   # try the text on the page before saving it
        s["guidance"] = "\n".join(x for x in [(p["guidance"].get("all") or "").strip(), ((p["guidance"].get("by_pipeline") or {}).get(s["product_key"]) or "").strip()] if x)
        s["examples"] = [e for e in p["guidance"].get("examples") or [] if e.get("body")][:5]
    research = json.loads(lead.get("research") or "null") or llm.research(lead, s["offer"], s["icp"], s["product_key"]) or {}
    used_ai = llm.enabled() and llm._usable("email")
    out = llm.write_email(lead, research, p.get("action") or "intro", s)
    return {"subject": out.get("subject"), "body": out.get("body"), "cta": out.get("cta"), "for": lead["company"], "used_ai": bool(used_ai), "pipeline": s.get("product_name")}


@router.post("/api/coach/suggest")
def coach_suggest():
    """Claude reads what was sent and how people answered, and proposes instructions to add."""
    if not (llm.enabled() and llm._usable("coach")):
        raise HTTPException(400, "This needs Claude, which is not connected, is switched off, or is over today's budget.")
    sent = _sent_with_outcome(days=120, limit=40)
    if len(sent) < 5:
        raise HTTPException(400, f"Only {len(sent)} outreach emails have been sent so far. Come back when there are more to learn from.")
    g = _guidance()
    try:
        out = llm.coach_suggestions(g["all"], [{"step": e["step"], "subject": e["subject"], "body": e["body"][:700], "outcome": e["outcome"]} for e in sent])
    except Exception as e:
        raise HTTPException(502, f"Claude could not review the emails: {e}")
    return {"observations": out.get("observations") or [], "suggestions": [x for x in out.get("suggestions") or [] if isinstance(x, dict) and x.get("text")],
            "based_on": len(sent), "replied": sum(e["outcome"] != "no reply" for e in sent)}


@router.post("/api/approvals/{approval_id}/rewrite")
def rewrite_draft(approval_id: int, p: dict = Body(...)):
    """Tell the AI what to change in one waiting draft. With remember, the same instruction is kept for future emails."""
    feedback = (p.get("feedback") or "").strip()
    if not feedback:
        raise HTTPException(400, "Say what should change.")
    a = db.one("SELECT * FROM approvals WHERE id=? AND state='pending'", (approval_id,))
    if not a or a["channel"] != "email":
        raise HTTPException(404, "That draft is no longer waiting.")
    lead = db.one("SELECT * FROM leads WHERE id=?", (a["lead_id"],))
    c = json.loads(a["content"])
    try:
        new = llm.rewrite_email(p.get("subject") or c.get("subject"), p.get("body") or c.get("body"), feedback, agent._settings(lead), signed=a["action"] in ("reply", "followup"))
    except Exception as e:
        raise HTTPException(502, f"Claude could not rewrite it: {e}")
    if not new:
        raise HTTPException(400, "Rewriting needs Claude, which is not connected, is switched off, or is over today's budget. You can edit the text yourself.")
    c.update(subject=new["subject"], body=new["body"])
    db.ex("UPDATE approvals SET content=? WHERE id=?", (json.dumps(c, ensure_ascii=False), approval_id))
    db.log(a["lead_id"], "system", f"Draft rewritten on request: {feedback[:120]}", channel="system")
    if p.get("remember"):
        g = _guidance()
        g["all"] = (g["all"] + "\n" if g["all"] else "") + "- " + feedback
        db.set_setting("email_guidance", json.dumps(g, ensure_ascii=False))
    return {"subject": new["subject"], "body": new["body"], "remembered": bool(p.get("remember"))}


# ---------- lead quality: clean-up, named contacts, good / bad marks ----------
@router.get("/api/cleanup")
def cleanup_state():
    return quality.cleanup_state()


@router.post("/api/cleanup")
def cleanup(p: dict = Body(...)):
    """start: check every lead the agent could still contact against the rules and for duplicates (runs in the background).
    apply: act on what was found. cancel: stop a running check."""
    if p.get("action") == "start":
        if not quality.cleanup_start():
            raise HTTPException(409, "A check is already running.")
    elif p.get("action") == "apply":
        M._need("leads.delete")
        n = quality.cleanup_apply()
        db.log(None, "system", f"Lead clean-up applied: {n} leads set aside", channel="system")
    elif p.get("action") == "cancel":
        quality.CLEAN["cancel"] = True
    return quality.cleanup_state()


@router.post("/api/leads/{lead_id}/decision-maker")
def decision_maker(lead_id: int):
    lead = db.one("SELECT * FROM leads WHERE id=?", (lead_id,))
    if not lead:
        raise HTTPException(404)
    return quality.find_person(lead, manual=True)


@router.post("/api/leads/{lead_id}/feedback")
def lead_feedback(lead_id: int, p: dict = Body(...)):
    lead = db.one("SELECT * FROM leads WHERE id=?", (lead_id,))
    if not lead:
        raise HTTPException(404)
    quality.set_feedback(lead, p.get("verdict"), p.get("reason"), p.get("note"))
    return {"ok": True}


@router.get("/api/feedback/suggestions")
def feedback_suggestions():
    return {**quality.suggestions(), "reasons": quality.BAD_REASONS}


@router.post("/api/leads/{lead_id}/similar")
def similar_leads(lead_id: int, p: dict = Body(default={})):
    """Find more companies like this one (a lead that replied, bought, or that you marked good) and add them to its pipeline."""
    lead = db.one("SELECT * FROM leads WHERE id=?", (lead_id,))
    if not lead:
        raise HTTPException(404)
    if not llm.enabled():
        raise HTTPException(400, "Finding similar companies needs Claude's web search (ANTHROPIC_API_KEY).")
    if llm.over_budget():
        raise HTTPException(400, "Today's Claude budget is used up. Try again tomorrow, or raise it in Settings.")
    x = quality._extra(lead)
    what = x.get("site_description") or x.get("site_title") or lead.get("segment") or lead.get("industry") or ""
    place = " in " + lead["city"] if lead.get("city") else ""
    try:
        rows = llm.discover_businesses(f"businesses similar to {lead['company']}{place}" + (f" ({what[:160]})" if what else "") + ", same kind and size, not the same company",
                                       lead.get("country") or "US", int(p.get("limit") or 10))
        rows = sources.enrich(rows)
    except Exception as e:
        raise HTTPException(502, f"The search did not work: {e}")
    for r in rows:
        r.setdefault("extra", {})["similar_to"] = lead["company"]
    res = M._import(rows, f"similar to {lead['company']}", {"product": lead.get("product"), "segment": lead.get("segment") or f"Like {lead['company']}", "country": lead.get("country") or "US"})
    db.log(lead_id, "system", f"Looked for companies like this one: {len(res['imported'])} added", channel="system")
    return {"added": len(res["imported"]), "skipped": res["skipped"]}


# ---------- clean-up of the drafts waiting in Approvals ----------
STALE_DAYS = 14
CLEAN_KINDS = {  # kind: (what it says on the page, ticked by default)
    "closed": ("The lead is closed, or asked not to be contacted", True),
    "rule": ("A lead rule now stops this lead", True),
    "replied": ("The lead has replied, or is already past outreach", True),
    "duplicate": ("A newer draft for the same lead is waiting", True),
    "no_address": ("The lead has no address or number to send it to", True),
    "stale": (f"Written more than {STALE_DAYS} days ago (it is written again, fresh)", False),
}


def _approval_scan():
    """Drafts in Approvals that should not go out as they are: [{id, lead_id, kind, company, action, hit}]."""
    old = _iso(datetime.now(timezone.utc) - timedelta(days=STALE_DAYS))
    out, seen = [], set()
    for a in db.q("SELECT id, lead_id, channel, action, created_at FROM approvals WHERE state='pending' ORDER BY id DESC"):   # newest first
        lead = db.one("SELECT * FROM leads WHERE id=?", (a["lead_id"],))
        outreach = a["action"] not in ("reply", "followup")
        key, kind, hit = (a["lead_id"], "outreach" if outreach else a["action"]), None, None
        if not lead or lead["stage"] in ("opted_out", "disqualified", "lost") or compliance.is_suppressed(lead):
            kind = "closed"
        elif key in seen and outreach:
            kind = "duplicate"
        elif outreach and ((lead.get("replied_at") and lead["replied_at"] > a["created_at"]) or lead["stage"] in ("won", "meeting_booked", "replied", "reply_manually")):
            kind = "replied"
        elif outreach and not lead.get("phone" if a["channel"] == "phone" else "email"):
            kind = "no_address"
        elif outreach:
            hit = rules.check(lead, network=False)
            kind = "rule" if hit else "stale" if a["created_at"] < old else None
        seen.add(key)
        if kind:
            out.append({"id": a["id"], "lead_id": a["lead_id"], "kind": kind, "company": (lead or {}).get("company") or "(deleted lead)", "action": a["action"], "hit": hit})
    return out


@router.get("/api/approvals-cleanup")
def approvals_cleanup():
    found, groups = _approval_scan(), []
    for kind, (label, on) in CLEAN_KINDS.items():
        mine = [p for p in found if p["kind"] == kind]
        if mine:
            groups.append({"kind": kind, "label": label, "drafts": len(mine), "on": on, "examples": [p["company"] for p in mine[:4]],
                           "rules": sorted({p["hit"]["name"] for p in mine if p.get("hit")})})
    return {"waiting": db.one("SELECT COUNT(*) n FROM approvals WHERE state='pending'")["n"], "found": len(found), "groups": groups}


@router.post("/api/approvals-cleanup")
def approvals_cleanup_apply(p: dict = Body(...)):
    """Take the chosen kinds of draft out of Approvals. Nothing is deleted: each is marked skipped and its lead's timeline says why."""
    kinds, out = set(p.get("kinds") or []), {"removed": 0, "rewrite": 0, "moved": 0}
    for it in _approval_scan():
        if it["kind"] not in kinds:
            continue
        lead = db.one("SELECT * FROM leads WHERE id=?", (it["lead_id"],))
        if it["kind"] == "rule" and lead:
            rules.apply(lead, it["hit"])   # the rule's own outcome: skipped, waits for a fix, paused, or moved to partners (drops the draft too)
            out["moved"] += it["hit"]["outcome"] == "partner"
        else:
            db.ex("UPDATE approvals SET state='rejected', decided_at=? WHERE id=? AND state='pending'", (db.now(), it["id"]))
            if lead:
                db.log(lead["id"], "approval", f"Draft '{it['action']}' removed in the Approvals clean-up: {CLEAN_KINDS[it['kind']][0].lower()}", channel="agent")
                if it["kind"] == "stale":
                    db.update_lead(lead["id"], stage="in_sequence", next_action="rewrite", next_action_at=db.now())
                    out["rewrite"] += 1
                elif it["kind"] == "no_address" and lead["stage"] == "awaiting_approval":
                    db.update_lead(lead["id"], stage="needs_contact", next_action=None, next_action_at=None)
        out["removed"] += 1
    db.log(None, "system", f"Approvals clean-up: {out['removed']} drafts removed" + (f", {out['rewrite']} to be written again" if out["rewrite"] else "")
           + (f", {out['moved']} agencies moved to partners" if out["moved"] else ""), channel="system")
    return {**out, "waiting": db.one("SELECT COUNT(*) n FROM approvals WHERE state='pending'")["n"]}


# ---------- owners and decision-makers for a pipeline ----------
@router.get("/api/owners")
def owners_state(product: str = None):
    return quality.people_state(product)


@router.post("/api/owners")
def owners_start(p: dict = Body(...)):
    """Look for the owner or manager of each lead in a pipeline that has no named person yet."""
    key = p.get("product")
    if key not in db.products():
        raise HTTPException(404, "unknown pipeline")
    st = quality.people_state(key)
    if not st["can"]["web"] and not st["can"]["finder"]:
        raise HTTPException(400, "Nothing is connected that can look people up. Add ANTHROPIC_API_KEY (searches public pages), APOLLO_API_KEY or HUNTER_API_KEY on the server.")
    if not st["waiting"]:
        raise HTTPException(400, "Every lead in this pipeline already has a named person, or was looked up in the last 30 days.")
    if not quality.people_start(key, int(p.get("limit") or 25)):
        raise HTTPException(409, "A search for owners is already running. Wait for it to finish.")
    return quality.people_state(key)


# ---------- leads from LinkedIn ----------
LI = {"running": False, "product": None, "started": None, "finished": None, "added": 0, "skipped": 0, "errors": [], "note": None}


@router.get("/api/linkedin")
def linkedin_page():
    """Everything that came from LinkedIn (added from a profile, or found through a public post), with its own funnel."""
    rows = db.q(f"SELECT id, source, stage, contact_name, email, phone, extra FROM leads WHERE {M.LINKEDIN_SQL}", M.LINKEDIN_ARGS)
    live = [r for r in rows if r["stage"] not in ("disqualified", "opted_out", "lost")]
    pipes = [{"key": k, "name": v.get("name") or k, "linkedin": bool((v.get("sources") or {}).get("linkedin")), "phrases": len((v.get("sources") or {}).get("intent") or [])}
             for k, v in db.products().items()]
    return {"funnel": funnel(linkedin=True), "search": dict(LI), "pipelines": pipes,
            "counts": {"all": len(rows), "from_posts": sum(r["source"] != "linkedin" for r in rows), "from_profiles": sum(r["source"] == "linkedin" for r in rows),
                       "with_email": sum(bool(r["email"]) for r in live), "with_phone": sum(bool(r["phone"]) for r in live),
                       "no_contact": sum(not r["email"] and not r["phone"] for r in live)},
            "can": {"search": llm.enabled(), "contacts": bool(os.environ.get("APOLLO_API_KEY"))}}


@router.post("/api/linkedin/search")
def linkedin_search(p: dict = Body(...)):
    """Search public LinkedIn posts for people asking for what a pipeline sells, and add them as leads."""
    import threading
    key = p.get("product")
    prod = db.products().get(key)
    if not prod:
        raise HTTPException(404, "unknown pipeline")
    if not llm.enabled():
        raise HTTPException(400, "Searching LinkedIn posts needs Claude's web search: set ANTHROPIC_API_KEY on the server.")
    if llm.over_budget():
        raise HTTPException(400, "Today's Claude budget is used up. Try tomorrow, or raise the budget in Settings.")
    src = prod.get("sources") or {}
    asks = [a if isinstance(a, dict) else {"query": a} for a in (src.get("intent") or [{"query": q} for q in (src.get("reddit") or [])[:2]])][:3]
    if p.get("query"):
        asks = [{"query": str(p["query"])[:200], "country": p.get("country") or ""}]
    if not asks:
        raise HTTPException(400, "This pipeline has no search phrases yet. Add some under Pipelines > edit > People asking for it, or type one here.")
    if LI["running"]:
        raise HTTPException(409, "A LinkedIn search is already running.")
    LI.update(running=True, product=key, started=db.now(), finished=None, added=0, skipped=0, errors=[], note=None)

    def run():
        try:
            for qd in asks:
                try:
                    rows = M._request_leads(qd["query"], M._where(qd), 8, linkedin=True)
                    res = M._import(rows, f"LinkedIn posts: {qd['query']}", {"product": key, "segment": "Asked for it", "country": qd.get("country") or None})
                    LI["added"] += len(res["imported"]); LI["skipped"] += res["skipped"]
                except Exception as e:
                    LI["errors"].append(f"{qd['query']}: {str(e)[:200]}")
            if not LI["added"] and not LI["errors"]:
                LI["note"] = "No public LinkedIn post asking for this was found that could be checked. Most LinkedIn posts are not visible to search, so this is common: try a more specific phrase."
        finally:
            LI.update(running=False, finished=db.now())

    threading.Thread(target=run, daemon=True).start()
    return dict(LI)


# ---------- Apollo ----------
@router.get("/api/apollo/usage")
def apollo_usage():
    """Apollo credits used today and this month, by kind, and the daily cap."""
    return M.prospect.apollo.usage()


@router.post("/api/apollo/preview")
def apollo_preview(p: dict = Body(...)):
    """Run a pipeline's Apollo search without saving anything: the first ten companies and who would be contacted there.
    The pipeline is sent as it stands in the editor, so a search can be tried before it is saved."""
    pr = M.prospect
    if not M.enrich.enabled():
        raise HTTPException(400, "Apollo is not connected. Add APOLLO_API_KEY in Railway Variables.")
    pipe = {**(db.products().get(p.get("key") or "") or {}), **{k: p[k] for k in ("titles", "sells_to_tech", "partner") if k in p},
            "sources": {"apollo": p.get("apollo") or {}}}
    if not pr.apollo.has_filters(pr.cfg(pipe)):
        raise HTTPException(400, "Nothing to search for yet. Fill in at least one of: industries, company size, countries, technologies or keywords.")
    try:
        found = pr.discover(p.get("key") or "preview", pipe, limit=10, preview=True)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    rows = []
    for r in found["rows"]:
        a = r["extra"]["apollo"]
        score, why = pr.fit({**r, "extra": json.dumps(r["extra"])}, pipe)
        rows.append({"company": r["company"], "website": r.get("website"), "industry": r.get("industry"), "employees": a.get("employees"), "country": r.get("country"),
                     "person": r.get("contact_name"), "title": r.get("title"), "has_email": a.get("has_email"), "hiring": bool(r["extra"].get("intent")),
                     "agency": bool(r["extra"].get("vendor")), "score": score, "why": why})
    db.log(None, "system", f"Apollo search previewed: {len(rows)} results, {found['credits']} credit(s)", channel="system")
    return {"rows": rows, "notes": found["notes"], "credits": found["credits"], "how": found["how"], "usage": pr.apollo.usage()}


# ---------- AI phone calls ----------
@router.get("/api/voice")
def voice_status():
    out = voice.status()
    out["budget"] = voice.budget()
    if out["connected"]:
        try:
            out["numbers"] = voice.numbers()
            out["spent"], out["spent_source"], out["stopped"] = round(voice.spent(fresh=True), 2), voice._spent["source"], voice.over_budget()
        except RuntimeError as e:
            out["error"] = str(e)
    return out


@router.post("/api/voice/setup")
def voice_setup(request: Request, p: dict = Body(default={})):
    """Create (or refresh) the AI caller in Vapi and choose the number it calls from."""
    try:
        out = voice.setup(M._public_base(request) + "/webhooks/voice", p.get("phone_id") or None)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    db.log(None, "system", "AI caller set up in Vapi", channel="phone")
    return out


@router.post("/api/voice/test")
def voice_test(p: dict = Body(...)):
    """Have the AI caller phone a number you give (your own), to hear how it sounds."""
    number = re.sub(r"[^\d+]", "", p.get("number") or "")
    if not re.fullmatch(r"\+\d{8,15}", number):
        raise HTTPException(400, "Enter the number in international form, for example +966501234567.")
    if voice.over_budget():
        raise HTTPException(400, f"Not called: {voice.over_budget()}. Raise the monthly call budget in Settings to go on.")
    try:
        cid = voice.call(number, "Test call", {"company": "your company", "contact_name": "", "opening": f"Hello, this is a test call from {db.setting('sender_name')}. Can you hear me clearly?",
                                               "talking_points": "This is only a test of the calling assistant. Ask how the line sounds, thank them, and end the call."})
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    db.log(None, "system", f"Test call placed to {number}", channel="phone")
    return {"ok": True, "call_id": cid}


# ---------- lead sources ----------
@router.post("/api/pipelines/suggest")
def suggest_sources(p: dict = Body(...)):
    """Search phrases that find buyers with a current need, for the pipeline being edited."""
    return llm.suggest_sources(p.get("name") or "", p.get("pitch") or "", p.get("icp") or "")


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
        return ZoneInfo(db.setting("timezone") or "UTC")
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
    tz = (p.get("tz") or db.setting("timezone") or "UTC")[:60]
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
