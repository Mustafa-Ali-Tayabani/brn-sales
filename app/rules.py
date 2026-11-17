"""Rules that decide which leads the agent may work on (Settings > Rules).

Each rule is checked before a lead is researched and again before every email, so a rule switched on today also
stops leads already in a sequence. A rule that fires does one of three things:
  fix     the lead waits under "Needs contact" until its details are corrected (wrong or dead email address)
  skip    the lead is disqualified
  review  the lead is paused for a person to decide
Built-in rules are switched on and off; custom rules are "when <field> <test> <value> then <outcome>"."""
import json
import os
import re
import time

import httpx

from . import countries, db
from .providers import sources

TYPOS = {"gmial.com": "gmail.com", "gamil.com": "gmail.com", "gmai.com": "gmail.com", "gmail.co": "gmail.com", "gmail.con": "gmail.com", "gnail.com": "gmail.com",
         "gmaill.com": "gmail.com", "hotmial.com": "hotmail.com", "hotmai.com": "hotmail.com", "hotmail.con": "hotmail.com", "outlok.com": "outlook.com",
         "outloo.com": "outlook.com", "yahooo.com": "yahoo.com", "yaho.com": "yahoo.com", "yahoo.con": "yahoo.com", "icloud.con": "icloud.com"}
NOREPLY = ("noreply", "no-reply", "no_reply", "donotreply", "do-not-reply", "do_not_reply", "mailer-daemon", "postmaster", "bounce", "bounces")
ROLE = ("info", "sales", "support", "admin", "contact", "hello", "office", "enquiries", "inquiries", "team", "help", "mail", "marketing", "hr", "jobs",
        "careers", "billing", "accounts", "webmaster", "service", "customerservice", "general")
FREE = ("gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "hotmail.com", "outlook.com", "live.com", "msn.com", "icloud.com", "me.com", "aol.com",
        "proton.me", "protonmail.com", "gmx.com", "mail.com", "zoho.com", "yandex.com")
DISPOSABLE = ("mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com", "temp-mail.org", "yopmail.com", "trashmail.com", "sharklasers.com",
              "getnada.com", "dispostable.com", "maildrop.cc", "fakeinbox.com", "throwawaymail.com", "tempmailo.com", "moakt.com")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")

