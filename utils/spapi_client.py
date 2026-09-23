"""SP-API Reports v2021-06-30 client.

Handles the request -> poll -> get-document -> download/decompress/parse cycle,
and a high-level pull of the three reports the inventory workbook needs.

Modern SP-API does not require AWS SigV4 signing — only the LWA access token,
carried in the `x-amz-access-token` header.
"""

import csv
import gzip
import io
import json
import time
from datetime import datetime, timedelta, timezone

import requests

from config import cfg
from utils.lwa_auth import get_access_token

_REPORTS = "/reports/2021-06-30/reports"
_DOCUMENTS = "/reports/2021-06-30/documents"

# Regional SP-API endpoints. A seller's reports must be requested from the
# endpoint + marketplace they actually participate in, or they come back FATAL.
REGION_ENDPOINTS = {
    "NA": "https://sellingpartnerapi-na.amazon.com",
    "EU": "https://sellingpartnerapi-eu.amazon.com",
    "FE": "https://sellingpartnerapi-fe.amazon.com",
}

# Real Amazon retail marketplaces, in selection priority. Sellers also "participate"
# in non-retail marketplaces (Amazon Pay, Sandbox, "Non-Amazon …") that are invalid
# for reports — those must be filtered out.
RETAIL_MARKETPLACES = [
    "ATVPDKIKX0DER",  # US
    "A2EUQ1WTGCTBG2",  # CA
    "A1AM78C64UM0Y8",  # MX
    "A2Q3Y263D00KWC",  # BR
    "A1F83G8C2ARO7P",  # UK
    "A1PA6795UKMFR9",  # DE
    "A13V1IB3VIYZZH",  # FR
    "APJ6JRA9NG5V4",   # IT
    "A1RKKUPIHCS9HS",  # ES
    "A1805IZSGTT6HS",  # NL
    "A2NODRKZP88ZB9",  # SE
    "A1C3SOZRARQ6R3",  # PL
    "A33AVAJ2PDY3EV",  # TR
    "A2VIGQ35RCS4UG",  # AE
    "A21TJRUUN4KGV",   # IN
    "A17E79C6D8DWNP",  # SA
    "A1VC38T7YXB528",  # JP
    "A39IBJ37TRP1C6",  # AU
    "A19VAU5U5O7RUS",  # SG
]
_RETAIL_SET = set(RETAIL_MARKETPLACES)

# Marketplace id -> short market code for the workbook/sheet label.
MARKET_CODES = {
    "ATVPDKIKX0DER": "US", "A2EUQ1WTGCTBG2": "CA", "A1AM78C64UM0Y8": "MX",
    "A2Q3Y263D00KWC": "BR", "A1F83G8C2ARO7P": "UK", "A1PA6795UKMFR9": "DE",
    "A13V1IB3VIYZZH": "FR", "APJ6JRA9NG5V4": "IT", "A1RKKUPIHCS9HS": "ES",
    "A1805IZSGTT6HS": "NL", "A2NODRKZP88ZB9": "SE", "A1C3SOZRARQ6R3": "PL",
    "A33AVAJ2PDY3EV": "TR", "A2VIGQ35RCS4UG": "AE", "A21TJRUUN4KGV": "IN",
    "A17E79C6D8DWNP": "SA", "A1VC38T7YXB528": "JP", "A39IBJ37TRP1C6": "AU",
    "A19VAU5U5O7RUS": "SG",
}

# Report type constants (from the skill).
RT_FBA_INVENTORY = "GET_FBA_MYI_ALL_INVENTORY_DATA"
RT_FBA_UNSUPPRESSED = "GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA"
RT_SALES_TRAFFIC = "GET_SALES_AND_TRAFFIC_REPORT"
# The Seller Central "All Listings Report" — full columns (item-name, seller-sku,
# asin1, fulfillment-channel, status …). The flat-file open-listings report only
# has sku/asin/price/quantity, so it is NOT used.
RT_OPEN_LISTINGS = "GET_MERCHANT_LISTINGS_ALL_DATA"
# Brand Analytics Search Query Performance (JSON) — weekly per-ASIN query demand,
# used by keyword harvesting for the SQP prioritization + CVR trend.
RT_SQP = "GET_BRAND_ANALYTICS_SEARCH_QUERY_PERFORMANCE_REPORT"

