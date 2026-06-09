"""Campaign Processor v2 — stateful, AdLabs-driven, role-gated wizard.

Flow: profile pick -> ASIN dashboard -> seed entry (approval gates) -> uploads ->
keyword/ASIN selection grids -> Semantics/PAT/MKL -> Campaign Naming + bidding ->
verify/preview/build. See docs/CAMPAIGN_PROCESSOR_V2_PLAN.md.

This file owns routing + per-step state; heavy lifting lives in:
  utils.campaign_db        — projects / roles / state / approvals
  utils.campaign_adlabs    — AdLabs profiles + ASIN ("product") metrics + Rank
  utils.campaign_engine    — xlsx builder (reused from v1)
  utils.campaign_orchestrator / campaign_ai — selection + build helpers
"""

import os
import traceback
import uuid

from flask import (Blueprint, render_template, request, jsonify, redirect,
                   url_for, g, abort)

from config import cfg
from utils import campaign_db as cdb
from utils import campaign_adlabs as cadl
from utils import campaign_store as cstore
from utils import campaign_orchestrator as orch
from utils.campaign_ai import available as ai_available

bp = Blueprint("campaign", __name__, url_prefix="/campaign")

# Ordered step keys for the wizard rail.
STEPS = [
    ("profile",   "Profile"),
    ("asins",     "ASIN Dashboard"),
    ("seed",      "Seed Keywords & ASINs"),
    ("uploads",   "Upload Files"),
    ("keywords",  "Keyword Selection"),
    ("semantics", "Semantics"),
    ("asin_sel",  "ASIN Selection (PAT)"),
    ("master",    "Master Keywords"),
    ("campaigns", "Campaign Naming & Bids"),
    ("build",     "Verify & Build"),
]


def _current_user():
    """Email of the signed-in user. Falls back to a dev identity when auth is off."""
    email = getattr(g, "user_email", None)
    if email:
        return email
    return "dev@local"


def _role(email=None):
    email = email or _current_user()
    # When auth is disabled (local dev), act as manager so the full flow is testable.
    if not cfg.AUTH_ENABLED:
        return cdb.ROLE_MANAGER
    return cdb.get_role(email)


def _ctx(**extra):
    user = _current_user()
    base = {
        "role": _role(user),
        "user_email": user,
        "is_manager": _role(user) == cdb.ROLE_MANAGER,
        "ai_ready": ai_available(),
        "adlabs_ready": bool(cfg.ADLABS_MCP_KEY),
        "steps": STEPS,
    }
    base.update(extra)
    return base


# --------------------------------------------------------------- dashboard ---
@bp.route("/")
def index():
    cdb.init()
    projects = cdb.list_projects()
    pending = cdb.pending_approvals() if _role() == cdb.ROLE_MANAGER else []
    return render_template("campaign.html", **_ctx(projects=projects, pending=pending))


@bp.route("/profiles")
def profiles():
    """JSON list of AdLabs advertising profiles for the new-project picker."""
    try:
        return jsonify({"success": True, "profiles": cadl.list_profiles(
            force=request.args.get("refresh") == "1")})
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 502


@bp.route("/projects", methods=["POST"])
def create_project():
    cdb.init()
    name = (request.form.get("name") or "").strip()
    team_id = request.form.get("team_id")
    slug = request.form.get("slug")
    profile_name = (request.form.get("profile_name") or "").strip()
    if not name or not slug or not team_id:
        return jsonify({"success": False, "error": "name, team_id and slug required"}), 400
    try:
        profile_id = cadl.resolve_profile_id(slug)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"Could not resolve profile: {e}"}), 502
    pid = cdb.create_project(name, team_id, profile_id, profile_name, _current_user())
    cdb.update_project(pid, current_step="asins")
    return jsonify({"success": True, "project_id": pid,
                    "redirect": url_for("campaign.project", pid=pid)})


# Seed sub-gates (each needs manager approval).
SEED_SUBS = [
    ("seed_poe",   "POE customer needs", "keywords"),
    ("seed_tatst", "TA- TST Keyword List", "keywords"),
    ("seed_h10",   "H10 Reverse ASIN list", "asins"),
    ("seed_sqp",   "SQP ASINs (≤3)", "asins"),
]


