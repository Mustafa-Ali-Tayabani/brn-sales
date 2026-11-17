"""Apollo as a lead source for a pipeline: find companies that match its filters and the right person at each, spot
the ones hiring for what we sell, score the fit from company facts, and reveal a person's email only when the lead is
worth a credit.

A lead found here starts with a company, a first name and a job title (that is all Apollo's free people search gives).
The full name, the email and the company's facts arrive when the person is revealed, which costs a credit and so only
happens for a lead that passes the rules and would be grade B or better."""
import json
import re

from . import agent, countries, db, quality, rules
from .providers import apollo, enrich

# Apollo industries that mean "sells services like ours": a competitor or a partner, not a customer
AGENCY_INDUSTRIES = ("information technology & services", "it services", "it services and it consulting", "computer software development", "software development",
                     "marketing & advertising", "marketing and advertising", "advertising services", "design", "design services", "graphic design",
                     "web design", "internet marketing", "outsourcing/offshoring", "outsourcing and offshoring consulting", "staffing & recruiting")
BLANK = {"industries": [], "employee_ranges": [], "countries": [], "technologies": [], "keywords": [], "job_titles": [], "hiring_keywords": [],
         "exclude_industries": [], "daily_limit": 25, "reveal_limit": 10}


def cfg(p):
    """A pipeline's Apollo search settings, with every field present."""
    c = {**BLANK, **((p.get("sources") or {}).get("apollo") or {})}
    for k in ("industries", "employee_ranges", "countries", "technologies", "keywords", "job_titles", "hiring_keywords", "exclude_industries"):
        v = c.get(k)
        c[k] = [x.strip() for x in (re.split(r"[\n;]", v) if isinstance(v, str) else v or []) if str(x).strip()]
    c["countries"] = [x for x in (countries.code(v) for v in c["countries"]) if x]
    c["employee_ranges"] = [r.replace("-", ",").replace(" ", "") for r in c["employee_ranges"]]
    for k, d in (("daily_limit", 25), ("reveal_limit", 10)):
        try:
            c[k] = max(0, min(int(c.get(k) if c.get(k) not in (None, "") else d), 100))
        except (TypeError, ValueError):
            c[k] = d
    return c


def titles(p):
    """Job titles to look for, most wanted first: the pipeline's own titles (what you typed in its editor), then the
    titles that came with its Apollo search, then the global list."""
    own = [t.strip() for t in re.split(r"[,\n]", p.get("titles") or "") if t.strip()] + cfg(p)["job_titles"]
    rest = [t.strip() for t in re.split(r"[,\n]", db.setting("dm_titles") or quality.DEFAULT_TITLES) if t.strip()]
    out = []
    for t in own + rest:
        if t.lower() not in [o.lower() for o in out]:
            out.append(t)
    return out


def _x(lead):
    try:
        return json.loads(lead.get("extra") or "{}") or {} if isinstance(lead.get("extra"), (str, type(None))) else (lead.get("extra") or {})
    except ValueError:
        return {}


def _known():
    """Web addresses and company names already in the list or on the do-not-contact list, so nothing is added twice."""
    domains, names = set(), set()
    for l in db.q("SELECT company, website, email FROM leads"):
        for v in (enrich_domain(l["website"]), (l["email"] or "").split("@")[-1].lower() or None):
            if v:
                domains.add(v)
        if l["company"]:
            names.add(l["company"].strip().lower())
    for s in db.q("SELECT value FROM suppression"):
        domains.add(s["value"].split("@")[-1].lower())
    return domains, names


def enrich_domain(url):
    return apollo.enrich_domain(url)


def agency(industry):
    """Is this Apollo industry one that sells services like ours?"""
    i = (industry or "").strip().lower()
    return bool(i) and (i in AGENCY_INDUSTRIES or any(w in i for w in ("it services", "software development", "web design", "advertising", "marketing")))


