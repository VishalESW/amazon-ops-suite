"""Keyword Harvesting section (Module 4) — the ASIN-level graduate/negate funnel.

Pulls the reports (AdLabs: search terms, existing targets, advertised products;
SP-API: Search Query Performance), runs the harvesting engine
(utils/keyword_harvester), and emits three dry-run review artifacts the user feeds
to the Campaign Processor. No direct account writes — the plan is for approval.
"""

import os
import re

from flask import Blueprint, render_template, request, jsonify, send_file, abort

import db
from config import cfg
from utils import jobs
from utils import spapi_client
from utils.adlabs_client import AdLabsClient, AdLabsError
from utils import keyword_harvester as kh
from utils.jsonutil import convert_numpy
from blueprints.ads import _adlabs, _date_filters

bp = Blueprint("harvest", __name__, url_prefix="/harvest")

_PROFILE_ID_RE = re.compile(r"Profile ID:\s*(\d+)")


@bp.route("")
def page():
    return render_template("harvest.html")


@bp.route("/accounts")
def accounts():
    """SP-API accounts the user can link for SQP (optional)."""
    return jsonify({"success": True, "accounts": db.list_accounts("spapi")})


def _resolve_profile_id(slug):
    res = _adlabs.read_resource(f"adlabs://profiles/{slug}")
    m = _PROFILE_ID_RE.search(res)
    if not m:
        raise AdLabsError("Could not resolve profile_id")
    return m.group(1)


def _asin_summary(ap_rows):
    """Aggregate advertised_product rows into per-ASIN economics + a pickable list.

    AdLabs has no literal price field; `aov` (average order value) is the ASP proxy.
    Returns {asin_lower: {asin, title, sku, price, target_acos, orders, sales, clicks}}.
    """
    by = {}
    for r in ap_rows:
        a = (r.get("asin") or "").strip()
        if not a:
            continue
        e = by.setdefault(a.lower(), {
            "asin": a, "title": r.get("title") or r.get("display_name") or "",
            "sku": r.get("sku") or "", "price": 0.0, "target_acos": 0.0,
            "orders": 0.0, "sales": 0.0, "clicks": 0.0,
        })
        e["price"] = e["price"] or kh._f(r.get("aov"))
        e["target_acos"] = e["target_acos"] or kh._f(r.get("target_acos"))
        e["orders"] += kh._f(r.get("orders"))
        e["sales"] += kh._f(r.get("sales"))
        e["clicks"] += kh._f(r.get("clicks"))
        if not e["title"]:
            e["title"] = r.get("title") or r.get("display_name") or ""
    return by


@bp.route("/asins")
def asins():
    """List the profile's advertised ASINs (with title + price) so the user can
    scope the harvest to specific products. Runs as a job (AdLabs pull)."""
    team_id, slug = request.args.get("team_id"), request.args.get("slug")
    if not team_id or not slug:
        return jsonify({"success": False, "error": "team_id and slug required"}), 400
    lookback = int(request.args.get("lookback_days", 60) or 60)
    filters = _date_filters(lookback)

    def work(progress):
        progress("Resolving profile…")
        profile_id = _resolve_profile_id(slug)
        progress("Loading advertised products…")
        ap_out = _adlabs.get_entity_data("advertised_product", team_id=int(team_id),
                                         profile_id=profile_id, filters=filters)
        rows = _adlabs.download_rows(_adlabs.first_reference(ap_out))
        summary = _asin_summary(rows)
        items = sorted(summary.values(), key=lambda x: (x["sales"], x["orders"]), reverse=True)
        return {"asins": items}

    return jsonify({"success": True, "job_id": jobs.start(work)})


