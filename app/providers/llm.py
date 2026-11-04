"""The agent's brain. Uses Claude when ANTHROPIC_API_KEY is set, otherwise deterministic
templates so the whole pipeline still runs end to end in demo mode."""
import json
import os
import re

MODEL = os.environ.get("BRN_MODEL", "claude-sonnet-5-5")

try:
    import anthropic
    _client = anthropic.Anthropic() if os.environ.get("ANTHROPIC_API_KEY") else None
except Exception:  # pragma: no cover
    _client = None


def enabled():
    return _client is not None


def _ask_json(system, prompt, max_tokens=1200):
    msg = _client.messages.create(
        model=MODEL, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0)) if m else {}


SYSTEM = (
    "You are the SDR agent for brn dynamics, a software and AI agency with offices in Riyadh, Kuwait City, "
    "Karachi and Geneva. You research B2B prospects, score fit, and write short, specific, honest outreach. "
    "Never invent facts about the prospect; if unsure, stay general. Never pressure. Always reply with JSON only."
)


def research(lead, offer, icp):
    if enabled():
        return _ask_json(SYSTEM, f"""Offer: {offer}
Ideal customer: {icp}
Lead: {json.dumps(lead, ensure_ascii=False, default=str)}
Return JSON: {{"summary": str, "pain_points": [str], "hooks": [str], "score": 0-100, "score_reason": str, "language": "ar"|"en"}}""")
    ind = (lead.get("industry") or "").lower()
    fit = any(k in ind for k in ("gym", "fitness", "club", "sport", "yoga", "crossfit", "studio"))
    score = 80 if fit else 35
    if lead.get("country") in ("SA", "KW"):
        score += 10
    return {
        "summary": f"{lead['company']} is a {lead.get('industry') or 'business'} in {lead.get('city') or lead.get('country')}.",
        "pain_points": ["manual membership renewals", "no-shows in classes", "branch reporting in spreadsheets"] if fit else ["unclear"],
        "hooks": ["Arabic/English member app", "automated renewals and payment reminders"],
        "score": min(score, 100),
        "score_reason": "industry matches ICP" if fit else "industry outside ICP",
        "language": "en",
        "_engine": "template",
    }


STEP_GOAL = {
    "intro": "first touch: one specific observation, the outcome we deliver, a soft question",
    "follow_up_1": "short bump adding one new proof point (e.g. results at another gym), ask for a 15 minute call",
    "follow_up_2": "share a useful idea they can use even without us, low pressure",
    "breakup": "polite last note, say we will stop reaching out, leave the door open",
}


def write_email(lead, research_, action, settings):
    if enabled():
        return _ask_json(SYSTEM, f"""Write a cold B2B email.
Goal of this step: {STEP_GOAL.get(action, action)}
Offer: {settings['offer']}
Sender: {settings['sender_name']}. Booking link: {settings['booking_link']}
Lead: {json.dumps(lead, ensure_ascii=False, default=str)}
Research: {json.dumps(research_, ensure_ascii=False)}
Rules: under 110 words, no hype, no fake claims, plain text. Write in {research_.get('language', 'en')}.
Return JSON: {{"subject": str, "body": str}}""", 800)
    name = (lead.get("contact_name") or "there").split(" ")[0]
    company = lead["company"]
    bodies = {
        "intro": f"Hi {name},\n\nRunning {company} across members, classes and renewals usually means a lot of manual follow-up. "
                 f"BRN Gym Club handles memberships, billing reminders, access control and an Arabic/English member app in one place.\n\n"
                 f"Would it be useful if I showed you how a gym in {lead.get('city') or 'the region'} cut missed renewals with it?",
        "follow_up_1": f"Hi {name},\n\nQuick follow-up. Gyms we work with usually recover renewals they were losing to reminders that never went out. "
                       f"Open to a 15 minute call this week? {settings['booking_link']}",
        "follow_up_2": f"Hi {name},\n\nOne idea even if we never work together: send renewal reminders 10, 3 and 0 days before expiry over WhatsApp. "
                       f"It is the single biggest lever we see. Happy to share the template.",
        "breakup": f"Hi {name},\n\nI will stop reaching out after this one. If member management becomes a priority at {company}, "
                   f"you can book time here: {settings['booking_link']}",
    }
    subjects = {"intro": f"{company} renewals", "follow_up_1": "re: renewals", "follow_up_2": "a renewal idea for you", "breakup": "closing the loop"}
    return {"subject": subjects.get(action, company), "body": bodies.get(action, bodies["intro"]), "_engine": "template"}


def call_script(lead, research_, settings):
    if enabled():
        return _ask_json(SYSTEM, f"""Write a short voice-agent brief for an outbound B2B call.
Offer: {settings['offer']}
Lead: {json.dumps(lead, ensure_ascii=False, default=str)}
Research: {json.dumps(research_, ensure_ascii=False)}
The agent must say it is an AI assistant calling for brn dynamics, ask permission to continue, offer to remove them from the list,
and aim to book a 15 minute demo. Return JSON: {{"opening": str, "talking_points": [str], "objections": {{str: str}}, "goal": str, "language": "ar"|"en"}}""")
    return {
        "opening": f"Hi, this is an AI assistant calling on behalf of brn dynamics. Is now an okay moment for 30 seconds about {lead['company']}'s member management?",
        "talking_points": research_.get("hooks", []),
        "objections": {"we have a system": "Totally fair. Most gyms we help switched from one; happy to compare in 15 minutes.",
                       "not interested": "Understood, I will remove you from our list. Thanks for your time."},
        "goal": "book a 15 minute demo",
        "language": research_.get("language", "en"),
        "_engine": "template",
    }


def classify_reply(text):
    """Classify an inbound email reply or call outcome."""
    if enabled():
        return _ask_json(SYSTEM, f"""Classify this prospect reply: {text!r}
Return JSON: {{"intent": "interested"|"meeting"|"not_now"|"not_interested"|"unsubscribe"|"wrong_person"|"question", "summary": str}}""", 300)
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
