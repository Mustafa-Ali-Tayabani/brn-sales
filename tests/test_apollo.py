"""Apollo as a lead source: discovery, hiring evidence, the credit guard on revealing emails, scoring, agencies."""
import json

from app import agent, compliance, db, prospect, rules
from app.providers import apollo

from .base import Case, client, main, org, person, revealed, SENT


class Discovery(Case):
    def setUp(self):
        super().setUp()
        self.ap.orgs = [org(1, "Iron Gym", "irongym.sa"), org(2, "Pulse Fitness", "pulse.sa"), org(3, "Known Gym", "known.sa"), org(4, "Blocked Gym", "blocked.sa"), org(5, "Empty Gym", "empty.sa")]
        self.ap.hiring_ids = {"org2"}
        self.ap.people = {"org1": [person(1, "Omar", "Operations Manager"), person(2, "Faisal", "Owner"), person(3, "Sara", "Receptionist")],
                          "org2": [person(4, "Noura", "General Manager")], "org3": [person(5, "Ali", "Owner")], "org4": [person(6, "Zed", "Owner")], "org5": []}
        self.ap.ads = {"org2": [{"title": "Front Desk Agent", "url": "https://jobs.test/1", "posted_at": "2026-11-10T00:00:00Z"}, {"title": "Accountant", "url": "https://jobs.test/2"}]}
        main._insert({"company": "Already here", "email": "x@known.sa"})
        compliance.suppress("blocked.sa", "asked")

    def test_companies_people_and_dedupe(self):
        out = main.discover_pipeline("gym_club", "apollo")
        self.assertEqual(out["errors"], [])
        names = [l["company"] for l in db.q("SELECT company FROM leads WHERE source='apollo' ORDER BY id")]
        self.assertEqual(names, ["Pulse Fitness", "Iron Gym", "Empty Gym"])          # hiring company first; known domain and do-not-contact domain left out
        iron = self.lead("Iron Gym")
        self.assertEqual((iron["contact_name"], iron["title"], iron["website"], iron["email"], iron["country"]), ("Faisal", "Owner", "https://irongym.sa", None, "SA"))
        self.assertEqual(self.extra("Iron Gym")["apollo"]["person_id"], "per2")       # the pipeline's first title (owner) wins over operations manager
        self.assertIsNone(self.lead("Empty Gym")["contact_name"])
        search = next(q for p, q in self.ap.calls if p == "/mixed_companies/search")
        self.assertEqual(search["organization_industries[]"], ["health, wellness & fitness"])
        self.assertEqual(search["organization_num_employees_ranges[]"], ["20,500"])
        self.assertEqual(search["organization_locations[]"], ["Saudi Arabia", "United Arab Emirates", "Kuwait", "Qatar"])

    def test_hiring_signal_with_evidence(self):
        main.discover_pipeline("gym_club", "apollo")
        it = self.extra("Pulse Fitness")["intent"]
        self.assertEqual((it["level"], it["quote"], it["url"], it["posted"]), ("hiring", "Front Desk Agent", "https://jobs.test/1", "2026-11-10"))
        self.assertEqual(agent.signal(self.lead("Pulse Fitness")), "hiring")
        self.assertIsNone(agent.signal(self.lead("Iron Gym")))
        self.assertEqual(client.get(f"/api/leads/{self.lead('Pulse Fitness')['id']}").json()["signal"], "hiring")

    def test_credits_are_counted(self):
        out = main.discover_pipeline("gym_club", "apollo")
        u = apollo.usage()
        self.assertEqual((u["today_by_kind"]["search"], u["today_by_kind"]["jobs"], u["today_by_kind"]["reveal"]), (2, 1, 0))   # filters search + hiring search, one job ad look-up
        self.assertEqual(out["apollo"]["credits"], 3)
        self.assertEqual(client.get("/api/apollo/usage").json()["month"], 3)

    def test_running_again_adds_nothing(self):
        main.discover_pipeline("gym_club", "apollo")
        n = db.one("SELECT COUNT(*) n FROM leads")["n"]
        main.discover_pipeline("gym_club", "apollo")
        self.assertEqual(db.one("SELECT COUNT(*) n FROM leads")["n"], n)

    def test_preview_saves_nothing(self):
        r = client.post("/api/apollo/preview", json={"key": "gym_club", "apollo": {"industries": ["health, wellness & fitness"], "countries": ["SA"], "job_titles": ["owner"], "hiring_keywords": ["front desk"]}})
        self.assertEqual(r.status_code, 200, r.text)
        rows = r.json()["rows"]
        self.assertEqual([x["company"] for x in rows][:2], ["Pulse Fitness", "Iron Gym"])
        self.assertTrue(rows[0]["hiring"]) and self.assertEqual(rows[1]["person"], "Faisal")
        self.assertEqual(db.one("SELECT COUNT(*) n FROM leads WHERE source='apollo'")["n"], 0)
        self.assertFalse(any(p.endswith("/job_postings") for p, _ in self.ap.calls))   # a preview does not spend credits on job ads
        self.assertEqual(client.post("/api/apollo/preview", json={"key": "gym_club", "apollo": {}}).status_code, 400)

    def test_free_plan_falls_back_to_people_search(self):
        self.ap.no_company_search = True
        self.ap.people = {"a": [person(1, "Faisal", "Owner", company="Iron Gym"), person(2, "Omar", "Operations Manager", company="Iron Gym"), person(3, "Noura", "General Manager", company="Pulse Fitness", _hiring=True)]}
        out = main.discover_pipeline("gym_club", "apollo")
        self.assertEqual(out["apollo"]["how"], "people")
        self.assertEqual(apollo.used(), 0)                                             # people search is free
        self.assertEqual(sorted(l["company"] for l in db.q("SELECT company FROM leads WHERE source='apollo'")), ["Iron Gym", "Pulse Fitness"])
        self.assertEqual(self.lead("Iron Gym")["contact_name"], "Faisal")
        self.assertEqual(agent.signal(self.lead("Pulse Fitness")), "hiring")
        self.assertIn("no company search", " ".join(out["notes"]))

    def test_cap_stops_searches(self):
        db.set_setting("apollo_daily_cap", "1")
        out = main.discover_pipeline("gym_club", "apollo")
        self.assertEqual(apollo.used(), 1)
        self.assertTrue(any("hiring" in n for n in out["notes"]))
        self.assertIn("cap", " ".join(main.discover_pipeline("gym_club", "apollo")["errors"]))

    def test_inactive_pipelines_are_skipped_by_the_daily_run(self):
        ran = []
        real, main.discover_pipeline = main.discover_pipeline, lambda key, *a: ran.append(key)
        try:
            db.set_setting("last_discovery", ""); db.set_setting("auto_discovery", "1")
            main._auto_discover()
        finally:
            main.discover_pipeline = real
        self.assertIn("gym_club", ran) and self.assertIn("white_label", ran)
        for key in ("web_mobile", "website_build", "ai_automation", "crm_erp", "security", "custom"):
            self.assertNotIn(key, ran)
        self.assertEqual(db.one("SELECT COUNT(*) n FROM leads")["n"], 1)               # nothing deleted