def _row(org, person, c, key, hiring):
    one = lambda xs: xs[0] if len(xs) == 1 else None
    industry = org.get("industry") or one(c["industries"])
    facts = {"org_id": org.get("id"), "person_id": person and person["id"], "has_email": bool(person and person["has_email"]), "title_rank": person["rank"] if person else None,
             "employees": org.get("employees"), "industry": org.get("industry"), "technologies": org.get("technologies") or [], "funding": org.get("funding"),
             "searched": {k: c[k] for k in ("industries", "employee_ranges", "countries", "technologies") if c[k]}}
    extra = {"apollo": facts}
    if hiring is not None:
        extra["intent"] = {"level": "hiring", "quote": hiring.get("title") or ("An open role matching: " + ", ".join(c["hiring_keywords"][:3])), "url": hiring.get("url"),
                           "posted": hiring.get("posted"), "where": "Apollo job postings"}
    if org.get("industry"):   # Apollo knows what the company does: that settles the competitor question, the website guess is not needed
        extra["vendor"] = f"Apollo lists it under {org['industry']}" if agency(org["industry"]) else False
    return {"company": org.get("name"), "website": org.get("website"), "phone": org.get("phone"), "industry": industry, "city": org.get("city"),
            "country": countries.code(org.get("country")) or one(c["countries"]), "contact_name": person and person["first_name"], "title": person and person["title"],
            "source": "apollo", "product": key, "segment": "Hiring for it" if hiring is not None else None, "consent_basis": "legitimate_interest_b2b",
            "extra": {**extra, **({"linkedin_company": org["linkedin"]} if org.get("linkedin") else {})}}


def discover(key, p, limit=None, preview=False):
    """Find new leads for one pipeline in Apollo. Returns {"rows", "notes", "how", "credits"}; nothing is saved here.
    Costs 1 credit for the company search, 1 more when the pipeline has hiring words, and (not in a preview) 1 per
    hiring company for the job ad that proves it, at most 5 a run. Finding the people is free."""
    c, before = cfg(p), apollo.used()
    out = {"rows": [], "notes": [], "how": "companies"}
    if not apollo.has_filters(c):
        out["notes"].append("This pipeline has no Apollo search set up yet: add industries, size or countries under Pipelines > Edit > Apollo search.")
        return {**out, "credits": 0}
    want, limit = titles(p), max(1, min(int(limit or c["daily_limit"] or 25), 100))
    domains, names = _known()
    skip_ind = [i.lower() for i in c["exclude_industries"]]
    try:
        orgs = apollo.companies(c, limit=min(100, limit * 3), pipeline=key)
        hiring = {}
        if c["hiring_keywords"]:
            try:
                hiring = {o["id"]: {} for o in apollo.companies(c, limit=100, hiring=True, pipeline=key)}
            except RuntimeError as e:
                out["notes"].append(f"Could not check which companies are hiring: {e}")
        orgs.sort(key=lambda o: o["id"] not in hiring)   # the ones hiring for it first
        seen = set()
        for o in orgs:
            if len(out["rows"]) >= limit:
                break
            d, n = o.get("domain"), (o.get("name") or "").strip().lower()
            if (d and (d in domains or d in seen)) or n in names or (o.get("industry") or "").lower() in skip_ind:
                continue
            if not p.get("sells_to_tech") and agency(o.get("industry")) and rules.state()["builtin"]["vendor"]["on"] and not rules.partner_pipeline():
                continue   # an agency, and there is no partner pipeline to put it in
            seen.add(d or n)
            try:
                found = apollo.people_at(o, want)
            except apollo.NoPlan as e:
                out["notes"].append(f"Apollo would not search for people ({e}). The API key must be a master key.")
                found = []
            out["rows"].append(_row(o, found[0] if found else None, c, key, hiring.get(o["id"]) if o["id"] in hiring else None))
        if not preview:   # the job ad itself, as evidence: one credit per hiring company, a few per run
            for r in [r for r in out["rows"] if r["extra"].get("intent")][:5]:
                try:
                    ads = apollo.job_ads(r["extra"]["apollo"]["org_id"], c["hiring_keywords"], pipeline=key)
                except RuntimeError:
                    break
                if ads:
                    r["extra"]["intent"].update(quote=ads[0]["title"], url=ads[0]["url"], posted=ads[0]["posted"])
    except apollo.NoPlan:
        # a free Apollo plan has no company search: look for the people directly (free), and learn the company's web address when one is revealed
        out["how"] = "people"
        out["notes"].append("Your Apollo plan has no company search, so people were searched for directly. Company web addresses arrive when a person's email is revealed.")
        found = apollo.people(c, want, limit=min(100, limit * 3))
        hiring_names = set()
        if c["hiring_keywords"]:
            try:
                hiring_names = {f["company"].lower() for f in apollo.people(c, want, limit=100, hiring=True)}
            except RuntimeError:
                pass
        best = {}
        for f in sorted(found, key=lambda f: (f["company"].lower() not in hiring_names, f["rank"], not f["has_email"])):
            best.setdefault(f["company"].strip().lower(), f)
        for n, f in best.items():
            if len(out["rows"]) >= limit:
                break
            if n in names or db.one("SELECT 1 x FROM leads WHERE extra LIKE ?", (f'%"person_id": "{f["id"]}"%',)):
                continue
            out["rows"].append(_row({"id": f.get("org_id"), "name": f["company"]}, f, c, key, {} if n in hiring_names else None))
    return {**out, "credits": apollo.used() - before}


