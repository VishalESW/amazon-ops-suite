"""Symmetric encryption for secrets stored at rest (refresh tokens).

Uses Fernet (AES-128-CBC + HMAC). The key comes from cfg.FERNET_KEY. If the key
is missing we fall back to a derived key so dev still works, but a real key
should always be set in .env.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from config import cfg


def _fernet() -> Fernet:
    key = cfg.FERNET_KEY
    if not key:
        # Derive a stable (but weak) key from the Flask secret so the app runs
        # without a configured FERNET_KEY. Not for production.
        digest = hashlib.sha256(cfg.SECRET_KEY.encode()).digest()
        key = base64.urlsafe_b64encode(digest).decode()
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(plaintext: str) -> str:
    if plaintext is None:
        return ""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return ""