class Backoff(Case):
    def test_short_rate_limit_is_waited_out(self):
        self.ap.orgs, self.ap.rate_limited = [org(1, "A Gym", "a.sa")], [2, 3]
        self.assertEqual(len(apollo.companies({"industries": ["restaurants"]})), 1)
        self.assertEqual(self.slept, [2, 4])                                           # the server's wait, a little longer each time

    def test_long_rate_limit_is_reported_not_slept(self):
        self.ap.rate_limited = [600]
        with self.assertRaisesRegex(RuntimeError, "rate limit.*10 minute"):
            apollo.companies({"industries": ["restaurants"]})
        self.assertEqual((self.slept, apollo.used()), ([], 0))


class Reveal(Case):
    def add(self, name, pid="per1", has_email=True, hiring=False, rank=0, domain=None):
        x = {"apollo": {"org_id": "org1", "person_id": pid, "has_email": has_email, "title_rank": rank, "searched": {"industries": ["x"], "employee_ranges": ["20,500"], "countries": ["SA"]}}}
        if hiring:
            x["intent"] = {"level": "hiring", "quote": "Hiring: Front Desk Agent", "url": "https://jobs.test/1", "where": "Apollo job postings"}
        main._insert({"company": name, "website": domain and f"https://{domain}", "contact_name": "Faisal", "title": "Owner", "country": "SA", "source": "apollo", "product": "gym_club", "extra": x}, "apollo")
        return self.lead(name)

    def test_good_lead_gets_a_verified_email_and_goes_on(self):
        self.add("Iron Gym", domain="irongym.sa")
        self.ap.persons["per1"] = revealed("Faisal Al Harbi", "Owner", "faisal@irongym.sa")
        agent.tick(force=True)
        l, x = self.lead("Iron Gym"), self.extra("Iron Gym")
        self.assertEqual((l["contact_name"], l["email"], l["stage"]), ("Faisal Al Harbi", "faisal@irongym.sa", "awaiting_approval"))   # a draft waits for a person; nothing is sent
        self.assertEqual((x["email_status"], x["contact_via"], x["apollo"]["industry"], x["apollo"]["employees"]), ("verified", "Apollo", "health, wellness & fitness", 120))
        self.assertEqual(apollo.usage()["today_by_kind"]["reveal"], 1)
        self.assertIn("verified by Apollo", " ".join(self.timeline("Iron Gym", "enrich")))
        self.assertEqual(rules.mailbox_status(l), "valid")                              # no second check with Hunter

    def test_guessed_email_is_not_used(self):
        self.add("Guess Gym")
        self.ap.persons["per1"] = revealed("Sam Guess", "Owner", "sam@guess.sa", status="guessed")
        agent.tick(force=True)
        l = self.lead("Guess Gym")
        self.assertEqual((l["email"], l["stage"], self.extra("Guess Gym")["email_status"]), (None, "needs_contact", "guessed"))
        self.assertIn("not verified", " ".join(self.timeline("Guess Gym")))

    def test_no_credit_for_a_grade_c_lead(self):
        self.add("Weak Gym", rank=None)
        db.ex("UPDATE leads SET contact_name=NULL, country='DE' WHERE company='Weak Gym'")   # nobody suitable, wrong country: a weak fit
        db.set_setting("min_score", "70")
        agent.tick(force=True)
        self.assertEqual((self.lead("Weak Gym")["stage"], apollo.used()), ("needs_contact", 0))
        self.assertIn("No credit was spent", " ".join(self.timeline("Weak Gym")))

    def test_no_credit_when_apollo_has_no_email(self):
        self.add("Noemail Gym", has_email=False)
        agent.tick(force=True)
        self.assertEqual((self.lead("Noemail Gym")["stage"], apollo.used()), ("needs_contact", 0))

    def test_no_credit_for_a_lead_a_rule_stops(self):
        client.post("/api/rules", json={"builtin": {"keywords": {"on": True, "value": "ruled"}}})
        self.add("Ruled Gym")
        agent.tick(force=True)
        self.assertEqual((self.lead("Ruled Gym")["stage"], apollo.used()), ("disqualified", 0))

    def test_global_cap_makes_the_lead_wait_until_tomorrow(self):
        db.set_setting("apollo_daily_cap", "1")
        self.add("First Gym", "per1"); self.add("Second Gym", "per2")
        self.ap.persons = {"per1": revealed("A One", "Owner", "a@first.sa"), "per2": revealed("B Two", "Owner", "b@second.sa")}
        agent.tick(force=True)
        second = self.lead("Second Gym")
        self.assertEqual((self.lead("First Gym")["email"], second["email"], second["stage"], second["next_action"]), ("a@first.sa", None, "new", "reveal_email"))
        self.assertGreater(second["next_action_at"], db.now())
        agent.tick(force=True)                                                          # not retried every minute
        self.assertEqual(sum(1 for p, _ in self.ap.calls if p == "/people/match"), 1)

    def test_pipeline_cap(self):
        p = db.products(); p["gym_club"]["sources"]["apollo"]["reveal_limit"] = 1
        db.set_setting("products", json.dumps(list(p.values())))
        self.add("First Gym", "per1"); self.add("Second Gym", "per2")
        self.ap.persons = {"per1": revealed("A One", "Owner", "a@first.sa"), "per2": revealed("B Two", "Owner", "b@second.sa")}
        agent.tick(force=True)
        self.assertEqual((apollo.used(pipeline="gym_club"), self.lead("Second Gym")["next_action"]), (1, "reveal_email"))

    def test_nothing_is_sent(self):
        self.add("Iron Gym"); self.ap.persons["per1"] = revealed("Faisal Al Harbi", "Owner", "faisal@irongym.sa")
        for _ in range(3):
            agent.tick(force=True)
        self.assertEqual((SENT, self.lead("Iron Gym")["stage"]), ([], "awaiting_approval"))


