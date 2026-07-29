"""Cross-edition story deduplication — parts A and B.

See docs/story-dedup-spec.md. The regression fixture at the bottom is built
from the real production failure: two NewsItem rows with byte-identical titles,
ingested from different sources four days apart, both of which ran.
"""
from datetime import timedelta

import pytest

from app.agent import memory as agent_memory
from app.agent.context import AgentSession
from app.agent.tools import _item_brief, t_list_scope_items, t_read_coverage
from app.models import NewsItem, Summary, SummaryRun, utcnow
from app.services.story_dedup import (
    TIER_FOLLOW_UP, TIER_LIKELY, find_prior_coverage, match_text,
)


def _dispatch(db, user):
    s = Summary(
        user_id=user.id, name="Daily", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(s)
    db.session.commit()
    return s


def _item(db, title, one_liner="", url=None, days_ago=0):
    it = NewsItem(
        dedup_hash=NewsItem.make_hash(title, url),
        title=title, one_liner=one_liner, url=url,
        fetched_at=utcnow().replace(tzinfo=None) - timedelta(days=days_ago),
    )
    db.session.add(it)
    db.session.commit()
    return it


def _cover(db, user, summary, items, days_ago=1, run_id=None):
    """Record ``items`` as covered by an edition ``days_ago`` days back."""
    ts = utcnow().replace(tzinfo=None) - timedelta(days=days_ago)
    return agent_memory.write_coverage(
        user, summary, ts,
        [
            {"item_id": i.id, "title": i.title, "url": i.url, "run_id": run_id}
            for i in items
        ],
    )


# ── A: coverage memory ──────────────────────────────────────────────────────

def test_write_and_read_coverage_round_trip(app, db, user):
    summary = _dispatch(db, user)
    item = _item(db, "OpenAI ships GPT-6", "A new frontier model.")
    _cover(db, user, summary, [item])

    records = agent_memory.recent_coverage(user, summary, days=14)
    assert len(records) == 1
    assert records[0]["item_id"] == item.id
    assert records[0]["title"] == "OpenAI ships GPT-6"
    assert records[0]["edition_ts"] is not None


def test_write_coverage_noop_when_nothing_cited(app, db, user):
    summary = _dispatch(db, user)
    assert agent_memory.write_coverage(user, summary, utcnow(), []) is None
    assert agent_memory.recent_coverage(user, summary, days=14) == []


def test_recent_coverage_respects_window(app, db, user):
    summary = _dispatch(db, user)
    item = _item(db, "Old news")
    _cover(db, user, summary, [item], days_ago=30)

    assert agent_memory.recent_coverage(user, summary, days=14) == []
    assert len(agent_memory.recent_coverage(user, summary, days=60)) == 1


def test_prune_coverage_removes_only_old_rows(app, db, user):
    summary = _dispatch(db, user)
    fresh = _item(db, "Fresh story")
    stale = _item(db, "Stale story")
    _cover(db, user, summary, [fresh], days_ago=1)
    _cover(db, user, summary, [stale], days_ago=40)

    assert agent_memory.prune_coverage(days=14) == 1
    remaining = agent_memory.recent_coverage(user, summary, days=60)
    assert [r["item_id"] for r in remaining] == [fresh.id]


def test_coverage_exists_is_idempotency_guard(app, db, user):
    summary = _dispatch(db, user)
    item = _item(db, "Something")
    row = _cover(db, user, summary, [item])

    assert agent_memory.coverage_exists(summary.id, row.edition_ts) is True
    assert agent_memory.coverage_exists(summary.id, utcnow()) is False


def test_coverage_is_scoped_per_summary(app, db, user, admin):
    mine = _dispatch(db, user)
    theirs = _dispatch(db, admin)
    item = _item(db, "Shared item")
    _cover(db, user, mine, [item])

    assert len(agent_memory.recent_coverage(user, mine, days=14)) == 1
    assert agent_memory.recent_coverage(admin, theirs, days=14) == []


# ── B: similarity matching ──────────────────────────────────────────────────

def test_identical_titles_flagged_as_likely_duplicate(app, db, user):
    """The production failure: same headline, two rows, two editions."""
    summary = _dispatch(db, user)
    title = "Anthropic Brings Claude Cowork to Web and Mobile for Max Subscribers"
    old = _item(db, title, "Claude Cowork expands to web and mobile.",
                url="https://a.example/cowork", days_ago=5)
    new = _item(db, title, "Claude Cowork expands to web and mobile.",
                url="https://b.example/cowork", days_ago=1)
    _cover(db, user, summary, [old], days_ago=4)

    flags = find_prior_coverage(user, summary, [new])
    assert new.id in flags
    match = flags[new.id][0]
    assert match.item_id == old.id
    assert match.tier == TIER_LIKELY


def test_unrelated_item_not_flagged(app, db, user):
    summary = _dispatch(db, user)
    old = _item(db, "EU AI Act enters into force",
                "New obligations for high-risk systems.", days_ago=5)
    new = _item(db, "Nvidia announces new gaming GPU",
                "Consumer graphics card with more VRAM.", days_ago=1)
    _cover(db, user, summary, [old], days_ago=4)

    assert find_prior_coverage(user, summary, [new]) == {}


def test_no_prior_coverage_returns_empty(app, db, user):
    summary = _dispatch(db, user)
    item = _item(db, "First ever story")
    assert find_prior_coverage(user, summary, [item]) == {}


def test_item_is_not_a_duplicate_of_itself(app, db, user):
    """A row already recorded as covered must not match against its own record."""
    summary = _dispatch(db, user)
    item = _item(db, "Anthropic ships Claude Opus 5", "Half the price of Fable 5.")
    _cover(db, user, summary, [item])

    assert find_prior_coverage(user, summary, [item]) == {}


def test_revision_excludes_its_own_edition_chain(app, db, user):
    """A revision replaces its parent, so the parent's coverage is not
    already-covered ground — otherwise every item the parent featured comes
    back flagged as a duplicate of itself."""
    summary = _dispatch(db, user)
    item = _item(db, "Anthropic ships Claude Opus 5", "Half the price of Fable 5.")
    other = _item(
        db, "Anthropic ships Claude Opus 5 at half the price",
        "Half the price of Fable 5.", url="https://x.example/2",
    )
    _cover(db, user, summary, [item], run_id=99)

    # Without the exclusion the near-identical item is flagged...
    assert other.id in find_prior_coverage(user, summary, [other])
    # ...and with the parent run excluded, it is not.
    assert find_prior_coverage(
        user, summary, [other], exclude_run_ids={99}
    ) == {}


def test_disabled_by_config(app, db, user):
    summary = _dispatch(db, user)
    title = "Anthropic Brings Claude Cowork to Web and Mobile"
    old = _item(db, title, "Cowork expands.", url="https://a.example/x", days_ago=5)
    new = _item(db, title, "Cowork expands.", url="https://b.example/y", days_ago=1)
    _cover(db, user, summary, [old], days_ago=4)

    app.config["AGENT_DEDUP_ENABLED"] = False
    assert find_prior_coverage(user, summary, [new]) == {}


def test_scoring_failure_degrades_to_no_flags(app, db, user, monkeypatch):
    """A broken deduper must never block edition generation."""
    summary = _dispatch(db, user)
    old = _item(db, "Some story", "Details.", days_ago=5)
    new = _item(db, "Some story", "Details.", url="https://x.example", days_ago=1)
    _cover(db, user, summary, [old], days_ago=4)

    from app.services import story_dedup
    monkeypatch.setattr(
        story_dedup, "_background_corpus",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert story_dedup.find_prior_coverage(user, summary, [new]) == {}


def test_threshold_separates_tiers(app, db, user):
    """A related-but-weaker match lands in the follow-up tier, not likely."""
    summary = _dispatch(db, user)
    old = _item(
        db, "Claude Cowork arrives on web and mobile for Max subscribers",
        "Anthropic expands Cowork to more platforms for paying users.",
        url="https://a.example/1", days_ago=5,
    )
    new = _item(
        db, "Claude Cowork now works on your phone without a Max subscription",
        "Anthropic widens Cowork availability beyond Max subscribers.",
        url="https://b.example/2", days_ago=1,
    )
    _cover(db, user, summary, [old], days_ago=4)

    # Force a high certainty bar so this pair cannot reach the likely tier.
    app.config["AGENT_DEDUP_CERTAIN_THRESHOLD"] = 0.99
    app.config["AGENT_DEDUP_TITLE_THRESHOLD"] = 0.99
    app.config["AGENT_DEDUP_THRESHOLD"] = 0.10

    flags = find_prior_coverage(user, summary, [new])
    assert new.id in flags
    assert flags[new.id][0].tier == TIER_FOLLOW_UP


def test_match_text_prefers_one_liner_over_summary():
    assert match_text("T", "the gist", "long body") == "T the gist"
    assert match_text("T", None, "long body") == "T long body"
    assert match_text("T", None, None) == "T"


# ── B: agent-facing surface ─────────────────────────────────────────────────

def test_item_brief_omits_prior_coverage_when_unflagged(app, db, user):
    item = _item(db, "A story", "Gist.")
    assert "prior_coverage" not in _item_brief(item, [], None)
    assert "prior_coverage" not in _item_brief(item, [], [])


def test_list_scope_items_includes_flags(app, db, user):
    summary = _dispatch(db, user)
    title = "Anthropic Brings Claude Cowork to Web and Mobile for Max Subscribers"
    old = _item(db, title, "Cowork expands.", url="https://a.example/x", days_ago=5)
    new = _item(db, title, "Cowork expands.", url="https://b.example/y", days_ago=1)
    _cover(db, user, summary, [old], days_ago=4)

    flags = find_prior_coverage(user, summary, [new])
    session = AgentSession(
        user=user, summary=summary, items=[new],
        range_start=None, range_end=None, prior_coverage=flags,
    )
    payload = t_list_scope_items(session)
    entry = payload["items"][0]
    assert entry["prior_coverage"][0]["tier"] == TIER_LIKELY
    assert entry["prior_coverage"][0]["item_id"] == old.id
    assert 0.0 <= entry["prior_coverage"][0]["score"] <= 1.0


def test_read_coverage_tool_lists_cited_articles(app, db, user):
    summary = _dispatch(db, user)
    item = _item(db, "Google ships Gemini 4", "New frontier model.")
    _cover(db, user, summary, [item])

    session = AgentSession(
        user=user, summary=summary, items=[], range_start=None, range_end=None,
    )
    payload = t_read_coverage(session, days=14)
    assert payload["count"] == 1
    assert payload["covered"][0]["title"] == "Google ships Gemini 4"
    assert payload["covered"][0]["item_id"] == item.id


# ── A: persistence from the generation path ─────────────────────────────────

def test_build_summary_persists_coverage_record(app, db, user, monkeypatch):
    """The record must list exactly the in-scope items the document cited."""
    from app.services import summarize

    summary = _dispatch(db, user)
    cited = _item(db, "Cited story", "In the edition.")
    skipped = _item(db, "Skipped story", "Left out.")

    document = [{
        "type": "item", "id": "b1", "headline": "Cited story",
        "subheader": "x", "summary": "y", "item_id": cited.id,
    }]

    class _Artifact:
        html = "<p>x</p>"
        file_path = None

    class _Plugin:
        is_agentic = True

        def build(self, *a, **kw):
            return _Artifact()

    monkeypatch.setattr(
        summarize, "_build_agentic",
        lambda *a, **kw: (_Artifact(), document, None, 0.0),
    )
    monkeypatch.setattr(summarize, "items_in_scope", lambda s: [cited, skipped])
    monkeypatch.setattr(
        summarize.summary_registry, "create", lambda key: _Plugin(),
    )

    _artifact, _items, run = summarize.build_summary(summary, record_run=True)
    assert run is not None

    records = agent_memory.recent_coverage(user, summary, days=14)
    assert [r["item_id"] for r in records] == [cited.id]
