"""Accounts, sign-in sessions and roles.

Three states, so nothing breaks on upgrade:
  open    no accounts and no BRN_ADMIN_PASSWORD: anyone who can open the page is an admin (as before)
  basic   no accounts, BRN_ADMIN_PASSWORD set: the browser's password prompt, everyone an admin (as before)
  users   at least one account exists: everybody signs in with their own email and password, and their role decides
          what they can do. BRN_ADMIN_PASSWORD then only works as a recovery password for admin accounts.
"""
import hashlib
import hmac
import os
import secrets
import time
from datetime import datetime, timedelta, timezone

from . import db

ADMIN_PW = os.environ.get("BRN_ADMIN_PASSWORD")
COOKIE = "brn_session"
SESSION_DAYS = 14

# What each permission lets someone do.
PERMS = {
    "view": "See the dashboard, leads, pipelines, calendar and activity log",
    "leads.write": "Add, import, edit and find leads; use the assistant",
    "leads.delete": "Delete leads",
    "export": "Download leads and the activity log",
    "send": "Approve, edit and skip emails and calls",
    "mailbox": "Read the mailbox and lead conversations, write and send email",
    "calendar": "Schedule, change and cancel meetings",
    "agent": "Start and stop the agent, run lead searches",
    "settings": "Change settings, the do-not-contact list and connections",
    "users": "Add people and change their roles",
}
ROLES = {
    "admin": {"label": "Admin", "about": "Everything, including people, roles and settings.", "perms": list(PERMS)},
    "manager": {"label": "Manager", "about": "Runs the pipeline day to day: leads, approvals, mailbox, meetings, the agent and exports. No settings or people.",
                "perms": ["view", "leads.write", "leads.delete", "export", "send", "mailbox", "calendar", "agent"]},
    "sales": {"label": "Sales rep", "about": "Works leads: adds and edits them, approves emails, uses the mailbox and books meetings. Cannot delete, export or change how the agent runs.",
              "perms": ["view", "leads.write", "send", "mailbox", "calendar"]},
    "viewer": {"label": "Viewer", "about": "Read only: dashboard, leads, pipelines, calendar and activity log. No mailbox, nothing can be sent or changed.",
               "perms": ["view"]},
}
IMPLICIT_ADMIN = {"id": None, "name": "Admin", "email": "", "role": "admin", "implicit": True}
_fails = {}


def mode():
    if db.one("SELECT 1 x FROM users LIMIT 1"):
        return "users"
    return "basic" if ADMIN_PW else "open"


def perms(user):
    return ROLES.get((user or {}).get("role"), {"perms": []})["perms"]


def can(perm, user=None):
    return perm in perms(user or db.actor.get() or {})


def required(method, path):
    """The permission a request needs. Anything not listed that changes data needs an admin."""
    if path.startswith("/api/users"):
        return "users"
    if path.startswith(("/api/mailbox", "/api/mail", "/api/inbox")):
        return "mailbox"
    if path.startswith("/api/export"):
        return "export"
    if path.startswith("/oauth/"):
        return "settings"
    if method in ("GET", "HEAD", "OPTIONS"):
        return "view"
    if path.startswith("/api/agent") or path.endswith("/discover"):
        return "agent"
    if path.startswith("/api/approvals"):
        return "send"
    if path.startswith("/api/calendar"):
        return "calendar"
    if path.startswith(("/api/leads", "/api/assistant", "/api/tasks")):
        return "leads.write"
    if path.startswith("/api/templates"):
        return "send"
    return "settings"


# ---------- passwords ----------
def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    return f"pbkdf2${salt}$" + hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 310_000).hex()


def check_pw(pw, stored):
    try:
        _, salt, _ = stored.split("$")
    except ValueError:
        return False
    return hmac.compare_digest(hash_pw(pw, salt), stored)


def check_new_password(pw):
    if len(pw or "") < 8:
        raise ValueError("Use a password of at least 8 characters.")


# ---------- sessions ----------
def _digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(user_id):
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")
    db.ex("INSERT INTO sessions(token, user_id, created_at, expires_at) VALUES (?,?,?,?)", (_digest(token), user_id, db.now(), expires))
    db.ex("UPDATE users SET last_login_at=? WHERE id=?", (db.now(), user_id))
    db.ex("DELETE FROM sessions WHERE expires_at<?", (db.now(),))
    return token


