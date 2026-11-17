"""The agent's brain. Uses Claude when ANTHROPIC_API_KEY is set, otherwise deterministic
templates so the whole pipeline still runs end to end in demo mode."""
import json
import os
import re

import time
from datetime import datetime, timedelta, timezone

from .. import db

# Model is picked in Settings (default Claude Haiku 5.5, the cheapest). BRN_MODEL on the server overrides it.
MODEL_ENV = os.environ.get("BRN_MODEL")
# USD per million tokens (input, output). Check anthropic.com/pricing if these change.
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.0, 10.0), "claude-opus-5-5": (4.0, 20.0)}
ECONOMY_TASKS = {"email", "reply", "intent", "coach"}
EFFORT = os.environ.get("BRN_EFFORT", "low")  # short sales copy and JSON: low effort keeps cost and latency down
_cooldown_until = 0.0
_use_fallbacks = True

try:
    import anthropic
    _client = anthropic.Anthropic() if os.environ.get("ANTHROPIC_API_KEY") else None
except Exception:  # pragma: no cover
    _client = None


def enabled():
    return _client is not None


def model():
    return MODEL_ENV or db.setting("ai_model") or "claude-haiku-5-5"


def spent(since_day=None):
    day = since_day or db.now()[:10]
    op = ">=" if since_day else "="
    return db.one(f"SELECT COALESCE(SUM(cost),0) c, COALESCE(SUM(calls),0) n FROM ai_usage WHERE day{op}?", (day,))


def _usable(task="research"):
    if _client is None or time.time() < _cooldown_until:
        return False
    mode = db.setting("ai_mode") or "economy"
    if mode == "off":
        return False
    if mode == "economy":
        if task not in ECONOMY_TASKS:
            return False
    budget = float(db.setting("ai_daily_budget") or 0)
    if budget > 0 and spent()["c"] >= budget:
        return False
    return True


