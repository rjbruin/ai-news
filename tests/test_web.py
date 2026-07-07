def test_index_ok(client):
    assert client.get("/").status_code == 200


def test_dashboard_requires_login(client):
    resp = client.get("/dashboard")
    assert resp.status_code == 302


def test_non_admin_cannot_access_admin(auth_client):
    resp = auth_client.get("/admin/")
    assert resp.status_code == 403


def test_authenticated_pages_render(auth_client, sample_tags, sample_items):
    for path in ["/dashboard", "/news", "/summaries", "/tags"]:
        assert auth_client.get(path).status_code == 200


def test_admin_pages_render(admin_client, sample_tags):
    assert admin_client.get("/admin/").status_code == 200
    assert admin_client.get("/admin/sources/new").status_code == 200


def test_tags_page_lists_global_and_own_tags(auth_client, db, user, sample_tags):
    from app.models import Tag

    mine = Tag(name="My Own Tag", scope="user", owner_user_id=user.id)
    db.session.add(mine)
    db.session.commit()

    resp = auth_client.get("/tags")
    assert resp.status_code == 200
    assert b"LLMs" in resp.data
    assert b"My Own Tag" in resp.data


def test_admin_can_promote_tag_from_tags_page(admin_client, db, admin):
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


def test_tags_nav_link_hidden_for_non_admin(auth_client):
    assert b">Tags<" not in auth_client.get("/dashboard").data


def test_tags_nav_link_shown_for_admin(admin_client):
    assert b">Tags<" in admin_client.get("/dashboard").data


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
