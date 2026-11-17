"""Lead quality: a grade for every lead, named contacts, a clean-up of the existing list, and learning from
"good lead" / "bad lead" marks.

Grade: A, B or C from three things anyone can see on the lead.
  need     asked for it or wrote to us (3) · hiring for it, or shows the problem (2) · nothing (0)
  contact  a named person with their own address (2) · some address or phone (1) · none (0)
  fit      score 75+ (2) · above the contact threshold (1) · below (0)
5 or more is an A, 3 or 4 a B, less a C. A competitor or a lead you marked bad is always a C; one you marked good gets 2 extra."""
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import agent, db, rules
from .providers import enrich, sources

LOCK = threading.Lock()
BAD_REASONS = {"competitor": "A competitor or agency", "too_small": "Too small to buy", "wrong_country": "Wrong country or market",
               "wrong_business": "Wrong kind of business", "not_real": "Not a real business", "other": "Something else"}
DEFAULT_TITLES = "owner, founder, ceo, managing director, general manager, director, head"


def _extra(lead):
    x = lead.get("extra")
    if isinstance(x, dict):
        return x
    try:
        return json.loads(x or "{}") or {}
    except ValueError:
        return {}


def shared_mailbox(email):
    local = (email or "").split("@")[0].lower()
    return bool(local) and (local in rules.ROLE or any(local.startswith(n) for n in rules.NOREPLY))


def grade(lead):
    """(letter, [reasons]) for a lead row."""
    x, sig = _extra(lead), agent.signal(lead)
    fb = (x.get("feedback") or {}).get("verdict")
    pts, why = 0, []
    if sig in ("asked", "inbound"):
        pts += 3; why.append(agent.SIGNALS[sig])
    elif sig in ("hiring", "problem"):
        pts += 2; why.append(agent.SIGNALS[sig])
    else:
        why.append("No sign they need it now")
    email = lead.get("email") if not x.get("email_bounced") else None
    if email and (lead.get("contact_name") or "").strip() and not shared_mailbox(email):
        pts += 2; why.append("Named contact")
    elif email or lead.get("phone"):
        pts += 1; why.append("Shared mailbox only" if email and shared_mailbox(email) else "No named contact" if not (lead.get("contact_name") or "").strip() else "Reachable")
    else:
        why.append("No way to reach them")
    score = lead.get("score")
    if score is None:
        pts += 1
    elif score >= 75:
        pts += 2; why.append("Strong fit")
    elif score >= int(db.setting("min_score") or 55):
        pts += 1; why.append("Fits")
    else:
        why.append("Weak fit")
    if fb == "good":
        pts += 2; why.append("You marked it good")
    if x.get("vendor") and sig not in ("asked", "inbound"):
        return "C", ["Looks like a competitor"] + why
    if fb == "bad":
        return "C", ["You marked it bad"] + why
    return ("A" if pts >= 5 else "B" if pts >= 3 else "C"), why


def regrade(lead_id):
    lead = db.one("SELECT * FROM leads WHERE id=?", (lead_id,))
    if lead:
        g = grade(lead)[0]
        if g != lead.get("grade"):
            db.ex("UPDATE leads SET grade=? WHERE id=?", (g, lead_id))
        return g


def backfill():
    """Grade every lead that has none yet (runs once after an upgrade, in the background)."""
    for r in db.q("SELECT id FROM leads WHERE grade IS NULL"):
        regrade(r["id"])


# ---------- a named person ----------
def titles_for(lead):
    p = db.products().get(lead.get("product") or "", {})
    return [t for t in re.split(r"[,\n]", (p.get("titles") or "") + "," + (db.setting("dm_titles") or DEFAULT_TITLES))]


def domain_of(lead):
    site = re.sub(r"^(https?://)?(www\.)?", "", (lead.get("website") or "").lower()).split("/")[0]
    if site and "." in site and not any(h in site for h in ("reddit.com", "facebook.com", "linkedin.com", "instagram.com", "google.com", "example.com")):
        return site
    mail = (lead.get("email") or "").split("@")[-1].lower()
    return mail if mail and mail not in rules.FREE else None


