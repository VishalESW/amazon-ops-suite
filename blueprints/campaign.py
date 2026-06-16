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

import io
import os
import traceback
import uuid

import pandas as pd
from flask import (Blueprint, render_template, request, jsonify, redirect,
                   url_for, g, abort)
from werkzeug.datastructures import FileStorage

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
    uploads = cdb.get_state(pid, "uploads", [])
    sels = cdb.get_state(pid, "selections", {})
    for u in uploads:
        file_sels = sels.get(u["filekey"], {})
        u["has_y_or_comp"] = any(v in ("Y", "Competitor") for v in file_sels.values())
    return jsonify({"success": True, "uploads": uploads})


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


@bp.route("/projects/<pid>/upload-workbook", methods=["POST"])
def upload_workbook(pid):
    """Accept a single multi-sheet Excel workbook and auto-detect each sheet's source."""
    if not cdb.get_project(pid):
        abort(404)
    fs = request.files.get("workbook")
    if not fs or not getattr(fs, "filename", ""):
        return jsonify({"success": False, "error": "No file provided"}), 400
    if not fs.filename.lower().endswith((".xlsx", ".xls")):
        return jsonify({"success": False, "error": "Only .xlsx / .xls files are supported"}), 400

    raw = fs.read()
    try:
        xf = pd.ExcelFile(io.BytesIO(raw))
    except Exception as e:
        return jsonify({"success": False, "error": f"Cannot read workbook: {e}"}), 400

    uploads = cdb.get_state(pid, "uploads", [])
    added, summary = 0, []

    for sheet_name in xf.sheet_names:
        try:
            raw_df = xf.parse(sheet_name, header=None, dtype=object).fillna("")
        except Exception as e:
            summary.append({"sheet": sheet_name, "source": None, "status": "error",
                            "error": str(e)})
            continue

        source = orch.detect_source_from_sheet(sheet_name, raw_df)
        if not source:
            summary.append({"sheet": sheet_name, "source": None, "status": "unrecognized",
                            "rows": len(raw_df), "cols": len(raw_df.columns)})
            continue

        has_grid = source != "str"
        try:
            grid = orch.parse_upload(None, source, raw_df=raw_df)
        except Exception as e:
            traceback.print_exc()
            summary.append({"sheet": sheet_name, "source": source, "status": "error",
                            "error": str(e)})
            continue

        # Store each sheet as its own raw xlsx so the existing _fs() pipeline works.
        filekey = uuid.uuid4().hex
        bio = io.BytesIO()
        raw_df.to_excel(bio, index=False, header=False, engine="openpyxl")
        bio.seek(0)
        cstore.save_raw(pid, filekey,
                        FileStorage(stream=bio, filename=f"{sheet_name}.xlsx"))
        cstore.save_parsed(pid, filekey, grid)

        label = _label_for(source, sheet_name)
        uploads.append({
            "filekey": filekey, "source": source, "field": source,
            "filename": f"{sheet_name} [{fs.filename}]",
            "label": label,
            "rowcount": len(grid["rows"]), "cols": len(grid["columns"]),
            "keyword_col": grid["keyword_col"],
            "asin_cols": grid["asin_cols"], "has_grid": has_grid,
        })
        summary.append({
            "sheet": sheet_name, "source": source, "label": label,
            "rows": len(grid["rows"]), "cols": len(grid["columns"]),
            "has_keywords": grid["keyword_col"] is not None,
            "has_asins": bool(grid["asin_cols"]),
            "status": "ok",
        })
        added += 1

    cdb.save_state(pid, "uploads", uploads)
    return jsonify({"success": True, "added": added, "summary": summary, "uploads": uploads})


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

    # Product name lookup from ASIN dashboard state
    asin_state = cdb.get_state(pid, "asins") or {}
    products_all = asin_state.get("products") or []
    name_by_asin = {p["asin"]: p.get("name", "") for p in products_all}

    # Only show rows whose keyword was tagged Y or Competitor in the keyword selection step
    file_sels = cdb.get_state(pid, "selections", {}).get(filekey, {})
    y_or_comp = {int(ri) for ri, tag in file_sels.items() if tag in ("Y", "Competitor")}
    all_rows = grid["rows"]
    rows = [row for i, row in enumerate(all_rows) if not y_or_comp or i in y_or_comp]

    kc = grid.get("keyword_col")
    seen, out = set(), []
    for row in rows:
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
                        "col": grid["columns"][ci] if ci < len(grid["columns"]) else "",
                        "product_name": name_by_asin.get(asin, "")})

    # Auto-detect a PAT-type selection column (any column whose values match the tag names)
    pat_tags_lower = {t.lower(): t for t in PAT_TAGS}
    auto_tags = {}
    for col_ci, _col_name in enumerate(grid["columns"]):
        matches = sum(
            1 for row in rows
            if col_ci < len(row) and str(row[col_ci] or "").strip().lower() in pat_tags_lower
        )
        if matches == 0:
            continue
        # Use the first column that has matching values
        for row in rows:
            if col_ci >= len(row):
                continue
            tag_val = str(row[col_ci] or "").strip().lower()
            if tag_val not in pat_tags_lower:
                continue
            for ac in grid.get("asin_cols", []):
                if ac >= len(row):
                    continue
                m = orch._ASIN_RE.search(str(row[ac] or "").strip())
                if m:
                    auto_tags[m.group(0).upper()] = pat_tags_lower[tag_val]
        break  # stop after first matching column

    # Saved tags take precedence over auto-detected ones
    saved_tags = cdb.get_state(pid, "asin_tags", {})
    merged_tags = {**auto_tags, **saved_tags}

    full_rows = rows[:ROW_CAP]
    return jsonify({"success": True, "asins": out,
                    "tags": merged_tags, "pat_tags": PAT_TAGS,
                    "columns": grid["columns"],
                    "rows": full_rows,
                    "asin_cols": grid.get("asin_cols", []),
                    "name_by_asin": name_by_asin,
                    "total": len(rows),
                    "truncated": len(rows) > ROW_CAP})


