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
            with self.assertRaisesRegex(RuntimeError, "is not switched on in the project this key belongs to.*Google said: .Places API \\(New\\) has not been used"):
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


class DeveloperList(Case):
    def test_code_is_never_stored_as_an_email_whichever_search_found_it(self):
        for src in ("web", "google_places", "intent", "apollo", "manual"):
            i = main._insert({"company": f"Code {src}", "email": "bootstrap@4.6.0", "website": f"https://code-{src}.test"}, src)
            l = db.one("SELECT email, extra FROM leads WHERE id=?", (i,))
            self.assertIsNone(l["email"], src); self.assertEqual(json.loads(l["extra"])["bad_email"], "bootstrap@4.6.0")
        agent.tick(force=True)
        self.assertTrue({l["stage"] for l in db.q("SELECT stage FROM leads")} <= {"needs_contact", "reply_manually"})   # none is researched for an email it does not have
        self.assertEqual(db.one("SELECT COUNT(*) n FROM leads WHERE email IS NOT NULL")["n"], 0)
        self.assertEqual(SENT, [])

    def test_a_lead_only_in_replied_because_of_an_automatic_message_goes_back(self):
        def lead(name, text, intent="interested", step=1):
            i = main._insert({"company": name, "contact_name": "Al", "email": f"al@{name.split()[0].lower()}.test", "product": "gym_club"})
            db.log(i, "email_sent", "intro: Hi", channel="email", payload={"action": "intro"})
            db.log(i, "email_reply", f"Reply ({intent}): x", channel="email", payload={"text": text, "intent": intent})
            db.update_lead(i, stage="replied", step=step, replied_at=db.now(), next_action="human_followup")
            return i
        auto = "Subject: Automatic reply\n\nI am out of the office until Monday."
        a = lead("Away Co", auto); b = lead("Real Co", "Subject: Re\n\nSounds useful, tell me more."); c = lead("Done Co", auto, step=9)
        d = lead("Both Co", auto); db.log(d, "email_reply", "Reply (interested): x", channel="email", payload={"text": "Yes please, tell me more.", "intent": "interested"})
        e = lead("Earlier Co", auto, intent="auto_reply")                                 # re-filed by the previous version, stage left behind
        f = lead("Acted Co", auto); db.update_lead(f, stage="meeting_booked")
        self.assertEqual(main.fix_auto_replies(), 4)
        st = lambda n: (self.lead(n)["stage"], self.lead(n)["replied_at"] is None)
        self.assertEqual((st("Away Co"), st("Earlier Co"), st("Done Co")), (("in_sequence", True), ("in_sequence", True), ("completed", True)))
        self.assertEqual((st("Real Co"), st("Both Co"), st("Acted Co")[0]), (("replied", False), ("replied", False), "meeting_booked"))   # a person answered, or someone acted: left alone
        self.assertGreater(self.lead("Away Co")["next_action_at"], db.now())               # the next email is drafted tomorrow, not this minute
        self.assertIn("automatic one, not a reply", self.timeline("Away Co", "stage_change")[-1])
        self.assertEqual({s["name"]: s["leads"] for s in client.get("/api/reports").json()["funnel"]["steps"]}["Replied"], 2)

    def test_connected_means_tried_and_working(self):
        r = {x["key"]: x for x in client.get("/api/connections/check").json()["results"]}
        self.assertEqual((r["claude"]["ok"], r["inbox"]["ok"], r["verify"]["ok"], r["voice"]["ok"], r["places"]["ok"], r["meet"]["ok"]), (None, None, None, None, None, None))
        self.assertTrue(r["enrich"]["ok"]); self.assertIn("paid Apollo plan", r["enrich"]["detail"])   # the stand-in Apollo answers
        self.ap.no_people_search = True
        r = {x["key"]: x for x in client.get("/api/connections/check").json()["results"]}
        self.assertEqual((r["enrich"]["ok"], r["people"]["ok"]), (False, False)); self.assertIn("master key", r["enrich"]["detail"])
        self.assertFalse(r["email"]["ok"])                                                 # smtp.test does not exist: a key being there is not enough
        class Client:
            class messages:
                @staticmethod
                def create(**k):
                    raise RuntimeError("Error code: 400 - {'error': {'message': 'Your credit balance is too low to access the Anthropic API.'}}")
        llm._client = Client
        try:
            c = {x["key"]: x for x in client.get("/api/connections/check").json()["results"]}["claude"]
        finally:
            llm._client = None
        self.assertEqual(c["ok"], False); self.assertIn("credit has run out", c["detail"])
        import os
        blocked = {"error": {"code": 403, "message": "Requests to this API places.googleapis.com method google.maps.places.v1.Places.SearchText are blocked.", "status": "PERMISSION_DENIED",
                             "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_SERVICE_BLOCKED", "domain": "googleapis.com"}]}}
        asked = []
        answers = [Resp(403, blocked), Resp(200, {"places": [{"id": "x"}]})]
        os.environ["GOOGLE_PLACES_API_KEY"] = "k"
        rh, sources.httpx = sources.httpx, types.SimpleNamespace(post=lambda url, headers=None, json=None, timeout=None: (asked.append(headers["X-Goog-FieldMask"]), answers.pop(0))[1], get=httpx.get)
        try:
            pl = {x["key"]: x for x in client.get("/api/connections/check").json()["results"]}["places"]
            self.assertEqual(pl["ok"], False)
            self.assertIn("is not allowed to use \"Places API (New)\"", pl["detail"]); self.assertIn("API restrictions", pl["detail"])
            self.assertIn("Google said: \"Requests to this API places.googleapis.com", pl["detail"])                 # their own words, whole
            self.assertEqual(client.get("/api/settings").json()["providers"]["places_error"], pl["detail"])
            fixed = {x["key"]: x for x in client.get("/api/connections/check").json()["results"]}["places"]            # after the key is put right in Google Cloud
            self.assertEqual((fixed["ok"], client.get("/api/settings").json()["providers"]["places_error"]), (True, None))
            self.assertEqual(asked, ["places.id", "places.id"])                                                        # the test asks for IDs only, which Google does not charge for
        finally:
            sources.httpx = rh; os.environ.pop("GOOGLE_PLACES_API_KEY")
        for text, want in (("billing to be enabled", "no billing account"), ('"reason": "API_KEY_HTTP_REFERRER_BLOCKED"', "only works from a website"), ("API key not valid. Please pass a valid API key.", "not a valid key")):
            self.assertIn(want, sources._why_google('Google Places error 403: {"error": {"message": "' + text.replace('"', "'") + '"}}' if "reason" not in text else 'x {"error": {"message": "m", "details": [{' + text + '}]}}'))
        self.assertEqual(len(client.get("/api/settings").json()["connections_checked"]["results"]), len(r))   # the last test result is kept for the page

    def test_errors_are_kept_whole(self):
        long = "Error code: 400 - {'error': {'message': '" + "the request was refused because " + "x" * 300 + " end of message'}}"
        self.assertTrue(llm.explain(long).endswith("end of message"))
        llm._note_error(RuntimeError(long))
        self.assertTrue(db.setting("llm_error").endswith("end of message"))
        llm._cooldown_until = 0

    def test_website_form_can_be_tied_to_your_site(self):
        db.set_setting("inbound_site", "brndynamics.com")
        body = {"name": "Sara", "email": "sara@realco.test", "company": "Real Co", "message": "Hello"}
        try:
            self.assertEqual(client.post("/api/inbound", json=body).status_code, 403)
            self.assertEqual(client.post("/api/inbound", json=body, headers={"origin": "https://evil.test"}).status_code, 403)
            self.assertEqual(client.post("/api/inbound", json=body, headers={"origin": "https://www.brndynamics.com"}).status_code, 200)
        finally:
            db.set_setting("inbound_site", "")


