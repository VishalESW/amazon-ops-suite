"""Campaign Processor v2 — AdLabs data access.

Profile discovery + per-ASIN ("product" entity) metrics for the ASIN dashboard,
plus the composite Rank. Built on utils.adlabs_client.AdLabsClient.

Confirmed live: entity_type="product" returns asin, sku, title, brand,
impressions, clicks, ctr, spend, cpc, sales, orders, cvr, acos, roas,
price_to_pay, total_asp, best_seller_rank (+ many *_comparison/_delta).
"""

from __future__ import annotations

import datetime as _dt
import re
import time

from utils.adlabs_client import AdLabsClient, AdLabsError

_client = AdLabsClient()

# profile list cache (rarely changes; AdLabs throttles)
_profiles_cache = {"ts": 0.0, "data": None}
_PROFILES_TTL = 600

_SLUG_RE = re.compile(r"adlabs://profiles/([0-9a-z]+)", re.I)
_PID_RE = re.compile(r"Profile ID:\s*(\d+)", re.I)


def _num(v, default=0.0):
    try:
        return float(str(v).replace(",", "").replace("$", "").replace("%", "").strip() or 0)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------- profiles ---
def list_profiles(force=False):
    """[{team_id, team_name, name, country, slug}] across all teams."""
    if not force and _profiles_cache["data"] and time.time() - _profiles_cache["ts"] < _PROFILES_TTL:
        return _profiles_cache["data"]
    teams_text = _client.get_entity_data("teams")
    out = []
    for tid, tname in re.findall(r"team_id=(\d+)\s+([^\n]+?)\s+org=", teams_text):
        ptext = _client.get_entity_data("profiles", team_id=int(tid))
        for row in AdLabsClient.parse_table(ptext):
            slug_m = _SLUG_RE.search(row.get("Resource URI (read this to get profile_id)", "")
                                     or " ".join(str(v) for v in row.values()))
            out.append({
                "team_id": int(tid),
                "team_name": tname.strip(),
                "name": (row.get("Name") or "").strip(),
                "country": (row.get("Country") or "").strip(),
                "currency": (row.get("Currency") or "").strip(),
                "brand": (row.get("Brand") or "").strip(),
                "slug": slug_m.group(1) if slug_m else None,
            })
    _profiles_cache.update(ts=time.time(), data=out)
    return out


def resolve_profile_id(slug):
    """slug (e.g. 'hilc5xxyk2') -> numeric profile_id string."""
    text = _client.read_resource(f"adlabs://profiles/{slug}")
    m = _PID_RE.search(text)
    if not m:
        raise AdLabsError(f"Could not resolve profile_id from {slug}")
    return m.group(1)


# --------------------------------------------------------------- products ---
def _date_filters(days=90):
    today = _dt.date.today()
    start = today - _dt.timedelta(days=days)
    prev_end = start - _dt.timedelta(days=1)
    prev_start = prev_end - _dt.timedelta(days=days)
    fmt = "%Y-%m-%d"
    return [
        {"key": "DATE", "conditions": [
            {"operator": ">=", "values": [start.strftime(fmt)]},
            {"operator": "<=", "values": [today.strftime(fmt)]}], "logical_operator": "AND"},
        {"key": "COMPARE_DATE", "conditions": [
            {"operator": ">=", "values": [prev_start.strftime(fmt)]},
            {"operator": "<=", "values": [prev_end.strftime(fmt)]}], "logical_operator": "AND"},
        {"key": "IMPRESSIONS", "conditions": [{"operator": ">", "values": ["0"]}]},
    ]


def fetch_products(team_id, profile_id, days=90, limit=1000):
    """Return per-ASIN metric dicts for the dashboard, with composite Rank."""
    out = _client.get_entity_data("product", team_id=int(team_id),
                                  profile_id=str(profile_id), filters=_date_filters(days))
    ref = AdLabsClient.first_reference(out)
    if not ref:
        return []
    try:
        rows = _client.download_rows(ref)        # full set, no 100-row cap
    except Exception:                            # noqa: BLE001
        rows = AdLabsClient.parse_table(_client.read(ref, limit=limit))
    products = []
    for r in rows:
        asin = (r.get("asin") or "").strip()
        if not asin:
            continue
        products.append({
            "asin": asin,
            "sku": (r.get("sku") or "").strip(),
            "name": (r.get("title") or r.get("display_name") or "").strip(),
            "brand": (r.get("brand") or "").strip(),
            "impressions": _num(r.get("impressions")),
            "clicks": _num(r.get("clicks")),
            "ctr": _num(r.get("ctr")),
            "spend": _num(r.get("spend")),
            "cpc": _num(r.get("cpc")),
            "sales": _num(r.get("sales")),
            "orders": _num(r.get("orders")),
            "acos": _num(r.get("acos")),
            "cvr": _num(r.get("cvr")),
            "price": _num(r.get("price_to_pay") or r.get("basis_price")),
            "asp": _num(r.get("total_asp")),
            "bsr": _num(r.get("best_seller_rank")),
        })
    add_ranks(products)
    return products


# ------------------------------------------------------------------ rank ---
def add_ranks(products):
    """Composite priority rank: 0.5*z(sales) + 0.3*z(cvr) - 0.2*z(acos).
    Rank 1 = best. Mutates each product dict: ['rank','rank_score']."""
    n = len(products)
    if n == 0:
        return products

    def _z(key):
        vals = [p[key] for p in products]
        mean = sum(vals) / n
        var = sum((v - mean) ** 2 for v in vals) / n
        sd = var ** 0.5
        return {id(p): (0.0 if sd == 0 else (p[key] - mean) / sd) for p in products}

    zs, zc, za = _z("sales"), _z("cvr"), _z("acos")
    for p in products:
        p["rank_score"] = round(0.5 * zs[id(p)] + 0.3 * zc[id(p)] - 0.2 * za[id(p)], 4)
    ordered = sorted(products, key=lambda p: p["rank_score"], reverse=True)
    for i, p in enumerate(ordered, start=1):
        p["rank"] = i
    return products


def health():
    ok = True
    try:
        _client._ensure_init()
    except AdLabsError:
        ok = False
    return ok