# ---------- fit score from company facts ----------
def _in_range(n, ranges):
    for r in ranges:
        lo, _, hi = r.partition(",")
        try:
            if int(lo or 0) <= int(n) <= int(hi or 10 ** 9):
                return True
        except ValueError:
            continue
    return False


def fit(lead, p=None):
    """(score 0-100, [reasons]) for a lead that came from Apollo, from what is known about the company and the person.
    Facts Apollo returned count in full; a fact only implied by the search that found the lead counts a little less."""
    x, p = _x(lead), p or db.products().get(lead.get("product") or "", {})
    a, c = x.get("apollo") or {}, cfg(p)
    searched = a.get("searched") or {}
    score, why = 30, []
    ind = (a.get("industry") or "").lower()
    if ind and ind in [i.lower() for i in c["exclude_industries"]]:
        return 20, [f"{a['industry']} is an industry this pipeline leaves out"]
    if ind and c["industries"]:
        hit = ind in [i.lower() for i in c["industries"]]
        score += 20 if hit else -15
        why.append(f"{a['industry']}: {'an industry you sell to' if hit else 'not one of your industries'}")
    elif searched.get("industries"):
        score += 14; why.append("found in a search of your industries")
    if a.get("employees") and c["employee_ranges"]:
        hit = _in_range(a["employees"], c["employee_ranges"])
        score += 15 if hit else -10
        why.append(f"{a['employees']} staff: {'the size you sell to' if hit else 'outside your size range'}")
    elif searched.get("employee_ranges"):
        score += 9; why.append("found in a search of your company sizes")
    where = countries.code(lead.get("country"))
    if where and c["countries"]:
        hit = where in c["countries"]
        score += 10 if hit else -10
        why.append(f"{countries.name(where)}: {'a country you sell in' if hit else 'not one of your countries'}")
    tech = [t for t in c["technologies"] if t.lower() in [str(v).lower() for v in a.get("technologies") or []]]
    if tech:
        score += 10; why.append("uses " + ", ".join(tech[:2]))
    elif searched.get("technologies"):
        score += 5; why.append("found in a search for the technology you named")
    if agent.signal(lead) == "hiring":
        score += 15; why.append("hiring for it")
    if a.get("funding"):
        score += 4; why.append("recently funded")
    rank = a.get("title_rank")
    if rank is not None and (lead.get("contact_name") or "").strip():
        pts = 12 if rank == 0 else 9 if rank < 3 else 5
        score += pts; why.append(f"{lead.get('title') or 'a contact'}: {'the person you most want' if rank == 0 else 'a suitable person'}")
    else:
        why.append("nobody with a suitable job title found")
    if a.get("has_email") or lead.get("email"):
        score += 4
    return max(0, min(100, score)), why