@bp.route("/projects/<pid>/asin-tags", methods=["GET"])
def get_asin_tags_r(pid):
    if not cdb.get_project(pid):
        abort(404)
    return jsonify({"success": True,
                    "tags": cdb.get_state(pid, "asin_tags", {}),
                    "names": cdb.get_state(pid, "asin_names", {}),
                    "meta": cdb.get_state(pid, "asin_pat", {})})


@bp.route("/projects/<pid>/asin-tags", methods=["POST"])
def save_asin_tags(pid):
    if not cdb.get_project(pid):
        abort(404)
    body = request.get_json(silent=True) or {}
    tags = cdb.get_state(pid, "asin_tags", {})
    tags.update(body.get("tags", {}))
    tags = {k: v for k, v in tags.items() if v}
    cdb.save_state(pid, "asin_tags", tags)
    if "names" in body:
        names = cdb.get_state(pid, "asin_names", {})
        names.update(body.get("names", {}))
        names = {k: v for k, v in names.items() if v}
        cdb.save_state(pid, "asin_names", names)
    if "meta" in body:
        # Per-ASIN PAT-sheet inputs: {asin: {source,product,asp,acos}}. Drop
        # entries where every field is blank so state stays lean.
        meta = cdb.get_state(pid, "asin_pat", {})
        for asin, m in (body.get("meta") or {}).items():
            cur = meta.get(asin, {})
            cur.update({k: v for k, v in m.items()})
            cur = {k: v for k, v in cur.items() if v not in ("", None)}
            if cur:
                meta[asin] = cur
            else:
                meta.pop(asin, None)
        cdb.save_state(pid, "asin_pat", meta)
    return jsonify({"success": True, "count": len(tags)})