# key, name shown, what it does, on by default, outcome, whether it takes a value (and its hint)
BUILTIN = [
    {"key": "invalid_email", "name": "Skip email addresses that are written wrong", "outcome": "fix", "default": True,
     "about": "No @, spaces, a missing or impossible ending, or a common typo such as gmial.com. The lead waits under Needs contact until the address is fixed."},
    {"key": "dead_domain", "name": "Skip addresses whose domain cannot receive email", "outcome": "fix", "default": True,
     "about": "Before the first email, the domain is looked up (through a public DNS service) to see whether it accepts mail at all. Saves bounces, which hurt your sender reputation."},
    {"key": "verify_mailbox", "name": "Check that the mailbox exists before the first email", "outcome": "fix", "default": True,
     "about": "Asks a verification service whether the address really exists. Catches addresses that look fine but bounce. Works once HUNTER_API_KEY is set on the server; each check uses one verification from your Hunter allowance."},
    {"key": "noreply", "name": "Skip no-reply addresses", "outcome": "fix", "default": True,
     "about": "Addresses like noreply@ or donotreply@ are never read by anyone."},
    {"key": "disposable", "name": "Skip throwaway email services", "outcome": "skip", "default": True,
     "about": "Temporary inboxes such as Mailinator. Nobody real is behind them."},
    {"key": "vendor", "name": "Never pitch companies that sell services like ours as customers", "outcome": "skip", "default": True, "value": "partner",
     "choices": {"partner": "Move them to the white-label partner pipeline, where they get the partnership pitch", "skip": "Skip them altogether"},
     "about": "Software houses, web and app agencies, IT service firms and marketing agencies do not buy what you sell to customers. Judged from the company's own website and name. They never receive a customer email; you choose whether they are approached as partners instead."},
    {"key": "role", "name": "Hold shared mailboxes for review", "outcome": "review", "default": False,
     "about": "Addresses like info@, sales@ or support@ go to a shared inbox instead of a person. The agent first looks for a named person with their own address; only when none is found is the lead paused for you, so no email goes to a shared inbox."},
    {"key": "free", "name": "Hold free email providers for review", "outcome": "review", "default": False,
     "about": "Gmail, Yahoo, Outlook and similar: often a private address, not a business one."},
    {"key": "one_per_company", "name": "One person per company at a time", "outcome": "review", "default": False,
     "about": "If someone at the same email domain is already being contacted or has replied, the next lead from that company is paused."},
    {"key": "no_name", "name": "Hold leads without a contact name", "outcome": "review", "default": False,
     "about": "Emails that open with \"Hi there\" get fewer replies. Paused until a name is added."},
    {"key": "only_countries", "name": "Only bring in leads from these countries", "outcome": "skip", "default": False, "value": "", "hint": "Countries, separated by commas, e.g. Pakistan, Saudi Arabia, UAE, United Kingdom",
     "about": "Searches look in these countries first, a lead found anywhere else is not added, and one already in the list is skipped. A lead whose country cannot be told (no phone, a .com address) is let through, marked Unknown."},
    {"key": "countries", "name": "Never contact leads in these countries", "outcome": "skip", "default": False, "value": "", "hint": "Countries, separated by commas, e.g. Germany, France, GB",
     "about": "Use for places where you do not sell, or where the law makes cold outreach unwise."},
    {"key": "keywords", "name": "Skip leads containing these words", "outcome": "skip", "default": False, "value": "", "hint": "Words or names, separated by commas, e.g. competitor name, student, freelance",
     "about": "Checked against the company, contact name, job title, email and website."},
    {"key": "max_emails", "name": "Stop after this many emails without a reply", "outcome": "skip", "default": False, "value": "4", "hint": "A number",
     "about": "A safety net on top of the sequence: no lead gets more emails than this, however the sequence is set up."},
]
FIELDS = {"email": "Email", "company": "Company", "contact_name": "Contact name", "title": "Job title", "website": "Website", "phone": "Phone", "country": "Country",
          "city": "City", "segment": "Category", "source": "Source", "product": "Pipeline", "score": "Fit score", "notes": "Notes"}
TESTS = {"contains": "contains", "not_contains": "does not contain", "equals": "is exactly", "starts": "starts with", "ends": "ends with",
         "empty": "is empty", "not_empty": "is not empty", "lt": "is less than", "gt": "is more than"}
OUTCOMES = {"skip": "skip the lead", "review": "pause it for me to decide", "fix": "wait for its details to be fixed"}
_dns = {}


def state():
    """Saved rules merged over the defaults: {"builtin": {key: {"on", "value"}}, "custom": [...]}."""
    try:
        saved = json.loads(db.setting("rules") or "{}")
    except ValueError:
        saved = {}
    builtin = {}
    for r in BUILTIN:
        s = (saved.get("builtin") or {}).get(r["key"]) or {}
        builtin[r["key"]] = {"on": bool(s.get("on", r["default"])), "value": str(s.get("value", r.get("value", "")) or "")}
    return {"builtin": builtin, "custom": [c for c in saved.get("custom") or [] if c.get("field") in FIELDS and c.get("test") in TESTS and c.get("outcome") in OUTCOMES]}


def save(payload):
    cur = state()
    for key, v in (payload.get("builtin") or {}).items():
        if key in cur["builtin"]:
            value = str(v.get("value") or "").strip()
            if key in ("countries", "only_countries"):   # "Pakistan, uae" is kept as "PK, AE": what a lead is stored with
                value = ", ".join(countries.codes(value))
            cur["builtin"][key] = {"on": bool(v.get("on")), "value": value}
    if "custom" in payload:
        cur["custom"] = [{"field": c["field"], "test": c["test"], "value": str(c.get("value") or "").strip(), "outcome": c["outcome"], "on": bool(c.get("on", True))}
                         for c in payload["custom"] if c.get("field") in FIELDS and c.get("test") in TESTS and c.get("outcome") in OUTCOMES][:50]
    db.set_setting("rules", json.dumps(cur, ensure_ascii=False))
    return cur


def wanted_countries():
    """The countries leads are brought in from (codes), or [] when leads from anywhere are welcome."""
    b = state()["builtin"]["only_countries"]
    return countries.codes(b["value"]) if b["on"] else []


