"""The whole mailbox over IMAP, for the Mailbox page: folders (Inbox, Sent, Drafts, Spam, Trash, ...), message lists,
reading, flags, moving and deleting, drafts, and keeping a copy of everything sent in the Sent folder.

Uses the same account as inbox.py (IMAP_HOST, plus IMAP_USER/IMAP_PASSWORD or SMTP_USER/SMTP_PASSWORD). Nothing is
copied into the database: the mail server stays the source of truth, so webmail and this page always agree.
One connection is kept open and shared; it reconnects by itself when the server drops it."""
import base64
import email
import html as htmllib
import imaplib
import os
import quopri
import re
import threading
import time
from datetime import datetime, timezone
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formataddr, formatdate, getaddresses, make_msgid

from .. import db

_lock = threading.RLock()
_box = None
_folders = {"at": 0, "list": []}
_raw = {}            # (folder, uid) -> raw message, a few most recent
_unread = {"at": 0, "n": None, "busy": False}
_warned = {"at": 0}

ROLE_FLAGS = {"\\sent": "sent", "\\drafts": "drafts", "\\trash": "trash", "\\junk": "spam", "\\archive": "archive"}
ROLE_NAMES = {"sent": "sent", "sent items": "sent", "sent mail": "sent", "sent messages": "sent", "drafts": "drafts", "draft": "drafts",
              "trash": "trash", "deleted items": "trash", "deleted messages": "trash", "bin": "trash",
              "junk": "spam", "spam": "spam", "junk e-mail": "spam", "junk email": "spam", "archive": "archive", "archives": "archive"}
LABELS = {"inbox": "Inbox", "sent": "Sent", "drafts": "Drafts", "archive": "Archive", "spam": "Spam", "trash": "Trash"}
ORDER = ["inbox", "sent", "drafts", "archive", "spam", "trash"]
CREATE_AS = {"sent": "Sent", "drafts": "Drafts", "trash": "Trash", "spam": "Junk", "archive": "Archive"}
LIST_RE = re.compile(rb'\((?P<flags>[^)]*)\) (?:"(?P<d>(?:[^"\\]|\\.)*)"|NIL) (?P<name>.*)', re.S)
LIST_FIELDS = "(UID FLAGS INTERNALDATE RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE CONTENT-TYPE CONTENT-TRANSFER-ENCODING)] BODY.PEEK[TEXT]<0.3000>)"


# what an email's HTML may load when shown in the page: images, styles and fonts. No scripts, frames or forms.
CSP = "default-src 'none'; img-src data: https: http:; style-src 'unsafe-inline' https:; font-src data: https:"
CSP_META = f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'


class MailboxError(Exception):
    pass


def enabled():
    return bool(os.environ.get("IMAP_HOST") and (os.environ.get("IMAP_USER") or os.environ.get("SMTP_USER")))


def address():
    return (os.environ.get("IMAP_USER") or os.environ.get("SMTP_USER") or "").lower()


def connect():
    """A logged-in IMAP connection. IMAP_SSL=0 talks plain IMAP, for a local test server only."""
    host, port = os.environ["IMAP_HOST"], int(os.environ.get("IMAP_PORT", "993"))
    box = imaplib.IMAP4(host, port, timeout=25) if os.environ.get("IMAP_SSL", "1") == "0" else imaplib.IMAP4_SSL(host, port, timeout=25)
    box.login(os.environ.get("IMAP_USER") or os.environ["SMTP_USER"], os.environ.get("IMAP_PASSWORD") or os.environ.get("SMTP_PASSWORD", ""))
    try:  # capabilities often grow after login (MOVE, UIDPLUS)
        box.caps = set(b" ".join(x for x in box.capability()[1] if x).decode().upper().split())
    except Exception:
        box.caps = {c.upper() for c in box.capabilities}
    return box


