"""Guardrails every outbound action passes through before it can leave the system.

Covers: suppression / opt-out list, lawful basis per lead, local calling windows,
daily caps, and the global send mode. Rules are conservative defaults, not legal advice:
have counsel confirm them for KSA (PDPL, CST telemarketing rules), Kuwait (CITRA) and GDPR.
"""
from datetime import datetime, timedelta, timezone

from . import db

# UTC offsets and working days per country. KSA/Kuwait/Qatar/Bahrain work Sun-Thu, UAE Mon-Fri.
# US/CA use a central-time approximation so the window is reasonable coast to coast.
COUNTRY_TZ = {"SA": 3, "KW": 3, "QA": 3, "BH": 3, "AE": 4, "OM": 4, "PK": 5, "GB": 0, "CH": 1,
              "US": -6, "CA": -6, "AU": 10, "DE": 1, "FR": 1, "NL": 1, "IE": 0, "IN": 5}
GULF_SUN_THU = {"SA", "KW", "QA", "BH", "OM"}
SUN_THU = {6, 0, 1, 2, 3}   # Python weekday(): Mon=0 .. Sun=6
MON_FRI = {0, 1, 2, 3, 4}
# Countries where AI-voice cold calls need prior written consent (US TCPA / FCC 2024 ruling, Canada CRTC).
CALL_NEEDS_OPT_IN = {"US", "CA"}
CALL_WINDOW = (10, 17)   # local hours, calls allowed 10:00-16:59
EMAIL_WINDOW = (8, 18)
GDPR_COUNTRIES = {"GB", "CH", "DE", "FR", "IT", "ES", "NL", "IE", "BE", "SE", "AT", "PL", "PT", "DK", "FI"}


def local_now(country, at=None):
    at = at or datetime.now(timezone.utc)
    return at + timedelta(hours=COUNTRY_TZ.get(country or "SA", 3))


def in_window(country, channel, at=None):
    t = local_now(country, at)
    days = SUN_THU if (country or "SA") in GULF_SUN_THU else MON_FRI
    lo, hi = CALL_WINDOW if channel == "phone" else EMAIL_WINDOW
    return t.weekday() in days and lo <= t.hour < hi


def next_window(country, channel, at=None):
    """Next UTC datetime at which this channel is allowed for the lead's country."""
    at = at or datetime.now(timezone.utc)
    probe = at
    for _ in range(24 * 8):
        if in_window(country, channel, probe):
            return probe
        probe = (probe + timedelta(hours=1)).replace(minute=5, second=0, microsecond=0)
    return probe


def is_suppressed(lead):
    vals = [v for v in (lead.get("email"), lead.get("phone")) if v]
    if lead.get("email") and "@" in lead["email"]:
        vals.append(lead["email"].split("@", 1)[1].lower())
    for v in vals:
        if db.one("SELECT 1 AS x FROM suppression WHERE value=?", (v.lower(),)):
            return True
    return False


def sent_today(kind):
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    row = db.one("SELECT COUNT(*) AS n FROM activities WHERE kind=? AND status IN ('ok','dry_run') AND created_at>=?", (kind, start))
    return row["n"]


def check(lead, channel, at=None):
    """Return (allowed: bool, reason: str). Called right before any outbound email or call."""
    if lead["stage"] in ("opted_out", "disqualified", "won", "lost", "meeting_booked", "paused"):
        return False, f"lead is {lead['stage']}"
    if is_suppressed(lead):
        return False, "on suppression list"
    basis = lead.get("consent_basis") or "none"
    if basis == "none":
        return False, "no lawful basis recorded for contacting this lead"
    if lead.get("country") in GDPR_COUNTRIES and basis not in ("opt_in", "existing_customer", "legitimate_interest_b2b"):
        return False, "GDPR: needs opt-in or documented legitimate interest"
    if channel == "email":
        if not lead.get("email"):
            return False, "no email"
        if sent_today("email_sent") >= int(db.setting("daily_email_cap")):
            return False, "daily email cap reached"
    if channel == "phone":
        if not lead.get("phone"):
            return False, "no phone"
        if basis not in ("opt_in", "existing_customer", "legitimate_interest_b2b"):
            return False, "calls need opt-in or a B2B business line"
        if lead.get("country") in CALL_NEEDS_OPT_IN and basis != "opt_in":
            return False, "no phone: AI calls in US/Canada need prior opt-in"
        if sent_today("call_completed") >= int(db.setting("daily_call_cap")):
            return False, "daily call cap reached"
    if not in_window(lead.get("country"), channel, at):
        return False, "outside local business hours"
    return True, "ok"


def suppress(value, reason):
    db.ex("INSERT OR IGNORE INTO suppression(value, reason, created_at) VALUES (?,?,?)", (value.lower(), reason, db.now()))
