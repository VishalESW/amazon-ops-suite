"""Ad Performance Dashboard — hierarchical product → campaign → keyword view.

Completely separate from the Bid Optimizer and Harvesting sections.
Shares only the AdLabs client and profile-load utilities with blueprints/ads.py.

Routes
------
GET  /dashboard                    → HTML page
POST /dashboard/load               → start background load job
GET  /dashboard/load/<job_id>      → poll status
GET  /dashboard/load/<job_id>/data → result
POST /dashboard/search-terms       → lazy-load search terms for one campaign
POST /dashboard/sqp                → load SQP data for profile
POST /dashboard/ai-insight         → NVIDIA GLM-5.1 AI analysis
"""

import json
import re
import threading
from collections import defaultdict
from datetime import datetime, timedelta

import requests
from flask import Blueprint, render_template, request, jsonify, redirect, Response

from utils import jobs
from utils.adlabs_client import AdLabsClient, AdLabsError
from utils.jsonutil import convert_numpy
from blueprints.ads import _adlabs, _PROFILE_ID_RE

bp = Blueprint("dashboard", __name__, url_prefix="/dashboard")

_NVIDIA_API_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
_NVIDIA_API_KEY = "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"
_NVIDIA_MODEL   = "google/gemma-3n-e4b-it"

# Simple in-process image cache: asin → image_url string
_img_cache: dict = {}

# Per-account listings cache: account_id → {ts, by_asin, skus, asins}
import time as _time
_acct_listings_cache: dict = {}
_ACCT_LISTINGS_TTL = 600  # seconds


# ─── helpers ─────────────────────────────────────────────────────────────────