def _run(fn):
    """Run fn(connection) on the shared connection, reconnecting once if the server has dropped it."""
    global _box
    if not enabled():
        raise MailboxError("Mailbox not connected. Set IMAP_HOST, SMTP_USER and SMTP_PASSWORD.")
    with _lock:
        for attempt in (0, 1):
            try:
                if _box is None:
                    _box = connect()
                return fn(_box)
            except MailboxError:
                # imaplib remembers a "read-only" refusal and would fail the next, unrelated command with it
                _box is not None and _box.untagged_responses.pop("READ-ONLY", None)
                raise
            except imaplib.IMAP4.readonly:
                _box is not None and _box.untagged_responses.pop("READ-ONLY", None)
                raise MailboxError("That folder is read-only on the mail server, so it can't be changed from here.")
            except (imaplib.IMAP4.abort, OSError, EOFError) as e:
                try:
                    _box and _box.shutdown()
                except Exception:
                    pass
                _box = None
                if attempt:
                    raise MailboxError(f"Could not reach the mailbox: {e}")
            except imaplib.IMAP4.error as e:
                if _box is not None and _box.state == "NONAUTH":
                    _box = None
                raise MailboxError(_clean(e))


def _clean(e):
    t = str(e)
    if t.startswith("b'") or t.startswith('b"'):
        t = t[2:-1]
    return "The mailbox rejected the login: check the address and password." if "AUTH" in t.upper() and "FAIL" in t.upper() else t


def _ok(res, what):
    typ, data = res
    if typ != "OK":
        detail = b" ".join(x for x in data if isinstance(x, bytes)).decode("utf-8", "replace") if data else ""
        raise MailboxError(f"{what}: {detail or typ}")
    return data


def _q(name):
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _utf7(s):
    """Folder names arrive in IMAP's modified UTF-7."""
    def dec(m):
        if not m.group(1):
            return "&"
        try:
            return base64.b64decode(m.group(1).replace(",", "/") + "=" * (-len(m.group(1)) % 4)).decode("utf-16-be")
        except Exception:
            return m.group(0)
    return re.sub(r"&([^-]*)-", dec, s)


def _hdr(v):
    if not v:
        return ""
    try:
        return re.sub(r"\s+", " ", str(make_header(decode_header(str(v))))).strip()
    except Exception:
        return str(v)


def _addrs(v):
    return [{"name": _hdr(n), "email": a} for n, a in getaddresses([str(v)] if v else []) if a]


# ---------- folders ----------
def _list(box):
    out = []
    for item in _ok(box.list(), "Could not list folders"):
        if not item:
            continue
        head, name = (item[0], item[1].decode("utf-8", "replace")) if isinstance(item, tuple) else (item, None)
        m = LIST_RE.match(head)
        if not m:
            continue
        flags = m["flags"].decode().lower().split()
        if "\\noselect" in flags or "\\nonexistent" in flags:
            continue
        if name is None:
            name = m["name"].decode("utf-8", "replace").strip()
            if name.startswith('"'):
                name = re.sub(r"\\(.)", r"\1", name[1:-1])
        delim = re.sub(r"\\(.)", r"\1", (m["d"] or b"").decode())
        role = "inbox" if name.upper() == "INBOX" else next((ROLE_FLAGS[f] for f in flags if f in ROLE_FLAGS), None)
        out.append({"name": name, "delim": delim, "role": role})
    taken = {f["role"] for f in out if f["role"]}
    for f in out:  # servers without special-use flags: go by the folder's name
        leaf = (f["name"].split(f["delim"])[-1] if f["delim"] else f["name"]).lower()
        if not f["role"] and ROLE_NAMES.get(leaf) and ROLE_NAMES[leaf] not in taken:
            f["role"] = ROLE_NAMES[leaf]; taken.add(f["role"])
    seen = set()
    for f in out:  # one folder per role
        if f["role"] in seen:
            f["role"] = None
        elif f["role"]:
            seen.add(f["role"])
        shown = _utf7(f["name"])
        if f["delim"] and shown.upper().startswith("INBOX" + f["delim"]):
            shown = shown[6:]
        f["label"] = LABELS.get(f["role"]) or shown.replace(f["delim"] or "\0", " / ")
    out.sort(key=lambda f: (ORDER.index(f["role"]) if f["role"] in ORDER else 99, f["label"].lower()))
    return out