class Cost(Case):
    def setUp(self):
        super().setUp()
        db.ex("DELETE FROM ai_usage")
        for k, v in (("ai_model", "claude-haiku-5-5"), ("ai_daily_budget", "0.50"), ("ai_balance", ""), ("ai_balance_at", ""), ("ai_balance_base", ""), ("web_search_max_uses", "3"), ("ai_mode", "economy")):
            db.set_setting(k, v)

    def usage(self, i=0, o=0, read=0, stored=0, searches=0):
        return types.SimpleNamespace(input_tokens=i, output_tokens=o, cache_read_input_tokens=read, cache_creation_input_tokens=stored,
                                     server_tool_use=types.SimpleNamespace(web_search_requests=searches) if searches else None)

    def client(self, seen, searches=2, tokens=40000):
        outer = self
        class Msg:
            content = [types.SimpleNamespace(type="text", text='{"requests": [], "businesses": [], "sites": []}')]; stop_reason = "end_turn"
            usage = outer.usage(i=tokens, o=500, searches=searches)
        class Client:
            class messages:
                @staticmethod
                def create(**k):
                    seen.append(k["tools"][0]); return Msg
        return Client

    def test_cost_is_what_anthropic_reports(self):
        cost, tin, tout, n = llm.cost_of(self.usage(i=40000, o=1000, searches=3), "claude-haiku-5-5")
        self.assertEqual((tin, tout, n), (40000, 1000, 3))
        self.assertAlmostEqual(cost, 40000 * 0.10 / 1e6 + 1000 * 0.50 / 1e6 + 3 * 0.01, 6)      # tokens (the pages read are in them) + $0.01 a search
        self.assertAlmostEqual(llm.cost_of(self.usage(i=1000, read=10000, stored=2000), "claude-sonnet-5-5")[0], (1000 * 2 + 10000 * 2 * 0.1 + 2000 * 2 * 1.25) / 1e6, 8)
        self.assertAlmostEqual(llm.cost_of(self.usage(i=150000, o=1000), "claude-haiku-5-5")[0], (150000 * 0.50 + 1000 * 2.50) / 1e6, 6)   # a long prompt costs more per token
        self.assertAlmostEqual(llm.cost_of({"input_tokens": 100, "output_tokens": 10, "server_tool_use": {"web_search_requests": 1}}, "claude-haiku-5-5")[0], 0.010015, 6)

    def test_searches_are_recorded_and_shown(self):
        seen = []; llm._client = self.client(seen, searches=2)
        try:
            llm._web_json("find something")
        finally:
            llm._client = None
        row = db.one("SELECT calls, searches, cost FROM ai_usage")
        self.assertEqual((row["calls"], row["searches"]), (1, 2)); self.assertAlmostEqual(row["cost"], 40000 * 0.10 / 1e6 + 500 * 0.50 / 1e6 + 0.02, 6)
        u = client.get("/api/ai/usage").json() if client.get("/api/ai/usage").status_code == 200 else llm.usage_report()
        self.assertEqual((u["searches_month"], u["max_searches"]), (2, 3))

    def test_every_search_is_held_to_the_ceiling(self):
        seen = []; llm._client = self.client(seen)
        try:
            llm.find_requests("need a website"); llm.discover_businesses("gyms"); llm.find_websites(["Acme"]); llm.find_owner("Acme")
            db.set_setting("web_search_max_uses", "2"); llm.find_requests("need a website")
            db.set_setting("web_search_max_uses", "99"); llm.find_requests("need a website")
        finally:
            llm._client = None
        self.assertEqual([t["max_uses"] for t in seen], [3, 3, 3, 3, 2, 5])
        self.assertTrue(all(t["name"] == "web_search" for t in seen))

    def test_the_daily_limit_also_stops_find_leads_now(self):
        seen = []; llm._client = self.client(seen, searches=3, tokens=2_000_000)
        try:
            llm.discover_businesses("gyms in Riyadh")                                         # one very long search: about $1 spent, past the $0.50 limit
            first = len(seen)
            again = client.post("/api/pipelines/gym_club/discover").json()                   # the button
            owner = None
            try:
                llm.find_owner("Acme")
            except RuntimeError as e:
                owner = str(e)
        finally:
            llm._client = None
        self.assertGreaterEqual(llm.spent()["c"], 0.50)
        self.assertEqual(len(seen), first)                                                    # nothing more was asked of Claude
        self.assertTrue(any("budget" in x for x in again["notes"] + again["errors"]), again)
        self.assertIn("Today's Claude budget of $0.50 is used up", owner)

    def test_only_spending_after_the_balance_was_entered_is_subtracted(self):
        db.ex("INSERT INTO ai_usage(day, model, task, calls, input_tokens, output_tokens, cost) VALUES (?,?,?,?,?,?,?)", (db.now()[:10], "claude-haiku-5-5", "email", 5, 1, 1, 1.30))
        client.post("/api/settings", json={"ai_balance": "8.61"})
        self.assertEqual(llm.usage_report()["balance_left"], 8.61)                            # what was spent earlier today is already out of the figure typed in
        db.ex("UPDATE ai_usage SET cost=cost+0.40")
        self.assertEqual(llm.usage_report()["balance_left"], 8.21)
        client.post("/api/settings", json={"ai_balance": "8.61", "sender_name": "x"})         # saving other settings does not start the count again
        self.assertEqual(llm.usage_report()["balance_left"], 8.21)
        client.post("/api/settings", json={"ai_balance": "20"})
        self.assertEqual(llm.usage_report()["balance_left"], 20.0)