def session_user(token):
    if not token:
        return None
    return db.one("""SELECT u.id, u.name, u.email, u.role FROM sessions s JOIN users u ON u.id=s.user_id
                     WHERE s.token=? AND s.expires_at>? AND u.active=1""", (_digest(token), db.now()))


def end_session(token):
    if token:
        db.ex("DELETE FROM sessions WHERE token=?", (_digest(token),))


def end_sessions_of(user_id):
    db.ex("DELETE FROM sessions WHERE user_id=?", (user_id,))


def login(email, password, ip=""):
    """Returns the user, or raises ValueError with what to tell the person."""
    key, now = (ip, (email or "").lower()), time.time()
    recent = [t for t in _fails.get(key, []) if now - t < 600]
    if len(recent) >= 8:
        raise ValueError("Too many wrong attempts. Wait 10 minutes and try again.")
    u = db.one("SELECT * FROM users WHERE lower(email)=?", ((email or "").strip().lower(),))
    ok = bool(u and u["active"] and (check_pw(password or "", u["password"])
              or (ADMIN_PW and u["role"] == "admin" and hmac.compare_digest(password or "", ADMIN_PW))))  # recovery password
    if not ok:
        _fails[key] = recent + [now]
        raise ValueError("Wrong email or password.")
    _fails.pop(key, None)
    return u


# ---------- people ----------
def public(u):
    return {k: u[k] for k in ("id", "name", "email", "role", "active", "created_at", "last_login_at") if k in u}


def users():
    return [public(u) for u in db.q("SELECT * FROM users ORDER BY active DESC, name COLLATE NOCASE")]


def _admins(excluding=None):
    return db.one("SELECT COUNT(*) n FROM users WHERE role='admin' AND active=1 AND id<>?", (excluding or 0,))["n"]


def create(name, email, role, password):
    name, email = (name or "").strip(), (email or "").strip().lower()
    if not name or "@" not in email:
        raise ValueError("Enter a name and a valid email address.")
    if role not in ROLES:
        raise ValueError("Pick a role.")
    if mode() != "users" and role != "admin":
        raise ValueError("The first account must be an admin, so someone can manage the rest.")
    check_new_password(password)
    if db.one("SELECT 1 x FROM users WHERE lower(email)=?", (email,)):
        raise ValueError("Someone with this email already has an account.")
    return db.ex("INSERT INTO users(name, email, role, password, active, created_at) VALUES (?,?,?,?,1,?)",
                 (name, email, role, hash_pw(password), db.now()))


def update(user_id, fields, by=None):
    u = db.one("SELECT * FROM users WHERE id=?", (user_id,))
    if not u:
        raise ValueError("That account no longer exists.")
    sets = {}
    if "name" in fields and (fields["name"] or "").strip():
        sets["name"] = fields["name"].strip()
    if "role" in fields:
        if fields["role"] not in ROLES:
            raise ValueError("Pick a role.")
        sets["role"] = fields["role"]
    if "active" in fields:
        sets["active"] = 1 if fields["active"] else 0
    losing_admin = u["role"] == "admin" and u["active"] and (sets.get("role", "admin") != "admin" or sets.get("active", 1) == 0)
    if losing_admin and not _admins(excluding=user_id):
        raise ValueError("This is the only admin. Make someone else an admin first.")
    if fields.get("password"):
        check_new_password(fields["password"])
        sets["password"] = hash_pw(fields["password"])
    if sets:
        db.ex(f"UPDATE users SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), user_id))
    if "password" in sets or sets.get("active") == 0:
        end_sessions_of(user_id)  # signed out everywhere
    return u


def delete(user_id, by=None):
    u = db.one("SELECT * FROM users WHERE id=?", (user_id,))
    if not u:
        return
    if by and by.get("id") == user_id:
        raise ValueError("You can't delete your own account.")
    if u["role"] == "admin" and u["active"] and not _admins(excluding=user_id):
        raise ValueError("This is the only admin. Make someone else an admin first.")
    end_sessions_of(user_id)
    db.ex("DELETE FROM users WHERE id=?", (user_id,))
    return u