def _record(task, usage):
    m = model()
    pin, pout = PRICES.get(m, PRICES["claude-opus-5-5"])
    i, o = getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0
    i += (getattr(usage, "cache_read_input_tokens", 0) or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
    cost = (i * pin + o * pout) / 1_000_000
    db.ex("""INSERT INTO ai_usage(day, model, task, calls, input_tokens, output_tokens, cost) VALUES (?,?,?,1,?,?,?)
             ON CONFLICT(day, model, task) DO UPDATE SET calls=calls+1, input_tokens=input_tokens+excluded.input_tokens,
             output_tokens=output_tokens+excluded.output_tokens, cost=cost+excluded.cost""", (db.now()[:10], m, task, i, o, cost))


def usage_report():
    """Spend per day (last 30 days), today, this month, and the estimated balance left."""
    today = db.now()[:10]
    month_ago = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
    days = db.q("""SELECT day, SUM(calls) calls, SUM(input_tokens) input_tokens, SUM(output_tokens) output_tokens, SUM(cost) cost
                   FROM ai_usage WHERE day>=? GROUP BY day ORDER BY day DESC""", (month_ago,))
    tasks = db.q("SELECT task, SUM(calls) calls, SUM(cost) cost FROM ai_usage WHERE day>=? GROUP BY task ORDER BY cost DESC", (month_ago,))
    bal, bal_at = db.setting("ai_balance"), db.setting("ai_balance_at")
    left = None
    if bal and bal_at:
        try:
            used = db.one("SELECT COALESCE(SUM(cost),0) c FROM ai_usage WHERE day>=?", (bal_at[:10],))["c"]
            left = round(float(bal) - used, 2)
        except ValueError:
            left = None
    return {"model": model(), "mode": db.setting("ai_mode"), "budget": float(db.setting("ai_daily_budget") or 0),
            "today": round(spent()["c"], 4), "today_calls": spent()["n"], "month": round(spent(today[:8] + "01")["c"], 4),
            "balance_entered": bal or None, "balance_at": bal_at or None, "balance_left": left,
            "days": days, "tasks": tasks, "prices": PRICES.get(model())}


def status():
    """For the dashboard: is Claude working, and if not, why."""
    r = usage_report()
    return {"configured": enabled(), "model": model(), "error": db.setting("llm_error") or None,
            "error_at": db.setting("llm_error_at") or None, "cooling_down": enabled() and time.time() < _cooldown_until,
            "ai_mode": r["mode"], "today": r["today"], "budget": r["budget"], "balance_left": r["balance_left"],
            "over_budget": r["budget"] > 0 and r["today"] >= r["budget"]}


def _note_error(e):
    """Remember the failure, pause Claude for a while on account problems, and let the caller fall back to templates."""
    global _cooldown_until
    msg = str(e)
    account = any(k in msg.lower() for k in ("credit balance", "billing", "authentication", "invalid x-api-key", "permission", "401", "403"))
    _cooldown_until = time.time() + (600 if account else 30)
    low = msg.lower()
    if "credit balance" in low:
        short = "Your Anthropic credit balance is too low. Add credits at console.anthropic.com > Billing."
    elif "invalid x-api-key" in low or "authentication" in low:
        short = "The Anthropic API key is not valid. Check ANTHROPIC_API_KEY in Railway Variables."
    else:
        short = msg[:300]
    db.set_setting("llm_error", short)
    db.set_setting("llm_error_at", db.now())


def _ask_json(system, prompt, max_tokens=1200, task="research"):
    global _use_fallbacks
    m = model()
    kwargs = dict(model=m, max_tokens=max_tokens, system=system,
                  messages=[{"role": "user", "content": prompt}], output_config={"effort": EFFORT})
    if m.startswith("claude-haiku"):
        kwargs["thinking"] = {"type": "disabled"}  # no thinking tokens for short copy: cheapest and fastest
    if _use_fallbacks and not m.startswith("claude-haiku"):  # Haiku has no server-side fallback  # server-side fallback if a safety classifier declines (tolerated if unsupported)
        kwargs["extra_headers"] = {"anthropic-beta": "server-side-fallback-2026-07-01"}
        kwargs["extra_body"] = {"fallbacks": "default"}
    try:
        msg = _client.messages.create(**kwargs)
    except Exception as e:
        if _use_fallbacks and "fallback" in str(e).lower():
            _use_fallbacks = False
            return _ask_json(system, prompt, max_tokens, task)
        raise
    if getattr(msg, "usage", None) is not None:
        _record(task, msg.usage)
    if getattr(msg, "stop_reason", None) == "refusal":
        raise RuntimeError("Claude declined this request")
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError("Claude returned no JSON")
    if db.setting("llm_error"):
        db.set_setting("llm_error", "")
    return json.loads(m.group(0))


SYSTEM = (
    "You are the SDR agent for brn dynamics, a software and AI agency with offices in Riyadh, Kuwait City, "
    "Karachi and Geneva. You research B2B prospects, score fit, and write short, specific, honest outreach. "
    "Never invent facts about the prospect; if unsure, stay general. Never pressure. Always reply with JSON only."
)


def _lead_view(lead):
    """Lead fields for the prompt, with the imported scan data (extra) unpacked."""
    v = {k: lead.get(k) for k in ("company", "contact_name", "title", "website", "city", "country", "industry", "segment", "notes")}
    try:
        extra = json.loads(lead.get("extra") or "{}")
    except Exception:
        extra = {}
    if extra:
        if isinstance(extra.get("issues"), list):
            extra = {**extra, "issues": extra["issues"][:4]}  # keep prompts short: the top issues are enough
        v["data"] = extra
    return v


def _extra(lead):
    try:
        return json.loads(lead.get("extra") or "{}")
    except Exception:
        return {}


def research(lead, offer, icp, product_key=None):
    if _usable("research"):
        try:
            return _ask_json(SYSTEM, f"""Offer: {offer}
Ideal customer: {icp}
Lead: {json.dumps(_lead_view(lead), ensure_ascii=False, default=str)}
If the lead data includes website scan results (health score, issues), use the most concrete issue as the main hook.
If the data includes "intent" (they asked for this, or are hiring for it), that is the hook: refer to what they said or the role they advertise.
Score how likely they are to BUY this now, not how well they match a description: 85+ only with evidence of a current need (a request,
a job ad, a concrete problem), 55-70 for a plain profile match. {"For this offer the buyers ARE agencies, software houses and consultancies: judge whether this one is likely to need outside delivery capacity, and set buyer to true." if db.products().get(product_key or "", {}).get("sells_to_tech") else 'Score below 30 when the lead is itself a provider of similar services (agency, software house, consultancy, freelancer): those are competitors, not buyers, so also set "buyer" to false.'}
Return JSON: {{"summary": str, "pain_points": [str], "hooks": [str], "score": 0-100, "score_reason": str, "buyer": true|false, "language": "ar"|"en"}}""", 800, "research")
        except Exception as e:
            _note_error(e)
    ind = f"{lead.get('industry') or ''} {lead.get('segment') or ''}".lower()
    ex = _extra(lead)
    if product_key == "gym_club" or (not product_key and "gym" in ind):
        fit = any(k in ind for k in ("gym", "fitness", "club", "sport", "yoga", "crossfit", "studio", "pilates"))
        score = (80 if fit else 35) + (10 if _home_market(lead.get("country")) else 0)
        pains = ["manual membership renewals", "no-shows in classes", "branch reporting in spreadsheets"] if fit else ["unclear"]
        hooks = ["Arabic/English member app", "automated renewals and payment reminders"]
        reason = "industry matches ICP" if fit else "industry outside ICP"
    elif product_key == "fixify":
        issues = [i.get("problem") for i in ex.get("issues", []) if i.get("problem")] or ([ex["top_issue"]] if ex.get("top_issue") else [])
        hs = ex.get("health_score")
        score = 85 if issues else (60 if lead.get("website") else 30)
        if isinstance(hs, (int, float)) and hs >= 98 and not issues:
            score = 45
        pains, hooks = issues[:3] or ["website upkeep takes time away from the business"], ["fixed monthly plan", "security, backups, speed and SEO handled"]
        reason = f"website scan found {len(issues)} issue(s)" if issues else "no scan issues recorded"
    else:
        score = 65 if (lead.get("email") or lead.get("phone")) else 40
        pains, hooks, reason = ["unclear"], ["custom software and AI automation"], "generic fit"
    return {"summary": f"{lead['company']} is a {lead.get('segment') or lead.get('industry') or 'business'} in {lead.get('city') or lead.get('country') or 'a place not yet known'}.",
            "pain_points": pains, "hooks": hooks, "score": min(score, 100), "score_reason": reason, "language": "en", "_engine": "template"}


STEP_GOAL = {
    "intro": "first touch: one specific observation, the outcome we deliver, a soft question",
    "follow_up_1": "short bump adding one new proof point (e.g. results at another gym), ask for a 15 minute call",
    "follow_up_2": "share a useful idea they can use even without us, low pressure",
    "breakup": "polite last note, say we will stop reaching out, leave the door open",
}


def house_style(settings):
    """The team's own instructions and example emails from Email coach, as a block for any writing prompt."""
    out = ""
    if (settings.get("guidance") or "").strip():
        out += ("\nThe team's own instructions for how our emails are written. Follow them; where they differ from the rules above, they win "
                "(but never invent facts, and never drop the greeting):\n" + settings["guidance"].strip() + "\n")
    if settings.get("examples"):
        out += "\nEmails the team likes. Match their voice and structure; do not copy their wording or facts:\n" + "\n---\n".join(
            f"Subject: {e.get('subject', '')}\n{e.get('body', '')}"[:900] for e in settings["examples"]) + "\n"
    return out


def rewrite_email(subject, body, feedback, settings, signed=False):
    """Rewrite a draft the way a reviewer asked. Returns None when Claude is not available."""
    if not _usable("email"):
        return None
    out = _ask_json(SYSTEM, f"""Rewrite this email draft following the reviewer's feedback. Keep every fact that is in the draft; add no new facts,
prices, clients or promises. Keep it plain text with paragraphs separated by blank lines, and keep the greeting.
{"Keep the sign-off." if signed else "Do NOT add a sign-off, signature, links or a booking URL: the email template adds those."}
Reviewer's feedback: {feedback!r}
Draft subject: {subject!r}
Draft body: {body!r}{house_style(settings)}
Return JSON: {{"subject": str, "body": str}}""", 900, "email")
    return {"subject": out.get("subject") or subject, "body": out.get("body") or body}


def coach_suggestions(guidance, emails):
    """Ideas for writing better emails, from what was sent and how people answered. emails: [{step, subject, body, outcome}]."""
    return _ask_json(SYSTEM, f"""You are reviewing our cold outreach to help the team improve it. Below are emails we sent, each with what happened
(no reply, or the kind of reply). Our current writing instructions: {guidance or 'none yet'!r}
Emails: {json.dumps(emails, ensure_ascii=False)}
Say what you see and what to change. Base every point on these emails; with few emails or few replies, say that the evidence is thin
rather than overstating it. Each suggestion must be an instruction we could add to our writing guide, in one sentence, concrete
(not "be more engaging").
Return JSON: {{"observations": [str, up to 4], "suggestions": [{{"text": "the instruction", "why": "what in the emails led to it"}}, up to 6]}}""", 1200, "coach")


def write_email(lead, research_, action, settings):
    if _usable("email"):
        try:
            return _ask_json(SYSTEM, f"""Write a cold B2B email.
Goal of this step: {STEP_GOAL.get(action, action)}
Product: {settings.get('product_name', '')}. Offer: {settings['offer']}
Sender: {settings['sender_name']}.
Lead: {json.dumps(_lead_view(lead), ensure_ascii=False, default=str)}
Research: {json.dumps(research_, ensure_ascii=False)}
Rules: under 110 words, no hype, no fake claims, plain text paragraphs separated by blank lines.
Start with a greeting ("Hi <first name>," or "Hi there," if no name). Do NOT add a sign-off, signature, links or the booking URL:
the branded template adds a call-to-action button and signature. Write in {research_.get('language', 'en')}.{house_style(settings)}
Return JSON: {{"subject": str, "subject_b": "a clearly different subject line for the same email, to compare which gets more replies", "body": str, "cta": "short button label, 2-4 words"}}""", 800, "email")
        except Exception as e:
            _note_error(e)
    name = (lead.get("contact_name") or "there").split(" ")[0]
    company = lead["company"]
    key = settings.get("product_key")
    pains = research_.get("pain_points") or []
    if key == "fixify":
        issue = pains[0] if pains and pains[0] != "unclear" else "a few things slowing the site down"
        bodies = {
            "intro": f"Hi {name},\n\nI ran a quick health check on the {company} website and noticed: {issue}.\n\n"
                     f"Fixify is our WordPress care plan: one fixed monthly fee covers security, backups, speed, SEO and fixes like this one, so you never have to chase a developer.\n\nWant me to send the full report?",
            "follow_up_1": f"Hi {name},\n\nFollowing up on the website check for {company}. Small issues like \"{issue}\" quietly cost visitors and Google ranking. Happy to walk you through the full list in 15 minutes.",
            "follow_up_2": f"Hi {name},\n\nOne free tip whatever you decide: compress your largest images and turn on page caching. On most small business sites that alone halves load time.",
            "breakup": f"Hi {name},\n\nI'll stop reaching out after this. If you ever want the website report for {company}, just reply and I'll send it over.",
        }
        subjects = {"intro": f"Quick website check for {company}", "follow_up_1": "re: your website check", "follow_up_2": "a free speed tip", "breakup": "closing the loop"}
        ctas = {"intro": "Get the free report", "breakup": "Get the report"}
    elif key == "gym_club":
        bodies = {
            "intro": f"Hi {name},\n\nRunning {company} across members, classes and renewals usually means a lot of manual follow-up. "
                     f"BRN Gym Club handles memberships, billing reminders, access control and an Arabic/English member app in one place.\n\n"
                     f"Would it be useful if I showed you how a gym in {lead.get('city') or 'the region'} cut missed renewals with it?",
            "follow_up_1": f"Hi {name},\n\nQuick follow-up. Gyms we work with usually recover renewals they were losing to reminders that never went out. Open to a 15 minute call this week?",
            "follow_up_2": f"Hi {name},\n\nOne idea even if we never work together: send renewal reminders 10, 3 and 0 days before expiry over WhatsApp. It is the single biggest lever we see.",
            "breakup": f"Hi {name},\n\nI will stop reaching out after this one. If member management becomes a priority at {company}, I'm one reply away.",
        }
        subjects = {"intro": f"{company} renewals", "follow_up_1": "re: renewals", "follow_up_2": "a renewal idea for you", "breakup": "closing the loop"}
        ctas = {"intro": "See a 15-min demo"}
    else:
        offer = settings["offer"].split(":")[0]
        bodies = {
            "intro": f"Hi {name},\n\nI'm reaching out from brn dynamics. We help businesses like {company} with {offer.lower()}.\n\nWould a short call to see if there's a fit be useful?",
            "follow_up_1": f"Hi {name},\n\nQuick follow-up on my last note. Happy to share examples of what we've built for similar teams.",
            "follow_up_2": f"Hi {name},\n\nSharing one idea we see work often: automate the most repetitive weekly task first. It usually pays back within a month.",
            "breakup": f"Hi {name},\n\nI'll stop reaching out after this one. If it becomes a priority, I'm one reply away.",
        }
        subjects = {"intro": f"Idea for {company}", "follow_up_1": "re: quick idea", "follow_up_2": "one idea for you", "breakup": "closing the loop"}
        ctas = {}
    return {"subject": subjects.get(action, company), "body": bodies.get(action, bodies["intro"]),
            "cta": ctas.get(action, "Book a 15-min call"), "_engine": "template"}


def call_script(lead, research_, settings):
    if _usable("call"):
        try:
            return _ask_json(SYSTEM, f"""Write a short voice-agent brief for an outbound B2B call.
Offer: {settings['offer']}
Lead: {json.dumps(lead, ensure_ascii=False, default=str)}
Research: {json.dumps(research_, ensure_ascii=False)}
The agent must say it is an AI assistant calling for brn dynamics, ask permission to continue, offer to remove them from the list,
and aim to book a 15 minute demo. Return JSON: {{"opening": str, "talking_points": [str], "objections": {{str: str}}, "goal": str, "language": "ar"|"en"}}""", 900, "call")
        except Exception as e:
            _note_error(e)
    return {
        "opening": f"Hi, this is an AI assistant calling on behalf of brn dynamics. Is now an okay moment for 30 seconds about {lead['company']}? "
                   + (f"It's about {settings.get('product_name')}." if settings.get("product_name") else ""),
        "talking_points": research_.get("hooks", []) or [settings.get("offer", "")],
        "objections": {"we have a system": "Totally fair. Happy to compare what you have with what we'd build in 15 minutes.",
                       "not interested": "Understood, I will remove you from our list. Thanks for your time."},
        "goal": "book a 15 minute demo",
        "language": research_.get("language", "en"),
        "_engine": "template",
    }


def classify_reply(text):
    """Classify an inbound email reply or call outcome."""
    if _usable("reply"):
        try:
            return _ask_json(SYSTEM, f"""Classify this prospect reply: {text!r}
Return JSON: {{"intent": "interested"|"meeting"|"not_now"|"not_interested"|"unsubscribe"|"wrong_person"|"question", "summary": str}}""", 300, "reply")
        except Exception as e:
            _note_error(e)
    t = text.lower()
    if any(w in t for w in ("unsubscribe", "remove me", "stop", "don't contact", "do not contact")):
        return {"intent": "unsubscribe", "summary": "asked to stop"}
    if any(w in t for w in ("book", "call me", "let's talk", "lets talk", "demo", "meeting", "tuesday", "tomorrow")):
        return {"intent": "meeting", "summary": "wants to meet"}
    if any(w in t for w in ("not interested", "no thanks", "no thank")):
        return {"intent": "not_interested", "summary": "declined"}
    if any(w in t for w in ("later", "next month", "next quarter")):
        return {"intent": "not_now", "summary": "timing"}
    if "?" in t:
        return {"intent": "question", "summary": "has a question"}
    return {"intent": "interested", "summary": "positive reply"}


def alt_subject(subject, lead, action):
    """A second subject line for the same email, when Claude did not supply one."""
    company, first = lead.get("company") or "your team", (lead.get("contact_name") or "").split(" ")[0]
    options = {"intro": [f"Idea for {company}", f"Question for {first or company}"], "follow_up_1": [f"{company}: worth a quick look?", "Following up"],
               "follow_up_2": [f"One thought for {company}", "A quick idea"], "breakup": [f"Should I close the file on {company}?", "Last note from me"]}.get(action, [f"A note for {company}", "Quick question"])
    return next((o for o in options if o.lower() != (subject or "").lower()), options[0])


def write_followup(lead, research_, settings, meeting):
    """Draft the email sent after a meeting. A person approves it, and edits in what was actually agreed."""
    name = (lead.get("contact_name") or "").split(" ")[0]
    sender = settings.get("sender_name") or "brn dynamics"
    if _usable("reply"):
        try:
            out = _ask_json(SYSTEM, f"""We just had a meeting with a prospect. Write the short follow-up email.
Meeting: {meeting.get('title')}. Notes on the invitation: {meeting.get('description') or 'none'}
Product: {settings.get('product_name', '')}. Offer: {settings['offer']}
Lead: {json.dumps(_lead_view(lead), ensure_ascii=False, default=str)}
Research: {json.dumps(research_, ensure_ascii=False)}
Rules: under 90 words, plain text, paragraphs separated by blank lines. Thank them for their time, and leave ONE line in square
brackets like "[add what we agreed and the next step]" for the sender to fill in, because you do not know what was said in the
meeting. Never invent what was discussed, prices or dates. Start with "Hi {name or 'there'}," and end with a short sign-off from {sender}.{house_style(settings)}
Return JSON: {{"subject": str, "body": str}}""", 600, "reply")
            if out.get("body"):
                return {"subject": out.get("subject") or "Thanks for your time today", "body": out["body"]}
        except Exception as e:
            _note_error(e)
    return {"subject": "Thanks for your time today",
            "body": f"Hi {name or 'there'},\n\nThank you for taking the time to speak with us today. It was great to learn more about {lead.get('company') or 'your business'}.\n\n"
                    f"[add what we agreed and the next step]\n\nIf anything comes up in the meantime, just reply to this email.\n\nBest regards,\n{sender}"}


def write_checkin(lead, research_, settings, their_text):
    """A short check-in to someone who said the timing was wrong. A person approves it."""
    name = (lead.get("contact_name") or "").split(" ")[0]
    sender = settings.get("sender_name") or "brn dynamics"
    if _usable("reply"):
        try:
            out = _ask_json(SYSTEM, f"""A while ago a prospect told us the timing was not right. Write a short, friendly check-in.
What they said then: {their_text!r}
Product: {settings.get('product_name', '')}. Offer: {settings['offer']}
Lead: {json.dumps(_lead_view(lead), ensure_ascii=False, default=str)}
Rules: under 70 words, plain text, no pressure, no "just following up". Refer lightly to what they said, ask one easy question about whether
the timing is better now, and make it easy to say no. Start with "Hi {name or 'there'}," and end with a short sign-off from {sender}.{house_style(settings)}
Return JSON: {{"subject": str, "body": str}}""", 500, "reply")
            if out.get("body"):
                return {"subject": out.get("subject") or "Checking in", "body": out["body"]}
        except Exception as e:
            _note_error(e)
    return {"subject": "Checking in", "body": f"Hi {name or 'there'},\n\nWhen we last spoke you said the timing wasn't right, so I left it there. "
            f"Has anything changed at {lead.get('company') or 'your end'}, or should I check back later in the year?\n\nEither answer is fine.\n\nBest regards,\n{sender}"}


def write_reply(lead, research_, settings, their_text, intent, last_sent=None):
    """Draft an answer to a lead's reply. A person approves it before it goes out, so it must be safe to send as is."""
    name = (lead.get("contact_name") or "").split(" ")[0]
    link, sender = settings.get("booking_link") or "", settings.get("sender_name") or "brn dynamics"
    if _usable("reply"):
        try:
            out = _ask_json(SYSTEM, f"""A prospect replied to our outreach email. Write our answer.
Their reply: {their_text!r}
How we read it: {intent}
Our last email to them: {json.dumps(last_sent or {}, ensure_ascii=False)}
Product: {settings.get('product_name', '')}. Offer: {settings['offer']}
Lead: {json.dumps(_lead_view(lead), ensure_ascii=False, default=str)}
Research: {json.dumps(research_, ensure_ascii=False)}
Booking link (use it only when proposing a call): {link}
Rules: under 90 words, plain text, paragraphs separated by blank lines, warm and direct, no hype. Answer what they actually asked.
Never invent prices, timelines, features, clients or facts that are not stated above: if you can't answer from what is given,
say you will confirm and offer a short call. If they want to meet, give the booking link and offer to send an invite for a time they name.
If the timing is wrong for them, be gracious and say you will check back later. If they are the wrong person, ask who the right person is.
Start with "Hi {name or 'there'}," and end with a short sign-off from {sender}. Write in the language of their reply.{house_style(settings)}
Return JSON: {{"body": str}}""", 700, "reply")
            if out.get("body"):
                return {"body": out["body"]}
        except Exception as e:
            _note_error(e)
    hi = f"Hi {name or 'there'},\n\n"
    bye = f"\n\nBest regards,\n{sender}"
    book = f" You can pick a time that suits you here: {link}" if link else ""
    body = {
        "meeting": f"Thanks for getting back to me, happy to set that up.{book}\n\nOr tell me a couple of times that work for you and I'll send an invite.",
        "interested": f"Thanks for getting back to me, glad this is of interest. The quickest way to show you is a 15-minute call.{book}\n\nIf you'd rather see details by email first, tell me what matters most to you and I'll send them over.",
        "question": f"Thanks for the question. I want to give you an accurate answer rather than a rushed one, so I'll confirm the details and come back to you shortly.\n\nIf it's easier, we can also go through it on a short call.{book}",
        "not_now": "Understood, and thanks for letting me know. I'll check back in a few months. If the timing changes before then, just reply to this email.",
        "wrong_person": f"Thanks for letting me know. Could you point me to the right person at {lead.get('company') or 'your company'} for this? I'm happy to take it from there.",
    }.get(intent, "Thanks for getting back to me. Happy to help with whatever you need: just let me know.")
    return {"body": hi + body + bye}


def intent_reply(lead, settings):
    """A public post where someone asks for a service (e.g. "need a website for my salon").
    Drafts a helpful, non-salesy reply that a person posts by hand. Nothing is sent automatically."""
    post = {k: lead.get(k) for k in ("company", "notes", "website", "city", "country")}
    post["data"] = _extra(lead)
    if _usable("intent"):
        try:
            return _ask_json(SYSTEM, f"""Someone posted publicly asking for help. Our relevant offer: {settings['offer']}
Post: {json.dumps(post, ensure_ascii=False, default=str)}
Decide if this is a genuine request we can help with, and draft a short, genuinely helpful public reply
(useful advice first, mention brn dynamics once at the end, no hard sell, no links unless natural).
Return JSON: {{"summary": str, "reply_draft": str, "score": int 0-100, "score_reason": str}}""", 700, "intent")
        except Exception as e:
            _note_error(e)
    title = lead.get("notes") or lead.get("company") or "your post"
    return {
        "summary": f"Public request: {title[:160]}",
        "reply_draft": ("A few things that usually help: be clear on the goal (bookings, enquiries or sales), keep the first version small, "
                        "and pick something you can update yourself. If it helps, we build these at brn dynamics and are happy to "
                        "give a free quick opinion, no strings attached."),
        "score": 55, "score_reason": "asks publicly for a service we offer", "_engine": "template",
    }


def _home_market(country):
    """The built-in scoring gives a little extra to leads in the countries you sell in: the ones under Settings > Rules >
    "Only bring in leads from these countries" when that is on, else Saudi Arabia and Kuwait. A lead whose country is not
    known is not marked down for it."""
    from .. import rules
    return not country or country == "--" or country in (rules.wanted_countries() or ["SA", "KW"])


def over_budget():
    budget = float(db.setting("ai_daily_budget") or 0)
    return budget > 0 and spent()["c"] >= budget


def _web_json(prompt, max_uses=5, max_tokens=3000):
    """Ask Claude something it needs web search for, and return the JSON object it ends with."""
    if _client is None:
        raise RuntimeError("ANTHROPIC_API_KEY is not set, so web search is unavailable")
    msgs, text = [{"role": "user", "content": prompt}], ""
    for _ in range(4):  # a long search can pause: continue it
        msg = _client.messages.create(model=model(), max_tokens=max_tokens, messages=msgs,
                                      tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": max_uses}])
        if getattr(msg, "usage", None) is not None:
            _record("discovery", msg.usage)
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        if getattr(msg, "stop_reason", None) != "pause_turn":
            break
        msgs = [msgs[0], {"role": "assistant", "content": msg.content}]
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError("web search returned no list")
    return json.loads(m.group(0))


def find_requests(need, country="", limit=10, linkedin=False):
    """People and businesses who said in public that they need this. Each comes with the page and their own words,
    so it can be checked. Returns [{who, website, email, url, quote, posted, where}]."""
    place = f" Prefer {country}." if country and country != "--" else ""
    data = _web_json(f"""Find up to {int(limit)} RECENT public requests (last 60 days) where a business or person says, in their own words, that they
are looking for this: "{need}".{place}
{"Search PUBLIC LinkedIn posts only (linkedin.com/posts/... and linkedin.com/feed/update/...): people writing that they are looking for a provider, an agency, a freelancer or recommendations. Use web search with site:linkedin.com." if linkedin else "Good places: forum and community posts, Q&A sites, requests for proposals and tenders, project briefs, freelance or job-board gigs, social posts that are publicly readable (LinkedIn included), local business groups. Use web search."}
Only include an item when the BUYER states the need. Never include providers advertising their services, agency websites, listicles,
directories of vendors, or news articles. Do not guess: if you cannot quote their words from the page, leave it out.
Reply with ONLY JSON: {{"requests":[{{"who":"company or person name","country":"the country they are in, only if the page or their profile shows it, else null","website":"their own website or null","email":"only if shown publicly on that page, else null",
"url":"the exact page where they said it","quote":"their words, copied exactly, at most 200 characters","posted":"YYYY-MM-DD or null","where":"name of the site",
"profile":"the author's LinkedIn profile URL (linkedin.com/in/...) if the page shows it, else null","title":"their job title and company if shown, else null"}}]}}""")
    out, seen = [], set()
    for it in data.get("requests") or []:
        url, quote = (it.get("url") or "").strip(), (it.get("quote") or "").strip()
        if not url.startswith("http") or not quote or url in seen or not it.get("who"):
            continue
        seen.add(url)
        out.append({"who": str(it["who"])[:120], "website": (it.get("website") or "").strip() or None, "email": (it.get("email") or "").strip().lower() or None,
                    "url": url, "quote": quote[:300], "posted": (it.get("posted") or "")[:10] or None, "where": str(it.get("where") or "")[:60],
                    "country": str(it.get("country") or "")[:60] or None,
                    "profile": (it.get("profile") or "").strip() if "linkedin.com/in/" in str(it.get("profile") or "") else None, "title": str(it.get("title") or "")[:120] or None})
    return out[:int(limit)]


def find_owner(company, website=None, place=None, titles=()):
    """The person who owns or runs this business, from public pages, with the page that says so.
    Returns {name, title, source, email, phone, linkedin, confidence} or None. Nothing is guessed: no source page, no person."""
    wanted = ", ".join(t for t in titles if t)[:200] or "owner, founder, general manager"
    data = _web_json(f"""Who owns or runs this business? We want ONE real person to write to.
Business: "{company}"{f' · website: {website}' if website else ''}{f' · location: {place}' if place else ''}
Preferred roles, best first: {wanted}. If none of those can be found, the most senior person who represents the business (manager, director).
Where to look: the business's own website (about, team, contact pages), its Google Business, Instagram, Facebook and LinkedIn pages,
business registers, local news and interviews.
Rules:
- The person must be clearly tied to THIS business (same name elsewhere is not enough; check the location or website matches).
- Never guess. Never build an email address from a pattern. Give an email or phone only when a public page shows it for this person or business.
- If you cannot find a named person together with a page that names them in that role, reply {{"person": null}}.
Reply with ONLY JSON: {{"person": {{"name": "full name", "title": "their role at the business", "source": "exact URL of the page that names them in this role",
"email": "or null", "phone": "in international form, or null", "linkedin": "their linkedin.com/in/... profile, or null", "confidence": "high, medium or low"}}}}""", max_uses=4, max_tokens=1500)
    p = data.get("person") or None
    if not isinstance(p, dict):
        return None
    name, source = re.sub(r"\s+", " ", str(p.get("name") or "")).strip(), str(p.get("source") or "").strip()
    if len(name) < 4 or " " not in name or name.lower() == (company or "").lower() or not source.startswith("http") or str(p.get("confidence") or "").lower().startswith("low"):
        return None
    email = str(p.get("email") or "").strip().lower()
    return {"name": name[:80], "title": str(p.get("title") or "")[:100] or None, "source": source[:400], "email": email if re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", email) else None,
            "phone": re.sub(r"[^\d+]", "", str(p.get("phone") or "")) or None,
            "linkedin": str(p["linkedin"]).strip() if "linkedin.com/in/" in str(p.get("linkedin") or "") else None, "confidence": str(p.get("confidence") or "medium").lower()}


def find_websites(companies):
    """The official website of each named company: {name: url}. Names it is not sure about are left out."""
    if not companies:
        return {}
    data = _web_json(f"""Find the official website of each of these companies. Use web search. If you are not confident which company is meant,
leave it out rather than guess. Companies: {json.dumps(companies[:20], ensure_ascii=False)}
Reply with ONLY JSON: {{"sites":[{{"company":"name exactly as given","url":"https://..."}}]}}""", max_uses=6, max_tokens=1500)
    return {x["company"]: x["url"] for x in data.get("sites") or [] if isinstance(x, dict) and str(x.get("url", "")).startswith("http") and x.get("company") in companies}


def suggest_sources(name, pitch, icp):
    """Searches that find people who need this pipeline's offer: what buyers say, and what roles they hire for."""
    if _usable("intent"):
        try:
            out = _ask_json(SYSTEM, f"""We sell: {name}. Pitch: {pitch}. Ideal customer: {icp}.
Give search phrases that find BUYERS with a current need, not providers.
"asks": 4 short phrases a buyer would type or post when they need this (their words, e.g. "need someone to fix my wordpress site").
"hiring": 3 job titles a company advertises when it needs this work done (e.g. "wordpress developer"). Lower case, 2-4 words each.
Return JSON: {{"asks": [str], "hiring": [str]}}""", 400, "intent")
            return {"asks": [str(x)[:80] for x in out.get("asks") or []][:5], "hiring": [str(x)[:50] for x in out.get("hiring") or []][:4], "ai": True}
        except Exception as e:
            _note_error(e)
    base = (name or "this").lower()
    return {"asks": [f"looking for {base}", f"need help with {base}", f"recommend a {base} provider"], "hiring": [], "ai": False}


def discover_businesses(query, country="US", limit=15):
    """Finds real small businesses on the web (Claude web search): e.g. 'lavender farms with online shops'.
    Returns rows with company, website, city, country. Needs ANTHROPIC_API_KEY."""
    if _client is None:
        raise RuntimeError("ANTHROPIC_API_KEY is not set, so web search is unavailable")
    prompt = (f"Find up to {int(limit)} real, currently operating small businesses for this search: \"{query}\" (country: {country}). "
              "Use web search. Only include businesses that have their own website. Skip big chains, marketplaces, directories and social pages. "
              'Reply with ONLY JSON: {"businesses":[{"business":"name","url":"https://their-own-site","city":"city or null"}]}')
    msgs = [{"role": "user", "content": prompt}]
    text = ""
    for _ in range(4):  # a long search can pause: continue it
        msg = _client.messages.create(model=model(), max_tokens=3000, messages=msgs,
                                      tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 6}])
        if getattr(msg, "usage", None) is not None:
            _record("discovery", msg.usage)
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        if getattr(msg, "stop_reason", None) != "pause_turn":
            break
        msgs = [msgs[0], {"role": "assistant", "content": msg.content}]
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError("web search returned no list")
    items = json.loads(m.group(0)).get("businesses") or []
    rows, seen = [], set()
    for it in items:
        url = (it.get("url") or "").strip()
        key = re.sub(r"^https?://(www\.)?", "", url.lower()).rstrip("/")
        if not url.startswith("http") or key in seen or not it.get("business"):
            continue
        seen.add(key)
        rows.append({"company": it["business"], "website": url, "city": it.get("city"), "country": country,
                     "source": "web_search", "consent_basis": "legitimate_interest_b2b"})
    return rows[:int(limit)]
