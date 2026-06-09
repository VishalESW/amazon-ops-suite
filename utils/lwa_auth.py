"""Login with Amazon (LWA) OAuth helpers, shared by SP-API and Advertising API.

Responsibilities:
  - Build the Seller Central consent URL (SP-API website authorization workflow).
  - Build the Amazon Advertising consent URL.
  - Exchange an authorization code for a refresh token.
  - Exchange a refresh token for a short-lived access token (cached in-memory).

References (verified against developer-docs.amazon.com):
  Consent:  https://sellercentral.amazon.com/apps/authorize/consent
            ?application_id=<APP_ID>&state=<state>[&version=beta]
  Token:    POST https://api.amazon.com/auth/o2/token
  Redirect: ...?state=&selling_partner_id=&spapi_oauth_code=
  Header:   SP-API calls carry the token in `x-amz-access-token`.
"""

import time
import threading
from urllib.parse import urlencode

import requests

from config import cfg

# Valid SP-API "Application ID" prefixes accepted in the consent URL.
# Newer apps use `amzn1.sp.solution.<uuid>`; older ones `amzn1.sellerapps.app.<uuid>`.
_VALID_APP_ID_PREFIXES = ("amzn1.sp.solution", "amzn1.sellerapps.app")

_SELLER_CENTRAL_CONSENT = "https://sellercentral.amazon.com/apps/authorize/consent"
_ADS_AUTH_URL = "https://www.amazon.com/ap/oa"

# refresh_token -> (access_token, expiry_epoch)
_token_cache: dict[str, tuple[str, float]] = {}
_cache_lock = threading.Lock()


# --------------------------------------------------------------- consent URLs ---

def spapi_consent_url(state: str) -> str:
    """Seller Central consent URL for the SP-API website authorization workflow.

    Note: redirect_uri is NOT a parameter here — Amazon uses the Redirect URI
    registered in the app listing. It must equal cfg.spapi_redirect_uri.
    """
    params = {
        "application_id": cfg.SPAPI_APPLICATION_ID,
        "state": state,
    }
    if cfg.SPAPI_APP_DRAFT:
        params["version"] = "beta"
    return f"{_SELLER_CENTRAL_CONSENT}?{urlencode(params)}"


def ads_consent_url(state: str) -> str:
    """Amazon Advertising LWA consent URL (standard OAuth authorize endpoint)."""
    params = {
        "client_id": cfg.LWA_CLIENT_ID,
        "scope": cfg.ADS_SCOPE,
        "response_type": "code",
        "redirect_uri": cfg.ads_redirect_uri,
        "state": state,
    }
    return f"{_ADS_AUTH_URL}?{urlencode(params)}"


def is_valid_spapi_app_id(app_id: str) -> bool:
    return bool(app_id) and app_id.startswith(_VALID_APP_ID_PREFIXES)


def spapi_config_warnings() -> list[str]:
    """Human-readable warnings if the SP-API OAuth config is incomplete."""
    warnings = []
    if not is_valid_spapi_app_id(cfg.SPAPI_APPLICATION_ID):
        warnings.append(
            "SPAPI_APPLICATION_ID is not set to a valid App ID "
            "(amzn1.sp.solution.<uuid> or amzn1.sellerapps.app.<uuid>). Set it from "
            "Seller Central > Develop Apps; the consent flow will not work without it."
        )
    if cfg.APP_BASE_URL.startswith("http://localhost") or cfg.APP_BASE_URL.startswith("http://127."):
        warnings.append(
            "APP_BASE_URL is localhost. Amazon OAuth requires the registered "
            "Redirect URI to match a public https URL (e.g. ngrok). Set APP_BASE_URL."
        )
    return warnings


# ------------------------------------------------------------- token exchange ---

def exchange_code_for_refresh_token(code: str, redirect_uri: str,
                                    client_id: str | None = None,
                                    client_secret: str | None = None) -> dict:
    """Exchange an authorization code for tokens.

    client_id/client_secret default to the main LWA client (used by Ads). SP-API
    passes its solution client pair, since the code is issued to that client.
    Returns the raw token response (access_token, refresh_token, expires_in...).
    """
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id or cfg.LWA_CLIENT_ID,
        "client_secret": client_secret or cfg.LWA_CLIENT_SECRET,
    }
    resp = requests.post(cfg.LWA_TOKEN_URL, data=data, timeout=30)
    if resp.status_code >= 400:
        # Surface the LWA error body (error / error_description) — it tells us
        # exactly what is wrong (invalid_client, invalid_grant, redirect mismatch…).
        raise RuntimeError(
            f"LWA token exchange {resp.status_code}: {resp.text} "
            f"(redirect_uri sent: {redirect_uri})"
        )
    return resp.json()


def get_access_token(refresh_token: str, scope: str | None = None,
                     client_id: str | None = None, client_secret: str | None = None) -> str:
    """Return a valid access token for a refresh token, caching until ~60s before expiry.

    client_id/client_secret default to the main LWA client; SP-API passes its
    own (solution) client pair so the refresh matches the issuing client.
    """
    client_id = client_id or cfg.LWA_CLIENT_ID
    client_secret = client_secret or cfg.LWA_CLIENT_SECRET
    cache_key = f"{client_id}:{scope or ''}:{refresh_token}"
    now = time.time()
    with _cache_lock:
        cached = _token_cache.get(cache_key)
        if cached and cached[1] - 60 > now:
            return cached[0]

    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    if scope:
        data["scope"] = scope
    resp = requests.post(cfg.LWA_TOKEN_URL, data=data, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"LWA refresh-token exchange {resp.status_code}: {resp.text}")
    payload = resp.json()
    access_token = payload["access_token"]
    expires_in = int(payload.get("expires_in", 3600))

    with _cache_lock:
        _token_cache[cache_key] = (access_token, now + expires_in)
    return access_token
