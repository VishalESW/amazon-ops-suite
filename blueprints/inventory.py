"""Inventory Transfer blueprint.

Flow:
  /inventory                 -> page (connected accounts, config warnings)
  /inventory/connect         -> redirect to Seller Central consent
  /inventory/callback        -> store account from spapi_oauth_code
  /inventory/<id>/fetch      -> start background report pull (returns job id)
  /inventory/fetch/<job>     -> poll job status
  /inventory/fetch/<job>/data-> get product-list preview when done
  /inventory/<id>/generate   -> build workbook from (edited) product list -> filename
  /inventory/<id>/delete     -> remove account
"""

import os
import secrets
from datetime import datetime

from flask import (Blueprint, render_template, request, jsonify, redirect,
                   session, current_app)

from config import cfg
import db
from utils import lwa_auth, jobs
from utils.spapi_client import (
    SpApiClient, pull_inventory_reports, resolve_endpoint_and_marketplace,
    get_store_name, MARKET_CODES)
from utils.band_classifier import build_product_list, BANDS
from utils.inventory_builder import build_inventory_workbook
from utils.jsonutil import convert_numpy

bp = Blueprint("inventory", __name__, url_prefix="/inventory")

# account_id -> last pulled reports (kept in-process for the generate step)
_report_cache = {}


@bp.route("")
def page():
    accounts = db.list_accounts(kind="spapi")
    # Backfill the friendly store name for any account still labelled with its
    # selling_partner_id (one cheap SP-API call each, best-effort).
    for a in accounts:
        # Backfill store name + real marketplace for accounts not resolved yet.
        if not (a.get("name") and a["name"] != a.get("selling_partner_id")):
            rt = db.get_account_refresh_token(a["id"])
            if rt:
                ep, mp, region, name, store = resolve_endpoint_and_marketplace(rt, cfg.SPAPI_REGION)
                if store or mp:
                    db.upsert_account("spapi", store or a.get("name"),
                                      a.get("selling_partner_id"), rt,
                                      region=region, marketplace_id=mp)
                    a["name"] = store or a["name"]
                    a["marketplace_id"] = mp
                    a["region"] = region
        a["market"] = MARKET_CODES.get(a.get("marketplace_id"), a.get("marketplace_id") or "—")
    warnings = lwa_auth.spapi_config_warnings()
    return render_template("inventory.html", accounts=accounts, warnings=warnings, bands=BANDS)


@bp.route("/accounts")
def accounts():
    return jsonify({"success": True, "accounts": db.list_accounts(kind="spapi")})


# ----------------------------------------------------------------- OAuth ---

@bp.route("/connect")
def connect():
    # The Seller Central consent page requires the SP-API "Application ID"
    # (amzn1.sellerapps.app.<uuid>), which is different from the LWA client id.
    # Without it Amazon shows an error instead of the consent page.
    if not lwa_auth.is_valid_spapi_app_id(cfg.SPAPI_APPLICATION_ID):
        return render_template(
            "oauth_result.html", ok=False, back="/inventory",
            message=("SP-API Application ID is missing or invalid. Set "
                     "SPAPI_APPLICATION_ID in .env to the App ID from Seller Central → "
                     "Develop Apps (amzn1.sp.solution.<uuid> or amzn1.sellerapps.app.<uuid>, "
                     "different from your LWA client id), then restart the app."))
    state = secrets.token_urlsafe(24)
    session["spapi_oauth_state"] = state
    return redirect(lwa_auth.spapi_consent_url(state))


@bp.route("/callback")
def callback():
    # Kept as an alias; the registered Redirect URI is the top-level /callback.
    return handle_spapi_callback()


def handle_spapi_callback():
    error = request.args.get("error")
    if error:
        return render_template("oauth_result.html", ok=False,
                               message=f"Authorization denied: {error}", back="/inventory")

    state = request.args.get("state")
    if not state or state != session.get("spapi_oauth_state"):
        return render_template("oauth_result.html", ok=False,
                               message="State mismatch — possible CSRF. Try again.",
                               back="/inventory")

    code = request.args.get("spapi_oauth_code")
    selling_partner_id = request.args.get("selling_partner_id")
    if not code:
        return render_template("oauth_result.html", ok=False,
                               message="No authorization code returned.", back="/inventory")

    try:
        tokens = lwa_auth.exchange_code_for_refresh_token(
            code, cfg.spapi_redirect_uri,
            client_id=cfg.SPAPI_CLIENT_ID, client_secret=cfg.SPAPI_CLIENT_SECRET)
        refresh_token = tokens["refresh_token"]
        # Use the seller's store/business name as the label when available.
        name = get_store_name(refresh_token, cfg.SPAPI_REGION) or selling_partner_id or "Seller account"
        db.upsert_account("spapi", name, selling_partner_id, refresh_token)
    except Exception as e:  # noqa: BLE001
        return render_template("oauth_result.html", ok=False,
                               message=f"Token exchange failed: {e}", back="/inventory")

    session.pop("spapi_oauth_state", None)
    return render_template("oauth_result.html", ok=True,
                           message="Seller account connected successfully.",
                           back="/inventory")


