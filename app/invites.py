"""Meeting invitations, sent by this app itself.

The calendar lives here, not in Google. A Google Meet link cannot be created without signing in to Google, so
instead you paste one or more permanent Meet links in Settings (meet.google.com > New meeting > Create a meeting
for later). Each meeting is given one of them, picking a link that no other meeting is using at that time.

Everyone invited gets an email with the details and a calendar file (.ics), which Gmail, Outlook and Apple Mail
show as an invitation they can add to their own calendar. Moving or cancelling a meeting sends an update."""
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import compliance, db
from .providers import channels, mailbox


def meet_links():
    return [x.strip() for x in (db.setting("meet_links") or "").replace(",", "\n").splitlines() if x.strip().lower().startswith("http")]


def pick_link(starts_at, ends_at, except_id=None):
    """A Meet link free for that time. With every link taken it falls back to the first one and says so."""
    links = meet_links()
    if not links:
        return None, False
    taken = {m["meet_link"] for m in db.q("SELECT id, meet_link FROM meetings WHERE status='scheduled' AND starts_at<? AND ends_at>? AND meet_link IS NOT NULL",
                                          (ends_at, starts_at)) if m["id"] != except_id}
    free = next((x for x in links if x not in taken), None)
    return (free or links[0]), free is None


def when_text(m):
    try:
        tz = ZoneInfo(m.get("timezone") or "UTC")
    except Exception:
        tz = timezone.utc
    s, e = datetime.fromisoformat(m["starts_at"]).astimezone(tz), datetime.fromisoformat(m["ends_at"]).astimezone(tz)
    return f"{s.strftime('%A %d %B %Y, %H:%M')} to {e.strftime('%H:%M')} ({m.get('timezone') or 'UTC'})"


def _esc(text):
    return str(text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n")


def _fold(line):
    """Calendar files wrap long lines at 75 bytes, continuing with a leading space."""
    raw, out = line.encode(), []
    while len(raw) > 73:
        cut = 73
        while cut > 0 and (raw[cut] & 0xC0) == 0x80:  # never split a multi-byte character
            cut -= 1
        out.append(raw[:cut].decode())
        raw = b" " + raw[cut:]
    out.append(raw.decode())
    return "\r\n".join(out)


def ics(m, attendees, method="REQUEST"):
    sender = channels._sender() or mailbox.address() or "noreply@localhost"
    stamp = lambda iso: datetime.fromisoformat(iso).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    about = "\n\n".join(x for x in [f"Join: {m['meet_link']}" if m.get("meet_link") else "", m.get("description") or ""] if x)
    lines = ["BEGIN:VCALENDAR", "PRODID:-//brn dynamics//brn pipeline//EN", "VERSION:2.0", "CALSCALE:GREGORIAN", f"METHOD:{method}", "BEGIN:VEVENT",
             f"UID:meeting-{m['id']}@{sender.split('@')[-1]}", f"SEQUENCE:{int(m.get('ics_seq') or 0)}", f"DTSTAMP:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
             f"DTSTART:{stamp(m['starts_at'])}", f"DTEND:{stamp(m['ends_at'])}", f"SUMMARY:{_esc(m['title'])}", f"DESCRIPTION:{_esc(about)}",
             f"LOCATION:{_esc(m.get('meet_link') or m.get('location') or '')}", f"ORGANIZER;CN={_esc(db.setting('sender_name') or sender)}:mailto:{sender}"]
    if m.get("meet_link"):
        lines.append(f"URL:{m['meet_link']}")
    lines += [f"ATTENDEE;CN={_esc(a)};ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{a}" for a in attendees]
    lines += [f"STATUS:{'CANCELLED' if method == 'CANCEL' else 'CONFIRMED'}", "TRANSP:OPAQUE"]
    if method != "CANCEL":
        lines += ["BEGIN:VALARM", "TRIGGER:-PT15M", "ACTION:DISPLAY", "DESCRIPTION:Meeting in 15 minutes", "END:VALARM"]
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_fold(x) for x in lines) + "\r\n"


def send(m, kind="invite"):
    """Email the invitation (or the update, or the cancellation) to everyone on the meeting.
    Returns the addresses it went to; empty when there is nobody to tell or email is not connected."""
    people = [a for a in dict.fromkeys(x["email"] if isinstance(x, dict) else x for x in m.get("attendees") or []) if a and not compliance.is_suppressed({"email": a})]
    if not people or not channels.email_provider():
        return []
    method = "CANCEL" if kind == "cancel" else "REQUEST"
    company = db.setting("sender_name") or "brn dynamics"
    subject = {"invite": "Invitation: ", "update": "Updated: ", "cancel": "Cancelled: "}[kind] + m["title"]
    body = {"invite": f"You are invited to a meeting with {company}.", "update": f"This meeting with {company} has changed. The new details:",
            "cancel": f"This meeting with {company} has been cancelled."}[kind]
    body += f"\n\n{m['title']}\nWhen: {when_text(m)}"
    if kind != "cancel":
        body += (f"\nJoin with Google Meet: {m['meet_link']}" if m.get("meet_link") else "") + (f"\n\n{m['description']}" if m.get("description") else "")
        body += "\n\nThe attached invitation adds it to your calendar. If the time no longer works, just reply to this email."
    body += f"\n\n{company}"
    msg = mailbox.build(company, channels._sender() or mailbox.address(), {"to": ", ".join(people), "subject": subject, "body": body})
    cal = ics(m, people, method)
    msg.add_alternative(cal, subtype="calendar", params={"method": method})  # what mail apps turn into an "Add to calendar" card
    msg.add_attachment(cal.encode(), maintype="text", subtype="calendar", filename="invite.ics", params={"method": method})
    try:
        channels.send_message(msg)
    except Exception as e:
        db.log(m.get("lead_id"), "meeting", f"Could not email the meeting invitation: {e}", channel="calendar", status="error")
        return []
    return people
