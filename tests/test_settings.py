import pytest

from app.agent import creds
from app.crypto import decrypt, encrypt
from app.models import User, utcnow

from conftest import give_edition_key


def test_crypto_roundtrip(app):
    with app.app_context():
        token = encrypt("sk-or-secret")
        assert token != "sk-or-secret"
        assert decrypt(token) == "sk-or-secret"
        assert decrypt("garbage") is None
        assert decrypt("") is None


def test_creds_resolve_requires_key(db, user, app):
    with app.app_context():
        u = db.session.get(User, user.id)
        with pytest.raises(creds.MissingCredentials):
            creds.resolve(u)

        give_edition_key(db, u, "sk-or-xyz", "anthropic/claude-sonnet-4.6")
        key, model = creds.resolve(u)
        assert key == "sk-or-xyz"
        assert model == "anthropic/claude-sonnet-4.6"


def test_creds_resolve_defaults_model(db, user, app):
    with app.app_context():
        u = db.session.get(User, user.id)
        give_edition_key(db, u, "sk-or-xyz")
        _, model = creds.resolve(u)
        assert model  # falls back to config OPENROUTER_MODEL


def test_creds_resolve_rejects_revoked_key(db, user, app):
    with app.app_context():
        u = db.session.get(User, user.id)
        key = give_edition_key(db, u, "sk-or-xyz")
        key.revoked_at = utcnow()
        db.session.commit()
        with pytest.raises(creds.MissingCredentials):
            creds.resolve(u)
