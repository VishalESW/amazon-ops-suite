"""Campaign Processor — orchestration.

Turns parsed uploads + manual inputs into a BuildInput for campaign_engine, then
builds the workbook. Keyword selection / categorisation runs through campaign_ai
(with deterministic fallbacks).

Upload contract: each uploaded file is a clean table — row 1 = headers matching
the corresponding sheet's data-table columns, rows 2+ = data. Passthrough data is
written positionally into the tab's data columns; the engine owns formula columns.
"""

from __future__ import annotations

import io
import os
import re

import pandas as pd

from utils import campaign_ai
from utils.campaign_engine import build, BuildInput

# Generic fallback only. Real categories are client-specific — supplied by the user
# in the wizard, or AI-derived from the keyword pool. Never hard-code a niche here.
FALLBACK_CATEGORIES = ["0-Gen"]


# --------------------------------------------------------------------------- #
# Upload parsing
# --------------------------------------------------------------------------- #
def read_table(file_storage):
    """Read an uploaded CSV/XLSX into a DataFrame (str-normalised headers)."""
    name = (getattr(file_storage, "filename", "") or "").lower()
    raw = file_storage.read()
    if not raw:
        return pd.DataFrame()
    bio = io.BytesIO(raw)
    if name.endswith(".csv"):
        df = pd.read_csv(bio, dtype=object, keep_default_na=False)
    else:
        df = pd.read_excel(bio, dtype=object, header=0)
    df.columns = [str(c).strip() for c in df.columns]
    return df.fillna("")


# Header tokens used to locate the real data-table row inside a raw export that
# has a control-panel preamble (Country / Reporting Range / ASIN list / etc.).
SOURCE_TOKENS = {
    "poe": ("search term", "customer need", "search volume"),
    "h10": ("keyword phrase", "search volume", "cerebro"),
    "ba": ("search term", "top clicked", "search frequency"),
    "brand": ("keyword phrase", "cerebro", "search volume"),
    "sqp": ("search query", "impressions", "clicks"),
    "batst": ("search term", "top clicked", "search frequency"),
    "str": ("search term", "spend", "acos"),
}


def _dedupe_headers(headers):
    seen, out = {}, []
    for h in headers:
        h = str(h).strip()
        if h in seen:
            seen[h] += 1
            out.append(f"{h}.{seen[h]}")
        else:
            seen[h] = 0
            out.append(h)
    return out


def _detect_header_row(full, tokens, max_scan=35):
    """Index of the first row containing >=2 of the expected header tokens."""
    n = min(max_scan, len(full))
    for i in range(n):
        cells = [str(x).strip().lower() for x in full.iloc[i].tolist()]
        score = sum(1 for t in tokens if any(t in c for c in cells))
        if score >= 2:
            return i
    return None


def read_table_smart(file_storage, source):
    """Read a raw export, auto-detecting the real data-table header row.

    Strips any control-panel preamble so both candidate extraction (by column
    name) and passthrough (positional) operate on the actual table.
    """
    name = (getattr(file_storage, "filename", "") or "").lower()
    raw = file_storage.read()
    if not raw:
        return pd.DataFrame()
    if name.endswith(".csv"):
        # Raw exports have ragged rows (preamble cells vs. wide table) — the C
        # parser rejects those, so read with the stdlib csv reader and pad.
        import csv
        text = raw.decode("utf-8-sig", errors="replace")
        rows = list(csv.reader(io.StringIO(text)))
        width = max((len(r) for r in rows), default=0)
        rows = [r + [""] * (width - len(r)) for r in rows]
        full = pd.DataFrame(rows, dtype=object)
    else:
        full = pd.read_excel(io.BytesIO(raw), dtype=object, header=None)
    full = full.fillna("")
    if full.empty:
        return pd.DataFrame()
    tokens = SOURCE_TOKENS.get(source, ())
    hdr = _detect_header_row(full, tokens) if tokens else 0
    if hdr is None:
        hdr = 0  # fall back to first row
    header = _dedupe_headers(full.iloc[hdr].tolist())
    data = full.iloc[hdr + 1:].reset_index(drop=True)
    df = pd.DataFrame(data.values, columns=header).fillna("")
    return df


def _rows(df):
    return [] if df is None or df.empty else df.values.tolist()