@bp.route("/projects/<pid>")
def project(pid):
    p = cdb.get_project(pid)
    if not p:
        abort(404)
    state = cdb.all_state(pid)
    approvals = cdb.approval_map(pid, [k for k, _, _ in SEED_SUBS])
    seed_meta = [{"key": k, "label": lbl, "kind": kind} for k, lbl, kind in SEED_SUBS]
    keys = [k for k, _ in STEPS]
    reached = keys.index(p["current_step"]) if p.get("current_step") in keys else 0
    if p.get("status") == cdb.STATUS_COMPLETED:
        reached = len(keys) - 1
    return render_template("campaign_project.html",
                           **_ctx(project=p, state=state, approvals=approvals,
                                  seed_subs=seed_meta, reached_index=reached))


@bp.route("/projects/<pid>/delete", methods=["POST"])
def delete_project(pid):
    cdb.delete_project(pid)
    cstore.delete_project(pid)
    return jsonify({"success": True, "redirect": url_for("campaign.index")})


# ----------------------------------------------------------- ASIN dashboard ---
@bp.route("/projects/<pid>/asins")
def asins(pid):
    """Fetch (or return cached) AdLabs ASIN metrics for this project's profile."""
    p = cdb.get_project(pid)
    if not p:
        abort(404)
    cached = cdb.get_state(pid, "asins")
    if cached and request.args.get("refresh") != "1":
        return jsonify({"success": True, "cached": True, **cached})
    try:
        days = int(request.args.get("days", 90))
        products = cadl.fetch_products(p["team_id"], p["profile_id"], days=days)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 502
    payload = {"products": products, "days": days}
    cdb.save_state(pid, "asins", payload)
    return jsonify({"success": True, "cached": False, **payload})


@bp.route("/projects/<pid>/asins", methods=["POST"])
def save_asins(pid):
    """Persist rank overrides / selected ASINs from the dashboard."""
    p = cdb.get_project(pid)
    if not p:
        abort(404)
    body = request.get_json(silent=True) or {}
    cur = cdb.get_state(pid, "asins") or {}
    cur["overrides"] = body.get("overrides", cur.get("overrides", {}))
    cur["selected"] = body.get("selected", cur.get("selected", []))
    cdb.save_state(pid, "asins", cur)
    if body.get("advance"):
        cdb.update_project(pid, current_step="seed")
    return jsonify({"success": True})


# ------------------------------------------------------------ seed gates ----
def _lines(text):
    return [x.strip() for x in (text or "").replace("\r", "\n").split("\n") if x.strip()]


@bp.route("/projects/<pid>/seed/<sub>", methods=["POST"])
def save_seed(pid, sub):
    """Persist a seed sub-gate. action='draft' just saves; action='submit' saves
    and requests manager approval (Save & Exit)."""
    p = cdb.get_project(pid)
    if not p:
        abort(404)
    step_key = sub if sub.startswith("seed_") else f"seed_{sub}"
    if step_key not in {k for k, _, _ in SEED_SUBS}:
        return jsonify({"success": False, "error": "unknown seed gate"}), 400
    body = request.get_json(silent=True) or {}
    items = _lines(body.get("content"))
    if step_key == "seed_sqp":
        items = items[:3]
    cdb.save_state(pid, step_key, {"items": items, "raw": body.get("content", "")})
    if body.get("action") == "submit":
        if not items:
            return jsonify({"success": False, "error": "Nothing to submit"}), 400
        cdb.request_approval(pid, step_key, _current_user())
        # Keep the rail/step on 'seed' (request_approval set it to the sub-key).
        cdb.update_project(pid, current_step="seed")
        return jsonify({"success": True, "submitted": True})
    return jsonify({"success": True, "submitted": False})


@bp.route("/projects/<pid>/advance", methods=["POST"])
def advance(pid):
    """Advance to the next step once its prerequisites are met."""
    p = cdb.get_project(pid)
    if not p:
        abort(404)
    body = request.get_json(silent=True) or {}
    to = body.get("to")
    if to == "uploads":
        statuses = cdb.approval_map(pid, [k for k, _, _ in SEED_SUBS])
        if any(v != "approved" for v in statuses.values()):
            return jsonify({"success": False,
                            "error": "All three seed gates must be approved first."}), 400
    cdb.update_project(pid, current_step=to, status=cdb.STATUS_DRAFT)
    return jsonify({"success": True, "redirect": url_for("campaign.project", pid=pid) + f"?step={to}"})


