"""AI phone calls through Vapi (VAPI_API_KEY).

Setting it up by hand means creating an "assistant" in Vapi with the right script, result fields and call-back
address. setup() does that for you, and you pick which of your Vapi phone numbers it calls from. The ids are kept
in Settings (VAPI_ASSISTANT_ID / VAPI_PHONE_NUMBER_ID on the server still win when set).

A call is only placed as a step of a sequence, after the usual checks (lawful basis, local calling hours, daily
limit) and, in approval mode, your click. What the lead said comes back to /webhooks/voice when the call ends."""
import json
import os
import time
from datetime import datetime, timedelta, timezone

import httpx

from .. import db

API = "https://api.vapi.ai"
ROOM = 1.0   # dollars kept free for one more call: a call is only placed while at least this much of the budget is left
_spent = {"at": 0.0, "value": None, "source": ""}
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


def budget():
    try:
        return max(0.0, float(db.setting("call_budget") or 0))
    except ValueError:
        return 0.0


def _month_start():
    return datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _recorded():
    """Call costs this month from the reports Vapi sent us (used when Vapi cannot be asked)."""
    total = 0.0
    for a in db.q("SELECT payload FROM activities WHERE channel='phone' AND created_at>=? AND payload LIKE ?", (_month_start().isoformat(), '%"cost"%')):
        try:
            total += float(json.loads(a["payload"] or "{}").get("cost") or 0)
        except (ValueError, TypeError):
            pass
    return total


def spent(fresh=False):
    """Dollars of calls this calendar month (UTC): Vapi's own figure for the account, else what the call reports added up to."""
    if not fresh and _spent["value"] is not None and time.time() - _spent["at"] < 60:
        return _spent["value"]
    start = _month_start()
    try:
        calls = _call("GET", f"/call?limit=1000&createdAtGe={start.strftime('%Y-%m-%dT%H:%M:%S.000Z')}")
        value = sum(float(c.get("cost") or 0) for c in calls if (c.get("createdAt") or "9") >= start.strftime("%Y-%m-%d"))
        value, source = max(value, _recorded()), "vapi"
    except Exception:
        value, source = _recorded(), "reports"
    _spent.update(at=time.time(), value=value, source=source)
    return value


def _running():
    """Calls placed in the last ten minutes: their cost is not known yet, so room is held for each."""
    since = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    return db.one("SELECT COUNT(*) n FROM activities WHERE channel='phone' AND created_at>=? AND (payload LIKE ? OR summary LIKE ?)",
                  (since, '%"in_progress"%', "Test call placed%"))["n"]


def over_budget():
    """Why no more calls may be placed this month, or None while there is room."""
    cap = budget()
    if not cap:
        return None
    used = spent()
    if used + ROOM > cap:
        return f"this month's AI call budget of ${cap:g} is used up (${used:.2f} spent)"
    if used + ROOM * (_running() + 1) > cap:
        return f"calls in progress may use what is left of this month's ${cap:g} AI call budget"
    return None


def status():
    return {"connected": enabled(), "assistant": bool(assistant_id()), "phone_id": phone_id(), "ready": ready(),
            "fixed_by_server": bool(os.environ.get("VAPI_ASSISTANT_ID") or os.environ.get("VAPI_PHONE_NUMBER_ID"))}


def call(number, name, variables, lead_id=None):
    """Place one call. Returns Vapi's call id."""
    if not ready():
        raise RuntimeError("AI calling is not fully set up: finish it under Settings > Connections.")
    why = over_budget()
    if why:  # every caller checks first; this makes sure nothing slips past
        raise RuntimeError(f"Not called: {why}.")
    out =_call("POST", "/call", {"phoneNumberId": phone_id(), "assistantId": assistant_id(), "customer": {"number": number, "name": name},
                                  "assistantOverrides": {"metadata": {"lead_id": lead_id}, "variableValues": variables}})
    return out.get("id")
