"""The bug fixes: junk email addresses, built-in email wording and waiting for Claude, automatic replies, who sent what,
and the pipeline list in Settings."""
import json

from app import agent, db, rules
from app.providers import llm, sources

from .base import Case, client, main, SENT

PAGE = """<html><head><script src="https://cdn.test/bootstrap@5.3.0/dist/js/bootstrap.min.js"></script>
<link href="https://cdn.test/npm/cookieconsent@3.1.0/build/cookieconsent.min.css" rel="stylesheet"><style>.a{background:url(logo@2x.png)}</style></head>
<body><script>var libs=["lodash@4.17.21"]; var support="robot@harborgym.test";</script><!-- old: former@harborgym.test -->
<form><input type="email" placeholder="you@company.com"><input placeholder="name@example.com"></form>
<p>Our auditors: security@compliance.com. Press: news@agency-partner.com</p><img src="team@2x.jpg">
<p>Write to us: <a href="mailto:Hello@HarborGym.test?subject=Hi">email us</a> or front.desk@harborgym.test</p></body></html>"""


class JunkEmails(Case):
    def test_what_counts_as_junk(self):
        for bad in ("bootstrap@5.3.0", "lodash@4.17.21", "cookieconsent@3.1.0", "you@company.com", "name@example.com", "email@yourdomain.com", "logo@2x.png", "react@18", "vue@3.4.21-beta", "user@domain.com"):
            self.assertIsNotNone(sources.junk_email(bad), bad)
        for good in ("faisal@irongym.sa", "info@harbor-gym.co.uk", "omar.s@gmail.com", "sales@company.io", "hello@my-company.com", "j@x.co"):
            self.assertIsNone(sources.junk_email(good), good)
        self.assertIn("version number", rules.email_problem("lodash@4.17.21"))

    def test_scraping_reads_the_page_not_its_code(self):
        real, sources._get = sources._get, lambda url, timeout=6: (200, PAGE if url.rstrip("/").endswith("harborgym.test") else "")
        try:
            self.assertEqual(sources.site_facts("https://www.harborgym.test")["email"], "hello@harborgym.test")
            page = PAGE.replace("Hello@HarborGym.test", "x@elsewhere.org").replace("front.desk@harborgym.test", "")
            sources._get = lambda url, timeout=6: (200, page if url.rstrip("/").endswith("harborgym.test") else "")
            self.assertIsNone(sources.site_facts("https://www.harborgym.test")["email"])   # only other companies' addresses, script contents and placeholders: nothing is taken
        finally:
            sources._get = real

    def test_one_off_clean_up(self):
        a = main._insert({"company": "Code Co", "email": "ok@codeco.test"}); b = main._insert({"company": "Fine Co", "email": "sam@fineco.test"}); c = main._insert({"company": "Answered Co", "email": "ok2@answered.test"})
        db.ex("UPDATE leads SET email='bootstrap@5.3.0', stage='awaiting_approval' WHERE id=?", (a,)); db.ex("UPDATE leads SET email='you@company.com', replied_at=?, stage='replied' WHERE id=?", (db.now(), c))
        db.ex("INSERT INTO approvals(lead_id, channel, action, content, created_at) VALUES (?,?,?,?,?)", (a, "email", "intro", "{}", db.now()))
        self.assertEqual(main.fix_junk_emails(), 1)
        l = self.lead("Code Co")
        self.assertEqual((l["email"], l["stage"], self.extra("Code Co")["bad_email"]), (None, "needs_contact", "bootstrap@5.3.0"))
        self.assertEqual(db.one("SELECT COUNT(*) n FROM approvals WHERE state='pending'")["n"], 0)
        self.assertEqual((self.lead("Fine Co")["email"], self.lead("Answered Co")["stage"]), ("sam@fineco.test", "replied"))
        self.assertIn("package name", self.timeline("Code Co", "blocked")[0])


