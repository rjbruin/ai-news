"""Symmetric encryption for secrets at rest (e.g. per-user API keys).

Uses Fernet (AES-128-CBC + HMAC) from the already-present ``cryptography``
package. The key is derived from a configured secret so no extra key file is
needed; set ``FERNET_SECRET`` in production for a stable, rotation-friendly key
independent of ``SECRET_KEY``.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app


def _fernet() -> Fernet:
    secret = current_app.config.get("FERNET_SECRET") or current_app.config["SECRET_KEY"]
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def encrypt(plaintext: str) -> str:
    """Encrypt a string; returns a urlsafe token."""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str | None:
    """Decrypt a token; returns None if it can't be decrypted."""
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None