def unwanted_country(code):
    """Is this (known) country one that leads are not brought in from?"""
    b = state()["builtin"]
    want = countries.codes(b["only_countries"]["value"]) if b["only_countries"]["on"] else []
    never = countries.codes(b["countries"]["value"]) if b["countries"]["on"] else []
    return bool(code) and ((bool(want) and code not in want) or code in never)


def _words(text):
    return [w.strip().lower() for w in re.split(r"[,\n;]", text or "") if w.strip()]


def email_problem(address):
    """Why this address is written wrong, or None when it looks fine."""
    a = (address or "").strip()
    if not a:
        return None
    if " " in a or a.count("@") != 1:
        return "it has spaces in it" if " " in a else "it needs exactly one @"
    local, domain = a.rsplit("@", 1)
    if not EMAIL_RE.match(a) or ".." in a or local.startswith(".") or local.endswith(".") or domain.startswith("-"):
        return "it is not a complete address"
    if domain.lower() in TYPOS:
        return f"the domain looks like a typo of {TYPOS[domain.lower()]}"
    return None


def domain_accepts_mail(domain):
    """True/False, or None when it could not be checked (never blocks a lead on a failed lookup)."""
    domain = domain.lower().strip()
    if os.environ.get("BRN_DNS_CHECK") == "0":
        return None
    hit = _dns.get(domain)
    if hit and time.time() - hit[0] < 86400:
        return hit[1]
    ok = None
    try:
        for kind in ("MX", "A"):
            r = httpx.get("https://dns.google/resolve", params={"name": domain, "type": kind}, timeout=6).json()
            if r.get("Status") == 3:      # the domain does not exist
                ok = False
                break
            if r.get("Status") == 0 and any(a.get("type") in (15, 1) for a in r.get("Answer") or []):
                ok = True
                break
            ok = False if r.get("Status") == 0 else None
            if ok is None:
                break
    except Exception:
        ok = None
    if ok is not None:
        _dns[domain] = (time.time(), ok)
    return ok


def _custom_hit(rule, lead):
    v = lead.get(rule["field"])
    text, want = ("" if v is None else str(v)).strip().lower(), rule["value"].lower()
    t = rule["test"]
    if t == "empty":
        return not text
    if t == "not_empty":
        return bool(text)
    if t in ("lt", "gt"):
        try:
            return float(text) < float(want) if t == "lt" else float(text) > float(want)
        except ValueError:
            return False
    if not want:
        return False
    return {"contains": want in text, "not_contains": want not in text, "equals": text == want, "starts": text.startswith(want), "ends": text.endswith(want)}[t]


_verified = {"day": "", "n": 0}


def mailbox_status(lead):
    """What the verifier says about this lead's mailbox, asked once and remembered on the lead. None when not checked."""
    try:
        extra = json.loads(lead.get("extra") or "{}") or {}
    except ValueError:
        extra = {}
    seen = extra.get("email_check") or {}
    if seen.get("email") == lead["email"].lower():
        return seen.get("status")
    if extra.get("email_status") == "verified" and extra.get("contact_via") == "Apollo":
        return "valid"   # Apollo already verified this mailbox when it revealed it: no second check, no Hunter needed
    from .providers import enrich
    today = db.now()[:10]
    if _verified["day"] != today:
        _verified.update(day=today, n=0)
    if not enrich.verifier() or _verified["n"] >= int(db.setting("verify_daily_cap") or 100):
        return None
    _verified["n"] += 1
    status = enrich.verify_email(lead["email"])
    if status and lead.get("id"):
        extra["email_check"] = {"email": lead["email"].lower(), "status": status, "at": today}
        db.ex("UPDATE leads SET extra=? WHERE id=?", (json.dumps(extra, ensure_ascii=False), lead["id"]))
    return status


def _lookup_pending(lead):
    """A shared mailbox is not held while a named person can still be looked up for it: the search runs first, during research."""
    from . import quality
    from .providers import enrich, llm
    try:
        looked = "person_lookup" in (json.loads(lead.get("extra") or "{}") or {})
    except ValueError:
        looked = False
    return not looked and not lead.get("emails_sent") and db.setting("dm_auto") == "1" and bool(enrich.people_finder() or llm.enabled()) and quality.needs_person(lead)