class Scoring(Case):
    def test_scores_spread_out(self):
        p = db.products()["gym_club"]
        base = {"industry": "health, wellness & fitness", "employees": 120, "has_email": True}
        def score(country="SA", rank=0, name="Faisal", hiring=False, **facts):
            x = {"apollo": {**base, "title_rank": rank, **facts}}
            if hiring:
                x["intent"] = {"level": "hiring", "quote": "q"}
            return prospect.fit({"contact_name": name, "title": "Owner", "country": country, "product": "gym_club", "extra": json.dumps(x)}, p)[0]
        best, plain, lower_title, nobody = score(hiring=True), score(), score(rank=2), score(rank=None, name=None)
        wrong_size, wrong_country, wrong_industry = score(employees=5), score(country="DE"), score(industry="mining & metals")
        self.assertEqual(len({best, plain, lower_title, nobody, wrong_size, wrong_country, wrong_industry}), 7)        # seven different leads, seven different scores
        self.assertGreater(best, plain); self.assertGreater(plain, lower_title); self.assertGreater(lower_title, nobody)
        self.assertGreater(nobody, wrong_industry); self.assertGreater(plain - wrong_size, 15)
        self.assertGreaterEqual(best, 90); self.assertLess(wrong_industry, 60)
        why = prospect.fit({"contact_name": "Faisal", "title": "Owner", "country": "SA", "extra": json.dumps({"apollo": base})}, p)[1]
        self.assertIn("120 staff: the size you sell to", why)

    def test_excluded_industry_scores_low(self):
        p = db.products()["qapipeline"]
        s, why = prospect.fit({"country": "US", "extra": json.dumps({"apollo": {"industry": "Marketing & Advertising", "employees": 100}})}, p)
        self.assertEqual(s, 20); self.assertIn("leaves out", why[0])

    def test_titles_are_per_pipeline_then_global(self):
        p = db.products()
        self.assertEqual(prospect.titles(p["qapipeline"])[:4], ["cto", "vp engineering", "head of qa", "qa lead"])
        self.assertIn("founder", prospect.titles(p["qapipeline"]))                      # the global list follows
        self.assertEqual(prospect.titles({"titles": "head chef", "sources": {"apollo": {"job_titles": ["owner"]}}})[:2], ["head chef", "owner"])   # what you typed on the pipeline comes first
        from app import quality
        self.assertEqual([t.strip() for t in quality.titles_for({"product": "qapipeline"})][:2], ["cto", "vp engineering"])


