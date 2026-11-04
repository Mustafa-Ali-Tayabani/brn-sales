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

from .. import db


def _live():
    return db.setting("mode") == "live"


def unsubscribe_footer(lead):
    return ("\n\n--\nbrn dynamics · Riyadh · Kuwait City · Karachi\n"
            "Reply \"stop\" and we will not contact you again.")


def email_provider():
    if os.environ.get("SMTP_HOST") and os.environ.get("SMTP_USER"):
        return "smtp"
    if os.environ.get("RESEND_API_KEY"):
        return "resend"
    return None


def _send_smtp(lead, subject, body):
    user = os.environ["SMTP_USER"]
    sender = db.setting("sender_email") or user
    msg = EmailMessage()
    msg["From"] = f"{db.setting('sender_name')} <{sender}>"
    msg["To"] = lead["email"]
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1])
    msg["List-Unsubscribe"] = f"<mailto:{sender}?subject=unsubscribe>"
    msg.set_content(body)
    port = int(os.environ.get("SMTP_PORT", "587"))
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(os.environ["SMTP_HOST"], port, context=ctx, timeout=30) as s:
            s.login(user, os.environ["SMTP_PASSWORD"]); s.send_message(msg)
    else:
        with smtplib.SMTP(os.environ["SMTP_HOST"], port, timeout=30) as s:
            s.starttls(context=ctx); s.login(user, os.environ["SMTP_PASSWORD"]); s.send_message(msg)
    return {"status": "ok", "provider": "smtp", "provider_id": msg["Message-ID"], "to": lead["email"], "subject": subject, "body": body}


def send_email(lead, subject, body):
    body = body + unsubscribe_footer(lead)
    provider = email_provider()
    if _live() and provider == "smtp":
        return _send_smtp(lead, subject, body)
    key = os.environ.get("RESEND_API_KEY")
    if not (_live() and key):
        return {"status": "dry_run", "to": lead["email"], "subject": subject, "body": body}
    r = httpx.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "from": f"{db.setting('sender_name')} <{db.setting('sender_email')}>",
            "to": [lead["email"]], "subject": subject, "text": body,
            "headers": {"List-Unsubscribe": f"<mailto:{db.setting('sender_email')}?subject=unsubscribe>"},
        },
        timeout=20,
    )
    r.raise_for_status()
    return {"status": "ok", "provider_id": r.json().get("id"), "to": lead["email"], "subject": subject, "body": body}


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
