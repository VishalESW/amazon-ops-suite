"""Campaign Processor v2 — assemble the workbook from a project's saved state.

Pulls together: AdLabs ASIN dashboard, uploaded files (passthrough tabs), the
manual keyword selections (Y -> Semantics; Competitor/Brand -> Master KW), the
ASIN/PAT tags, root-keyword generation + the SV-digit campaign rule, and bidding.
Produces the .xlsx via campaign_engine.
"""

from __future__ import annotations

import io
import os

from werkzeug.datastructures import FileStorage

from utils import campaign_db as cdb
from utils import campaign_store as cstore
from utils import campaign_ai as ai
from utils import campaign_orchestrator as orch
from utils.campaign_engine import build, BuildInput

# upload.source -> Semantics "source" tag
SRC_TAG = {"poe": "POE", "h10": "H10", "ba": "BA", "sqp": "SQP", "brand": "Brand"}
# PAT type -> MKL bucket field
PAT_BUCKET = {"Main": "main_competitor_asins", "Low Rated": "lower_rated_asins",
              "High Priced": "higher_priced_asins", "Bestselling": "bestselling_asins"}

DEFAULT_ACOS = 0.30
DEFAULT_PLACEMENT = 0.25
DEFAULT_ASP = 24.95

# Editable grids: which fields the user may override + which are numeric.
# Edits are saved per-row by index: {"<rowIndex>": {field: value, ...}}.
SEM_EDITABLE = ["keyword", "source", "organic_rank", "impression_share", "ctr",
                "category", "disp_kw_type", "disp_match", "disp_broad", "product",
                "placement_mod", "asp", "acos_target"]
SEM_NUMERIC = {"placement_mod", "asp", "acos_target"}
PAT_EDITABLE = ["asin", "type", "product", "asp", "acos"]
PAT_NUMERIC = {"asp", "acos"}


def _apply_grid_edits(rows, edits, numeric_fields):
    """Override row fields with the user's saved edits (keyed by row index).

    Additive: with no edits the rows are untouched. Numeric fields are coerced
    to float; blanks/invalid numbers are ignored so a bad cell never breaks build.
    """
    if not edits:
        return
    for i, row in enumerate(rows):
        e = edits.get(str(i))
        if not isinstance(e, dict):
            continue
        for field, val in e.items():
            if field in numeric_fields:
                try:
                    row[field] = float(str(val).replace(",", "").replace("%", "").strip())
                except (TypeError, ValueError):
                    continue   # keep the computed default
            else:
                row[field] = val


def _fs(pid, upload):
    """Re-open a stored raw upload as a werkzeug FileStorage for re-parsing."""
    path = cstore.raw_path(pid, upload["filekey"])
    if not path or not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        data = f.read()
    return FileStorage(stream=io.BytesIO(data), filename=upload["filename"])


def _digit_rule(sv):
    """SV digit count -> (kw_type, match). 6->SKW Ex, 5->MKW Ex, 4->Br.M, 3->Br., <=2->Ph."""
    try:
        n = int(round(float(sv)))
    except (TypeError, ValueError):
        n = 0
    d = len(str(abs(n))) if n else 0
    if d >= 6:
        return "SKW", "Ex."
    if d == 5:
        return "MKW", "Ex."
    if d == 4:
        return "MKW", "Br.M"
    if d == 3:
        return "MKW", "Br."
    return "MKW", "Ph."


def build_from_project(pid, out_path):
    inp, meta = assemble(pid)
    build(inp, out_path)
    return out_path, meta


