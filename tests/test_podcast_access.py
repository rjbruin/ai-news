from app.models import ApiKey, AdminSettings, Summary, SummaryRun, User


def _give_elevenlabs_key(db, user, secret="sk_el_test"):
    key = ApiKey(owner_user_id=user.id, label="ElevenLabs key", provider="elevenlabs")
    key.set_key(secret)
    db.session.add(key)
    db.session.commit()
    return key


def test_has_podcast_access_admin_always_true(db, admin):
    assert admin.podcast_enabled is False
    assert admin.has_podcast_access is True


def test_has_podcast_access_regular_user_defaults_off(db, user):
    assert user.has_podcast_access is False
    user.podcast_enabled = True
    db.session.commit()
    assert user.has_podcast_access is True


def test_has_podcast_access_self_serve_via_own_elevenlabs_key(db, user):
    assert user.has_podcast_access is False
    _give_elevenlabs_key(db, user)
    assert user.has_podcast_access is True


def test_elevenlabs_key_save_creates_then_updates_single_row(auth_client, db, user):
    resp = auth_client.post("/keys/elevenlabs/save", data={"secret": "sk_el_1"}, follow_redirects=True)
    assert resp.status_code == 200
    assert ApiKey.query.filter_by(owner_user_id=user.id, provider="elevenlabs").count() == 1
    key = ApiKey.query.filter_by(owner_user_id=user.id, provider="elevenlabs").first()
    assert key.get_key() == "sk_el_1"

    auth_client.post("/keys/elevenlabs/save", data={"secret": "sk_el_2"}, follow_redirects=True)
    assert ApiKey.query.filter_by(owner_user_id=user.id, provider="elevenlabs").count() == 1
    db.session.refresh(key)
    assert key.get_key() == "sk_el_2"


def test_elevenlabs_key_remove_deletes_key_and_disables_auto_generate(auth_client, db, user):
    _give_elevenlabs_key(db, user)
    user.podcast_auto_generate = True
    db.session.commit()

    resp = auth_client.post("/keys/elevenlabs/remove", follow_redirects=True)
    assert resp.status_code == 200
    assert user.elevenlabs_key is None
    db.session.refresh(user)
    assert user.podcast_auto_generate is False


def test_openrouter_and_elevenlabs_keys_coexist_for_same_user(db, user):
    """The unique index is now (owner, provider) — a user can have both."""
    openrouter_key = ApiKey(owner_user_id=user.id, label="OR", provider="openrouter")
    openrouter_key.set_key("sk-or-x")
    db.session.add(openrouter_key)
    db.session.commit()
    _give_elevenlabs_key(db, user)

    assert user.api_key is not None
    assert user.elevenlabs_key is not None
    assert user.api_key.id != user.elevenlabs_key.id


def test_admin_can_toggle_podcast_access(admin_client, db, user):
    assert not user.podcast_enabled
    resp = admin_client.post(f"/admin/users/{user.id}/podcast-access", follow_redirects=True)
    assert resp.status_code == 200
    db.session.refresh(user)
    assert user.podcast_enabled

    admin_client.post(f"/admin/users/{user.id}/podcast-access", follow_redirects=True)
    db.session.refresh(user)
    assert not user.podcast_enabled


def test_admin_settings_singleton(app, db):
    with app.app_context():
        row = AdminSettings.get()
        row_id = row.id
        row.elevenlabs_model = "eleven_multilingual_v2"
        db.session.commit()
        assert AdminSettings.get().id == row_id
        assert AdminSettings.get().elevenlabs_model == "eleven_multilingual_v2"


