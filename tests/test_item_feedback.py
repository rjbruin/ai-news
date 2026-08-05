"""Tests for per-item reader feedback: the vote badge/route, and the two
signals derived from votes (topic-tier suggestions, per-item reader_signal).
"""
import pytest

from app.models import ItemFeedback, NewsItem, NewsItemTag, Summary, SummaryRun, Tag
from app.services import reader_feedback


def _dispatch(db, user, name="D"):
    s = Summary(user_id=user.id, name=name, type_key="agentic_page", params={})
    db.session.add(s)
    db.session.commit()
    return s


def _item(db, title, summary_text="", n=[0]):
    n[0] += 1
    it = NewsItem(
        dedup_hash=f"h{n[0]}", title=title, summary_text=summary_text,
        url=f"https://example.com/{n[0]}",
    )
    db.session.add(it)
    db.session.commit()
    return it


def _run(db, dispatch, blocks):
    run = SummaryRun(
        summary_id=dispatch.id, label="Mon", status="ok",
        content="<p>x</p>", document=blocks,
    )
    db.session.add(run)
    db.session.commit()
    return run


def _item_block(block_id, item_id=None):
    return {
        "type": "item", "id": block_id, "headline": "H", "subheader": "S",
        "summary": "text", "item_id": item_id, "sources": [],
    }


# ── The vote route ──────────────────────────────────────────────────────────

def test_vote_records_and_toggles(auth_client, db, user):
    dispatch = _dispatch(db, user)
    item = _item(db, "A story")
    run = _run(db, dispatch, [_item_block("b1", item.id)])
    url = f"/summaries/{dispatch.id}/editions/{run.id}/vote"

    resp = auth_client.post(url, json={"block_id": "b1", "vote": 1})
    assert resp.status_code == 200
    assert resp.get_json()["vote"] == 1
    row = ItemFeedback.query.filter_by(user_id=user.id, run_id=run.id).one()
    assert row.vote == 1
    # item_id comes from the stored document, not the client.
    assert row.item_id == item.id

    # Same vote again clears it — the badge is a toggle.
    resp = auth_client.post(url, json={"block_id": "b1", "vote": 1})
    assert resp.get_json()["vote"] is None
    assert ItemFeedback.query.filter_by(user_id=user.id, run_id=run.id).count() == 0


def test_vote_flips_without_duplicating(auth_client, db, user):
    dispatch = _dispatch(db, user)
    run = _run(db, dispatch, [_item_block("b1", None)])
    url = f"/summaries/{dispatch.id}/editions/{run.id}/vote"

    auth_client.post(url, json={"block_id": "b1", "vote": 1})
    resp = auth_client.post(url, json={"block_id": "b1", "vote": -1})
    assert resp.get_json()["vote"] == -1
    row = ItemFeedback.query.filter_by(user_id=user.id, run_id=run.id).one()
    assert row.vote == -1


def test_vote_rejects_unknown_block(auth_client, db, user):
    dispatch = _dispatch(db, user)
    run = _run(db, dispatch, [_item_block("b1", None)])
    resp = auth_client.post(
        f"/summaries/{dispatch.id}/editions/{run.id}/vote",
        json={"block_id": "nope", "vote": 1},
    )
    assert resp.status_code == 404


def test_vote_rejects_bad_payload(auth_client, db, user):
    dispatch = _dispatch(db, user)
    run = _run(db, dispatch, [_item_block("b1", None)])
    url = f"/summaries/{dispatch.id}/editions/{run.id}/vote"
    assert auth_client.post(url, json={"vote": 1}).status_code == 400
    assert auth_client.post(url, json={"block_id": "b1", "vote": 5}).status_code == 400


def test_vote_requires_read_access(auth_client, db, user, admin):
    """A non-follower can't vote on someone else's Dispatch."""
    other = _dispatch(db, admin, name="Theirs")
    run = _run(db, other, [_item_block("b1", None)])
    resp = auth_client.post(
        f"/summaries/{other.id}/editions/{run.id}/vote",
        json={"block_id": "b1", "vote": 1},
    )
    assert resp.status_code == 403


