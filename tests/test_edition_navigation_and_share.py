"""Tests for the edition-page bundle: next-edition nav, the About box, the
share dialog's link route, quick-hit upvotes, and the dashboard's switch to
oldest-unread.
"""
from datetime import datetime, timedelta

from app.models import ApiKey, EditionRead, ItemFeedback, NewsItem, Summary, SummaryRun
from app.services import summarize


def _dispatch(db, user, name="D"):
    s = Summary(user_id=user.id, name=name, type_key="agentic_page", params={})
    db.session.add(s)
    db.session.commit()
    return s


def _item(db, title, n=[0]):
    n[0] += 1
    it = NewsItem(dedup_hash=f"nav{n[0]}", title=title, url=f"https://ex.com/{n[0]}")
    db.session.add(it)
    db.session.commit()
    return it


def _item_block(bid, item_id=None, headline="H"):
    return {"type": "item", "id": bid, "headline": headline, "subheader": "S",
            "summary": "Body text.", "item_id": item_id, "sources": []}


def _run(db, dispatch, blocks=None, days_ago=0, label="Mon", kind="edition"):
    run = SummaryRun(
        summary_id=dispatch.id, label=label, status="ok", kind=kind,
        content="<p>x</p>", document=blocks if blocks is not None else [_item_block("b1")],
        generated_at=datetime(2026, 6, 1) + timedelta(days=30 - days_ago),
    )
    db.session.add(run)
    db.session.commit()
    return run


# ── Next-edition navigation ─────────────────────────────────────────────────

def test_next_edition_is_the_newer_one(db, user):
    d = _dispatch(db, user)
    older = _run(db, d, days_ago=2, label="Older")
    middle = _run(db, d, days_ago=1, label="Middle")
    newest = _run(db, d, days_ago=0, label="Newest")

    assert summarize.next_edition(older).id == middle.id
    assert summarize.next_edition(middle).id == newest.id
    assert summarize.next_edition(newest) is None


def test_next_edition_ignores_other_kinds(db, user):
    """A review is a different artefact and shouldn't be offered as the next
    daily edition."""
    d = _dispatch(db, user)
    edition = _run(db, d, days_ago=2, label="Edition")
    _run(db, d, days_ago=1, label="Review", kind="review")
    later_edition = _run(db, d, days_ago=0, label="Later")

    assert summarize.next_edition(edition).id == later_edition.id


def _next_card(html):
    """Just the next-edition card, so read-state assertions can't accidentally
    match the current edition's own read toggle."""
    return html.split("Next edition", 1)[1].split("</a>", 1)[0]


def test_edition_page_shows_next_edition_with_read_state(auth_client, db, user):
    d = _dispatch(db, user)
    current = _run(db, d, days_ago=1, label="Today")
    nxt = _run(db, d, days_ago=0, label="Tomorrow")

    html = auth_client.get(f"/summaries/{d.id}/editions/{current.id}").data.decode()
    assert "Next edition" in html
    assert f"/summaries/{d.id}/editions/{nxt.id}" in html
    card = _next_card(html)
    assert "Unread" in card
    assert "Tomorrow" in card

    db.session.add(EditionRead(user_id=user.id, run_id=nxt.id))
    db.session.commit()
    card = _next_card(auth_client.get(f"/summaries/{d.id}/editions/{current.id}").data.decode())
    assert "Read" in card and "Unread" not in card


def test_newest_edition_has_no_next_nav(auth_client, db, user):
    d = _dispatch(db, user)
    only = _run(db, d)
    html = auth_client.get(f"/summaries/{d.id}/editions/{only.id}").data.decode()
    assert "Next edition" not in html


def test_about_this_edition_box_groups_the_meta(auth_client, db, user):
    d = _dispatch(db, user)
    run = _run(db, d)
    run.agent_cost = 0.05
    db.session.commit()
    html = auth_client.get(f"/summaries/{d.id}/editions/{run.id}").data.decode()
    assert "About this edition" in html
    assert "Give feedback to the editor" in html


# ── Share ───────────────────────────────────────────────────────────────────

def test_share_link_mints_token_for_owner(auth_client, db, user):
    d = _dispatch(db, user)
    run = _run(db, d)
    assert run.share_token is None

    resp = auth_client.post(f"/summaries/{d.id}/editions/{run.id}/share-link")
    assert resp.status_code == 200
    assert "/shared/" in resp.get_json()["url"]
    assert db.session.get(SummaryRun, run.id).share_token is not None


def test_share_link_is_stable_across_calls(auth_client, db, user):
    d = _dispatch(db, user)
    run = _run(db, d)
    first = auth_client.post(f"/summaries/{d.id}/editions/{run.id}/share-link").get_json()["url"]
    second = auth_client.post(f"/summaries/{d.id}/editions/{run.id}/share-link").get_json()["url"]
    assert first == second


