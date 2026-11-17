"""Apollo as a source of leads (APOLLO_API_KEY): companies that match a pipeline's filters, the right person at each,
their open job ads, and the person's verified email.

What each call costs (Apollo's API reference, checked 2026-11):
  company search   POST mixed_companies/search          1 credit per page of up to 100. Paid plans only.
  people search    POST mixed_people/api_search         free. Needs a master API key. Gives first name and job title
                                                         only: no surname, no email, no LinkedIn link.
  job postings     GET  organizations/{id}/job_postings 1 credit per company.
  reveal a person  POST people/match                    1 credit. Full name, email and its status, LinkedIn, company facts.
Every credit is counted here (per day, by kind and by pipeline) and nothing is spent past the daily cap in Settings.
Rate limits come back as HTTP 429 with a retry-after header: a short wait is slept through, a long one is reported."""
import json
import threading
import time

import httpx

from .. import db
from . import enrich

LOCK = threading.Lock()
KINDS = {"search": "company searches", "jobs": "job ad look-ups", "reveal": "emails revealed"}
MAX_WAIT = 20   # seconds of rate-limit waiting worth sleeping through; anything longer is left for the next run


class NoPlan(RuntimeError):
    """The Apollo plan does not include this call (company search needs a paid plan)."""


class CapReached(RuntimeError):
    """Today's Apollo credit cap (Settings > Agent & sending) is used up."""


# ---------- credits ----------
def _usage():
    try:
        return json.loads(db.setting("apollo_usage") or "{}") or {}
    except ValueError:
        return {}


def spend(kind, credits=1, pipeline=None):
    with LOCK:
        u, day = _usage(), db.now()[:10]
        d = u.setdefault(day, {})
        d[kind] = d.get(kind, 0) + credits
        if pipeline and kind == "reveal":
            d.setdefault("by", {})[pipeline] = d["by"].get(pipeline, 0) + credits
        for old in sorted(u)[:-70]:   # ten weeks of history is plenty
            u.pop(old)
        db.set_setting("apollo_usage", json.dumps(u))


def used(day=None, pipeline=None):
    """Credits used on a day (today by default): all of them, or one pipeline's reveals."""
    d = _usage().get(day or db.now()[:10], {})
    return (d.get("by") or {}).get(pipeline, 0) if pipeline else sum(v for k, v in d.items() if k != "by")


def cap():
    try:
        return max(0, int(db.setting("apollo_daily_cap") or 0))
    except ValueError:
        return 0


def room(credits=1):
    """May this many more credits be spent today? (A cap of 0 means no limit.)"""
    return not cap() or used() + credits <= cap()


def usage():
    """For Settings > AI & cost: today, this month, by kind, and the cap."""
    u, today = _usage(), db.now()[:10]
    month = {k: 0 for k in KINDS}
    for day, d in u.items():
        if day[:7] == today[:7]:
            for k in KINDS:
                month[k] += d.get(k, 0)
    t = u.get(today, {})
    return {"connected": enrich.enabled(), "today": used(), "today_by_kind": {k: t.get(k, 0) for k in KINDS}, "month": sum(month.values()), "month_by_kind": month,
            "cap": cap(), "labels": KINDS, "days": [{"day": d, **{k: u[d].get(k, 0) for k in KINDS}} for d in sorted(u, reverse=True)[:14]]}