# Canonical data-table headers (exact order) for the sheets whose columns feed
# live formulas. Uploaded columns are matched to these BY NAME so they land in
# the exact cells the formulas read — regardless of the export's column order.
POE_CANON = [  # -> sheet cols B..O (P is the =ROUND(E/12) formula)
    "Customer Need", "Search Term", "Selected? (Y/N/Competitor/Brand)",
    "Search Volume (Past 360 days)", "Search Volume Growth (90 days)",
    "Search Volume Growth (180 days)", "Click Share (360 days)",
    "Search Conversion Rate (360 days)", "Top Clicked Product 1 (Title)",
    "Top Clicked Product 1 (Asin)", "Top Clicked Product 2 (Title)",
    "Top Clicked Product 2 (Asin)", "Top Clicked Product 3 (Title)",
    "Top Clicked Product 3 (Asin)",
]
H10_CANON = [  # -> sheet cols A..AF (A=Keyword Phrase, G=Search Volume)
    "Keyword Phrase", "ABA Total Click Share", "ABA Total Conv. Share",
    "Selected? (Y/N/Brand/Copetitor)", "Keyword Sales", "Cerebro IQ Score",
    "Search Volume", "Search Volume Trend", "H10 PPC Sugg. Bid",
    "H10 PPC Sugg. Min Bid", "H10 PPC Sugg. Max Bid", "Sponsored ASINs",
    "Competing Products", "CPR", "Title Density", "Amazon Recommended",
    "Sponsored", "Organic", "Sponsored Rank (avg)", "Sponsored Rank (count)",
    "Amazon Recommended Rank (avg)", "Amazon Recommended Rank (count)",
    "Position (Rank)", "Relative Rank", "Competitor Rank (avg)",
    "Ranking Competitors (count)", "Competitor Performance Score",
]
STR_CANON = [  # -> Search Term Report cols A..Z (Q=ACOS, R=CVR, S=CPC, U=Orders)
    "Search Term", "Selected? (Y/N/Brand/Competitor)", "Actively Targeted",
    "Harvested", "Negated", "Target", "Target Type", "Campaign Ad Type", "Bid",
    "Match Type", "Campaign", "Last 30d Sales", "Last 30d Spend", "Spend", "Sales",
    "Clicks", "ACOS", "CVR", "CPC", "Impressions", "Orders", "Units",
    "Same SKU Orders", "Same SKU Sales", "Other SKU Sales", "CTR",
]
SQP_CANON = [  # -> sheet cols A..N (A=Search Query, D=Search Query Volume)
    "Search Query", "Select (Y/N/Brand/Competitor)", "Search Query Score",
    "Search Query Volume", "Impressions: Total Count", "Impressions: ASIN Count",
    "Impressions: ASIN Share %", "Clicks: Total Count", "Clicks: Click Rate %",
    "Clicks: ASIN Count", "Clicks: ASIN Share %", "Clicks: Price (Median)",
    "Clicks: ASIN Price (Median)", "Clicks: Same Day Shipping Speed",
]


def _norm_h(s):
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def _canonicalize(df, canonical):
    """Re-order an uploaded table to the canonical column order, matching by
    normalised header name (exact first, then substring). Missing columns become
    blank; extra columns are dropped. Returns a list of aligned row-lists."""
    if df is None or df.empty:
        return []
    canon_norm = [_norm_h(c) for c in canonical]
    df_cols = list(df.columns)
    norm_to_col = {}
    for c in df_cols:
        norm_to_col.setdefault(_norm_h(c), c)
    chosen, used = [None] * len(canonical), set()
    # pass 1: exact normalised match
    for i, cn in enumerate(canon_norm):
        col = norm_to_col.get(cn)
        if col is not None and col not in used:
            chosen[i] = col
            used.add(col)
    # pass 2: substring match (one contains the other), longest wins
    for i, cn in enumerate(canon_norm):
        if chosen[i] is not None:
            continue
        best, best_len = None, 0
        for c in df_cols:
            if c in used:
                continue
            nc = _norm_h(c)
            if nc and (cn in nc or nc in cn):
                m = min(len(cn), len(nc))
                if m > best_len:
                    best, best_len = c, m
        if best is not None and best_len >= 4:
            chosen[i] = best
            used.add(best)
    out = []
    for _, r in df.iterrows():
        out.append([("" if chosen[i] is None else r[chosen[i]]) for i in range(len(canonical))])
    return out


def _find_col(df, *needles):
    """Return the first column whose header contains any needle (case-insensitive)."""
    for c in df.columns:
        cl = str(c).lower()
        if any(n in cl for n in needles):
            return c
    return None