# ---------------------------------------------------------------- uploads ----
# field name -> (source, multiple?, keyword-selection grid?)
UPLOAD_FIELDS = {
    "poe_files":             ("poe",   True,  True),
    "h10_file":              ("h10",   False, True),
    "brand_analytics_file":  ("ba",    False, True),
    "sqp_files":             ("sqp",   True,  True),
    "brand_file":            ("brand", False, True),
    "ba_tst_files":          ("batst", True,  True),
    "str_file":              ("str",   False, False),
}
GRID_LABELS = {"poe": "Product Opportunity Explorer", "h10": "H10 Reverse ASIN",
               "ba": "Brand Analytics", "sqp": "SQP Report", "brand": "Brand (H10)",
               "batst": "BA TST", "str": "Search Term Report"}
ROW_CAP = 2000  # max rows rendered in a selection grid


def _label_for(source, filename):
    base = filename.rsplit(".", 1)[0]
    if source == "sqp":
        return orch._match_asin(filename) or base
    if source == "batst":
        return orch._ba_word(filename) or base
    if source == "poe":
        return base
    return GRID_LABELS.get(source, base)


@bp.route("/projects/<pid>/uploads", methods=["GET"])
def get_uploads(pid):
    if not cdb.get_project(pid):
        abort(404)
    return jsonify({"success": True, "uploads": cdb.get_state(pid, "uploads", [])})


@bp.route("/projects/<pid>/uploads", methods=["POST"])
def post_uploads(pid):
    if not cdb.get_project(pid):
        abort(404)
    uploads = cdb.get_state(pid, "uploads", [])
    added = 0
    for field, (source, multiple, has_grid) in UPLOAD_FIELDS.items():
        files = request.files.getlist(field) if multiple else \
            ([request.files[field]] if field in request.files else [])
        for fs in files:
            if not getattr(fs, "filename", ""):
                continue
            filekey = uuid.uuid4().hex
            try:
                cstore.save_raw(pid, filekey, fs)
                fs.stream.seek(0)
                grid = orch.parse_upload(fs, source)
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                return jsonify({"success": False, "error": f"{fs.filename}: {e}"}), 400
            cstore.save_parsed(pid, filekey, grid)
            uploads.append({
                "filekey": filekey, "source": source, "field": field,
                "filename": fs.filename, "label": _label_for(source, fs.filename),
                "rowcount": len(grid["rows"]), "cols": len(grid["columns"]),
                "keyword_col": grid["keyword_col"],
                "asin_cols": grid["asin_cols"], "has_grid": has_grid,
            })
            added += 1
    cdb.save_state(pid, "uploads", uploads)
    return jsonify({"success": True, "added": added, "uploads": uploads})


@bp.route("/projects/<pid>/uploads/<filekey>/delete", methods=["POST"])
def delete_upload(pid, filekey):
    if not cdb.get_project(pid):
        abort(404)
    uploads = [u for u in cdb.get_state(pid, "uploads", []) if u["filekey"] != filekey]
    cdb.save_state(pid, "uploads", uploads)
    sels = cdb.get_state(pid, "selections", {})
    sels.pop(filekey, None)
    cdb.save_state(pid, "selections", sels)
    cstore.delete_file(pid, filekey)
    return jsonify({"success": True, "uploads": uploads})


@bp.route("/projects/<pid>/table/<filekey>")
def get_table(pid, filekey):
    if not cdb.get_project(pid):
        abort(404)
    grid = cstore.load_parsed(pid, filekey)
    if grid is None:
        abort(404)
    sels = cdb.get_state(pid, "selections", {}).get(filekey, {})
    rows = grid["rows"][:ROW_CAP]
    return jsonify({"success": True, "columns": grid["columns"], "rows": rows,
                    "keyword_col": grid["keyword_col"], "asin_cols": grid["asin_cols"],
                    "selections": sels, "truncated": len(grid["rows"]) > ROW_CAP,
                    "total": len(grid["rows"])})


@bp.route("/projects/<pid>/selections/<filekey>", methods=["POST"])
def save_selections(pid, filekey):
    if not cdb.get_project(pid):
        abort(404)
    body = request.get_json(silent=True) or {}
    sels = cdb.get_state(pid, "selections", {})
    sels[filekey] = body.get("selections", {})
    cdb.save_state(pid, "selections", sels)
    return jsonify({"success": True})


