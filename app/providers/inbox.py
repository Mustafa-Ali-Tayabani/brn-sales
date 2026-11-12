"""Reads replies from your mailbox over IMAP (IMAP_HOST, plus SMTP_USER/SMTP_PASSWORD or IMAP_USER/IMAP_PASSWORD).

Every email from a known lead in the last IMAP_LOOKBACK_DAYS (default 14) is recorded once, classified by the
agent (meeting, interested, not now, stop, ...) and shown in the Mailbox. A message counts as a lead's when it
comes from the lead's address, or when it answers an email the agent sent (In-Reply-To / References), so a
colleague replying from another address is still caught. Replies someone already opened in webmail are picked
up too. Messages from anyone else are left untouched and unread."""
import email
import html
import imaplib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime

from .. import db

MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
HEADERS = "(BODY.PEEK[HEADER.FIELDS (FROM MESSAGE-ID IN-REPLY-TO REFERENCES)])"


def enabled():
    return bool(os.environ.get("IMAP_HOST") and (os.environ.get("IMAP_USER") or os.environ.get("SMTP_USER")))


def _decode(part):
    return (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", "replace")


def _text(msg):
    plain = htm = None
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        if "attachment" in str(part.get("Content-Disposition")):
            continue
        if part.get_content_type() == "text/plain" and plain is None:
            plain = _decode(part)
        elif part.get_content_type() == "text/html" and htm is None:
            htm = _decode(part)
    if plain and plain.strip():
        return plain
    if htm:  # HTML-only email: keep the words, drop the markup
        htm = re.sub(r"(?is)<(style|script|head).*?</\1>", "", htm)
        htm = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", htm)
        return html.unescape(re.sub(r"<[^>]+>", "", htm))
    return ""


def _strip_quoted(body):
    out = []
    for line in body.splitlines():
        if line.startswith(">") or (line.startswith("On ") and line.rstrip().endswith("wrote:")) or line.startswith("-----Original Message"):
            break
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()[:4000]


def _refs(msg):
    return re.findall(r"<[^>]+>", f"{msg.get('In-Reply-To', '')} {msg.get('References', '')}")


def _sent_ids():
    """Message-ID -> lead for emails the agent sent recently, to recognise replies from a different address."""
    since = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
    out = {}
    for r in db.q("SELECT lead_id, payload FROM activities WHERE kind='email_sent' AND status='ok' AND lead_id IS NOT NULL AND created_at>=?", (since,)):
        try:
            pid = json.loads(r["payload"] or "{}").get("provider_id") or ""
        except ValueError:
            continue
        if pid.startswith("<"):
            out[pid] = r["lead_id"]
    return out


def _received(msg):
    try:
        d = parsedate_to_datetime(msg.get("Date"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return min(d.astimezone(timezone.utc), datetime.now(timezone.utc)).isoformat(timespec="seconds")
    except Exception:
        return db.now()


def _already_logged(lead_id, text):
    """Replies recorded before Message-IDs were tracked (or pasted in by hand) are matched on their text."""
    for a in db.q("SELECT payload FROM activities WHERE lead_id=? AND kind='email_reply'", (lead_id,)):
        try:
            p = json.loads(a["payload"] or "{}")
        except ValueError:
            continue
        if not p.get("message_id") and p.get("text") == text:
            return True
    return False


def poll(handle_reply):
    """Check the inbox once. Returns the number of lead replies recorded."""
    if not enabled():
        return 0
    user = os.environ.get("IMAP_USER") or os.environ["SMTP_USER"]
    pw = os.environ.get("IMAP_PASSWORD") or os.environ["SMTP_PASSWORD"]
    mine = {user.lower(), (db.setting("sender_email") or "").lower()}
    box = imaplib.IMAP4_SSL(os.environ["IMAP_HOST"], int(os.environ.get("IMAP_PORT", "993")))
    done = 0
    try:
        box.login(user, pw)
        box.select("INBOX")
        d = datetime.now(timezone.utc) - timedelta(days=int(os.environ.get("IMAP_LOOKBACK_DAYS", "14")))
        _, data = box.search(None, "SINCE", f"{d.day:02d}-{MONTHS[d.month - 1]}-{d.year}")
        recent = data[0].split()[-300:]
        _, data = box.search(None, "UNSEEN")
        unseen = set(data[0].split()[-100:])
        nums = sorted(set(recent) | unseen, key=int)
        sent = None
        for i in range(0, len(nums), 100):
            _, data = box.fetch(",".join(n.decode() for n in nums[i:i + 100]), HEADERS)
            for item in data:
                if not isinstance(item, tuple):
                    continue
                num, hdr = item[0].split()[0], email.message_from_bytes(item[1])
                sender = parseaddr(hdr.get("From", ""))[1].lower()
                mid = (hdr.get("Message-ID") or "").strip()
                if not sender or sender in mine:
                    continue
                if mid and db.one("SELECT 1 x FROM inbox_seen WHERE message_id=?", (mid,)):
                    continue
                lead = db.one("SELECT id FROM leads WHERE lower(email)=?", (sender,))
                if not lead and not sender.startswith(("mailer-daemon", "postmaster")):
                    sent = _sent_ids() if sent is None else sent
                    lid = next((sent[r] for r in _refs(hdr) if r in sent), None)
                    lead = {"id": lid} if lid else None
                if not lead:
                    continue  # not a lead: leave it unread for a human
                _, raw = box.fetch(num.decode(), "(RFC822)")  # marks as read in the mailbox
                msg = email.message_from_bytes(raw[0][1])
                subject = str(make_header(decode_header(msg.get("Subject", ""))))
                text = f"Subject: {subject}\n\n{_strip_quoted(_text(msg))}"
                if not _already_logged(lead["id"], text):
                    handle_reply(lead["id"], text, {"from": sender, "subject": subject, "message_id": mid,
                                                    "received_at": _received(msg), "already_read": num not in unseen})
                    done += 1
                if mid:
                    db.ex("INSERT OR IGNORE INTO inbox_seen(message_id, lead_id, created_at) VALUES (?,?,?)", (mid, lead["id"], db.now()))
        db.set_setting("inbox_checked_at", db.now())
    finally:
        try:
            box.logout()
        except Exception:
            pass
    return done
