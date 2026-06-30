"""Amazon Sponsored Products bulk-upload sheet generator.

Turns the assembled campaign plan into Amazon's Entity-row bulk format (the
"Sponsored Products Campaigns" sheet) so the output uploads directly in Bulk
Operations — no manual conversion. Net-new only: every row Operation=Create with
temp negative IDs linking children to their parent in the same upload, so each
campaign gets exactly one ad group (no duplicates).

Scope: Sponsored Products only — SKW/MKW (keyword), PT/STPP (product targeting),
SPA (auto). Sponsored Brands / Display need creative assets Amazon won't accept
via bulk-create and are intentionally excluded. Negatives are omitted for now.
"""

from __future__ import annotations

import datetime as _dt

import openpyxl

from utils import campaign_orchestrator as orch

# Exact Amazon Sponsored Products bulksheet header order.
SP_HEADERS = [
    "Product", "Entity", "Operation", "Campaign ID", "Ad Group ID", "Portfolio ID",
    "Ad ID", "Keyword ID", "Product Targeting ID", "Campaign Name", "Ad Group Name",
    "Start Date", "End Date", "Targeting Type", "State", "Daily Budget", "SKU",
    "Ad Group Default Bid", "Bid", "Keyword Text", "Native Language Keyword",
    "Native Language Locale", "Match Type", "Bidding Strategy", "Placement",
    "Percentage", "Product Targeting Expression", "Audience ID",
    "Shopper Cohort Percentage", "Shopper Cohort Type", "Sites", "Off-Amazon ad serving",
]

# Planning Match Type -> Amazon keyword match type.
_MATCH = {"Ex.": "exact", "Br.": "broad", "Br.M": "broad", "Ph.": "phrase"}
# Planning Bidding Strategy -> Amazon strategy.
_STRATEGY = {"Fixed Bids": "Fixed bid", "Down Only": "Dynamic bids - down only",
             "Up and Down Only": "Dynamic bids - up and down"}
# PAT category -> BuildInput ASIN-bucket attribute.
_PAT_BUCKET = {"Main": "main_competitor_asins", "Low Rated": "lower_rated_asins",
               "High Priced": "higher_priced_asins", "Bestselling": "bestselling_asins"}

DEFAULT_AG_BID = 0.75


def _clean(terms):
    """Drop junk (blanks, 1-char, flag values like 'Y'/'N', non-alpha) + dedupe."""
    out = []
    for t in terms:
        t = str(t or "").strip()
        if len(t) < 2 or not any(ch.isalpha() for ch in t):
            continue
        out.append(t)
    return list(dict.fromkeys(out))


def _le4(terms):
    """Negative PHRASE keywords are capped at 4 words by Amazon — drop longer ones."""
    return [t for t in terms if t and len(str(t).split()) <= 4]


def _negatives(cr, inp, skw_kws):
    """Expand the planning negative LABELS (cols AC/AD/AE) into real negative rows.
    AC -> negative phrase, AD -> negative exact, AE -> negative product targeting.
    Returns list of (entity, field, value, match) tuples. Labels with no data source
    (e.g. 'Misspellings KWs') are skipped."""
    own_kw = _le4(_clean((inp.own_branded_kws or []) + (inp.own_branded_searches or [])))
    comp_kw = _le4(_clean(inp.competitor_searches or []))
    out = []
    ac = str(cr.get("AC") or "")
    if "Own Branded KWs" in ac:
        out += [("Negative Keyword", k, "negativePhrase") for k in own_kw]
    if "Competitor KWs" in ac:
        out += [("Negative Keyword", k, "negativePhrase") for k in comp_kw]
    if "SKW EX. Match Keywords" in str(cr.get("AD") or ""):
        out += [("Negative Keyword", k, "negativeExact") for k in _clean(skw_kws)]
    if "Own Branded ASINs" in str(cr.get("AE") or ""):
        out += [("Negative Product Targeting", a, "") for a in (inp.own_brand_asins or [])]
    return out


def _name(cr):
    """Campaign Name = TEXTJOIN(' | ', skip blanks, B,C,D,E,F,G,H)."""
    parts = [cr.get(c, "") for c in ("B", "C", "D", "E", "F", "G", "H")]
    return " | ".join(str(p) for p in parts if p not in (None, ""))


def _ag_name(cr):
    """Ad Group Name = TEXTJOIN(' | ', skip blanks, B,E,G,F)."""
    parts = [cr.get(c, "") for c in ("B", "E", "G", "F")]
    return " | ".join(str(p) for p in parts if p not in (None, ""))


