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

        give_edition_key(db, u, "sk-or-xyz")
        key, model = creds.resolve(u)
        assert key == "sk-or-xyz"
        assert model  # falls back to config OPENROUTER_MODEL


def test_creds_resolve_uses_summary_model_setting(db, user, app):
    from app.models import Summary

    with app.app_context():
        u = db.session.get(User, user.id)
        give_edition_key(db, u, "sk-or-xyz")
        s = Summary(
            user_id=u.id, name="Daily", type_key="agentic_page",
            scope_mode="fixed_period", period="day",
            params={"model": "anthropic/claude-sonnet-4.6"},
        )
        db.session.add(s)
        db.session.commit()

        key, model = creds.resolve(u, summary=s)
        assert key == "sk-or-xyz"
        assert model == "anthropic/claude-sonnet-4.6"


def test_creds_resolve_defaults_model(db, user, app):
    with app.app_context():
        u = db.session.get(User, user.id)
        give_edition_key(db, u, "sk-or-xyz")
        _, model = creds.resolve(u)
        assert model  # falls back to config OPENROUTER_MODEL


def test_creds_resolve_blank_summary_model_falls_back(db, user, app):
    from app.models import Summary

    with app.app_context():
        u = db.session.get(User, user.id)
        give_edition_key(db, u, "sk-or-xyz")
        s = Summary(
            user_id=u.id, name="Daily", type_key="agentic_page",
            scope_mode="fixed_period", period="day",
            params={"model": ""},
        )
        db.session.add(s)
        db.session.commit()

        _, model = creds.resolve(u, summary=s)
        assert model  # blank param falls back to config OPENROUTER_MODEL, not ""


def test_creds_resolve_rejects_revoked_key(db, user, app):
    with app.app_context():
        u = db.session.get(User, user.id)
        key = give_edition_key(db, u, "sk-or-xyz")
        key.revoked_at = utcnow()
        db.session.commit()
        with pytest.raises(creds.MissingCredentials):
            creds.resolve(u)
