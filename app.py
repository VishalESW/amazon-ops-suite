"""
Amazon Operations Suite — Flask application.

Three sections, each its own blueprint:
  - N-Gram Analyzer    (blueprints/ngram.py)    — PPC search-term N-gram reports
  - Inventory Transfer (blueprints/inventory.py)— SP-API report pull + FBA workbook
  - Ads Bid Optimizer  (blueprints/ads.py)      — Advertising API bid optimisation

A landing page lets the user pick a section.
"""

import os
import time

from flask import Flask, render_template, jsonify, send_file, request, redirect, g
from werkzeug.utils import secure_filename

import utils.jsonutil  # noqa: F401 — installs numpy JSON encoder on import
from config import cfg
from db import init_db


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = cfg.SECRET_KEY
    app.config["MAX_CONTENT_LENGTH"] = cfg.MAX_CONTENT_LENGTH
    app.config["UPLOAD_FOLDER"] = cfg.UPLOAD_FOLDER
    app.config["OUTPUT_FOLDER"] = cfg.OUTPUT_FOLDER
    app.config["ARCHIVE_FOLDER"] = cfg.ARCHIVE_FOLDER
    app.config["ALLOWED_EXTENSIONS"] = cfg.ALLOWED_EXTENSIONS
    app.config["TEMPLATES_AUTO_RELOAD"] = True  # pick up template edits without a restart

    for folder in (cfg.UPLOAD_FOLDER, cfg.OUTPUT_FOLDER, cfg.ARCHIVE_FOLDER, cfg.DATA_FOLDER):
        os.makedirs(folder, exist_ok=True)

    init_db()

    # Blueprints
    from blueprints.ngram import bp as ngram_bp
    from blueprints.inventory import bp as inventory_bp
    from blueprints.ads import bp as ads_bp
    from blueprints.harvest import bp as harvest_bp
    from blueprints.dashboard import bp as dashboard_bp
    from blueprints.campaign import bp as campaign_bp

    app.register_blueprint(ngram_bp)
    app.register_blueprint(inventory_bp)
    app.register_blueprint(ads_bp)
    app.register_blueprint(harvest_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(campaign_bp)

    # ---- Clerk auth: domain-restricted gate -------------------------------
    # Paths reachable without a (valid, in-domain) session.
    _PUBLIC_PATHS = {"/sign-in", "/access-denied", "/sign-out", "/health", "/favicon.ico"}

    @app.context_processor
    def _inject_clerk():
        return {
            "clerk_pk": cfg.CLERK_PUBLISHABLE_KEY,
            "clerk_frontend_api": cfg.CLERK_FRONTEND_API,
            "auth_enabled": cfg.AUTH_ENABLED,
            "current_email": getattr(g, "user_email", None),
        }

    @app.before_request
    def _auth_gate():
        if not cfg.AUTH_ENABLED:
            return None
        p = request.path
        if p.startswith("/static") or p.startswith("/__clerk") or p in _PUBLIC_PATHS:
            return None
        from utils import clerk_auth
        auth = clerk_auth.authenticate(request)
        if not auth["signed_in"]:
            return redirect("/sign-in")
        if not clerk_auth.domain_ok(auth.get("email")):
            return render_template("access_denied.html",
                                   email=auth.get("email"),
                                   domain=cfg.AUTH_ALLOWED_DOMAIN), 403
        g.user_email = auth.get("email")
        return None

    @app.route("/sign-in")
    def sign_in():
        return render_template("sign_in.html")

    @app.route("/sign-out")
    def sign_out():
        return render_template("sign_out.html")

    @app.route("/access-denied")
    def access_denied():
        return render_template("access_denied.html", email=None,
                               domain=cfg.AUTH_ALLOWED_DOMAIN), 403

    @app.route("/")
    def landing():
        import db
        from datetime import datetime
        from utils.archive_store import load_archive

        accounts = db.list_accounts(kind="spapi")

        # Most recent generated workbooks/reports for quick re-download.
        recent = []
        if os.path.isdir(cfg.OUTPUT_FOLDER):
            files = [f for f in os.listdir(cfg.OUTPUT_FOLDER) if f.lower().endswith((".xlsx", ".xls"))]
            files.sort(key=lambda f: os.path.getmtime(os.path.join(cfg.OUTPUT_FOLDER, f)), reverse=True)
            for f in files[:5]:
                path = os.path.join(cfg.OUTPUT_FOLDER, f)
                kind = ("Inventory" if f.startswith("Inventory") else
                        "ASIN" if f.startswith("ASIN") else "N-Gram")
                recent.append({
                    "name": f, "kind": kind,
                    "size_kb": max(1, round(os.path.getsize(path) / 1024)),
                    "when": datetime.fromtimestamp(os.path.getmtime(path)).strftime("%b %d, %H:%M"),
                })

        status = {
            "accounts": accounts,
            "account_count": len(accounts),
            "ads_ready": bool(cfg.ADLABS_MCP_KEY),
            "ai_ready": bool(cfg.AI_API_URL),
            "ai_model": cfg.AI_MODEL,
            "archive_count": len(load_archive()),
            "recent": recent,
        }
        return render_template("landing.html", status=status)

    @app.route("/callback")
    def spapi_callback():
        """Top-level SP-API OAuth redirect target (registered in Seller Central)."""
        from blueprints.inventory import handle_spapi_callback
        return handle_spapi_callback()

    @app.route("/download/<filename>")
    def download_file(filename):
        """Shared download for any generated workbook in OUTPUT_FOLDER."""
        try:
            filepath = os.path.join(cfg.OUTPUT_FOLDER, secure_filename(filename))
            if not os.path.exists(filepath):
                return jsonify({"success": False, "error": "File not found"}), 404
            return send_file(
                filepath,
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                as_attachment=True,
                download_name=filename,
            )
        except Exception as e:  # noqa: BLE001
            return jsonify({"success": False, "error": str(e)}), 500

    @app.route("/health")
    def health_check():
        from datetime import datetime
        return jsonify({"status": "healthy", "timestamp": datetime.now().isoformat()})

    return app


def cleanup_old_files():
    """Remove upload/output files older than 1 hour."""
    max_age = 3600
    now = time.time()
    for folder in (cfg.UPLOAD_FOLDER, cfg.OUTPUT_FOLDER):
        if not os.path.exists(folder):
            continue
        for filename in os.listdir(folder):
            filepath = os.path.join(folder, filename)
            if os.path.isfile(filepath) and now - os.path.getmtime(filepath) > max_age:
                try:
                    os.remove(filepath)
                except OSError:
                    pass


app = create_app()


if __name__ == "__main__":
    cleanup_old_files()
    app.run(host="127.0.0.1", port=5000, threaded=True)