# ---------- the HTTP call ----------
def _call(method, path, params=None, credits=0, kind="search", pipeline=None):
    if not enrich.enabled():
        raise RuntimeError("Apollo is not connected. Set APOLLO_API_KEY on the server.")
    if credits and not room(credits):
        raise CapReached(f"Today's Apollo credit cap of {cap()} is used up. It resets tomorrow, or raise it under Settings > Agent & sending.")
    flat = []
    for k, v in (params or {}).items():
        flat += [(k, str(x)) for x in v] if isinstance(v, (list, tuple)) else [(k, str(v))] if v not in (None, "") else []
    r = None
    for attempt in range(3):
        try:
            r = httpx.request(method, f"{enrich.API}{path}", params=flat, headers=enrich._headers(), timeout=40)
        except httpx.HTTPError as e:
            raise RuntimeError(f"Could not reach Apollo: {e}")
        if r.status_code != 429:
            break
        try:
            body = r.json() or {}
        except Exception:
            body = {}
        wait = r.headers.get("retry-after") or next((s.get("retry_after_seconds") for s in (body.get("error_details") or {}).get("suggestions") or [] if isinstance(s, dict)), None) or 60
        try:
            wait = int(float(wait))
        except (TypeError, ValueError):
            wait = 60
        if wait > MAX_WAIT or attempt == 2:
            raise RuntimeError(f"Apollo's rate limit is reached. Try again in about {max(1, round(wait / 60))} minute(s).")
        time.sleep(wait + attempt)   # back off a little more each time
    if r.status_code == 403 or (r.status_code == 422 and "plan" in r.text.lower()):
        raise NoPlan(str(enrich._fail(r)))
    if r.status_code >= 400:
        raise enrich._fail(r)
    if credits:
        spend(kind, credits, pipeline)
    return r.json() or {}


# ---------- what a pipeline searches for ----------
TECH = {"wordpress": "wordpress_org", "woocommerce": "woocommerce", "shopify": "shopify", "odoo": "odoo", "hubspot": "hubspot", "salesforce": "salesforce", "magento": "magento"}


def _filters(cfg, people=False):
    """A pipeline's Apollo settings as Apollo's own search parameters (the same names work for companies and for people)."""
    from .. import countries
    p = {"organization_industries[]": [i.strip().lower() for i in cfg.get("industries") or [] if i.strip()],
         "organization_num_employees_ranges[]": [r.replace("-", ",").replace(" ", "") for r in cfg.get("employee_ranges") or [] if r],
         "organization_locations[]": [countries.name(c) for c in cfg.get("countries") or [] if c],
         "q_organization_keyword_tags[]": [k.strip() for k in cfg.get("keywords") or [] if k.strip()],
         "currently_using_any_of_technology_uids[]": [TECH.get(t.strip().lower(), t.strip().lower().replace(" ", "_").replace(".", "_")) for t in cfg.get("technologies") or [] if t.strip()]}
    return {k: v for k, v in p.items() if v}


def has_filters(cfg):
    return bool(_filters(cfg or {}))


def _org(o):
    site = o.get("website_url") or (f"https://{o['primary_domain']}" if o.get("primary_domain") else None)
    phone = o.get("sanitized_phone") or (o.get("primary_phone") or {}).get("sanitized_number") or o.get("phone")
    return {"id": o.get("id"), "name": o.get("name"), "domain": (o.get("primary_domain") or enrich_domain(site) or "").lower() or None, "website": site,
            "linkedin": o.get("linkedin_url"), "phone": phone, "industry": o.get("industry"), "employees": o.get("estimated_num_employees"),
            "country": o.get("country"), "city": o.get("city"), "founded": o.get("founded_year"), "funding": o.get("latest_funding_stage") or o.get("total_funding_printed"),
            "technologies": [t for t in (o.get("technology_names") or [])][:30]}


def enrich_domain(url):
    import re
    host = re.sub(r"^(https?://)?(www\.)?", "", str(url or "").strip().lower()).split("/")[0]
    return host if "." in host else None


def companies(cfg, limit=25, hiring=False, pipeline=None):
    """Companies matching a pipeline's filters (1 credit). hiring=True narrows to those with an open job ad matching its hiring words."""
    p = _filters(cfg)
    if hiring:
        p["q_organization_job_titles[]"] = [k.strip() for k in cfg.get("hiring_keywords") or [] if k.strip()]
    data = _call("POST", "/mixed_companies/search", {**p, "page": 1, "per_page": max(1, min(int(limit), 100))}, credits=1, kind="search", pipeline=pipeline)
    return [_org(o) for o in (data.get("organizations") or []) + (data.get("accounts") or []) if o.get("name")]