# ----------------------------------------------------- ASIN selection (PAT) ---
PAT_TAGS = ["Main", "Low Rated", "High Priced", "Bestselling", "Non-relevant"]


@bp.route("/projects/<pid>/asin-table/<filekey>")
def asin_table(pid, filekey):
    """Unique ASINs found in a file's ASIN columns, with a context label + current tags."""
    if not cdb.get_project(pid):
        abort(404)
    grid = cstore.load_parsed(pid, filekey)
    if grid is None:
        abort(404)
    kc = grid.get("keyword_col")
    seen, out = set(), []
    for row in grid["rows"]:
        ctx = (row[kc] if (kc is not None and kc < len(row)) else "") or ""
        for ci in grid.get("asin_cols", []):
            if ci >= len(row):
                continue
            val = str(row[ci] or "").strip()
            m = orch._ASIN_RE.search(val)
            if not m:
                continue
            asin = m.group(0).upper()
            if asin in seen:
                continue
            seen.add(asin)
            out.append({"asin": asin, "context": str(ctx)[:60],
                        "col": grid["columns"][ci] if ci < len(grid["columns"]) else ""})
    return jsonify({"success": True, "asins": out,
                    "tags": cdb.get_state(pid, "asin_tags", {}), "pat_tags": PAT_TAGS})


@bp.route("/projects/<pid>/asin-tags", methods=["POST"])
def save_asin_tags(pid):
    if not cdb.get_project(pid):
        abort(404)
    body = request.get_json(silent=True) or {}
    tags = cdb.get_state(pid, "asin_tags", {})
    tags.update(body.get("tags", {}))
    # drop cleared entries
    tags = {k: v for k, v in tags.items() if v}
    cdb.save_state(pid, "asin_tags", tags)
    return jsonify({"success": True, "count": len(tags)})


# --------------------------------------------------- assemble / build -------
@bp.route("/projects/<pid>/assemble")
def assemble_preview(pid):
    """Live preview of what will be written (Semantics / Master / Campaigns)."""
    if not cdb.get_project(pid):
        abort(404)
    from utils import campaign_builder as cb
    try:
        inp, meta = cb.assemble(pid)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500
    semantics = [{"keyword": s["keyword"], "source": s["source"], "category": s["category"],
                  "type": s["kw_type"], "match": s["match"]} for s in inp.semantics_rows]
    campaigns = [{"name": cb.campaign_name(r), "type": r.get("E"), "match": r.get("F"),
                  "root": r.get("G")} for r in inp.campaign_rows]
    master = {"competitor_kws": inp.competitor_kws, "competitor_searches": inp.competitor_searches,
              "own_branded_kws": inp.own_branded_kws, "own_branded_searches": inp.own_branded_searches,
              "own_brand_asins": inp.own_brand_asins}
    pat = [{"asin": t["asin"], "type": t["type"]} for t in inp.pat_targets]
    return jsonify({"success": True, "meta": meta, "semantics": semantics,
                    "campaigns": campaigns, "master": master, "pat": pat})


@bp.route("/projects/<pid>/build", methods=["POST"])
def build_workbook(pid):
    p = cdb.get_project(pid)
    if not p:
        abort(404)
    from utils import campaign_builder as cb
    import time as _t
    safe = "".join(ch if ch.isalnum() else "_" for ch in (p.get("name") or "Campaign")).strip("_") or "Campaign"
    filename = f"Campaigns_{safe}_{_t.strftime('%Y%m%d-%H%M%S')}.xlsx"
    out_path = os.path.join(cfg.OUTPUT_FOLDER, filename)
    try:
        _, meta = cb.build_from_project(pid, out_path)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500
    cdb.update_project(pid, status=cdb.STATUS_COMPLETED, current_step="build")
    return jsonify({"success": True, "filename": filename, "meta": meta,
                    "download": url_for("download_file", filename=filename)})


# -------------------------------------------------------------- approvals ----
@bp.route("/projects/<pid>/approve", methods=["POST"])
def approve(pid):
    if _role() != cdb.ROLE_MANAGER:
        return jsonify({"success": False, "error": "Manager role required"}), 403
    body = request.get_json(silent=True) or {}
    cdb.approve(pid, body.get("step_key", ""), _current_user(), body.get("note"))
    return jsonify({"success": True})
