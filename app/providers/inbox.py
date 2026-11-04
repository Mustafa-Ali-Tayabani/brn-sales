"""Reads replies from your mailbox over IMAP (IMAP_HOST, plus SMTP_USER/SMTP_PASSWORD or IMAP_USER/IMAP_PASSWORD).
Every unread email from a known lead is classified by the agent (meeting, interested, not now, stop, ...)
and the lead's stage is updated. Messages from unknown senders are left untouched and unread."""
import email
import imaplib
import os
from email.header import decode_header, make_header
from email.utils import parseaddr

from .. import db


def enabled():
    return bool(os.environ.get("IMAP_HOST") and (os.environ.get("IMAP_USER") or os.environ.get("SMTP_USER")))


def _text(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition")):
                return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
        return ""
    return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "replace")


def _strip_quoted(body):
    out = []
    for line in body.splitlines():
        if line.startswith(">") or (line.startswith("On ") and line.rstrip().endswith("wrote:")):
            break
        out.append(line)
    return "\n".join(out).strip()[:4000]


def poll(handle_reply):
    """Check the inbox once. Returns the number of lead replies processed."""
    if not enabled():
        return 0
    user = os.environ.get("IMAP_USER") or os.environ["SMTP_USER"]
    pw = os.environ.get("IMAP_PASSWORD") or os.environ["SMTP_PASSWORD"]
    box = imaplib.IMAP4_SSL(os.environ["IMAP_HOST"], int(os.environ.get("IMAP_PORT", "993")))
    done = 0
    try:
        box.login(user, pw)
        box.select("INBOX")
        _, data = box.search(None, "UNSEEN")
        for num in data[0].split()[-100:]:
            _, hdr = box.fetch(num, "(BODY.PEEK[HEADER.FIELDS (FROM)])")
            sender = parseaddr(email.message_from_bytes(hdr[0][1]).get("From", ""))[1].lower()
            lead = db.one("SELECT id FROM leads WHERE lower(email)=?", (sender,)) if sender else None
            if not lead:
                continue  # not a lead: leave it unread for a human
            _, raw = box.fetch(num, "(RFC822)")  # marks as read
            msg = email.message_from_bytes(raw[0][1])
            subject = str(make_header(decode_header(msg.get("Subject", ""))))
            handle_reply(lead["id"], f"Subject: {subject}\n\n{_strip_quoted(_text(msg))}")
            done += 1
    finally:
        try:
            box.logout()
        except Exception:
            pass
    return done