def test_follower_may_vote(auth_client, db, user, admin):
    """Followers' votes are recorded — they're a real engagement signal, even
    though only the owner's steer generation."""
    other = _dispatch(db, admin, name="Theirs")
    user.follow(other)
    db.session.commit()
    run = _run(db, other, [_item_block("b1", None)])

    resp = auth_client.post(
        f"/summaries/{other.id}/editions/{run.id}/vote",
        json={"block_id": "b1", "vote": -1},
    )
    assert resp.status_code == 200
    assert ItemFeedback.query.filter_by(user_id=user.id).one().vote == -1


# ── Badge rendering ─────────────────────────────────────────────────────────

def test_edition_page_renders_vote_badge(auth_client, db, user):
    dispatch = _dispatch(db, user)
    run = _run(db, dispatch, [_item_block("b1", None)])
    html = auth_client.get(f"/summaries/{dispatch.id}/editions/{run.id}").data.decode()
    assert 'class="item-vote"' in html
    assert 'data-block-id="b1"' in html


def test_cast_vote_renders_active(auth_client, db, user):
    dispatch = _dispatch(db, user)
    run = _run(db, dispatch, [_item_block("b1", None)])
    auth_client.post(
        f"/summaries/{dispatch.id}/editions/{run.id}/vote",
        json={"block_id": "b1", "vote": 1},
    )
    html = auth_client.get(f"/summaries/{dispatch.id}/editions/{run.id}").data.decode()
    assert 'data-vote="1"' in html
    assert "is-active" in html


def test_shared_view_has_no_vote_badge(client, db, user):
    """The shared link is anonymous — there'd be nobody to attribute a vote to."""
    dispatch = _dispatch(db, user)
    run = _run(db, dispatch, [_item_block("b1", None)])
    run.share_token = "tok123"
    db.session.commit()

    html = client.get("/shared/tok123").data.decode()
    assert "item-vote" not in html


def test_pdf_and_email_render_paths_have_no_badge(app, db, user):
    """render_blocks defaults votable=false, so the PDF/print template and the
    stored (emailed) HTML never carry non-functional vote buttons."""
    from flask import render_template
    dispatch = _dispatch(db, user)
    run = _run(db, dispatch, [_item_block("b1", None)])

    with app.test_request_context():
        printed = render_template(
            "summaries/print.html", summary=dispatch, run=run,
            is_agentic=True, font_scale=80,
        )
        stored = render_template("summaries/agentic_page.html", blocks=run.document)
    assert "item-vote" not in printed
    assert "item-vote" not in stored


# ── Layer 1: topic suggestions ──────────────────────────────────────────────

def _vote_on_tagged_items(db, user, dispatch, tag, votes):
    """Create one item per vote, tag it, and record the vote."""
    blocks, run_items = [], []
    for i, v in enumerate(votes):
        item = _item(db, f"{tag.name} story {i}", "body text")
        db.session.add(NewsItemTag(news_item_id=item.id, tag_id=tag.id, method="llm"))
        blocks.append(_item_block(f"b{tag.id}_{i}", item.id))
        run_items.append((f"b{tag.id}_{i}", item, v))
    run = _run(db, dispatch, blocks)
    for block_id, item, v in run_items:
        db.session.add(ItemFeedback(
            user_id=user.id, run_id=run.id, block_id=block_id,
            item_id=item.id, vote=v,
        ))
    db.session.commit()
    return run


def test_topic_suggestion_from_lopsided_downvotes(db, user):
    dispatch = _dispatch(db, user)
    tag = Tag(name="Funding", scope="global")
    db.session.add(tag)
    db.session.commit()
    _vote_on_tagged_items(db, user, dispatch, tag, [-1, -1, -1, -1, 1])

    suggestions = reader_feedback.topic_suggestions(dispatch)
    assert len(suggestions) == 1
    assert suggestions[0].tag_name == "Funding"
    assert suggestions[0].suggested_tier == "none"
    assert suggestions[0].down == 4 and suggestions[0].up == 1


def test_no_suggestion_below_vote_threshold(db, user):
    dispatch = _dispatch(db, user)
    tag = Tag(name="Funding", scope="global")
    db.session.add(tag)
    db.session.commit()
    _vote_on_tagged_items(db, user, dispatch, tag, [-1, -1])

    assert reader_feedback.topic_suggestions(dispatch) == []


