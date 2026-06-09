"""Assemble the Sheet-1 static product list (cols A-L) for the inventory workbook.

Sources every SKU from the FBA inventory + All Listings reports, looks up the
child ASIN / title / fulfilment, auto-assigns a BAND from sales velocity, and
merges any per-account overrides the user saved previously.

BAND auto-rules (per the skill's guidance — A fastest, EOL = no sales):
  - Zero units across all 6 periods            -> EOL
  - Otherwise rank by L30D units (then Avg):
       top 25%   -> BAND A
       next 35%  -> BAND B
       remainder -> BAND C
The user can override any of these in the preview table before generating.
"""

BANDS = ["BAND A", "BAND B", "BAND C", "EOL"]


def _is_fba(fba_row, listing_row):
    if fba_row and str(fba_row.get("afn-listing-exists", "")).strip().lower() in {"yes", "true", "1"}:
        return True
    chan = (listing_row or {}).get("fulfillment-channel", "") or ""
    if "AMAZON" in chan.upper() or chan.upper().startswith("AFN"):
        return True
    # FBA inventory presence implies FBA.
    if fba_row and (fba_row.get("afn-fulfillable-quantity") not in (None, "", "0") or fba_row.get("asin")):
        return True
    return False


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
        asin = (fba.get("asin") or listing.get("asin1") or "").strip()
        title = (fba.get("product-name") or listing.get("item-name") or "").strip()
        fulfillment = "FBA" if _is_fba(fba, listing) else "FBM"

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

    return rows


def _auto_assign_bands(rows):
    sellers = [r for r in rows if r["_total_units"] > 0]
    dead = [r for r in rows if r["_total_units"] <= 0]

    for r in dead:
        r["band"] = "EOL"

    if not sellers:
        return

    sellers.sort(key=lambda r: (r["_l30d"], r["_total_units"]), reverse=True)
    n = len(sellers)
    a_cut = max(1, round(n * 0.25))
    b_cut = a_cut + max(1, round(n * 0.35))
    for i, r in enumerate(sellers):
        if i < a_cut:
            r["band"] = "BAND A"
        elif i < b_cut:
            r["band"] = "BAND B"
        else:
            r["band"] = "BAND C"
