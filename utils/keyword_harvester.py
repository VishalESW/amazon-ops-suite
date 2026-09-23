"""Keyword Harvesting engine (Module 4 of the PPC suite).

The ASIN-level funnel from keyword_harvesting_process.md: qualify converting
customer search terms, dedup against what's already targeted, ROUTE each to a
destination (keyword terms grouped by root -> one SPM·MKW·Ex. themed campaign per
root; ASIN -> SPM·PT·Ex. add-to-existing), compute a starting bid,
prioritize with SQP demand, and emit three review artifacts:

  - create_campaigns : rows in the Campaign Naming bulk schema (feed Campaign Processor)
  - add_targets      : ADD_TARGET ops for existing PT / MKW campaigns
  - add_negatives    : ADD_NEGATIVE ops for the source discovery campaigns

Pure functions only — the blueprint pulls the reports (AdLabs STR/targeting +
SP-API SQP) and passes rows in. Dry-run: this module never calls a write API.
"""

import csv
import io
import re

_ASIN_RE = re.compile(r"^b0[0-9a-z]{8}$", re.I)

# Exact Campaign Naming header order (assets/campaign_template.xlsx, row 1) so the
# create_campaigns artifact drops straight into the Campaign Processor.
CREATE_HEADERS = [
    "Action", "Product Name", "Campaign Type", "Landing Page", "KW or PT",
    "Match Type", "Root KW", "Campaign Goal", "Campaign Name", "Profile",
    "Daily Budget", "Date Range", "Targeting Type", "Brand", "Goals",
    "Bidding Strategy", "Placement Bid Adjustment (Top of Search)",
    "Placement Bid Adjustment (Product Pages)",
    "Placement Bid Adjustment (Rest of Search)", "Campaign Tag", "Portfolio",
    "Targeting Type ", "Ad Group Name", "Landing Page ", "Cost Control",
    "Default Bid", "ASIN/SKU", "Targets", "Negative Phrase", "Negative Exact",
    "Negative PAT", "Headline", "Video Filename", "Placement Modifier", "ASP",
    "ACoS Target", "Conversion Rate", "Starting Bid", "Repurpose Campaign", "Helper",
]
ADD_TARGET_HEADERS = ["op", "product", "campaign", "ad_group", "target",
                      "expression", "match", "bid", "source_term"]
ADD_NEGATIVE_HEADERS = ["op", "product", "source_campaign", "value", "match", "object"]

# Theme dictionary (spec §9) — longest seed match assigns a keyword's root.
THEME_DICT = [
    ("1-Gift", ["gift", "gifts", "present"]),
    ("2-Accessories", ["accessory", "accessories", "kit", "set"]),
    ("3-Training Aids", ["training aid", "trainer aid", "practice aid"]),
    ("4-Putter", ["putter"]),
    ("5-Swing", ["swing"]),
    ("6-Mirror", ["mirror"]),
    ("7-Putting", ["putting", "putt", "green"]),
    ("8-Practice", ["practice", "drill"]),
    ("9-Trainer", ["trainer", "training"]),
    ("11-Alignment", ["alignment", "align", "aim"]),
]

_STOP = {"for", "the", "and", "with", "your", "a", "to", "of", "in", "on", "best",
         "new", "men", "women", "kids"}


class HarvestConfig:
    """Config block (spec §1). Built from the request form with these defaults."""

    def __init__(self, **kw):
        self.min_clicks = int(kw.get("min_clicks", 5))
        self.min_orders = int(kw.get("min_orders", 2))
        self.max_acos = float(kw.get("max_acos", 0.30))
        self.lookback_days = int(kw.get("lookback_days", 60))
        self.target_acos_default = float(kw.get("target_acos_default", 0.20))
        self.rank_aggression = float(kw.get("rank_aggression", 1.20))
        self.bid_min = float(kw.get("bid_min", 0.10))
        self.bid_max_multiplier = float(kw.get("bid_max_multiplier", 1.50))
        self.high_price_pt_threshold = float(kw.get("high_price_pt_threshold", 1.25))
        self.max_new_campaigns_per_run = int(kw.get("max_new_campaigns_per_run", 50))
        self.daily_budget = float(kw.get("daily_budget", 5))
        self.tos_bid = int(kw.get("tos_bid", 25))
        self.ros_bid = int(kw.get("ros_bid", 25))
        self.own_brand_tokens = [t.strip().lower() for t in (kw.get("own_brand_tokens") or []) if t.strip()]
        self.competitor_brand_tokens = [t.strip().lower() for t in (kw.get("competitor_brand_tokens") or []) if t.strip()]
        # priority weights (spec §8)
        self.w1, self.w2, self.w3, self.w4 = (float(kw.get("w1", 0.4)), float(kw.get("w2", 0.25)),
                                              float(kw.get("w3", 0.2)), float(kw.get("w4", 0.15)))