def build_sp_rows(inp):
    """Return a list of Amazon SP bulksheet row-dicts (keyed by SP_HEADERS) for
    every Sponsored Products campaign in the plan."""
    own_asin = inp.products[0]["asin"] if inp.products else ""
    own_sku = inp.products[0].get("sku", "") if inp.products else ""
    today = _dt.date.today().strftime("%Y%m%d")

    # Keyword pool per (root, match) for MKW expansion.
    sem = inp.semantics_rows
    # All SKW keywords (exact) — used to expand the "SKW EX. Match Keywords" negative.
    skw_kws = [s["keyword"] for s in sem if (s.get("disp_kw_type") or "").upper() == "SKW"]
    rows = []
    cid = -1   # temp campaign id counter (negative)

    def add(**kw):
        r = {h: "" for h in SP_HEADERS}
        r["Product"] = "Sponsored Products"
        r["Operation"] = "Create"
        r.update(kw)
        rows.append(r)

    for cr in inp.campaign_rows:
        ctype, e, f, g = cr.get("C"), cr.get("E"), cr.get("F"), cr.get("G")
        if ctype not in ("SPM", "SPA"):
            continue  # SB/SBV/SDI handled elsewhere (creative assets required)
        camp_id, ag_id = cid, cid       # reuse the same temp id within a campaign
        cid -= 1
        is_auto = ctype == "SPA"
        strategy = _STRATEGY.get(cr.get("P"), "Dynamic bids - down only")
        # Campaign
        add(Entity="Campaign", **{"Campaign ID": camp_id, "Campaign Name": _name(cr),
            "Start Date": today, "Targeting Type": "AUTO" if is_auto else "MANUAL",
            "State": "enabled", "Daily Budget": cr.get("K", 5) or 5,
            "Bidding Strategy": strategy})
        # Ad Group
        ag_bid = cr.get("Z") if isinstance(cr.get("Z"), (int, float)) and cr.get("Z") else DEFAULT_AG_BID
        add(Entity="Ad Group", **{"Campaign ID": camp_id, "Ad Group ID": ag_id,
            "Ad Group Name": _ag_name(cr), "State": "enabled",
            "Ad Group Default Bid": ag_bid})
        # Product Ad (the advertised own product)
        if own_sku or own_asin:
            add(Entity="Product Ad", **{"Campaign ID": camp_id, "Ad Group ID": ag_id,
                "State": "enabled", "SKU": own_sku or own_asin})
        # Negatives (expanded from the planning AC/AD/AE labels)
        for entity, val, match in _negatives(cr, inp, skw_kws):
            if entity == "Negative Keyword":
                add(Entity="Negative Keyword", **{"Campaign ID": camp_id, "Ad Group ID": ag_id,
                    "State": "enabled", "Keyword Text": val, "Match Type": match})
            else:  # Negative Product Targeting
                add(Entity="Negative Product Targeting", **{"Campaign ID": camp_id,
                    "Ad Group ID": ag_id, "State": "enabled",
                    "Product Targeting Expression": f'asin="{val}"'})
        # Targeting
        if is_auto:
            continue  # auto campaigns: Amazon creates the auto-targeting groups
        if e in ("SKW", "MKW"):
            match = _MATCH.get(f, "exact")
            if e == "SKW":
                kws = [(g, cr.get("Z"))]
            else:  # MKW: every keyword under this root + match
                kws = [(s["keyword"], None) for s in sem
                       if (s.get("category") or "").strip() == g
                       and (s.get("disp_kw_type") or "").upper() == "MKW"
                       and orch.canon_match(s.get("disp_match")) == f]
            for text, bid in kws:
                add(Entity="Keyword", **{"Campaign ID": camp_id, "Ad Group ID": ag_id,
                    "State": "enabled", "Keyword Text": text, "Match Type": match,
                    "Bid": bid if isinstance(bid, (int, float)) and bid else ""})
        elif e in ("PT", "STPP"):
            # Product targeting expressions. STPP (brand defence) targets the own
            # ASIN; PT targets the competitor ASINs of the category (G).
            if e == "STPP":
                asins = [own_asin] if own_asin else []
            else:
                asins = list(getattr(inp, _PAT_BUCKET.get(g, ""), []) or [])
            for asin in asins:
                add(Entity="Product Targeting", **{"Campaign ID": camp_id,
                    "Ad Group ID": ag_id, "State": "enabled",
                    "Product Targeting Expression": f'asin="{asin}"'})
    return rows


def build_sp_bulksheet(inp, out_path):
    """Write the Amazon SP bulksheet .xlsx to out_path. Returns (path, n_campaigns)."""
    rows = build_sp_rows(inp)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sponsored Products Campaigns"
    ws.append(SP_HEADERS)
    for r in rows:
        ws.append([r.get(h, "") for h in SP_HEADERS])
    wb.save(out_path)
    n_camp = sum(1 for r in rows if r["Entity"] == "Campaign")
    return out_path, n_camp