@bp.route("/projects/<pid>/pat-flat", methods=["GET"])
def get_pat_flat(pid):
    """Flat deduplicated ASIN list from Y/Competitor keywords in SQP, BA, and POE uploads."""
    if not cdb.get_project(pid):
        abort(404)

    ALLOWED = {"sqp", "ba", "poe"}
    SRC_LABEL = {"sqp": "SQP", "ba": "Brand Analytics", "poe": "POE"}

    uploads = cdb.get_state(pid, "uploads", [])
    sels = cdb.get_state(pid, "selections", {})
    asin_tags = cdb.get_state(pid, "asin_tags", {})
    flat_edits = cdb.get_state(pid, "pat_flat_edits", {})

    # Build CVR map from h10/str/ba/brand raw files
    cvr_by_kw = {}
    for u in uploads:
        src = u.get("source", "")
        if src not in ("h10", "str", "ba", "brand"):
            continue
        path = cstore.raw_path(pid, u["filekey"])
        if not path or not os.path.exists(path):
            continue
        try:
            with open(path, "rb") as f:
                data = f.read()
            fs = FileStorage(stream=io.BytesIO(data), filename=u["filename"])
            df = orch.read_table_smart(fs, src)
            cvr_by_kw.update(orch.extract_cvr_by_kw(df, src))
        except Exception:
            pass

    seen = {}  # asin -> row dict; first occurrence wins

    for u in uploads:
        src = u.get("source", "")
        if src not in ALLOWED:
            continue
        if not u.get("has_grid"):
            continue
        file_sels = sels.get(u["filekey"], {})
        y_comp = {int(ri): tag for ri, tag in file_sels.items() if tag in ("Y", "Competitor")}
        if not y_comp:
            continue
        grid = cstore.load_parsed(pid, u["filekey"])
        if not grid:
            continue
        rows = grid["rows"]
        kc = grid.get("keyword_col")
        asin_cols = grid.get("asin_cols", [])
        columns = grid.get("columns", [])

        # Try to find a "7 Day Total Orders" or "Purchases - Total" metric column
        orders_ci = None
        for ci, col in enumerate(columns):
            cl = col.strip().lower()
            if ("7 day" in cl and "order" in cl) or ("purchase" in cl and "total" in cl):
                orders_ci = ci
                break

        for ridx in y_comp:
            try:
                row = rows[ridx]
            except IndexError:
                continue
            kw = str(row[kc]).strip() if kc is not None and kc < len(row) else ""
            orders = str(row[orders_ci]).strip() if orders_ci is not None and orders_ci < len(row) else ""
            cvr = cvr_by_kw.get(kw.lower(), "")

            for ci in asin_cols:
                if ci >= len(row):
                    continue
                m = orch._ASIN_RE.search(str(row[ci] or "").strip())
                if not m:
                    continue
                asin = m.group(0).upper()
                if asin in seen:
                    continue
                seen[asin] = {
                    "asin": asin,
                    "source": SRC_LABEL.get(src, src.upper()),
                    "keyword": kw,
                    "orders": orders,
                    "cvr": cvr,
                    "type": asin_tags.get(asin, ""),
                    "cpc": "",
                    "acos_pct": "",
                    "product": "",
                    "placement_mod": "",
                    "asp": "",
                    "acos_target": "",
                }

    return jsonify({"success": True, "rows": list(seen.values()), "edits": flat_edits})


@bp.route("/projects/<pid>/pat-flat-edits", methods=["POST"])
def save_pat_flat_edits(pid):
    if not cdb.get_project(pid):
        abort(404)
    body = request.get_json(silent=True) or {}
    cdb.save_state(pid, "pat_flat_edits", body.get("edits", {}))
    return jsonify({"success": True})


@bp.route("/projects/<pid>/pat-table/<filekey>")
def get_pat_table(pid, filekey):
    """All unique ASINs from every ASIN column in one SQP/BA/POE file."""
    if not cdb.get_project(pid):
        abort(404)
    grid = cstore.load_parsed(pid, filekey)
    if grid is None:
        abort(404)

    columns = grid.get("columns", [])
    asin_cols = grid.get("asin_cols", [])
    all_rows = grid["rows"]

    # Fallback: derive asin_cols from column names if parsing missed them.
    if not asin_cols:
        asin_cols = [i for i, c in enumerate(columns) if "asin" in c.lower()]

    asin_tags = cdb.get_state(pid, "asin_tags", {})

    # Collect all unique ASINs from every ASIN column across all rows.
    asin_info = {}  # asin -> {count, cols: set of column names}
    for row in all_rows:
        for ci in asin_cols:
            if ci >= len(row):
                continue
            m = orch._ASIN_RE.search(str(row[ci] or "").strip())
            if not m:
                continue
            asin = m.group(0).upper()
            col_name = columns[ci] if ci < len(columns) else ""
            if asin not in asin_info:
                asin_info[asin] = {"count": 0, "cols": set()}
            asin_info[asin]["count"] += 1
            asin_info[asin]["cols"].add(col_name)

    asins_out = sorted([
        {
            "asin": asin,
            "count": info["count"],
            "cols": sorted(info["cols"]),
            "tag": asin_tags.get(asin, ""),
        }
        for asin, info in asin_info.items()
    ], key=lambda x: -x["count"])

    col_names = [columns[i] for i in asin_cols if i < len(columns)]
    return jsonify({"success": True, "asins": asins_out, "col_names": col_names})