def _num(v):
    try:
        return float(re.sub(r"[^0-9.\-]", "", str(v)) or 0)
    except ValueError:
        return 0.0


# --------------------------------------------------------------------------- #
# Candidate pooling for Semantics
# --------------------------------------------------------------------------- #
def _candidates(df, source, kw_tokens, vol_tokens, annualized=False):
    """Generic candidate extractor. Locates the keyword + search-volume columns by
    header tokens and emits {keyword, source, search_volume (monthly)}."""
    out = []
    if df is None or df.empty:
        return out
    kw = _find_col(df, *kw_tokens)
    sv = _find_col(df, *vol_tokens)
    if kw is None:
        return out
    for _, row in df.iterrows():
        k = str(row.get(kw, "")).strip()
        if not k or k.lower() in ("nan", "none"):
            continue
        vol = _num(row.get(sv, 0)) if sv else 0
        if annualized and vol:
            vol = round(vol / 12)
        out.append({"keyword": k, "source": source, "search_volume": vol})
    return out


# Per-source column-token config: (keyword tokens, volume tokens, annualized?)
CANDIDATE_CFG = {
    "POE": (("search term", "keyword"), ("search volume",), True),
    "H10": (("keyword phrase", "keyword"), ("search volume",), False),
    "BA":  (("search term", "search query", "keyword phrase", "keyword"),
            ("search volume", "volume"), False),
    "SQP": (("search query", "keyword"),
            ("search query volume", "query volume", "volume"), False),
    "Brand": (("keyword phrase", "keyword"), ("search volume",), False),
}


def _extract(df, source):
    kw_tok, vol_tok, ann = CANDIDATE_CFG[source]
    return _candidates(df, source, kw_tok, vol_tok, ann)


# Keyword-column tokens per source (for the selection grids).
_KW_TOKENS = ("search term", "keyword phrase", "search query", "keyword")


def parse_upload(file_storage, source):
    """Smart-read a raw export into a grid table for the selection UI.

    Returns {columns, rows, keyword_col, asin_cols, brand_cols} where *_col(s)
    are 0-based indices into columns. Preamble is auto-stripped (read_table_smart).
    """
    df = read_table_smart(file_storage, source)
    if df is None or df.empty:
        return {"columns": [], "rows": [], "keyword_col": None,
                "asin_cols": [], "brand_cols": []}
    cols = [str(c) for c in df.columns]
    low = [c.lower() for c in cols]
    kw_idx = None
    for tok in _KW_TOKENS:
        for i, c in enumerate(low):
            if tok in c:
                kw_idx = i
                break
        if kw_idx is not None:
            break
    asin_cols = [i for i, c in enumerate(low) if "asin" in c]
    brand_cols = [i for i, c in enumerate(low) if "brand" in c]
    rows = df.astype(object).where(df.notna(), "").values.tolist()
    rows = [["" if v is None else str(v) for v in r] for r in rows]
    return {"columns": cols, "rows": rows, "keyword_col": kw_idx,
            "asin_cols": asin_cols, "brand_cols": brand_cols}


def _splitlines(text):
    return [x.strip() for x in (text or "").replace("\r", "\n").split("\n") if x.strip()]