def assemble(pid):
    """Construct the BuildInput + meta from saved project state (no file written)."""
    p = cdb.get_project(pid)
    if not p:
        raise ValueError("project not found")
    state = cdb.all_state(pid)
    uploads = state.get("uploads") or []
    selections = state.get("selections") or {}
    asin_tags = state.get("asin_tags") or {}
    asin_state = state.get("asins") or {}
    products_all = asin_state.get("products") or []
    selected_asins = set(asin_state.get("selected") or [p_["asin"] for p_ in products_all])

    brand = p.get("profile_name") or "Brand"
    inp = BuildInput(brand=brand)

    # ---- ASIN List + CTR block (from AdLabs) -------------------------------
    chosen = [pr for pr in products_all if pr["asin"] in selected_asins] or products_all
    inp.products = [{"asin": pr["asin"], "sku": pr.get("sku", ""), "name": pr.get("name", "")}
                    for pr in chosen]
    inp.asin_ctr = [{
        "asin": pr["asin"], "sku": pr.get("sku", ""), "title": pr.get("name", ""),
        "state": "Active", "profile": brand,
        "impression": pr.get("impressions"), "click": pr.get("clicks"), "ctr": pr.get("ctr"),
        "spend": pr.get("spend"), "cpc": pr.get("cpc"), "sales": pr.get("sales"),
        "orders": pr.get("orders"), "acos": pr.get("acos"), "price": pr.get("price"),
        "asp": pr.get("asp"),
    } for pr in chosen]
    default_product = inp.products[0]["name"] if inp.products else brand
    default_asp = next((pr.get("asp") for pr in chosen if pr.get("asp")), DEFAULT_ASP) or DEFAULT_ASP

    # ---- Passthrough tabs + per-keyword search volume ----------------------
    sv_by_kw = {}
    for u in uploads:
        src = u["source"]
        fs = _fs(pid, u)
        if fs is None:
            continue
        if src == "poe":
            df = orch.read_table_smart(fs, "poe")
            inp.poe_tables[u["label"]] = orch._canonicalize(df, orch.POE_CANON)
            _collect_sv(sv_by_kw, orch._extract(df, "POE"))
        elif src == "h10":
            df = orch.read_table_smart(fs, "h10")
            inp.h10_table = orch._canonicalize(df, orch.H10_CANON)
            _collect_sv(sv_by_kw, orch._extract(df, "H10"))
        elif src == "ba":
            df = orch.read_table_smart(fs, "ba")
            inp.brand_analytics_table = orch._rows(df)
            _collect_sv(sv_by_kw, orch._extract(df, "BA"))
        elif src == "sqp":
            df = orch.read_table_smart(fs, "sqp")
            # Prefer an ASIN from the filename; else a short index so the SQP tab
            # name stays valid/short (Excel caps sheet names at 31 chars).
            asin = orch._match_asin(u["filename"]) or str(len(inp.sqp_reports) + 1)
            inp.sqp_reports[asin] = orch._canonicalize(df, orch.SQP_CANON)
            _collect_sv(sv_by_kw, orch._extract(df, "SQP"))
        elif src == "brand":
            df = orch.read_table_smart(fs, "brand")
            inp.brand_table = orch._rows(df)
            _collect_sv(sv_by_kw, orch._extract(df, "Brand"))
        elif src == "batst":
            inp.ba_tst[u["label"]] = orch._rows(orch.read_table_smart(fs, "batst"))
        elif src == "str":
            df = orch.read_table_smart(fs, "str")
            inp.str_table = orch._canonicalize(df, orch.STR_CANON)

    # ---- Walk selections: Y -> Semantics, Competitor/Brand -> Master KW ----
    y_kws, comp_searches, comp_names = [], [], []
    own_searches, own_names = [], []
    seen_y = set()
    for u in uploads:
        if not u.get("has_grid") or u.get("keyword_col") is None:
            continue
        grid = cstore.load_parsed(pid, u["filekey"])
        if not grid:
            continue
        kc = grid["keyword_col"]
        bcols = grid.get("brand_cols") or []
        rows = grid["rows"]
        for ridx, tag in (selections.get(u["filekey"]) or {}).items():
            try:
                row = rows[int(ridx)]
            except (ValueError, IndexError):
                continue
            kw = str(row[kc]).strip() if kc < len(row) else ""
            if not kw:
                continue
            bname = next((str(row[i]).strip() for i in bcols
                          if i < len(row) and str(row[i]).strip()), "")
            if tag == "Y":
                nk = kw.lower()
                if nk not in seen_y:
                    seen_y.add(nk)
                    y_kws.append({"keyword": kw, "source": SRC_TAG.get(u["source"], u["source"])})
            elif tag == "Competitor":
                comp_searches.append(kw)
                if bname:
                    comp_names.append(bname)
            elif tag == "Brand":
                own_searches.append(kw)
                own_names.append(bname or brand)

    # ---- Root keywords + categories ----------------------------------------
    kw_texts = [c["keyword"] for c in y_kws]
    ctx = f"{brand}: {default_product}"
    # Persist roots on first assembly so the preview and the build agree (the AI is
    # non-deterministic). Cleared automatically when keyword selections change count.
    cached = state.get("roots") or {}
    if cached.get("n") == len(kw_texts) and cached.get("items"):
        roots = cached["items"]
    else:
        roots = ai.generate_roots(kw_texts, ctx) if kw_texts else []
        cdb.save_state(pid, "roots", {"items": roots, "n": len(kw_texts)})
    inp.root_categories = roots or ["0-Gen"]
    usage = {}

    # ---- Semantics rows ----------------------------------------------------
    sem_rows = []
    for c in y_kws:
        kw = c["keyword"]
        sv = sv_by_kw.get(kw.lower(), 0)
        kw_type, match = _digit_rule(sv)
        root = ai.assign_root(kw, roots, usage) if roots else ""
        sem_rows.append({
            "keyword": kw, "source": c["source"],
            "category": root or (roots[0] if roots else "0-Gen"),
            "kw_type": kw_type, "match": match, "product": default_product,
            "placement_mod": DEFAULT_PLACEMENT, "asp": default_asp, "acos_target": DEFAULT_ACOS,
            # Sheet-display columns — empty by default, filled only by user edits.
            "disp_kw_type": "", "disp_match": "", "disp_broad": "",
            "organic_rank": "", "impression_share": "", "ctr": "",
            # Search Volume (monthly) — shown read-only in the sheet view; the
            # workbook keeps the live SV formula in column C.
            "sv": int(round(sv)) if sv else 0,
        })
    _apply_grid_edits(sem_rows, state.get("semantics_edits") or {}, SEM_NUMERIC)
    inp.semantics_rows = sem_rows

    # ---- PAT targets + MKL ASIN buckets ------------------------------------
    pat_targets, pat_types = [], []
    for asin, tag in asin_tags.items():
        if tag not in PAT_BUCKET:
            continue
        getattr(inp, PAT_BUCKET[tag]).append(asin)
        if tag not in pat_types:
            pat_types.append(tag)
        pat_targets.append({"asin": asin, "type": tag, "source": "Competitor",
                            "product": default_product, "asp": default_asp, "acos": DEFAULT_ACOS})
    _apply_grid_edits(pat_targets, state.get("pat_edits") or {}, PAT_NUMERIC)
    inp.pat_targets = pat_targets

    # ---- Master Keyword List manual fields ---------------------------------
    inp.competitor_searches = comp_searches
    inp.competitor_kws = list(dict.fromkeys(comp_names))
    inp.own_branded_searches = own_searches
    inp.own_branded_kws = list(dict.fromkeys(own_names)) or [brand]
    inp.own_brand_asins = [a for a in selected_asins]

    # ---- Seed-derived input lists (H10 / Brand ASIN lists) -----------------
    inp.h10_asins = (state.get("seed_h10") or {}).get("items", [])
    inp.brand_asins = inp.own_brand_asins[:1]

    # ---- Campaigns: keyword campaigns + PAT (PT) campaigns -----------------
    inp.campaign_rows = orch._campaign_rows(sem_rows, brand, default_product,
                                            default_asp, DEFAULT_ACOS, DEFAULT_PLACEMENT)
    inp.campaign_rows += orch._pat_campaign_rows(pat_types, brand, default_product,
                                                 default_asp, DEFAULT_ACOS, DEFAULT_PLACEMENT)

    meta = {
        "semantics": len(sem_rows),
        "campaigns": len(inp.campaign_rows),
        "pat_targets": len(pat_targets),
        "roots": roots,
        "competitors": len(inp.competitor_kws),
        "own_brand_kws": len(inp.own_branded_kws),
        "str_included": bool(inp.str_table),
        "cvr_source": "Search Term Report" if inp.str_table else "H10 ABA Total Conv. Share",
    }
    return inp, meta


def campaign_name(row):
    """Replicate the sheet formula I = B | C | E | F | G | H."""
    parts = [row.get(c, "") for c in ("B", "C", "E", "F", "G", "H")]
    return " | ".join(str(x) for x in parts if x not in (None, ""))


def _collect_sv(acc, candidates):
    for c in candidates:
        k = (c.get("keyword") or "").lower()
        v = c.get("search_volume") or 0
        if k and v > acc.get(k, 0):
            acc[k] = v
