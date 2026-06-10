"""Assemble the Sheet-1 static product list (cols A-L) for the inventory workbook.

Sources every FBA SKU from the FBA inventory + All Listings reports (FBM/MFN
products are excluded), looks up the child ASIN / title / fulfilment,
auto-assigns a BAND from sales velocity, and merges any per-account overrides
the user saved previously.

BAND auto-rules (A fastest). EOL is NOT auto-assigned — the client decides
end-of-life themselves, so every product is ranked into A/B/C by L30D units
(then total units):
       top 25%   -> BAND A
       next 35%  -> BAND B
       remainder -> BAND C   (zero-sales products fall here)
The user can override any of these in the preview table before generating.

The final list is ordered by BAND (A → B → C) then alphabetically (A-Z) by title.
"""

BANDS = ["BAND A", "BAND B", "BAND C"]

_BAND_ORDER = {"BAND A": 0, "BAND B": 1, "BAND C": 2}


def sort_product_list(rows):
    """Order rows by band (A→B→C), then A-Z by title, then SKU."""
    return sorted(rows, key=lambda r: (
        _BAND_ORDER.get(r.get("band"), 9),
        (r.get("title") or "").strip().lower(),
        (r.get("sku") or "").strip().lower(),
    ))


def _is_fba(fba_row, listing_row):
    """True only for Amazon-fulfilled (FBA) SKUs.

    The All Listings report's `fulfillment-channel` is authoritative:
      - AMAZON_* / AFN  -> FBA
      - DEFAULT / MFN / merchant -> FBM (excluded)
    A merchant channel is decisive even if the SKU also appears in the FBA
    inventory report (sellers often list an "-FBM" duplicate of an FBA ASIN).
    Only when there is no listing channel do we fall back to FBA-report signals.
    """
    chan = ((listing_row or {}).get("fulfillment-channel") or "").strip().upper()
    if chan:
        if "AMAZON" in chan or chan.startswith("AFN"):
            return True
        return False   # DEFAULT / MERCHANT / MFN / anything explicit = FBM

    # No listing channel — rely on the FBA report.
    if str((fba_row or {}).get("afn-listing-exists", "")).strip().lower() in {"yes", "true", "1"}:
        return True
    try:
        qty = float(str((fba_row or {}).get("afn-fulfillable-quantity", 0)).replace(",", "") or 0)
    except (TypeError, ValueError):
        qty = 0.0
    return qty > 0


def build_product_list(reports, overrides=None):
    """Build the ordered product list for Sheet 1.

    reports: output of spapi_client.pull_inventory_reports
    overrides: {sku: {band, category, size, color, title, asin, ...}} from db
    Returns list[dict] with keys: parent_sku, parent_asin, parent_title, sku,
    asin, title, brand, category, size, color, fulfillment, band, _l30d.
    """
    overrides = overrides or {}
    fba_by_sku = {r.get("sku"): r for r in reports.get("fba_inventory", []) if r.get("sku")}
    listing_by_sku = {
        r.get("seller-sku"): r for r in reports.get("open_listings", []) if r.get("seller-sku")
    }
    sales30 = reports.get("sales_by_period", {}).get("L30D", {})
    sales_all = reports.get("sales_by_period", {})

    skus = sorted(set(fba_by_sku) | set(listing_by_sku))

    rows = []
    for sku in skus:
        fba = fba_by_sku.get(sku, {})
        listing = listing_by_sku.get(sku, {})
        # FBA-only: skip FBM/MFN products entirely.
        if not _is_fba(fba, listing):
            continue
        asin = (fba.get("asin") or listing.get("asin1") or "").strip()
        title = (fba.get("product-name") or listing.get("item-name") or "").strip()
        fulfillment = "FBA"

        l30 = int(sales30.get(asin, 0) or 0)
        total_units = sum(int(period.get(asin, 0) or 0) for period in sales_all.values())

        rows.append({
            "parent_sku": "",
            "parent_asin": "",
            "parent_title": "",
            "sku": sku,
            "asin": asin,
            "title": title,
            "brand": "",
            "category": "",
            "size": "",
            "color": "",
            "fulfillment": fulfillment,
            "band": None,            # filled below
            "_l30d": l30,
            "_total_units": total_units,
        })

    _auto_assign_bands(rows)

    # Apply saved overrides last (they win over auto values).
    for r in rows:
        ov = overrides.get(r["sku"])
        if not ov:
            continue
        for key in ("band", "category", "size", "color", "brand",
                    "parent_sku", "parent_asin", "parent_title"):
            if ov.get(key) not in (None, ""):
                r[key] = ov[key]

    # Order by band (A→B→C) then A-Z by title.
    return sort_product_list(rows)


def _auto_assign_bands(rows):
    """Rank ALL products into A/B/C by velocity. EOL is never auto-assigned —
    zero-sales products simply fall into BAND C (lowest velocity)."""
    if not rows:
        return
    ranked = sorted(rows, key=lambda r: (r["_l30d"], r["_total_units"]), reverse=True)
    n = len(ranked)
    a_cut = max(1, round(n * 0.25))
    b_cut = a_cut + max(1, round(n * 0.35))
    for i, r in enumerate(ranked):
        if i < a_cut:
            r["band"] = "BAND A"
        elif i < b_cut:
            r["band"] = "BAND B"
        else:
            r["band"] = "BAND C"