class Agencies(Case):
    def add(self, name, industry, product="qapipeline"):
        x = {"apollo": {"org_id": "o", "person_id": "per1", "has_email": True, "title_rank": 0, "searched": {"industries": ["computer software"]}}}
        main._insert({"company": name, "contact_name": "Dana", "title": "CTO", "country": "US", "source": "apollo", "product": product, "extra": x}, "apollo")
        self.ap.persons["per1"] = revealed("Dana Fox", "CTO", f"dana@{name.split()[0].lower()}.com", industry=industry, employees=200, country="United States")

    def test_apollo_industry_marks_an_agency_and_moves_it_to_partners(self):
        self.add("Brightside", "information technology & services")
        agent.tick(force=True)
        l = self.lead("Brightside")
        self.assertEqual((l["product"], l["stage"]), ("white_label", "new"))
        self.assertIn("Apollo lists it under information technology & services", " ".join(self.timeline("Brightside", "blocked")))
        self.assertEqual(SENT, [])

    def test_apollo_industry_beats_a_name_that_only_sounds_like_an_agency(self):
        self.add("Harbor Web Solutions", "computer software")                           # the old name guess would have flagged "web solutions"
        agent.tick(force=True)
        l = self.lead("Harbor Web Solutions")
        self.assertEqual((l["product"], l["stage"]), ("qapipeline", "awaiting_approval"))

    def test_without_apollo_data_the_website_guess_still_works(self):
        main._insert({"company": "Pixel Web Solutions", "email": "a@pixel.test", "product": "qapipeline"})
        agent.tick(force=True)
        self.assertEqual(self.lead("Pixel Web Solutions")["product"], "white_label")


