"""Ads Bid & Placement Optimizer — powered by AdLabs (MCP).

Replaces the direct Amazon Ads API integration. Flow:
  /ads                      -> page
  /ads/profiles             -> background: list teams + advertising profiles
  /ads/profiles/<job>       -> poll / data
  /ads/analyze              -> background: pull targets + placements from AdLabs,
                               run the 4-rule bid engine + placement suggestions,
                               AI summary
  /ads/analyze/<job>        -> poll / data
  /ads/apply                -> push approved bid + placement changes via AdLabs

AdLabs has the metrics but does not pre-fill rpc_category unless a Target ACOS is
configured, so the app computes the bid rules itself (the exact 4-rule table) from
AdLabs data, then applies changes back through AdLabs update_entities.
"""

import re
import time
from datetime import date, timedelta

from flask import Blueprint, render_template, request, jsonify

from utils import jobs
from utils.adlabs_client import AdLabsClient, AdLabsError
from utils.bid_optimizer import optimize
from utils.ai_client import summarize_keywords
from utils.jsonutil import convert_numpy

bp = Blueprint("ads", __name__, url_prefix="/ads")

_adlabs = AdLabsClient()
# profile_id -> {target_ref, placement_ref, team_id, target_acos, target_cpa}
_analysis_cache = {}
# Cache the profile list (rarely changes) to avoid hammering AdLabs on every visit.
_profiles_cache = {"ts": 0, "data": None}
_PROFILES_TTL = 600  # seconds

_TEAM_RE = re.compile(r"team_id=(\d+)\s+(.+?)\s+org=", re.I)
_PROFILE_ID_RE = re.compile(r"Profile ID:\s*(\d+)")