@bp.route("/projects/<pid>/pat-all-asins")
def get_pat_all_asins(pid):
    """Flat deduplicated ASIN list from all SQP/BA/POE files with product names."""
    if not cdb.get_project(pid):
        abort(404)
    ALLOWED = {"sqp", "ba", "poe"}
    uploads = cdb.get_state(pid, "uploads", [])
    asin_tags = cdb.get_state(pid, "asin_tags", {})
    asin_names = cdb.get_state(pid, "asin_names", {})
    asin_pat = cdb.get_state(pid, "asin_pat", {})  # {asin: {source,product,asp,acos}}

    asin_state = cdb.get_state(pid, "asins") or {}
    products_all = asin_state.get("products") or []
    name_by_asin = {p["asin"]: p.get("name", "") for p in products_all}
    # Defaults mirror campaign_builder.assemble (first selected product / ASP).
    selected = set(asin_state.get("selected") or [p["asin"] for p in products_all])
    chosen = [p for p in products_all if p["asin"] in selected] or products_all
    default_product = chosen[0].get("name", "") if chosen else ""
    default_asp = next((p.get("asp") for p in chosen if p.get("asp")), 24.95) or 24.95
    default_acos = 0.30

    # Conversion rate per ASIN — mirrors the PAT sheet's H column
    # =XLOOKUP(asin, 'Search Term Report'!A:A, …!R:R): look the ASIN up in the
    # STR (PT report search terms are ASINs). cvr_calc replicates the P column
    # =IF(H>1, H/100, H) so a % like 14.3 becomes the 0.143 fraction.
    cvr_by_asin = {}
    for u in uploads:
        if u.get("source") != "str":
            continue
        path = cstore.raw_path(pid, u.get("filekey"))
        if not path or not os.path.exists(path):
            continue
        with open(path, "rb") as f:
            fs = FileStorage(stream=io.BytesIO(f.read()), filename=u.get("filename"))
        df = orch.read_table_smart(fs, "str")
        cvr_by_asin.update(orch.extract_cvr_by_kw(df, "str"))

    def _cvr_calc(v):
        try:
            f = float(str(v).replace("%", "").replace(",", "").strip())
        except (TypeError, ValueError):
            return ""
        return round(f / 100, 4) if f > 1 else round(f, 4)

    # asin -> first non-empty product title found across all files
    asin_to_title = {}
    # asin -> conversion rate from the source row where it appears (POE/BA/SQP).
    # A regular STR holds keyword search terms, not ASINs, so the workbook's
    # STR lookup is usually empty for competitor ASINs — fall back to the
    # conversion rate of the file row that surfaced the ASIN.
    asin_to_cvr = {}
    CVR_TOKENS = ("conversion rate", "conversion share", "cvr")

    for u in uploads:
        if u.get("source") not in ALLOWED or not u.get("has_grid"):
            continue
        grid = cstore.load_parsed(pid, u["filekey"])
        if not grid:
            continue
        columns = grid.get("columns", [])
        asin_cols = grid.get("asin_cols", [])
        if not asin_cols:
            asin_cols = [i for i, c in enumerate(columns) if "asin" in c.lower()]

        # For POE files: "Top Clicked Product N (Asin)" pairs with "Top Clicked Product N (Title)"
        # Build asin_col_idx -> title_col_idx map using the (Asin)→(Title) name substitution.
        title_col_map = {}
        col_lows = [c.lower() for c in columns]
        for ci in asin_cols:
            if ci >= len(columns):
                continue
            col_low = col_lows[ci]
            if col_low.endswith("(asin)"):
                prefix = columns[ci][:-6]  # strip trailing "(Asin)" — always 6 chars
                target_low = prefix.lower() + "(title)"
                for ti, cl in enumerate(col_lows):
                    if cl == target_low:
                        title_col_map[ci] = ti
                        break

        # Conversion-rate column for this file (first header matching a CVR token).
        cvr_col = next((i for i, cl in enumerate(col_lows)
                        if any(tok in cl for tok in CVR_TOKENS)), None)

        for row in grid["rows"]:
            row_cvr = ""
            if cvr_col is not None and cvr_col < len(row):
                row_cvr = str(row[cvr_col] or "").strip()
            for ci in asin_cols:
                if ci >= len(row):
                    continue
                m = orch._ASIN_RE.search(str(row[ci] or "").strip())
                if not m:
                    continue
                asin = m.group(0).upper()
                if asin not in asin_to_title:
                    asin_to_title[asin] = ""
                # Fill title on the first row that has one for this ASIN
                if not asin_to_title[asin]:
                    ti = title_col_map.get(ci)
                    if ti is not None and ti < len(row):
                        title = str(row[ti] or "").strip()
                        if title:
                            asin_to_title[asin] = title
                # Fill conversion rate on the first row that has one for this ASIN
                if not asin_to_cvr.get(asin) and row_cvr and row_cvr.lower() not in ("nan", "none", "0", "0.0"):
                    asin_to_cvr[asin] = row_cvr

    def _row(asin, title):
        m = asin_pat.get(asin) or {}
        # STR lookup wins (workbook-faithful); else the source-file conversion rate.
        raw_cvr = cvr_by_asin.get(asin.lower(), "") or asin_to_cvr.get(asin, "")
        return {
            "asin": asin,
            # Priority: user-saved name > AdLabs product name > file-sourced title
            "product_name": asin_names.get(asin) or name_by_asin.get(asin, "") or title,
            "tag": asin_tags.get(asin, ""),
            # Per-ASIN PAT-sheet inputs (saved overrides; blank = use default placeholder)
            "source": m.get("source", ""),
            "product": m.get("product", ""),
            "asp": m.get("asp", ""),
            "acos": m.get("acos", ""),
            # Conversion rate (col H, raw lookup) + calc (col P, normalized fraction)
            "cvr": raw_cvr,
            "cvr_calc": _cvr_calc(raw_cvr),
        }

    asins_out = sorted([_row(asin, title) for asin, title in asin_to_title.items()],
                       key=lambda x: x["asin"])

    return jsonify({"success": True, "asins": asins_out,
                    "defaults": {"source": "Competitor", "product": default_product,
                                 "asp": default_asp, "acos": default_acos}})