class Seeds(Case):
    def test_seed_is_added_only_where_a_pipeline_has_no_apollo_search(self):
        db.set_setting("products", json.dumps([{"key": "gym_club", "name": "Gym", "pitch": "x", "sources": {"apollo": {"industries": ["my own"]}}},
                                               {"key": "estimates", "name": "Est", "pitch": "x", "sources": {}}, {"key": "security_testing", "name": "Sec", "pitch": "x"},
                                               {"key": "custom_ai", "name": "Custom", "pitch": "x"}, {"key": "new_websites", "name": "Sites", "pitch": "x", "active": True}]))
        p = db.products()
        self.assertEqual(p["gym_club"]["sources"]["apollo"]["industries"], ["my own"])
        self.assertEqual(p["estimates"]["sources"]["apollo"]["countries"], ["CH", "GB", "AE", "SA"])
        self.assertEqual(p["security_testing"]["sources"]["apollo"]["job_titles"][1], "ciso")
        self.assertEqual((p["custom_ai"]["active"], p["new_websites"]["active"], p["gym_club"]["active"]), (False, True, True))
        self.assertEqual((p["custom_ai"]["type"], p["security_testing"]["type"], p["estimates"]["type"], p["white_label"]["type"]), ("service", "service", "product", "partner"))


class Problems(Case):
    BODY = {"key": "gym_club", "apollo": {"industries": ["health, wellness & fitness"], "technologies": ["WordPress"], "job_titles": ["owner"]}}

    def test_a_key_that_cannot_search_says_what_to_do(self):
        self.ap.no_company_search = self.ap.no_people_search = True
        r = client.post("/api/apollo/preview", json=self.BODY)
        self.assertEqual(r.status_code, 400)
        self.assertIn("master API key", r.json()["detail"]); self.assertIn("paid Apollo plan", r.json()["detail"])
        checks = client.get("/api/apollo/check").json()["checks"]
        self.assertEqual([c["ok"] for c in checks], [True, False, None]); self.assertIn("master key", checks[1]["detail"])

    def test_connection_check_when_it_works(self):
        self.ap.people = {"a": [person(1, "Faisal", "Owner", company="Iron Gym")]}
        checks = client.get("/api/apollo/check").json()["checks"]
        self.assertEqual([c["ok"] for c in checks], [True, True, None])
        self.assertEqual(apollo.used(), 0)                                               # the check itself is free

    def test_a_rejected_filter_is_left_out_and_reported(self):
        self.ap.orgs, self.ap.reject_tech = [org(1, "Iron Gym", "irongym.sa")], True
        self.ap.people = {"org1": [person(1, "Faisal", "Owner")]}
        r = client.post("/api/apollo/preview", json=self.BODY)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["rows"][0]["company"], "Iron Gym")
        self.assertIn("technologies filter", " ".join(r.json()["notes"]))

    def test_an_unexpected_answer_does_not_break_the_search(self):
        self.ap.odd = True
        r = client.post("/api/apollo/preview", json=self.BODY)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([x["company"] for x in r.json()["rows"]], ["Odd Co"])
