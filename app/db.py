"""Storage. SQLite by default (one file, nothing to set up). Set DATABASE_URL to a postgres:// address to use
Postgres instead: the same SQL is translated on the way through, and the first start on an empty Postgres copies
everything over from the SQLite file, so switching loses nothing."""
import json
import os
import contextvars
import re
import sqlite3
import threading
from datetime import datetime, timezone

DB_PATH = os.environ.get("BRN_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "pipeline.db"))
DATABASE_URL = os.environ.get("DATABASE_URL") or ""
PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
_lock = threading.RLock()
actor = contextvars.ContextVar("brn_actor", default=None)  # the signed-in person behind the current request, if any

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  company TEXT NOT NULL,
  contact_name TEXT,
  title TEXT,
  email TEXT,
  phone TEXT,
  website TEXT,
  city TEXT,
  country TEXT,            -- ISO code: SA, KW, AE, ...
  industry TEXT,
  source TEXT,             -- csv, google_places, apollo, manual, inbound
  consent_basis TEXT,      -- legitimate_interest_b2b, opt_in, existing_customer, none
  stage TEXT NOT NULL DEFAULT 'new',
  score INTEGER,
  research TEXT,           -- JSON blob produced by the research agent
  next_action TEXT,
  next_action_at TEXT,
  step INTEGER NOT NULL DEFAULT 0,
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS activities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lead_id INTEGER,
  kind TEXT NOT NULL,      -- research, score, email_draft, email_sent, email_reply, call_scheduled, call_completed, stage_change, blocked, approval, system
  channel TEXT,            -- email, phone, agent, system
  status TEXT,             -- ok, dry_run, pending_approval, blocked, error
  summary TEXT,
  payload TEXT,            -- JSON
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lead_id INTEGER NOT NULL,
  channel TEXT NOT NULL,
  action TEXT NOT NULL,
  content TEXT NOT NULL,   -- JSON (subject/body or call script)
  state TEXT NOT NULL DEFAULT 'pending',  -- pending, approved, rejected, sent
  created_at TEXT NOT NULL,
  decided_at TEXT
);
CREATE TABLE IF NOT EXISTS suppression (
  value TEXT PRIMARY KEY,  -- email, phone or domain
  reason TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_usage (
    day TEXT NOT NULL, model TEXT NOT NULL, task TEXT NOT NULL DEFAULT '', calls INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0, cost REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, model, task)
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inbox_seen (
  message_id TEXT PRIMARY KEY,  -- Message-ID of every mailbox email already recorded, so nothing is logged twice
  lead_id INTEGER,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE,
  role TEXT NOT NULL,          -- admin, manager, sales, viewer
  password TEXT NOT NULL,      -- salted hash, never the password itself
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  last_login_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY,      -- hash of the browser's sign-in cookie
  user_id INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meetings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lead_id INTEGER,
  title TEXT NOT NULL,
  starts_at TEXT NOT NULL,     -- UTC
  ends_at TEXT NOT NULL,
  timezone TEXT,
  attendees TEXT,              -- JSON list of {email, response}
  description TEXT,
  location TEXT,
  meet_link TEXT,
  google_event_id TEXT,
  google_link TEXT,
  status TEXT NOT NULL DEFAULT 'scheduled',  -- scheduled, done, cancelled
  created_by TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_meet_start ON meetings(starts_at);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lead_id INTEGER,
  title TEXT NOT NULL,
  notes TEXT,
  due_at TEXT,                 -- UTC; NULL = no date
  assigned_to INTEGER,         -- users.id; NULL = anyone
  done_at TEXT,
  done_by TEXT,
  notified_at TEXT,            -- reminder email sent
  created_by TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_due ON tasks(due_at);
CREATE TABLE IF NOT EXISTS templates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  subject TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '',
  created_by TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_act_lead ON activities(lead_id);
CREATE INDEX IF NOT EXISTS idx_act_time ON activities(created_at);
CREATE INDEX IF NOT EXISTS idx_lead_next ON leads(next_action_at);
"""

CALL_FIRST = [
    {"day": 0, "channel": "phone", "action": "call"},
    {"day": 3, "channel": "phone", "action": "call"},
    {"day": 7, "channel": "phone", "action": "call"},
]

# Each entry is one pipeline: a product or a service, with its own pitch, ideal customer,
# optional sequence (falls back to the global one) and lead sources the agent searches daily.
#   sources.places: Google Maps searches   sources.only_without_website: keep businesses with no site
#   sources.reddit: phrases people post when they need this (intent leads, answered by hand)
PRODUCTS = [
    {"key": "fixify", "name": "Fixify", "type": "product",
     "pitch": "Fixify by brn dynamics: WordPress maintenance as a service. One fixed monthly plan covers security, backups, speed, SEO, bug fixes, content updates and technical support, so the business has a technical team without hiring one.",
     "icp": "Small businesses whose WordPress or small-business website is slow, insecure or has SEO problems",
     "sources": {"places": [], "reddit": ["wordpress site hacked", "need wordpress developer", "wordpress site slow help"]}},
    {"key": "gym_club", "name": "BRN Gym Club", "type": "product",
     "pitch": "BRN Gym Club: gym and club management platform. Memberships, reservations, access control, POS, inventory, an AI assistant and an Arabic/English member app in one system.",
     "icp": "Gyms, fitness studios and sports clubs in Saudi Arabia and Kuwait with 1 to 20 branches",
     "sources": {"places": [{"query": "gyms in Riyadh", "country": "SA", "limit": 20}, {"query": "fitness studios in Jeddah", "country": "SA", "limit": 20},
                            {"query": "gyms in Kuwait City", "country": "KW", "limit": 20}], "reddit": ["gym management software"]}},
    {"key": "ai_waiter", "name": "AI Waiter", "type": "product",
     "pitch": "AI Waiter: the table QR code becomes an ordering assistant with recommendations, voice ordering, loyalty and payments.",
     "icp": "Restaurants and cafes, single site or small chains",
     "sources": {"places": [{"query": "restaurants in Riyadh", "country": "SA", "limit": 20}], "reddit": ["restaurant qr ordering system"]}},
    {"key": "estimates", "name": "Estimate & Property Case Mgmt", "type": "product",
     "pitch": "Estimate and property case management: service requests, estimates, scheduling, building data and diagnostics in one platform.",
     "icp": "Property management, facilities and construction service companies",
     "sources": {"places": [{"query": "property management companies in Riyadh", "country": "SA", "limit": 20}], "reddit": []}},
    {"key": "qapipeline", "name": "QAPipeline", "type": "product",
     "pitch": "QAPipeline: AI-powered test management. Test cases, automated runs, results analytics and AI-generated test scenarios.",
     "icp": "Software companies and in-house product teams with QA needs",
     "sources": {"places": [], "reddit": ["test management tool recommendation"]}},
    {"key": "rpa_finance", "name": "RPA Finance Automation", "type": "product",
     "pitch": "Finance automation: reads bills from email with OCR, maps them to branches and supports reconciliation. One client saved 412 hours.",
     "icp": "Multi-branch businesses with heavy bill processing in finance teams",
     "sources": {"places": [], "reddit": ["automate invoice processing"]}},
    {"key": "website_build", "name": "New websites (no-site businesses)", "type": "service",
     "pitch": "brn dynamics designs and launches a fast, mobile-friendly business website with Google Maps, WhatsApp and booking built in, at a fixed price, live in about two weeks.",
     "icp": "Local businesses with good reviews but no website",
     "sequence": CALL_FIRST,
     "sources": {"places": [{"query": "salons in Riyadh", "country": "SA", "limit": 20}, {"query": "clinics in Kuwait City", "country": "KW", "limit": 20}],
                 "only_without_website": True, "reddit": ["need a website for my business", "looking for web designer small business"]}},
    {"key": "web_mobile", "name": "Web & mobile apps", "type": "service",
     "pitch": "brn dynamics builds web and mobile apps end to end: product design, development, launch and support. 80+ projects since 2019, 5.0 on Clutch.",
     "icp": "Startups and businesses planning a new app or replacing an old one",
     "sources": {"places": [], "reddit": ["looking for app developer", "need someone to build an app"]}},
    {"key": "ai_automation", "name": "AI & automation", "type": "service",
     "pitch": "brn dynamics builds AI agents and automations that take repetitive work off teams: document processing, customer support, internal assistants.",
     "icp": "Teams with repetitive manual processes",
     "sources": {"places": [], "reddit": ["looking for ai automation developer", "automate business process ai"]}},
    {"key": "crm_erp", "name": "CRM / ERP", "type": "service",
     "pitch": "brn dynamics builds and customises CRM and ERP systems around how a business actually works.",
     "icp": "Growing businesses outgrowing spreadsheets",
     "sources": {"places": [], "reddit": ["custom crm developer"]}},
    {"key": "product_design", "name": "Product design (UI/UX)", "type": "service",
     "pitch": "brn dynamics designs products people enjoy using: research, UX, UI and design systems.",
     "icp": "Startups and product teams", "sources": {"places": [], "reddit": ["need ui ux designer for app"]}},
    {"key": "security", "name": "Security & compliance", "type": "service",
     "pitch": "brn dynamics reviews and hardens apps and infrastructure, and prepares teams for compliance audits.",
     "icp": "Companies handling customer data", "sources": {"places": [], "reddit": []}},
    {"key": "custom", "name": "Custom software & AI", "type": "service",
     "pitch": "brn dynamics builds custom software: product design, SaaS, CRM/ERP, AI and automation, web and mobile apps. 80+ projects since 2019, 5.0 on Clutch.",
     "icp": "Growing businesses that need custom software or AI automation", "sources": {"places": [], "reddit": []}},
]

DEFAULT_SETTINGS = {
    # approval: every email and call waits for a human click, then really goes out. live: sent automatically within caps.
    "mode": "approval",
    "offer": "BRN Gym Club: gym and club management software (memberships, billing, access control, classes, Arabic/English app)",
    "icp": "Gyms, fitness studios and sports clubs in Saudi Arabia and Kuwait with 1 to 20 branches",
    "sender_name": "brn dynamics",
    "sender_email": "sales@brndynamics.com",
    "booking_link": "https://brndynamics.com/contact",
    "logo_url": "",   # logo image in emails; empty = the copy this server hosts at /brand/logo-light.png
    "currency": "USD",            # deal values and the forecast are shown in this currency
    "ab_subjects": "1",           # try two subject lines per outreach step and compare reply rates in Reports
    "email_guidance": "{}",       # how the team wants emails written, and examples they like: see Email coach
    "roles": "{}",                # changes to the built-in roles, and roles you added: see auth.py
    "rules": "{}",                # which leads the agent may work on: see rules.py
    "meet_links": "",             # permanent Google Meet links, one per line; each meeting gets one that is free at that time
    # when the agent runs: always while it is on, or only inside these hours
    "agent_schedule": "0",
    "agent_days": "6,0,1,2,3",    # Python weekdays, Monday = 0: Sunday to Thursday
    "agent_start": "09:00",
    "agent_end": "18:00",
    "agent_timezone": "Asia/Riyadh",
    "booking_page_url": "",       # where your booking page lives on your own website, if it is embedded there
    "meeting_reminder_min": "60", # email a brief to whoever booked a meeting this many minutes before it (0 = off)
    "meeting_followup": "1",      # after a meeting, draft a follow-up email for approval
    # public booking page (/book): which slots a lead may pick
    "booking_enabled": "1",
    "booking_title": "Intro call with brn dynamics",
    "booking_days": "6,0,1,2,3",  # Python weekdays, Monday = 0: Sunday to Thursday
    "booking_start": "10:00",
    "booking_end": "17:00",
    "booking_slot_min": "30",
    "booking_timezone": "Asia/Riyadh",
    "booking_notice_hours": "4",
    "booking_days_ahead": "14",
    "daily_email_cap": "40",
    "daily_call_cap": "20",
    "min_score": "55",
    "products": json.dumps(PRODUCTS),
    "default_product": "custom",
    "company_address": "brn dynamics, Karachi · Riyadh · Kuwait City · Geneva",
    "agent_enabled": "1",
    "discovery_queries": json.dumps([]),
    "last_discovery": "",
    "auto_discovery": "1",
    # Claude usage. economy: Claude only writes emails that really go out (approval/live), reads replies and drafts
    # replies to people asking online; research, dry runs and call briefs use free templates. full: Claude for everything.
    "ai_mode": "economy",
    "ai_model": "claude-haiku-5-5",
    "ai_daily_budget": "0.50",
    "ai_balance": "",
    "ai_balance_at": "",
    "pipeline_runs": "{}",
    "sequence": json.dumps([
        {"day": 0, "channel": "email", "action": "intro"},
        {"day": 3, "channel": "email", "action": "follow_up_1"},
        {"day": 5, "channel": "phone", "action": "call"},
        {"day": 9, "channel": "email", "action": "follow_up_2"},
        {"day": 14, "channel": "email", "action": "breakup"},
    ]),
}


# Columns added after a table first shipped: (table, column, type). Applied on start when missing.
MIGRATIONS = [
    ("leads", "product", "TEXT"), ("leads", "segment", "TEXT"), ("leads", "extra", "TEXT"),
    ("leads", "emails_sent", "INTEGER NOT NULL DEFAULT 0"), ("leads", "last_contacted_at", "TEXT"), ("leads", "replied_at", "TEXT"),
    ("leads", "mail_seen_at", "TEXT"),      # when a human last opened this lead's conversation in the Mailbox
    ("leads", "deal_value", "REAL"),        # what the deal is worth, in the currency set in Settings
    ("leads", "expected_close", "TEXT"),    # date the deal is expected to close
    ("leads", "closed_at", "TEXT"),         # when it was marked won or lost
    ("activities", "actor", "TEXT"),        # who did it, when a person did
    ("meetings", "reminded_at", "TEXT"), ("meetings", "followup_at", "TEXT"),
    ("meetings", "ics_seq", "INTEGER NOT NULL DEFAULT 0"),   # version of the invitation, raised on every change
]
ID_TABLES = ("leads", "activities", "approvals", "users", "meetings", "tasks", "templates")  # tables whose INSERT returns a new id


def products():
    """Saved pipelines, plus any default pipeline added in a newer version (never overwrites edits)."""
    try:
        saved = json.loads(setting("products"))
    except Exception:
        saved = []
    out = {p["key"]: p for p in saved}
    for p in PRODUCTS:
        if p["key"] not in out:
            out[p["key"]] = p
        else:
            for k in ("type", "sources"):
                out[p["key"]].setdefault(k, p.get(k))
    return out


def sequence_for(lead):
    p = products().get((lead or {}).get("product") or "")
    if p and p.get("sequence"):
        return p["sequence"]
    return json.loads(setting("sequence"))


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _pg_sql(sql):
    """The app writes SQLite-flavoured SQL; this is the same statement for Postgres."""
    sql = sql.replace("%", "%%").replace("?", "%s")
    sql = re.sub(r"\s+COLLATE NOCASE", "", sql)
    sql = re.sub(r"\bLIKE\b", "ILIKE", sql)   # SQLite's LIKE ignores case; keep searches working the same way
    if re.match(r"\s*INSERT OR IGNORE", sql, re.I):
        sql = re.sub(r"INSERT OR IGNORE", "INSERT", sql, count=1, flags=re.I) + " ON CONFLICT DO NOTHING"
    return sql


class _Pg:
    """One shared Postgres connection that looks enough like sqlite3's for the helpers below."""
    def __init__(self):
        import psycopg
        from psycopg.rows import dict_row
        self._psycopg, self._row = psycopg, dict_row
        self._c = None

    def _conn(self):
        if self._c is None or self._c.closed:
            self._c = self._psycopg.connect(DATABASE_URL, autocommit=True, row_factory=self._row)
        return self._c

    def run(self, sql, args=(), fetch=False, insert_id=False):
        sql = _pg_sql(sql)
        if insert_id:
            sql += " RETURNING id"
        for attempt in (0, 1):
            try:
                cur = self._conn().execute(sql, tuple(args))
                if insert_id:
                    row = cur.fetchone()
                    return row["id"] if row else None
                return [dict(r) for r in cur.fetchall()] if fetch else None
            except self._psycopg.OperationalError:  # the server dropped the connection: reconnect once
                try:
                    self._c and self._c.close()
                except Exception:
                    pass
                self._c = None
                if attempt:
                    raise


def conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


_conn = None


def _columns(table):
    if PG:
        return {r["column_name"] for r in _conn.run("SELECT column_name FROM information_schema.columns WHERE table_name=?", (table,), fetch=True)}
    return {r[1] for r in _conn.execute(f"PRAGMA table_info({table})")}


def _setup():
    """Create tables, add columns introduced by newer versions, and seed default settings."""
    run = (lambda sql, args=(): _conn.run(sql, args)) if PG else (lambda sql, args=(): _conn.execute(sql, args))
    schema = SCHEMA
    if PG:
        schema = re.sub(r"--[^\n]*", "", schema).replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY").replace(" REAL", " DOUBLE PRECISION")
        # SQLite lets you add up true/false directly, as in SUM(stage='won'); teach Postgres the same
        run("CREATE OR REPLACE FUNCTION brn_bool_add(bigint, boolean) RETURNS bigint AS 'SELECT $1 + CASE WHEN $2 THEN 1 ELSE 0 END' LANGUAGE SQL IMMUTABLE")
        if not _conn.run("SELECT 1 x FROM pg_aggregate a JOIN pg_proc p ON p.oid=a.aggfnoid WHERE p.proname='sum' AND p.proargtypes[0]='boolean'::regtype", fetch=True):
            run("CREATE AGGREGATE sum(boolean) (SFUNC = brn_bool_add, STYPE = bigint, INITCOND = '0')")
    if PG:
        for stmt in (x.strip() for x in schema.split(";")):
            if stmt:
                run(stmt)
    else:
        _conn.executescript(schema)
    for table in sorted({t for t, _, _ in MIGRATIONS}):
        have = _columns(table)
        for t, col, typ in MIGRATIONS:
            if t == table and col not in have:
                run(f"ALTER TABLE {table} ADD COLUMN {col} {typ.replace('REAL', 'DOUBLE PRECISION') if PG else typ}")
    run("CREATE INDEX IF NOT EXISTS idx_lead_product ON leads(product)")
    if PG:
        _import_sqlite()
    # dry-run mode is gone: anything that was on it now waits for approval (never auto-sends by surprise)
    run("UPDATE settings SET value='approval' WHERE key='mode' AND value NOT IN ('approval','live')")
    for k, v in DEFAULT_SETTINGS.items():
        run("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
    if not PG:
        _conn.commit()


def _import_sqlite():
    """First start on an empty Postgres: bring everything over from the SQLite file this app used before."""
    if not os.path.isfile(DB_PATH) or _conn.run("SELECT 1 x FROM settings LIMIT 1", fetch=True) or _conn.run("SELECT 1 x FROM leads LIMIT 1", fetch=True):
        return
    src = sqlite3.connect(DB_PATH)
    src.row_factory = sqlite3.Row
    copied = {}
    try:
        tables = [r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for table in tables:
            have = _columns(table)
            if not have:
                continue
            rows = [dict(r) for r in src.execute(f"SELECT * FROM {table}")]
            for r in rows:
                cols = [c for c in r if c in have]
                _conn.run(f"INSERT INTO {table}({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", [r[c] for c in cols])
            copied[table] = len(rows)
            if table in ID_TABLES and rows:  # carry on numbering after the highest copied id
                _conn.run(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), (SELECT MAX(id) FROM {table}))", fetch=True)
    finally:
        src.close()
    summary = ", ".join(f"{n} {t}" for t, n in copied.items() if n)
    _conn.run("INSERT INTO activities(lead_id, kind, channel, status, summary, payload, created_at) VALUES (?,?,?,?,?,?,?)",
              (None, "system", "system", "ok", f"Moved to Postgres: copied {summary or 'an empty database'} from the SQLite file", "{}", now()))


def db():
    global _conn
    if _conn is None:
        with _lock:
            if _conn is None:
                _conn = _Pg() if PG else conn()
                _setup()
    return _conn


def q(sql, args=()):
    with _lock:
        if PG:
            return db().run(sql, args, fetch=True)
        return [dict(r) for r in db().execute(sql, args).fetchall()]


def one(sql, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None


def ex(sql, args=()):
    """Run a change. For an INSERT into a table with ids, returns the new row's id."""
    with _lock:
        if PG:
            m = re.match(r"\s*INSERT\s+INTO\s+(\w+)", sql, re.I)
            return db().run(sql, args, insert_id=bool(m and m[1].lower() in ID_TABLES and "RETURNING" not in sql.upper()))
        cur = db().execute(sql, args)
        db().commit()
        return cur.lastrowid


def setting(key, cast=str):
    row = one("SELECT value FROM settings WHERE key=?", (key,))
    val = row["value"] if row else DEFAULT_SETTINGS.get(key)
    if val is None:
        return None
    return cast(val) if cast is not json.loads else json.loads(val)


def set_setting(key, value):
    ex("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def log(lead_id, kind, summary, channel="agent", status="ok", payload=None):
    who = actor.get()
    return ex(
        "INSERT INTO activities(lead_id, kind, channel, status, summary, payload, created_at, actor) VALUES (?,?,?,?,?,?,?,?)",
        (lead_id, kind, channel, status, summary, json.dumps(payload or {}, ensure_ascii=False), now(),
         who["name"] if who and not who.get("implicit") else None),
    )


def update_lead(lead_id, **fields):
    fields["updated_at"] = now()
    if "stage" in fields:  # a deal closes when it is won or lost, and reopens if it moves on
        fields.setdefault("closed_at", now() if fields["stage"] in ("won", "lost") else None)
    cols = ", ".join(f"{k}=?" for k in fields)
    ex(f"UPDATE leads SET {cols} WHERE id=?", (*fields.values(), lead_id))
