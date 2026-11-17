"""Fixes that came out of the production audit: the provider's send limit, errors in plain words, sources that refuse,
rules that had gaps, wrong research notes, the settings log and the public website form."""
import json
import types

import httpx

from app import agent, compliance, db, rules
from app.providers import channels, llm, sources

from .base import Case, client, main, Resp, SENT


class SendLimit(Case):
    def test_quota_stops_sending_for_the_day_and_says_so_once(self):
        ids = [main._insert({"company": f"Quota Gym {i}", "contact_name": "Sam", "email": f"sam@quota{i}.test", "product": "gym_club", "industry": "gym"}) for i in range(3)]
        for _ in range(2):
            agent.tick(force=True)
        def refuse(*a, **k):
            raise channels.QuotaReached("Resend's sending limit for your plan is used up for today")
        real, channels.send_email = channels.send_email, refuse
        try:
            aps = [a["id"] for a in client.get("/api/approvals").json()]
            r = client.post(f"/api/approvals/{aps[0]}", json={"approve": True})
            self.assertEqual(r.status_code, 502)
            self.assertTrue(channels.paused())
            second = client.post(f"/api/approvals/{aps[1]}", json={"approve": True})   # not even tried: no second failure
            self.assertIn("Sending starts again tomorrow", second.json()["detail"])
        finally:
            channels.send_email = real
        errors = db.q("SELECT summary FROM activities WHERE status='error'")
        self.assertEqual(len(errors), 1); self.assertIn("Sending stopped until tomorrow", errors[0]["summary"])
        self.assertEqual(db.one("SELECT COUNT(*) n FROM approvals WHERE state='pending'")["n"], 3)     # every draft is still there
        st = client.get("/api/stats").json()["send_paused"]
        self.assertIn("used up", st["why"]); self.assertGreater(st["until"], db.now())
        db.update_lead(ids[2], stage="in_sequence", step=1, next_action_at="2020-01-01T00:00:00+00:00")
        agent.tick(force=True)                                                                          # a lead that is due waits for tomorrow, without an error
        self.assertEqual(self.lead("Quota Gym 2")["next_action_at"], st["until"])
        self.assertEqual(len(db.q("SELECT 1 x FROM activities WHERE status='error'")), 1)

    def test_resend_quota_answer_is_recognised(self):
        from email.message import EmailMessage
        import os
        m = EmailMessage(); m["From"] = "a@b.test"; m["To"] = "c@d.test"; m["Subject"] = "x"; m.set_content("hi")
        os.environ["RESEND_API_KEY"] = "k"
        real = channels.httpx
        channels.httpx = types.SimpleNamespace(post=lambda *a, **k: Resp(429, {"name": "daily_quota_exceeded", "message": "You have reached your daily email sending quota."}))
        try:
            with self.assertRaises(channels.QuotaReached):
                channels._resend(m)
        finally:
            channels.httpx = real; os.environ.pop("RESEND_API_KEY")


