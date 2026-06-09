"""Keyword bid optimizer — the 4-rule formula table.

| Rule                | Condition                          | New Bid                       |
|---------------------|------------------------------------|-------------------------------|
| High ACOS           | ACOS > Target ACOS                 | RPC x Target ACOS             |
| High Spend, No Sales| Spend > Target CPA and Orders = 0  | (AOV / Clicks) x Target ACOS  |
| Low ACOS            | ACOS < (Target - 20% buffer)       | Current Bid x 1.05..1.10      |
| Low Visibility      | Clicks < aCTC                      | Current Bid x 1.05            |

Rules are evaluated in the order above; the first matching rule wins.
Derived metrics: RPC = Sales/Clicks, ACOS = Spend/Sales, AOV = Sales/Orders
(account-level fallback), aCTC = account average Clicks per Order.
"""

DEFAULTS = {
    "buffer": 0.20,          # Low-ACOS buffer: threshold = target * (1 - buffer)
    "low_acos_mult": 1.07,   # within 1.05..1.10
    "low_vis_mult": 1.05,
    "min_bid": 0.02,
    "max_bid": 1000.0,
}


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def account_aggregates(keywords):
    """Account-level AOV and aCTC used as fallbacks."""
    tot_sales = sum(_f(k.get("sales")) for k in keywords)
    tot_orders = sum(_f(k.get("orders")) for k in keywords)
    tot_clicks = sum(_f(k.get("clicks")) for k in keywords)
    aov = (tot_sales / tot_orders) if tot_orders else 0.0
    actc = (tot_clicks / tot_orders) if tot_orders else 0.0
    return {"aov": aov, "actc": actc}


def optimize(keywords, target_acos, target_cpa, options=None):
    """Return enriched rows with proposed new_bid + rule (None if unchanged).

    keywords: list of dicts with at least keywordId, campaignId, campaignName,
    keywordText, matchType, bid, impressions, clicks, spend, sales, orders.
    target_acos: fraction (e.g. 0.30). target_cpa: dollars.
    """
    opt = {**DEFAULTS, **(options or {})}
    agg = account_aggregates(keywords)
    low_acos_threshold = target_acos * (1 - opt["buffer"])

    results = []
    for k in keywords:
        bid = _f(k.get("bid"))
        clicks = _f(k.get("clicks"))
        spend = _f(k.get("spend"))
        sales = _f(k.get("sales"))
        orders = _f(k.get("orders"))

        acos = (spend / sales) if sales > 0 else None
        rpc = (sales / clicks) if clicks > 0 else 0.0
        aov = (sales / orders) if orders > 0 else agg["aov"]

        rule = None
        new_bid = None

        if acos is not None and acos > target_acos:
            rule = "High ACOS"
            new_bid = rpc * target_acos
        elif spend > target_cpa and orders == 0:
            rule = "High Spend, No Sales"
            if clicks > 0:
                new_bid = (aov / clicks) * target_acos
            else:
                new_bid = bid  # no clicks -> leave unchanged
        elif acos is not None and acos < low_acos_threshold:
            rule = "Low ACOS"
            new_bid = bid * opt["low_acos_mult"]
        elif agg["actc"] and clicks < agg["actc"]:
            rule = "Low Visibility"
            new_bid = bid * opt["low_vis_mult"]

        if new_bid is not None:
            new_bid = round(min(max(new_bid, opt["min_bid"]), opt["max_bid"]), 2)
            if new_bid == round(bid, 2):
                rule, new_bid = None, None  # no effective change

        results.append({
            "keywordId": k.get("keywordId"),
            "campaignId": k.get("campaignId"),
            "campaign": k.get("campaignName"),
            "keyword": k.get("keywordText"),
            "match": k.get("matchType"),
            "placement": k.get("placement", ""),
            "current_bid": round(bid, 2),
            "new_bid": new_bid,
            "rule": rule,
            "impressions": int(_f(k.get("impressions"))),
            "clicks": int(clicks),
            "spend": round(spend, 2),
            "sales": round(sales, 2),
            "orders": int(orders),
            "acos": round(acos, 4) if acos is not None else None,
            "rpc": round(rpc, 4),
        })
    return results