# Period label -> number of days back (skill legend values).
PERIODS = {
    "L30D": 30,
    "L60D": 60,
    "L90D": 90,
    "L180D": 180,
    "L270D": 270,
    "L365D": 365,
}

_TERMINAL_OK = {"DONE"}
_TERMINAL_BAD = {"CANCELLED", "FATAL"}


class SpApiError(RuntimeError):
    pass


class SpApiClient:
    def __init__(self, refresh_token, endpoint=None, marketplace_id=None):
        self.refresh_token = refresh_token
        self.endpoint = (endpoint or cfg.SPAPI_ENDPOINT).rstrip("/")
        self.marketplace_id = marketplace_id or cfg.SPAPI_DEFAULT_MARKETPLACE_ID

    # -- low level --------------------------------------------------------

    def _headers(self):
        return {
            "x-amz-access-token": get_access_token(
                self.refresh_token,
                client_id=cfg.SPAPI_CLIENT_ID,
                client_secret=cfg.SPAPI_CLIENT_SECRET,
            ),
            "content-type": "application/json",
        }

    def create_report(self, report_type, data_start=None, data_end=None, report_options=None,
                      max_retries=7):
        body = {
            "reportType": report_type,
            "marketplaceIds": [self.marketplace_id],
        }
        if data_start:
            body["dataStartTime"] = data_start
        if data_end:
            body["dataEndTime"] = data_end
        if report_options:
            body["reportOptions"] = report_options

        # createReport (esp. Sales & Traffic) has a very low quota. Retry on 429
        # with backoff so all 6 windows succeed instead of getting throttled.
        attempt = 0
        while True:
            resp = requests.post(
                self.endpoint + _REPORTS, headers=self._headers(),
                data=json.dumps(body), timeout=30,
            )
            if resp.status_code == 429 and attempt < max_retries:
                wait = float(resp.headers.get("Retry-After", 0)) or min(90, 20 * (attempt + 1))
                time.sleep(wait)
                attempt += 1
                continue
            if resp.status_code >= 400:
                raise SpApiError(f"createReport {report_type} failed: {resp.status_code} {resp.text}")
            return resp.json()["reportId"]

    def get_marketplace_participations(self):
        """GET /sellers/v1/marketplaceParticipations — which marketplaces this seller is in."""
        resp = requests.get(self.endpoint + "/sellers/v1/marketplaceParticipations",
                            headers=self._headers(), timeout=30)
        if resp.status_code >= 400:
            raise SpApiError(f"marketplaceParticipations failed: {resp.status_code} {resp.text}")
        return resp.json().get("payload", []) or []

    def fetch_fba_inventory_summaries(self):
        """GET /fba/inventory/v1/summaries — synchronous FBA inventory with quantity
        breakdown. Returns rows keyed like GET_FBA_MYI_ALL_INVENTORY_DATA so the
        workbook builder can consume them unchanged. Far more reliable than the report.
        """
        rows = []
        token = None
        while True:
            params = {
                "details": "true",
                "granularityType": "Marketplace",
                "granularityId": self.marketplace_id,
                "marketplaceIds": self.marketplace_id,
            }
            if token:
                params["nextToken"] = token
            resp = requests.get(self.endpoint + "/fba/inventory/v1/summaries",
                                headers=self._headers(), params=params, timeout=60)
            if resp.status_code >= 400:
                raise SpApiError(f"fba inventory summaries failed: {resp.status_code} {resp.text}")
            payload = resp.json().get("payload", {}) or {}
            for s in payload.get("inventorySummaries", []) or []:
                d = s.get("inventoryDetails", {}) or {}
                reserved = d.get("reservedQuantity", {}) or {}
                researching = d.get("researchingQuantity", {}) or {}
                unfulfillable = d.get("unfulfillableQuantity", {}) or {}
                rows.append({
                    "sku": s.get("sellerSku", ""),
                    "fnsku": s.get("fnSku", ""),
                    "asin": s.get("asin", ""),
                    "product-name": s.get("productName", ""),
                    "condition": s.get("condition", ""),
                    "your-price": "",
                    "mfn-listing-exists": "",
                    "mfn-fulfillable-quantity": 0,
                    "afn-listing-exists": "Yes",
                    "afn-warehouse-quantity": 0,
                    "afn-fulfillable-quantity": d.get("fulfillableQuantity", 0) or 0,
                    "afn-unsellable-quantity": unfulfillable.get("totalUnfulfillableQuantity", 0) or 0,
                    "afn-reserved-quantity": reserved.get("totalReservedQuantity", 0) or 0,
                    "afn-total-quantity": s.get("totalQuantity", 0) or 0,
                    "per-unit-volume": 0,
                    "afn-inbound-working-quantity": d.get("inboundWorkingQuantity", 0) or 0,
                    "afn-inbound-shipped-quantity": d.get("inboundShippedQuantity", 0) or 0,
                    "afn-inbound-receiving-quantity": d.get("inboundReceivingQuantity", 0) or 0,
                    "afn-researching-quantity": researching.get("totalResearchingQuantity", 0) or 0,
                    "afn-reserved-future-supply": 0,
                    "afn-future-supply-buyable": 0,
                    "store": "",
                })
            token = (payload.get("pagination") or {}).get("nextToken")
            if not token:
                return rows

    def get_report(self, report_id):
        resp = requests.get(
            f"{self.endpoint}{_REPORTS}/{report_id}", headers=self._headers(), timeout=30,
        )
        if resp.status_code >= 400:
            raise SpApiError(f"getReport failed: {resp.status_code} {resp.text}")
        return resp.json()

    def get_document_meta(self, document_id):
        resp = requests.get(
            f"{self.endpoint}{_DOCUMENTS}/{document_id}", headers=self._headers(), timeout=30,
        )
        if resp.status_code >= 400:
            raise SpApiError(f"getReportDocument failed: {resp.status_code} {resp.text}")
        return resp.json()

    def poll_until_done(self, report_id, interval=5, timeout=900):
        """Poll a report until DONE. Returns the reportDocumentId."""
        deadline = time.time() + timeout
        while True:
            info = self.get_report(report_id)
            status = info.get("processingStatus")
            if status in _TERMINAL_OK:
                return info["reportDocumentId"]
            if status in _TERMINAL_BAD:
                raise SpApiError(f"report {report_id} ended with status {status}")
            if time.time() > deadline:
                raise SpApiError(f"report {report_id} timed out (last status {status})")
            time.sleep(interval)

    def download_document(self, document_id):
        """Download a report document and return decoded text."""
        meta = self.get_document_meta(document_id)
        url = meta["url"]
        compression = meta.get("compressionAlgorithm")
        resp = requests.get(url, timeout=120)
        resp.raise_for_status()
        raw = resp.content
        if compression == "GZIP":
            raw = gzip.decompress(raw)
        # Flat-file reports are often cp1252/latin-1; try utf-8 first.
        for enc in ("utf-8", "cp1252", "latin-1"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    # -- high level -------------------------------------------------------

    def run_report(self, report_type, data_start=None, data_end=None, report_options=None,
                   poll_interval=5):
        report_id = self.create_report(report_type, data_start, data_end, report_options)
        try:
            document_id = self.poll_until_done(report_id, interval=poll_interval)
        except SpApiError as e:
            # Surface which report failed and the window, so FATALs are diagnosable.
            window = f" [{data_start}..{data_end}]" if data_start else ""
            raise SpApiError(f"{report_type}{window}: {e}") from e
        return self.download_document(document_id)

    def fetch_tsv_report(self, report_type):
        """Run a flat-file report and return list[dict] (tab-delimited)."""
        text = self.run_report(report_type)
        return parse_tsv(text)

    def fetch_sqp(self, asins, weeks=4, max_reports=40, cache_get=None, cache_put=None):
        """Brand Analytics Search Query Performance, weekly, per ASIN, over the last
        `weeks` full weeks (the trend splits them recent-half vs prior-half). Amazon
        requires one report per (ASIN, week) — the report option `asin` is mandatory,
        so reports cannot be batched across ASINs; keeping `weeks` small is the main
        way to speed up a first (uncached) run. Returns flat rows:
        {asin, week_start, week_end, search_query, search_query_volume,
         asin_impression_share, asin_click_share, asin_cart_add_share,
         asin_purchase_share, asin_purchase_count}. Degrades to [] on any failure so
         a missing/late report never breaks the harvest run.

        Amazon SQP WEEK reports must span *exactly one* Sun–Sat reporting period, so
        we request one report per (ASIN, week). To stay fast, ALL reports are
        created up front and then polled together — Amazon generates them in
        parallel, instead of us waiting out a full create→poll→download cycle for
        each one in turn (which made a multi-ASIN run take many minutes).
        `max_reports` caps the total fan-out (asins × weeks).

        Completed weeks are immutable, so `cache_get(asin, week_start)` /
        `cache_put(asin, week_start, week_end, rows)` (optional) let a harvest reuse
        already-pulled weeks and only fetch the ones it's missing — near-instant on
        repeat runs. The SP-API report is only requested for cache misses."""
        # Amazon SQP weeks are Sun–Sat and require dataStartTime to be a Sunday.
        # Anchor on the most recent completed Saturday (weekday 5).
        last_sat = datetime.now(timezone.utc).date() - timedelta(days=2)
        last_sat -= timedelta(days=(last_sat.weekday() - 5) % 7)
        # Build the list of full weeks, most recent first: (sunday, saturday).
        week_spans = []
        for i in range(weeks):
            sat = last_sat - timedelta(days=7 * i)
            week_spans.append((sat - timedelta(days=6), sat))

        # 1) Build the (ASIN, week) task list, capped. Serve cache hits immediately
        #    and only fetch the misses from SP-API.
        rows, tasks = [], []
        for asin in [a for a in dict.fromkeys(asins) if a]:
            for sun, sat in week_spans:
                if len(tasks) >= max_reports:
                    break
                cached = cache_get(asin, sun.isoformat()) if cache_get else None
                if cached is not None:
                    rows.extend(cached)
                    continue
                tasks.append({"asin": asin, "sun": sun, "sat": sat})
            if len(tasks) >= max_reports:
                break

        # 2) Create every missing report up front (create_report backs off on 429).
        pending = []
        for t in tasks:
            try:
                t["report_id"] = self.create_report(
                    RT_SQP,
                    data_start=f"{t['sun'].isoformat()}T00:00:00Z",
                    data_end=f"{t['sat'].isoformat()}T00:00:00Z",
                    report_options={"reportPeriod": "WEEK", "asin": t["asin"]},
                )
                pending.append(t)
            except SpApiError:
                continue

        # 3) Poll them all together until each is terminal (parallel generation).
        done, deadline = [], time.time() + 900
        while pending and time.time() < deadline:
            still = []
            for t in pending:
                try:
                    info = self.get_report(t["report_id"])
                except SpApiError:
                    continue  # transient — drop this one
                status = info.get("processingStatus")
                if status in _TERMINAL_OK:
                    t["doc"] = info.get("reportDocumentId")
                    done.append(t)
                elif status in _TERMINAL_BAD:
                    continue  # FATAL/CANCELLED — skip
                else:
                    still.append(t)
            pending = still
            if pending:
                time.sleep(5)

        # 4) Download + parse the finished reports, caching each completed week.
        for t in done:
            if not t.get("doc"):
                continue
            try:
                text = self.download_document(t["doc"])
            except Exception:  # noqa: BLE001 — one bad doc must not sink the batch
                continue
            week_rows = []
            for r in parse_sqp(text):
                # Guarantee week bounds even if the report omits them.
                r["week_start"] = r.get("week_start") or t["sun"].isoformat()
                r["week_end"] = r.get("week_end") or t["sat"].isoformat()
                week_rows.append(r)
            rows.extend(week_rows)
            if cache_put:
                try:
                    cache_put(t["asin"], t["sun"].isoformat(), t["sat"].isoformat(), week_rows)
                except Exception:  # noqa: BLE001 — caching must never break the run
                    pass
        return rows

    def fetch_sales_traffic(self, days):
        """Run GET_SALES_AND_TRAFFIC_REPORT for the last `days` and return its JSON.

        End date is 2 days before today — the current/just-past days are not yet
        finalized, and requesting them makes the report come back FATAL.
        """
        end = datetime.now(timezone.utc).date() - timedelta(days=2)
        start = end - timedelta(days=days)
        text = self.run_report(
            RT_SALES_TRAFFIC,
            data_start=f"{start.isoformat()}T00:00:00Z",
            data_end=f"{end.isoformat()}T00:00:00Z",
            report_options={"asinGranularity": "CHILD", "dateGranularity": "DAY"},
        )
        return json.loads(text)


def resolve_endpoint_and_marketplace(refresh_token, preferred_region="NA"):
    """Find the seller's actual region + marketplace by probing participations.

    Returns (endpoint, marketplace_id, region, market_name, store_name). Falls back
    to the preferred region + configured default marketplace if nothing resolves.
    `store_name` is the seller's business/store name (e.g. "Healthful Seasons, LLC").
    """
    order = [preferred_region] + [r for r in REGION_ENDPOINTS if r != preferred_region]
    for region in order:
        endpoint = REGION_ENDPOINTS[region]
        try:
            parts = SpApiClient(refresh_token, endpoint=endpoint).get_marketplace_participations()
        except SpApiError:
            continue
        active = [p for p in parts if (p.get("participation") or {}).get("isParticipating")]
        # Keep only real Amazon retail marketplaces (drop Amazon Pay / sandbox / non-Amazon).
        retail = [p for p in active
                  if (p.get("marketplace", {}) or {}).get("id") in _RETAIL_SET]
        if retail:
            by_id = {(p.get("marketplace", {}) or {}).get("id"): p for p in retail}
            # Pick by global priority order (US first).
            for mid in RETAIL_MARKETPLACES:
                if mid in by_id:
                    entry = by_id[mid]
                    mk = entry.get("marketplace", {}) or {}
                    return (endpoint, mid, region,
                            mk.get("name") or mk.get("countryCode"),
                            entry.get("storeName"))
    return (REGION_ENDPOINTS.get(preferred_region, cfg.SPAPI_ENDPOINT),
            cfg.SPAPI_DEFAULT_MARKETPLACE_ID, preferred_region, None, None)


def get_store_name(refresh_token, preferred_region="NA"):
    """Best-effort: return the seller's store/business name, or None."""
    try:
        return resolve_endpoint_and_marketplace(refresh_token, preferred_region)[4]
    except Exception:  # noqa: BLE001 — name resolution must never break the caller
        return None


def parse_tsv(text):
    """Parse tab-delimited report text into a list of dicts keyed by header."""
    text = text.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    return [dict(row) for row in reader]


def parse_sqp(text):
    """Flatten a Search Query Performance JSON report into per-week rows. Defensive
    about the nested SQP shape (searchQueryData / impressionData / clickData /
    cartAddData / purchaseData)."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return []
    out = []
    for e in (data.get("dataByAsin") or data.get("dataByDepartmentAndSearchTerm") or []):
        sq = e.get("searchQueryData") or {}
        imp = e.get("impressionData") or {}
        clk = e.get("clickData") or {}
        cart = e.get("cartAddData") or {}
        pur = e.get("purchaseData") or {}
        q = (sq.get("searchQuery") or e.get("searchQuery") or "").strip()
        if not q:
            continue
        out.append({
            "asin": e.get("asin", ""),
            "week_start": e.get("startDate", ""), "week_end": e.get("endDate", ""),
            "search_query": q,
            "search_query_volume": sq.get("searchQueryVolume", 0) or 0,
            "asin_impression_share": imp.get("asinImpressionShare", 0) or 0,
            "asin_click_share": clk.get("asinClickShare", 0) or 0,
            "asin_cart_add_share": cart.get("asinCartAddShare", 0) or 0,
            "asin_purchase_share": pur.get("asinPurchaseShare", 0) or 0,
            "asin_purchase_count": pur.get("asinPurchaseCount", 0) or 0,
            "asin_click_count": clk.get("asinClickCount", 0) or 0,
        })
    return out


def _enrich_fba_from_listings(fba_rows, listings):
    """Fill your-price / mfn-listing-exists / afn-listing-exists on Inventory-API
    fallback rows using the All Listings report (which we already have)."""
    by_sku = {r.get("seller-sku"): r for r in listings if r.get("seller-sku")}
    for row in fba_rows:
        lst = by_sku.get(row.get("sku"))
        if not lst:
            continue
        if not row.get("your-price"):
            row["your-price"] = lst.get("price", "")
        channel = (lst.get("fulfillment-channel") or "").upper()
        is_mfn = channel in ("", "DEFAULT") or channel.startswith("MFN")
        row["mfn-listing-exists"] = "Yes" if is_mfn else "No"
        row["afn-listing-exists"] = "No" if is_mfn else "Yes"


def pull_inventory_reports(client: SpApiClient, progress=None):
    """Pull the three reports the inventory workbook needs.

    Returns dict:
      {
        "fba_inventory": [ {col: val}, ... ],   # GET_FBA_MYI_ALL_INVENTORY_DATA
        "open_listings": [ {col: val}, ... ],   # GET_FLAT_FILE_OPEN_LISTINGS_DATA
        "sales_by_period": { "L30D": {child_asin: units, ...}, ... },
        "sales_meta": { "L30D": {child_asin: {...full entry...}}, ... },
      }
    `progress(msg)` is an optional callback for status updates.
    """
    def note(msg):
        if progress:
            progress(msg)

    note("Requesting All Listings report…")
    listings = client.fetch_tsv_report(RT_OPEN_LISTINGS)

    # FBA inventory: prefer the full report (all columns). Amazon returns FATAL
    # transiently on this report, so retry several times, then try the unsuppressed
    # variant, then fall back to the synchronous Inventory API enriched with listings.
    fba = None
    plan = [(RT_FBA_INVENTORY, 5), (RT_FBA_UNSUPPRESSED, 3)]
    for report_type in (rt for rt, _ in plan):
        attempts = dict(plan)[report_type]
        for attempt in range(1, attempts + 1):
            note(f"Loading FBA inventory report — try {attempt}/{attempts} "
                 f"(Amazon retries are normal)…")
            try:
                fba = client.fetch_tsv_report(report_type)
                note(f"FBA inventory report loaded ({len(fba)} SKUs).")
                break
            except SpApiError:
                time.sleep(25)
        if fba:
            break
    if not fba:
        note("FBA report kept timing out on Amazon's side — using Inventory API "
             "(quantities are exact; some display columns filled from listings).")
        fba = client.fetch_fba_inventory_summaries()
        _enrich_fba_from_listings(fba, listings)

    sales_by_period = {}
    sales_meta = {}
    for label, days in PERIODS.items():
        note(f"Requesting Business Report ({label})…")
        try:
            data = client.fetch_sales_traffic(days)
        except SpApiError as e:
            # Degrade gracefully: a single bad window (e.g. account younger than
            # the window) becomes zero sales for that period rather than failing.
            note(f"Business Report ({label}) unavailable — skipping. {e}")
            sales_by_period[label] = {}
            sales_meta[label] = {}
            continue
        by_asin = data.get("salesAndTrafficByAsin", []) or []
        units = {}
        meta = {}
        for entry in by_asin:
            child = entry.get("childAsin") or entry.get("asin")
            if not child:
                continue
            sales = entry.get("salesByAsin", {}) or {}
            units[child] = int(sales.get("unitsOrdered", 0) or 0)
            meta[child] = entry
        sales_by_period[label] = units
        sales_meta[label] = meta

    note("All reports downloaded.")
    return {
        "fba_inventory": fba,
        "open_listings": listings,
        "sales_by_period": sales_by_period,
        "sales_meta": sales_meta,
    }