class Wording(Case):
    S = {"sender_name": "brn dynamics", "product_key": "qapipeline", "product_name": "QAPipeline", "offer": "QAPipeline by brndynamics: AI-assisted test management for QA teams. Plans, runs and reports in one place."}

    def test_templates_use_the_first_name_and_claim_nothing(self):
        e = llm.template_email({"company": "Acme", "contact_name": "Sara Khan"}, {}, "intro", self.S)
        self.assertTrue(e["body"].startswith("Hi Sara,"))
        self.assertIn("We make QAPipeline (AI-assisted test management for QA teams)", e["body"])
        self.assertNotIn("by brndynamics", e["body"])
        nameless = llm.template_email({"company": "Acme"}, {}, "intro", self.S)["body"]
        self.assertTrue(nameless.startswith("Hello,")); self.assertNotIn("Hi there", nameless)
        fix = {**self.S, "product_key": "fixify", "product_name": "Fixify"}
        for step in ("intro", "follow_up_1", "follow_up_2", "breakup"):
            body = llm.template_email({"company": "Acme", "contact_name": "Sara"}, {"pain_points": ["unclear"]}, step, fix)["body"].lower()
            for claim in ("i ran", "health check", "report", "i noticed", "we noticed", "i looked"):
                self.assertNotIn(claim, body, step)
        scanned = llm.template_email({"company": "Acme", "extra": json.dumps({"issues": [{"problem": "Home page took 7.0s to respond"}]})}, {}, "intro", fix)["body"]
        self.assertIn("Home page took 7.0s to respond", scanned)                       # a real scan result may be quoted

    def test_no_outreach_draft_while_claude_is_down(self):
        llm._client = object(); db.set_setting("ai_mode", "economy"); db.set_setting("ai_daily_budget", "5")
        real, llm._cooldown_until = llm._cooldown_until, llm.time.time() + 600
        try:
            i = main._insert({"company": "Wait Gym", "contact_name": "Sam", "email": "sam@waitgym.test", "product": "gym_club", "industry": "gym"})
            for _ in range(3):
                agent.tick(force=True)
            l = self.lead("Wait Gym")
            self.assertEqual((l["stage"], l["next_action"]), ("in_sequence", "waiting_for_claude"))
            self.assertEqual(db.one("SELECT COUNT(*) n FROM approvals")["n"], 0)
            self.assertEqual(len([t for t in self.timeline("Wait Gym") if t.startswith("Not drafted yet")]), 1)      # said once, not every hour
            self.assertEqual(client.get("/api/stats").json()["llm_status"]["waiting"], 1)
            llm._cooldown_until = 0
            llm._ask_json = lambda *a, **k: {"subject": "Renewals at Wait Gym", "body": "Hi Sam,\n\nA real email.", "cta": "Book a call"}
            db.ex("UPDATE leads SET next_action_at=? WHERE id=?", ("2020-01-01T00:00:00+00:00", i))
            agent.tick(force=True)
            self.assertEqual(self.lead("Wait Gym")["stage"], "awaiting_approval")
            self.assertIn("A real email.", json.loads(db.one("SELECT content FROM approvals")["content"])["body"])            # written by Claude once it is back, not the built-in wording
        finally:
            llm._cooldown_until = real

    def test_replies_still_get_a_holding_draft_while_claude_is_down(self):
        llm._client = object(); db.set_setting("ai_mode", "economy")
        real, llm._cooldown_until = llm._cooldown_until, llm.time.time() + 600
        try:
            i = main._insert({"company": "Keen Gym", "contact_name": "Kim", "email": "kim@keen.test", "product": "gym_club"})
            agent.handle_reply(i, "Subject: Re\n\nYes, I'm interested. Can you send pricing?", {"from": "kim@keen.test"})
            a = db.one("SELECT action, content FROM approvals WHERE lead_id=?", (i,))
            self.assertEqual(a["action"], "reply"); self.assertIn("Hi Kim,", json.loads(a["content"])["body"])
        finally:
            llm._cooldown_until = real

    def test_with_ai_switched_off_the_built_in_wording_is_used(self):
        main._insert({"company": "Off Gym", "contact_name": "Sam", "email": "sam@offgym.test", "product": "gym_club", "industry": "gym"})
        for _ in range(2):
            agent.tick(force=True)
        self.assertEqual(self.lead("Off Gym")["stage"], "awaiting_approval")