def needs_person(lead):
    return not (lead.get("contact_name") or "").strip() or shared_mailbox(lead.get("email")) or not lead.get("email")


def lookups_today():
    return db.one("SELECT COUNT(*) n FROM activities WHERE kind='enrich' AND created_at>=?", (db.now()[:10],))["n"]


def find_person(lead, manual=False):
    """Look for a named decision-maker at the lead's company and put them on the lead. The shared address it had is kept.
    Returns {"found": bool, "detail": str}."""
    if not enrich.people_finder():
        return {"found": False, "detail": "No people finder is connected. Add APOLLO_API_KEY or HUNTER_API_KEY to the server's variables."}
    domain = domain_of(lead)
    if not domain:
        return {"found": False, "detail": "This lead has no company website or business email to search from."}
    cap = int(db.setting("dm_daily_cap") or 20)
    if not manual and lookups_today() >= cap:
        return {"found": False, "detail": f"Today's limit of {cap} lookups is reached."}
    try:
        p = enrich.find_decision_maker(domain, titles_for(lead))
    except RuntimeError as e:
        return {"found": False, "detail": str(e)}
    x = _extra(lead)
    x["person_lookup"] = db.now()[:10]
    if not p or p["email"] == (lead.get("email") or "").lower():
        db.log(lead["id"], "enrich", f"Looked for a named contact at {domain}: nobody suitable found", channel="system", payload={"domain": domain})
        db.update_lead(lead["id"], extra=json.dumps(x, ensure_ascii=False))
        return {"found": False, "detail": f"Nobody suitable was found at {domain}."}
    if db.one("SELECT 1 x FROM leads WHERE lower(email)=? AND id<>?", (p["email"], lead["id"])):
        db.update_lead(lead["id"], extra=json.dumps(x, ensure_ascii=False))
        return {"found": False, "detail": f"{p['contact_name']} at {domain} is already a lead."}
    if lead.get("email"):
        x["generic_email"] = lead["email"]
    x.update({k: v for k, v in {"email_status": p.get("email_status"), "linkedin": p.get("linkedin") or x.get("linkedin")}.items() if v})
    x.pop("email_bounced", None); x.pop("email_check", None)
    fields = {"contact_name": p["contact_name"], "title": p.get("title") or lead.get("title"), "email": p["email"], "extra": json.dumps(x, ensure_ascii=False)}
    if lead.get("stage") == "needs_contact":
        fields["stage"] = "new"
    db.update_lead(lead["id"], **fields)
    db.log(lead["id"], "enrich", f"Found a named contact: {p['contact_name']}" + (f", {p['title']}" if p.get("title") else "") + f" ({p['email']}), via {p['via']}",
           channel="system", payload={"domain": domain, "via": p["via"], "previous_email": lead.get("email")})
    return {"found": True, "detail": f"{p['contact_name']}" + (f", {p['title']}" if p.get("title") else "") + f" · {p['email']}", **p}


# ---------- clean-up of the existing list ----------
SCOPE = ("new", "needs_contact", "in_sequence", "awaiting_approval", "completed", "nurture", "paused")
STAGE_RANK = {s: i for i, s in enumerate(("awaiting_approval", "in_sequence", "completed", "paused", "nurture", "new", "needs_contact"))}
CLEAN = {"running": False, "checked": 0, "total": 0, "proposals": [], "started": None, "finished": None, "applied": None, "cancel": False}


def _host(lead):
    return re.sub(r"^(https?://)?(www\.)?", "", (lead.get("website") or "").lower()).split("/")[0].strip()


