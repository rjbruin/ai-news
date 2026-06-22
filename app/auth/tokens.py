"""Signed, single-use tokens for magic-link login and email verification."""
from __future__ import annotations

import hashlib
from datetime import timedelta

from flask import current_app
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from ..extensions import db
from ..models import AuthToken, User, utcnow

DEFAULT_MAX_AGE = 60 * 30  # 30 minutes


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="auth-link")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate(user: User, purpose: str, max_age: int = DEFAULT_MAX_AGE) -> str:
    """Create a signed token and record it (single-use) in the DB."""
    token = _serializer().dumps({"uid": user.id, "purpose": purpose})
    record = AuthToken(
        user_id=user.id,
        purpose=purpose,
        token_hash=_hash(token),
        expires_at=utcnow() + timedelta(seconds=max_age),
    )
    db.session.add(record)
    db.session.commit()
    return token


def verify(token: str, purpose: str, max_age: int = DEFAULT_MAX_AGE) -> User | None:
    """Validate a token's signature, purpose, expiry and single-use status."""
    try:
        data = _serializer().loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    if data.get("purpose") != purpose:
        return None

    record = (
        AuthToken.query.filter_by(token_hash=_hash(token), purpose=purpose, used=False)
        .order_by(AuthToken.id.desc())
        .first()
    )
    if record is None:
        return None
    record.used = True
    db.session.commit()
    return db.session.get(User, data.get("uid"))
