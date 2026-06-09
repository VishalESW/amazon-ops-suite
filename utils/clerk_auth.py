"""Clerk session verification + organization-domain gate for the Flask app.

Verifies the Clerk session on each request (via the official backend SDK, which
reads the `__session` cookie and validates the JWT against Clerk's JWKS), then
resolves the signed-in user's primary email and checks it belongs to the allowed
company domain (esellerworld.com). Non-company accounts are rejected.
"""

import time

import httpx
from clerk_backend_api import Clerk
from clerk_backend_api import AuthenticateRequestOptions

from config import cfg

_clerk = Clerk(bearer_auth=cfg.CLERK_SECRET_KEY) if cfg.CLERK_SECRET_KEY else None
_email_cache = {}  # user_id -> (email, expiry_epoch)


def authenticate(flask_request):
    """Return {'signed_in': bool, 'user_id': str|None, 'email': str|None}."""
    if not _clerk:
        return {"signed_in": False, "user_id": None, "email": None}
    try:
        req = httpx.Request(flask_request.method, str(flask_request.url),
                            headers=dict(flask_request.headers))
        opts = AuthenticateRequestOptions(
            authorized_parties=[cfg.APP_BASE_URL] if cfg.APP_BASE_URL else None)
        state = _clerk.authenticate_request(req, opts)
    except Exception:  # noqa: BLE001 — any failure = not authenticated
        return {"signed_in": False, "user_id": None, "email": None}

    if not getattr(state, "is_signed_in", False):
        return {"signed_in": False, "user_id": None, "email": None}

    payload = getattr(state, "payload", None) or {}
    user_id = payload.get("sub")
    return {"signed_in": True, "user_id": user_id,
            "email": _resolve_email(user_id, payload)}


def _resolve_email(user_id, payload):
    # A custom session claim is fastest if configured; otherwise look the user up.
    email = payload.get("email") or payload.get("primary_email")
    if email:
        return email
    if not user_id:
        return None
    cached = _email_cache.get(user_id)
    if cached and cached[1] > time.time():
        return cached[0]
    try:
        user = _clerk.users.get(user_id=user_id)
        primary_id = getattr(user, "primary_email_address_id", None)
        addrs = getattr(user, "email_addresses", None) or []
        email = next((a.email_address for a in addrs if a.id == primary_id), None)
        if not email and addrs:
            email = addrs[0].email_address
    except Exception:  # noqa: BLE001
        email = None
    if email:
        _email_cache[user_id] = (email, time.time() + 600)
    return email


def domain_ok(email):
    return bool(email) and email.lower().endswith("@" + cfg.AUTH_ALLOWED_DOMAIN)
