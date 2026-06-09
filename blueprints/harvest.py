"""Keyword Harvesting section — powered by AdLabs.

Step 1 (Harvest & Negate): pull search-term + SQP data, split into keywords worth
harvesting (converting, ACOS <= target) and terms worth negating (wasted spend /
too high ACOS), score relevancy with AI so irrelevant terms can be negated too.

Step 2 (Apply): add selected keywords to their ad groups (create_entities target)
and create negatives for selected terms (create_entities negative_targeting).

Reuses the shared AdLabs client + profile list from the Ads blueprint.
"""

import json
import re
from collections import defaultdict
from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, jsonify

from utils import jobs
from utils.adlabs_client import AdLabsClient, AdLabsError
from utils.keyword_harvester import categorize_search_terms, sqp_opportunities
from utils.ai_client import keyword_relevancy, harvest_summary, keyword_brand_flags
from utils.jsonutil import convert_numpy
from blueprints.ads import _adlabs, _date_filters


def _filters(lookback, start, end):
    """Build AdLabs DATE/COMPARE_DATE filters from a preset lookback OR a custom
    start/end range (YYYY-MM-DD). COMPARE is the immediately-preceding equal window."""
    if start and end:
        try:
            s = datetime.strptime(start, "%Y-%m-%d").date()
            e = datetime.strptime(end, "%Y-%m-%d").date()
        except ValueError:
            return _date_filters(lookback), None
        if e < s:
            s, e = e, s
        days = (e - s).days + 1
        c_end = s - timedelta(days=1)
        c_start = c_end - timedelta(days=days - 1)

        def blk(key, a, b):
            return {"key": key, "conditions": [
                {"operator": ">=", "values": [a.isoformat()]},
                {"operator": "<=", "values": [b.isoformat()]}], "logical_operator": "AND"}
        return [blk("DATE", s, e), blk("COMPARE_DATE", c_start, c_end)], f"{s.isoformat()} → {e.isoformat()}"
    return _date_filters(lookback), f"last {lookback} days"

bp = Blueprint("harvest", __name__, url_prefix="/harvest")

_PROFILE_ID_RE = re.compile(r"Profile ID:\s*(\d+)")
# profile_id -> {st_ref, adg_ref, sqp_ref, team_id, target_acos}
_cache = {}


@bp.route("")
def page():
    return render_template("harvest.html")


@bp.route("/analyze", methods=["POST"])
def analyze():
    body = request.get_json() or {}
    team_id = body.get("team_id"); slug = body.get("slug")
    if not team_id or not slug:
        return jsonify({"success": False, "error": "team_id and slug required"}), 400
    target_acos = float(body.get("target_acos", 0.30))
    min_negate_spend = float(body.get("min_negate_spend", 1.0))
    lookback = int(body.get("lookback_days", 30))
    start_date = body.get("start_date")
    end_date = body.get("end_date")
    brand = body.get("brand", "")
    filters, range_label = _filters(lookback, start_date, end_date)

    def work(progress):
        progress("Resolving profile…")
        res = _adlabs.read_resource(f"adlabs://profiles/{slug}")
        m = _PROFILE_ID_RE.search(res)
        if not m:
            raise AdLabsError("Could not resolve profile_id")
        profile_id = m.group(1)

        # --- Search terms ---
        progress("Fetching search terms…")
        st_out = _adlabs.get_entity_data("search_term", team_id=int(team_id),
                                         profile_id=profile_id, filters=filters)
        st_ref = _adlabs.first_reference(st_out)
        progress("Downloading all search terms…")
        # Full set via CSV export (read caps at 100 rows; download has no cap).
        all_rows = _adlabs.download_rows(st_ref)
        cats = categorize_search_terms(all_rows, target_acos, min_negate_spend)
        total_terms = len(cats["all"])
        # Bound the All-terms payload/DOM for very large accounts (counts stay exact).
        if total_terms > 3000:
            cats["all"] = cats["all"][:3000]

        # --- Ad groups (for harvest destinations) ---
        progress("Loading ad groups…")
        adg_out = _adlabs.get_entity_data("ad_group", team_id=int(team_id),
                                          profile_id=profile_id, filters=filters)
        adg_ref = _adlabs.first_reference(adg_out)
        adg_rows = AdLabsClient.parse_table(_adlabs.read(adg_ref, limit=100))
        ad_groups = [{"ad_group_id": r.get("ad_group_id"), "ad_group_name": r.get("ad_group_name"),
                      "campaign_name": r.get("campaign_name"), "campaign_id": r.get("campaign_id")}
                     for r in adg_rows if r.get("ad_group_id")]

        # --- Which product each ad group advertises (ad_group_id -> ASIN/title) ---
        progress("Mapping keywords to products…")
        ag_product = {}
        brand_hints = set()
        if brand:
            brand_hints.add(brand)
        try:
            ap_out = _adlabs.get_entity_data("advertised_product", team_id=int(team_id),
                                             profile_id=profile_id, filters=filters)
            for r in AdLabsClient.parse_table(_adlabs.read(_adlabs.first_reference(ap_out), limit=200)):
                ag = r.get("ad_group_id")
                if ag and ag not in ag_product:
                    ag_product[ag] = {"asin": r.get("asin") or r.get("product_asin") or "",
                                      "title": r.get("title") or r.get("product_title") or ""}
                if r.get("brand"):
                    brand_hints.add(r["brand"].strip())
        except AdLabsError:
            pass

        def _attach_product(rows):
            for x in rows:
                p = ag_product.get(x.get("ad_group_id"))
                if p:
                    x["product_asin"] = p["asin"]
                    x["product_title"] = p["title"]
        _attach_product(cats["all"])
        _attach_product(cats["harvest"])
        _attach_product(cats["negate"])

        # --- SQP opportunities ---
        progress("Fetching SQP opportunities…")
        try:
            sqp_out = _adlabs.get_entity_data("search_query", team_id=int(team_id),
                                              profile_id=profile_id, filters=filters)
            sqp_rows = AdLabsClient.parse_table(_adlabs.read(_adlabs.first_reference(
                _adlabs.query(_adlabs.first_reference(sqp_out),
                              "SELECT * FROM reference_data WHERE asin_purchase_count > 0 "
                              "ORDER BY search_query_volume DESC LIMIT 100")), limit=100))
            sqp = sqp_opportunities(sqp_rows)
            for r in sqp_rows:
                if r.get("brand"):
                    brand_hints.add(r["brand"].strip())
        except AdLabsError:
            sqp = []

        # Real brand(s) come from the products, not just the profile name.
        brand_str = " ".join(sorted(b for b in brand_hints if b)) or brand
        # --- AI relevancy + summary ---
        progress("Scoring relevancy with AI…")
        titles = list(dict.fromkeys(x.get("title") for x in sqp if x.get("title")))[:5]
        context = f"Brand(s): {brand_str or 'unknown'}. Example products: {'; '.join(titles)}"
        rel = keyword_relevancy([h["search_term"] for h in cats["harvest"]], context)
        for h in cats["harvest"]:
            info = rel.get(h["search_term"], {})
            h["relevant"] = info.get("relevant", True)
            h["relevance_reason"] = info.get("reason", "")
        # Branded vs generic across all search terms (AI, with heuristic fallback).
        progress("Classifying branded vs generic…")
        brand_flags = keyword_brand_flags([t["search_term"] for t in cats["all"]], brand_str)
        for t in cats["all"]:
            t["branded"] = brand_flags.get(t["search_term"], False)
        progress("Generating AI summary…")
        brief = harvest_summary(cats["harvest"], cats["negate"], sqp, target_acos)

        _cache[profile_id] = {"st_ref": st_ref, "adg_ref": adg_ref,
                              "team_id": int(team_id), "target_acos": target_acos}
        irrelevant = sum(1 for h in cats["harvest"] if not h.get("relevant", True))
        return {
            "profile_id": profile_id, "harvest": cats["harvest"], "negate": cats["negate"],
            "all_terms": cats["all"], "sqp": sqp, "ad_groups": ad_groups,
            "summary": brief, "range_label": range_label,
            "stats": {
                "all": total_terms, "shown": len(cats["all"]),
                "harvest": len(cats["harvest"]), "negate": len(cats["negate"]),
                "sqp": len(sqp), "irrelevant": irrelevant,
                "harvest_sales": round(sum(x["sales"] for x in cats["harvest"]), 2),
                "negate_spend": round(sum(x["spend"] for x in cats["negate"]), 2),
            },
        }

    return jsonify({"success": True, "job_id": jobs.start(work)})


