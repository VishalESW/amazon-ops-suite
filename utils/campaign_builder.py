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

from config import cfg
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

_ARTICLES = {"the", "a", "an"}


def _short_product(title, brand):
    """Short product label for campaign names: the first MEANINGFUL word of the
    ASIN title — skipping leading articles (The/A/An) and non-alphabetic tokens
    (e.g. '360°', pure numbers). Falls back to the brand. Avoids the old bug where
    'The 360° Total Windshield…' collapsed to 'The'."""
    for w in str(title or "").split():
        if w.lower().strip(".,:;-—") in _ARTICLES:
            continue
        if not any(ch.isalpha() for ch in w):
            continue
        return w
    return brand

# Editable grids: which fields the user may override + which are numeric.
# Edits are saved per-row by index: {"<rowIndex>": {field: value, ...}}.
SEM_EDITABLE = ["keyword", "source", "ctr",
                "category", "disp_kw_type", "disp_match", "disp_broad", "product", "product_asin",
                "placement_mod", "asp", "acos_target"]
SEM_NUMERIC = {"placement_mod", "asp", "acos_target"}
PAT_EDITABLE = ["asin", "type", "product", "asp", "acos"]
PAT_NUMERIC = {"asp", "acos"}


def _apply_grid_edits(rows, edits, numeric_fields, skip_empty=None):
    """Override row fields with the user's saved edits (keyed by row index).

    Additive: with no edits the rows are untouched. Numeric fields are coerced
    to float; blanks/invalid numbers are ignored so a bad cell never breaks build.
    skip_empty: set of field names where an empty-string saved edit should NOT
    erase a freshly generated value (e.g. 'category' from AI root assignment).
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
                if skip_empty and field in skip_empty and not val:
                    continue  # stale blank edit; keep the generated value
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
    # Campaign/Semantics product label = the SHORT product name (first token of the
    # ASIN title, e.g. "Lounge-IT" from "Lounge-IT Car Phone Holder, ..."), not the
    # full title — that's what the client's campaign names use.
    _full_product = inp.products[0]["name"] if inp.products else brand
    default_product = _short_product(_full_product, brand)
    default_asin = inp.products[0]["asin"] if inp.products else ""
    default_sku = inp.products[0].get("sku", "") if inp.products else ""
    default_asp = next((pr.get("asp") for pr in chosen if pr.get("asp")), DEFAULT_ASP) or DEFAULT_ASP
    # Account/product context for Campaign Naming auxiliary columns (brand legal
    # name, portfolio, own ASIN/SKU) — matches the client's master workbook.
    camp_ctx = {
        "profile": brand,                       # J  (Ads profile name)
        "product": default_product,             # B  (short product label)
        "brand_legal": cfg.CAMPAIGN_BRAND_LEGAL, # N  (legal entity)
        "portfolio_suffix": cfg.CAMPAIGN_PORTFOLIO_SUFFIX,  # U = product + suffix
        "asin_sku": (f"{default_asin}/{default_sku}" if default_asin and default_sku
                     else (default_asin or "")),  # AA
    }

    # ---- Passthrough tabs + per-keyword search volume + CVR ----------------
    sv_by_kw = {}
    cvr_by_kw = {}   # str values; STR overrides H10/BA (processed last wins)
    poe_cvr_by_kw = {}  # POE "Search Conversion Rate" — preferred CVR source
    str_metrics = {}  # {kw_lower: {orders,cpc,acos}} from the Search Term Report
    for u in uploads:
        src = u["source"]
        fs = _fs(pid, u)
        if fs is None:
            continue
        if src == "poe":
            df = orch.read_table_smart(fs, "poe")
            inp.poe_tables[u["label"]] = orch._canonicalize(df, orch.POE_CANON)
            _collect_sv(sv_by_kw, orch._extract(df, "POE"))
            poe_cvr_by_kw.update(orch.extract_cvr_by_kw(df, "poe"))
        elif src == "h10":
            df = orch.read_table_smart(fs, "h10")
            inp.h10_table = orch._canonicalize(df, orch.H10_CANON)
            _collect_sv(sv_by_kw, orch._extract(df, "H10"))
            cvr_by_kw.update(orch.extract_cvr_by_kw(df, "h10"))
        elif src == "ba":
            df = orch.read_table_smart(fs, "ba")
            inp.brand_analytics_table = orch._rows(df)
            _collect_sv(sv_by_kw, orch._extract(df, "BA"))
            cvr_by_kw.update(orch.extract_cvr_by_kw(df, "ba"))
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
            cvr_by_kw.update(orch.extract_cvr_by_kw(df, "brand"))
        elif src == "batst":
            inp.ba_tst[u["label"]] = orch._rows(orch.read_table_smart(fs, "batst"))
        elif src == "str":
            df = orch.read_table_smart(fs, "str")
            inp.str_table = orch._canonicalize(df, orch.STR_CANON)
            cvr_by_kw.update(orch.extract_cvr_by_kw(df, "str"))  # STR wins
            str_metrics.update(orch.extract_str_metrics_by_kw(df))

    # ---- Walk selections: Y -> Semantics, Competitor/Brand -> Master KW ----
    y_kws, comp_searches, comp_names = [], [], []
    own_searches, own_names = [], []
    seen_y = set()
    for u in uploads:
        if not u.get("has_grid"):
            continue
        grid = cstore.load_parsed(pid, u["filekey"])
        if not grid:
            continue
        kc = grid.get("keyword_col")
        # Fallback: some BA/batst files store keyword_col=None when the column
        # header uses a niche name instead of "Search Term". Apply the same
        # detection the frontend kcFallback uses.
        if kc is None:
            cols = grid.get("columns", [])
            kw_toks = ("search term", "keyword phrase", "search query", "keyword")
            for tok in kw_toks:
                for i, c in enumerate(cols):
                    if tok in c.lower():
                        kc = i
                        break
                if kc is not None:
                    break
            if kc is None and cols:
                col0 = cols[0].lower()
                kc = 1 if ("frequency" in col0 or "rank" in col0 or "sfr" in col0) and len(cols) > 1 else 0
        if kc is None:
            continue
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
    # Root assignment follows the PPC root rule (campaign_ai.assign_roots_ruled):
    # one lowercase root per keyword — brand/device > feature/type > foreign-language
    # group > generic noun, capped at 15 categories. The user's custom roots (set in
    # the Semantics step) are reused first. Persisted so preview and build agree (AI
    # is non-deterministic); recomputed only when the keyword set or custom roots change.
    cached = state.get("roots") or {}
    custom_roots = cached.get("custom") or []
    # Signature over the EXACT keyword set (not just its count) so changing WHICH
    # keywords are selected — even when the count is unchanged — invalidates a stale
    # map. A stale map misses on lookup and would dump every unmatched keyword into a
    # single fallback root: the "phantom root in the badge but not in the column" bug.
    import hashlib
    sig = hashlib.md5("\n".join(sorted(k.lower() for k in kw_texts)).encode("utf-8")).hexdigest()
    if cached.get("map") and cached.get("sig") == sig and cached.get("custom", []) == custom_roots:
        root_map = cached["map"]
    else:
        res = ai.assign_roots_ruled(kw_texts, custom_roots) if kw_texts else {"map": {}}
        root_map = res["map"]
        cdb.save_state(pid, "roots", {"map": root_map, "custom": custom_roots,
                                      "sig": sig, "n": len(kw_texts)})
    # Lower-cased lookup so the keyword text matches regardless of original casing.
    rm_lower = {str(k).lower(): v for k, v in (root_map or {}).items()}

    # ---- Semantics rows ----------------------------------------------------
    sem_rows = []
    for c in y_kws:
        kw = c["keyword"]
        sv = sv_by_kw.get(kw.lower(), 0)
        kw_type, match = _digit_rule(sv)
        # Every keyword gets a real root: the map entry, else a per-keyword rule
        # fallback (never silently dumped into the top root).
        root = rm_lower.get(kw.lower()) or ai.root_for(kw, custom_roots) or "0-Gen"
        sem_rows.append({
            "keyword": kw, "source": c["source"],
            "category": root,
            "kw_type": kw_type, "match": match, "product": default_product,
            "product_asin": default_asin,
            "placement_mod": DEFAULT_PLACEMENT, "asp": default_asp, "acos_target": DEFAULT_ACOS,
            # Sheet-display columns — empty by default, filled only by user edits.
            "disp_kw_type": "", "disp_match": "", "disp_broad": "",
            "organic_rank": "", "impression_share": "", "ctr": "",
            # Search Volume (monthly) — shown read-only in the sheet view; the
            # workbook keeps the live SV formula in column C.
            "sv": int(round(sv)) if sv else 0,
            # Conversion Rate — POE "Search Conversion Rate" preferred; falls back
            # to STR CVR / H10 ABA Total Conv. Share when the kw isn't in POE.
            "cvr": poe_cvr_by_kw.get(kw.lower()) or cvr_by_kw.get(kw.lower(), ""),
            # Orders / CPC / ACoS — looked up per keyword in the Search Term
            # Report; shown read-only while the workbook keeps the live formula.
            "orders": (str_metrics.get(kw.lower()) or {}).get("orders", ""),
            "cpc": (str_metrics.get(kw.lower()) or {}).get("cpc", ""),
            "acos": (str_metrics.get(kw.lower()) or {}).get("acos", ""),
        })
    _apply_grid_edits(sem_rows, state.get("semantics_edits") or {}, SEM_NUMERIC,
                      skip_empty={"category"})
    # The editable sheet columns KW Vol. / Match / Broad KW List are stored as
    # disp_* aliases; map a non-empty manual entry onto the real build fields so
    # the workbook (Semantics L/M/N + campaign generation) reflects the user's edits.
    for s in sem_rows:
        if s.get("disp_kw_type"):
            s["kw_type"] = s["disp_kw_type"]
        if s.get("disp_match"):
            # Normalize the manual match entry (EX/ex/Br/ph...) to the canonical
            # taxonomy so campaign names read "... | Ex. | ..." not "... | EX | ...".
            s["disp_match"] = orch.canon_match(s["disp_match"])
            s["match"] = s["disp_match"]
        if s.get("disp_broad"):
            s["broad_list"] = s["disp_broad"]
    inp.semantics_rows = sem_rows

    # Root summary is derived from the FINAL Root KW column (after the per-keyword
    # fallback AND any manual edits) so the "Roots used" badges always equal what is
    # actually in the grid — never a phantom count from a pre-edit map.
    from collections import Counter
    _rc = Counter(s["category"] for s in sem_rows if s.get("category"))
    root_summary = [[r, n] for r, n in _rc.most_common()]
    roots = [r for r, _ in root_summary]
    inp.root_categories = roots or ["0-Gen"]

    # ---- PAT targets + MKL ASIN buckets ------------------------------------
    # Per-ASIN PAT-sheet inputs from the ASIN-selection grid (title/source/
    # product/asp/acos overrides). Blank fields fall back to the defaults.
    asin_names = state.get("asin_names") or {}
    asin_pat = state.get("asin_pat") or {}
    # own product ASIN -> name, so a Product-ASIN pick drives the Product Name.
    own_name_by_asin = {pr["asin"]: pr.get("name", "") for pr in products_all}

    def _num(v, dflt):
        try:
            return float(str(v).replace(",", "").replace("%", "").strip())
        except (TypeError, ValueError):
            return dflt

    # PAT conversion rate: competitor ASINs have no per-ASIN CVR in any upload, so use
    # the POE-derived product CVR — the average POE "Search Conversion Rate" across the
    # selected (Y) keywords. Keeps PAT CVR sourced from POE and makes the bid compute.
    # Strict parse so a stray non-numeric CVR (e.g. a leaked header label) can never
    # crash the average; such values are simply skipped.
    poe_cvrs = [n for n in (orch.clean_num(v) for v in poe_cvr_by_kw.values()) if n]
    pat_cvr = round(sum(poe_cvrs) / len(poe_cvrs), 4) if poe_cvrs else ""

    # Per-ASIN CVR (fraction) from the source files — POE "Search Conversion Rate"
    # / BA "Top Clicked Product #X: Conversion Share" — restricted to the selected
    # keyword rows. SAME source the PAT grid shows, so displayed == generated.
    sel_map = state.get("selections") or {}
    asin_cvr = {}
    for u in uploads:
        if u.get("source") not in ("poe", "ba", "batst"):
            continue
        g = cstore.load_parsed(pid, u["filekey"])
        if not g or not g.get("asin_cols"):
            continue
        fsel = sel_map.get(u["filekey"], {})
        yc = {int(ri) for ri, t in fsel.items() if t in ("Y", "Competitor")} or None
        for a, v in orch.extract_asin_cvr(g["columns"], g["rows"], g["asin_cols"], yc).items():
            asin_cvr.setdefault(a, v)

    pat_targets, pat_types = [], []
    for asin, tag in asin_tags.items():
        if tag not in PAT_BUCKET:
            continue
        getattr(inp, PAT_BUCKET[tag]).append(asin)
        if tag not in pat_types:
            pat_types.append(tag)
        m = asin_pat.get(asin) or {}
        # Product Name = the SHORT product (matches Campaign Naming B), so PAT's
        # campaign-name lookup key (Product-PT-Ex.-CatType) resolves against the PT
        # campaign's AN helper (B-E-F-G). A manual grid override still wins.
        prod = m.get("product") or default_product
        pat_targets.append({
            "asin": asin, "type": tag,
            "title": asin_names.get(asin, ""),
            "source": m.get("source") or "Competitor",
            "product": prod,
            "asp": _num(m.get("asp"), default_asp),
            "acos": _num(m.get("acos"), DEFAULT_ACOS),
            # Per-ASIN source CVR; flat POE average only as a last-resort fallback.
            "cvr": asin_cvr.get(asin, pat_cvr),
        })
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
    inp.campaign_rows = orch._campaign_rows(sem_rows, camp_ctx,
                                            default_asp, DEFAULT_ACOS, DEFAULT_PLACEMENT)
    inp.campaign_rows += orch._pat_campaign_rows(pat_types, camp_ctx,
                                                 default_asp, DEFAULT_ACOS, DEFAULT_PLACEMENT)
    # SB/SBV (per Root KW), SPM PT Exp. + SDI PT (per PAT cat), SPA, STPP, SDI
    # remarketing — the rest of the client's campaign taxonomy (SPM | CT excluded).
    inp.campaign_rows += orch._extra_campaign_rows(roots, pat_types, camp_ctx,
                                                   DEFAULT_PLACEMENT)

    meta = {
        "semantics": len(sem_rows),
        "campaigns": len(inp.campaign_rows),
        "pat_targets": len(pat_targets),
        "roots": roots,
        "root_summary": root_summary,
        "custom_roots": custom_roots,
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