def _folder_list(box, fresh=False):
    if fresh or not _folders["list"] or time.time() - _folders["at"] > 600:
        _folders["list"], _folders["at"] = _list(box), time.time()
    return _folders["list"]


def _status(box, name):
    try:
        d = _ok(box.status(_q(name), "(MESSAGES UNSEEN)"), "status")
        line = b" ".join(x if isinstance(x, bytes) else b" ".join(x) for x in d if x)
        return int(re.search(rb"MESSAGES (\d+)", line)[1]), int(re.search(rb"UNSEEN (\d+)", line)[1])
    except Exception:
        return None, None


def folders():
    def go(box):
        out = []
        for f in _folder_list(box)[:25]:
            total, unseen = _status(box, f["name"])
            out.append({"name": f["name"], "label": f["label"], "role": f["role"], "total": total, "unread": unseen})
            if f["role"] == "inbox" and unseen is not None:
                _unread.update(at=time.time(), n=unseen)
        return out
    return _run(go)


def folder_for(role, create=False):
    """The real folder name behind a role (sent, drafts, trash, spam, archive)."""
    def go(box):
        f = next((f for f in _folder_list(box) if f["role"] == role), None)
        if f or not create:
            return f["name"] if f else None
        sample = next((x for x in _folder_list(box) if x["delim"] and x["name"].upper().startswith("INBOX" + x["delim"])), None)
        name = (f"INBOX{sample['delim']}" if sample else "") + CREATE_AS[role]
        _ok(box.create(_q(name)), f"Could not create the {role} folder")
        box.subscribe(_q(name))
        _folder_list(box, fresh=True)
        return name
    return _run(go)


def inbox_unread():
    """Unread count for the sidebar badge. Never waits on the mail server: refreshes in the background when stale."""
    if not enabled():
        return None
    if time.time() - _unread["at"] > 60 and not _unread["busy"]:
        _unread["busy"] = True

        def refresh():
            try:
                n = _run(lambda box: _status(box, "INBOX")[1])
                _unread.update(at=time.time(), n=n if n is not None else _unread["n"])
            except Exception:
                _unread["at"] = time.time()
            finally:
                _unread["busy"] = False
        threading.Thread(target=refresh, daemon=True).start()
    return _unread["n"]


# ---------- lists ----------
def _groups(data):
    """imaplib FETCH data -> one (text outside literals, {section: literal}) per message."""
    out = []
    for item in data or []:
        head = item[0] if isinstance(item, tuple) else item
        if not isinstance(head, bytes):
            continue
        if re.match(rb"\d+ \(", head) or not out:
            out.append([b"", {}])
        out[-1][0] += head + b" "
        if isinstance(item, tuple):
            m = re.search(rb"(BODY\[[^\]]*\](?:<\d+>)?|RFC822[.\w]*) \{\d+\}\s*$", head)
            out[-1][1][m[1].decode() if m else "?"] = item[1]
    return out


def _meta(text):
    uid = re.search(rb"\bUID (\d+)", text)
    flags = re.search(rb"FLAGS \(([^)]*)\)", text)
    when = re.search(rb'INTERNALDATE "([^"]+)"', text)
    size = re.search(rb"RFC822\.SIZE (\d+)", text)
    at = None
    if when:
        try:
            at = datetime.strptime(when[1].decode().strip(), "%d-%b-%Y %H:%M:%S %z").astimezone(timezone.utc).isoformat(timespec="seconds")
        except ValueError:
            pass
    return (int(uid[1]) if uid else None, (flags[1].decode().lower().split() if flags else []), at, int(size[1]) if size else 0)