def _scan():
    try:
        leads = db.q(f"SELECT * FROM leads WHERE stage IN ({','.join('?' * len(SCOPE))})", SCOPE)
        CLEAN.update(total=len(leads), checked=0, proposals=[])
        st = rules.state()

        def one(lead):
            if CLEAN["cancel"]:
                return None
            lead = agent.check_site(lead)                      # reads the home page once, if not read before
            hit = rules.check(lead, network=True, rules=st, verify=False)
            with LOCK:
                CLEAN["checked"] += 1
            return {"id": lead["id"], "company": lead["company"], "email": lead.get("email"), "kind": "rule", "reason": hit["name"], "detail": hit["reason"], "hit": hit} if hit else None

        with ThreadPoolExecutor(8) as pool:
            found = [x for x in pool.map(one, leads) if x]
        flagged = {x["id"] for x in found}
        groups = {}
        for lead in leads:   # the same company entered more than once
            h = _host(lead)
            if h and lead["id"] not in flagged and not any(s in h for s in ("reddit.com", "facebook.com", "linkedin.com", "instagram.com")):
                groups.setdefault(h, []).append(lead)
        for h, group in groups.items():
            if len(group) < 2:
                continue
            group.sort(key=lambda l: (STAGE_RANK.get(l["stage"], 9), -(l.get("emails_sent") or 0), not (l.get("contact_name") or "").strip(), l["id"]))
            keep = group[0]
            for dup in group[1:]:
                same_person = (dup.get("email") or "").lower() == (keep.get("email") or "").lower() or not dup.get("email") or shared_mailbox(dup.get("email"))
                if same_person:
                    found.append({"id": dup["id"], "company": dup["company"], "email": dup.get("email"), "kind": "duplicate", "reason": "Entered more than once",
                                  "detail": f"The same company as {keep['company']} (#{keep['id']}), which is kept.", "keep": keep["id"]})
        with LOCK:   # results and "finished" appear together, so nobody sees a finished check with no results
            CLEAN.update(proposals=found, running=False, finished=db.now())
    finally:
        if CLEAN["running"]:
            CLEAN.update(running=False, finished=db.now())


def cleanup_start():
    with LOCK:
        if CLEAN["running"]:
            return False
        CLEAN.update(running=True, cancel=False, started=db.now(), finished=None, applied=None, proposals=[], checked=0, total=0)
    threading.Thread(target=_scan, daemon=True).start()
    return True


def cleanup_state():
    with LOCK:
        snap = dict(CLEAN, proposals=list(CLEAN["proposals"]))
    groups = {}
    for p in snap["proposals"]:
        g = groups.setdefault(p["reason"], {"reason": p["reason"], "leads": 0, "examples": []})
        g["leads"] += 1
        if len(g["examples"]) < 3:
            g["examples"].append(p["company"] + (f" ({p['email']})" if p.get("email") else ""))
    grades = {r["grade"] or "?": r["n"] for r in db.q("SELECT grade, COUNT(*) n FROM leads WHERE stage NOT IN ('disqualified','opted_out','lost') GROUP BY grade")}
    return {**{k: snap[k] for k in ("running", "checked", "total", "started", "finished", "applied")}, "found": len(snap["proposals"]),
            "groups": sorted(groups.values(), key=lambda g: -g["leads"]), "grades": grades}


def cleanup_apply():
    """Act on what the scan found: rule hits are handled the way the rule says, duplicates are set aside. Nothing is deleted."""
    done = 0
    for p in CLEAN["proposals"]:
        lead = db.one("SELECT * FROM leads WHERE id=?", (p["id"],))
        if not lead or lead["stage"] not in SCOPE:
            continue
        if p["kind"] == "rule":
            rules.apply(lead, p["hit"])
        else:
            db.update_lead(lead["id"], stage="disqualified", next_action=None, next_action_at=None)
            db.ex("UPDATE approvals SET state='rejected', decided_at=? WHERE lead_id=? AND state='pending'", (db.now(), lead["id"]))
            db.log(lead["id"], "blocked", f"Clean-up: {p['detail']}", channel="agent", status="blocked", payload={"duplicate_of": p.get("keep")})
        done += 1
    CLEAN.update(proposals=[], applied=done)
    return done


# ---------- learning from good / bad marks ----------
COMMON = {"company", "group", "international", "global", "services", "service", "limited", "trading", "center", "centre", "official", "online", "saudi", "arabia", "kuwait"}