class PlainErrors(Case):
    def test_claude_errors_are_explained_in_full(self):
        raw = "People asking for 'x': Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing.'}}"
        self.assertEqual(llm.explain(raw), "People asking for 'x': Claude refused the request because the Anthropic credit has run out. Add credit at console.anthropic.com > Billing.")
        other = llm.explain("Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'max_uses must be a positive integer and not something else entirely'}}")
        self.assertEqual(other, "Claude refused the request: max_uses must be a positive integer and not something else entirely")
        self.assertEqual(llm.explain("Reddit refused the search"), "Reddit refused the search")

    def test_an_account_problem_stops_the_other_searches(self):
        calls = []
        class Client:
            class messages:
                @staticmethod
                def create(**k):
                    calls.append(k["tools"][0]["type"])
                    raise RuntimeError("Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'Your credit balance is too low to access the Anthropic API.'}}")
        llm._client = Client; real = llm.over_budget; llm.over_budget = lambda: False
        try:
            first = main.discover_pipeline("gym_club", "intent")
            again = main.discover_pipeline("estimates", "intent")
        finally:
            llm._client = None; llm.over_budget = real
        self.assertEqual(len(calls), 1)                                                   # asked once, not once per phrase and pipeline
        self.assertIn("credit has run out", first["errors"][0]); self.assertNotIn("Error code", first["errors"][0])
        self.assertIn("Web searches were skipped", " ".join(again["notes"]))
        self.assertIn("credit has run out", db.one("SELECT summary FROM activities WHERE summary LIKE 'Lead discovery for BRN Gym Club%'")["summary"])   # the log has the whole reason
        self.assertEqual(client.get("/api/pipelines").json()["pipelines"][1]["last_run"]["problems"][0][:20], first["errors"][0][:20])

    def test_the_other_web_search_version_is_tried(self):
        seen = []
        class Msg:
            content = [types.SimpleNamespace(type="text", text='{"requests": []}')]; usage = None; stop_reason = "end_turn"
        class Client:
            class messages:
                @staticmethod
                def create(**k):
                    seen.append(k["tools"][0]["type"])
                    if len(seen) == 1:
                        raise RuntimeError("Error code: 400 - {'error': {'message': 'tool type web_search_20250305 is not supported for this model'}}")
                    return Msg
        llm._client = Client; order = list(llm.WEB_TOOLS)
        try:
            self.assertEqual(llm._web_json("find"), {"requests": []})
            self.assertEqual(seen, order); self.assertEqual(llm.WEB_TOOLS[0], order[1])   # remembered for next time
        finally:
            llm._client = None; llm.WEB_TOOLS[:] = order

    def test_google_and_reddit_refusals(self):
        real = sources._google_places
        def refuse(*a, **k):
            raise RuntimeError('Places API error 403: {"error": {"status": "PERMISSION_DENIED", "message": "Places API (New) has not been used in project 1 before or it is disabled."}}')
        sources._google_places = refuse
        try:
            with self.assertRaisesRegex(RuntimeError, "not switched on for this key's project"):
                sources.google_places("gyms in Riyadh")
            sources._google_places = lambda *a, **k: self.fail("asked again after a refusal")
            with self.assertRaisesRegex(RuntimeError, "not switched on"):
                sources.google_places("gyms in Jeddah")
        finally:
            sources._google_places = real
        self.assertIn("not switched on", client.get("/api/settings").json()["providers"]["places_error"])
        rh, sources.httpx = sources.httpx, types.SimpleNamespace(get=lambda *a, **k: Resp(403, {}), post=httpx.post)
        try:
            with self.assertRaisesRegex(RuntimeError, "REDDIT_CLIENT_ID"):
                sources.reddit_search("need a website")
        finally:
            sources.httpx = rh


class RuleGaps(Case):
    def test_support_and_department_mailboxes_get_no_cold_email(self):
        self.assertTrue(rules.state()["builtin"]["support_desk"]["on"])
        for address in ("support@ziina.test", "help@x.test", "billing@x.test", "careers@x.test", "support2@x.test"):
            self.assertEqual((rules.check({"email": address}, network=False) or {}).get("rule"), "support_desk", address)
        for address in ("sara@x.test", "info@x.test", "supporters.club@x.test"):
            self.assertIsNone(rules.check({"email": address}, network=False), address)
        i = main._insert({"company": "Ziina", "email": "support@ziina.test", "product": "gym_club", "industry": "gym"})
        for _ in range(2):
            agent.tick(force=True)
        self.assertEqual((self.lead("Ziina")["stage"], SENT, db.one("SELECT COUNT(*) n FROM approvals")["n"]), ("needs_contact", [], 0))

    def test_a_message_sent_by_a_machine_is_not_a_reply(self):
        i = main._insert({"company": "Bot Co", "contact_name": "Al", "email": "al@botco.test", "product": "gym_club"})
        db.update_lead(i, stage="in_sequence", step=1)
        agent.handle_reply(i, "Subject: Re: Idea\n\nHi! How can we help you today? Which product is your question about?", {"from": "support@botco.test", "automatic": True})
        l = self.lead("Bot Co")
        self.assertEqual((l["stage"], l["replied_at"], db.one("SELECT COUNT(*) n FROM approvals")["n"]), ("in_sequence", None, 0))

    def test_agencies_are_not_customers_even_where_software_companies_are(self):
        p = db.products(); p["qapipeline"]["sells_to_tech"] = True
        db.set_setting("products", json.dumps(list(p.values())))
        check = lambda company, **x: (rules.check({"company": company, "email": "a@b.test", "product": "qapipeline", "extra": json.dumps(x)}, network=False) or {}).get("rule")
        self.assertEqual(check("White Label Agency"), "vendor")
        self.assertEqual(check("WP Creative", vendor="wordpress agency"), "vendor")
        self.assertEqual(check("Cheesecake Labs", vendor="software development company"), "vendor")
        self.assertIsNone(check("Acme Analytics", vendor="software solutions"))             # a software product company is who QAPipeline is for
        self.assertIsNone(check("Harbor Travel Agency"))                                    # a travel agency sells no software services
        self.assertIsNone(rules.check({"company": "White Label Agency", "email": "a@b.test", "product": "white_label"}, network=False))   # the partner pipeline wants them

    def test_mailbox_check_says_when_it_cannot_run(self):
        r = {x["key"]: x for x in client.get("/api/rules").json()["builtin"]}
        self.assertIn("Not running", r["verify_mailbox"]["idle"]); self.assertIsNone(r["invalid_email"]["idle"])