def _plain(htm):
    htm = re.sub(r"(?is)<(style|script|head|title)\b.*?(</\1>|$)", " ", htm)
    htm = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>|</h\d>", "\n", htm)
    return htmllib.unescape(re.sub(r"(?s)<[^>]*(>|$)", "", htm))


def _decode_partial(h, body):
    cte = (h.get("Content-Transfer-Encoding") or "").lower().strip()
    if cte == "base64":
        b = re.sub(rb"[^A-Za-z0-9+/]", b"", body)
        body = base64.b64decode(b[:len(b) // 4 * 4])
    elif cte == "quoted-printable":
        body = quopri.decodestring(body)
    text = body.decode(h.get_content_charset() or "utf-8", "replace")
    return _plain(text) if h.get_content_type() == "text/html" else text


def _snippet(h, raw, depth=0):
    """A one-line preview from the first 3 KB of the body, without downloading the message."""
    try:
        if h.get_content_maintype() == "multipart" and h.get_boundary() and depth < 3:
            for part in raw.split(b"--" + h.get_boundary().encode())[1:]:
                bits = re.split(rb"\r?\n\r?\n", part.lstrip(b"\r\n"), 1)
                ph = email.message_from_bytes(bits[0] + b"\r\n\r\n")
                if len(bits) == 2 and (ph.get_content_maintype() in ("text", "multipart")) and not ph.get_filename():
                    s = _snippet(ph, bits[1], depth + 1)
                    if s:
                        return s
            return ""
        if h.get_content_maintype() != "text":
            return ""
        return " ".join(_decode_partial(h, raw).split())[:160]
    except Exception:
        return ""


def _select(box, folder, readonly=True):
    _ok(box.select(_q(folder), readonly=readonly), "Could not open that folder")


def messages(folder, page=0, size=50, q=None, flt=None):
    def go(box):
        _select(box, folder)
        crit = [c for c in ({"unread": "UNSEEN", "starred": "FLAGGED"}.get(flt),) if c]
        if q and q.isascii():
            crit += ["TEXT", _q(q)]
        elif q:
            box.literal = q.encode("utf-8")
            crit = ["CHARSET", "UTF-8"] + crit + ["TEXT"]
        uids = (_ok(box.uid("SEARCH", *(crit or ["ALL"])), "Search failed")[0] or b"").split()[::-1]  # newest first
        pick = uids[page * size:(page + 1) * size]
        rows = []
        if pick:
            for text, lit in _groups(_ok(box.uid("FETCH", b",".join(pick).decode(), LIST_FIELDS), "Could not read the folder")):
                uid, flags, at, sz = _meta(text)
                hb = next((v for k, v in lit.items() if k.startswith("BODY[HEADER")), None)
                if uid is None or hb is None:
                    continue
                h = email.message_from_bytes(hb)
                frm = (_addrs(h.get("From")) or [{"name": "", "email": ""}])[0]
                if not at:
                    try:
                        at = email.utils.parsedate_to_datetime(h.get("Date")).astimezone(timezone.utc).isoformat(timespec="seconds")
                    except Exception:
                        at = None
                rows.append({"uid": uid, "from": frm, "to": _addrs(h.get("To"))[:4], "subject": _hdr(h.get("Subject")), "at": at, "size": sz,
                             "seen": "\\seen" in flags, "starred": "\\flagged" in flags, "answered": "\\answered" in flags,
                             "attach": h.get_content_type() == "multipart/mixed",
                             "snippet": _snippet(h, next((v for k, v in lit.items() if k.startswith("BODY[TEXT")), b""))})
            rows.sort(key=lambda r: -r["uid"])
        return {"total": len(uids), "page": page, "size": size, "rows": rows}
    return _run(go)


# ---------- one message ----------
def raw(folder, uid, seen=False):
    """The full message. seen=True also marks it read."""
    def go(box):
        key = (folder, int(uid))
        if key not in _raw or seen:
            _select(box, folder, readonly=not seen)
            g = _groups(_ok(box.uid("FETCH", str(int(uid)), "(UID FLAGS BODY.PEEK[])"), "Could not read the message"))
            body = next((v for _, lit in g for k, v in lit.items() if k.startswith("BODY[]")), None)
            if body is None:
                raise MailboxError("That message is no longer there.")
            flags = next((_meta(t)[1] for t, lit in g if lit), [])
            if seen and "\\seen" not in flags:
                box.uid("STORE", str(int(uid)), "+FLAGS.SILENT", "(\\Seen)")
                flags.append("\\seen"); _unread["at"] = 0
            _raw[key] = (body, flags)
            while len(_raw) > 12:
                _raw.pop(next(iter(_raw)))
        return _raw[key]
    return _run(go)


def parts(msg):
    """(text, html, attachments) of a parsed message. Attachments keep their order, so an index identifies one."""
    text = htm = None
    atts = []
    for p in msg.walk():
        if p.is_multipart():
            continue
        ctype, disp, name = p.get_content_type(), p.get_content_disposition(), p.get_filename()
        if disp != "attachment" and not name and ctype in ("text/plain", "text/html"):
            try:
                body = p.get_content()
            except Exception:
                body = (p.get_payload(decode=True) or b"").decode("utf-8", "replace")
            if ctype == "text/plain" and text is None:
                text = body
                continue
            if ctype == "text/html" and htm is None:
                htm = body
                continue
        atts.append(p)
    return text, htm, atts


def parse(rawbytes):
    return email.message_from_bytes(rawbytes, policy=policy.default)


def message(folder, uid, seen=True):
    body, flags = raw(folder, uid, seen=seen)
    msg = parse(body)
    text, htm, atts = parts(msg)
    try:
        at = email.utils.parsedate_to_datetime(str(msg.get("Date"))).astimezone(timezone.utc).isoformat(timespec="seconds")
    except Exception:
        at = None
    inline = lambda p: bool(htm and p.get("Content-ID") and str(p.get("Content-ID")).strip("<>") in htm)
    return {"folder": folder, "uid": int(uid), "subject": _hdr(msg.get("Subject")), "from": (_addrs(msg.get("From")) or [{"name": "", "email": ""}])[0],
            "to": _addrs(msg.get("To")), "cc": _addrs(msg.get("Cc")), "bcc": _addrs(msg.get("Bcc")), "reply_to": _addrs(msg.get("Reply-To")),
            "at": at, "seen": "\\seen" in flags, "starred": "\\flagged" in flags,
            "text": (text if text is not None else _plain(htm or "")).strip(), "has_html": htm is not None,
            "forward": _fwd_ref(msg), "message_id": str(msg.get("Message-ID") or "").strip(), "in_reply_to": str(msg.get("In-Reply-To") or "").strip(),
            "references": " ".join(str(msg.get("References") or "").split()),
            "attachments": [{"i": i, "name": _hdr(p.get_filename()) or f"attachment-{i + 1}", "type": p.get_content_type(),
                             "size": len(p.get_payload(decode=True) or b"")} for i, p in enumerate(atts) if not inline(p)]}


def _fwd_ref(msg):
    try:
        uid, folder = str(msg.get("X-Brn-Forward") or "").split(" ", 1)
        return {"uid": int(uid), "folder": base64.b64decode(folder).decode()}
    except Exception:
        return None


def html_body(folder, uid):
    """The HTML part, with inline images embedded and links opening in a new tab. Served sandboxed, scripts off."""
    msg = parse(raw(folder, uid)[0])
    _, htm, atts = parts(msg)
    if htm is None:
        return None
    for p in atts:
        cid = str(p.get("Content-ID") or "").strip("<>")
        data = p.get_payload(decode=True) or b""
        if cid and f"cid:{cid}" in htm and len(data) < 3_000_000:
            htm = htm.replace(f"cid:{cid}", f"data:{p.get_content_type()};base64,{base64.b64encode(data).decode()}")
    return CSP_META + '<base target="_blank"><style>body{font:14px/1.5 system-ui,sans-serif;margin:14px;overflow-wrap:anywhere}img{max-width:100%;height:auto}</style>' + htm


def attachment(folder, uid, index):
    atts = parts(parse(raw(folder, uid)[0]))[2]
    if not 0 <= index < len(atts):
        raise MailboxError("That attachment is no longer there.")
    p = atts[index]
    return _hdr(p.get_filename()) or f"attachment-{index + 1}", p.get_content_type(), p.get_payload(decode=True) or b""


# ---------- changes ----------
def _set(uids):
    return ",".join(str(int(u)) for u in uids)


def set_flag(folder, uids, flag, on):
    def go(box):
        _select(box, folder, readonly=False)
        _ok(box.uid("STORE", _set(uids), "+FLAGS.SILENT" if on else "-FLAGS.SILENT", f"({flag})"), "Could not update the message")
        for u in uids:
            _raw.pop((folder, int(u)), None)
        _unread["at"] = 0
    if uids:
        _run(go)


def move(folder, uids, target=None):
    """Move messages to another folder, or delete them for good when target is None."""
    def go(box):
        _select(box, folder, readonly=False)
        s = _set(uids)
        if target and "MOVE" in box.caps:
            _ok(box.uid("MOVE", s, _q(target)), "Could not move the message")
        else:
            if target:
                _ok(box.uid("COPY", s, _q(target)), "Could not move the message")
            _ok(box.uid("STORE", s, "+FLAGS.SILENT", "(\\Deleted)"), "Could not delete the message")
            if "UIDPLUS" in box.caps:
                box.uid("EXPUNGE", s)
            else:
                box.expunge()
        for u in uids:
            _raw.pop((folder, int(u)), None)
        _unread["at"] = 0
    if uids:
        _run(go)


def append(folder, msg, flags="\\Seen"):
    """Store a message in a folder. Returns its UID there when the server tells us (or we can find it)."""
    data = msg.as_bytes(policy=policy.SMTP) if isinstance(msg, EmailMessage) else msg

    def go(box):
        d = _ok(box.append(_q(folder), f"({flags})", imaplib.Time2Internaldate(time.time()), data), "Could not save the message")
        m = re.search(rb"APPENDUID \d+ (\d+)", b" ".join(x for x in d if isinstance(x, bytes)))
        if m:
            return int(m[1])
        mid = str(msg.get("Message-ID") or "") if isinstance(msg, EmailMessage) else ""
        if mid:
            _select(box, folder)
            found = (_ok(box.uid("SEARCH", "HEADER", "Message-ID", _q(mid)), "search")[0] or b"").split()
            return int(found[-1]) if found else None
        return None
    return _run(go)


def save_sent(msg):
    """Keep a copy of a sent email in the Sent folder, unless the mail server already put one there. Never raises."""
    if not enabled():
        return
    try:
        folder = folder_for("sent", create=True)
        mid = str(msg.get("Message-ID") or "")

        def exists(box):
            _select(box, folder)
            return bool((_ok(box.uid("SEARCH", "HEADER", "Message-ID", _q(mid)), "search")[0] or b"").split())
        if not (mid and _run(exists)):
            append(folder, msg)
    except Exception as e:
        if time.time() - _warned["at"] > 3600:  # say it once an hour at most
            _warned["at"] = time.time()
            db.log(None, "system", f"Sent, but could not save a copy to the Sent folder: {e}", channel="email", status="error")


FWD_MARK = "---------- Forwarded message ----------"


def forward_parts(folder, uid):
    """What a forward needs from the original: its header lines, its text, its HTML (None if it has none) and the
    images embedded in that HTML as (content id, content type, bytes)."""
    m = message(folder, uid, seen=False)
    _, htm, atts = parts(parse(raw(folder, uid)[0]))
    who = lambda p: formataddr((p["name"], p["email"])) if p["name"] else p["email"]
    try:
        when = datetime.fromisoformat(m["at"]).strftime("%a, %d %b %Y, %H:%M UTC")
    except Exception:
        when = ""
    head = [FWD_MARK, f"From: {who(m['from'])}", f"Date: {when}", f"Subject: {m['subject']}", f"To: {', '.join(who(p) for p in m['to'])}"]
    related = []
    if htm is not None:
        for p in atts:
            cid = str(p.get("Content-ID") or "").strip("<>")
            if cid and f"cid:{cid}" in htm:
                related.append((cid, p.get_content_type(), p.get_payload(decode=True) or b""))
        styles = "".join(re.findall(r"(?is)<style\b.*?</style>", htm))
        body = re.search(r"(?is)<body[^>]*>(.*)</body>", htm)
        htm = styles + (body[1] if body else re.sub(r"(?is)</?(html|head|body|!doctype)[^>]*>", "", htm))
    return head, m["text"], htm, related


def build(sender_name, sender_email, c, attachments=(), forward=None, draft=False):
    """An email from a compose form. c: to, cc, bcc, subject, body, in_reply_to, references.
    attachments: (filename, content_type, bytes). forward: forward_parts() of the email being forwarded, so it
    travels with its original formatting; c["body"] is then only the note written above it."""
    msg = EmailMessage()
    msg["From"] = formataddr((sender_name or "", sender_email))
    for k in ("To", "Cc", "Bcc"):
        addrs = [formataddr((n, a)) for n, a in getaddresses([c.get(k.lower()) or ""]) if "@" in a]
        if addrs:
            msg[k] = ", ".join(addrs)
    msg["Subject"] = (c.get("subject") or "").replace("\n", " ").strip()
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender_email.split("@")[-1] or None)
    for k, key in (("In-Reply-To", "in_reply_to"), ("References", "references")):
        if c.get(key):
            msg[k] = " ".join(re.findall(r"<[^<>\s]+>", c[key]))
    note = c.get("body") or ""
    if forward:
        head, text, htm, related = forward
        msg.set_content((note.rstrip() + "\n\n" if note.strip() else "") + "\n".join(head) + "\n\n" + text)
        if htm is not None:
            esc = htmllib.escape
            note_html = "".join(f'<p style="margin:0 0 12px 0">{esc(p).replace(chr(10), "<br>")}</p>' for p in re.split(r"\n\s*\n", note.strip()) if p)
            msg.add_alternative(f'<div style="font:14px/1.5 Arial,Helvetica,sans-serif;color:#1a1b20">{note_html}</div>'
                                f'<div style="font:13px/1.6 Arial,Helvetica,sans-serif;color:#555;margin:14px 0">{"<br>".join(esc(h) for h in head)}</div>' + htm, subtype="html")
            part = msg.get_body(("html",))
            for cid, ctype, data in related:
                main, _, sub = ctype.partition("/")
                part.add_related(data, maintype=main or "image", subtype=sub or "png", cid=f"<{cid}>")
        if draft and c.get("forward"):  # lets a reopened draft find the original again
            msg["X-Brn-Forward"] = f"{int(c['forward']['uid'])} {base64.b64encode(c['forward']['folder'].encode()).decode()}"
    else:
        msg.set_content(note)
    for name, ctype, data in attachments:
        main, _, sub = (ctype or "application/octet-stream").partition("/")
        msg.add_attachment(data, maintype=main or "application", subtype=sub or "octet-stream", filename=name)
    return msg


def recipients(msg):
    # one header at a time: newer Pythons return nothing at all if any value in the list is empty
    return [a.lower() for k in ("To", "Cc", "Bcc") if msg.get(k) for _, a in getaddresses([str(msg[k])]) if "@" in a]