@bp.route("/projects/<pid>/pat-row-edits", methods=["POST"])
def save_pat_row_edits(pid):
    if not cdb.get_project(pid):
        abort(404)
    body = request.get_json(silent=True) or {}
    filekey = body.get("filekey")
    if not filekey:
        return jsonify({"success": False, "error": "filekey required"}), 400
    all_edits = cdb.get_state(pid, "pat_row_edits", {})
    all_edits[filekey] = body.get("edits", {})
    cdb.save_state(pid, "pat_row_edits", all_edits)
    return jsonify({"success": True})


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
    # ASIN dashboard products for the Product ASIN dropdown
    products = [{"asin": pr["asin"], "name": pr.get("name", ""), "sku": pr.get("sku", "")}
                for pr in inp.products]
    # Full Semantics sheet (every column A-T), one dict per row.
    semantics = [{
        "keyword": s.get("keyword", ""), "source": s.get("source", ""),
        "sv": s.get("sv", 0),                       # C  Search Volume (read-only)
        "ctr": s.get("ctr", ""),                    # J
        "category": s.get("category", ""),          # K  Root KW
        "disp_kw_type": s.get("disp_kw_type", ""),  # L
        "disp_match": s.get("disp_match", ""),      # M
        "disp_broad": s.get("disp_broad", ""),      # N
        "product": s.get("product", ""),            # O
        "product_asin": s.get("product_asin", ""),  # O+
        "placement_mod": s.get("placement_mod", ""),# Q
        "asp": s.get("asp", ""),                    # R
        "acos_target": s.get("acos_target", ""),    # S
        "cvr": s.get("cvr", ""),                    # Conversion Rate (read-only)
        # computed (read-only) — shown as the SV-rule suggestion placeholder
        "_auto_kw_type": s.get("kw_type", ""), "_auto_match": s.get("match", ""),
    } for s in inp.semantics_rows]
    campaigns = [{"name": cb.campaign_name(r), "type": r.get("E"), "match": r.get("F"),
                  "root": r.get("G")} for r in inp.campaign_rows]
    master = {"competitor_kws": inp.competitor_kws, "competitor_searches": inp.competitor_searches,
              "own_branded_kws": inp.own_branded_kws, "own_branded_searches": inp.own_branded_searches,
              "own_brand_asins": inp.own_brand_asins}
    pat = [{"asin": t.get("asin", ""), "type": t.get("type", ""),
            "product": t.get("product", ""), "asp": t.get("asp", ""),
            "acos": t.get("acos", "")} for t in inp.pat_targets]
    return jsonify({"success": True, "meta": meta, "semantics": semantics,
                    "products": products,
                    "sem_columns": cb.SEM_EDITABLE, "pat_columns": cb.PAT_EDITABLE,
                    "campaigns": campaigns, "master": master, "pat": pat})


@bp.route("/projects/<pid>/semantics-edits", methods=["POST"])
def save_semantics_edits(pid):
    """Persist per-row Semantics edits {rowIndex: {field: value}} for the build."""
    if not cdb.get_project(pid):
        abort(404)
    edits = (request.get_json(silent=True) or {}).get("edits") or {}
    cdb.save_state(pid, "semantics_edits", edits)
    return jsonify({"success": True, "count": len(edits)})


@bp.route("/projects/<pid>/pat-edits", methods=["POST"])
def save_pat_edits(pid):
    """Persist per-row PAT edits {rowIndex: {field: value}} for the build."""
    if not cdb.get_project(pid):
        abort(404)
    edits = (request.get_json(silent=True) or {}).get("edits") or {}
    cdb.save_state(pid, "pat_edits", edits)
    return jsonify({"success": True, "count": len(edits)})


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
