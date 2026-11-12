"""Contact finder: a person's name and LinkedIn profile in, their work email and phone number out.

Uses Apollo's People Enrichment API (APOLLO_API_KEY). LinkedIn itself is never opened or scraped: it does not
publish contact details, and automated access breaks its terms. Apollo matches the profile against its own database.
The email comes back straight away. Mobile numbers take Apollo a few minutes, so they are collected afterwards
with phone_result(). Only work emails are requested, not personal ones.

Costs Apollo credits: about 1 per person found, plus about 8 more when a mobile number is returned."""
import os
import re

import httpx

API = "https://api.apollo.io/api/v1"
COUNTRIES = {"saudi arabia": "SA", "kuwait": "KW", "united arab emirates": "AE", "qatar": "QA", "bahrain": "BH", "oman": "OM",
             "pakistan": "PK", "united states": "US", "united kingdom": "GB", "canada": "CA", "australia": "AU", "germany": "DE",
             "france": "FR", "switzerland": "CH", "netherlands": "NL", "ireland": "IE", "india": "IN", "egypt": "EG", "jordan": "JO",
             "spain": "ES", "italy": "IT", "turkey": "TR", "singapore": "SG"}


def enabled():
    return bool(os.environ.get("APOLLO_API_KEY"))


def linkedin_url(text):
    """The profile link in whatever form it was pasted, normalised. None if there isn't one."""
    m = re.search(r"linkedin\.com/(in|pub)/([^\s/?#,;]+)", text or "", re.I)
    return f"https://www.linkedin.com/in/{m[2]}" if m else None


def _headers():
    return {"x-api-key": os.environ["APOLLO_API_KEY"], "accept": "application/json", "Cache-Control": "no-cache"}


def _fail(r):
    try:
        detail = r.json()
        detail = detail.get("error") or detail.get("message") or detail.get("error_code") or str(detail)
    except Exception:
        detail = r.text
    detail = str(detail)[:200]
    if r.status_code in (401, 403):
        return RuntimeError(f"Apollo refused the request: check APOLLO_API_KEY, and that your Apollo plan includes API access. ({detail})")
    if r.status_code == 429:
        return RuntimeError("Apollo's hourly limit is reached. Try again in a while.")
    if r.status_code == 402 or "credit" in detail.lower():
        return RuntimeError(f"Apollo is out of credits. ({detail})")
    return RuntimeError(f"Apollo error {r.status_code}: {detail}")


def _phones(obj):
    """Every phone-number entry anywhere in an Apollo response, mobiles first."""
    found = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "phone_numbers" and isinstance(v, list):
                    found.extend(x for x in v if isinstance(x, dict))
                else:
                    walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    walk(obj)
    found.sort(key=lambda x: (x.get("type_cd") or x.get("type") or "") != "mobile")
    out = []
    for x in found:
        n = x.get("sanitized_number") or x.get("raw_number")
        if n and n not in out and (x.get("status_cd") or x.get("status") or "") not in ("invalid_number", "invalid"):
            out.append(n)
    return out


def find_person(name=None, linkedin=None, company=None, domain=None, want_phone=True):
    """Look one person up. Returns None when Apollo doesn't know them, else a dict with whatever was found:
    contact_name, title, email, company, website, city, country, linkedin, phone, phone_request (when a mobile
    number is still being looked up), confidence."""
    if not enabled():
        raise RuntimeError("The contact finder is not connected. Set APOLLO_API_KEY.")
    params = {k: v for k, v in {"name": name, "linkedin_url": linkedin, "organization_name": company, "domain": domain}.items() if v}
    if not (linkedin or (name and (company or domain))):
        raise RuntimeError("Give a LinkedIn profile link, or a name together with the company.")

    def call(phone):
        extra = {"reveal_phone_number": "true", "poll_only": "true"} if phone else {}
        return httpx.post(f"{API}/people/match", params={**params, **extra}, headers=_headers(), timeout=40)
    note = None
    r = call(want_phone)
    if want_phone and r.status_code in (400, 402, 403, 422):  # plan without phone numbers: still get the email
        r = call(False)
        if r.status_code < 400:
            note = "Apollo would not look up phone numbers (usually a plan or credit limit), so only emails were fetched."
    if r.status_code >= 400:
        raise _fail(r)
    data = r.json() or {}
    p = data.get("person")
    if not p or p.get("match_confidence") == "none":
        return None
    org = p.get("organization") or {}
    email = p.get("email") or ""
    if "not_unlocked" in email or p.get("email_status") in ("unavailable", "bounced"):
        email = ""
    phones = _phones(p)
    if not phones and org.get("phone"):
        phones = [org.get("sanitized_phone") or org["phone"]]  # the company switchboard, until a direct number arrives
    pending = (data.get("phone_enrichment") or {}).get("status") == "pending"
    return {"contact_name": p.get("name") or name, "title": p.get("title") or p.get("headline"), "email": email.lower() or None,
            "email_status": p.get("email_status"), "company": org.get("name") or company, "website": org.get("website_url"),
            "industry": org.get("industry"), "city": p.get("city"), "country": COUNTRIES.get((p.get("country") or "").lower()),
            "linkedin": p.get("linkedin_url") or linkedin, "phone": phones[0] if phones else None, "company_line": bool(phones) and not _phones(p),
            "phone_request": str(data.get("request_id_str") or data.get("request_id") or "") if pending else None,
            "confidence": p.get("match_confidence"), "note": note}


def phone_result(request_id):
    """A mobile-number lookup started by find_person. Returns ("pending", seconds to wait), ("done", [numbers]) or ("gone", None)."""
    r = httpx.get(f"{API}/webhook_result/{request_id}", headers=_headers(), timeout=30)
    if r.status_code == 200:
        return "done", _phones(r.json())
    try:
        body = r.json()
    except Exception:
        body = {}
    if r.status_code == 404 and "pending" in str(body.get("error_code") or body):
        return "pending", int(body.get("retry_after_seconds") or 20)
    if r.status_code == 429:
        return "pending", 60
    return "gone", None