def _f(v, d=0.0):
    try:
        return float(str(v).replace(",", "").replace("$", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return d


def _date_filters(lookback_days):
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=lookback_days - 1)
    c_end = start - timedelta(days=1)
    c_start = c_end - timedelta(days=lookback_days - 1)

    def block(key, a, b):
        return {"key": key, "conditions": [
            {"operator": ">=", "values": [a.isoformat()]},
            {"operator": "<=", "values": [b.isoformat()]}], "logical_operator": "AND"}
    return [block("DATE", start, end), block("COMPARE_DATE", c_start, c_end)]


@bp.route("")
def page():
    return render_template("ads.html")


# ----------------------------------------------------------- profiles ---

@bp.route("/profiles")
def profiles():
    def work(progress):
        # Serve from cache when fresh — profiles rarely change and AdLabs throttles.
        if _profiles_cache["data"] and time.time() - _profiles_cache["ts"] < _PROFILES_TTL:
            return {"profiles": _profiles_cache["data"], "cached": True}
        progress("Loading teams…")
        teams_text = _adlabs.get_entity_data("teams")
        teams = _TEAM_RE.findall(teams_text)
        out = []
        for team_id, team_name in teams:
            progress(f"Loading profiles for {team_name.strip()}…")
            ptext = _adlabs.get_entity_data("profiles", team_id=int(team_id))
            for row in AdLabsClient.parse_table(ptext):
                uri = next((v for k, v in row.items() if "Resource URI" in k and v), "")
                slug = uri.rsplit("/", 1)[-1] if uri else ""
                if not slug:
                    continue
                out.append({
                    "team_id": int(team_id), "team_name": team_name.strip(),
                    "name": row.get("Name", ""), "country": row.get("Country", ""),
                    "currency": row.get("Currency", ""), "brand": row.get("Brand", ""),
                    "slug": slug,
                })
        _profiles_cache.update(ts=time.time(), data=out)
        return {"profiles": out}

    # If we already have a fresh cache, skip the job entirely.
    if _profiles_cache["data"] and time.time() - _profiles_cache["ts"] < _PROFILES_TTL:
        return jsonify({"success": True, "cached": True, "profiles": _profiles_cache["data"]})
    return jsonify({"success": True, "job_id": jobs.start(work)})


@bp.route("/profiles/<job_id>")
def profiles_status(job_id):
    s = jobs.public_status(job_id)
    if not s:
        return jsonify({"success": False, "error": "Unknown job"}), 404
    return jsonify({"success": True, **s})


@bp.route("/profiles/<job_id>/data")
def profiles_data(job_id):
    job = jobs.get(job_id)
    if not job or job["state"] != "done":
        return jsonify({"success": False, "error": "Not ready"}), 409
    return jsonify({"success": True, **job["result"]})


# ------------------------------------------------------------ analyze ---

@bp.route("/analyze", methods=["POST"])
def analyze():
    body = request.get_json() or {}
    team_id = body.get("team_id")
    slug = body.get("slug")
    if not team_id or not slug:
        return jsonify({"success": False, "error": "team_id and slug required"}), 400
    target_acos = float(body.get("target_acos", 0.30))
    target_cpa = float(body.get("target_cpa", 15.0))
    lookback = int(body.get("lookback_days", 14))

    def work(progress):
        progress("Resolving profile…")
        res = _adlabs.read_resource(f"adlabs://profiles/{slug}")
        m = _PROFILE_ID_RE.search(res)
        if not m:
            raise AdLabsError("Could not resolve profile_id")
        profile_id = m.group(1)
        filters = _date_filters(lookback)

        # --- Targets (keywords) ---
        progress("Fetching keywords from AdLabs…")
        t_out = _adlabs.get_entity_data("target", team_id=int(team_id),
                                        profile_id=profile_id, filters=filters)
        target_ref = _adlabs.first_reference(t_out)
        progress("Selecting active keywords…")
        t_top = _adlabs.query(
            target_ref,
            "SELECT * FROM reference_data WHERE target_state='Enabled' "
            "AND (spend > 0 OR clicks > 0) ORDER BY spend DESC LIMIT 200")
        t_rows = AdLabsClient.parse_table(_adlabs.read(_adlabs.first_reference(t_top), limit=200))

        kw_input = [{
            "keywordId": r.get("target_id"), "campaignId": r.get("campaign_id"),
            "campaignName": r.get("campaign_name"), "keywordText": r.get("targeting"),
            "matchType": r.get("match_types"), "bid": _f(r.get("bid")),
            "impressions": _f(r.get("impressions")), "clicks": _f(r.get("clicks")),
            "spend": _f(r.get("spend")), "sales": _f(r.get("sales")),
            "orders": _f(r.get("orders")),
        } for r in t_rows]
        progress("Computing bid changes…")
        keyword_results = optimize(kw_input, target_acos, target_cpa)
        # Carry through TOS impression share + last-bid-change history (same order).
        for kr, src in zip(keyword_results, t_rows):
            kr["tos_share"] = _f(src.get("top_of_search_impression_share"))
            kr["last_change"] = (src.get("last_optimized_at") or "").strip()
            kr["last_note"] = (src.get("last_optimized_note") or "").strip()

        # --- Placements ---
        progress("Fetching placements from AdLabs…")
        p_out = _adlabs.get_entity_data("placement", team_id=int(team_id),
                                        profile_id=profile_id, filters=filters)
        placement_ref = _adlabs.first_reference(p_out)
        p_rows = AdLabsClient.parse_table(_adlabs.read(placement_ref, limit=200))
        placement_results = _suggest_placements(p_rows, target_acos)

        progress("Generating AI summary…")
        brief = summarize_keywords(keyword_results, target_acos, target_cpa)

        _analysis_cache[profile_id] = {
            "target_ref": target_ref, "placement_ref": placement_ref,
            "team_id": int(team_id), "target_acos": target_acos, "target_cpa": target_cpa,
        }
        kw_changed = [r for r in keyword_results if r.get("rule")]
        pl_changed = [r for r in placement_results if r.get("changed")]
        from collections import Counter
        rule_counts = Counter(r["rule"] for r in kw_changed)
        stats = {
            "keywords_analyzed": len(keyword_results),
            "kw_changed": len(kw_changed),
            "placements_analyzed": len(placement_results),
            "pl_changed": len(pl_changed),
            "spend_in_scope": round(sum(r.get("spend", 0) for r in kw_changed), 2),
            "sales_in_scope": round(sum(r.get("sales", 0) for r in kw_changed), 2),
            "by_rule": dict(rule_counts),
            "lookback_days": lookback,
        }
        return {
            "profile_id": profile_id,
            "keywords": keyword_results, "placements": placement_results,
            "summary": brief, "kw_changed": len(kw_changed), "pl_changed": len(pl_changed),
            "target_acos": target_acos, "target_cpa": target_cpa, "stats": stats,
        }

    return jsonify({"success": True, "job_id": jobs.start(work)})


def _suggest_placements(rows, target_acos):
    """Simple, transparent placement modifier suggestions based on ACOS vs target.
    Respects AdLabs' update guardrails (only Enabled, non-ended, no opt-rule campaigns)."""
    out = []
    for r in rows:
        spend = _f(r.get("spend")); sales = _f(r.get("sales"))
        pct = _f(r.get("percentage"))
        acos = (spend / sales) if sales > 0 else None
        can_apply = (
            r.get("campaign_state") == "Enabled"
            and str(r.get("has_opt_rule")).lower() != "true"
            and not (r.get("campaign_global_id") or "").strip()
            and not (r.get("end_date") or "").strip()
        )
        new_pct = pct
        if can_apply:
            if sales == 0 and spend > 0:
                new_pct = max(0, round(pct - 15))
            elif acos is not None and acos > target_acos:
                new_pct = max(0, round(pct - 10))
            elif acos is not None and acos < target_acos * 0.8:
                new_pct = min(900, round(pct + 10))
        out.append({
            "placementId": r.get("placement_id"), "campaign": r.get("campaign_name"),
            "placement": r.get("placement_type"), "current_pct": round(pct),
            "new_pct": int(new_pct), "changed": can_apply and int(new_pct) != round(pct),
            "can_apply": can_apply,
            "acos": round(acos, 4) if acos is not None else None,
            "cvr": _f(r.get("cvr")), "spend": round(spend, 2), "sales": round(sales, 2),
            "clicks": int(_f(r.get("clicks"))), "orders": int(_f(r.get("orders"))),
            "recommendation": r.get("placement_recommendation", ""),
            "last_change": (r.get("placement_last_optimized_at") or "").strip(),
            "last_note": (r.get("placement_last_optimized_note") or "").strip(),
        })
    return out


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


# -------------------------------------------------------------- apply ---

@bp.route("/apply", methods=["POST"])
def apply():
    body = request.get_json() or {}
    profile_id = body.get("profile_id")
    cache = _analysis_cache.get(profile_id)
    if not cache:
        return jsonify({"success": False, "error": "No analysis in memory — run Analyze first."}), 409
    team_id = cache["team_id"]
    kw_updates = body.get("keyword_updates") or []   # [{target_id, bid}]
    pl_updates = body.get("placement_updates") or []  # [{placement_id, pct}]
    note = "Bid/placement optimization via N-Gram Suite"

    applied = {"keywords": 0, "placements": 0}
    errors = []

    # Group keyword updates by target bid, then bulk-update each group.
    try:
        for bid, ids in _group(kw_updates, "target_id", "bid").items():
            sub = _adlabs.query(cache["target_ref"], _in_sql("target_id", ids))
            ref = _adlabs.first_reference(sub)
            _adlabs.update_entities(entity_type="target", action="update_bid",
                                    team_id=team_id, profile_id=profile_id,
                                    bid_update_type="SET_BID_TO_AMOUNT", bid_amount=bid,
                                    reference=ref, note=note)
            applied["keywords"] += len(ids)
    except AdLabsError as e:
        errors.append(f"keywords: {e}")

    try:
        for pct, ids in _group(pl_updates, "placement_id", "pct").items():
            sub = _adlabs.query(cache["placement_ref"], _in_sql("placement_id", ids))
            ref = _adlabs.first_reference(sub)
            _adlabs.update_entities(entity_type="placement",
                                    action="update_placement_bid_adjustment",
                                    team_id=team_id, profile_id=profile_id,
                                    placement_update_type="SET_TO_PERCENTAGE",
                                    placement_update_value=float(pct),
                                    reference=ref, note=note)
            applied["placements"] += len(ids)
    except AdLabsError as e:
        errors.append(f"placements: {e}")

    return jsonify({"success": not errors, "applied": applied, "errors": errors})


def _group(updates, id_key, val_key):
    groups = {}
    for u in updates:
        groups.setdefault(round(float(u[val_key]), 2), []).append(str(u[id_key]))
    return groups


def _in_sql(col, ids):
    quoted = ",".join("'" + i.replace("'", "") + "'" for i in ids)
    return f"SELECT * FROM reference_data WHERE {col} IN ({quoted})"
