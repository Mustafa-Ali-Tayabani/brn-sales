"""Google Calendar and Google Meet. One Google account is connected for the whole team (Settings > Google Calendar):
meetings booked here are created in its calendar with a Meet link, Google emails the invitations, and the Calendar
page shows that calendar's events.

Needs an OAuth client from Google Cloud: GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET, with this server's
/oauth/google/callback address listed as an authorised redirect URI. The connection (a refresh token) is kept in
the database; it only allows reading and writing calendar events."""
import base64
import json
import os
import secrets
import time
from urllib.parse import urlencode

import httpx

from .. import db

SCOPES = "openid email https://www.googleapis.com/auth/calendar.events"
API = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
_access = {"token": None, "until": 0}


class CalendarError(Exception):
    pass


def configured():
    return bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))


def connected():
    return configured() and bool(db.setting("secret_google_refresh_token"))


def account():
    return db.setting("google_account") or ""


def auth_url(redirect_uri):
    state = secrets.token_urlsafe(24)
    db.set_setting("secret_google_state", state)
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": os.environ["GOOGLE_CLIENT_ID"], "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPES,
        "access_type": "offline", "prompt": "consent", "state": state})


def finish(code, state, redirect_uri):
    """Second half of connecting: swap Google's one-time code for a lasting connection."""
    if not state or state != db.setting("secret_google_state"):
        raise CalendarError("That sign-in link has expired. Start again from Settings.")
    r = httpx.post("https://oauth2.googleapis.com/token", timeout=30, data={
        "code": code, "client_id": os.environ["GOOGLE_CLIENT_ID"], "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
        "redirect_uri": redirect_uri, "grant_type": "authorization_code"})
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code >= 400 or not data.get("refresh_token"):
        raise CalendarError(f"Google did not complete the connection: {data.get('error_description') or data.get('error') or r.text[:200]}")
    email = ""
    try:  # who connected, read from the signed token Google just handed us
        part = data["id_token"].split(".")[1]
        email = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))).get("email", "")
    except Exception:
        pass
    db.set_setting("secret_google_refresh_token", data["refresh_token"])
    db.set_setting("google_account", email)
    db.set_setting("secret_google_state", "")
    _access.update(token=data.get("access_token"), until=time.time() + int(data.get("expires_in", 3000)) - 120)
    return email


def disconnect():
    token = db.setting("secret_google_refresh_token")
    if token:
        try:
            httpx.post("https://oauth2.googleapis.com/revoke", data={"token": token}, timeout=15)
        except Exception:
            pass
    db.set_setting("secret_google_refresh_token", "")
    db.set_setting("google_account", "")
    _access.update(token=None, until=0)


def _token():
    if _access["token"] and time.time() < _access["until"]:
        return _access["token"]
    if not connected():
        raise CalendarError("Google Calendar is not connected. Connect it in Settings.")
    r = httpx.post("https://oauth2.googleapis.com/token", timeout=30, data={
        "client_id": os.environ["GOOGLE_CLIENT_ID"], "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
        "refresh_token": db.setting("secret_google_refresh_token"), "grant_type": "refresh_token"})
    data = r.json() if r.status_code < 500 else {}
    if r.status_code >= 400:
        if data.get("error") == "invalid_grant":  # access was removed on Google's side
            db.set_setting("secret_google_refresh_token", "")
            raise CalendarError("Google Calendar access was removed or has expired. Connect it again in Settings.")
        raise CalendarError(f"Google sign-in failed: {data.get('error_description') or r.text[:200]}")
    _access.update(token=data["access_token"], until=time.time() + int(data.get("expires_in", 3000)) - 120)
    return _access["token"]


def _call(method, url, params=None, body=None):
    try:
        r = httpx.request(method, url, params=params, json=body, headers={"Authorization": f"Bearer {_token()}"}, timeout=30)
    except httpx.HTTPError as e:
        raise CalendarError(f"Could not reach Google Calendar: {e}")
    if r.status_code in (404, 410) and method == "DELETE":
        return {}  # already gone
    if r.status_code >= 400:
        try:
            detail = r.json()["error"]["message"]
        except Exception:
            detail = r.text[:200]
        raise CalendarError(f"Google Calendar refused that: {detail}")
    return r.json() if r.content else {}


def _body(m, new):
    body = {"summary": m["title"], "description": m.get("description") or "",
            "start": {"dateTime": m["starts_at"], "timeZone": m.get("timezone") or "UTC"},
            "end": {"dateTime": m["ends_at"], "timeZone": m.get("timezone") or "UTC"},
            "attendees": [{"email": a} for a in m.get("attendees") or []]}
    if m.get("location"):
        body["location"] = m["location"]
    if new:
        body["conferenceData"] = {"createRequest": {"requestId": secrets.token_hex(12), "conferenceSolutionKey": {"type": "hangoutsMeet"}}}
    return body


def _meet(ev):
    return ev.get("hangoutLink") or next((e.get("uri") for e in (ev.get("conferenceData") or {}).get("entryPoints", []) if e.get("entryPointType") == "video"), None)


def create_event(m):
    """Create the calendar event with a Meet link. Google emails the invitation to every attendee."""
    ev = _call("POST", API, {"conferenceDataVersion": 1, "sendUpdates": "all"}, _body(m, True))
    return {"id": ev.get("id"), "meet_link": _meet(ev), "link": ev.get("htmlLink")}


def update_event(event_id, m):
    ev = _call("PATCH", f"{API}/{event_id}", {"conferenceDataVersion": 1, "sendUpdates": "all"}, _body(m, False))
    return {"id": ev.get("id"), "meet_link": _meet(ev), "link": ev.get("htmlLink")}


def delete_event(event_id):
    _call("DELETE", f"{API}/{event_id}", {"sendUpdates": "all"})


def list_events(time_min, time_max):
    """Everything in the connected calendar between two moments (ISO, with offset)."""
    out, page = [], None
    for _ in range(5):
        data = _call("GET", API, {"timeMin": time_min, "timeMax": time_max, "singleEvents": "true", "orderBy": "startTime",
                                  "maxResults": 250, **({"pageToken": page} if page else {})})
        for ev in data.get("items", []):
            s, e = ev.get("start") or {}, ev.get("end") or {}
            out.append({"id": ev.get("id"), "title": ev.get("summary") or "(no title)", "starts_at": s.get("dateTime") or s.get("date"),
                        "ends_at": e.get("dateTime") or e.get("date"), "all_day": "date" in s, "status": ev.get("status"),
                        "meet_link": _meet(ev), "link": ev.get("htmlLink"),
                        "attendees": [{"email": a.get("email"), "response": a.get("responseStatus")} for a in ev.get("attendees") or [] if a.get("email")]})
        page = data.get("nextPageToken")
        if not page:
            break
    return out