def _person(p, wanted):
    org = p.get("organization") or {}
    return {"id": p.get("id"), "first_name": p.get("first_name"), "title": p.get("title"), "has_email": bool(p.get("has_email")), "rank": enrich._rank(p.get("title"), wanted),
            "company": org.get("name"), "org_id": p.get("organization_id") or org.get("id")}


def people_at(org, titles, limit=10):
    """The people Apollo knows at one company, best job title first (free). titles: the pipeline's, most wanted first."""
    wanted = [t.strip().lower() for t in titles if t and t.strip()]
    p = {"organization_ids[]": [org["id"]]} if org.get("id") else {"q_organization_domains_list[]": [org["domain"]]}
    data = _call("POST", "/mixed_people/api_search", {**p, "person_titles[]": wanted[:8], "include_similar_titles": "true", "page": 1, "per_page": limit})
    found = [_person(x, wanted) for x in data.get("people") or []]
    return sorted([f for f in found if f["rank"] is not None and f["first_name"]], key=lambda f: (f["rank"], not f["has_email"]))


def people(cfg, titles, limit=25, hiring=False):
    """Without company search (a free Apollo plan): people with the wanted job titles at companies matching the filters
    (free). Gives the company's name but not its web address, which arrives when the person's email is revealed."""
    wanted = [t.strip().lower() for t in titles if t and t.strip()]
    p = _filters(cfg, people=True)
    if hiring:
        p["q_organization_job_titles[]"] = [k.strip() for k in cfg.get("hiring_keywords") or [] if k.strip()]
    data = _call("POST", "/mixed_people/api_search", {**p, "person_titles[]": wanted[:8], "include_similar_titles": "true", "page": 1, "per_page": max(1, min(int(limit), 100))})
    found = [_person(x, wanted) for x in data.get("people") or []]
    return [f for f in found if f["rank"] is not None and f["first_name"] and f["company"]]


def job_ads(org_id, words, pipeline=None):
    """The company's open job ads whose title contains one of the words (1 credit): [{title, url, posted}]."""
    data = _call("GET", f"/organizations/{org_id}/job_postings", {"page": 1, "per_page": 100}, credits=1, kind="jobs", pipeline=pipeline)
    words = [w.strip().lower() for w in words if w and w.strip()]
    out = []
    for j in data.get("organization_job_postings") or data.get("job_postings") or []:
        title = str(j.get("title") or "")
        if any(w in title.lower() for w in words):
            out.append({"title": title[:140], "url": j.get("url"), "posted": str(j.get("posted_at") or "")[:10] or None})
    return out


OK_EMAIL = ("verified",)   # Apollo's other statuses (guessed, unavailable, bounced, pending_manual_fulfillment, ...) are not used


def reveal(person_id, pipeline=None):
    """One person's full name, email and company facts (1 credit). The email is only returned when Apollo marks it verified."""
    data = _call("POST", "/people/match", {"id": person_id, "reveal_personal_emails": "false"}, credits=1, kind="reveal", pipeline=pipeline)
    p = data.get("person") or {}
    if not p:
        return None
    org = p.get("organization") or {}
    email, status = (p.get("email") or "").lower(), (p.get("email_status") or "").lower() or None
    if "not_unlocked" in email or status not in OK_EMAIL:
        email = ""
    return {"contact_name": p.get("name") or " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x), "title": p.get("title") or p.get("headline"),
            "email": email or None, "email_status": status or "unavailable", "linkedin": p.get("linkedin_url"), "city": p.get("city") or org.get("city"),
            "country": enrich.COUNTRIES.get((p.get("country") or org.get("country") or "").lower()) or p.get("country") or org.get("country"), "org": _org(org) if org else {}}
