"""Reads replies from your mailbox over IMAP (IMAP_HOST, plus SMTP_USER/SMTP_PASSWORD or IMAP_USER/IMAP_PASSWORD).

Every email from a known lead in the last IMAP_LOOKBACK_DAYS (default 14) is recorded once, classified by the
agent (meeting, interested, not now, stop, ...) and shown in the Mailbox. A message counts as a lead's when it
comes from the lead's address, or when it answers an email the agent sent (In-Reply-To / References), so a
colleague replying from another address is still caught. Replies someone already opened in webmail are picked
up too. Nothing is marked as read: that only happens when a person opens the message."""
import email
import html
import json
import os
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime

from .. import db
from . import mailbox

MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
HEADERS = "(BODY.PEEK[HEADER.FIELDS (FROM MESSAGE-ID IN-REPLY-TO REFERENCES CONTENT-TYPE SUBJECT X-FAILED-RECIPIENTS)])"
HARD_WORDS = ("user unknown", "no such user", "does not exist", "doesn't exist", "not found", "unknown recipient", "invalid recipient", "mailbox unavailable",
              "address rejected", "no mailbox", "recipient rejected", "account disabled", "550 ", "551 ", "553 ", "5.1.1", "5.1.0", "5.4.1", "nxdomain", "domain not found")
SOFT_WORDS = ("mailbox full", "over quota", "quota exceeded", "temporar", "try again", "delayed", "4.2.2", "452 ", "421 ")


def is_bounce(hdr, sender):
    """Is this a mail server telling us an email could not be delivered?"""
    ctype, subject = (hdr.get("Content-Type") or "").lower(), (hdr.get("Subject") or "").lower()
    return ("delivery-status" in ctype or sender.startswith(("mailer-daemon", "postmaster"))
            or any(w in subject for w in ("undeliverable", "undelivered mail", "delivery status notification", "delivery failure", "returned mail", "failure notice")))


def bounce_details(msg):
    """[(address, reason, hard)] from a bounce notice: the machine-readable report when there is one, else the wording."""
    out = []
    for part in msg.walk():
        if part.get_content_type() == "message/delivery-status":
            for block in part.get_payload() if isinstance(part.get_payload(), list) else []:
                rcpt = block.get("Final-Recipient") or block.get("Original-Recipient")
                if rcpt:
                    status, action = (block.get("Status") or "").strip(), (block.get("Action") or "").strip().lower()
                    if action in ("delivered", "relayed", "expanded"):
                        continue
                    hard = status.startswith("5") or (action == "failed" and not status.startswith("4"))
                    out.append((rcpt.split(";")[-1].strip().strip("<>").lower(), re.sub(r"\s+", " ", block.get("Diagnostic-Code") or status)[:200], hard))
    if out:
        return out
    text = _text(msg).lower()
    hard = any(w in text for w in HARD_WORDS) and not any(w in text for w in SOFT_WORDS)
    found = [a.strip().lower() for a in (msg.get("X-Failed-Recipients") or "").split(",") if "@" in a]
    if not found:  # no report and no header: look for a lead's address in the wording
        mine = {mailbox.address(), (db.setting("sender_email") or "").lower()}
        for a in dict.fromkeys(re.findall(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+", text)):
            if a not in mine and db.one("SELECT 1 x FROM leads WHERE lower(email)=?", (a,)):
                found.append(a)
    reason = next((line.strip()[:200] for line in _text(msg).splitlines() if any(w in line.lower() for w in HARD_WORDS + SOFT_WORDS)), "")
    return [(a, reason, hard) for a in found]


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


def poll(handle_reply, handle_bounce=None):
    """Check the inbox once. Returns the number of lead replies recorded. Bounce notices go to handle_bounce."""
    if not enabled():
        return 0
    mine = {mailbox.address(), (db.setting("sender_email") or "").lower()}
    box = mailbox.connect()  # its own connection, so a long check never blocks the Mailbox page
    done = 0
    try:
        box.select("INBOX", readonly=True)  # reading a reply here must not mark it as read: a person still has to see it
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
                if handle_bounce and is_bounce(hdr, sender):
                    _, raw = box.fetch(num.decode(), "(BODY.PEEK[])")
                    for address, reason, hard in bounce_details(email.message_from_bytes(raw[0][1])):
                        handle_bounce(address, reason, hard)
                    if mid:
                        db.ex("INSERT OR IGNORE INTO inbox_seen(message_id, lead_id, created_at) VALUES (?,?,?)", (mid, None, db.now()))
                    continue
                lead = db.one("SELECT id FROM leads WHERE lower(email)=?", (sender,))
                if not lead and not sender.startswith(("mailer-daemon", "postmaster")):
                    sent = _sent_ids() if sent is None else sent
                    lid = next((sent[r] for r in _refs(hdr) if r in sent), None)
                    lead = {"id": lid} if lid else None
                if not lead:
                    continue  # not a lead: leave it unread for a human
                _, raw = box.fetch(num.decode(), "(BODY.PEEK[])")
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