def set_feedback(lead, verdict, reason=None, note=None):
    x = _extra(lead)
    who = (db.actor.get() or {}).get("name")
    if verdict in ("good", "bad"):
        x["feedback"] = {"verdict": verdict, "reason": reason if reason in BAD_REASONS else None, "note": (note or "")[:300] or None, "at": db.now()[:10], "by": who}
    else:
        x.pop("feedback", None)
    fields = {"extra": json.dumps(x, ensure_ascii=False)}
    if verdict == "bad" and lead["stage"] not in ("won", "lost", "opted_out", "disqualified"):
        fields.update(stage="disqualified", next_action=None, next_action_at=None)
        db.ex("UPDATE approvals SET state='rejected', decided_at=? WHERE lead_id=? AND state='pending'", (db.now(), lead["id"]))
    db.update_lead(lead["id"], **fields)
    db.log(lead["id"], "system", {"good": "Marked as a good lead", "bad": f"Marked as a bad lead: {BAD_REASONS.get(reason, 'no reason given')}"}.get(verdict, "Good/bad mark removed")
           + (f" ({note[:120]})" if note else ""), channel="system")


def suggestions():
    """Rules worth adding, drawn from the leads marked bad. Each says what it is based on."""
    bad = [(l, _extra(l)["feedback"]) for l in db.q("SELECT * FROM leads WHERE extra LIKE ?", ('%"verdict": "bad"%',))]
    st, out = rules.state(), []
    blocked = set(rules._words(st["builtin"]["countries"]["value"]))
    by_country = {}
    for l, f in bad:
        if f.get("reason") == "wrong_country" and l.get("country"):
            by_country.setdefault(l["country"].upper(), []).append(l["company"])
    for c, names in by_country.items():
        if len(names) >= 2 and c.lower() not in blocked:
            out.append({"kind": "country", "value": c, "text": f"Never contact leads in {c}", "why": f"You marked {len(names)} leads there as the wrong market: {', '.join(names[:3])}."})
    words, have = {}, set(rules._words(st["builtin"]["keywords"]["value"]))
    for l, f in bad:
        if f.get("reason") in ("competitor", "wrong_business", "not_real"):
            for w in set(re.findall(r"[a-z]{5,}", f"{l.get('company') or ''} {_extra(l).get('site_title') or ''}".lower())) - COMMON:
                words.setdefault(w, []).append(l["company"])
    for w, names in sorted(words.items(), key=lambda x: -len(x[1])):
        if len(names) >= 3 and w not in have and len([o for o in out if o["kind"] == "keyword"]) < 4:
            out.append({"kind": "keyword", "value": w, "text": f'Skip leads containing "{w}"', "why": f"{len(names)} leads you marked bad have it: {', '.join(names[:3])}."})
    cats = {}
    for l, f in bad:
        if f.get("reason") in ("too_small", "wrong_business") and (l.get("segment") or l.get("industry")):
            cats.setdefault((l.get("segment") or l.get("industry")).strip(), []).append(l["company"])
    for c, names in cats.items():
        if len(names) >= 3 and not any(r["field"] == "segment" and r["value"].lower() == c.lower() for r in st["custom"]):
            out.append({"kind": "category", "value": c, "text": f'When category is exactly "{c}", skip the lead', "why": f"You marked {len(names)} leads in this category bad: {', '.join(names[:3])}."})
    searches = {}
    for l in db.q("SELECT extra FROM leads WHERE extra LIKE ?", ('%"found_by"%',)):
        x = _extra(l)
        s = searches.setdefault(x.get("found_by"), [0, 0])
        s[0] += 1; s[1] += (x.get("feedback") or {}).get("verdict") == "bad"
    for q, (n, b) in searches.items():
        if b >= 3 and b / n >= 0.5:
            out.append({"kind": "search", "value": q, "text": f'Remove or reword the search "{q}"', "why": f"{b} of the {n} leads it found were marked bad. Change it under Settings > Pipelines."})
    return {"suggestions": out, "marked_bad": len(bad), "marked_good": db.one("SELECT COUNT(*) n FROM leads WHERE extra LIKE ?", ('%"verdict": "good"%',))["n"]}