def test_no_suggestion_when_votes_are_mixed(db, user):
    dispatch = _dispatch(db, user)
    tag = Tag(name="Funding", scope="global")
    db.session.add(tag)
    db.session.commit()
    _vote_on_tagged_items(db, user, dispatch, tag, [-1, -1, 1, 1, 1])

    assert reader_feedback.topic_suggestions(dispatch) == []


def test_no_suggestion_when_already_in_that_tier(db, user):
    """Don't nag about a change the owner has already made."""
    dispatch = _dispatch(db, user)
    tag = Tag(name="Funding", scope="global")
    db.session.add(tag)
    db.session.commit()
    _vote_on_tagged_items(db, user, dispatch, tag, [-1, -1, -1, -1])

    assert len(reader_feedback.topic_suggestions(dispatch)) == 1
    dispatch.params = {"topic_tiers": {"none": [tag.id], "highlights": []}}
    db.session.commit()
    assert reader_feedback.topic_suggestions(dispatch) == []


def test_follower_votes_do_not_steer_owner_suggestions(db, user, admin):
    """Only the owner's votes count toward their Dispatch's suggestions."""
    dispatch = _dispatch(db, user)
    tag = Tag(name="Funding", scope="global")
    db.session.add(tag)
    db.session.commit()
    # admin (a follower, not the owner) downvotes everything.
    _vote_on_tagged_items(db, admin, dispatch, tag, [-1, -1, -1, -1, -1])

    assert reader_feedback.topic_suggestions(dispatch) == []


# ── Layer 2: per-item reader_signal ─────────────────────────────────────────

def test_score_items_silent_until_both_sides_have_votes(db, user):
    dispatch = _dispatch(db, user)
    tag = Tag(name="T", scope="global")
    db.session.add(tag)
    db.session.commit()
    # Ten downvotes, no upvotes — one-sided, so no contrast to draw.
    _vote_on_tagged_items(db, user, dispatch, tag, [-1] * 10)

    scope = [_item(db, "Some new story", "about something")]
    assert reader_feedback.score_items(dispatch, scope) == {}


def test_score_items_flags_resemblance_to_past_votes(db, user):
    dispatch = _dispatch(db, user)
    blocks, recorded = [], []
    # Two clearly distinct vocabularies so TF-IDF has a real contrast.
    for i in range(10):
        liked = _item(db, f"Quantum compiler research paper {i}",
                      "quantum compiler lattice research benchmark paper")
        disliked = _item(db, f"Series B funding round {i}",
                         "startup raises venture funding round valuation investors")
        blocks.append(_item_block(f"up{i}", liked.id))
        blocks.append(_item_block(f"down{i}", disliked.id))
        recorded += [(f"up{i}", liked, 1), (f"down{i}", disliked, -1)]
    run = _run(db, dispatch, blocks)
    for block_id, item, v in recorded:
        db.session.add(ItemFeedback(
            user_id=user.id, run_id=run.id, block_id=block_id,
            item_id=item.id, vote=v,
        ))
    db.session.commit()

    like_me = _item(db, "New quantum compiler benchmark",
                    "quantum compiler lattice benchmark research")
    hate_me = _item(db, "Startup raises Series C",
                    "startup venture funding round investors valuation")
    scores = reader_feedback.score_items(dispatch, [like_me, hate_me])

    assert scores[like_me.id]["score"] > 0
    assert scores[hate_me.id]["score"] < 0


def test_score_items_empty_scope(db, user):
    dispatch = _dispatch(db, user)
    assert reader_feedback.score_items(dispatch, []) == {}


def test_reader_signal_omitted_from_unflagged_items(db, user):
    """Items with no signal carry no extra tokens — same contract as
    prior_coverage."""
    from app.agent.tools import _item_brief
    item = _item(db, "Plain story")
    assert "reader_signal" not in _item_brief(item, [], None, None)
    flagged = _item_brief(item, [], None, {"score": -0.4, "basis": "x"})
    assert flagged["reader_signal"]["score"] == -0.4
