import pytest
from sqlalchemy.exc import IntegrityError

from app.models import ApiKey, Source, Summary, User


def _give_dispatch(db, user):
    """Sources/Topics pages and their mutation routes now require owning a
    Dispatch (dispatch_required)."""
    db.session.add(Summary(user_id=user.id, name="D", type_key="agentic_page", params={}))
    db.session.commit()


def _give_api_key(db, user, secret: str = "sk-or-x") -> ApiKey:
    """Create ``user``'s one personal ApiKey — funds both their editions and
    every Source they own."""
    key = ApiKey(owner_user_id=user.id, label="OpenRouter key")
    key.set_key(secret)
    db.session.add(key)
    db.session.commit()
    return key


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
    key = _give_api_key(db, user, "sk-or-mine")
    assert key.key_enc and "sk-or-mine" not in key.key_enc
    assert key.get_key() == "sk-or-mine"


def test_can_manage(db, user, admin):
    personal = _give_api_key(db, user)

    assert personal.can_manage(user)
    assert personal.can_manage(admin)  # admins can manage anyone's key

    other = User(username="other", email="other@example.com", email_verified=True)
    db.session.add(other)
    db.session.commit()
    assert not personal.can_manage(other)

    global_key = ApiKey.get_or_create_global()
    assert not global_key.can_manage(user)
    assert global_key.can_manage(admin)


def test_one_key_per_user_enforced(db, user):
    _give_api_key(db, user, "sk-or-first")
    dupe = ApiKey(owner_user_id=user.id, label="Second key")
    dupe.set_key("sk-or-second")
    db.session.add(dupe)
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


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

    _give_api_key(db, user, "sk-or-mine")
    _give_api_key(db, other, "sk-or-theirs")

    global_source = Source(type_key="rss_feed", name="Global", config={})
    mine_source = Source(type_key="rss_feed", name="Mine", config={}, owner_user_id=user.id)
    theirs_source = Source(type_key="rss_feed", name="Theirs", config={}, owner_user_id=other.id)
    db.session.add_all([global_source, mine_source, theirs_source])
    db.session.commit()

    assert global_source.payment_label(user) == "Included in system"
    assert mine_source.payment_label(user) == "your API key"
    assert theirs_source.payment_label(user) == "another user's API key"

    assert global_source.usage_visible_to(user) is False
    assert mine_source.usage_visible_to(user) is True
    assert theirs_source.usage_visible_to(user) is False
    # Admins don't get special visibility into another user's key either.
    assert theirs_source.usage_visible_to(admin) is False


def test_sources_page_hides_costs_except_own_key(auth_client, db, user):
    from datetime import timedelta

    from app.models import ApiKeyUsage, utcnow

    _give_dispatch(db, user)
    global_key = ApiKey.get_or_create_global()
    mine_key = _give_api_key(db, user, "sk-or-mine")

    global_source = Source(type_key="rss_feed", name="Global Feed", config={})
    mine_source = Source(type_key="rss_feed", name="Mine Feed", config={}, owner_user_id=user.id)
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
    _give_api_key(db, user)

    mine = Source(type_key="rss_feed", name="Mine", owner_user_id=user.id)
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


def test_source_new_blocks_without_api_key(auth_client, db, user):
    user.approved = True
    _give_dispatch(db, user)
    db.session.commit()

    resp = auth_client.post(
        "/sources/new",
        data={"name": "My feed", "type_key": "rss_feed", "cfg_url": "https://example.com/feed.xml"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert Source.query.filter_by(name="My feed").first() is None
    assert b"Add your API key" in resp.data


def test_approved_user_can_add_source(auth_client, db, user):
    user.approved = True
    _give_dispatch(db, user)
    _give_api_key(db, user)

    resp = auth_client.post(
        "/sources/new",
        data={
            "name": "My feed",
            "type_key": "rss_feed",
            "cfg_url": "https://example.com/feed.xml",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    source = Source.query.filter_by(name="My feed").first()
    assert source is not None
    assert source.owner_user_id == user.id


def test_api_key_save_creates_then_updates_single_row(auth_client, db, user):
    resp = auth_client.post("/keys/save", data={"secret": "sk-or-first"}, follow_redirects=True)
    assert resp.status_code == 200
    assert ApiKey.query.filter_by(owner_user_id=user.id).count() == 1
    key = ApiKey.query.filter_by(owner_user_id=user.id).first()
    assert key.get_key() == "sk-or-first"

    resp = auth_client.post("/keys/save", data={"secret": "sk-or-second"}, follow_redirects=True)
    assert resp.status_code == 200
    assert ApiKey.query.filter_by(owner_user_id=user.id).count() == 1
    db.session.refresh(key)
    assert key.get_key() == "sk-or-second"


def test_api_key_remove_deletes_key_and_disables_owned_sources(auth_client, db, user):
    user.approved = True
    key = _give_api_key(db, user)
    source = Source(type_key="rss_feed", name="Mine", owner_user_id=user.id, enabled=True)
    db.session.add(source)
    db.session.commit()

    resp = auth_client.post("/keys/remove", follow_redirects=True)
    assert resp.status_code == 200
    assert ApiKey.query.filter_by(owner_user_id=user.id).first() is None
    db.session.refresh(source)
    assert not source.enabled


def test_dispatch_settings_shows_api_key_hero_and_explainer_modal(auth_client, db, user):
    _give_dispatch(db, user)
    resp = auth_client.get("/dispatch/settings")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "API Key" in html
    assert "What are API keys?" in html
    assert "More about API keys" in html
    assert "openrouter.ai" in html
    assert "id=\"api-key-explainer\"" in html
    assert "$0.50 per edition" in html  # cost expectation blurb


def test_api_keys_redirects_to_dispatch_settings(auth_client, db, user):
    resp = auth_client.get("/keys")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/dispatch/settings#sec-api-keys")


def test_non_approved_user_with_dispatch_still_sees_api_keys_section(auth_client, db, user):
    user.approved = False
    _give_dispatch(db, user)
    db.session.commit()

    resp = auth_client.get("/dispatch/settings")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "id=\"sec-api-keys\"" in html
    assert "id=\"api-key-explainer\"" in html


def test_dispatchless_user_does_not_see_api_keys_section(auth_client, db, user):
    resp = auth_client.get("/dispatch/settings")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "id=\"sec-api-keys\"" not in html


def test_owner_can_retract_own_source_but_not_others(auth_client, db, user):
    user.approved = True
    _give_dispatch(db, user)
    _give_api_key(db, user)

    mine = Source(type_key="rss_feed", name="Mine", owner_user_id=user.id, enabled=True)
    others = Source(type_key="rss_feed", name="Others", owner_user_id=999, enabled=True)
    db.session.add_all([mine, others])
    db.session.commit()

    resp = auth_client.post(f"/sources/{mine.id}/retract", follow_redirects=True)
    assert resp.status_code == 200
    db.session.refresh(mine)
    assert not mine.enabled

    resp = auth_client.post(f"/sources/{others.id}/retract")
    assert resp.status_code == 403


def test_sources_page_no_type_column_and_privacy(auth_client, db, user):
    _give_dispatch(db, user)
    other = User(username="other4", email="other4@example.com", email_verified=True)
    db.session.add(other)
    db.session.commit()
    _give_api_key(db, other, "sk-or-theirs")
    theirs = Source(
        type_key="rss_feed", name="Their feed", owner_user_id=other.id,
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
