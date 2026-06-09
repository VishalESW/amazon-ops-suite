"""SQLite persistence layer.

Stores connected Amazon accounts (with encrypted refresh tokens), Ads profiles,
per-account SKU->BAND overrides, and key/value settings (e.g. target ACOS/CPA).

Refresh tokens are encrypted via utils.crypto before they touch the DB.
"""

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager

from config import cfg
from utils.crypto import encrypt, decrypt

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id                 TEXT PRIMARY KEY,
    kind               TEXT NOT NULL,            -- 'spapi' | 'ads'
    name               TEXT,
    selling_partner_id TEXT,
    refresh_token_enc  TEXT NOT NULL,
    region             TEXT,
    marketplace_id     TEXT,
    created_at         REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS ads_profiles (
    id           TEXT PRIMARY KEY,
    account_id   TEXT NOT NULL,
    profile_id   TEXT NOT NULL,
    country_code TEXT,
    currency     TEXT,
    account_name TEXT,
    account_type TEXT,
    UNIQUE(account_id, profile_id)
);

CREATE TABLE IF NOT EXISTS band_map (
    account_id  TEXT NOT NULL,
    sku         TEXT NOT NULL,
    asin        TEXT,
    title       TEXT,
    band        TEXT,
    category    TEXT,
    size        TEXT,
    color       TEXT,
    fulfillment TEXT,
    PRIMARY KEY (account_id, sku)
);

CREATE TABLE IF NOT EXISTS settings (
    scope TEXT NOT NULL,   -- e.g. account id or 'global'
    key   TEXT NOT NULL,
    value TEXT,
    PRIMARY KEY (scope, key)
);
"""


def init_db():
    os.makedirs(cfg.DATA_FOLDER, exist_ok=True)
    with _conn() as c:
        c.executescript(_SCHEMA)


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


# ---------------------------------------------------------------- accounts ---

def upsert_account(kind, name, selling_partner_id, refresh_token,
                   region=None, marketplace_id=None):
    """Insert or update an account by (kind, selling_partner_id). Returns id."""
    region = region or cfg.SPAPI_REGION
    marketplace_id = marketplace_id or cfg.SPAPI_DEFAULT_MARKETPLACE_ID
    with _conn() as c:
        existing = c.execute(
            "SELECT id FROM accounts WHERE kind=? AND selling_partner_id=?",
            (kind, selling_partner_id),
        ).fetchone()
        token_enc = encrypt(refresh_token)
        if existing:
            acct_id = existing["id"]
            c.execute(
                "UPDATE accounts SET name=?, refresh_token_enc=?, region=?, "
                "marketplace_id=? WHERE id=?",
                (name, token_enc, region, marketplace_id, acct_id),
            )
        else:
            acct_id = uuid.uuid4().hex
            c.execute(
                "INSERT INTO accounts (id, kind, name, selling_partner_id, "
                "refresh_token_enc, region, marketplace_id, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (acct_id, kind, name, selling_partner_id, token_enc,
                 region, marketplace_id, time.time()),
            )
        return acct_id


def list_accounts(kind=None):
    with _conn() as c:
        if kind:
            rows = c.execute(
                "SELECT * FROM accounts WHERE kind=? ORDER BY created_at DESC", (kind,)
            ).fetchall()
        else:
            rows = c.execute("SELECT * FROM accounts ORDER BY created_at DESC").fetchall()
    return [_account_public(r) for r in rows]


def get_account(account_id):
    with _conn() as c:
        r = c.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    return _account_public(r) if r else None


def get_account_refresh_token(account_id):
    with _conn() as c:
        r = c.execute(
            "SELECT refresh_token_enc FROM accounts WHERE id=?", (account_id,)
        ).fetchone()
    return decrypt(r["refresh_token_enc"]) if r else None


def delete_account(account_id):
    with _conn() as c:
        c.execute("DELETE FROM accounts WHERE id=?", (account_id,))
        c.execute("DELETE FROM ads_profiles WHERE account_id=?", (account_id,))
        c.execute("DELETE FROM band_map WHERE account_id=?", (account_id,))


def _account_public(row):
    """Strip the encrypted token before handing an account to the app layer."""
    d = dict(row)
    d.pop("refresh_token_enc", None)
    return d


# ------------------------------------------------------------ ads profiles ---

def replace_ads_profiles(account_id, profiles):
    """profiles: list of dicts with profileId, countryCode, currencyCode, accountInfo."""
    with _conn() as c:
        c.execute("DELETE FROM ads_profiles WHERE account_id=?", (account_id,))
        for p in profiles:
            info = p.get("accountInfo") or {}
            c.execute(
                "INSERT OR REPLACE INTO ads_profiles (id, account_id, profile_id, "
                "country_code, currency, account_name, account_type) "
                "VALUES (?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, account_id, str(p.get("profileId")),
                 p.get("countryCode"), p.get("currencyCode"),
                 info.get("name"), info.get("type")),
            )


def list_ads_profiles(account_id):
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM ads_profiles WHERE account_id=?", (account_id,)
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------- band map ---

def get_band_map(account_id):
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM band_map WHERE account_id=?", (account_id,)
        ).fetchall()
    return {r["sku"]: dict(r) for r in rows}


def save_band_map(account_id, rows):
    """rows: iterable of dicts with sku/asin/title/band/category/size/color/fulfillment."""
    with _conn() as c:
        for r in rows:
            c.execute(
                "INSERT OR REPLACE INTO band_map (account_id, sku, asin, title, "
                "band, category, size, color, fulfillment) VALUES (?,?,?,?,?,?,?,?,?)",
                (account_id, r.get("sku"), r.get("asin"), r.get("title"),
                 r.get("band"), r.get("category"), r.get("size"),
                 r.get("color"), r.get("fulfillment")),
            )


# ---------------------------------------------------------------- settings ---

def set_setting(scope, key, value):
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO settings (scope, key, value) VALUES (?,?,?)",
            (scope, key, json.dumps(value)),
        )


def get_setting(scope, key, default=None):
    with _conn() as c:
        r = c.execute(
            "SELECT value FROM settings WHERE scope=? AND key=?", (scope, key)
        ).fetchone()
    if not r:
        return default
    try:
        return json.loads(r["value"])
    except (TypeError, ValueError):
        return default