# --------------------------------------------------------------------------- #
# Main entry
# --------------------------------------------------------------------------- #
def generate(form, files, out_path, categories=None):
    """form: dict-like of text fields. files: werkzeug MultiDict of uploads.
    Returns (out_path, meta)."""
    # Categories are client-specific: prefer the explicit arg, then the user's
    # wizard list, else AI-derive from the keyword pool after selection (below).
    user_categories = categories or _splitlines(form.get("categories"))
    brand = (form.get("brand") or "Brand").strip()

    products = _parse_products(form)
    product_names = [p["name"] for p in products if p.get("name")]
    product_ctx = f"{brand}: " + "; ".join(product_names) if product_names else brand

    inp = BuildInput(
        brand=brand,
        products=products,
        h10_asins=_splitlines(form.get("h10_asins")),
        brand_asins=_splitlines(form.get("brand_asins")),
        own_branded_kws=_splitlines(form.get("own_branded_kws")),
        own_branded_searches=_splitlines(form.get("own_branded_searches")),
        competitor_kws=_splitlines(form.get("competitor_kws")),
        competitor_searches=_splitlines(form.get("competitor_searches")),
        own_brand_asins=_splitlines(form.get("own_brand_asins")),
        main_competitor_asins=_splitlines(form.get("main_competitor_asins")),
        lower_rated_asins=_splitlines(form.get("lower_rated_asins")),
        higher_priced_asins=_splitlines(form.get("higher_priced_asins")),
        bestselling_asins=_splitlines(form.get("bestselling_asins")),
        harvested_asins=_splitlines(form.get("harvested_asins")),
    )

    candidates = []

    # POE files (one per product, named by product)
    for fs in files.getlist("poe_files"):
        if not getattr(fs, "filename", ""):
            continue
        df = read_table_smart(fs, "poe")
        product = _match_product(fs.filename, product_names)
        inp.poe_tables[product or fs.filename] = _canonicalize(df, POE_CANON)
        candidates += _extract(df, "POE")

    # H10 reverse ASIN
    fs = files.get("h10_file")
    if fs and fs.filename:
        df = read_table_smart(fs, "h10")
        inp.h10_table = _canonicalize(df, H10_CANON)
        candidates += _extract(df, "H10")

    # Brand Analytics (with reverse ASIN)
    fs = files.get("brand_analytics_file")
    if fs and fs.filename:
        df = read_table_smart(fs, "ba")
        inp.brand_analytics_table = _rows(df)
        candidates += _extract(df, "BA")

    # Brand (own H10 reverse ASIN)
    fs = files.get("brand_file")
    if fs and fs.filename:
        df = read_table_smart(fs, "brand")
        inp.brand_table = _rows(df)
        candidates += _extract(df, "Brand")

    # Search Term Report (optional)
    fs = files.get("str_file")
    if fs and fs.filename:
        inp.str_table = _rows(read_table_smart(fs, "str"))

    # SQP reports (per ASIN, named by ASIN)
    for fs in files.getlist("sqp_files"):
        if not getattr(fs, "filename", ""):
            continue
        df = read_table_smart(fs, "sqp")
        asin = _match_asin(fs.filename) or os.path.splitext(fs.filename)[0]
        inp.sqp_reports[asin] = _canonicalize(df, SQP_CANON)
        candidates += _extract(df, "SQP")

    # BA TST (per word, named by word)
    for fs in files.getlist("ba_tst_files"):
        if not getattr(fs, "filename", ""):
            continue
        word = _ba_word(fs.filename)
        inp.ba_tst[word] = _rows(read_table_smart(fs, "batst"))

    # ---- Semantics: select + categorise + classify --------------------------
    selected = campaign_ai.select_keywords(candidates, product_ctx)
    kw_list = [c["keyword"] for c in selected]

    # Resolve categories: user's list wins; otherwise AI-derives them from this
    # client's own keywords; otherwise a generic single bucket.
    if user_categories:
        categories = user_categories
    elif kw_list:
        categories = campaign_ai.derive_categories(kw_list, product_ctx) or FALLBACK_CATEGORIES
    else:
        categories = FALLBACK_CATEGORIES
    inp.root_categories = categories

    cat_map = campaign_ai.categorize(kw_list, categories)
    type_map = campaign_ai.classify_targets(kw_list)

    default_product = product_names[0] if product_names else brand
    asp = _num(form.get("asp")) or 24.95
    acos_target = (_num(form.get("acos_target")) or 25) / 100
    placement = (_num(form.get("placement_mod")) or 25) / 100

    sem_rows = []
    for c in selected:
        kw = c["keyword"]
        t = type_map.get(kw, {"kw_type": "SKW", "match": "Ex."})
        sem_rows.append({
            "keyword": kw, "source": c.get("source", ""),
            "category": cat_map.get(kw, categories[0] if categories else ""),
            "kw_type": t["kw_type"], "match": t["match"],
            "product": default_product,
            "placement_mod": placement, "asp": asp, "acos_target": acos_target,
        })
    inp.semantics_rows = sem_rows

    # ---- PAT: competitor ASINs we target -----------------------------------
    bucket_map = [
        ("Main", inp.main_competitor_asins),
        ("Low Rated", inp.lower_rated_asins),
        ("High Priced", inp.higher_priced_asins),
        ("Bestselling", inp.bestselling_asins),
    ]
    pat_targets, pat_types = [], []
    for cat_type, asins in bucket_map:
        if asins:
            pat_types.append(cat_type)
        for asin in asins:
            pat_targets.append({
                "asin": asin, "type": cat_type, "source": "Competitor",
                "product": default_product, "asp": asp, "acos": acos_target,
            })
    inp.pat_targets = pat_targets

    # ---- Campaign Naming: SKW/MKW keyword campaigns + PAT (PT) campaigns ----
    inp.campaign_rows = _campaign_rows(sem_rows, brand, default_product,
                                       asp, acos_target, placement)
    inp.campaign_rows += _pat_campaign_rows(pat_types, brand, default_product,
                                            asp, acos_target, placement)

    build(inp, out_path)

    warnings = []
    if not inp.str_table:
        warnings.append("No Search Term Report uploaded — Semantics Orders / CPC / ACoS "
                        "columns are left blank and marked in the file.")
    if not candidates:
        warnings.append("No keywords found in the uploads — check that your POE / H10 "
                        "Reverse ASIN / Brand Analytics / SQP files contain a keyword column.")
    elif not selected:
        warnings.append("Keywords were found but none were selected for Semantics.")

    meta = {
        "candidates": len(candidates),
        "selected": len(selected),
        "campaigns": len(inp.campaign_rows),
        "pat_targets": len(pat_targets),
        "ai_used": campaign_ai.available(),
        "str_included": bool(inp.str_table),
        "sqp_count": len(inp.sqp_reports),
        "warnings": warnings,
    }
    return out_path, meta


