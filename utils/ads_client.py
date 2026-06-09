"""Amazon Advertising API client (Sponsored Products).

Covers what the bid optimizer needs:
  - list profiles (account selection)
  - list SP campaigns + keywords (with current bids and placement adjustments)
  - pull keyword performance via the v3 reporting API (async: create/poll/download)
  - apply keyword bid updates and campaign placement bid adjustments

Auth: every call sends `Authorization: Bearer <lwa access token>` and
`Amazon-Advertising-API-ClientId: <client id>`. Entity/report calls also send
`Amazon-Advertising-API-Scope: <profileId>`.

Note: live calls require the security profile to have the Advertising API
enabled and the seller account onboarded; otherwise /v2/profiles is empty.
"""

import gzip
import io
import json
import time

import requests

from config import cfg
from utils.lwa_auth import get_access_token


class AdsApiError(RuntimeError):
    pass


class AdsClient:
    def __init__(self, refresh_token, profile_id=None):
        self.refresh_token = refresh_token
        self.profile_id = profile_id
        self.base = cfg.ADS_API_ENDPOINT

    def _headers(self, scoped=True, extra=None):
        h = {
            "Authorization": f"Bearer {get_access_token(self.refresh_token)}",
            "Amazon-Advertising-API-ClientId": cfg.LWA_CLIENT_ID,
            "Content-Type": "application/json",
        }
        if scoped and self.profile_id:
            h["Amazon-Advertising-API-Scope"] = str(self.profile_id)
        if extra:
            h.update(extra)
        return h

    def _paginate(self, path, list_key, media_type, state="ENABLED"):
        """POST {path}/list with v3 media-type headers, following nextToken."""
        headers = self._headers(extra={
            "Content-Type": media_type, "Accept": media_type})
        items, token = [], None
        while True:
            body = {"maxResults": 500, "stateFilter": {"include": [state]}}
            if token:
                body["nextToken"] = token
            resp = requests.post(self.base + path, headers=headers,
                                 data=json.dumps(body), timeout=60)
            if resp.status_code >= 400:
                raise AdsApiError(f"{path} failed: {resp.status_code} {resp.text}")
            data = resp.json()
            items.extend(data.get(list_key, []))
            token = data.get("nextToken")
            if not token:
                return items

    # -- profiles ---------------------------------------------------------

    def list_profiles(self):
        resp = requests.get(self.base + "/v2/profiles",
                            headers=self._headers(scoped=False), timeout=30)
        if resp.status_code >= 400:
            raise AdsApiError(f"profiles failed: {resp.status_code} {resp.text}")
        return resp.json()

    # -- entities ---------------------------------------------------------

    # Sponsored Products v3 media types.
    _MT_CAMPAIGN = "application/vnd.spCampaign.v3+json"
    _MT_KEYWORD = "application/vnd.spKeyword.v3+json"

    def list_campaigns(self):
        return self._paginate("/sp/campaigns/list", "campaigns", self._MT_CAMPAIGN)

    def list_keywords(self):
        return self._paginate("/sp/keywords/list", "keywords", self._MT_KEYWORD)

    def update_keyword_bids(self, updates):
        """updates: list of {keywordId, bid}. Returns API response."""
        payload = {"keywords": [
            {"keywordId": str(u["keywordId"]), "bid": round(float(u["bid"]), 2)}
            for u in updates]}
        headers = self._headers(extra={
            "Content-Type": self._MT_KEYWORD, "Accept": self._MT_KEYWORD})
        resp = requests.put(self.base + "/sp/keywords",
                            headers=headers, data=json.dumps(payload), timeout=60)
        if resp.status_code >= 400:
            raise AdsApiError(f"update keywords failed: {resp.status_code} {resp.text}")
        return resp.json()

    def update_campaign_placements(self, campaign_id, placement_bidding):
        """placement_bidding: list of {placement, percentage} adjustments (v3)."""
        body = {"campaigns": [{
            "campaignId": str(campaign_id),
            "dynamicBidding": {"placementBidding": placement_bidding}}]}
        headers = self._headers(extra={
            "Content-Type": self._MT_CAMPAIGN, "Accept": self._MT_CAMPAIGN})
        resp = requests.put(self.base + "/sp/campaigns",
                            headers=headers, data=json.dumps(body), timeout=60)
        if resp.status_code >= 400:
            raise AdsApiError(f"update placements failed: {resp.status_code} {resp.text}")
        return resp.json()

    # -- v3 reporting (async) ---------------------------------------------

    def keyword_report(self, start_date, end_date, poll_interval=10, timeout=600):
        """Create + poll + download an SP keyword SUMMARY report. Returns list[dict]."""
        config = {
            "name": "spKeywords-bidopt",
            "startDate": start_date,   # YYYY-MM-DD
            "endDate": end_date,
            "configuration": {
                "adProduct": "SPONSORED_PRODUCTS",
                "groupBy": ["targeting"],
                "columns": [
                    "keywordId", "campaignId", "impressions", "clicks", "cost",
                    "purchases30d", "sales30d",
                ],
                "reportTypeId": "spTargeting",
                "timeUnit": "SUMMARY",
                "format": "GZIP_JSON",
            },
        }
        headers = self._headers(extra={"Accept": "application/vnd.createasyncreportrequest.v3+json"})
        resp = requests.post(self.base + "/reporting/reports",
                            headers=headers, data=json.dumps(config), timeout=60)
        if resp.status_code >= 400:
            raise AdsApiError(f"create report failed: {resp.status_code} {resp.text}")
        report_id = resp.json()["reportId"]

        deadline = time.time() + timeout
        while True:
            r = requests.get(f"{self.base}/reporting/reports/{report_id}",
                            headers=self._headers(), timeout=30)
            if r.status_code >= 400:
                raise AdsApiError(f"get report failed: {r.status_code} {r.text}")
            info = r.json()
            status = info.get("status")
            if status == "COMPLETED":
                return self._download_report(info.get("url"))
            if status in {"FAILURE", "CANCELLED"}:
                raise AdsApiError(f"report ended {status}: {info}")
            if time.time() > deadline:
                raise AdsApiError("report timed out")
            time.sleep(poll_interval)

    def impression_share_report(self, start_date, end_date, poll_interval=10, timeout=600):
        """Create + poll + download an SP targeting report that includes
        topOfSearchImpressionShare per keyword. Returns list[dict].

        Columns returned: keywordId, campaignId, adGroupId,
                          impressions, topOfSearchImpressionShare.

        topOfSearchImpressionShare is a decimal (0–1). Amazon returns null
        when there is insufficient data for a keyword; those rows are kept
        so callers can distinguish 'no data' from 'zero IS'.
        """
        config = {
            "name": "spIS-dashboard",
            "startDate": start_date,
            "endDate": end_date,
            "configuration": {
                "adProduct": "SPONSORED_PRODUCTS",
                "groupBy": ["targeting"],
                "columns": [
                    "keywordId", "campaignId", "adGroupId",
                    "impressions", "topOfSearchImpressionShare",
                ],
                "reportTypeId": "spTargeting",
                "timeUnit": "SUMMARY",
                "format": "GZIP_JSON",
            },
        }
        headers = self._headers(extra={"Accept": "application/vnd.createasyncreportrequest.v3+json"})
        resp = requests.post(self.base + "/reporting/reports",
                             headers=headers, data=json.dumps(config), timeout=60)
        if resp.status_code >= 400:
            raise AdsApiError(f"create IS report failed: {resp.status_code} {resp.text}")
        report_id = resp.json()["reportId"]

        deadline = time.time() + timeout
        while True:
            r = requests.get(f"{self.base}/reporting/reports/{report_id}",
                             headers=self._headers(), timeout=30)
            if r.status_code >= 400:
                raise AdsApiError(f"get IS report failed: {r.status_code} {r.text}")
            info = r.json()
            status = info.get("status")
            if status == "COMPLETED":
                return self._download_report(info.get("url"))
            if status in {"FAILURE", "CANCELLED"}:
                raise AdsApiError(f"IS report ended {status}: {info}")
            if time.time() > deadline:
                raise AdsApiError("IS report timed out")
            time.sleep(poll_interval)

    def _download_report(self, url):
        resp = requests.get(url, timeout=120)
        resp.raise_for_status()
        raw = resp.content
        try:
            raw = gzip.decompress(raw)
        except (OSError, gzip.BadGzipFile):
            pass
        return json.loads(raw.decode("utf-8"))


def build_keyword_rows(campaigns, keywords, report_rows):
    """Merge entities + report metrics into rows for bid_optimizer.optimize()."""
    camp_name = {str(c.get("campaignId")): c.get("name") for c in campaigns}
    metrics = {}
    for row in report_rows or []:
        kid = str(row.get("keywordId"))
        if kid:
            metrics[kid] = row

    out = []
    for k in keywords:
        kid = str(k.get("keywordId"))
        m = metrics.get(kid, {})
        out.append({
            "keywordId": k.get("keywordId"),
            "campaignId": k.get("campaignId"),
            "campaignName": camp_name.get(str(k.get("campaignId")), ""),
            "keywordText": k.get("keywordText"),
            "matchType": k.get("matchType"),
            "bid": k.get("bid"),
            "impressions": m.get("impressions", 0),
            "clicks": m.get("clicks", 0),
            "spend": m.get("cost", 0),
            "sales": m.get("sales30d", 0),
            "orders": m.get("purchases30d", 0),
        })
    return out
