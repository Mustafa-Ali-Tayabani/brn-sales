"""SQLite storage. Swap for Postgres (Supabase) in production: the schema is plain SQL."""
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

DB_PATH = os.environ.get("BRN_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "pipeline.db"))
_lock = threading.RLock()

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
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_act_lead ON activities(lead_id);
CREATE INDEX IF NOT EXISTS idx_act_time ON activities(created_at);
CREATE INDEX IF NOT EXISTS idx_lead_next ON leads(next_action_at);
"""

PRODUCTS = [
    {"key": "fixify", "name": "Fixify",
     "pitch": "Fixify by brn dynamics: WordPress maintenance as a service. One fixed monthly plan covers security, backups, speed, SEO, bug fixes, content updates and technical support, so the business has a technical team without hiring one.",
     "icp": "Small businesses whose WordPress or small-business website is slow, insecure or has SEO problems"},
    {"key": "gym_club", "name": "BRN Gym Club",
     "pitch": "BRN Gym Club: gym and club management platform. Memberships, reservations, access control, POS, inventory, an AI assistant and an Arabic/English member app in one system.",
     "icp": "Gyms, fitness studios and sports clubs in Saudi Arabia and Kuwait with 1 to 20 branches"},
    {"key": "ai_waiter", "name": "AI Waiter",
     "pitch": "AI Waiter: the table QR code becomes an ordering assistant with recommendations, voice ordering, loyalty and payments.",
     "icp": "Restaurants and cafes, single site or small chains"},
    {"key": "estimates", "name": "Estimate & Property Case Mgmt",
     "pitch": "Estimate and property case management: service requests, estimates, scheduling, building data and diagnostics in one platform.",
     "icp": "Property management, facilities and construction service companies"},
    {"key": "qapipeline", "name": "QAPipeline",
     "pitch": "QAPipeline: AI-powered test management. Test cases, automated runs, results analytics and AI-generated test scenarios.",
     "icp": "Software companies and in-house product teams with QA needs"},
    {"key": "rpa_finance", "name": "RPA Finance Automation",
     "pitch": "Finance automation: reads bills from email with OCR, maps them to branches and supports reconciliation. One client saved 412 hours.",
     "icp": "Multi-branch businesses with heavy bill processing in finance teams"},
    {"key": "custom", "name": "Custom software & AI",
     "pitch": "brn dynamics builds custom software: product design, SaaS, CRM/ERP, AI and automation, web and mobile apps. 80+ projects since 2019, 5.0 on Clutch.",
     "icp": "Growing businesses that need custom software or AI automation"},
]

DEFAULT_SETTINGS = {
    # dry_run: nothing leaves the system. approval: every send waits for a human click. live: auto-send within caps.
    "mode": "dry_run",
    "offer": "BRN Gym Club: gym and club management software (memberships, billing, access control, classes, Arabic/English app)",
    "icp": "Gyms, fitness studios and sports clubs in Saudi Arabia and Kuwait with 1 to 20 branches",
    "sender_name": "brn dynamics",
    "sender_email": "sales@brndynamics.com",
    "booking_link": "https://brndynamics.com/contact",
    "daily_email_cap": "40",
    "daily_call_cap": "20",
    "min_score": "55",
    "products": json.dumps(PRODUCTS),
    "default_product": "custom",
    "company_address": "brn dynamics, Karachi · Riyadh · Kuwait City · Geneva",
    "agent_enabled": "1",
    "discovery_queries": json.dumps([]),
    "last_discovery": "",
    "sequence": json.dumps([
        {"day": 0, "channel": "email", "action": "intro"},
        {"day": 3, "channel": "email", "action": "follow_up_1"},
        {"day": 5, "channel": "phone", "action": "call"},
        {"day": 9, "channel": "email", "action": "follow_up_2"},
        {"day": 14, "channel": "email", "action": "breakup"},
    ]),
}


MIGRATIONS = [
    ("product", "TEXT"), ("segment", "TEXT"), ("extra", "TEXT"),
    ("emails_sent", "INTEGER NOT NULL DEFAULT 0"), ("last_contacted_at", "TEXT"), ("replied_at", "TEXT"),
]


def products():
    try:
        return {p["key"]: p for p in json.loads(setting("products"))}
    except Exception:
        return {p["key"]: p for p in PRODUCTS}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


_conn = None


def db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = conn()
        _conn.executescript(SCHEMA)
        cols = {r[1] for r in _conn.execute("PRAGMA table_info(leads)")}
        for col, typ in MIGRATIONS:
            if col not in cols:
                _conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {typ}")
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_lead_product ON leads(product)")
        for k, v in DEFAULT_SETTINGS.items():
            _conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
        _conn.commit()
    return _conn


def q(sql, args=()):
    with _lock:
        return [dict(r) for r in db().execute(sql, args).fetchall()]


def one(sql, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None


def ex(sql, args=()):
    with _lock:
        cur = db().execute(sql, args)
        db().commit()
        return cur.lastrowid


def setting(key, cast=str):
    row = one("SELECT value FROM settings WHERE key=?", (key,))
    val = row["value"] if row else DEFAULT_SETTINGS.get(key)
    return cast(val) if cast is not json.loads else json.loads(val)


def set_setting(key, value):
    ex("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def log(lead_id, kind, summary, channel="agent", status="ok", payload=None):
    return ex(
        "INSERT INTO activities(lead_id, kind, channel, status, summary, payload, created_at) VALUES (?,?,?,?,?,?,?)",
        (lead_id, kind, channel, status, summary, json.dumps(payload or {}, ensure_ascii=False), now()),
    )


def update_lead(lead_id, **fields):
    fields["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    ex(f"UPDATE leads SET {cols} WHERE id=?", (*fields.values(), lead_id))
