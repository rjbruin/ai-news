"""Regression tests for two button/route gating mismatches.

Both bugs had the same shape: a template showed (or hid) a button based on a
flag different from the one the route actually enforced, so a legitimate user
either saw a dead link (-> 403) or never saw a working one.
"""
from app.models import ApiKey, Summary, SummaryRun


def _give_dispatch(db, user):
    s = Summary(user_id=user.id, name="D", type_key="agentic_page", params={})
    db.session.add(s)
    db.session.commit()
    return s


def _give_api_key(db, user, secret="sk-or-x"):
    key = ApiKey(owner_user_id=user.id, label="OpenRouter key")
    key.set_key(secret)
    db.session.add(key)
    db.session.commit()
    return key


def _give_elevenlabs_key(db, user, secret="sk_el_test"):
    key = ApiKey(owner_user_id=user.id, label="ElevenLabs key", provider="elevenlabs")
    key.set_key(secret)
    db.session.add(key)
    db.session.commit()
    return key


# ── "Add source" button must match dispatch_required, not is_approved ──────

def test_add_source_button_shown_for_dispatch_owner_without_approval(auth_client, db, user):
    """The route (@dispatch_required) only checks own_dispatch/admin — a
    self-serve Dispatch owner an admin never explicitly approved must still
    see the button, not just be able to reach the URL directly."""
    _give_dispatch(db, user)
    assert user.approved is False

    html = auth_client.get("/sources").data.decode()
    assert 'href="/sources/new"' in html


def test_add_source_button_hidden_without_a_dispatch(auth_client, db, user):
    html = auth_client.get("/sources").data.decode()
    assert 'href="/sources/new"' not in html


def test_add_source_button_shown_for_admin(admin_client, db, admin):
    html = admin_client.get("/sources").data.decode()
    assert 'href="/sources/new"' in html


def test_dispatch_owner_without_approval_can_actually_add_a_source(auth_client, db, user):
    """Not just the button — the route itself never checked approval; confirm
    that stays true so the button isn't lying."""
    _give_dispatch(db, user)
    _give_api_key(db, user)
    assert user.approved is False

    resp = auth_client.post(
        "/sources/new",
        data={"name": "My feed", "type_key": "rss_feed", "cfg_url": "https://example.com/feed.xml"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    from app.models import Source
    assert Source.query.filter_by(name="My feed").first() is not None


# ── Podcast menu item must match has_podcast_access, not just is_own ───────

def test_podcast_menu_item_hidden_without_access(auth_client, db, user):
    dispatch = _give_dispatch(db, user)
    run = SummaryRun(summary_id=dispatch.id, label="Mon", status="ok", content="<p>x</p>")
    db.session.add(run)
    db.session.commit()
    assert user.has_podcast_access is False

    html = auth_client.get(f"/summaries/{dispatch.id}/editions/{run.id}").data.decode()
    assert f'href="/summaries/{dispatch.id}/editions/{run.id}/podcast"' not in html
    assert "add a key first" in html


def test_podcast_menu_item_shown_with_access(auth_client, db, user):
    dispatch = _give_dispatch(db, user)
    run = SummaryRun(summary_id=dispatch.id, label="Mon", status="ok", content="<p>x</p>")
    db.session.add(run)
    db.session.commit()
    _give_elevenlabs_key(db, user)

    html = auth_client.get(f"/summaries/{dispatch.id}/editions/{run.id}").data.decode()
    assert f'href="/summaries/{dispatch.id}/editions/{run.id}/podcast"' in html


def test_owner_can_still_view_existing_podcast_after_losing_access(auth_client, db, user):
    """The route used to 403 an is_own visitor purely on has_podcast_access,
    even when their own podcast already existed — e.g. after they removed
    their ElevenLabs key. Viewing an edition you already generated audio for
    must never depend on still having generation access."""
    dispatch = _give_dispatch(db, user)
    run = SummaryRun(
        summary_id=dispatch.id, label="Mon", status="ok", content="<p>x</p>",
        news_podcast_audio="podcast_1.mp3",
    )
    db.session.add(run)
    db.session.commit()
    assert user.has_podcast_access is False

    resp = auth_client.get(f"/summaries/{dispatch.id}/editions/{run.id}/podcast")
    assert resp.status_code == 200


def test_owner_with_zero_access_still_403s_the_page(auth_client, db, user):
    """No elevenlabs_key, no podcast_enabled grant, not admin — genuinely no
    access at all, and no existing audio to fall back to viewing. This is
    exactly the case the menu-item fix hides the link for; navigating there
    directly still 403s, unchanged from before."""
    dispatch = _give_dispatch(db, user)
    run = SummaryRun(summary_id=dispatch.id, label="Mon", status="ok", content="<p>x</p>")
    db.session.add(run)
    db.session.commit()
    assert user.has_podcast_access is False

    resp = auth_client.get(f"/summaries/{dispatch.id}/editions/{run.id}/podcast")
    assert resp.status_code == 403

    resp = auth_client.post(f"/summaries/{dispatch.id}/editions/{run.id}/podcast/start")
    assert resp.status_code == 403


def test_owner_with_access_but_no_key_yet_gets_a_helpful_redirect(auth_client, db, user):
    """has_podcast_access can be true via an admin-granted podcast_enabled
    flag alone, with no ElevenLabs key yet — that's the "flash + redirect to
    add a key" case, distinct from the flat 403 above."""
    dispatch = _give_dispatch(db, user)
    run = SummaryRun(summary_id=dispatch.id, label="Mon", status="ok", content="<p>x</p>")
    db.session.add(run)
    user.podcast_enabled = True
    db.session.commit()
    assert user.has_podcast_access is True

    resp = auth_client.get(f"/summaries/{dispatch.id}/editions/{run.id}/podcast")
    assert resp.status_code == 302
    assert "sec-elevenlabs-key" in resp.headers["Location"]


def test_generate_script_button_hidden_without_access_even_though_page_now_loads(
    auth_client, db, user,
):
    """Regression for the page becoming viewable: the podcast page must not
    show a "Generate" button that would just 403 on click."""
    dispatch = _give_dispatch(db, user)
    run = SummaryRun(
        summary_id=dispatch.id, label="Mon", status="ok", content="<p>x</p>",
        news_podcast_audio="podcast_1.mp3",
    )
    db.session.add(run)
    db.session.commit()

    html = auth_client.get(f"/summaries/{dispatch.id}/editions/{run.id}/podcast").data.decode()
    assert '<button id="btn-generate-script"' not in html
    assert 'id="auto-toggle"' not in html
    assert '<button class="btn btn-outline-secondary" id="btn-regenerate-audio">' not in html
    # The JS must not unconditionally bind to an element that isn't rendered —
    # it degrades to a defensive lookup instead (see the `on()` helper).
    assert "function on(id, event, handler)" in html
    assert "if (IS_OWN && HAS_ACCESS)" in html