def _campaign_rows(sem_rows, brand, product, asp, acos, placement):
    """One campaign row per selected keyword (mirrors the example defaults)."""
    rows = []
    for s in sem_rows:
        ktype = s["kw_type"]
        camp_type = "SPM"  # Sponsored Products Manual
        rows.append({
            "A": "Create", "B": s.get("product") or product, "C": camp_type,
            "E": ktype, "F": s["match"], "G": s["keyword"], "H": "Rank",
            "J": brand, "K": 5, "L": "Any Date Range", "M": "Manual Targeting",
            "P": "Fixed Bids", "Q": placement, "S": placement,
            "U": s.get("product") or product, "V": "Keyword Targeting",
            "Z": 0.2, "AA": "", "AB": "Refer Semantics",
            "AC": "None", "AD": "None", "AE": "None",
            "AH": placement, "AI": asp, "AJ": acos, "AK": 0.14,
        })
    return rows


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _pat_campaign_rows(pat_types, brand, product, asp, acos, placement):
    """One PT campaign per competitor-ASIN category so PAT's campaign-name XLOOKUP
    (K&"-PT-Ex.-"&J -> Campaign Naming AN -> I) resolves. AN = B-E-F-G = product-PT-Ex.-type."""
    rows = []
    for cat_type in pat_types:
        rows.append({
            "A": "Create", "B": product, "C": "SPM",
            "E": "PT", "F": "Ex.", "G": cat_type, "H": "Rank",
            "J": brand, "K": 5, "L": "Any Date Range", "M": "Manual Targeting",
            "P": "Fixed Bids", "Q": placement, "S": placement,
            "U": product, "V": "Product Targeting",
            "Z": 0.2, "AB": "Refer Semantics",
            "AC": "None", "AD": "None", "AE": "None",
            "AH": placement, "AI": asp, "AJ": acos, "AK": 0.14,
        })
    return rows


def _parse_products(form):
    """products come as products[0][asin], products[0][sku], products[0][name] ..."""
    out, i = [], 0
    while True:
        asin = form.get(f"products[{i}][asin]")
        sku = form.get(f"products[{i}][sku]")
        name = form.get(f"products[{i}][name]")
        if asin is None and sku is None and name is None:
            break
        if (asin or sku or name):
            out.append({"asin": (asin or "").strip(), "sku": (sku or "").strip(),
                        "name": (name or "").strip()})
        i += 1
    return out


def _match_product(filename, product_names):
    base = os.path.splitext(os.path.basename(filename))[0].lower()
    for name in product_names:
        if name and name.lower() in base:
            return name
    return None


_ASIN_RE = re.compile(r"\bB0[A-Z0-9]{8}\b", re.I)


def _match_asin(filename):
    m = _ASIN_RE.search(filename or "")
    return m.group(0).upper() if m else None


def _ba_word(filename):
    base = os.path.splitext(os.path.basename(filename))[0]
    # strip common prefixes like "BA TST - " or "POE - "
    base = re.sub(r"^(ba\s*tst|poe)\s*[-_ ]*", "", base, flags=re.I)
    return base.strip()