def check(lead, network=True, rules=None, verify=True, early=False):
    """The first rule this lead breaks, as {"rule", "name", "reason", "outcome"}, or None when it may go ahead."""
    st = rules or state()
    on = lambda k: st["builtin"][k]["on"]
    val = lambda k: st["builtin"][k]["value"]
    def hit(key, reason):
        outcome = next(r["outcome"] for r in BUILTIN if r["key"] == key)
        if key == "vendor" and val("vendor") != "skip" and partner_pipeline():
            outcome = "partner"
        return {"rule": key, "name": next(r["name"] for r in BUILTIN if r["key"] == key), "reason": reason, "outcome": outcome}
    email = (lead.get("email") or "").strip()
    local, _, domain = email.lower().rpartition("@")
    if email:
        problem = email_problem(email)
        if on("invalid_email") and problem:
            return hit("invalid_email", f"The email address {email} looks wrong: {problem}.")
        if not problem:
            if on("noreply") and any(local == n or local.startswith(n) for n in NOREPLY):
                return hit("noreply", f"{email} is a no-reply address that nobody reads.")
            if on("disposable") and domain in DISPOSABLE:
                return hit("disposable", f"{email} is a throwaway inbox.")
            if on("role") and local in ROLE and not (early and _lookup_pending(lead)):
                return hit("role", f"{email} is a shared mailbox, not a person.")
            if on("free") and domain in FREE:
                return hit("free", f"{email} is a free personal address ({domain}).")
            if on("dead_domain") and network and not lead.get("emails_sent") and domain_accepts_mail(domain) is False:
                return hit("dead_domain", f"The domain {domain} cannot receive email, so {email} would bounce.")
            if on("verify_mailbox") and network and verify and not lead.get("emails_sent"):
                status = mailbox_status(lead)
                if status in ("invalid", "disposable"):
                    return hit("verify_mailbox", f"{email} does not exist ({status})." if status == "invalid" else f"{email} is a throwaway inbox.")
            if on("one_per_company") and domain not in FREE and lead.get("id"):
                other = db.one("""SELECT company, contact_name FROM leads WHERE id<>? AND lower(email) LIKE ? AND
                                  stage IN ('in_sequence','awaiting_approval','replied','meeting_booked') LIMIT 1""", (lead["id"], f"%@{domain}"))
                if other:
                    return hit("one_per_company", f"Someone else at {domain} ({other['contact_name'] or other['company']}) is already being contacted.")
    if on("vendor") and not db.products().get(lead.get("product") or "", {}).get("sells_to_tech"):
        try:
            extra = json.loads(lead.get("extra") or "{}") or {}
        except ValueError:
            extra = {}
        if (extra.get("apollo") or {}).get("industry") and "vendor" in extra:   # Apollo says what the company does: that decides it, not a guess from its name or site
            why = extra["vendor"]
        else:
            why = extra.get("vendor") or sources.looks_like_vendor(lead.get("company"), lead.get("industry"), lead.get("segment") if lead.get("source") != "hiring" else "")
        if why and not (extra.get("intent") or {}).get("level") in ("asked", "inbound"):   # someone who asked us is a buyer, whatever they do
            if str(why).startswith("Apollo lists"):
                return hit("vendor", f"{why}, an industry that sells services like ours, so it is a competitor rather than a buyer.")
            return hit("vendor", f'It looks like a provider of similar services ("{why}"), so a competitor rather than a buyer.')
    if on("no_name") and not (lead.get("contact_name") or "").strip():
        return hit("no_name", "There is no contact name for this lead.")
    where = countries.code(lead.get("country"))
    from .agent import CHOSEN_SOURCES   # a lead a person added themselves is their choice, whatever country it is in
    if (on("only_countries") and where and countries.codes(val("only_countries")) and where not in countries.codes(val("only_countries"))
            and (lead.get("source") or "") not in CHOSEN_SOURCES):
        return hit("only_countries", f"It is in {countries.name(where)}, which is not one of the countries you bring leads from.")
    if on("countries") and where and where in countries.codes(val("countries")):
        return hit("countries", f"Leads in {countries.name(where)} are not contacted.")
    if on("keywords"):
        hay = " ".join(str(lead.get(k) or "") for k in ("company", "contact_name", "title", "email", "website")).lower()
        word = next((w for w in _words(val("keywords")) if w in hay), None)
        if word:
            return hit("keywords", f'It contains "{word}".')
    if on("max_emails") and val("max_emails").isdigit() and int(lead.get("emails_sent") or 0) >= int(val("max_emails")) and not lead.get("replied_at"):
        return hit("max_emails", f"{lead.get('emails_sent')} emails have gone out without a reply.")
    for i, c in enumerate(st["custom"]):
        if c.get("on", True) and _custom_hit(c, lead):
            return {"rule": f"custom_{i}", "name": describe(c), "reason": f"Your rule: {describe(c)}.", "outcome": c["outcome"]}
    return None


