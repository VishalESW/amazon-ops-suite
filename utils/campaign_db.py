"""Campaign Processor v2 — persistence (roles, projects, per-step state, approvals).

Self-contained: uses the same SQLite file as db.py (cfg.DB_PATH) but its own
`cp_*` tables, so it never touches the other features' schema. Tables are created
lazily on first use via init().
"""

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager

from config import cfg

ROLE_OPERATOR = "operator"
ROLE_MANAGER = "manager"

# Project lifecycle
STATUS_DRAFT = "draft"                      # operator working
STATUS_AWAITING = "awaiting_approval"       # paused at a gate, in manager queue
STATUS_APPROVED = "approved"               # manager approved current gate
STATUS_COMPLETED = "completed"             # workbook built

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cp_users (
    email TEXT PRIMARY KEY,
    role  TEXT NOT NULL DEFAULT 'operator'
);
CREATE TABLE IF NOT EXISTS cp_projects (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    team_id       TEXT,
    profile_id    TEXT,
    profile_name  TEXT,
    status        TEXT NOT NULL DEFAULT 'draft',
    current_step  TEXT,
    created_by    TEXT,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS cp_state (
    project_id TEXT NOT NULL,
    step_key   TEXT NOT NULL,
    blob       TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (project_id, step_key)
);
CREATE TABLE IF NOT EXISTS cp_approvals (
    id           TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL,
    step_key     TEXT NOT NULL,
    requested_by TEXT,
    requested_at REAL,
    approved_by  TEXT,
    approved_at  REAL,
    note         TEXT
);
"""


@contextmanager
def _conn():
    os.makedirs(cfg.DATA_FOLDER, exist_ok=True)
    conn = sqlite3.connect(cfg.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init():
    with _conn() as c:
        c.executescript(_SCHEMA)
        # Seed managers from .env on every init (idempotent upsert).
        for email in cfg.CAMPAIGN_MANAGER_EMAILS:
            e = email.strip().lower()
            if e:
                c.execute(
                    "INSERT INTO cp_users (email, role) VALUES (?, 'manager') "
                    "ON CONFLICT(email) DO UPDATE SET role='manager'", (e,))


# ------------------------------------------------------------------ roles ---
def get_role(email):
    if not email:
        return ROLE_OPERATOR
    e = email.strip().lower()
    if e in {m.strip().lower() for m in cfg.CAMPAIGN_MANAGER_EMAILS}:
        return ROLE_MANAGER
    with _conn() as c:
        r = c.execute("SELECT role FROM cp_users WHERE email=?", (e,)).fetchone()
    return r["role"] if r else ROLE_OPERATOR


def set_role(email, role):
    e = (email or "").strip().lower()
    if not e or role not in (ROLE_OPERATOR, ROLE_MANAGER):
        return
    with _conn() as c:
        c.execute("INSERT INTO cp_users (email, role) VALUES (?,?) "
                  "ON CONFLICT(email) DO UPDATE SET role=excluded.role", (e, role))


def list_users():
    with _conn() as c:
        rows = c.execute("SELECT * FROM cp_users ORDER BY email").fetchall()
    return [dict(r) for r in rows]


def is_manager(email):
    return get_role(email) == ROLE_MANAGER


# --------------------------------------------------------------- projects ---
def create_project(name, team_id, profile_id, profile_name, created_by):
    pid = uuid.uuid4().hex
    now = time.time()
    with _conn() as c:
        c.execute(
            "INSERT INTO cp_projects (id, name, team_id, profile_id, profile_name, "
            "status, current_step, created_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (pid, name, str(team_id) if team_id is not None else None,
             str(profile_id) if profile_id is not None else None, profile_name,
             STATUS_DRAFT, "profile", created_by, now, now))
    return pid


def get_project(pid):
    with _conn() as c:
        r = c.execute("SELECT * FROM cp_projects WHERE id=?", (pid,)).fetchone()
    return dict(r) if r else None


def list_projects(status=None):
    with _conn() as c:
        if status:
            rows = c.execute("SELECT * FROM cp_projects WHERE status=? "
                             "ORDER BY updated_at DESC", (status,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM cp_projects ORDER BY updated_at DESC").fetchall()
    return [dict(r) for r in rows]


def update_project(pid, **fields):
    if not fields:
        return
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [pid]
    with _conn() as c:
        c.execute(f"UPDATE cp_projects SET {cols} WHERE id=?", vals)


def delete_project(pid):
    with _conn() as c:
        c.execute("DELETE FROM cp_projects WHERE id=?", (pid,))
        c.execute("DELETE FROM cp_state WHERE project_id=?", (pid,))
        c.execute("DELETE FROM cp_approvals WHERE project_id=?", (pid,))


# ------------------------------------------------------------ step state ---
def save_state(pid, step_key, data):
    with _conn() as c:
        c.execute(
            "INSERT INTO cp_state (project_id, step_key, blob, updated_at) "
            "VALUES (?,?,?,?) ON CONFLICT(project_id, step_key) "
            "DO UPDATE SET blob=excluded.blob, updated_at=excluded.updated_at",
            (pid, step_key, json.dumps(data), time.time()))


def get_state(pid, step_key, default=None):
    with _conn() as c:
        r = c.execute("SELECT blob FROM cp_state WHERE project_id=? AND step_key=?",
                      (pid, step_key)).fetchone()
    if not r or r["blob"] is None:
        return default
    try:
        return json.loads(r["blob"])
    except (TypeError, ValueError):
        return default


def all_state(pid):
    with _conn() as c:
        rows = c.execute("SELECT step_key, blob FROM cp_state WHERE project_id=?",
                         (pid,)).fetchall()
    out = {}
    for r in rows:
        try:
            out[r["step_key"]] = json.loads(r["blob"]) if r["blob"] else None
        except (TypeError, ValueError):
            out[r["step_key"]] = None
    return out


# ------------------------------------------------------------- approvals ---
def request_approval(pid, step_key, requested_by, note=None):
    aid = uuid.uuid4().hex
    with _conn() as c:
        c.execute(
            "INSERT INTO cp_approvals (id, project_id, step_key, requested_by, "
            "requested_at, note) VALUES (?,?,?,?,?,?)",
            (aid, pid, step_key, requested_by, time.time(), note))
    update_project(pid, status=STATUS_AWAITING, current_step=step_key)
    return aid


def approve(pid, step_key, approved_by, note=None):
    with _conn() as c:
        r = c.execute(
            "SELECT id FROM cp_approvals WHERE project_id=? AND step_key=? "
            "AND approved_at IS NULL ORDER BY requested_at DESC LIMIT 1",
            (pid, step_key)).fetchone()
        if r:
            c.execute("UPDATE cp_approvals SET approved_by=?, approved_at=?, "
                      "note=COALESCE(?, note) WHERE id=?",
                      (approved_by, time.time(), note, r["id"]))
    update_project(pid, status=STATUS_APPROVED, current_step=step_key)


def pending_approvals():
    """All projects awaiting approval, with their latest pending request."""
    with _conn() as c:
        rows = c.execute(
            "SELECT a.*, p.name AS project_name, p.profile_name "
            "FROM cp_approvals a JOIN cp_projects p ON p.id = a.project_id "
            "WHERE a.approved_at IS NULL ORDER BY a.requested_at DESC").fetchall()
    return [dict(r) for r in rows]


def project_approvals(pid):
    with _conn() as c:
        rows = c.execute("SELECT * FROM cp_approvals WHERE project_id=? "
                         "ORDER BY requested_at", (pid,)).fetchall()
    return [dict(r) for r in rows]


def approval_status(pid, step_key):
    """'none' (never requested) | 'awaiting' (requested, not approved) | 'approved'."""
    with _conn() as c:
        r = c.execute("SELECT approved_at FROM cp_approvals WHERE project_id=? "
                      "AND step_key=? ORDER BY requested_at DESC LIMIT 1",
                      (pid, step_key)).fetchone()
    if not r:
        return "none"
    return "approved" if r["approved_at"] else "awaiting"


def approval_map(pid, step_keys):
    return {k: approval_status(pid, k) for k in step_keys}
