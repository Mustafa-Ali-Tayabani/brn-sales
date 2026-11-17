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


# ---------- the right person at a company, and whether a mailbox exists ----------
# Two services can do this. Apollo (APOLLO_API_KEY) searches its people database for free and spends a credit to
# reveal the chosen person's email. Hunter (HUNTER_API_KEY) lists the people it knows at a domain with their
# emails in one call, and can also check whether a single mailbox exists. Apollo is used when both are set.
SENIOR = ("owner", "founder", "co-founder", "cofounder", "ceo", "chief executive", "managing director", "president", "principal", "general manager",
          "partner", "chief", "director", "head of", "head", "vp", "vice president", "manager", "lead")
NOT_THEM = ("assistant", "intern", "student", "trainee", "receptionist", "recruit", "hr ", "human resources", "talent", "junior")


def people_finder():
    return "apollo" if os.environ.get("APOLLO_API_KEY") else "hunter" if os.environ.get("HUNTER_API_KEY") else None


def verifier():
    return "hunter" if os.environ.get("HUNTER_API_KEY") else None


def _rank(title, wanted):
    """How good a contact this job title makes: the pipeline's own titles first, then seniority. Lower is better; None = not them."""
    t = f" {(title or '').lower()} "
    if not t.strip() or any(w in t for w in NOT_THEM):
        return None
    for i, w in enumerate(wanted):
        if w and w in t:
            return i
    for i, w in enumerate(SENIOR):
        if w in t:
            return 100 + i
    return None


def _apollo_person(domain, wanted):
    r = httpx.post(f"{API}/mixed_people/api_search", headers=_headers(), timeout=40, params=[
        ("q_organization_domains_list[]", domain), ("per_page", "25"), ("page", "1"),
        *[("person_seniorities[]", s) for s in ("owner", "founder", "c_suite", "partner", "vp", "head", "director", "manager")]])
    if r.status_code >= 400:
        raise _fail(r)
    people = [(p, _rank(p.get("title"), wanted)) for p in (r.json() or {}).get("people") or [] if p.get("has_email")]
    people = sorted([x for x in people if x[1] is not None], key=lambda x: x[1])
    for p, _ in people[:2]:   # revealing an email costs a credit: try the best match, then one more
        m = httpx.post(f"{API}/people/match", params={"id": p["id"]}, headers=_headers(), timeout=40)
        if m.status_code >= 400:
            raise _fail(m)
        person = (m.json() or {}).get("person") or {}
        email = (person.get("email") or "").lower()
        if email and "not_unlocked" not in email and person.get("email_status") not in ("unavailable", "bounced"):
            return {"contact_name": person.get("name") or p.get("first_name"), "title": person.get("title") or p.get("title"), "email": email,
                    "email_status": person.get("email_status"), "linkedin": person.get("linkedin_url"), "via": "Apollo"}
    return None


def _hunter_person(domain, wanted):
    r = httpx.get("https://api.hunter.io/v2/domain-search", params={"domain": domain, "type": "personal", "limit": 10, "api_key": os.environ["HUNTER_API_KEY"]}, timeout=30)
    if r.status_code in (401, 403):
        raise RuntimeError("Hunter refused the request: check HUNTER_API_KEY.")
    if r.status_code == 429:
        raise RuntimeError("Hunter's limit for this month or minute is reached.")
    if r.status_code >= 400:
        raise RuntimeError(f"Hunter error {r.status_code}: {r.text[:200]}")
    best = None
    for e in ((r.json() or {}).get("data") or {}).get("emails") or []:
        rank = _rank(e.get("position") or ("executive" if e.get("seniority") == "executive" else ""), wanted + ["executive"])
        if rank is None or not e.get("value") or not (e.get("first_name") or e.get("last_name")) or (e.get("verification") or {}).get("status") == "invalid":
            continue
        key = (rank, -int(e.get("confidence") or 0))
        if best is None or key < best[0]:
            best = (key, e)
    if not best:
        return None
    e = best[1]
    return {"contact_name": " ".join(x for x in (e.get("first_name"), e.get("last_name")) if x), "title": e.get("position"), "email": e["value"].lower(),
            "email_status": (e.get("verification") or {}).get("status"), "linkedin": e.get("linkedin"), "via": "Hunter"}


def find_decision_maker(domain, titles=()):
    """A named person worth writing to at this company: {contact_name, title, email, email_status, via}, or None."""
    wanted = [t.strip().lower() for t in titles if t and t.strip()]
    who = people_finder()
    if not who:
        raise RuntimeError("No people finder is connected. Set APOLLO_API_KEY or HUNTER_API_KEY.")
    domain = re.sub(r"^(https?://)?(www\.)?", "", (domain or "").strip().lower()).split("/")[0]
    if not domain or "." not in domain:
        return None
    return _apollo_person(domain, wanted) if who == "apollo" else _hunter_person(domain, wanted)


def verify_email(address):
    """Does this mailbox exist? "valid", "invalid", "accept_all" (the server accepts anything, so unknowable), "webmail",
    "disposable" or "unknown"; None when no verifier is connected or it could not be asked."""
    if not verifier():
        return None
    try:
        for _ in range(2):   # 202 means Hunter is still checking: ask once more
            r = httpx.get("https://api.hunter.io/v2/email-verifier", params={"email": address, "api_key": os.environ["HUNTER_API_KEY"]}, timeout=30)
            if r.status_code != 202:
                break
        if r.status_code != 200:
            return None
        return ((r.json() or {}).get("data") or {}).get("status")
    except Exception:
        return None