def test_admin_can_save_admin_settings(admin_client, db):
    resp = admin_client.post(
        "/admin/settings",
        data={
            "elevenlabs_voice_host_a": "voice-a",
            "elevenlabs_voice_host_b": "voice-b",
            "elevenlabs_model": "eleven_multilingual_v2",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    settings = AdminSettings.get()
    assert settings.elevenlabs_voice_host_a == "voice-a"
    assert settings.elevenlabs_voice_host_b == "voice-b"
    assert settings.elevenlabs_model == "eleven_multilingual_v2"


def test_non_admin_cannot_save_admin_settings(auth_client):
    resp = auth_client.post(
        "/admin/settings", data={"elevenlabs_voice_host_a": "x"},
    )
    assert resp.status_code == 403


def _make_edition(db, user):
    summary = Summary(
        user_id=user.id, name="My daily", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(summary)
    db.session.commit()
    run = SummaryRun(summary_id=summary.id, item_count=0, content="<p>hi</p>")
    db.session.add(run)
    db.session.commit()
    return summary, run


def test_podcast_page_requires_access(auth_client, db, user):
    summary, run = _make_edition(db, user)
    resp = auth_client.get(f"/summaries/{summary.id}/editions/{run.id}/podcast")
    assert resp.status_code == 403

    user.podcast_enabled = True
    db.session.commit()
    resp = auth_client.get(f"/summaries/{summary.id}/editions/{run.id}/podcast")
    # No ELEVENLABS_API_KEY configured in tests -> redirected with a flash, not 403.
    assert resp.status_code == 302


def test_podcast_start_requires_access(auth_client, db, user):
    summary, run = _make_edition(db, user)
    resp = auth_client.post(f"/summaries/{summary.id}/editions/{run.id}/podcast/start")
    assert resp.status_code == 403


def test_podcast_save_script_requires_access(auth_client, db, user):
    summary, run = _make_edition(db, user)
    resp = auth_client.post(
        f"/summaries/{summary.id}/editions/{run.id}/podcast/save-script",
        json={"script": "HOST A: hi"},
    )
    assert resp.status_code == 403


def test_podcast_set_auto_requires_access(auth_client, db, user):
    summary, run = _make_edition(db, user)
    resp = auth_client.post(
        f"/summaries/{summary.id}/editions/{run.id}/podcast/set-auto-generate",
        json={"enabled": True},
    )
    assert resp.status_code == 403


def test_follower_can_view_podcast_once_audio_exists_without_own_access(auth_client, db, user, admin):
    """Podcasts are free to listen to once generated — a follower doesn't
    need their own ElevenLabs access, just to be able to read the edition."""
    summary, run = _make_edition(db, admin)
    run.news_podcast_audio = "podcast_1.mp3"
    db.session.commit()
    user.follow(summary)
    db.session.commit()

    assert user.has_podcast_access is False
    resp = auth_client.get(f"/summaries/{summary.id}/editions/{run.id}/podcast")
    assert resp.status_code == 200


def test_follower_cannot_view_podcast_page_before_audio_exists(auth_client, db, user, admin):
    summary, run = _make_edition(db, admin)
    user.follow(summary)
    db.session.commit()

    resp = auth_client.get(f"/summaries/{summary.id}/editions/{run.id}/podcast")
    assert resp.status_code == 404


def test_channel_icon_shows_podcast_to_any_reader_once_audio_exists(auth_client, db, user, admin):
    summary, run = _make_edition(db, admin)
    summary.is_published = True
    run.news_podcast_audio = "podcast_1.mp3"
    db.session.commit()
    user.follow(summary)
    db.session.commit()

    resp = auth_client.get(f"/summaries/{summary.id}/editions/{run.id}")
    assert resp.status_code == 200
    assert b"Listen to podcast" in resp.data


def test_dispatch_settings_shows_elevenlabs_key_card_for_own_dispatch_without_access(auth_client, db, user):
    """The key card itself must be reachable before access exists, or
    self-serve could never bootstrap."""
    _make_edition(db, user)
    assert user.has_podcast_access is False

    resp = auth_client.get("/dispatch/settings")
    assert b'id="sec-elevenlabs-key"' in resp.data
    assert b"Podcast feed" not in resp.data


def test_subscribe_to_podcast_button_shown_once_owner_has_feed_token(auth_client, db, user, admin):
    summary, run = _make_edition(db, admin)
    summary.is_published = True
    db.session.commit()
    admin.get_or_create_feed_token()
    user.follow(summary)
    db.session.commit()

    resp = auth_client.get("/dispatches")
    assert b"Subscribe to Podcast" in resp.data


def test_dispatch_settings_page_hides_podcast_sections_without_access(auth_client, user, db):
    resp = auth_client.get("/dispatch/settings")
    assert b"Podcast feed" not in resp.data
    assert b'id="sec-podcast-format"' not in resp.data

    user.podcast_enabled = True
    db.session.commit()
    resp = auth_client.get("/dispatch/settings")
    assert b"Podcast feed" in resp.data
    assert b'id="sec-podcast-format"' in resp.data


def test_settings_page_never_shows_elevenlabs_key_form(auth_client, admin_client):
    for client in (auth_client, admin_client):
        resp = client.get("/settings")
        assert b"elevenlabs_api_key" not in resp.data
        assert b"ElevenLabs" not in resp.data
