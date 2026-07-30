"""Tests for admin-configurable global ingest API key (get_key() precedence
and the save/remove admin routes)."""
import pytest

from app.models import ApiKey


def test_get_key_prefers_key_enc_over_env_var(app, db):
    key = ApiKey.get_or_create_global()
    key.set_key("db-stored-secret")
    db.session.commit()
    app.config["OPENROUTER_API_KEY"] = "env-secret"
    assert key.get_key() == "db-stored-secret"


def test_get_key_falls_back_to_env_var_when_unset(app, db):
    key = ApiKey.get_or_create_global()
    assert key.key_enc is None
    app.config["OPENROUTER_API_KEY"] = "env-secret"
    assert key.get_key() == "env-secret"


def test_get_key_none_when_neither_set(app, db):
    key = ApiKey.get_or_create_global()
    app.config["OPENROUTER_API_KEY"] = None
    assert key.get_key() is None


def test_save_global_api_key_requires_admin(auth_client, db):
    resp = auth_client.post("/admin/settings/global-api-key/save", data={"secret": "sk-or-x"})
    assert resp.status_code in (302, 403)
    key = ApiKey.get_or_create_global()
    assert key.key_enc is None


def test_save_global_api_key_creates_then_updates_singleton(admin_client, db):
    resp = admin_client.post(
        "/admin/settings/global-api-key/save", data={"secret": "first-secret"}, follow_redirects=True
    )
    assert resp.status_code == 200
    key = ApiKey.get_or_create_global()
    assert key.get_key() == "first-secret"

    resp = admin_client.post(
        "/admin/settings/global-api-key/save", data={"secret": "second-secret"}, follow_redirects=True
    )
    assert resp.status_code == 200
    assert ApiKey.query.filter_by(is_global=True).count() == 1
    key = ApiKey.get_or_create_global()
    assert key.get_key() == "second-secret"


def test_save_global_api_key_rejects_blank_secret(admin_client, db):
    resp = admin_client.post(
        "/admin/settings/global-api-key/save", data={"secret": "  "}, follow_redirects=True
    )
    assert resp.status_code == 200
    key = ApiKey.get_or_create_global()
    assert key.key_enc is None


def test_remove_global_api_key_requires_admin(auth_client, db):
    key = ApiKey.get_or_create_global()
    key.set_key("secret")
    db.session.commit()

    resp = auth_client.post("/admin/settings/global-api-key/remove")
    assert resp.status_code in (302, 403)
    assert ApiKey.get_or_create_global().key_enc is not None


def test_remove_global_api_key_clears_without_deleting_row(admin_client, db, app):
    key = ApiKey.get_or_create_global()
    key.set_key("secret")
    db.session.commit()
    key_id = key.id

    app.config["OPENROUTER_API_KEY"] = "env-fallback"
    resp = admin_client.post("/admin/settings/global-api-key/remove", follow_redirects=True)
    assert resp.status_code == 200

    key = ApiKey.get_or_create_global()
    assert key.id == key_id
    assert key.key_enc is None
    assert key.get_key() == "env-fallback"