@bp.route("/analyze/<job_id>")
def analyze_status(job_id):
    s = jobs.public_status(job_id)
    if not s:
        return jsonify({"success": False, "error": "Unknown job"}), 404
    return jsonify({"success": True, **s})


@bp.route("/analyze/<job_id>/data")
def analyze_data(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"success": False, "error": "Unknown job"}), 404
    if job["state"] != "done":
        return jsonify({"success": False, "error": "Not ready"}), 409
    return jsonify(convert_numpy({"success": True, **job["result"]}))


@bp.route("/apply", methods=["POST"])
def apply():
    body = request.get_json() or {}
    profile_id = body.get("profile_id")
    cache = _cache.get(profile_id)
    if not cache:
        return jsonify({"success": False, "error": "No analysis in memory — run Analyze first."}), 409
    team_id = cache["team_id"]
    harvest = body.get("harvest") or []      # [{search_term, ad_group_id, bid}]
    negate = body.get("negate") or []        # [{search_term_id, ...}]
    note = "Keyword harvesting via N-Gram Suite"
    applied = {"harvested": 0, "negated": 0}
    errors = []

    # Harvest: group by (ad_group_id, match, bid), create keyword targets per group.
    try:
        groups = defaultdict(list)
        for h in harvest:
            match = (h.get("match") or "EXACT").upper()
            groups[(h["ad_group_id"], match, round(float(h["bid"]), 2))].append(h["search_term"])
        for (ag_id, match, bid), kws in groups.items():
            adg_sub = _adlabs.first_reference(
                _adlabs.query(cache["adg_ref"], _in_sql("ad_group_id", [ag_id])))
            _adlabs.create_entities(
                entity_type="target", team_id=team_id, profile_id=profile_id,
                reference=adg_sub, match_types=json.dumps([match]),
                keywords=json.dumps(kws), bid_amount=bid, note=note)
            applied["harvested"] += len(kws)
    except AdLabsError as e:
        errors.append(f"harvest: {e}")

    # Negate: build a search_term reference of selected rows, create AD_GROUP negatives.
    try:
        ids = [str(n["search_term_id"]) for n in negate if n.get("search_term_id")]
        if ids:
            st_sub = _adlabs.first_reference(
                _adlabs.query(cache["st_ref"], _in_sql("search_term_id", ids)))
            _adlabs.create_entities(
                entity_type="negative_targeting", team_id=team_id, profile_id=profile_id,
                reference=st_sub, match_types=json.dumps(["AD_GROUP_NEGATIVE_EXACT"]), note=note)
            applied["negated"] += len(ids)
    except AdLabsError as e:
        errors.append(f"negate: {e}")

    return jsonify({"success": not errors, "applied": applied, "errors": errors})


def _in_sql(col, ids):
    quoted = ",".join("'" + str(i).replace("'", "") + "'" for i in ids)
    return f"SELECT * FROM reference_data WHERE {col} IN ({quoted})"
