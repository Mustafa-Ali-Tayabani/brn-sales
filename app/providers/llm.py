"""The agent's brain. Uses Claude when ANTHROPIC_API_KEY is set, otherwise deterministic
templates so the whole pipeline still runs end to end in demo mode."""
import json
import os
import re

import time

from .. import db

# Model is picked in Settings (default Claude Haiku 5.5, the cheapest). BRN_MODEL on the server overrides it.
MODEL_ENV = os.environ.get("BRN_MODEL")
# USD per million tokens (input, output). Check anthropic.com/pricing if these change.
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.0, 10.0), "claude-opus-5-5": (4.0, 20.0)}
ECONOMY_TASKS = {"email", "reply", "intent"}
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
    days = db.q("""SELECT day, SUM(calls) calls, SUM(input_tokens) input_tokens, SUM(output_tokens) output_tokens, SUM(cost) cost
                   FROM ai_usage WHERE day>=date(?, '-30 day') GROUP BY day ORDER BY day DESC""", (today,))
    tasks = db.q("SELECT task, SUM(calls) calls, SUM(cost) cost FROM ai_usage WHERE day>=date(?, '-30 day') GROUP BY task ORDER BY cost DESC", (today,))
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
Return JSON: {{"summary": str, "pain_points": [str], "hooks": [str], "score": 0-100, "score_reason": str, "language": "ar"|"en"}}""", 800, "research")
        except Exception as e:
            _note_error(e)
    ind = f"{lead.get('industry') or ''} {lead.get('segment') or ''}".lower()
    ex = _extra(lead)
    if product_key == "gym_club" or (not product_key and "gym" in ind):
        fit = any(k in ind for k in ("gym", "fitness", "club", "sport", "yoga", "crossfit", "studio", "pilates"))
        score = (80 if fit else 35) + (10 if lead.get("country") in ("SA", "KW") else 0)
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
    return {"summary": f"{lead['company']} is a {lead.get('segment') or lead.get('industry') or 'business'} in {lead.get('city') or lead.get('country')}.",
            "pain_points": pains, "hooks": hooks, "score": min(score, 100), "score_reason": reason, "language": "en", "_engine": "template"}


STEP_GOAL = {
    "intro": "first touch: one specific observation, the outcome we deliver, a soft question",
    "follow_up_1": "short bump adding one new proof point (e.g. results at another gym), ask for a 15 minute call",
    "follow_up_2": "share a useful idea they can use even without us, low pressure",
    "breakup": "polite last note, say we will stop reaching out, leave the door open",
}


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
the branded template adds a call-to-action button and signature. Write in {research_.get('language', 'en')}.
Return JSON: {{"subject": str, "body": str, "cta": "short button label, 2-4 words"}}""", 800, "email")
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