# ---------- the email, when the lead is worth a credit ----------
def due(lead):
    """Does this lead still wait for its person to be revealed?"""
    a = _x(lead).get("apollo") or {}
    return bool(a.get("person_id")) and not lead.get("email") and not a.get("revealed")


def reveal(lead):
    """Spend a credit on a lead's email if it has earned one. Returns (lead, None) when research can go on, or
    (lead, "wait"/"stop") when it cannot: wait = caps reached, try tomorrow; stop = stays under Needs contact."""
    x = _x(lead)
    a, key = x["apollo"], lead.get("product") or ""
    p = db.products().get(key, {})
    score, why = fit(lead, p)
    db.update_lead(lead["id"], score=score)
    letter, _ = quality.grade({**lead, "score": score, "email": "person@their-company.example", "extra": json.dumps(x)})   # the grade it would have with the email
    say = lambda text: db.log(lead["id"], "enrich", text, channel="system", payload={"score": score, "why": why})
    if not a.get("has_email"):
        say("Apollo has no email for this person, so no credit was spent. The lead waits under Needs contact.")
        return lead, "stop"
    if letter == "C":
        say(f"Email not revealed: with a fit score of {score} and no sign of need, the lead would be grade C even with the email. No credit was spent.")
        return lead, "stop"
    limit = cfg(p)["reveal_limit"]
    if (limit and apollo.used(pipeline=key) >= limit) or not apollo.room(1):
        return lead, "wait"
    try:
        r = apollo.reveal(a["person_id"], pipeline=key)
    except apollo.CapReached:
        return lead, "wait"
    except RuntimeError as e:
        say(f"The email could not be revealed: {e}")
        return lead, "wait"
    a["revealed"] = db.now()[:10]
    if not r:
        say("Apollo no longer has this person. The lead waits under Needs contact.")
        db.update_lead(lead["id"], extra=json.dumps(x, ensure_ascii=False))
        return lead, "stop"
    org = r.get("org") or {}
    a.update({k: v for k, v in {"industry": org.get("industry"), "employees": org.get("employees"), "technologies": org.get("technologies"), "funding": org.get("funding"),
                                "org_id": org.get("id") or a.get("org_id")}.items() if v})
    x.update({k: v for k, v in {"email_status": r["email_status"], "linkedin": r.get("linkedin"), "contact_via": "Apollo"}.items() if v})
    if org.get("industry") and not p.get("sells_to_tech"):
        x["vendor"] = f"Apollo lists it under {org['industry']}" if agency(org["industry"]) else False
    fields = {"contact_name": r.get("contact_name") or lead.get("contact_name"), "title": r.get("title") or lead.get("title"), "extra": json.dumps(x, ensure_ascii=False),
              "industry": org.get("industry") or lead.get("industry"), "website": lead.get("website") or org.get("website"), "city": lead.get("city") or r.get("city"),
              "country": lead.get("country") or countries.code(r.get("country")), "phone": lead.get("phone") or org.get("phone")}
    email = r.get("email")
    if email and db.one("SELECT 1 x FROM leads WHERE lower(email)=? AND id<>?", (email, lead["id"])):
        email = None
    if email:
        fields["email"] = email
    db.update_lead(lead["id"], **{k: v for k, v in fields.items() if v is not None})
    lead = db.one("SELECT * FROM leads WHERE id=?", (lead["id"],))
    score, why = fit(lead, p)
    db.update_lead(lead["id"], score=score)
    lead["score"] = score
    if not email:
        say(f"Revealed {fields['contact_name']}, but Apollo's email for them is \"{r['email_status']}\", not verified, so it is not used. The lead waits under Needs contact.")
        return lead, "stop"
    say(f"Revealed {fields['contact_name']}" + (f", {fields['title']}" if fields.get("title") else "") + f" ({email}, verified by Apollo). 1 credit.")
    return lead, None