def _f(v, d=0.0):
    try:
        return float(str(v).replace(",", "").replace("$", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return d


def _date_filters_dash(lookback, start=None, end=None):
    """Return (adlabs_filters_list, range_label)."""
    if start and end:
        try:
            s = datetime.strptime(start, "%Y-%m-%d").date()
            e = datetime.strptime(end, "%Y-%m-%d").date()
        except ValueError:
            pass
        else:
            if e < s:
                s, e = e, s
            days = (e - s).days + 1
            c_end   = s - timedelta(days=1)
            c_start = c_end - timedelta(days=days - 1)

            def blk(key, a, b):
                return {"key": key, "conditions": [
                    {"operator": ">=", "values": [a.isoformat()]},
                    {"operator": "<=", "values": [b.isoformat()]}],
                    "logical_operator": "AND"}
            return (
                [blk("DATE", s, e), blk("COMPARE_DATE", c_start, c_end)],
                f"{s.isoformat()} → {e.isoformat()}",
            )

    from datetime import date
    end_d   = date.today() - timedelta(days=1)
    start_d = end_d - timedelta(days=lookback - 1)
    c_end   = start_d - timedelta(days=1)
    c_start = c_end - timedelta(days=lookback - 1)

    def blk(key, a, b):
        return {"key": key, "conditions": [
            {"operator": ">=", "values": [a.isoformat()]},
            {"operator": "<=", "values": [b.isoformat()]}],
            "logical_operator": "AND"}
    return (
        [blk("DATE", start_d, end_d), blk("COMPARE_DATE", c_start, c_end)],
        f"last {lookback} days",
    )


def _camp_type(name, field=""):
    """Detect SP / SB / SD from campaign name or ad_type field."""
    n = (name or "").upper()
    f = (field or "").upper()
    for key, variants in [
        ("SB", ["SPONSORED_BRANDS", " - SB -", " SB -", "-SB-", "_SB_"]),
        ("SD", ["SPONSORED_DISPLAY", " - SD -", " SD -", "-SD-", "_SD_"]),
    ]:
        if key in f or any(v in n for v in variants):
            return key
    return "SP"


def _amazon_img(asin):
    return f"https://images-na.ssl-images-amazon.com/images/P/{asin}.01._SX80_.jpg"


# ─── helpers ─────────────────────────────────────────────────────────────────

def _fetch_inventory(sp_client, skus):
    """Return {asin: on_hand_qty} matching Seller Central 'On-hand (FBA)'.

    On-hand = units physically in fulfillment centers = fulfillable + in-transit
    between FCs (pendingTransshipment) + being researched. Excludes inbound and
    order/processing reservations — exactly how Seller Central computes it.

    The FBA Inventory summaries API silently omits some ASINs when called without
    a SKU filter, so we query it WITH `sellerSkus` (the seller's full SKU list
    from the listings report), batched. Inventory is deduped by FNSKU because a
    single physical stock can be listed under multiple SKUs (e.g. an "-FBM"
    duplicate) — summing those would double-count.
    """
    inv = {}
    seen_fnsku = set()
    skus = [s for s in dict.fromkeys(skus) if s]
    if not skus:
        return inv

    for i in range(0, len(skus), 20):
        batch = skus[i:i + 20]
        params = {
            "details": "true",
            "granularityType": "Marketplace",
            "granularityId": sp_client.marketplace_id,
            "marketplaceIds": sp_client.marketplace_id,
            "sellerSkus": ",".join(batch),
        }
        try:
            resp = requests.get(sp_client.endpoint + "/fba/inventory/v1/summaries",
                                headers=sp_client._headers(), params=params, timeout=60)
            if resp.status_code >= 400:
                continue
            for s in (resp.json().get("payload", {}) or {}).get("inventorySummaries", []) or []:
                asin  = (s.get("asin") or "").strip().upper()
                fnsku = s.get("fnSku") or s.get("sellerSku")
                if not asin or not fnsku or fnsku in seen_fnsku:
                    continue
                seen_fnsku.add(fnsku)
                d  = s.get("inventoryDetails", {}) or {}
                rq = d.get("reservedQuantity", {}) or {}
                ful   = int(d.get("fulfillableQuantity", 0) or 0)
                trans = int(rq.get("pendingTransshipmentQuantity", 0) or 0)
                resr  = int((d.get("researchingQuantity", {}) or {}).get(
                            "totalResearchingQuantity", 0) or 0)
                inv[asin] = inv.get(asin, 0) + ful + trans + resr
        except Exception:
            continue

    return inv


def _account_listings(acct):
    """Fetch (and cache) one SP-API account's listings.

    Returns {"by_asin": {asin: {price, image_url}}, "skus": [...], "asins": set}.
    Cached per account for _ACCT_LISTINGS_TTL so switching profiles / reloading
    doesn't re-pull the (slow) listings report every time.
    """
    cid = acct["id"]
    c = _acct_listings_cache.get(cid)
    if c and (_time.time() - c["ts"]) < _ACCT_LISTINGS_TTL:
        return c

    from db import get_account_refresh_token
    from utils.spapi_client import SpApiClient, RT_OPEN_LISTINGS
    rt = get_account_refresh_token(cid)
    sp = SpApiClient(rt, marketplace_id=acct.get("marketplace_id"))

    by_asin, skus = {}, []
    try:
        for r in sp.fetch_tsv_report(RT_OPEN_LISTINGS):
            sku = (r.get("seller-sku") or "").strip()
            if sku:
                skus.append(sku)
            asin = (r.get("asin1") or "").strip().upper()
            if not asin:
                continue
            e = by_asin.setdefault(asin, {})
            if not e.get("price"):
                p = _f(r.get("price", 0)) or None
                if p:
                    e["price"] = p
            if not e.get("image_url"):
                img = (r.get("image-url") or "").strip()
                if img:
                    e["image_url"] = img
    except Exception:
        pass

    data = {"ts": _time.time(), "sp": sp, "by_asin": by_asin,
            "skus": skus, "asins": set(by_asin.keys()), "acct": acct}
    _acct_listings_cache[cid] = data
    return data


def _build_parent_groups(products, spapi_by_asin):
    """Group flat product rows by their Parent ASIN (variation families).

    Returns a list of groups, each:
      {parent_asin, title, image_url, variation_theme, child_count,
       is_family (bool), <aggregated metrics>, children: [product, ...]}

    A product with no parent (or whose parent has only itself advertised) becomes
    a single-child group flagged is_family=False, so the frontend can render it as
    a normal row instead of an extra nesting level.
    """
    by_parent = defaultdict(list)
    for p in products:
        key = p.get("parent_asin") or p["asin"]   # standalone → keyed by itself
        by_parent[key].append(p)

    groups = []
    for parent_asin, children in by_parent.items():
        children.sort(key=lambda c: c["spend"], reverse=True)
        is_family = (len(children) > 1) or any(c.get("parent_asin") for c in children)

        agg = {
            "impressions": sum(c["impressions"] for c in children),
            "clicks":      sum(c["clicks"]      for c in children),
            "spend":       round(sum(c["spend"]    for c in children), 2),
            "ad_sales":    round(sum(c["ad_sales"] for c in children), 2),
            "orders":      sum(c["orders"]      for c in children),
        }
        agg["ctr"]     = round(agg["clicks"] / agg["impressions"], 4) if agg["impressions"] else 0
        agg["acos"]    = round(agg["spend"] / agg["ad_sales"], 4) if agg["ad_sales"] else None
        agg["avg_coc"] = round(agg["spend"] / agg["orders"], 2) if agg["orders"] else 0
        # Parent header display: prefer the parent ASIN's own catalog title/image,
        # else fall back to the top child's.
        pinfo = spapi_by_asin.get(parent_asin, {})
        top   = children[0]
        inv   = sum((c.get("inventory") or 0) for c in children) if any(c.get("inventory") is not None for c in children) else None

        groups.append({
            "parent_asin":    parent_asin,
            "title":          pinfo.get("catalog_title") or (top["title"] if not is_family else f"Variation family · {top['title']}"),
            "image_url":      pinfo.get("image_url") or top.get("image_url") or f"/dashboard/asin-image/{parent_asin}",
            "variation_theme": top.get("variation_theme", ""),
            "child_count":    len(children),
            "is_family":      is_family,
            "inventory":      inv,
            **agg,
            "children":       children,
        })

    groups.sort(key=lambda g: g["spend"], reverse=True)
    return groups


def _norm_name(s):
    """Normalize an account/profile name for fuzzy matching: lowercase alphanumerics."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _match_spapi_account(profile_name, brand=""):
    """Pick the SP-API account that best matches the selected AdLabs profile.

    Profiles ("Oh Norman! (US)") and SP-API account names ("Oh Norman!") rarely
    match exactly, so compare on normalized names. Returns the account dict, or
    None if nothing connected. Falls back to the first account only when there
    is exactly one (unambiguous).
    """
    try:
        from db import list_accounts
    except Exception:
        return None
    accts = list_accounts(kind="spapi")
    if not accts:
        return None
    if len(accts) == 1:
        return accts[0]

    targets = [_norm_name(profile_name)]
    if brand:
        targets.append(_norm_name(brand))
    targets = [t for t in targets if t]

    best = None
    for a in accts:
        an = _norm_name(a.get("name"))
        if not an:
            continue
        for t in targets:
            # Exact, or either contains the other (handles "ohnorman" vs "ohnormanllc")
            if an == t or an in t or t in an:
                return a
    return best  # None if no confident match → caller skips SP-API rather than guess


def _catalog_image(sp_client, asin):
    """Fetch the MAIN image URL for one ASIN via SP-API Catalog Items v2022-04-01.
    Returns URL string or None."""
    try:
        params = {
            "identifiers":     asin,
            "identifiersType": "ASIN",
            "marketplaceIds":  sp_client.marketplace_id,
            "includedData":    "images",
        }
        resp = requests.get(
            sp_client.endpoint + "/catalog/2022-04-01/items",
            headers=sp_client._headers(),
            params=params,
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        for item in (resp.json().get("items") or []):
            for ig in (item.get("images") or []):
                for img in (ig.get("images") or []):
                    if img.get("variant") == "MAIN":
                        return img.get("link")
    except Exception:
        pass
    return None


def _bulk_catalog_data(sp_client, asins):
    """Batch-fetch image, Sales Rank, title and parent ASIN for up to N ASINs.

    Returns {asin: {"image_url", "rank", "rank_category", "title", "parent_asin",
                    "variation_theme"}}.
    Sales Rank uses displayGroupRanks[0] (the Seller Central 'Sales rank N in
    <Category>'); parent_asin comes from the VARIATION relationship.
    """
    result = {}
    for i in range(0, len(asins), 20):
        batch = asins[i:i + 20]
        try:
            params = {
                "identifiers":     ",".join(batch),
                "identifiersType": "ASIN",
                "marketplaceIds":  sp_client.marketplace_id,
                "includedData":    "images,salesRanks,relationships,summaries",
            }
            resp = requests.get(
                sp_client.endpoint + "/catalog/2022-04-01/items",
                headers=sp_client._headers(),
                params=params,
                timeout=20,
            )
            if resp.status_code != 200:
                continue
            for item in (resp.json().get("items") or []):
                asin = (item.get("asin") or "").strip().upper()
                if not asin:
                    continue
                entry = result.setdefault(asin, {
                    "image_url": None, "rank": None, "rank_category": "",
                    "title": "", "parent_asin": None, "variation_theme": ""})
                # MAIN image
                for ig in (item.get("images") or []):
                    for img in (ig.get("images") or []):
                        if img.get("variant") == "MAIN" and img.get("link"):
                            entry["image_url"] = img["link"]
                            break
                    if entry["image_url"]:
                        break
                # Sales Rank
                for sr in (item.get("salesRanks") or []):
                    dg = sr.get("displayGroupRanks") or []
                    cr = sr.get("classificationRanks") or []
                    pick = (dg[0] if dg else (cr[0] if cr else None))
                    if pick and pick.get("rank") is not None:
                        try:
                            entry["rank"] = int(pick["rank"])
                            entry["rank_category"] = pick.get("title", "")
                        except (TypeError, ValueError):
                            pass
                        break
                # Title (from summaries)
                for s in (item.get("summaries") or []):
                    if s.get("itemName"):
                        entry["title"] = s["itemName"]
                        break
                # Parent ASIN (from VARIATION relationship)
                for rg in (item.get("relationships") or []):
                    for rel in (rg.get("relationships") or []):
                        if rel.get("type") == "VARIATION":
                            parents = rel.get("parentAsins") or []
                            if parents:
                                entry["parent_asin"] = (parents[0] or "").strip().upper()
                                vt = rel.get("variationTheme") or {}
                                entry["variation_theme"] = vt.get("theme", "")
                                break
                    if entry["parent_asin"]:
                        break
        except Exception:
            continue
    return result


# ─── routes ──────────────────────────────────────────────────────────────────

@bp.route("")
def page():
    return render_template("dashboard.html")


_SVG_PLACEHOLDER = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="52" height="52" viewBox="0 0 52 52">'
    '<rect width="52" height="52" rx="6" fill="#1e293b"/>'
    '<rect x="10" y="10" width="32" height="28" rx="3" fill="none" stroke="#334155" stroke-width="1.5"/>'
    '<path d="M16 32 l6-8 4 5 3-3 7 6" fill="none" stroke="#475569" stroke-width="1.5" stroke-linejoin="round"/>'
    '<circle cx="20" cy="20" r="3" fill="#475569"/>'
    '</svg>'
)


@bp.route("/asin-image/<asin>")
def asin_image(asin):
    """Return product image for an ASIN.

    Priority:
      1. In-process cache  (if we already fetched this ASIN's real image)
      2. SP-API Catalog Items API  (when SP-API account is connected)
      3. SVG placeholder  (never a broken image — clean fallback)
    """
    asin = asin.strip().upper()

    # 1 — Serve from cache
    cached = _img_cache.get(asin)
    if cached:
        return redirect(cached)

    # 2 — Try SP-API Catalog Items API (fast, 10–15 s at most)
    try:
        from db import list_accounts, get_account_refresh_token
        from utils.spapi_client import SpApiClient
        accts = list_accounts(kind="spapi")
        if accts:
            acct = accts[0]
            rt   = get_account_refresh_token(acct["id"])
            sp   = SpApiClient(rt, marketplace_id=acct.get("marketplace_id"))
            url  = _catalog_image(sp, asin)
            if url:
                _img_cache[asin] = url
                return redirect(url)
    except Exception:
        pass

    # 3 — Return inline SVG placeholder (never a broken <img>)
    return Response(_SVG_PLACEHOLDER, mimetype="image/svg+xml",
                    headers={"Cache-Control": "max-age=86400"})


# ── main load ──────────────────────────────────────────────────────────────

@bp.route("/load", methods=["POST"])
def load():
    body        = request.get_json() or {}
    team_id     = body.get("team_id")
    slug        = body.get("slug")
    if not team_id or not slug:
        return jsonify({"success": False, "error": "team_id and slug required"}), 400

    profile_name = body.get("profile_name", "")   # used to match the SP-API account
    brand        = body.get("brand", "")
    lookback    = int(body.get("lookback_days", 30))
    start_date  = body.get("start_date")
    end_date    = body.get("end_date")
    filters, range_label = _date_filters_dash(lookback, start_date, end_date)

    def work(progress):
        progress("Resolving profile…")
        res = _adlabs.read_resource(f"adlabs://profiles/{slug}")
        m   = _PROFILE_ID_RE.search(res)
        if not m:
            raise AdLabsError("Could not resolve profile_id")
        profile_id = m.group(1)

        # ── SP-API enrichment (works for ANY AdLabs profile) ─────────────────
        # We do NOT rely on the profile name matching an SP-API account name.
        # Instead we pull listings for EVERY connected SP-API account (in
        # parallel, cached), then after AdLabs gives us the advertised ASINs we
        # pick the account that actually owns them (max ASIN overlap) and use it
        # for price, inventory, image and Sales Rank. This makes inventory /
        # price / image / rank appear for all profiles, not just name-matches.
        spapi_by_asin: dict = {}
        sp_error: list = []
        sp_holder: dict = {"per_acct": []}   # [{acct, listings_data}]

        def _fetch_all_listings():
            try:
                from db import list_accounts
                accts = list_accounts(kind="spapi")
                if not accts:
                    sp_error.append("no SP-API accounts connected")
                    return
                results = [None] * len(accts)

                def _one(idx, a):
                    try:
                        results[idx] = (a, _account_listings(a))
                    except Exception:
                        results[idx] = None

                threads = [threading.Thread(target=_one, args=(i, a), daemon=True)
                           for i, a in enumerate(accts)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=90)
                sp_holder["per_acct"] = [r for r in results if r]
            except Exception as exc:
                sp_error.append(str(exc))

        sp_thread = threading.Thread(target=_fetch_all_listings, daemon=True)
        sp_thread.start()

        # NOTE: AdLabs' `read` tool hard-caps at 100 rows regardless of `limit`.
        # Accounts here have hundreds of campaigns / ad groups / products, so we
        # MUST use download_rows (CSV export, no cap) for every entity — otherwise
        # the campaign↔keyword hierarchy is truncated to 100 and most campaigns
        # show no targeting.

        # 1 — Advertised products (ASIN → ad_group mapping)
        progress("Loading products…")
        ap_ref  = _adlabs.first_reference(
            _adlabs.get_entity_data("advertised_product",
                                    team_id=int(team_id), profile_id=profile_id,
                                    filters=filters))
        ap_rows = _adlabs.download_rows(ap_ref) if ap_ref else []

        # 2 — Ad groups (campaign + ad-group names)
        progress("Loading ad groups…")
        adg_ref  = _adlabs.first_reference(
            _adlabs.get_entity_data("ad_group",
                                    team_id=int(team_id), profile_id=profile_id,
                                    filters=filters))
        adg_rows = _adlabs.download_rows(adg_ref) if adg_ref else []

        # 3 — Campaigns with metrics
        progress("Loading campaigns…")
        try:
            camp_ref  = _adlabs.first_reference(
                _adlabs.get_entity_data("campaign",
                                        team_id=int(team_id), profile_id=profile_id,
                                        filters=filters))
            camp_rows = _adlabs.download_rows(camp_ref) if camp_ref else []
        except AdLabsError:
            camp_rows = []

        # 4 — All keywords/targets (full CSV download, no row cap).
        progress("Loading keywords…")
        t_ref  = _adlabs.first_reference(
            _adlabs.get_entity_data("target",
                                    team_id=int(team_id), profile_id=profile_id,
                                    filters=filters))
        t_rows = _adlabs.download_rows(t_ref) if t_ref else []

        # Advertised ASINs in this profile
        ad_asins = sorted({(r.get("asin") or r.get("product_asin") or "").strip().upper()
                           for r in ap_rows
                           if (r.get("asin") or r.get("product_asin"))})

        # Wait for SP-API listings (all accounts, in parallel with AdLabs above).
        progress("Merging inventory, pricing & rank…")
        sp_thread.join(timeout=120)

        # ── Pick the SP-API account that OWNS these ASINs ───────────────────
        # By ASIN overlap, not profile-name matching — so every profile works.
        per_acct = sp_holder.get("per_acct", [])
        ad_set   = set(ad_asins)
        owner    = None
        best_overlap = 0
        for a, data in per_acct:
            overlap = len(ad_set & data.get("asins", set()))
            if overlap > best_overlap:
                best_overlap = overlap
                owner = (a, data)
        # Fallback: if no overlap (e.g. listings lag), try the name match, else
        # the first connected account (catalog data is public, so still useful).
        if not owner and per_acct:
            named = _match_spapi_account(profile_name, brand)
            if named:
                owner = next(((a, d) for a, d in per_acct if a["id"] == named["id"]), per_acct[0])
            else:
                owner = per_acct[0]

        if owner:
            acct, data = owner
            sp = data["sp"]

            # Price + image from the owning account's listings
            for asin in ad_asins:
                info = data["by_asin"].get(asin, {})
                entry = spapi_by_asin.setdefault(asin, {})
                if info.get("price") and not entry.get("price"):
                    entry["price"] = info["price"]
                if info.get("image_url") and not entry.get("image_url"):
                    entry["image_url"] = info["image_url"]
                    _img_cache[asin] = info["image_url"]

            # Inventory: Seller-Central 'On-hand (FBA)' via SKU-filtered summaries
            try:
                for asin, on_hand in _fetch_inventory(sp, data.get("skus", [])).items():
                    spapi_by_asin.setdefault(asin, {})["inventory"] = on_hand
            except Exception:
                pass

            # Catalog data (image, Sales Rank, title, parent ASIN) for the
            # advertised ASINs. Public data, so any connected account can fetch it.
            if ad_asins:
                progress("Fetching product images, rank & variations…")
                try:
                    cat = _bulk_catalog_data(sp, ad_asins)
                    # Also resolve the parent ASINs' own catalog (title + image) so
                    # parent group headers can show a real name/photo.
                    parent_asins = sorted({info["parent_asin"] for info in cat.values()
                                           if info.get("parent_asin")
                                           and info["parent_asin"] not in cat})
                    if parent_asins:
                        cat.update(_bulk_catalog_data(sp, parent_asins))
                    for a, info in cat.items():
                        a = a.upper()
                        entry = spapi_by_asin.setdefault(a, {})
                        if info.get("image_url") and not entry.get("image_url"):
                            entry["image_url"] = info["image_url"]
                            _img_cache[a] = info["image_url"]
                        if info.get("rank") is not None:
                            entry["sales_rank"]    = info["rank"]
                            entry["rank_category"] = info.get("rank_category", "")
                        if info.get("parent_asin"):
                            entry["parent_asin"]     = info["parent_asin"]
                            entry["variation_theme"] = info.get("variation_theme", "")
                        if info.get("title"):
                            entry["catalog_title"] = info["title"]
                except Exception:
                    pass

        # ── Build indices ────────────────────────────────────────────────────

        # campaign_id → campaign row
        camps_by_id = {r.get("campaign_id"): r
                       for r in camp_rows if r.get("campaign_id")}

        # ad_group_id → {campaign_id, campaign_name}
        adg_by_id = {}
        for r in adg_rows:
            agid = r.get("ad_group_id")
            if agid:
                adg_by_id[agid] = {
                    "campaign_id":   r.get("campaign_id", ""),
                    "campaign_name": r.get("campaign_name", ""),
                }

        # ASIN → {title, ad_group_ids, campaign_ids}
        products_map = {}
        for r in ap_rows:
            asin = (r.get("asin") or r.get("product_asin") or "").strip()
            agid = (r.get("ad_group_id") or "").strip()
            if not asin:
                continue
            if asin not in products_map:
                sp_info = spapi_by_asin.get(asin, {})
                products_map[asin] = {
                    "asin":         asin,
                    "title":        r.get("title") or r.get("product_title") or asin,
                    "brand":        r.get("brand") or "",
                    "image_url":    sp_info.get("image_url") or f"/dashboard/asin-image/{asin}",
                    "ad_group_ids": set(),
                    "campaign_ids": set(),
                }
            if agid:
                products_map[asin]["ad_group_ids"].add(agid)
                cid = adg_by_id.get(agid, {}).get("campaign_id")
                if cid:
                    products_map[asin]["campaign_ids"].add(cid)

        # ── Build targets indexed by campaign ────────────────────────────────
        # keyword_text → set of campaign names (for cross-campaign info)
        kw_camps = defaultdict(lambda: {"ids": set(), "names": set()})
        targets_by_camp = defaultdict(list)   # campaign_id → [target_dict]

        for r in t_rows:
            cid    = r.get("campaign_id") or ""
            kw     = (r.get("targeting") or r.get("keyword_text") or "").strip()
            match  = (r.get("match_types") or r.get("match_type") or "").strip().upper()
            bid    = _f(r.get("bid"))
            impr   = _f(r.get("impressions"))
            impr_p = _f(r.get("compare_impressions") or r.get("prev_impressions"))
            clicks = _f(r.get("clicks"))
            spend  = _f(r.get("spend"))
            sales  = _f(r.get("sales"))
            orders = _f(r.get("orders"))
            acos   = (spend / sales) if sales > 0 else None
            # Impression share is NOT fetched from AdLabs — it is fetched from the
            # Amazon Ads API v3 (topOfSearchImpressionShare) after the target loop
            # and merged in. Placeholder None here; filled below.
            tos = None
            state  = (r.get("target_state") or r.get("state") or "Enabled").strip()
            cname  = (r.get("campaign_name") or "").strip()
            ctype  = _camp_type(cname, r.get("campaign_ad_type") or "")

            tgt = {
                "target_id":          r.get("target_id", ""),
                "keyword":            kw,
                "match_type":         match,
                "state":              state,
                "bid":                round(bid, 2),
                "impressions":        int(impr),
                "impressions_prev":   int(impr_p),
                "impression_share":   None,   # filled by Ads API IS fetch below
                "clicks":             int(clicks),
                "spend":              round(spend, 2),
                "sales":              round(sales, 2),
                "orders":             int(orders),
                "acos":               round(acos, 4) if acos is not None else None,
                "ctr":                round(clicks / impr, 4) if impr > 0 else 0,
                "rpc":                round(sales / clicks, 4) if clicks > 0 else 0,
                "campaign_id":        cid,
                "campaign_name":      cname,
                "campaign_type":      ctype,
                "ad_group_id":        r.get("ad_group_id") or "",
                "last_bid_date":      (r.get("last_optimized_at") or "").strip(),
                "last_bid_note":      (r.get("last_optimized_note") or "").strip(),
                # cross-campaign filled after full scan
                "cross_campaign_ids":   [],
                "cross_campaign_names": [],
            }
            if cid:
                targets_by_camp[cid].append(tgt)
            if kw:
                kw_camps[kw]["ids"].add(cid)
                kw_camps[kw]["names"].add(cname)

        # IS (topOfSearchImpressionShare) is fetched via a separate /dashboard/is
        # endpoint after the main load completes — Amazon's async report takes
        # 2–5 minutes and must not block the initial page render.

        # Enrich cross-campaign info
        for cid, tgts in targets_by_camp.items():
            for t in tgts:
                kw    = t["keyword"]
                info  = kw_camps.get(kw, {"ids": set(), "names": set()})
                t["cross_campaign_ids"]   = [x for x in info["ids"]   if x != cid]
                t["cross_campaign_names"] = [x for x in info["names"] if x != t["campaign_name"]]

        # ── Build campaign objects ───────────────────────────────────────────
        def _build_camp(cid, fallback_name="", fallback_type="SP"):
            crow    = camps_by_id.get(cid, {})
            spend   = _f(crow.get("spend"))
            sales   = _f(crow.get("sales"))
            impr    = _f(crow.get("impressions"))
            clicks  = _f(crow.get("clicks"))
            orders  = _f(crow.get("orders"))
            cname   = crow.get("campaign_name") or fallback_name or cid
            ctype   = _camp_type(cname, crow.get("campaign_type") or crow.get("ad_type") or "")
            if ctype == "SP" and fallback_type != "SP":
                ctype = fallback_type
            tgts    = targets_by_camp.get(cid, [])

            # If campaign not in camps_by_id, aggregate from targets
            if not crow and tgts:
                spend  = sum(t["spend"]  for t in tgts)
                sales  = sum(t["sales"]  for t in tgts)
                impr   = sum(t["impressions"] for t in tgts)
                clicks = sum(t["clicks"] for t in tgts)
                orders = sum(t["orders"] for t in tgts)

            return {
                "campaign_id":   cid,
                "campaign_name": cname,
                "campaign_type": ctype,
                "state":         crow.get("campaign_state") or "Enabled",
                "budget":        _f(crow.get("budget") or crow.get("daily_budget")),
                "impressions":   int(impr),
                "clicks":        int(clicks),
                "spend":         round(spend, 2),
                "sales":         round(sales, 2),
                "orders":        int(orders),
                "acos":          round(spend / sales, 4) if sales > 0 else None,
                "ctr":           round(clicks / impr, 4) if impr > 0 else 0,
                "cvr":           round(orders / clicks, 4) if clicks > 0 else 0,
                "targets":       tgts,
            }

        # ── Build product hierarchy ──────────────────────────────────────────
        products = []
        for asin, pdata in products_map.items():
            camp_ids  = pdata["campaign_ids"]
            camp_objs = [_build_camp(cid) for cid in camp_ids]

            # If no campaigns found via ad_group, scan targets directly
            if not camp_objs:
                found_ids = {t["campaign_id"]
                             for tlist in targets_by_camp.values()
                             for t in tlist
                             if t.get("ad_group_id") in pdata["ad_group_ids"]}
                camp_objs = [_build_camp(cid) for cid in found_ids]

            by_type = {"SP": [], "SB": [], "SD": []}
            for c in camp_objs:
                by_type[c["campaign_type"]].append(c)
            for ct in by_type:
                by_type[ct].sort(key=lambda x: x["spend"], reverse=True)

            total_impr   = sum(c["impressions"] for c in camp_objs)
            total_clicks = sum(c["clicks"]      for c in camp_objs)
            total_spend  = sum(c["spend"]       for c in camp_objs)
            total_sales  = sum(c["sales"]       for c in camp_objs)
            total_orders = sum(c["orders"]      for c in camp_objs)

            sp_info = spapi_by_asin.get(asin, {})

            products.append({
                "asin":          asin,
                "title":         pdata["title"],
                "brand":         pdata["brand"],
                "image_url":     pdata["image_url"],
                "inventory":     sp_info.get("inventory"),
                "price":         sp_info.get("price"),
                "sales_rank":    sp_info.get("sales_rank"),
                "rank_category": sp_info.get("rank_category", ""),
                "parent_asin":   sp_info.get("parent_asin"),
                "variation_theme": sp_info.get("variation_theme", ""),
                # Ad metrics
                "impressions":   total_impr,
                "clicks":        total_clicks,
                "ctr":           round(total_clicks / total_impr, 4) if total_impr > 0 else 0,
                "spend":         round(total_spend, 2),
                "ad_sales":      round(total_sales, 2),
                "orders":        total_orders,
                "acos":          round(total_spend / total_sales, 4) if total_sales > 0 else None,
                "avg_coc":       round(total_spend / total_orders, 2) if total_orders > 0 else 0,
                "roas":          round(total_sales / total_spend, 2) if total_spend > 0 else 0,
                "organic_sales": None,   # requires SP-API Business Report (not loaded here)
                # Hierarchy
                "campaigns":       by_type,
                "campaign_count":  len(camp_objs),
            })

        products.sort(key=lambda p: p["spend"], reverse=True)

        # ── Group products by Parent ASIN (variation families) ───────────────
        parent_groups = _build_parent_groups(products, spapi_by_asin)

        total_spend = round(sum(p["spend"]    for p in products), 2)
        total_sales = round(sum(p["ad_sales"] for p in products), 2)
        stats = {
            "products":    len(products),
            "parents":     len(parent_groups),
            "campaigns":   len(camps_by_id) or len({t["campaign_id"] for tgts in targets_by_camp.values() for t in tgts}),
            "keywords":    len(t_rows),
            "total_spend": total_spend,
            "total_sales": total_sales,
            "total_acos":  round(total_spend / total_sales, 4) if total_sales > 0 else None,
        }
        return {
            "profile_id":  profile_id,
            "range_label": range_label,
            "products":    products,         # flat list (kept for AI + back-compat)
            "parent_groups": parent_groups,  # parent → children hierarchy
            "stats":       stats,
        }

    return jsonify({"success": True, "job_id": jobs.start(work)})


@bp.route("/load/<job_id>")
def load_status(job_id):
    s = jobs.public_status(job_id)
    if not s:
        return jsonify({"success": False, "error": "Unknown job"}), 404
    return jsonify({"success": True, **s})


@bp.route("/load/<job_id>/data")
def load_data(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"success": False, "error": "Unknown job"}), 404
    if job["state"] != "done":
        return jsonify({"success": False, "error": "Not ready"}), 409
    return jsonify(convert_numpy({"success": True, **job["result"]}))


# ── search terms (lazy, per campaign) ──────────────────────────────────────

@bp.route("/search-terms", methods=["POST"])
def search_terms():
    body        = request.get_json() or {}
    team_id     = body.get("team_id")
    profile_id  = body.get("profile_id")
    campaign_id = body.get("campaign_id")
    # Rebuild filters server-side from date params (never trust raw filters from client)
    lookback    = int(body.get("lookback_days", 30))
    start_date  = body.get("start_date")
    end_date    = body.get("end_date")
    filters, _  = _date_filters_dash(lookback, start_date, end_date)

    if not team_id or not profile_id or not campaign_id:
        return jsonify({"success": False, "error": "team_id, profile_id, campaign_id required"}), 400

    def work(progress):
        progress("Fetching search terms…")
        st_out = _adlabs.get_entity_data("search_term",
                                         team_id=int(team_id), profile_id=profile_id,
                                         filters=filters)
        st_ref  = _adlabs.first_reference(st_out)
        if not st_ref:
            return {"rows": []}
        # Filter to this campaign
        sub = _adlabs.first_reference(
            _adlabs.query(st_ref,
                          f"SELECT * FROM reference_data WHERE campaign_id='{campaign_id}' "
                          "ORDER BY spend DESC LIMIT 300"))
        if not sub:
            return {"rows": []}
        rows = _adlabs.download_rows(sub)

        out = []
        for r in rows:
            spend  = _f(r.get("spend"))
            sales  = _f(r.get("sales"))
            clicks = _f(r.get("clicks"))
            orders = _f(r.get("orders"))
            acos   = (spend / sales) if sales > 0 else None
            out.append({
                "search_term":  (r.get("search_term") or "").strip(),
                "impressions":  int(_f(r.get("impressions"))),
                "clicks":       int(clicks),
                "spend":        round(spend, 2),
                "sales":        round(sales, 2),
                "orders":       int(orders),
                "acos":         round(acos, 4) if acos is not None else None,
                "ctr":          round(clicks / _f(r.get("impressions"), 1), 4) if _f(r.get("impressions")) > 0 else 0,
                "cvr":          round(orders / clicks, 4) if clicks > 0 else 0,
                "cpc":          round(spend / clicks, 2) if clicks > 0 else 0,
                "match_types":  (r.get("match_types") or "").strip(),
            })
        out.sort(key=lambda x: x["spend"], reverse=True)
        return {"rows": out}

    return jsonify({"success": True, "job_id": jobs.start(work)})


@bp.route("/search-terms/<job_id>")
def search_terms_status(job_id):
    s = jobs.public_status(job_id)
    if not s:
        return jsonify({"success": False, "error": "Unknown job"}), 404
    return jsonify({"success": True, **s})


@bp.route("/search-terms/<job_id>/data")
def search_terms_data(job_id):
    job = jobs.get(job_id)
    if not job or job["state"] != "done":
        return jsonify({"success": False, "error": "Not ready"}), 409
    return jsonify(convert_numpy({"success": True, **job["result"]}))


# ── SQP ────────────────────────────────────────────────────────────────────

@bp.route("/sqp", methods=["POST"])
def sqp():
    body       = request.get_json() or {}
    team_id    = body.get("team_id")
    profile_id = body.get("profile_id")
    lookback   = int(body.get("lookback_days", 30))
    start_date = body.get("start_date")
    end_date   = body.get("end_date")
    filters, _ = _date_filters_dash(lookback, start_date, end_date)

    if not team_id or not profile_id:
        return jsonify({"success": False, "error": "team_id and profile_id required"}), 400

    def work(progress):
        progress("Fetching SQP data…")
        try:
            sqp_out = _adlabs.get_entity_data("search_query",
                                              team_id=int(team_id), profile_id=profile_id,
                                              filters=filters)
            sqp_ref = _adlabs.first_reference(sqp_out)
            if not sqp_ref:
                return {"rows": []}
            sub = _adlabs.first_reference(
                _adlabs.query(sqp_ref,
                              "SELECT * FROM reference_data ORDER BY search_query_volume DESC LIMIT 200"))
            rows = AdLabsClient.parse_table(_adlabs.read(sub, limit=200)) if sub else []
        except AdLabsError:
            return {"rows": []}

        out = []
        for r in rows:
            q = (r.get("search_query") or "").strip()
            if not q:
                continue
            out.append({
                "search_query":       q,
                "volume":             int(_f(r.get("search_query_volume"))),
                "asin":               r.get("asin", ""),
                "brand":              r.get("brand", ""),
                "title":              r.get("title", ""),
                "purchases":          int(_f(r.get("asin_purchase_count"))),
                "clicks":             int(_f(r.get("asin_click_count") or r.get("asin_clicks"))),
                "conversion":         round(_f(r.get("asin_conversion_rate")), 4),
                "purchase_share":     round(_f(r.get("asin_purchase_share")), 4),
                "click_share":        round(_f(r.get("asin_click_share")), 4),
                "impressions":        int(_f(r.get("asin_impression_count") or r.get("asin_impressions"))),
                "impression_share":   round(_f(r.get("asin_impression_share")), 4),
                "cart_adds":          int(_f(r.get("asin_cart_add_count") or 0)),
                "cart_add_share":     round(_f(r.get("asin_cart_add_share") or 0), 4),
                "targeted":           bool((r.get("existing_targets") or "").strip()),
            })
        return {"rows": out}

    return jsonify({"success": True, "job_id": jobs.start(work)})


@bp.route("/sqp/<job_id>")
def sqp_status(job_id):
    s = jobs.public_status(job_id)
    if not s:
        return jsonify({"success": False, "error": "Unknown job"}), 404
    return jsonify({"success": True, **s})


@bp.route("/sqp/<job_id>/data")
def sqp_data(job_id):
    job = jobs.get(job_id)
    if not job or job["state"] != "done":
        return jsonify({"success": False, "error": "Not ready"}), 409
    return jsonify(convert_numpy({"success": True, **job["result"]}))


# ── AI insight ─────────────────────────────────────────────────────────────
# The model has high latency (cold-start can be 30–60 s+), so this runs as a
# background job and the frontend polls — avoids HTTP / proxy request timeouts.

def _normalize_acos(obj):
    """Recursively convert any raw ACOS fraction to an explicit percentage.

    Defends against older/cached frontends that send acos as a fraction (0.43).
    Every `acos`/`*_acos` key is converted to `<key>_percent` (rounded, ×100)
    and the raw key removed, so the model only ever sees percentages.
    `acos_percent` keys already in percentage form are left untouched.
    """
    if isinstance(obj, dict):
        for k in [k for k in obj if k == "acos" or (k.endswith("_acos"))]:
            v = obj.pop(k)
            try:
                if v is not None:
                    obj[k + "_percent"] = round(float(v) * 100, 1)
            except (TypeError, ValueError):
                pass
        for v in obj.values():
            _normalize_acos(v)
    elif isinstance(obj, list):
        for item in obj:
            _normalize_acos(item)
    return obj


@bp.route("/ai-insight", methods=["POST"])
def ai_insight():
    body    = request.get_json() or {}
    context = body.get("context", "")      # what the user is looking at
    data    = _normalize_acos(body.get("data", {}))   # acos → percentage, always

    prompt = (
        "Analyze this Amazon Advertising account and write a clear, valuable report.\n\n"
        "Write EXACTLY these four sections, using '## ' before each section title:\n"
        "## Summary\n"
        "One short paragraph: overall ACOS health, total spend vs sales, and the single "
        "biggest opportunity or risk.\n"
        "## Wasted Spend\n"
        "Bullet list. Name the specific keywords/campaigns burning money (high ACOS or "
        "spend with no sales). For each, give the exact metric and a one-line action "
        "starting with 'Action:'.\n"
        "## Scale Winners\n"
        "Bullet list. Name the specific keywords/campaigns that are profitable (low ACOS, "
        "real sales). For each, give the metric and an 'Action:' to scale (raise bid, "
        "harvest, expand).\n"
        "## Priorities\n"
        "A short numbered list of the top 3 actions to take this week, most impactful first.\n\n"
        "Rules: be specific with real names and numbers from the data. Quantify every claim "
        "(ACOS %, $ spend, $ sales). Keep each bullet to 1-2 sentences. Do NOT invent data "
        "not present below. Skip a section only if there is genuinely nothing to report.\n\n"
        "DATA UNITS — read carefully:\n"
        "- Every field ending in '_percent' is ALREADY a percentage. Quote it directly with "
        "a % sign. Example: acos_percent: 89.1 means the ACOS is 89.1%. Do NOT divide, "
        "multiply, or reinterpret these values.\n"
        "- Every field ending in '_usd' is a US dollar amount. Quote it with a $ sign.\n"
        "- ACOS = ad spend ÷ ad sales. A high acos_percent (e.g. 80+) is BAD (unprofitable); "
        "a low one (e.g. under 30) is GOOD. Never describe a high ACOS as 'low' or vice versa.\n\n"
        f"Context: {context}\n\n"
        f"Data (JSON):\n{json.dumps(data, default=str)[:4000]}"
    )

    def work(progress):
        progress("Analyzing data…")
        # gemma-3n-e4b-it has no system role — prepend the persona to the prompt.
        full_prompt = ("You are an expert Amazon Advertising (PPC) analyst writing a "
                       "concise, actionable report for a seller.\n\n" + prompt)
        resp = requests.post(
            _NVIDIA_API_URL,
            headers={"Authorization": f"Bearer {_NVIDIA_API_KEY}",
                     "Accept": "application/json",
                     "Content-Type": "application/json"},
            json={"model": _NVIDIA_MODEL,
                  "messages": [{"role": "user", "content": full_prompt}],
                  "max_tokens": 900,
                  "temperature": 0.20,
                  "top_p": 0.70,
                  "frequency_penalty": 0.0,
                  "presence_penalty": 0.0,
                  "stream": False},
            timeout=120,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"AI API returned {resp.status_code}: {resp.text[:200]}")
        choices = resp.json().get("choices") or []
        if not choices:
            raise RuntimeError("AI API returned no choices")
        msg = choices[0].get("message", {}) or {}
        text = (msg.get("content") or msg.get("reasoning_content") or "").strip()
        if not text:
            raise RuntimeError("AI returned an empty response")
        return {"insight": text}

    return jsonify({"success": True, "job_id": jobs.start(work)})


@bp.route("/ai-insight/<job_id>")
def ai_insight_status(job_id):
    s = jobs.public_status(job_id)
    if not s:
        return jsonify({"success": False, "error": "Unknown job"}), 404
    return jsonify({"success": True, **s})


@bp.route("/ai-insight/<job_id>/data")
def ai_insight_data(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"success": False, "error": "Unknown job"}), 404
    if job["state"] != "done":
        return jsonify({"success": False, "error": "Not ready"}), 409
    return jsonify({"success": True, **job["result"]})
