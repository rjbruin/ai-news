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


def test_owner_display_preserves_privacy(db, user, admin):
    other = User(username="other3", email="other3@example.com", email_verified=True)
    db.session.add(other)
    db.session.commit()

    global_source = Source(type_key="rss_feed", name="Global", config={})
    mine = Source(type_key="rss_feed", name="Mine", owner_user_id=user.id, config={})
    theirs = Source(type_key="rss_feed", name="Theirs", owner_user_id=other.id, config={})
    db.session.add_all([global_source, mine, theirs])
    db.session.commit()

    assert global_source.owner_display(user) == "global"
    assert mine.owner_display(user) == "you"
    assert theirs.owner_display(user) == "other user"
    # Admins get the same privacy-preserving labels on this view too.
    assert theirs.owner_display(admin) == "other user"


def test_payment_label_and_usage_visibility(db, user, admin):
    other = User(username="other4", email="other4@example.com", email_verified=True)
    db.session.add(other)
    db.session.commit()

    global_key = ApiKey.get_or_create_global()
    mine_key = ApiKey(owner_user_id=user.id, label="Mine")
    mine_key.set_key("sk-or-mine")
    theirs_key = ApiKey(owner_user_id=other.id, label="Theirs")
    theirs_key.set_key("sk-or-theirs")
    db.session.add_all([mine_key, theirs_key])
    db.session.commit()

    global_source = Source(type_key="rss_feed", name="Global", config={}, api_key_id=global_key.id)
    mine_source = Source(type_key="rss_feed", name="Mine", config={}, api_key_id=mine_key.id)
    theirs_source = Source(type_key="rss_feed", name="Theirs", config={}, api_key_id=theirs_key.id)
    unassigned = Source(type_key="rss_feed", name="Unassigned", config={})
    db.session.add_all([global_source, mine_source, theirs_source, unassigned])
    db.session.commit()

    assert global_source.payment_label(user) == "Included in system"
    assert mine_source.payment_label(user) == "your API key"
    assert theirs_source.payment_label(user) == "another user's API key"
    assert unassigned.payment_label(user) == "none assigned"

    assert global_source.usage_visible_to(user) is False
    assert mine_source.usage_visible_to(user) is True
    assert theirs_source.usage_visible_to(user) is False
    # Admins don't get special visibility into another user's key either.
    assert theirs_source.usage_visible_to(admin) is False


def test_sources_page_hides_costs_except_own_key(auth_client, db, user):
    from datetime import timedelta

    from app.models import ApiKeyUsage, utcnow

    global_key = ApiKey.get_or_create_global()
    mine_key = ApiKey(owner_user_id=user.id, label="Mine")
    mine_key.set_key("sk-or-mine")
    db.session.add(mine_key)
    db.session.commit()

    global_source = Source(type_key="rss_feed", name="Global Feed", config={}, api_key_id=global_key.id)
    mine_source = Source(type_key="rss_feed", name="Mine Feed", config={}, api_key_id=mine_key.id)
    db.session.add_all([global_source, mine_source])
    db.session.commit()

    db.session.add(ApiKeyUsage(api_key_id=mine_key.id, source_id=mine_source.id, kind="ingest", tokens=1, cost=1.2345))
    old = ApiKeyUsage(api_key_id=mine_key.id, source_id=mine_source.id, kind="ingest", tokens=1, cost=9.0)
    db.session.add(old)
    db.session.commit()
    old.created_at = utcnow() - timedelta(days=30)
    db.session.commit()

    resp = auth_client.get("/sources")
    html = resp.data.decode()
    assert "Payment" in html
    assert "Usage" not in html  # old column header removed
    assert "Included in system" in html
    assert "your API key" in html
    assert "$10.23 total" in html  # 1.2345 + 9.0 rounded
    assert "$1.23 in the last week" in html  # only the recent row