class Notes(Case):
    def test_summary_names_the_kind_of_business_not_our_product(self):
        s = llm.research({"company": "ERA Members Club", "segment": "BRN Gym Club", "city": "Sharq", "email": "a@era.test"}, "offer", "icp", "gym_club")["summary"]
        self.assertEqual(s, "ERA Members Club is a business in Sharq.")
        s = llm.research({"company": "ERA Members Club", "industry": "gym", "segment": "BRN Gym Club", "city": "Sharq", "email": "a@era.test"}, "offer", "icp", "gym_club")["summary"]
        self.assertEqual(s, "ERA Members Club is a gym in Sharq.")
        self.assertIsNone(llm._lead_view({"company": "x", "segment": "BRN Gym Club"})["segment"])


class SettingsLog(Case):
    def test_only_what_changed_is_logged(self):
        s = client.get("/api/settings").json()
        body = {k: s[k] for k in ("daily_email_cap", "min_score", "sender_name", "products", "sequence", "timezone")}
        client.post("/api/settings", json=body)
        self.assertEqual(db.q("SELECT 1 x FROM activities WHERE summary LIKE 'Settings changed%'"), [])
        client.post("/api/settings", json={**body, "daily_email_cap": "25"})
        self.assertEqual([a["summary"] for a in db.q("SELECT summary FROM activities WHERE summary LIKE 'Settings changed%'")], ["Settings changed: daily_email_cap"])


class WebsiteForm(Case):
    GOOD = {"name": "Sara", "email": "sara@realco.test", "company": "Real Co", "message": "Can you tell me more about pricing?"}

    def test_spam_is_dropped_quietly(self):
        for bad in ({**self.GOOD, "email": "you@company.com"}, {**self.GOOD, "message": "Buy backlinks http://a.test http://b.test http://c.test"}, {**self.GOOD, "message": "Best SEO services for you"}, {**self.GOOD, "hp": "x"}):
            self.assertEqual(client.post("/api/inbound", json=bad).json(), {"ok": True})
        self.assertEqual(db.one("SELECT COUNT(*) n FROM leads")["n"], 0)
        self.assertEqual(client.post("/api/inbound", json=self.GOOD).status_code, 200)
        self.assertEqual(self.lead("Real Co")["stage"], "replied")

    def test_daily_ceiling_and_per_visitor_limit(self):
        db.set_setting("inbound_daily_cap", "3")
        codes = [client.post("/api/inbound", json={**self.GOOD, "email": f"p{i}@co{i}.test", "company": f"Co {i}"}, headers={"x-forwarded-for": f"10.0.0.{i}"}).status_code for i in range(5)]
        self.assertEqual(codes, [200, 200, 200, 429, 429])
        db.set_setting("inbound_daily_cap", "60")

    def test_a_key_can_be_required(self):
        db.set_setting("inbound_key", "s3cret")
        self.assertEqual(client.post("/api/inbound", json=self.GOOD).status_code, 403)
        self.assertEqual(client.post("/api/inbound", json={**self.GOOD, "key": "s3cret"}).status_code, 200)
