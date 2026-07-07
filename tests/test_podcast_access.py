from app.models import AdminSettings, Summary, SummaryRun, User


def test_has_podcast_access_admin_always_true(db, admin):
    assert admin.podcast_enabled is False
    assert admin.has_podcast_access is True


def test_has_podcast_access_regular_user_defaults_off(db, user):
    assert user.has_podcast_access is False
    user.podcast_enabled = True
    db.session.commit()
    assert user.has_podcast_access is True


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


def test_settings_page_hides_podcast_sections_without_access(auth_client, user, db):
    resp = auth_client.get("/settings")
    assert b"Podcast feed" not in resp.data
    assert b'id="sec-podcast-format"' not in resp.data

    user.podcast_enabled = True
    db.session.commit()
    resp = auth_client.get("/settings")
    assert b"Podcast feed" in resp.data
    assert b'id="sec-podcast-format"' in resp.data


def test_settings_page_never_shows_elevenlabs_key_form(auth_client, admin_client):
    for client in (auth_client, admin_client):
        resp = client.get("/settings")
        assert b"elevenlabs_api_key" not in resp.data
        assert b"ElevenLabs" not in resp.data