def test_follower_cannot_publish_someone_elses_edition(auth_client, db, user, admin):
    """Handing out a public link is the owner's call. A follower asking for one
    on an unpublished edition is refused rather than silently publishing it."""
    other = _dispatch(db, admin, name="Theirs")
    user.follow(other)
    db.session.commit()
    run = _run(db, other)

    resp = auth_client.post(f"/summaries/{other.id}/editions/{run.id}/share-link")
    assert resp.status_code == 403
    assert db.session.get(SummaryRun, run.id).share_token is None


def test_follower_gets_existing_share_link(auth_client, db, user, admin):
    other = _dispatch(db, admin, name="Theirs")
    user.follow(other)
    run = _run(db, other)
    run.share_token = "tok-existing"
    db.session.commit()

    resp = auth_client.post(f"/summaries/{other.id}/editions/{run.id}/share-link")
    assert resp.status_code == 200
    assert "tok-existing" in resp.get_json()["url"]


def test_share_link_requires_read_access(auth_client, db, user, admin):
    other = _dispatch(db, admin, name="Theirs")
    run = _run(db, other)
    assert auth_client.post(
        f"/summaries/{other.id}/editions/{run.id}/share-link").status_code == 403


def test_item_has_anchor_id_for_shared_links(auth_client, db, user):
    d = _dispatch(db, user)
    run = _run(db, d, blocks=[_item_block("anchor1")])
    html = auth_client.get(f"/summaries/{d.id}/editions/{run.id}").data.decode()
    assert 'id="anchor1"' in html
    assert "item-share" in html


# ── Quick-hit upvotes ───────────────────────────────────────────────────────

def _more_news(bid, entries):
    return {"type": "more_news", "id": bid,
            "items": [{"headline": h, "url": "", "item_id": i} for h, i in entries]}


def test_quick_hit_upvote_renders_without_downvote(auth_client, db, user):
    d = _dispatch(db, user)
    item = _item(db, "Quick story")
    run = _run(db, d, blocks=[_more_news("mn1", [("A quick hit", item.id)])])

    html = auth_client.get(f"/summaries/{d.id}/editions/{run.id}").data.decode()
    assert 'data-block-id="mn1:0"' in html
    # The quick-hit badge is upvote-only.
    marker = html.split('data-block-id="mn1:0"')[1].split("</div>")[0]
    assert 'data-vote="1"' in marker
    assert 'data-vote="-1"' not in marker


def test_quick_hit_vote_resolves_synthesized_block_id(auth_client, db, user):
    d = _dispatch(db, user)
    item = _item(db, "Quick story")
    run = _run(db, d, blocks=[_more_news("mn1", [("first", None), ("second", item.id)])])

    resp = auth_client.post(
        f"/summaries/{d.id}/editions/{run.id}/vote",
        json={"block_id": "mn1:1", "vote": 1},
    )
    assert resp.status_code == 200
    row = ItemFeedback.query.filter_by(user_id=user.id, run_id=run.id).one()
    assert row.block_id == "mn1:1"
    assert row.item_id == item.id


def test_quick_hit_vote_rejects_out_of_range_index(auth_client, db, user):
    d = _dispatch(db, user)
    run = _run(db, d, blocks=[_more_news("mn1", [("only one", None)])])
    resp = auth_client.post(
        f"/summaries/{d.id}/editions/{run.id}/vote",
        json={"block_id": "mn1:7", "vote": 1},
    )
    assert resp.status_code == 404


# ── Dashboard: oldest unread ────────────────────────────────────────────────

def test_dashboard_features_oldest_unread(auth_client, db, user):
    d = _dispatch(db, user, name="Mine")
    user.follow(d)
    oldest = _run(db, d, days_ago=3, label="Oldest")
    _run(db, d, days_ago=2, label="Middle")
    _run(db, d, days_ago=1, label="Newest")
    db.session.commit()

    html = auth_client.get("/dashboard").data.decode()
    assert f"/summaries/{d.id}/editions/{oldest.id}" in html
    assert "2 more unread editions in this Dispatch" in html


def test_dashboard_skips_already_read(auth_client, db, user):
    d = _dispatch(db, user, name="Mine")
    user.follow(d)
    oldest = _run(db, d, days_ago=3, label="Oldest")
    middle = _run(db, d, days_ago=2, label="Middle")
    db.session.add(EditionRead(user_id=user.id, run_id=oldest.id))
    db.session.commit()

    html = auth_client.get("/dashboard").data.decode()
    assert f"/summaries/{d.id}/editions/{middle.id}" in html
    # Only one unread left, so no "more unread" line.
    assert "more unread edition" not in html


def test_dashboard_falls_back_to_newest_when_all_read(auth_client, db, user):
    d = _dispatch(db, user, name="Mine")
    user.follow(d)
    old = _run(db, d, days_ago=2, label="Old")
    newest = _run(db, d, days_ago=1, label="Newest")
    db.session.add(EditionRead(user_id=user.id, run_id=old.id))
    db.session.add(EditionRead(user_id=user.id, run_id=newest.id))
    db.session.commit()

    html = auth_client.get("/dashboard").data.decode()
    assert f"/summaries/{d.id}/editions/{newest.id}" in html