class AutoReplies(Case):
    AUTO = ["Subject: Automatic reply: Idea\n\nI am out of the office until Monday with limited access to email.",
            "Subject: Re: Idea\n\nThank you for contacting Acme. We have received your message and a member of our team will respond as soon as possible. Ticket #48213.",
            "Subject: How did we do?\n\nPlease rate your conversation with our team. Your feedback matters.",
            "Subject: Re: Idea\n\nThis mailbox is for job applications. For recruitment please send your CV to careers.",
            "Subject: Re: Idea\n\nYour email has been forwarded to the relevant department."]
    REAL = [("Subject: Re: Idea\n\nThanks for contacting us. Yes we are interested, let's talk next week.", "meeting"),
            ("Subject: Re: Idea\n\nSounds useful, tell me more.", "interested"), ("Subject: Re\n\nNot interested, thanks.", "not_interested"), ("Subject: Re\n\nPlease remove me from your list.", "unsubscribe")]

    def test_classification(self):
        for t in self.AUTO:
            self.assertEqual(llm.classify_reply(t)["intent"], "auto_reply", t[:50])
        for t, want in self.REAL:
            self.assertEqual(llm.classify_reply(t)["intent"], want, t[:50])

    def test_an_automatic_message_changes_nothing(self):
        i = main._insert({"company": "Away Co", "contact_name": "Al", "email": "al@away.test", "product": "gym_club"})
        db.update_lead(i, stage="in_sequence", step=1)
        agent.handle_reply(i, self.AUTO[0], {"from": "al@away.test"})
        l = self.lead("Away Co")
        self.assertEqual((l["stage"], l["replied_at"]), ("in_sequence", None))
        self.assertEqual(db.one("SELECT COUNT(*) n FROM approvals")["n"], 0)
        self.assertTrue(self.timeline("Away Co", "email_reply")[0].startswith("Automatic message, not a reply"))

    def test_old_ones_are_refiled_and_leave_the_reports(self):
        i = main._insert({"company": "Old Co", "contact_name": "Al", "email": "al@old.test", "product": "gym_club"})
        db.log(i, "email_sent", "intro: Hi", channel="email", payload={"action": "intro"})
        db.log(i, "email_reply", "Reply (interested): positive reply", channel="email", payload={"text": self.AUTO[1], "intent": "interested"})
        db.update_lead(i, replied_at=db.now(), stage="replied")
        before = client.get("/api/reports").json()
        self.assertEqual((before["totals"]["replies"], before["totals"]["positive"]), (1, 1))
        self.assertEqual(main.fix_auto_replies(), 1)
        after = client.get("/api/reports").json()
        self.assertEqual((after["totals"]["replies"], after["totals"]["positive"]), (0, 0))
        self.assertEqual({s["name"]: s["leads"] for s in after["funnel"]["steps"]}["Replied positively"], 0)


class Reports(Case):
    def send(self, name, approve_as=None, automatic=False):
        i = main._insert({"company": name, "contact_name": "Sam", "email": f"sam@{name.split()[0].lower()}.test", "product": "gym_club", "industry": "gym"})
        if automatic:
            db.set_setting("mode", "live")
        for _ in range(2):
            agent.tick(force=True)
        db.set_setting("mode", "approval")
        if not automatic:
            aid = db.one("SELECT id FROM approvals WHERE lead_id=? AND state='pending'", (i,))["id"]
            token = db.actor.set({"name": approve_as}) if approve_as else None
            try:
                self.assertEqual(main._decide(aid, True), "sent")
            finally:
                if token:
                    db.actor.reset(token)
        return i

    def test_by_person_says_who_approved(self):
        import os
        os.environ["BRN_ALLOW_LIVE"] = "1"
        self.send("Shared Gym"); self.send("Named Gym", approve_as="Mustafa"); self.send("Auto Gym", automatic=True)
        people = {p["name"]: p["sent"] for p in client.get("/api/reports").json()["by_person"]}
        self.assertEqual(people, {"Approved by hand (shared sign-in)": 1, "Mustafa": 1, "Agent (sent automatically)": 1})
        self.assertEqual(len(SENT), 3)

    def test_funnel_and_period_agree_on_positive_replies(self):
        a, b, c = self.send("One Gym"), self.send("Two Gym"), self.send("Three Gym")
        agent.handle_reply(a, "Subject: Re\n\nSounds useful, tell me more.", {"from": "x@one.test"})
        agent.handle_reply(b, "Subject: Re\n\nNot interested, thanks.", {"from": "x@two.test"})
        agent.handle_reply(c, "Subject: Automatic reply\n\nI am out of the office.", {"from": "x@three.test"})
        r = client.get("/api/reports").json()
        steps = {s["name"]: s["leads"] for s in r["funnel"]["steps"]}
        self.assertEqual((r["totals"]["replies"], r["totals"]["positive"]), (2, 1))
        self.assertEqual((steps["Replied"], steps["Replied positively"]), (2, 1))


class PipelineList(Case):
    def test_settings_lists_every_pipeline_with_its_kind(self):
        db.set_setting("products", json.dumps([{"key": "gym_club", "name": "Gym", "pitch": "x"}, {"key": "new_websites", "name": "New sites", "pitch": "x"}, {"key": "my_own", "name": "Mine", "pitch": "x"}]))
        prods = {p["key"]: p for p in client.get("/api/settings").json()["products"]}
        self.assertIn("white_label", prods)                                             # the partner pipeline can be edited even where pipelines were saved before it existed
        self.assertEqual((prods["white_label"]["type"], prods["white_label"]["partner"], prods["new_websites"]["type"], prods["gym_club"]["type"], prods["my_own"]["type"]), ("partner", True, "service", "product", "product"))
        self.assertTrue(all(p["type"] in ("product", "service", "partner") for p in client.get("/api/pipelines").json()["pipelines"]))
        self.assertTrue(prods["white_label"]["pitch"] and prods["white_label"]["sources"]["apollo"]["industries"])
