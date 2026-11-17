"""Deleting a lead, statuses the team adds itself, and switching a pipeline on and off."""
import json

from app import agent, db

from .base import Case, client, main, SENT


class DeleteLead(Case):
    def test_one_lead_goes_with_everything_attached(self):
        a = main._insert({"company": "Gone Gym", "contact_name": "Sam", "email": "sam@gone.test", "product": "gym_club", "industry": "gym"})
        b = main._insert({"company": "Kept Gym", "contact_name": "Kim", "email": "kim@kept.test", "product": "gym_club", "industry": "gym"})
        for _ in range(2):
            agent.tick(force=True)
        db.ex("INSERT INTO tasks(lead_id, title, created_at) VALUES (?,?,?)", (a, "Call them", db.now()))
        self.assertEqual(client.delete(f"/api/leads/{a}").json(), {"ok": True})
        self.assertIsNone(self.lead("Gone Gym"))
        for table in ("activities", "approvals"):
            self.assertEqual(db.one(f"SELECT COUNT(*) n FROM {table} WHERE lead_id=?", (a,))["n"], 0, table)
        self.assertEqual(db.one("SELECT lead_id FROM tasks WHERE title='Call them'")["lead_id"], None)   # the task stays, without the lead
        self.assertEqual(self.lead("Kept Gym")["id"], b)
        self.assertGreater(db.one("SELECT COUNT(*) n FROM approvals WHERE lead_id=?", (b,))["n"], 0)
        self.assertEqual(client.delete(f"/api/leads/{a}").status_code, 404)

    def test_several_at_once(self):
        ids = [main._insert({"company": f"Bulk {i}", "email": f"x@bulk{i}.test"}) for i in range(3)]
        self.assertEqual(client.post("/api/leads/bulk", json={"action": "delete", "ids": ids[:2]}).json()["updated"], 2)
        self.assertEqual([l["company"] for l in db.q("SELECT company FROM leads")], ["Bulk 2"])


class OwnStatuses(Case):
    def setUp(self):
        super().setUp()
        db.set_setting("custom_stages", "[]")

    def test_add_use_rename_remove(self):
        r = client.post("/api/stages", json={"name": "Proposal sent"}).json()
        key = r["stage"]["key"]
        self.assertEqual((key, r["stage"]["name"]), ("c_proposal_sent", "Proposal sent"))
        self.assertTrue(r["stage"]["color"].startswith("#"))
        self.assertEqual([c["key"] for c in client.get("/api/leads/facets").json()["custom_stages"]], [key])
        i = main._insert({"company": "Deal Gym", "contact_name": "Sam", "email": "sam@deal.test", "product": "gym_club", "industry": "gym"})
        self.assertEqual(client.patch(f"/api/leads/{i}", json={"stage": key}).status_code, 200)
        for _ in range(3):
            agent.tick(force=True)                                                       # the agent leaves a lead in a status of your own alone
        self.assertEqual((self.lead("Deal Gym")["stage"], SENT, db.one("SELECT COUNT(*) n FROM approvals")["n"]), (key, [], 0))
        self.assertEqual(client.get(f"/api/leads?stage={key}").json()["total"], 1)
        self.assertEqual(client.get("/api/stages").json()["custom"][0]["leads"], 1)
        self.assertEqual(client.post("/api/stages", json={"key": key, "name": "Quote sent", "color": "#123456"}).json()["stage"], {"key": key, "name": "Quote sent", "color": "#123456"})
        self.assertEqual(client.delete(f"/api/stages/{key}").status_code, 409)          # not while leads are in it
        client.patch(f"/api/leads/{i}", json={"stage": "paused"})
        self.assertEqual(client.delete(f"/api/stages/{key}").json()["custom"], [])

    def test_names_must_be_new_and_statuses_must_exist(self):
        client.post("/api/stages", json={"name": "Demo done"})
        for name in ("demo done", "Won", "In sequence", "x", ""):
            self.assertEqual(client.post("/api/stages", json={"name": name}).status_code, 400, name)
        i = main._insert({"company": "Any Gym", "email": "a@any.test"})
        self.assertEqual(client.patch(f"/api/leads/{i}", json={"stage": "c_made_up"}).status_code, 400)
        self.assertEqual(client.patch(f"/api/leads/{i}", json={"stage": "paused"}).status_code, 200)

    def test_a_reply_still_surfaces(self):
        key = client.post("/api/stages", json={"name": "Waiting on legal"}).json()["stage"]["key"]
        i = main._insert({"company": "Legal Gym", "contact_name": "Sam", "email": "sam@legal.test", "product": "gym_club"})
        client.patch(f"/api/leads/{i}", json={"stage": key})
        agent.handle_reply(i, "Subject: Re\n\nLet's talk on Tuesday.", {"from": "sam@legal.test"})
        self.assertEqual(self.lead("Legal Gym")["stage"], "meeting_booked")

    def test_bulk_and_import_accept_them(self):
        key = client.post("/api/stages", json={"name": "Demo done"}).json()["stage"]["key"]
        i = main._insert({"company": "Bulk Gym", "email": "a@bulkgym.test"})
        client.post("/api/leads/bulk", json={"action": "set_stage", "value": key, "ids": [i]})
        j = main._insert({"company": "Imported Gym", "email": "a@imported.test", "status": "Demo Done"})
        self.assertEqual((self.lead("Bulk Gym")["stage"], self.lead("Imported Gym")["stage"]), (key, key))


