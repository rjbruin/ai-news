def test_index_ok(client):
    assert client.get("/").status_code == 200


def test_index_shows_no_emoji_logo(client):
    resp = client.get("/")
    assert b"\xf0\x9f\x93\xb0" not in resp.data  # newspaper emoji U+1F4F0
    assert b"icon-192.png" in resp.data


def test_index_shows_enabled_source_badges(client, db):
    from app.models import Source

    src = Source(type_key="seed", name="Debug Seed Data", enabled=True)
    db.session.add(src)
    db.session.commit()

    resp = client.get("/")
    assert b"Debug Seed Data" in resp.data


def test_index_sources_header_links_to_register(client):
    resp = client.get("/")
    html = resp.data.decode()
    assert '<a href="/auth/register" class="card-header' in html
    assert "Sources already being tracked" in html


def test_index_shows_admin_shared_edition_demo(client, db, admin):
    from app.models import Summary, SummaryRun

    summary = Summary(
        user_id=admin.id, name="Admin Daily", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(summary)
    db.session.commit()
    run = SummaryRun(
        summary_id=summary.id, label="Monday July 6",
        document=[{"type": "intro", "markdown": "A busy day."}],
        share_token="demo-token-123",
    )
    db.session.add(run)
    db.session.commit()

    resp = client.get("/")
    assert b"A recent edition" in resp.data
    assert b"demo-token-123" in resp.data
    assert b"Create" not in resp.data  # no podcast/PDF create dropdown leaking in


def test_index_does_not_show_non_admin_shared_edition(client, db, user):
    from app.models import Summary, SummaryRun

    summary = Summary(
        user_id=user.id, name="User Daily", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(summary)
    db.session.commit()
    run = SummaryRun(
        summary_id=summary.id, document=[{"type": "intro", "markdown": "Hi."}],
        share_token="user-token-456",
    )
    db.session.add(run)
    db.session.commit()

    resp = client.get("/")
    assert b"A recent edition" not in resp.data


def test_dashboard_requires_login(client):
    resp = client.get("/dashboard")
    assert resp.status_code == 302


def test_dashboard_shows_onboarding_once_for_new_user(auth_client, db, user):
    # auth_client's login redirect already visits /dashboard once — reset the
    # flag to simulate a genuinely fresh registration for this test.
    user.has_seen_onboarding = False
    db.session.commit()

    resp = auth_client.get("/dashboard")
    assert resp.status_code == 200
    assert b"onboarding-modal" in resp.data
    assert b"Welcome to Dispatch" in resp.data
    assert b"AI models that cost money" in resp.data

    db.session.refresh(user)
    assert user.has_seen_onboarding is True

    resp2 = auth_client.get("/dashboard")
    assert b"onboarding-modal" not in resp2.data


def test_dashboard_no_onboarding_for_already_seen_user(auth_client, db, user):
    user.has_seen_onboarding = True
    db.session.commit()

    resp = auth_client.get("/dashboard")
    assert b"onboarding-modal" not in resp.data


def test_fresh_registration_sees_onboarding_on_first_login(client, db):
    from app.models import AdminSettings, User

    AdminSettings.get().registration_open = True
    db.session.commit()

    client.post(
        "/auth/register",
        data={
            "username": "freshuser", "email": "freshuser@dispatch-users.test-domain.com",
            "password": "password123", "confirm": "password123", "submit": "Create account",
        },
        follow_redirects=True,
    )
    new_user = User.query.filter_by(email="freshuser@dispatch-users.test-domain.com").first()
    assert new_user.has_seen_onboarding is False

    resp = client.post(
        "/auth/login",
        data={"email": new_user.email, "password": "password123", "submit": "Sign in"},
        follow_redirects=True,
    )
    assert b"onboarding-modal" in resp.data


def test_changelog_shown_once_for_stale_user(auth_client, db, user, monkeypatch):
    from app import changelog
    from app.version import get_version

    monkeypatch.setattr(changelog, "ENTRIES", [
        {"version": "99.0.0", "date": "2026-01-01",
         "summary": ["User-facing summary line."], "admin_extra": ["Admin-only line."]},
    ])
    user.last_seen_version = "0.0.0"
    db.session.commit()

    resp = auth_client.get("/dashboard")
    assert resp.status_code == 200
    assert b"changelog-modal" in resp.data
    assert b"User-facing summary line." in resp.data
    assert b"Admin-only line." not in resp.data  # non-admin

    db.session.refresh(user)
    assert user.last_seen_version == get_version()

    resp2 = auth_client.get("/dashboard")
    assert b"changelog-modal" not in resp2.data


def test_changelog_shows_admin_extra_for_admin(admin_client, db, admin, monkeypatch):
    from app import changelog

    monkeypatch.setattr(changelog, "ENTRIES", [
        {"version": "99.0.0", "date": "2026-01-01",
         "summary": ["User-facing summary line."], "admin_extra": ["Admin-only line."]},
    ])
    admin.last_seen_version = "0.0.0"
    db.session.commit()

    resp = admin_client.get("/dashboard")
    assert b"User-facing summary line." in resp.data
    assert b"Admin-only line." in resp.data


def test_changelog_not_shown_when_already_caught_up(auth_client, db, user, monkeypatch):
    from app import changelog
    from app.version import get_version

    monkeypatch.setattr(changelog, "ENTRIES", [
        {"version": "99.0.0", "date": "2026-01-01",
         "summary": ["Should not appear."], "admin_extra": []},
    ])
    user.last_seen_version = get_version()
    db.session.commit()

    resp = auth_client.get("/dashboard")
    assert b"changelog-modal" not in resp.data


def test_changelog_entries_since_compares_numerically():
    from app import changelog

    monkeypatch_entries = [
        {"version": "0.9.0", "date": "d", "summary": [], "admin_extra": []},
        {"version": "0.10.0", "date": "d", "summary": [], "admin_extra": []},
    ]
    orig = changelog.ENTRIES
    try:
        changelog.ENTRIES = monkeypatch_entries
        # Lexicographic comparison would wrongly treat "0.10.0" <= "0.9.0".
        versions = [e["version"] for e in changelog.entries_since("0.9.0")]
        assert versions == ["0.10.0"]
    finally:
        changelog.ENTRIES = orig


def test_non_admin_cannot_access_admin(auth_client):
    resp = auth_client.get("/admin/")
    assert resp.status_code == 403


def test_authenticated_pages_render(auth_client, sample_tags, sample_items):
    for path in ["/dashboard", "/news", "/summaries", "/topics"]:
        assert auth_client.get(path).status_code == 200


def test_tags_redirects_to_topics(auth_client):
    resp = auth_client.get("/tags")
    assert resp.status_code == 301
    assert resp.headers["Location"].endswith("/topics")


def test_admin_pages_render(admin_client, sample_tags):
    assert admin_client.get("/admin/").status_code == 200
    assert admin_client.get("/admin/sources/new").status_code == 200


def test_topics_page_lists_global_and_own_topics(auth_client, db, user, sample_tags):
    from app.models import Tag

    mine = Tag(name="My Own Tag", scope="user", owner_user_id=user.id)
    db.session.add(mine)
    db.session.commit()

    resp = auth_client.get("/topics")
    assert resp.status_code == 200
    assert b"LLMs" in resp.data
    assert b"My Own Tag" in resp.data


def test_admin_can_promote_tag_from_topics_page(admin_client, db, admin):
    from app.models import Tag

    tag = Tag(name="Promote Me", scope="user", owner_user_id=admin.id)
    db.session.add(tag)
    db.session.commit()

    resp = admin_client.post(f"/admin/tags/{tag.id}/promote", follow_redirects=True)
    assert resp.status_code == 200
    db.session.refresh(tag)
    assert tag.scope == "global"


def test_nav_order_editions_before_news_and_admin_on_right(admin_client):
    resp = admin_client.get("/dashboard")
    html = resp.data.decode()
    assert html.index(">Editions<") < html.index(">News<")
    assert html.index(">Settings<") < html.index(">Admin<") < html.index(">Sign out<")


def test_topics_nav_link_shown_for_non_admin(auth_client):
    assert b">Topics<" in auth_client.get("/dashboard").data


def test_topics_nav_link_shown_for_admin(admin_client):
    assert b">Topics<" in admin_client.get("/dashboard").data


def test_dashboard_no_header_shows_sources_and_key_nudge(auth_client, db, user):
    from app.models import ApiKey, Source

    user.approved = True
    db.session.commit()

    resp = auth_client.get("/dashboard")
    assert resp.status_code == 200
    assert b"<h1" not in resp.data  # the page-level "Dashboard" header was removed
    assert b"No API key yet" in resp.data
    assert b"No sources enabled yet." in resp.data

    mailbox = Source(type_key="imap_newsletter", name="Mailbox", config={}, enabled=True)
    db.session.add(mailbox)
    db.session.commit()
    child = Source(
        type_key="imap_newsletter", name="TLDR AI", parent_source_id=mailbox.id,
        config={"newsletter_sender": "news@tldrnewsletter.com"},
        subscription_status="subscribed", enabled=True,
    )
    rss = Source(type_key="rss_feed", name="Import AI", config={"url": "https://x/feed"}, enabled=True)
    db.session.add_all([child, rss])
    db.session.commit()

    key = ApiKey(owner_user_id=user.id, label="Mine")
    key.set_key("sk-or-x")
    db.session.add(key)
    db.session.commit()

    resp = auth_client.get("/dashboard")
    assert b"No API key yet" not in resp.data
    assert b"tldrnewsletter.com" in resp.data
    assert b"Import AI" in resp.data
    # The mailbox connection itself isn't shown as a "source".
    assert b">Mailbox<" not in resp.data


def test_create_and_view_summary(auth_client, db, user, sample_items):
    # "New summary" creation via a form was removed (single implicit
    # agentic_page summary per user); a Summary row is created directly here,
    # same as elsewhere in the app's current (admin/seed-provisioned) flow.
    from app.models import Summary

    summary = Summary(
        user_id=user.id, name="My daily", type_key="app_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(summary)
    db.session.commit()

    # Cut an edition, then view it via the edition URL.
    from app.services import summarize
    _, _, run = summarize.build_summary(summary, record_run=True)
    assert run is not None
    resp = auth_client.get(f"/summaries/{summary.id}/editions/{run.id}")
    assert resp.status_code == 200


def test_editions_list_name_links_to_edition(auth_client, db, user, sample_items, monkeypatch):
    import json

    from app.models import Summary
    from app.services import summarize
    from tests.conftest import give_edition_key

    give_edition_key(db, user)

    state = {"n": 0}

    def _scripted_chat(messages, *, tools=None, api_key=None, model=None, **kw):
        state["n"] += 1
        if state["n"] == 1:
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "set_document", "arguments": json.dumps({"blocks": [
                    {"type": "edition_header", "title": "My Daily", "date": "Monday June 29"},
                    {"type": "intro", "markdown": "A busy day."},
                ]})},
            }], "_usage": {"total_tokens": 200, "cost": 0.001}}
        if state["n"] == 2:
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "c2", "type": "function",
                "function": {"name": "write_headlines",
                             "arguments": json.dumps({"notes": "- item"})},
            }], "_usage": {"total_tokens": 30, "cost": 0.0002}}
        return {"role": "assistant", "content": "Edition complete.",
                "_usage": {"total_tokens": 5, "cost": 0.00005}}

    monkeypatch.setattr("app.agent.runner.openrouter.chat", _scripted_chat)

    summary = Summary(
        user_id=user.id, name="My daily", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(summary)
    db.session.commit()
    _, _, run = summarize.build_summary(summary, record_run=True)
    assert run is not None

    edition_url = f"/summaries/{summary.id}/editions/{run.id}"
    list_resp = auth_client.get("/summaries")
    assert edition_url.encode() in list_resp.data


def _make_failed_run(db, summary, *, parent_run_id=None, retry_context=None):
    from app.models import SummaryRun, utcnow

    run = SummaryRun(
        summary_id=summary.id, label="Monday July 6",
        status="failed", error_message="OpenRouter HTTP 402: Insufficient credits",
        parent_run_id=parent_run_id, revision=1, retry_context=retry_context,
        range_end=utcnow(),
    )
    db.session.add(run)
    db.session.commit()
    return run


def test_failed_edition_page_shows_error_and_retry(auth_client, db, user):
    from app.models import Summary

    summary = Summary(
        user_id=user.id, name="Daily", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(summary)
    db.session.commit()
    run = _make_failed_run(db, summary)

    resp = auth_client.get(f"/summaries/{summary.id}/editions/{run.id}")
    assert resp.status_code == 200
    assert b"Failed" in resp.data
    assert b"Insufficient credits" in resp.data
    retry_url = f"/summaries/{summary.id}/editions/{run.id}/retry".encode()
    assert retry_url in resp.data


def test_editions_list_shows_failed_pill_and_retry(auth_client, db, user):
    from app.models import Summary

    summary = Summary(
        user_id=user.id, name="Daily", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(summary)
    db.session.commit()
    run = _make_failed_run(db, summary)

    resp = auth_client.get("/summaries")
    assert b"Failed" in resp.data
    retry_url = f"/summaries/{summary.id}/editions/{run.id}/retry".encode()
    assert retry_url in resp.data


def test_edition_retry_first_generation_redirects_to_generate_debug(auth_client, db, user):
    from app.models import Summary

    summary = Summary(
        user_id=user.id, name="Daily", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(summary)
    db.session.commit()
    run = _make_failed_run(db, summary)

    resp = auth_client.post(f"/summaries/{summary.id}/editions/{run.id}/retry")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/summaries/{summary.id}/generate/debug")


def test_edition_retry_revision_restashes_feedback_and_redirects(auth_client, db, user):
    from app.models import Summary

    summary = Summary(
        user_id=user.id, name="Daily", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(summary)
    db.session.commit()
    parent = _make_failed_run(db, summary)
    parent.status = "ok"
    db.session.commit()
    failed = _make_failed_run(
        db, summary, parent_run_id=parent.id,
        retry_context={"feedback": "Drop crypto coverage.", "from_scratch": True},
    )

    resp = auth_client.post(f"/summaries/{summary.id}/editions/{failed.id}/retry")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(
        f"/summaries/{summary.id}/editions/{parent.id}/feedback/debug"
    )

    with auth_client.session_transaction() as sess:
        assert sess[f"feedback_{parent.id}"] == "Drop crypto coverage."
        assert sess[f"feedback_scratch_{parent.id}"] is True


def test_edition_retry_rejects_non_failed_run(auth_client, db, user):
    from app.models import Summary, SummaryRun

    summary = Summary(
        user_id=user.id, name="Daily", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(summary)
    db.session.commit()
    run = SummaryRun(summary_id=summary.id, status="ok", content="<p>hi</p>")
    db.session.add(run)
    db.session.commit()

    resp = auth_client.post(f"/summaries/{summary.id}/editions/{run.id}/retry")
    assert resp.status_code == 400


def test_purge_empty_editions_keeps_failed_runs(db, user):
    from app import _purge_empty_editions
    from app.models import Summary, SummaryRun

    summary = Summary(
        user_id=user.id, name="Daily", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(summary)
    db.session.commit()

    failed = _make_failed_run(db, summary)
    # A genuinely empty non-failed row (e.g. a crash before this feature
    # existed) should still be purged.
    stale = SummaryRun(summary_id=summary.id, status="ok")
    db.session.add(stale)
    db.session.commit()

    failed_id, stale_id = failed.id, stale.id
    _purge_empty_editions()
    # The bulk delete inside doesn't sync the identity map, and a plain
    # expire leaves Session.get() raising ObjectDeletedError for the row
    # that's actually gone — fully detach so both checks do a clean fetch.
    db.session.expunge_all()

    assert db.session.get(SummaryRun, failed_id) is not None
    assert db.session.get(SummaryRun, stale_id) is None


def test_edition_retry_requires_ownership(auth_client, db, admin):
    from app.models import Summary

    summary = Summary(
        user_id=admin.id, name="Admin's", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(summary)
    db.session.commit()
    run = _make_failed_run(db, summary)

    resp = auth_client.post(f"/summaries/{summary.id}/editions/{run.id}/retry")
    assert resp.status_code == 403
