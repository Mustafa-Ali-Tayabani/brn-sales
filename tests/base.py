"""Shared set-up: the app on a throwaway database, a mail server that records instead of sending, and a stand-in Apollo."""
import json
import types
import unittest

import httpx
from fastapi.testclient import TestClient

import app.main as main
from app import agent, compliance, db, prospect, rules
from app.providers import apollo, channels, llm

SENT = []
compliance.in_window = lambda *a, **k: True
channels._smtp = lambda msg: (SENT.append(str(msg["To"])), str(msg["Message-ID"]))[1]
client = TestClient(main.app)


class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body if body is not None else {}, headers or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeApollo:
    """Answers Apollo's four endpoints from data set on it, and records every call."""

    def __init__(self):
        self.orgs, self.hiring_ids, self.people, self.ads, self.persons = [], set(), {}, {}, {}
        self.calls, self.no_company_search, self.rate_limited = [], False, []
        self.no_people_search, self.reject_tech, self.odd = False, False, False

    def request(self, method, url, params=None, headers=None, timeout=None):
        q = {}
        for k, v in params or []:
            q.setdefault(k, []).append(v)
        path = url.split("/api/v1")[-1]
        self.calls.append((path, q))
        if self.rate_limited:
            return Resp(429, {"error_details": {"code": "USAGE.RATE_LIMIT.API_RATE_LIMIT_EXCEEDED"}}, {"retry-after": str(self.rate_limited.pop(0))})
        assert headers["x-api-key"] == "test-key"
        if path == "/mixed_companies/search":
            if self.no_company_search:
                return Resp(403, {"error": "This endpoint is only available to Apollo users on paid plans."})
            orgs = [o for o in self.orgs if o["id"] in self.hiring_ids] if "q_organization_job_titles[]" in q else self.orgs
            if self.reject_tech and "currently_using_any_of_technology_uids[]" in q:
                return Resp(422, {"error": "invalid technology uid"})
            if self.odd:   # a shape the documentation does not show
                return Resp(200, {"organizations": [{"id": "o9", "name": "Odd Co", "primary_domain": "odd.sa", "primary_phone": "+966 11 000", "technology_names": [{"name": "WordPress"}, None]}, "junk", {"id": "o8"}], "accounts": None})
            return Resp(200, {"organizations": orgs})
        if path == "/mixed_people/api_search":
            if self.no_people_search:
                return Resp(403, {"error": "api/v1/mixed_people/api_search is not accessible with this api_key"})
            if self.reject_tech and "currently_using_any_of_technology_uids[]" in q:
                return Resp(422, {"error": "invalid technology uid"})
            if "organization_ids[]" in q:
                return Resp(200, {"people": self.people.get(q["organization_ids[]"][0], [])})
            everyone = [p for ps in self.people.values() for p in ps]
            if "q_organization_job_titles[]" in q:
                everyone = [p for p in everyone if p.get("_hiring")]
            return Resp(200, {"people": everyone})
        if path.endswith("/job_postings"):
            return Resp(200, {"organization_job_postings": self.ads.get(path.split("/")[2], [])})
        if path == "/people/match":
            return Resp(200, {"person": self.persons.get(q["id"][0])})
        return Resp(404, {"error": "unknown"})


class Case(unittest.TestCase):
    """Each test starts with no leads, default settings, rules and pipelines, and a fresh stand-in Apollo."""

    def setUp(self):
        for t in ("leads", "activities", "approvals", "suppression"):
            db.ex(f"DELETE FROM {t}")
        for k in ("products", "rules", "apollo_usage"):
            db.set_setting(k, json.dumps(db.PRODUCTS) if k == "products" else "")
        for k, v in (("ai_mode", "off"), ("mode", "approval"), ("min_score", "55"), ("dm_auto", "0"), ("apollo_daily_cap", "30"), ("contact_grades", "C"), ("lead_quality", "fit"), ("agent_enabled", "1")):
            db.set_setting(k, v)
        SENT.clear()
        self.ap = FakeApollo()
        apollo.httpx = types.SimpleNamespace(request=self.ap.request, HTTPError=httpx.HTTPError)
        apollo.time = types.SimpleNamespace(sleep=lambda s: self.slept.append(s))
        self.slept = []
        llm._client = None

    def lead(self, company):
        return db.one("SELECT * FROM leads WHERE company=?", (company,))

    def extra(self, company):
        return json.loads(self.lead(company)["extra"] or "{}")

    def timeline(self, company, kind=None):
        rows = db.q("SELECT kind, summary FROM activities WHERE lead_id=? ORDER BY id", (self.lead(company)["id"],))
        return [r["summary"] for r in rows if not kind or r["kind"] == kind]


def org(i, name, domain, **more):
    return {"id": f"org{i}", "name": name, "primary_domain": domain, "website_url": f"https://{domain}", "sanitized_phone": None, **more}


def person(i, first, title, has_email=True, **more):
    return {"id": f"per{i}", "first_name": first, "last_name_obfuscated": "A***b", "title": title, "has_email": has_email, "organization": {"name": more.pop("company", None)}, **more}


def revealed(name, title, email, status="verified", industry="health, wellness & fitness", employees=120, country="Saudi Arabia", domain=None):
    return {"name": name, "title": title, "email": email, "email_status": status, "linkedin_url": "https://www.linkedin.com/in/" + name.lower().replace(" ", "-"),
            "country": country, "organization": {"id": "orgx", "name": "x", "industry": industry, "estimated_num_employees": employees, "country": country,
                                                 "primary_domain": domain, "website_url": domain and f"https://{domain}"}}