class PipelineSwitch(Case):
    def test_off_means_nothing_is_searched_researched_or_sent(self):
        a = main._insert({"company": "Off Gym", "contact_name": "Sam", "email": "sam@offgym.test", "product": "gym_club", "industry": "gym"})
        b = main._insert({"company": "On Co", "contact_name": "Kim", "email": "kim@onco.test", "product": "estimates"})
        self.assertEqual(client.post("/api/pipelines/gym_club/active", json={"active": False}).json(), {"key": "gym_club", "active": False})
        for _ in range(3):
            agent.tick(force=True)
        self.assertEqual((self.lead("Off Gym")["stage"], self.lead("On Co")["stage"]), ("new", "awaiting_approval"))
        ran = []
        real, main.discover_pipeline = main.discover_pipeline, lambda key, *x: ran.append(key)
        try:
            db.set_setting("last_discovery", ""); db.set_setting("auto_discovery", "1"); main._auto_discover()
        finally:
            main.discover_pipeline = real
        self.assertNotIn("gym_club", ran); self.assertIn("estimates", ran)
        pipes = {p["key"]: p["active"] for p in client.get("/api/pipelines").json()["pipelines"]}
        self.assertEqual((pipes["gym_club"], pipes["estimates"]), (False, True))
        client.post("/api/pipelines/gym_club/active", json={"active": True})
        for _ in range(2):
            agent.tick(force=True)
        self.assertEqual(self.lead("Off Gym")["stage"], "awaiting_approval")            # picked up again where it was
        self.assertEqual(SENT, [])

    def test_a_lead_already_in_a_sequence_waits_too(self):
        a = main._insert({"company": "Mid Gym", "contact_name": "Sam", "email": "sam@midgym.test", "product": "gym_club", "industry": "gym"})
        db.update_lead(a, stage="in_sequence", step=0, score=80, next_action_at="2020-01-01T00:00:00+00:00")
        client.post("/api/pipelines/gym_club/active", json={"active": False})
        agent.tick(force=True)
        self.assertEqual((self.lead("Mid Gym")["stage"], db.one("SELECT COUNT(*) n FROM approvals")["n"]), ("in_sequence", 0))
        self.assertEqual(client.post("/api/pipelines/nope/active", json={"active": True}).status_code, 404)


class PerPipeline(Case):
    def seed(self):
        a = main._insert({"company": "Clean Gym", "contact_name": "Sam", "email": "sam@cleangym.test", "product": "gym_club", "industry": "gym"})
        b = main._insert({"company": "Typo Gym", "contact_name": "Al", "email": "al@typogym.test", "product": "gym_club", "industry": "gym"})
        c = main._insert({"company": "Typo Estate", "contact_name": "Kim", "email": "kim@typoestate.test", "product": "estimates"})
        for _ in range(2):
            agent.tick(force=True)
        db.ex("UPDATE leads SET email='al@gmial.com' WHERE id=?", (b,)); db.ex("UPDATE leads SET email='kim@gmial.com' WHERE id=?", (c,))
        return a, b, c

    def test_leads_clean_up_for_one_pipeline(self):
        import time
        from app import quality
        self.seed()
        self.assertEqual(client.post("/api/cleanup", json={"action": "start", "product": "gym_club"}).status_code, 200)
        for _ in range(100):
            st = client.get("/api/cleanup").json()
            if not st["running"]:
                break
            time.sleep(0.05)
        self.assertEqual((st["product"], st["pipeline"], st["total"], st["found"]), ("gym_club", "BRN Gym Club", 2, 1))   # only this pipeline's leads were looked at
        client.post("/api/cleanup", json={"action": "apply", "product": "gym_club"})
        self.assertEqual((self.lead("Typo Gym")["stage"], self.lead("Typo Estate")["stage"], self.lead("Clean Gym")["stage"]), ("needs_contact", "awaiting_approval", "awaiting_approval"))

    def test_approvals_carry_the_pipeline_and_clean_up_by_pipeline(self):
        self.seed()
        rows = client.get("/api/approvals").json()
        self.assertEqual(sorted((r["company"], r["product"]) for r in rows), [("Clean Gym", "gym_club"), ("Typo Estate", "estimates"), ("Typo Gym", "gym_club")])
        self.assertTrue(all("grade" in r and "score" in r for r in rows))
        self.assertEqual(client.get("/api/approvals-cleanup?product=gym_club").json()["found"], 1)
        self.assertEqual(client.get("/api/approvals-cleanup").json()["found"], 2)
        r = client.post("/api/approvals-cleanup", json={"kinds": ["rule"], "product": "gym_club"}).json()
        self.assertEqual((r["removed"], r["waiting"]), (1, 2))
        self.assertEqual(self.lead("Typo Estate")["stage"], "awaiting_approval")         # the other pipeline's draft was left alone


class WebSearchSwitch(Case):
    def test_off_skips_web_search_sources_only(self):
        from app.providers import llm
        asked = []
        llm._client = object(); llm.over_budget = lambda: False
        real = llm.find_requests
        llm.find_requests = lambda *a, **k: asked.append(a) or []
        try:
            db.set_setting("web_search_leads", "0")
            out = main.discover_pipeline("gym_club", "intent")
            self.assertEqual(asked, []); self.assertIn("switched off", " ".join(out["notes"]))
            self.assertEqual(client.post("/api/linkedin/search", json={"product": "gym_club"}).status_code, 400)
            self.ap.orgs = []
            self.assertIn("apollo", main.discover_pipeline("gym_club", "apollo"))            # Apollo carries on
            self.assertTrue(any(p == "/mixed_companies/search" for p, _ in self.ap.calls))
            db.set_setting("web_search_leads", "1")
            main.discover_pipeline("gym_club", "intent")
            self.assertTrue(asked)
        finally:
            llm.find_requests = real; db.set_setting("web_search_leads", "1")
        self.assertEqual(client.post("/api/settings", json={"web_search_leads": "0"}).status_code, 200)
        self.assertEqual(client.get("/api/settings").json()["web_search_leads"], "0")
        db.set_setting("web_search_leads", "1")