# --------------------------------------------------------------- helpers ---

def _f(v, d=0.0):
    try:
        return float(str(v).replace(",", "").replace("$", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return d


def _norm(term):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", str(term or "").lower())).strip()


def is_asin(term):
    return bool(_ASIN_RE.match(str(term or "").strip()))


def brand_tag(term, cfg):
    t = " " + _norm(term) + " "
    if any(f" {tok} " in t or t.strip() == tok for tok in cfg.own_brand_tokens):
        return "own_brand"
    if any(f" {tok} " in t or t.strip() == tok for tok in cfg.competitor_brand_tokens):
        return "competitor"
    return "generic"


# Discovery campaign detection: keep Auto + Broad/Broad-Mod/Phrase + CT/STPP; drop
# rows already served by an Exact target (those are graduates, not discovery).
def is_discovery(row):
    match = str(row.get("match_types") or row.get("match_type") or "").lower()
    return "exact" not in match


def assign_root(term):
    """Root/theme for a keyword: longest theme-dict seed match, else the most
    descriptive non-stopword token."""
    t = " " + _norm(term) + " "
    best_theme, best_len = "", 0
    for theme, seeds in THEME_DICT:
        for s in seeds:
            if f" {s} " in t and len(s) > best_len:
                best_theme, best_len = theme, len(s)
    if best_theme:
        return best_theme
    toks = [w for w in _norm(term).split() if w not in _STOP and len(w) > 2]
    return max(toks, key=len) if toks else (_norm(term) or "misc")


def build_dedup(target_rows):
    """From the Targeting report: sets of already-targeted exact keywords and
    existing product-target ASINs (global across the profile)."""
    kw, asin = set(), set()
    for r in target_rows:
        tgt = str(r.get("targeting") or r.get("keyword_text") or "").strip()
        match = str(r.get("match_types") or r.get("match_type") or "").lower()
        if not tgt:
            continue
        if is_asin(tgt) or "asin=" in tgt.lower() or "product" in match:
            m = _ASIN_RE.search(re.sub(r'.*asin="?', "", tgt, flags=re.I))
            asin.add((m.group(0) if m else tgt).lower())
        elif "exact" in match:
            kw.add(_norm(tgt))
    return {"keyword": kw, "asin": asin}


def build_sqp_index(sqp_rows):
    """Aggregate weekly SQP rows into {normalized_query: {...}} with the current
    4-week vs prior 4-week purchase-share trend (spec §2 step 2 / §8). Also flags
    non-targeted converting queries as fresh opportunities."""
    by_q = {}
    for r in sqp_rows:
        q = _norm(r.get("search_query"))
        if not q:
            continue
        by_q.setdefault(q, []).append(r)
    index, opportunities = {}, []
    for q, rows in by_q.items():
        rows.sort(key=lambda x: x.get("week_start", ""))
        # Adaptive trend: split the available weeks in half (recent vs prior) so the
        # signal works whether we pulled 8, 4 or 2 weeks.
        h = max(1, len(rows) // 2)
        cur, prior = rows[-h:], rows[-2 * h:-h]

        def _avg(rs, k):
            vals = [_f(x.get(k)) for x in rs]
            return (sum(vals) / len(vals)) if vals else 0.0
        ps_cur, ps_prior = _avg(cur, "asin_purchase_share"), _avg(prior, "asin_purchase_share")
        purchases = sum(_f(x.get("asin_purchase_count")) for x in cur)
        volume = _f(cur[-1].get("search_query_volume")) if cur else 0.0
        index[q] = {
            "purchase_share": round(ps_cur, 4), "purchase_share_prior": round(ps_prior, 4),
            "trend": round(ps_cur - ps_prior, 4), "purchases": int(purchases),
            "volume": int(volume), "impression_share": round(_avg(cur, "asin_impression_share"), 4),
            "raw": cur[-1] if cur else {},
        }
        # fresh opportunity: real volume + the ASIN converts on it
        if volume > 0 and purchases >= 1:
            opportunities.append({"search_query": (cur[-1].get("search_query") if cur else q),
                                  **index[q]})
    opportunities.sort(key=lambda x: (x["purchases"], x["volume"]), reverse=True)
    return index, opportunities


def harvest_bid(asp, target_acos, cvr, goal, suggested, cfg):
    """spec §7: ASP * target_acos * cvr, ×rank_aggression for Rank, clamped."""
    base = (asp or 0) * (target_acos or cfg.target_acos_default) * (cvr or 0)
    if goal == "Rank":
        base *= cfg.rank_aggression
    high = (suggested * cfg.bid_max_multiplier) if suggested else (base or cfg.bid_min)
    return round(max(cfg.bid_min, min(base if base else cfg.bid_min, high)), 2)


# --------------------------------------------------------------- engine ---

def run_harvest(search_rows, target_rows, cfg, ap_map=None, blacklist=None,
                sqp_index=None, product=None, profile="", asp=None, target_acos=None,
                asin_econ=None, asins=None):
    """Core funnel. Returns {plan, create, targets, negatives, stats}.

    search_rows : AdLabs search_term rows. target_rows : AdLabs target rows.
    ap_map      : {ad_group_id: {asin,title}}. blacklist : set of n-gram terms.
    sqp_index   : {normalized_query: {purchase_share, purchase_share_prior, ...}}.
    product     : default product label.
    asin_econ   : {asin_lower: {price, target_acos, title}} — per-ASIN economics
                  pulled from AdLabs (price = advertised_product AOV). The bid uses
                  the price of the ASIN the search term's ad group advertises.
    asins       : optional iterable of ASINs to scope the run to (ASIN-level harvest);
                  terms on any other ASIN are skipped. Empty/None = whole profile.
    asp/target_acos : profile-wide fallbacks when a term's ASIN has no economics.
    """
    ap_map = ap_map or {}
    blacklist = blacklist or set()
    sqp_index = sqp_index or {}
    asin_econ = asin_econ or {}
    selected = {str(a).strip().lower() for a in (asins or []) if str(a).strip()}
    dedup = build_dedup(target_rows)
    tacos = target_acos or cfg.target_acos_default

    plan = []
    for r in search_rows:
        raw = (r.get("search_term") or "").strip()
        if not raw:
            continue
        term = _norm(raw)
        clicks = _f(r.get("clicks")); spend = _f(r.get("spend"))
        sales = _f(r.get("sales")); orders = _f(r.get("orders"))
        acos = (spend / sales) if sales > 0 else None
        cvr = (orders / clicks) if clicks > 0 else 0.0
        cpc = (spend / clicks) if clicks > 0 else 0.0
        obj = "asin" if is_asin(raw) else "keyword"
        tag = brand_tag(raw, cfg)
        prod = (ap_map.get(r.get("ad_group_id")) or {}).get("title") or product or profile
        prod_asin = (ap_map.get(r.get("ad_group_id")) or {}).get("asin") or ""
        asin_key = prod_asin.strip().lower()
        econ = asin_econ.get(asin_key, {})
        term_asp = econ.get("price") or asp or 0.0
        term_tacos = econ.get("target_acos") or tacos

        row = {
            "product": prod, "product_asin": prod_asin, "search_term": raw, "object": obj,
            "brand_tag": tag, "source_campaign": r.get("campaign_name"),
            "source_match": r.get("match_types"), "clicks": int(clicks),
            "orders": int(orders), "spend": round(spend, 2), "sales": round(sales, 2),
            "acos": round(acos, 4) if acos is not None else None, "cvr": round(cvr, 4),
            "asp": round(term_asp, 2), "target_acos": round(term_tacos, 4),
        }

        # ASIN scope (ASIN-level harvest): only work the selected product(s). Terms
        # whose ad group can't be attributed to a selected ASIN are out of scope.
        if selected and asin_key not in selected:
            plan.append({**row, "decision": "SKIP", "reason": "ASIN not selected"}); continue

        # Gates (§4) + scope + blacklist.
        if not is_discovery(r):
            plan.append({**row, "decision": "SKIP", "reason": "not a discovery source"}); continue
        if term in blacklist:
            plan.append({**row, "decision": "SKIP", "reason": "n-gram blacklisted"}); continue
        if clicks < cfg.min_clicks:
            plan.append({**row, "decision": "SKIP", "reason": f"clicks<{cfg.min_clicks}"}); continue
        if orders < cfg.min_orders:
            plan.append({**row, "decision": "SKIP", "reason": f"orders<{cfg.min_orders}"}); continue
        if acos is None or acos > cfg.max_acos:
            plan.append({**row, "decision": "SKIP", "reason": f"acos>{cfg.max_acos:.0%}"}); continue

        # Dedup (§5).
        key = _norm(re.sub(r'.*asin="?', "", raw, flags=re.I)).replace(" ", "") if obj == "asin" else term
        already = key in dedup[obj]
        row["priority"] = _priority(row, sqp_index.get(term, {}), cfg)
        if already:
            plan.append({**row, "decision": "SKIP-EXISTS",
                         "reason": "already targeted", "negative_only": True}); continue

        # Route (§6).
        if obj == "keyword" and tag == "own_brand":
            plan.append({**row, "decision": "FLAG", "reason": "own-brand -> manual defense"})
            continue
        bid = harvest_bid(term_asp, term_tacos, cvr, "Rank" if obj == "keyword" else "Perf",
                          _f(r.get("suggested_bid")) or None, cfg)
        root = assign_root(raw)
        row.update({"decision": "PROMOTE", "suggested_bid": bid, "root": root})
        # Every graduated keyword is grouped into its root's MKW · Ex. campaign;
        # ASINs go to the product-targeting campaign. No single-keyword (SKW) route.
        row["dest"] = f"SPM·MKW·Exact ({root})" if obj == "keyword" else "SPM·PT·Exact"
        plan.append(row)

    # Prioritize + cap (§8). A "campaign" is one root's MKW group, so the per-run
    # cap limits the number of ROOT campaigns (keeping each root's best-priority
    # member). ASIN promotes are add-to-existing PT targets and aren't capped.
    kw_promos = [p for p in plan if p["decision"] == "PROMOTE" and p["object"] == "keyword"]
    root_pri = {}
    for p in kw_promos:
        rk = (p["product"], p["root"])
        root_pri[rk] = max(root_pri.get(rk, float("-inf")), p.get("priority", 0))
    kept_roots = {rk for rk, _ in sorted(root_pri.items(), key=lambda x: x[1], reverse=True)
                  [: cfg.max_new_campaigns_per_run]}
    for p in kw_promos:
        if (p["product"], p["root"]) not in kept_roots:
            p["decision"] = "SKIP"; p["reason"] = "over per-run cap"
    kept_ids = {id(p) for p in plan if p["decision"] == "PROMOTE"}

    create, targets, negatives = _artifacts(plan, kept_ids, cfg)
    stats = {
        "total": len(plan),
        "promote": sum(1 for p in plan if p["decision"] == "PROMOTE"),
        "skip_exists": sum(1 for p in plan if p["decision"] == "SKIP-EXISTS"),
        "flag": sum(1 for p in plan if p["decision"] == "FLAG"),
        "skip": sum(1 for p in plan if p["decision"] == "SKIP"),
        "roots": len(kept_roots),   # = number of MKW · Ex. campaigns created
    }
    return {"plan": plan, "create": create, "targets": targets,
            "negatives": negatives, "stats": stats}


def _priority(row, sqp, cfg):
    ps = _f(sqp.get("purchase_share"))
    ps_prior = _f(sqp.get("purchase_share_prior"))
    acos = row.get("acos") or 0
    return round(cfg.w1 * row["orders"] + cfg.w2 * ps + cfg.w3 * (ps - ps_prior)
                 - cfg.w4 * acos, 4)


def _neg_rows(row):
    """Negative write-back for a graduate (spec §6): block the term/ASIN in its
    source discovery campaign so it stops re-serving what you now own."""
    if row["object"] == "keyword":
        return [{"op": "ADD_NEGATIVE", "product": row["product"],
                 "source_campaign": row.get("source_campaign") or "", "value": row["search_term"],
                 "match": "Negative Exact", "object": "keyword"}]
    return [{"op": "ADD_NEGATIVE", "product": row["product"],
             "source_campaign": row.get("source_campaign") or "", "value": row["search_term"],
             "match": "Negative PAT", "object": "asin"}]


def _artifacts(plan, kept_ids, cfg):
    """Group kept keyword promotes by (product, root) → one MKW · Ex. campaign per
    root whose Targets are all the graduated keywords sharing that root. ASINs go to
    the PT campaign. Every graduate also writes a negative back to its source."""
    targets, negatives = [], []
    groups = {}   # (product, root) -> [member rows], insertion-ordered
    for p in plan:
        if p["decision"] == "SKIP-EXISTS" and p.get("negative_only"):
            negatives += _neg_rows(p); continue
        if p["decision"] != "PROMOTE" or id(p) not in kept_ids:
            continue
        negatives += _neg_rows(p)
        if p["object"] == "keyword":
            groups.setdefault((p["product"], p["root"]), []).append(p)
        else:                                               # ASIN → PT Ex add-to-existing
            targets.append(_pt_target(p, cfg))

    create = []
    for (product, root), members in groups.items():
        create.append(_mkw_group_row(product, root, members, cfg, "Ex.", "Rank"))
    return create, targets, negatives


def _base_create(p, cfg):
    return {h: "" for h in CREATE_HEADERS} | {
        "Action": "Create", "Product Name": p["product"], "Campaign Type": "SPM",
        "Campaign Goal": "Rank", "Daily Budget": cfg.daily_budget,
        "Targeting Type": "Manual Targeting", "Bidding Strategy": "Fixed Bids",
        "Placement Bid Adjustment (Top of Search)": cfg.tos_bid,
        "Placement Bid Adjustment (Rest of Search)": cfg.ros_bid,
        "Portfolio": p["product"], "ASIN/SKU": p.get("product_asin", ""),
        "Default Bid": p.get("suggested_bid", cfg.bid_min),
        "Starting Bid": p.get("suggested_bid", cfg.bid_min),
    }


def _mkw_group_row(product, root, members, cfg, match, goal):
    """One themed multi-keyword campaign for a root. Root KW = the root; Targets =
    all graduated keywords sharing that root (newline-separated); the ad-group
    default bid = the average of the members' per-keyword bids."""
    terms = list(dict.fromkeys(m["search_term"] for m in members))
    bids = [m.get("suggested_bid") for m in members if m.get("suggested_bid")]
    bid = round(sum(bids) / len(bids), 2) if bids else cfg.bid_min
    p0 = members[0]
    return _base_create(p0, cfg) | {
        "KW or PT": "MKW", "Match Type": match, "Root KW": root,
        "Campaign Goal": goal,
        "Campaign Name": f'{product} | SPM | MKW | {match} | {root} | {goal}',
        "Ad Group Name": f'{product} | MKW | {root} | {match}',
        "Campaign Tag": f'{product} > {goal}',
        "Targets": "\n".join(terms),
        "Default Bid": bid, "Starting Bid": bid,
        "Helper": f'{product}-MKW-{match}-{root}',
    }


def _pt_target(p, cfg):
    asin = re.sub(r'.*asin="?', "", p["search_term"], flags=re.I)
    m = _ASIN_RE.search(asin); asin = (m.group(0) if m else p["search_term"]).upper()
    grp = "Main"   # High/Main split needs target-ASIN price; defaults to Main here.
    return {"op": "ADD_TARGET", "product": p["product"],
            "campaign": f'{p["product"]} | SPM | PT | Ex. | {grp} | Perf',
            "ad_group": f'{p["product"]} | PT | {grp} | Ex.',
            "target": asin, "expression": "exact", "match": "Ex.",
            "bid": p.get("suggested_bid", cfg.bid_min), "source_term": p["search_term"]}


def to_csv(rows, headers):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue()
