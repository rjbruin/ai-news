def test_index_ok(client):
    assert client.get("/").status_code == 200


def test_dashboard_requires_login(client):
    resp = client.get("/dashboard")
    assert resp.status_code == 302


def test_non_admin_cannot_access_admin(auth_client):
    resp = auth_client.get("/admin/")
    assert resp.status_code == 403


def test_authenticated_pages_render(auth_client, sample_tags, sample_items):
    for path in ["/dashboard", "/news", "/summaries"]:
        assert auth_client.get(path).status_code == 200


def test_admin_pages_render(admin_client, sample_tags):
    assert admin_client.get("/admin/").status_code == 200
    assert admin_client.get("/admin/sources/new").status_code == 200


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
