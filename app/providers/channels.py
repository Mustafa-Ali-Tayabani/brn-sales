"""Outbound channels. Each one only sends for real when mode == live AND its keys are configured.
Otherwise it returns a dry-run result so the pipeline and logs behave identically.

Email: your own mailbox over SMTP (SMTP_HOST/SMTP_USER/SMTP_PASSWORD, e.g. Google Workspace or Outlook),
or the Resend API (RESEND_API_KEY). SMTP wins when both are set.
Voice: Vapi (VAPI_API_KEY, VAPI_PHONE_NUMBER_ID, VAPI_ASSISTANT_ID). Retell or Twilio+ElevenLabs fit the same interface.
"""
import os
import random
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid

import httpx

from .. import db, email_template


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


def _send_smtp(lead, subject, body, html=None, headers=None):
    user = os.environ["SMTP_USER"]
    sender = _sender()
    msg = EmailMessage()
    msg["From"] = f"{db.setting('sender_name')} <{sender}>"
    msg["To"] = lead["email"]
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1])
    msg["List-Unsubscribe"] = f"<mailto:{sender}?subject=unsubscribe>"
    for k, v in (headers or {}).items():
        msg[k] = v
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
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
    except (TimeoutError, OSError) as e:
        if isinstance(e, smtplib.SMTPException):
            raise
        raise RuntimeError(f"Could not reach {os.environ['SMTP_HOST']}:{port} ({e}). Your host is probably blocking "
                           "email ports (Railway does on Free/Trial/Hobby plans). Use Resend: set RESEND_API_KEY.")
    return {"status": "ok", "provider": "smtp", "provider_id": msg["Message-ID"], "to": lead["email"], "subject": subject, "body": body}


def _send_resend(lead, subject, body, html=None, headers=None):
    sender = _sender()
    r = httpx.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}"},
        json={
            "from": f"{db.setting('sender_name')} <{sender}>", "reply_to": sender,
            "to": [lead["email"]], "subject": subject, "text": body, **({"html": html} if html else {}),
            "headers": {"List-Unsubscribe": f"<mailto:{sender}?subject=unsubscribe>", **(headers or {})},
        },
        timeout=20,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Resend refused the email: {r.text[:300]}")
    return {"status": "ok", "provider": "resend", "provider_id": r.json().get("id"), "to": lead["email"], "subject": subject, "body": body}


def deliver(lead, subject, body, html=None, headers=None):
    """Send for real through whichever provider is configured (no mode check: callers decide)."""
    provider = email_provider()
    if provider == "resend":
        return _send_resend(lead, subject, body, html, headers)
    if provider == "smtp":
        return _send_smtp(lead, subject, body, html, headers)
    raise RuntimeError("No email provider configured")


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
