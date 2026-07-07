from app.models import ApiKey, Source, User


def test_global_key_is_lazily_created_and_shared_by_admins(app, db):
    with app.app_context():
        key = ApiKey.get_or_create_global()
        assert key.is_global
        assert key.owner_user_id is None
        # Idempotent — a second call returns the same row.
        assert ApiKey.get_or_create_global().id == key.id


def test_global_key_reads_secret_from_env_config(app, db):
    app.config["OPENROUTER_API_KEY"] = "sk-or-global"
    with app.app_context():
        key = ApiKey.get_or_create_global()
        assert key.get_key() == "sk-or-global"
        assert key.key_enc is None  # never stored in the DB


def test_user_key_encrypted_at_rest(db, user):
    key = ApiKey(owner_user_id=user.id, label="Mine")
    key.set_key("sk-or-mine")
    db.session.add(key)
    db.session.commit()
    assert key.key_enc and "sk-or-mine" not in key.key_enc
    assert key.get_key() == "sk-or-mine"


def test_can_manage(db, user, admin):
    personal = ApiKey(owner_user_id=user.id, label="Mine")
    personal.set_key("sk-or-x")
    db.session.add(personal)
    db.session.commit()

    assert personal.can_manage(user)
    assert personal.can_manage(admin)  # admins can manage anyone's key

    other = User(username="other", email="other@example.com", email_verified=True)
    db.session.add(other)
    db.session.commit()
    assert not personal.can_manage(other)

    global_key = ApiKey.get_or_create_global()
    assert not global_key.can_manage(user)
    assert global_key.can_manage(admin)


def test_manageable_by_includes_global_only_for_admins(db, user, admin):
    assert ApiKey.manageable_by(user) == []
    admin_keys = ApiKey.manageable_by(admin)
    assert len(admin_keys) == 1
    assert admin_keys[0].is_global


def test_source_can_manage(db, user, admin):
    key = ApiKey(owner_user_id=user.id, label="Mine")
    key.set_key("sk-or-x")
    db.session.add(key)
    db.session.commit()

    mine = Source(type_key="rss_feed", name="Mine", owner_user_id=user.id, api_key_id=key.id)
    legacy = Source(type_key="rss_feed", name="Legacy", owner_user_id=None)
    db.session.add_all([mine, legacy])
    db.session.commit()

    assert mine.can_manage(user)
    assert mine.can_manage(admin)
    assert not legacy.can_manage(user)  # not the owner, and legacy has none
    assert legacy.can_manage(admin)  # admins can always manage


# ───────────────────────── web routes ─────────────────────────
def test_source_new_requires_approval(auth_client, user):
    resp = auth_client.get("/sources/new")
    assert resp.status_code == 403


def test_approved_user_can_add_source(auth_client, db, user):
    user.approved = True
    db.session.commit()
    key = ApiKey(owner_user_id=user.id, label="Mine")
    key.set_key("sk-or-x")
    db.session.add(key)
    db.session.commit()

    resp = auth_client.post(
        "/sources/new",
        data={
            "name": "My feed",
            "type_key": "rss_feed",
            "api_key_id": str(key.id),
            "cfg_url": "https://example.com/feed.xml",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    source = Source.query.filter_by(name="My feed").first()
    assert source is not None
    assert source.owner_user_id == user.id
    assert source.api_key_id == key.id


def test_unapproved_user_cannot_add_api_key(auth_client, user):
    resp = auth_client.post(
        "/keys/new", data={"label": "x", "secret": "sk-or-y"}, follow_redirects=True
    )
    assert resp.status_code == 403


def test_approved_user_can_add_and_revoke_key(auth_client, db, user):
    user.approved = True
    db.session.commit()

    auth_client.post(
        "/keys/new", data={"label": "Mine", "secret": "sk-or-y"}, follow_redirects=True
    )
    key = ApiKey.query.filter_by(owner_user_id=user.id).first()
    assert key is not None
    assert key.get_key() == "sk-or-y"

    source = Source(type_key="rss_feed", name="Mine", owner_user_id=user.id, api_key_id=key.id, enabled=True)
    db.session.add(source)
    db.session.commit()

    auth_client.post(f"/keys/{key.id}/revoke", follow_redirects=True)
    db.session.refresh(key)
    db.session.refresh(source)
    assert not key.active
    assert not source.enabled  # dependent source auto-disabled


def test_owner_can_retract_own_source_but_not_others(auth_client, db, user):
    user.approved = True
    db.session.commit()
    key = ApiKey(owner_user_id=user.id, label="Mine")
    key.set_key("sk-or-x")
    db.session.add(key)
    db.session.commit()

    mine = Source(type_key="rss_feed", name="Mine", owner_user_id=user.id, api_key_id=key.id, enabled=True)
    others = Source(type_key="rss_feed", name="Others", owner_user_id=999, enabled=True)
    db.session.add_all([mine, others])
    db.session.commit()

    resp = auth_client.post(f"/sources/{mine.id}/retract", follow_redirects=True)
    assert resp.status_code == 200
    db.session.refresh(mine)
    assert not mine.enabled

    resp = auth_client.post(f"/sources/{others.id}/retract")
    assert resp.status_code == 403


def test_admin_approve_toggle(admin_client, db, user):
    assert not user.is_approved
    resp = admin_client.post(f"/admin/users/{user.id}/approve", follow_redirects=True)
    assert resp.status_code == 200
    db.session.refresh(user)
    assert user.is_approved

    admin_client.post(f"/admin/users/{user.id}/approve", follow_redirects=True)
    db.session.refresh(user)
    assert not user.is_approved
