"""Leads with a reason to be contacted now, instead of businesses that merely fit a profile.

Two kinds of signal are collected here:
  asked    someone said, in public and in their own words, that they need what we sell (a forum post, a request
           for proposals, a job-board gig). Found with Claude's web search, then each one is checked: the page is
           fetched and the quoted words must really be on it, so nothing invented gets through.
  hiring   a company is advertising a job for the skill we sell. They need the work done now; a service or a
           product can be the faster answer. Read from free public job boards (no key needed).
A third kind, "problem" (a website scan found faults, or a business has no website), comes from the existing
scan and map search. Every lead from here carries its evidence in extra.intent: the quote, the link and the date."""
import html
import re
from datetime import datetime, timedelta, timezone

import httpx

UA = {"User-Agent": "Mozilla/5.0 (compatible; brn-lead-research/1.0)"}
NOT_BUYERS = ("recruit", "staffing", "talent", "headhunt", "hiring agency", "jobs", "careers")


def _words(text):
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def _match(keyword, *texts):
    """Every word of the keyword appears in the texts (so "wordpress developer" matches "Senior WordPress Developer")."""
    hay = set(_words(" ".join(t if isinstance(t, str) else " ".join(map(str, t or [])) for t in texts)))
    return all(w in hay for w in _words(keyword))


def _row(company, title, url, posted, where, keyword, location=""):
    return {"company": company.strip()[:120], "industry": f"Hiring: {title.strip()[:90]}", "source": "hiring", "consent_basis": "legitimate_interest_b2b",
            "city": (location or "")[:60] or None, "notes": f"Advertising a job for: {title.strip()} ({where}{', ' + posted[:10] if posted else ''}).",
            "extra": {"intent": {"level": "hiring", "quote": title.strip()[:200], "url": url, "posted": (posted or "")[:10], "where": where}, "found_by": keyword}}


def hiring(keywords, limit=15, days=30):
    """Companies advertising a job that matches any of the keywords in the last `days`. One row per company."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    out, seen, errors = [], set(), []

    def add(company, title, url, posted, where, kw, location=""):
        key = (company or "").strip().lower()
        if not key or key in seen or any(w in key for w in NOT_BUYERS):
            return
        seen.add(key); out.append(_row(company, title, url, posted, where, kw, location))

    def recent(text):
        try:
            d = datetime.fromtimestamp(int(text), timezone.utc) if str(text).isdigit() else datetime.fromisoformat(str(text).replace("Z", "+00:00"))
            return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)) >= since
        except (ValueError, TypeError):
            return True

    for kw in keywords:
        try:
            for j in httpx.get("https://remotive.com/api/remote-jobs", params={"search": kw, "limit": 40}, headers=UA, timeout=25).json().get("jobs", []):
                if _match(kw, j.get("title"), j.get("tags")) and recent(j.get("publication_date")):
                    add(j.get("company_name"), j.get("title") or "", j.get("url"), j.get("publication_date"), "Remotive", kw, j.get("candidate_required_location"))
        except Exception as e:
            errors.append(f"Remotive: {e}")
    boards = (("RemoteOK", "https://remoteok.com/api", lambda d: [x for x in d if isinstance(x, dict) and x.get("company")], "company", "position", "url", "date", "location"),
              ("Arbeitnow", "https://www.arbeitnow.com/api/job-board-api", lambda d: d.get("data", []), "company_name", "title", "url", "created_at", "location"))
    for where, url, rows, c, t, u, dt, loc in boards:
        try:
            for j in rows(httpx.get(url, headers=UA, timeout=25).json()):
                kw = next((k for k in keywords if _match(k, j.get(t), j.get("tags"))), None)
                if kw and recent(j.get(dt)):
                    add(j.get(c), j.get(t) or "", j.get(u), str(j.get(dt) or ""), where, kw, j.get(loc))
        except Exception as e:
            errors.append(f"{where}: {e}")
    return out[:limit], errors


def page_text(url):
    """The readable text of a public page, or None when it cannot be fetched."""
    try:
        r = httpx.get(url, headers=UA, timeout=12, follow_redirects=True)
        if r.status_code >= 400:
            return None
        t = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", r.text[:600000])
        return html.unescape(re.sub(r"(?s)<[^>]+>", " ", t))
    except Exception:
        return None


def verify(quote, url):
    """Is the quoted request really on that page? True, False (page loaded, words not there) or None (page not readable)."""
    text = page_text(url)
    if text is None:
        return None
    want = [w for w in _words(quote) if len(w) > 3]
    if len(want) < 3:
        return None
    hay = set(_words(text))
    return sum(w in hay for w in want) / len(want) >= 0.7