class MonthlyLimit(Case):
    def spend(self, dollars, day=None):
        db.ex("INSERT INTO ai_usage(day, model, task, calls, input_tokens, output_tokens, cost) VALUES (?,?,?,?,?,?,?)", (day or db.now()[:8] + "01", "claude-haiku-5-5", "discovery", 1, 1, 1, dollars))

    def test_the_month_has_its_own_limit(self):
        db.set_setting("ai_monthly_budget", "5"); db.set_setting("ai_mode", "economy"); llm._client = object()
        try:
            self.spend(4.90)
            self.assertIsNone(llm.limit_reached()); self.assertTrue(llm._usable("email"))
            self.spend(0.20, day=db.now()[:8] + "02" if db.now()[8:10] != "02" else db.now()[:8] + "03")
            self.assertEqual(llm.limit_reached(), "this month's Claude budget of $5.00 is used up ($5.10 spent)")
            self.assertFalse(llm._usable("email")); self.assertTrue(llm.over_budget())
            with self.assertRaisesRegex(RuntimeError, "This month's Claude budget of \\$5.00 is used up"):
                llm._web_json("find")
            i = main._insert({"company": "Wait Gym", "contact_name": "Sam", "email": "sam@waitgym.test", "product": "gym_club", "industry": "gym"})
            for _ in range(2):
                agent.tick(force=True)
            self.assertEqual((self.lead("Wait Gym")["next_action"], db.one("SELECT COUNT(*) n FROM approvals")["n"]), ("waiting_for_claude", 0))   # no built-in wording sent instead
            st = client.get("/api/stats").json()["llm_status"]
            self.assertEqual((st["over_budget"], st["month_budget"], st["month"]), (True, 5.0, 5.1)); self.assertIn("this month", st["limit"])
            client.post("/api/settings", json={"ai_monthly_budget": "20"})
            self.assertIsNone(llm.limit_reached())
        finally:
            llm._client = None

    def test_anthropics_own_figure_is_used_when_higher(self):
        import os
        import httpx
        self.spend(1.57)
        self.assertIsNone(llm.anthropic_reported())                                        # no admin key: nothing is asked
        os.environ["ANTHROPIC_ADMIN_KEY"] = "sk-ant-admin-test"
        seen = []
        def get(url, params=None, headers=None, timeout=None):
            seen.append((url, params, headers))
            return Resp(200, {"data": [{"starting_at": "x", "cost": {"currency": "USD", "amount": "1300.000000", "list_amount": "1300.000000"}}, {"cost": {"amount": "24.000000"}}],
                              "has_more": False, "data_refreshed_at": "2026-11-18T06:00:00Z"})
        real, httpx.get = httpx.get, get
        try:
            r = llm.anthropic_reported()
            self.assertEqual((r["month"], r["as_of"], r["error"]), (13.24, "2026-11-18T06:00:00Z", None))      # cents in, dollars out
            self.assertEqual(seen[0][0], "https://api.anthropic.com/v1/organizations/analytics/cost")
            self.assertEqual((seen[0][2]["x-api-key"], seen[0][2]["anthropic-beta"]), ("sk-ant-admin-test", "analytics-usage-cost-2026-08-24"))
            self.assertTrue(seen[0][1]["starting_at"].endswith("-01T00:00:00Z"))
            self.assertEqual(llm.month_spent(), 13.24)                                     # their figure, not the app's 1.57
            db.set_setting("ai_monthly_budget", "10")
            self.assertIn("$13.24 spent", llm.limit_reached())
            llm.anthropic_reported(); self.assertEqual(len(seen), 1)                        # asked once in half an hour, not on every page load
            u = llm.usage_report(); self.assertEqual((u["month_spent"], u["reported"]["month"]), (13.24, 13.24))
            httpx.get = lambda *a, **k: Resp(401, {"error": "bad key"})
            bad = llm.anthropic_reported(fresh=True)
            self.assertEqual((bad["month"], bad["error"]), (None, "Anthropic refused the admin key."))
            self.assertEqual(llm.month_spent(), 1.57)                                      # falls back to the app's own count
        finally:
            httpx.get = real; os.environ.pop("ANTHROPIC_ADMIN_KEY", None)
