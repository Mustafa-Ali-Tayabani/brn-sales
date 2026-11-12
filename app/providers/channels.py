"""Outbound channels. Each one only sends for real when mode == live AND its keys are configured.
Otherwise it returns a dry-run result so the pipeline and logs behave identically.

Email: your own mailbox over SMTP (SMTP_HOST/SMTP_USER/SMTP_PASSWORD, e.g. Google Workspace or Outlook),
or the Resend API (RESEND_API_KEY). SMTP wins when both are set.
Voice: Vapi (VAPI_API_KEY, VAPI_PHONE_NUMBER_ID, VAPI_ASSISTANT_ID). Retell or Twilio+ElevenLabs fit the same interface.
"""
import base64
import os
import random
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, getaddresses, make_msgid

import httpx

from .. import db, email_template
from . import mailbox


def _live():
    return db.setting("mode") == "live"


def email_provider():
    """Resend (HTTPS) wins when its key is set: many hosts, including Railway's cheaper plans, block SMTP ports."""
    if os.environ.get("RESEND_API_KEY"):
        return "resend"
    if os.environ.get("SMTP_HOST") and os.environ.get("SMTP_USER"):
        return "smtp"
    return None


def _sender():
    return db.setting("sender_email") or os.environ.get("SMTP_USER", "")


def _lead_message(lead, subject, body, html=None, headers=None):
    sender = _sender()
    msg = EmailMessage()
    msg["From"] = f"{db.setting('sender_name')} <{sender}>"
    msg["To"] = lead["email"]
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1])
    msg["List-Unsubscribe"] = f"<mailto:{sender}?subject=unsubscribe>"
    for k, v in (headers or {}).items():
        msg[k] = v
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


def _smtp(msg):
    user = os.environ["SMTP_USER"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    ctx = ssl.create_default_context()
    try:
        if port == 465:
            with smtplib.SMTP_SSL(os.environ["SMTP_HOST"], port, context=ctx, timeout=15) as s:
                s.login(user, os.environ["SMTP_PASSWORD"]); s.send_message(msg)
        else:
            with smtplib.SMTP(os.environ["SMTP_HOST"], port, timeout=15) as s:
                s.starttls(context=ctx); s.login(user, os.environ["SMTP_PASSWORD"]); s.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise RuntimeError("Mailbox rejected the login: check SMTP_USER and SMTP_PASSWORD")
    except smtplib.SMTPRecipientsRefused as e:
        raise RuntimeError(f"The mail server refused the address: {', '.join(e.recipients)}")
    except (TimeoutError, OSError) as e:
        if isinstance(e, smtplib.SMTPException):
            raise
        raise RuntimeError(f"Could not reach {os.environ['SMTP_HOST']}:{port} ({e}). Your host is probably blocking "
                           "email ports (Railway does on Free/Trial/Hobby plans). Use Resend: set RESEND_API_KEY.")
    return str(msg["Message-ID"])


def _resend(msg):
    sender = _sender()
    addrs = lambda k: [formataddr(x) for x in getaddresses([str(msg.get(k) or "")]) if x[1]]
    text, html = msg.get_body(("plain",)), msg.get_body(("html",))
    payload = {"from": str(msg["From"]), "reply_to": sender, "to": addrs("To"), "subject": str(msg["Subject"] or ""),
               "text": text.get_content() if text else "",
               "headers": {k: str(msg[k]) for k in ("List-Unsubscribe", "In-Reply-To", "References") if msg.get(k)}}
    for k in ("Cc", "Bcc"):
        if addrs(k):
            payload[k.lower()] = addrs(k)
    if html:
        payload["html"] = html.get_content()
    files = [{"filename": a.get_filename() or "attachment", "content": base64.b64encode(a.get_payload(decode=True) or b"").decode()}
             for a in msg.iter_attachments()]
    if files:
        payload["attachments"] = files
    r = httpx.post("https://api.resend.com/emails", headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}"}, json=payload, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"Resend refused the email: {r.text[:300]}")
    return r.json().get("id")


def send_message(msg):
    """Send a finished email through whichever provider is configured, and keep a copy in the mailbox's Sent folder."""
    provider = email_provider()
    if not provider:
        raise RuntimeError("No email provider configured")
    pid = _resend(msg) if provider == "resend" else _smtp(msg)
    mailbox.save_sent(msg)
    return {"provider": provider, "provider_id": pid}


def deliver(lead, subject, body, html=None, headers=None):
    """Send to a lead for real (no mode check: callers decide)."""
    res = send_message(_lead_message(lead, subject, body, html, headers))
    return {"status": "ok", **res, "to": lead["email"], "subject": subject, "body": body}


def build_email(lead, subject, body, cta=None, cta_label=None):
    prod = db.products().get(lead.get("product") or "")
    html = email_template.render(subject, body, cta, cta_label, prod["name"] if prod else None)
    text = email_template.text_version(body, cta, cta_label)
    return text, html


def send_email(lead, subject, body, cta=None, cta_label=None):
    text, html = build_email(lead, subject, body, cta, cta_label)
    if not (_live() and email_provider()):
        return {"status": "dry_run", "to": lead["email"], "subject": subject, "body": text, "cta": cta_label}
    res = deliver(lead, subject, text, html)
    res["body"], res["cta"] = text, cta_label
    return res


def place_call(lead, script):
    key = os.environ.get("VAPI_API_KEY")
    if not (_live() and key):
        # Simulated outcome so the dashboard and follow-up logic can be exercised.
        outcome = random.Random(lead["id"]).choice(["no_answer", "voicemail", "interested", "not_now", "meeting"])
        return {"status": "dry_run", "outcome": outcome, "duration_s": 0 if outcome == "no_answer" else 74,
                "transcript": f"[simulated] {script.get('opening', '')} ... outcome: {outcome}"}
    r = httpx.post(
        "https://api.vapi.ai/call",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "phoneNumberId": os.environ["VAPI_PHONE_NUMBER_ID"],
            "assistantId": os.environ["VAPI_ASSISTANT_ID"],
            "customer": {"number": lead["phone"], "name": lead.get("contact_name") or lead["company"]},
            "assistantOverrides": {"variableValues": {"company": lead["company"], "opening": script.get("opening", ""),
                                                      "talking_points": "; ".join(script.get("talking_points", []))}},
        },
        timeout=20,
    )
    r.raise_for_status()
    # Final outcome arrives later through the /webhooks/voice endpoint.
    return {"status": "ok", "provider_id": r.json().get("id"), "outcome": "in_progress"}