@bp.route("/analyze", methods=["POST"])
def analyze():
    body = request.get_json() or {}
    team_id, slug = body.get("team_id"), body.get("slug")
    if not team_id or not slug:
        return jsonify({"success": False, "error": "team_id and slug required"}), 400
    spapi_account_id = body.get("spapi_account_id")
    selected_asins = body.get("asins") or []           # ASIN-level scope (empty = whole profile)
    target_acos = float(body.get("target_acos", 0.20) or 0.20)
    cfg_obj = kh.HarvestConfig(
        min_clicks=body.get("min_clicks", 5), min_orders=body.get("min_orders", 2),
        max_acos=body.get("max_acos", 0.30), lookback_days=body.get("lookback_days", 60),
        target_acos_default=target_acos,
        max_new_campaigns_per_run=body.get("max_new_campaigns_per_run", 50),
        own_brand_tokens=body.get("own_brand_tokens") or [],
        competitor_brand_tokens=body.get("competitor_brand_tokens") or [],
    )
    filters = _date_filters(cfg_obj.lookback_days)

    def work(progress):
        progress("Resolving profile…")
        res = _adlabs.read_resource(f"adlabs://profiles/{slug}")
        m = _PROFILE_ID_RE.search(res)
        if not m:
            raise AdLabsError("Could not resolve profile_id")
        profile_id = m.group(1)

        progress("Fetching search terms (STR)…")
        st_out = _adlabs.get_entity_data("search_term", team_id=int(team_id),
                                         profile_id=profile_id, filters=filters)
        search_rows = _adlabs.download_rows(_adlabs.first_reference(st_out))
        if not search_rows:
            raise AdLabsError("No search-term data returned — cannot harvest.")

        progress("Loading existing targets (dedup)…")
        tg_out = _adlabs.get_entity_data("target", team_id=int(team_id),
                                         profile_id=profile_id, filters=filters)
        target_rows = _adlabs.download_rows(_adlabs.first_reference(tg_out))
        if not target_rows:
            raise AdLabsError("No targeting data returned — cannot dedup safely; aborting.")

        progress("Mapping ad groups to products…")
        ap_map, asin_econ = {}, {}
        try:
            ap_out = _adlabs.get_entity_data("advertised_product", team_id=int(team_id),
                                             profile_id=profile_id, filters=filters)
            ap_rows = _adlabs.download_rows(_adlabs.first_reference(ap_out))
            for r in ap_rows:
                ag = r.get("ad_group_id")
                if ag and ag not in ap_map:
                    ap_map[ag] = {"asin": r.get("asin") or r.get("product_asin") or "",
                                  "title": r.get("title") or r.get("product_title") or ""}
            asin_econ = _asin_summary(ap_rows)   # per-ASIN price (AOV) + target ACoS
        except AdLabsError:
            pass

        # Price fallback for any ASIN with no economics: median of known AOVs.
        prices = sorted(e["price"] for e in asin_econ.values() if e.get("price"))
        asp_fallback = prices[len(prices) // 2] if prices else float(body.get("asp", 0) or 0)

        # --- SQP via SP-API (optional; degrades if no account linked) ---
        sqp_index, sqp_opps = {}, []
        if spapi_account_id:
            progress("Pulling SQP from SP-API…")
            try:
                rt = db.get_account_refresh_token(spapi_account_id)
                if rt:
                    endpoint, mkt, *_ = spapi_client.resolve_endpoint_and_marketplace(rt)
                    client = spapi_client.SpApiClient(rt, endpoint=endpoint, marketplace_id=mkt)
                    # Scope SQP to the selected ASINs when set, else the whole profile,
                    # and order by sales so the most important products come first.
                    scope = {a.strip().lower() for a in selected_asins if str(a).strip()}
                    cand = [a for a in asin_econ.values() if a.get("asin")
                            and (not scope or a["asin"].lower() in scope)]
                    cand.sort(key=lambda x: (x.get("sales", 0), x.get("orders", 0)), reverse=True)
                    asins = list(dict.fromkeys(a["asin"] for a in cand))
                    # Each ASIN = 8 weekly reports; Amazon's create-report burst is ~15,
                    # so keep the fan-out small (cap ~2 ASINs) to stay fast. Scope to
                    # specific ASINs in the UI for more products without the throttle.
                    progress(f"Pulling SQP for {min(len(asins), 2)} product(s)…")
                    sqp_rows = client.fetch_sqp(asins[:2])
                    sqp_index, sqp_opps = kh.build_sqp_index(sqp_rows)
            except Exception as e:  # noqa: BLE001 — SQP is prioritization-only; never fail the run
                progress(f"SQP skipped: {e}")

        progress("Running harvest engine…")
        out = kh.run_harvest(search_rows, target_rows, cfg_obj, ap_map=ap_map,
                             sqp_index=sqp_index, profile=slug, asp=asp_fallback,
                             target_acos=target_acos, asin_econ=asin_econ,
                             asins=selected_asins)

        # Persist the three CSV artifacts for download.
        os.makedirs(cfg.OUTPUT_FOLDER, exist_ok=True)
        files = _write_artifacts(out)

        # Keep the response light: cap the plan table.
        plan = out["plan"]
        return {
            "profile_id": profile_id, "stats": out["stats"],
            "plan": plan[:2000], "plan_truncated": len(plan) > 2000,
            "sqp_opportunities": sqp_opps[:60], "files": files,
            "range_label": f"last {cfg_obj.lookback_days} days",
            "counts": {"create": len(out["create"]), "targets": len(out["targets"]),
                       "negatives": len(out["negatives"])},
        }

    return jsonify({"success": True, "job_id": jobs.start(work)})


def _write_artifacts(out):
    """Write the 3 CSVs to OUTPUT_FOLDER under a run id; return {name: filename}."""
    import time as _t
    run = _t.strftime("%Y%m%d-%H%M%S")
    spec = [("create_campaigns", out["create"], kh.CREATE_HEADERS),
            ("add_targets", out["targets"], kh.ADD_TARGET_HEADERS),
            ("add_negatives", out["negatives"], kh.ADD_NEGATIVE_HEADERS)]
    files = {}
    for name, rows, headers in spec:
        fn = f"harvest_{name}_{run}.csv"
        with open(os.path.join(cfg.OUTPUT_FOLDER, fn), "w", encoding="utf-8-sig", newline="") as f:
            f.write(kh.to_csv(rows, headers))
        files[name] = fn
    return files


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


@bp.route("/download/<path:filename>")
def download(filename):
    """Download a harvest artifact CSV (must be a generated harvest_ file)."""
    if not re.fullmatch(r"harvest_[a-z_]+_\d{8}-\d{6}\.csv", filename):
        abort(404)
    path = os.path.join(cfg.OUTPUT_FOLDER, filename)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=filename)
