import pytest

from app.agent import creds
from app.crypto import decrypt, encrypt
from app.models import User


def test_crypto_roundtrip(app):
    with app.app_context():
        token = encrypt("sk-or-secret")
        assert token != "sk-or-secret"
        assert decrypt(token) == "sk-or-secret"
        assert decrypt("garbage") is None
        assert decrypt("") is None


def test_user_key_encrypted_at_rest(db, user):
    user.set_openrouter_key("sk-or-abc123")
    db.session.commit()
    # Stored value is ciphertext, not the plaintext key.
    assert user.openrouter_api_key_enc
    assert "sk-or-abc123" not in user.openrouter_api_key_enc
    assert user.get_openrouter_key() == "sk-or-abc123"
    assert user.has_openrouter_key

    user.set_openrouter_key(None)
    assert user.openrouter_api_key_enc is None
    assert user.get_openrouter_key() is None
    assert not user.has_openrouter_key


def test_creds_resolve_requires_key(db, user, app):
    with app.app_context():
        u = db.session.get(User, user.id)
        with pytest.raises(creds.MissingCredentials):
            creds.resolve(u)

        u.set_openrouter_key("sk-or-xyz")
        u.openrouter_model = "anthropic/claude-sonnet-4.6"
        db.session.commit()
        key, model = creds.resolve(u)
        assert key == "sk-or-xyz"
        assert model == "anthropic/claude-sonnet-4.6"


def test_creds_resolve_defaults_model(db, user, app):
    with app.app_context():
        u = db.session.get(User, user.id)
        u.set_openrouter_key("sk-or-xyz")
        db.session.commit()
        _, model = creds.resolve(u)
        assert model  # falls back to config OPENROUTER_MODEL


def test_settings_page_saves_key_without_echo(auth_client, db, user):
    resp = auth_client.post(
        "/settings",
        data={"openrouter_api_key": "sk-or-topsecret", "openrouter_model": "x/y"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    # The key is never rendered back to the page.
    assert b"sk-or-topsecret" not in resp.data
    refetched = db.session.get(User, user.id)
    assert refetched.get_openrouter_key() == "sk-or-topsecret"
    assert refetched.openrouter_model == "x/y"


def test_settings_blank_key_keeps_existing(auth_client, db, user):
    user.set_openrouter_key("sk-keep")
    db.session.commit()
    auth_client.post(
        "/settings",
        data={"openrouter_api_key": "", "openrouter_model": "m/n"},
        follow_redirects=True,
    )
    refetched = db.session.get(User, user.id)
    assert refetched.get_openrouter_key() == "sk-keep"  # unchanged
    assert refetched.openrouter_model == "m/n"


def test_settings_clear_key(auth_client, db, user):
    user.set_openrouter_key("sk-bye")
    db.session.commit()
    auth_client.post(
        "/settings",
        data={"clear_key": "1", "openrouter_model": "m/n"},
        follow_redirects=True,
    )
    refetched = db.session.get(User, user.id)
    assert refetched.get_openrouter_key() is None