@bp.route("/<account_id>/delete", methods=["POST"])
def delete(account_id):
    db.delete_account(account_id)
    _report_cache.pop(account_id, None)
    return jsonify({"success": True})


# --------------------------------------------------------- report pulling ---

@bp.route("/<account_id>/fetch", methods=["POST"])
def fetch(account_id):
    account = db.get_account(account_id)
    if not account:
        return jsonify({"success": False, "error": "Account not found"}), 404
    refresh_token = db.get_account_refresh_token(account_id)

    body = request.get_json(silent=True) or {}
    buffer_pct = float(body.get("buffer_pct", 0) or 0)
    increase_factor = max(0.0, buffer_pct / 100.0)
    market_override = (body.get("market") or "").strip().upper()

    def work(progress):
        progress("Resolving seller marketplace…")
        endpoint, marketplace_id, region, name, store_name = resolve_endpoint_and_marketplace(
            refresh_token, cfg.SPAPI_REGION)
        market = market_override or MARKET_CODES.get(marketplace_id, "US")
        progress(f"Using marketplace {marketplace_id} ({name or region})")
        db.upsert_account("spapi",
                          store_name or account.get("name") or account.get("selling_partner_id"),
                          account.get("selling_partner_id"), refresh_token,
                          region=region, marketplace_id=marketplace_id)
        client = SpApiClient(refresh_token, endpoint=endpoint, marketplace_id=marketplace_id)
        reports = pull_inventory_reports(client, progress=progress)

        # Bands are auto-assigned by sales velocity (no manual step).
        product_list = build_product_list(reports)
        _report_cache[account_id] = reports

        progress("Building inventory workbook…")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"Inventory_Transfer_{market}_{ts}.xlsx"
        output_path = os.path.join(cfg.OUTPUT_FOLDER, filename)
        build_inventory_workbook(reports, product_list, output_path, market=market,
                                 increase_factor=increase_factor)

        # Stats for the dashboard.
        band_counts = {"BAND A": 0, "BAND B": 0, "BAND C": 0}
        for p in product_list:
            b = p.get("band") or "BAND C"
            band_counts[b] = band_counts.get(b, 0) + 1
        stats = {
            "products": len(product_list),
            "bands": band_counts,
            "fba_skus": len(reports.get("fba_inventory", [])),
            "listings": len(reports.get("open_listings", [])),
            "store_name": store_name, "marketplace": marketplace_id, "market": market,
            "buffer_pct": round(increase_factor * 100),
        }
        return {"product_list": product_list, "filename": filename,
                "market": market, "stats": stats}

    job_id = jobs.start(work)
    return jsonify({"success": True, "job_id": job_id})


@bp.route("/fetch/<job_id>")
def fetch_status(job_id):
    status = jobs.public_status(job_id)
    if not status:
        return jsonify({"success": False, "error": "Unknown job"}), 404
    return jsonify({"success": True, **status})


@bp.route("/fetch/<job_id>/data")
def fetch_data(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"success": False, "error": "Unknown job"}), 404
    if job["state"] != "done":
        return jsonify({"success": False, "error": "Not ready"}), 409
    return jsonify(convert_numpy({"success": True, **job["result"]}))


# ---------------------------------------------------- workbook generation ---

@bp.route("/<account_id>/generate", methods=["POST"])
def generate(account_id):
    account = db.get_account(account_id)
    if not account:
        return jsonify({"success": False, "error": "Account not found"}), 404

    reports = _report_cache.get(account_id)
    if not reports:
        return jsonify({"success": False,
                        "error": "No fetched reports in memory — run Fetch first."}), 409

    data = request.get_json() or {}
    product_list = data.get("product_list") or []
    market = data.get("market", "US")
    if not product_list:
        return jsonify({"success": False, "error": "Empty product list"}), 400

    # Persist user overrides so next fetch remembers them.
    db.save_band_map(account_id, product_list)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"Inventory_Transfer_{market}_{ts}.xlsx"
    output_path = os.path.join(current_app.config["OUTPUT_FOLDER"], filename)
    build_inventory_workbook(reports, product_list, output_path, market=market)

    return jsonify({"success": True, "filename": filename})