def describe(c):
    return f"when {FIELDS[c['field']].lower()} {TESTS[c['test']]}" + (f' "{c["value"]}"' if c["test"] not in ("empty", "not_empty") else "") + f", {OUTCOMES[c['outcome']]}"


STAGE_FOR = {"fix": "needs_contact", "skip": "disqualified", "review": "paused"}


def partner_pipeline():
    """The pipeline where agencies are the customer (white label), if there is one."""
    return next((k for k, p in db.products().items() if p.get("partner")), None)


def apply(lead, hit):
    """Act on a rule that fired: move the lead, drop drafts waiting for it, and say why in its timeline."""
    if hit["outcome"] == "partner":
        return to_partner(lead, hit)
    db.update_lead(lead["id"], stage=STAGE_FOR[hit["outcome"]], next_action=None, next_action_at=None)
    db.ex("UPDATE approvals SET state='rejected', decided_at=? WHERE lead_id=? AND state='pending' AND action NOT IN ('reply','followup')", (db.now(), lead["id"]))
    db.log(lead["id"], "blocked", f"Rule: {hit['name']}. {hit['reason']}", channel="agent", status="blocked", payload={"rule": hit["rule"], "outcome": hit["outcome"]})


def to_partner(lead, hit):
    """A service provider is not a customer: move it to the partner pipeline and drop any customer email waiting for it.
    One that never got an email starts fresh there, with the partnership pitch. One that already got a customer email is
    paused, so a person decides whether a second, different approach is welcome."""
    target = partner_pipeline()
    try:
        extra = json.loads(lead.get("extra") or "{}") or {}
    except ValueError:
        extra = {}
    extra["moved_from"] = lead.get("product")
    fresh = not lead.get("emails_sent")
    fields = {"product": target, "segment": "Agency (possible partner)", "extra": json.dumps(extra, ensure_ascii=False), "next_action": None, "next_action_at": None,
              "stage": "new" if fresh else "paused"}
    if fresh:
        fields.update(step=0, score=None, research=None)
    db.update_lead(lead["id"], **fields)
    db.ex("UPDATE approvals SET state='rejected', decided_at=? WHERE lead_id=? AND state='pending' AND action NOT IN ('reply','followup')", (db.now(), lead["id"]))
    db.log(lead["id"], "blocked", f"Rule: {hit['name']}. {hit['reason']} Moved to the partner pipeline"
           + ("." if fresh else ", and paused because it already received a customer email: start outreach by hand if you want to approach it as a partner."),
           channel="agent", status="blocked", payload={"rule": hit["rule"], "outcome": "partner"})


def preview(apply_now=False):
    """How many of the leads the agent is working on break a rule right now (without any lookups over the network)."""
    st, counts, examples, moved = state(), {}, {}, 0
    for lead in db.q("SELECT * FROM leads WHERE stage IN ('new','in_sequence','awaiting_approval')"):
        hit = check(lead, network=False, rules=st)
        if hit:
            counts[hit["name"]] = counts.get(hit["name"], 0) + 1
            examples.setdefault(hit["name"], [])
            if len(examples[hit["name"]]) < 3:
                examples[hit["name"]].append(f"{lead['company']}" + (f" ({lead['email']})" if lead.get("email") else ""))
            if apply_now:
                apply(lead, hit); moved += 1
    return {"rules": [{"name": k, "leads": v, "examples": examples[k]} for k, v in sorted(counts.items(), key=lambda x: -x[1])], "total": sum(counts.values()), "moved": moved}