def test_type_label_uses_plugin_label(app, db):
    with app.app_context():
        rss = Source(type_key="rss_feed", name="Feed", config={})
        db.session.add(rss)
        db.session.commit()
        assert "RSS" in rss.type_label or "Atom" in rss.type_label

        unknown = Source(type_key="totally_unknown", name="?", config={})
        db.session.add(unknown)
        db.session.commit()
        assert unknown.type_label == "totally_unknown"


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


def test_payment_page_shows_hero_and_explainer_modal(auth_client, db, user):
    user.approved = True
    db.session.commit()

    resp = auth_client.get("/keys")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "Payment" in html
    assert "What are API keys?" in html
    assert "More about API keys" in html
    assert "openrouter.ai" in html
    assert "id=\"api-key-explainer\"" in html
    assert "a few cents" in html  # cost expectation blurb in "Add a key"


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


def test_use_for_editions(auth_client, db, user):
    user.approved = True
    db.session.commit()
    key = ApiKey(owner_user_id=user.id, label="Mine")
    key.set_key("sk-or-x")
    db.session.add(key)
    db.session.commit()

    resp = auth_client.post(f"/keys/{key.id}/use-for-editions", follow_redirects=True)
    assert resp.status_code == 200
    db.session.refresh(user)
    assert user.edition_api_key_id == key.id


def test_use_for_editions_rejects_global_key(auth_client, db, user):
    user.approved = True
    db.session.commit()
    global_key = ApiKey.get_or_create_global()

    resp = auth_client.post(f"/keys/{global_key.id}/use-for-editions", follow_redirects=True)
    assert resp.status_code == 200
    db.session.refresh(user)
    assert user.edition_api_key_id is None


def test_use_for_editions_rejects_other_users_key(auth_client, db, user):
    user.approved = True
    db.session.commit()
    other = User(username="other2", email="other2@example.com", email_verified=True)
    db.session.add(other)
    db.session.commit()
    other_key = ApiKey(owner_user_id=other.id, label="Not yours")
    other_key.set_key("sk-or-x")
    db.session.add(other_key)
    db.session.commit()

    resp = auth_client.post(f"/keys/{other_key.id}/use-for-editions", follow_redirects=True)
    assert resp.status_code == 200
    db.session.refresh(user)
    assert user.edition_api_key_id is None


def test_deleting_edition_key_clears_selection(auth_client, db, user):
    user.approved = True
    db.session.commit()
    key = ApiKey(owner_user_id=user.id, label="Mine")
    key.set_key("sk-or-x")
    db.session.add(key)
    db.session.commit()
    user.edition_api_key_id = key.id
    db.session.commit()

    auth_client.post(f"/keys/{key.id}/delete", follow_redirects=True)
    db.session.refresh(user)
    assert user.edition_api_key_id is None


def test_sources_page_no_type_column_and_privacy(auth_client, db, user):
    other = User(username="other4", email="other4@example.com", email_verified=True)
    db.session.add(other)
    db.session.commit()
    key = ApiKey(owner_user_id=other.id, label="Theirs")
    key.set_key("sk-or-x")
    db.session.add(key)
    db.session.commit()
    theirs = Source(
        type_key="rss_feed", name="Their feed", owner_user_id=other.id, api_key_id=key.id,
        config={}, enabled=True, last_status="2 new items (2 checked)",
    )
    db.session.add(theirs)
    db.session.commit()

    resp = auth_client.get("/sources")
    assert resp.status_code == 200
    assert b"<th>Type</th>" not in resp.data
    assert b"other user" in resp.data
    assert other.username.encode() not in resp.data  # never leak the identity
    assert b"RSS" in resp.data or b"Atom" in resp.data  # type now shown as a badge


def test_admin_approve_toggle(admin_client, db, user):
    assert not user.is_approved
    resp = admin_client.post(f"/admin/users/{user.id}/approve", follow_redirects=True)
    assert resp.status_code == 200
    db.session.refresh(user)
    assert user.is_approved

    admin_client.post(f"/admin/users/{user.id}/approve", follow_redirects=True)
    db.session.refresh(user)
    assert not user.is_approved
