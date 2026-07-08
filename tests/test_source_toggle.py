from app.models import NewsItem, Source, UserDisabledSource
from app.services.summarize import items_in_window


def _source(db, name="Feed A"):
    s = Source(type_key="rss_feed", name=name, config={"url": f"https://x/{name}"}, enabled=True)
    db.session.add(s)
    db.session.commit()
    return s


def _item(db, source, title):
    item = NewsItem(
        dedup_hash=NewsItem.make_hash(title, f"http://x/{title}"),
        title=title, url=f"http://x/{title}", source_id=source.id,
    )
    db.session.add(item)
    db.session.commit()
    return item


def test_items_in_window_without_user_includes_everything(db):
    a, b = _source(db, "A"), _source(db, "B")
    _item(db, a, "From A")
    _item(db, b, "From B")

    items = items_in_window(None, None)
    assert {it.title for it in items} == {"From A", "From B"}


def test_items_in_window_excludes_users_disabled_source(db, user):
    a, b = _source(db, "A"), _source(db, "B")
    _item(db, a, "From A")
    _item(db, b, "From B")
    db.session.add(UserDisabledSource(user_id=user.id, source_id=b.id))
    db.session.commit()

    items = items_in_window(None, None, user=user)
    assert {it.title for it in items} == {"From A"}


def test_items_in_window_disabled_source_only_affects_that_user(db, user, admin):
    a, b = _source(db, "A"), _source(db, "B")
    _item(db, a, "From A")
    _item(db, b, "From B")
    db.session.add(UserDisabledSource(user_id=user.id, source_id=b.id))
    db.session.commit()

    assert {it.title for it in items_in_window(None, None, user=user)} == {"From A"}
    assert {it.title for it in items_in_window(None, None, user=admin)} == {"From A", "From B"}


def test_source_toggle_mine_route_creates_and_removes_row(auth_client, db, user):
    source = _source(db)
    assert UserDisabledSource.query.filter_by(user_id=user.id, source_id=source.id).first() is None

    resp = auth_client.post(f"/sources/{source.id}/toggle-mine", follow_redirects=True)
    assert resp.status_code == 200
    assert UserDisabledSource.query.filter_by(user_id=user.id, source_id=source.id).first() is not None

    auth_client.post(f"/sources/{source.id}/toggle-mine", follow_redirects=True)
    assert UserDisabledSource.query.filter_by(user_id=user.id, source_id=source.id).first() is None


def test_sources_page_shows_toggle_and_state(auth_client, db, user):
    source = _source(db)
    resp = auth_client.get("/sources")
    assert b"On for me" in resp.data

    db.session.add(UserDisabledSource(user_id=user.id, source_id=source.id))
    db.session.commit()
    resp = auth_client.get("/sources")
    assert b"Off for me" in resp.data


def test_sources_page_hides_toggle_for_mailbox_itself(auth_client, db):
    mailbox = Source(type_key="imap_newsletter", name="Mailbox", config={}, enabled=True)
    db.session.add(mailbox)
    db.session.commit()

    resp = auth_client.get("/sources")
    # The mailbox connection produces no items itself, only its per-newsletter
    # children do — no point toggling it.
    html = resp.data.decode()
    mailbox_row_start = html.index("Mailbox")
    mailbox_row_end = html.index("</tr>", mailbox_row_start)
    assert "On for me" not in html[mailbox_row_start:mailbox_row_end]
