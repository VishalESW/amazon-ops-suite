"""Keyword harvesting / negation logic over AdLabs search-term & SQP data.

Splits search terms into:
  - HARVEST: converting terms worth adding as exact keywords (orders, ACOS <= target)
  - NEGATE:  wasted-spend terms worth blocking (spend, no sales / ACOS too high)

SQP (search query performance) rows become harvest *opportunities* — high-volume
queries the product converts on but isn't yet targeting.

All inputs are AdLabs TSV rows (string values); numbers are parsed defensively.
"""

import re

from utils.bid_optimizer import DEFAULTS as _OPT_DEFAULTS

_ASIN_RE = re.compile(r"^b0[0-9a-z]{8}$", re.I)


def _f(v, d=0.0):
    try:
        return float(str(v).replace(",", "").replace("$", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return d


def _looks_like_asin(term):
    return bool(_ASIN_RE.match((term or "").strip()))


def _account_agg(rows):
    """Account-level AOV and aCTC (avg clicks per order) for fallbacks in rule 4."""
    tot_sales = sum(_f(r.get("sales")) for r in rows)
    tot_orders = sum(_f(r.get("orders")) for r in rows)
    tot_clicks = sum(_f(r.get("clicks")) for r in rows)
    return {
        "aov": (tot_sales / tot_orders) if tot_orders else 0.0,
        "actc": (tot_clicks / tot_orders) if tot_orders else 0.0,
    }


def _harvest_bid(cpc, rpc, clicks, acos, target_acos, agg):
    """Starting bid for a harvested keyword using the 4-rule formula.

    CPC is used as a proxy for the current bid (no existing bid for new keywords).
    Rules evaluated in order; first match wins:
      Rule 1 – High ACOS        : acos > target  → RPC × target  (rare for harvest)
      Rule 3 – Low ACOS         : acos < target × 0.80 → CPC × 1.07
      Rule 4 – Low Visibility   : clicks < account aCTC → CPC × 1.05
      Default – on-target       : CPC (no adjustment needed)
    """
    bid = max(cpc, 0.05)
    low_threshold = target_acos * (1 - _OPT_DEFAULTS["buffer"])

    if acos is not None and acos > target_acos:
        new_bid = rpc * target_acos
    elif acos is not None and acos < low_threshold:
        new_bid = bid * _OPT_DEFAULTS["low_acos_mult"]
    elif agg["actc"] and clicks < agg["actc"]:
        new_bid = bid * _OPT_DEFAULTS["low_vis_mult"]
    else:
        new_bid = bid

    return max(0.05, round(min(new_bid, _OPT_DEFAULTS["max_bid"]), 2))


def categorize_search_terms(rows, target_acos, min_negate_spend=1.0,
                            negate_acos_mult=1.5):
    """Return {"harvest": [...], "negate": [...], "all": [...]} from search_term rows.

    `all` is every parsed term annotated with a `category` (harvest / negate / neutral)
    for the "All search terms" overview.
    """
    # Pre-compute account-level aggregates (needed for rule 4 – Low Visibility).
    agg = _account_agg(rows)

    harvest, negate, all_terms = [], [], []
    for r in rows:
        term = (r.get("search_term") or "").strip()
        if not term:
            continue
        clicks = _f(r.get("clicks")); spend = _f(r.get("spend"))
        sales = _f(r.get("sales")); orders = _f(r.get("orders"))
        acos = (spend / sales) if sales > 0 else None
        cpc = (spend / clicks) if clicks > 0 else 0.0
        rpc = (sales / clicks) if clicks > 0 else 0.0
        already = bool((r.get("harvested_targets") or "").strip())
        ad_type = r.get("campaign_ad_type", "")

        base = {
            "search_term": term, "search_term_id": r.get("search_term_id"),
            "campaign": r.get("campaign_name"), "campaign_id": r.get("campaign_id"),
            "ad_group": r.get("ad_group_name"), "ad_group_id": r.get("ad_group_id"),
            "match_types": r.get("match_types"), "campaign_ad_type": ad_type,
            "impressions": int(_f(r.get("impressions"))),
            "clicks": int(clicks), "spend": round(spend, 2), "sales": round(sales, 2),
            "orders": int(orders), "acos": round(acos, 4) if acos is not None else None,
            "cvr": _f(r.get("cvr")), "cpc": round(cpc, 2), "rpc": round(rpc, 4),
            "already_harvested": already, "is_brand": (r.get("is_brand_asin") or "").lower() in ("true", "1", "yes"),
        }

        category = "neutral"
        # HARVEST: converting, efficient, not an ASIN, not already harvested.
        if (orders >= 1 and acos is not None and acos <= target_acos
                and not already and not _looks_like_asin(term)):
            harvest.append({**base, "suggested_bid": _harvest_bid(cpc, rpc, int(clicks), acos, target_acos, agg),
                            "reason": f"{int(orders)} orders at {acos*100:.0f}% ACOS"})
            category = "harvest"
        # NEGATE: wasted spend or very inefficient.
        elif spend >= min_negate_spend and orders == 0:
            negate.append({**base, "suggested_match": "EXACT",
                           "reason": f"${spend:.2f} spend, 0 orders"})
            category = "negate"
        elif acos is not None and acos >= negate_acos_mult * target_acos:
            negate.append({**base, "suggested_match": "EXACT",
                           "reason": f"ACOS {acos*100:.0f}% (>{negate_acos_mult:.1f}× target)"})
            category = "negate"

        all_terms.append({**base, "category": category})

    harvest.sort(key=lambda x: x["sales"], reverse=True)
    negate.sort(key=lambda x: x["spend"], reverse=True)
    all_terms.sort(key=lambda x: x["spend"], reverse=True)
    return {"harvest": harvest, "negate": negate, "all": all_terms}


def sqp_opportunities(rows, max_rows=60):
    """High-volume, converting SQP queries not yet targeted — harvest opportunities."""
    out = []
    for r in rows:
        q = (r.get("search_query") or "").strip()
        if not q or _looks_like_asin(q):
            continue
        volume = _f(r.get("search_query_volume"))
        purchases = _f(r.get("asin_purchase_count"))
        conv = _f(r.get("asin_conversion_rate"))
        purchase_share = _f(r.get("asin_purchase_share"))
        targeted = bool((r.get("existing_targets") or "").strip())
        # Opportunity: real volume + the product actually converts on it + not targeted.
        if volume <= 0 or targeted or purchases < 1:
            continue
        out.append({
            "search_query": q, "volume": int(volume),
            "purchases": int(purchases), "conversion": round(conv, 4),
            "purchase_share": round(purchase_share, 4),
            "click_share": _f(r.get("asin_click_share")),
            "asin": r.get("asin"), "brand": r.get("brand"), "title": r.get("title"),
            "score": round(volume * max(conv, 0.0) * max(purchase_share, 0.0), 2),
        })
    out.sort(key=lambda x: (x["purchases"], x["volume"]), reverse=True)
    return out[:max_rows]
