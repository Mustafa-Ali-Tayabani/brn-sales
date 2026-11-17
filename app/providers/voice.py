"""AI phone calls through Vapi (VAPI_API_KEY).

Setting it up by hand means creating an "assistant" in Vapi with the right script, result fields and call-back
address. setup() does that for you, and you pick which of your Vapi phone numbers it calls from. The ids are kept
in Settings (VAPI_ASSISTANT_ID / VAPI_PHONE_NUMBER_ID on the server still win when set).

A call is only placed as a step of a sequence, after the usual checks (lawful basis, local calling hours, daily
limit) and, in approval mode, your click. What the lead said comes back to /webhooks/voice when the call ends."""
import os

import httpx

from .. import db

API = "https://api.vapi.ai"
OUTCOMES = ["meeting", "interested", "not_now", "not_interested", "unsubscribe", "wrong_person", "no_answer", "voicemail"]


def enabled():
    return bool(os.environ.get("VAPI_API_KEY"))


def assistant_id():
    return os.environ.get("VAPI_ASSISTANT_ID") or db.setting("vapi_assistant_id") or ""


def phone_id():
    return os.environ.get("VAPI_PHONE_NUMBER_ID") or db.setting("vapi_phone_id") or ""


def ready():
    return enabled() and bool(assistant_id() and phone_id())


def _call(method, path, body=None):
    if not enabled():
        raise RuntimeError("AI calling is not connected. Set VAPI_API_KEY (your Vapi private key) on the server.")
    try:
        r = httpx.request(method, API + path, json=body, headers={"Authorization": f"Bearer {os.environ['VAPI_API_KEY']}"}, timeout=30)
    except httpx.HTTPError as e:
        raise RuntimeError(f"Could not reach Vapi: {e}")
    if r.status_code in (401, 403):
        raise RuntimeError("Vapi refused the key: check that VAPI_API_KEY is the private key, not the public one.")
    if r.status_code >= 400:
        try:
            msg = r.json().get("message")
            msg = "; ".join(msg) if isinstance(msg, list) else msg
        except Exception:
            msg = r.text
        raise RuntimeError(f"Vapi said: {str(msg)[:300]}")
    return r.json() if r.content else {}


def numbers():
    """The phone numbers in your Vapi account that calls can be placed from."""
    return [{"id": n.get("id"), "number": n.get("number") or n.get("sipUri") or "(no number)", "name": n.get("name") or "", "provider": n.get("provider"), "status": n.get("status")}
            for n in _call("GET", "/phone-number") or []]


def _script():
    company = db.setting("sender_name") or "brn dynamics"
    return f"""You are a polite, brief caller for {company}, phoning a business about something specific to them.
The person you are calling: {{{{contact_name}}}} at {{{{company}}}}.
What to raise, in your own words and only if it fits the conversation: {{{{talking_points}}}}

How to behave:
- Say who you are and why you are calling in one sentence, then ask if they have a minute. If not, offer to call back or email, and end the call.
- Speak the language they answer in (Arabic or English). Short sentences. One question at a time. Never talk over them.
- Never invent prices, clients, features or dates. If asked something you do not know, say a colleague will follow up with the details.
- The goal is a short meeting with a colleague, or permission to send details by email. Do not push after a clear no.
- If they ask not to be called again, apologise, confirm they will not be contacted, and end the call.
- If you reach voicemail, leave one sentence with the company name and that you will email, then end the call.
- Say you are an AI assistant if asked. Keep the whole call under three minutes."""


def setup(server_url, chosen_phone=None):
    """Create the calling assistant in Vapi (or bring the existing one up to date) and remember which number to call from."""
    body = {
        "name": f"{db.setting('sender_name') or 'brn'} outreach caller",
        "firstMessage": "{{opening}}",
        "model": {"provider": "openai", "model": "gpt-4o", "temperature": 0.4, "messages": [{"role": "system", "content": _script()}]},
        "server": {"url": server_url}, "serverMessages": ["end-of-call-report"],
        "maxDurationSeconds": 240, "endCallPhrases": ["goodbye", "have a good day", "مع السلامة"],
        "analysisPlan": {"structuredDataPlan": {"enabled": True, "schema": {"type": "object", "required": ["outcome"], "properties": {
            "outcome": {"type": "string", "enum": OUTCOMES, "description": "meeting: agreed to a meeting. interested: wants details. not_now: timing is wrong. "
                        "not_interested: declined. unsubscribe: asked not to be contacted. wrong_person: not the right contact. no_answer / voicemail: nobody to talk to."}}}},
                         "summaryPlan": {"enabled": True}},
    }
    existing = assistant_id() if not os.environ.get("VAPI_ASSISTANT_ID") else ""
    if existing:
        try:
            _call("PATCH", f"/assistant/{existing}", body)
        except RuntimeError:
            existing = ""   # it was deleted in Vapi: make a new one
    if not existing and not os.environ.get("VAPI_ASSISTANT_ID"):
        db.set_setting("vapi_assistant_id", _call("POST", "/assistant", body)["id"])
    if chosen_phone:
        db.set_setting("vapi_phone_id", chosen_phone)
    elif not phone_id():
        mine = [n for n in numbers() if n["status"] in (None, "active")]
        if len(mine) == 1:
            db.set_setting("vapi_phone_id", mine[0]["id"])
    return status()


def status():
    return {"connected": enabled(), "assistant": bool(assistant_id()), "phone_id": phone_id(), "ready": ready(),
            "fixed_by_server": bool(os.environ.get("VAPI_ASSISTANT_ID") or os.environ.get("VAPI_PHONE_NUMBER_ID"))}


def call(number, name, variables, lead_id=None):
    """Place one call. Returns Vapi's call id."""
    if not ready():
        raise RuntimeError("AI calling is not fully set up: finish it under Settings > Connections.")
    out = _call("POST", "/call", {"phoneNumberId": phone_id(), "assistantId": assistant_id(), "customer": {"number": number, "name": name},
                                  "assistantOverrides": {"metadata": {"lead_id": lead_id}, "variableValues": variables}})
    return out.get("id")
