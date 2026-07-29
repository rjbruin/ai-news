"""Review editions — the slower retrospective cadence over a Dispatch's own
editions. See docs/review-editions-spec.md.

The kind-filter tests at the top guard the change's main hazard: a review run
covers a whole month, so its range_end sits far ahead of a daily cadence. Any
"latest run" lookup that forgets to filter on kind concludes the daily period
is already cut and stops producing daily editions, silently.
"""
from datetime import datetime, timedelta

import pytest

from app.models import Summary, SummaryRun, utcnow
from app.services import summarize


def _dispatch(db, user, **kw):
    kw.setdefault("params", {})
    s = Summary(
        user_id=user.id, name="Daily", type_key="agentic_page",
        scope_mode="fixed_period", period="day", **kw,
    )
    db.session.add(s)
    db.session.commit()
    return s


def _run(db, summary, *, kind="edition", days_ago=0, span_days=1, status="ok", **kw):
    end = utcnow().replace(tzinfo=None) - timedelta(days=days_ago)
    r = SummaryRun(
        summary_id=summary.id, kind=kind, status=status,
        range_start=end - timedelta(days=span_days), range_end=end,
        generated_at=end, label="X", **kw,
    )
    db.session.add(r)
    db.session.commit()
    return r


# ── kind discriminator basics ───────────────────────────────────────────────

def test_runs_default_to_edition_kind(app, db, user):
    summary = _dispatch(db, user)
    run = SummaryRun(summary_id=summary.id, label="X")
    db.session.add(run)
    db.session.commit()
    assert run.kind == "edition"
    assert run.is_review is False


def test_review_kind_flag(app, db, user):
    summary = _dispatch(db, user)
    review = _run(db, summary, kind="review")
    assert review.is_review is True


# ── the hazard: reviews must not look like the latest edition ───────────────

def test_review_does_not_shrink_the_next_edition_window(app, db, user):
    """resolve_range starts the next window at the last edition's range_end.
    A month-spanning review must not become that anchor."""
    summary = _dispatch(db, user)
    _run(db, summary, kind="edition", days_ago=1)
    start_before, _ = summarize.resolve_range(summary)

    # A review covering the last 30 days, generated just now.
    _run(db, summary, kind="review", days_ago=0, span_days=30)
    start_after, _ = summarize.resolve_range(summary)

    assert start_after == start_before


def test_review_does_not_suppress_the_next_daily_edition(app, db, user, monkeypatch):
    """The end-to-end version: cut_due_editions must still cut a daily edition
    on a Dispatch that just produced a review."""
    summary = _dispatch(db, user)
    _run(db, summary, kind="review", days_ago=0, span_days=30)

    built = []
    monkeypatch.setattr(
        summarize, "build_summary",
        lambda s, **kw: (built.append(s.id), (None, [], None))[1],
    )
    summarize.cut_due_editions(force=True)
    assert built == [summary.id]


def test_failed_review_does_not_consume_the_edition_retry_budget(app, db, user, monkeypatch):
    """The per-period failure backoff counts failed attempts; failed reviews
    must not count against the daily edition's allowance."""
    summary = _dispatch(db, user)
    _, expected_end = summarize.resolve_range(summary)
    expected_naive = expected_end.replace(tzinfo=None)
    for _ in range(summarize.MAX_FAILED_ATTEMPTS_PER_PERIOD):
        r = SummaryRun(
            summary_id=summary.id, kind="review", status="failed",
            range_end=expected_naive, label="R",
        )
        db.session.add(r)
    db.session.commit()

    built = []
    monkeypatch.setattr(
        summarize, "build_summary",
        lambda s, **kw: (built.append(s.id), (None, [], None))[1],
    )
    summarize.cut_due_editions()
    assert built == [summary.id]


# ── edition_heads kind filtering ────────────────────────────────────────────

def test_edition_heads_returns_both_kinds_by_default(app, db, user):
    """Surfaces that show everything a Dispatch published (calendar, feed)
    need both."""
    summary = _dispatch(db, user)
    _run(db, summary, kind="edition", days_ago=2)
    _run(db, summary, kind="review", days_ago=1)

    kinds = {r.kind for r in summarize.edition_heads(summary)}
    assert kinds == {"edition", "review"}


def test_edition_heads_can_filter_to_one_kind(app, db, user):
    summary = _dispatch(db, user)
    _run(db, summary, kind="edition", days_ago=2)
    _run(db, summary, kind="review", days_ago=1)

    assert [r.kind for r in summarize.edition_heads(summary, kind="edition")] == ["edition"]
    assert [r.kind for r in summarize.edition_heads(summary, kind="review")] == ["review"]


def test_past_editions_tool_excludes_reviews(app, db, user):
    from app.agent.context import AgentSession
    from app.agent.tools import t_list_past_editions

    summary = _dispatch(db, user)
    _run(db, summary, kind="edition", days_ago=2, document=[{"type": "intro", "markdown": "x"}])
    _run(db, summary, kind="review", days_ago=1, document=[{"type": "intro", "markdown": "y"}])

    session = AgentSession(
        user=user, summary=summary, items=[], range_start=None, range_end=None,
    )
    got = t_list_past_editions(session)
    assert len(got["editions"]) == 1


# ── review period boundaries ────────────────────────────────────────────────

@pytest.mark.parametrize("period,now,expected_start,expected_end", [
    ("month", datetime(2026, 8, 3), datetime(2026, 7, 1), datetime(2026, 8, 1)),
    ("month", datetime(2026, 1, 15), datetime(2025, 12, 1), datetime(2026, 1, 1)),
    ("week", datetime(2026, 7, 29), datetime(2026, 7, 20), datetime(2026, 7, 27)),
    ("quarter", datetime(2026, 8, 3), datetime(2026, 4, 1), datetime(2026, 7, 1)),
    ("quarter", datetime(2026, 2, 3), datetime(2025, 10, 1), datetime(2026, 1, 1)),
    ("year", datetime(2026, 8, 3), datetime(2025, 1, 1), datetime(2026, 1, 1)),
])
def test_resolve_review_range_aligns_to_calendar(app, db, user, period, now,
                                                 expected_start, expected_end):
    """A monthly review covers a calendar month, not a rolling 30 days —
    "the July review" has to mean July."""
    summary = _dispatch(db, user, review_period=period)
    start, end = summarize.resolve_review_range(summary, now=now)
    assert (start.replace(tzinfo=None), end.replace(tzinfo=None)) == (
        expected_start, expected_end,
    )


def test_resolve_review_range_off_when_no_period(app, db, user):
    summary = _dispatch(db, user)
    assert summarize.resolve_review_range(summary) == (None, None)


def test_cut_due_reviews_skips_dispatches_with_reviews_off(app, db, user, monkeypatch):
    _dispatch(db, user)
    called = []
    monkeypatch.setattr(summarize, "build_review",
                        lambda *a, **kw: called.append(1))
    assert summarize.cut_due_reviews() == 0
    assert called == []


def test_cut_due_reviews_does_not_repeat_a_period(app, db, user, monkeypatch):
    summary = _dispatch(db, user, review_period="month")
    start, end = summarize.resolve_review_range(summary)
    SummaryRun.query.delete()
    db.session.add(SummaryRun(
        summary_id=summary.id, kind="review", status="ok",
        range_start=start.replace(tzinfo=None), range_end=end.replace(tzinfo=None),
        label="prev",
    ))
    db.session.commit()

    called = []
    monkeypatch.setattr(summarize, "build_review", lambda *a, **kw: called.append(1))
    summarize.cut_due_reviews()
    assert called == []


# ── digest ──────────────────────────────────────────────────────────────────

def _doc(headline, subtitle, item_headlines, block_type="item"):
    doc = [{"type": "edition_header", "id": "b_h", "title": headline, "subtitle": subtitle}]
    for i, h in enumerate(item_headlines):
        doc.append({
            "type": block_type, "id": f"b_{i}", "headline": h,
            "subheader": "sub", "summary": "body text",
        })
    doc.append({"type": "more_news", "id": "b_qh", "items": [
        {"headline": "a quick hit", "url": "https://x.example/1"},
    ]})
    return doc


def test_digest_keeps_headers_and_item_headlines_only(app, db, user):
    from app.services.review_digest import digest_for_range, render_digest

    summary = _dispatch(db, user)
    _run(db, summary, days_ago=1, document=_doc("Big day", "and more", ["One", "Two"]))

    d = digest_for_range(summary, utcnow().replace(tzinfo=None) - timedelta(days=5),
                         utcnow().replace(tzinfo=None) + timedelta(days=1))
    assert len(d) == 1
    assert d[0]["headline"] == "Big day"
    assert d[0]["subheader"] == "and more"
    assert [i["headline"] for i in d[0]["items"]] == ["One", "Two"]

    text = render_digest(d)
    assert "One" in text and "Two" in text
    assert "a quick hit" not in text     # quick hits excluded
    assert "body text" not in text       # item bodies excluded
    assert "sub" not in text             # item subheaders excluded


def test_digest_includes_legacy_story_blocks(app, db, user):
    """3 of 14 real July editions use the pre-`item` block types; matching on
    `item` alone silently drops ~15% of a month."""
    from app.services.review_digest import digest_for_range

    summary = _dispatch(db, user)
    _run(db, summary, days_ago=1,
         document=_doc("Legacy", "x", ["Old one"], block_type="story"))

    d = digest_for_range(summary, utcnow().replace(tzinfo=None) - timedelta(days=5),
                         utcnow().replace(tzinfo=None) + timedelta(days=1))
    assert [i["headline"] for i in d[0]["items"]] == ["Old one"]


def test_digest_excludes_reviews(app, db, user):
    from app.services.review_digest import digest_for_range

    summary = _dispatch(db, user)
    _run(db, summary, days_ago=2, document=_doc("An edition", "", ["A"]))
    _run(db, summary, kind="review", days_ago=1, document=_doc("A review", "", ["B"]))

    d = digest_for_range(summary, utcnow().replace(tzinfo=None) - timedelta(days=5),
                         utcnow().replace(tzinfo=None) + timedelta(days=1))
    assert [x["headline"] for x in d] == ["An edition"]


def test_digest_reports_what_it_drops_when_over_budget(app, db, user):
    """A silently trimmed digest reads to the editor as a complete record."""
    from app.services.review_digest import digest_for_range, render_digest

    summary = _dispatch(db, user)
    _run(db, summary, days_ago=1, document=_doc("Busy", "", [f"H{i}" for i in range(20)]))

    d = digest_for_range(summary, utcnow().replace(tzinfo=None) - timedelta(days=5),
                         utcnow().replace(tzinfo=None) + timedelta(days=1),
                         max_item_lines=5)
    assert d[0]["items"] == []
    assert d[0]["items_omitted"] == 20
    assert "20 featured items omitted" in render_digest(d)


def test_item_payload_normalises_legacy_field_names(app):
    from app.services.review_digest import item_block_payload

    modern = item_block_payload({
        "id": "b1", "headline": "H", "subheader": "S", "summary": "T",
        "sources": ["https://a.example/x"],
    })
    legacy = item_block_payload({
        "id": "b1", "headline": "H", "dek": "S", "body": "T",
        "url": "https://a.example/x",
    })
    assert modern == legacy


# ── review tool set ─────────────────────────────────────────────────────────

def test_review_toolset_swaps_data_tools(app):
    from app.agent.tools import REVIEW_TOOL_SPECS

    names = {s["function"]["name"] for s in REVIEW_TOOL_SPECS}
    assert "list_editions_in_scope" in names
    assert "get_edition_item" in names
    # Whole-document access would defeat the digest entirely.
    assert "get_edition" not in names
    assert "list_scope_items" not in names
    assert "get_item" not in names
    # Editor tools stay.
    assert "set_document" in names


def test_get_edition_item_returns_one_story(app, db, user):
    from app.agent.context import AgentSession
    from app.agent.tools import t_get_edition_item

    summary = _dispatch(db, user)
    run = _run(db, summary, days_ago=1, document=_doc("D", "", ["Only one"]))
    session = AgentSession(user=user, summary=summary, items=[],
                           range_start=None, range_end=None)

    got = t_get_edition_item(session, run.id, "b_0")
    assert got["headline"] == "Only one"
    assert got["text"] == "body text"

    assert "error" in t_get_edition_item(session, run.id, "nope")
    assert "error" in t_get_edition_item(session, 999999, "b_0")


# ── interface ───────────────────────────────────────────────────────────────

def test_settings_page_offers_every_review_period(auth_client, db, user):
    _dispatch(db, user)
    html = auth_client.get("/dispatch/settings").data.decode()
    assert 'id="sec-review"' in html
    for period in summarize.REVIEW_PERIODS:
        assert f'value="{period}"' in html
    assert "mem_review_content_config" in html
    # Reviews inherit interests from the editions; no separate interests file.
    assert "review_interests" not in html


def test_settings_saves_review_period_and_config(auth_client, db, user):
    from app.agent import memory as agent_memory

    dispatch = _dispatch(db, user)
    auth_client.post("/dispatch/settings", data={
        "period": "day",
        "review_period": "month",
        "mem_review_content_config": "# My review rules",
    })
    db.session.refresh(dispatch)
    assert dispatch.review_period == "month"
    assert agent_memory.read(user, dispatch, "review_content_config") == "# My review rules"


def test_settings_rejects_an_unknown_review_period(auth_client, db, user):
    dispatch = _dispatch(db, user, review_period="month")
    auth_client.post("/dispatch/settings", data={"period": "day", "review_period": "fortnight"})
    db.session.refresh(dispatch)
    assert dispatch.review_period is None


def test_settings_has_no_manual_generate_button(auth_client, db, user):
    """Reviews are cut by the schedule alone — an on-demand button would let a
    review land before its period had finished."""
    _dispatch(db, user, review_period="month")
    html = auth_client.get("/dispatch/settings").data.decode()
    assert "Generate review now" not in html
    assert "review/generate" not in html


def test_review_marked_on_the_edition_page(auth_client, db, user):
    dispatch = _dispatch(db, user)
    user.follow(dispatch)
    db.session.commit()
    review = _run(db, dispatch, kind="review", days_ago=1, content="<p>x</p>")

    html = auth_client.get(f"/summaries/{dispatch.id}/editions/{review.id}").data.decode()
    assert "Review edition" in html
    assert "pill-purple" in html


def test_edition_page_not_marked_as_review(auth_client, db, user):
    dispatch = _dispatch(db, user)
    user.follow(dispatch)
    db.session.commit()
    run = _run(db, dispatch, days_ago=1, content="<p>x</p>")

    html = auth_client.get(f"/summaries/{dispatch.id}/editions/{run.id}").data.decode()
    assert "Review edition" not in html


def test_calendar_shows_a_dot_per_kind(auth_client, db, user):
    dispatch = _dispatch(db, user)
    user.follow(dispatch)
    db.session.commit()
    _run(db, dispatch, kind="edition", days_ago=0)
    _run(db, dispatch, kind="review", days_ago=0)

    html = auth_client.get("/summaries").data.decode()
    assert "calendar-dot--review" in html
    assert html.count("calendar-dot") >= 2


def test_frontpage_shows_latest_edition_of_every_followed_dispatch(
    auth_client, db, user, admin,
):
    mine = _dispatch(db, user)
    theirs = _dispatch(db, admin)
    theirs.name = "Theirs"
    db.session.commit()
    user.follow(mine)
    user.follow(theirs)
    db.session.commit()

    _run(db, mine, days_ago=5, headline="Mine latest")
    _run(db, theirs, days_ago=1, headline="Theirs latest")

    html = auth_client.get("/dashboard").data.decode()
    # The older Dispatch is no longer hidden by the newer one.
    assert "Mine latest" in html
    assert "Theirs latest" in html


def test_frontpage_adds_an_unread_review_as_an_extra_card(auth_client, db, user):
    dispatch = _dispatch(db, user)
    user.follow(dispatch)
    db.session.commit()
    _run(db, dispatch, kind="edition", days_ago=2, headline="The edition")
    review = _run(db, dispatch, kind="review", days_ago=1, headline="The review")

    html = auth_client.get("/dashboard").data.decode()
    assert "The edition" in html
    assert "The review" in html

    # Once read it drops off, so it does not linger forever.
    from app.models import EditionRead
    db.session.add(EditionRead(user_id=user.id, run_id=review.id))
    db.session.commit()
    html = auth_client.get("/dashboard").data.decode()
    assert "The edition" in html
    assert "The review" not in html


def test_digest_accepts_aware_bounds(app, db, user):
    """resolve_review_range and the CLI both hand over aware datetimes, while
    generated_at is stored naive — comparing them raises TypeError."""
    from datetime import timezone

    from app.services.review_digest import digest_for_range

    summary = _dispatch(db, user)
    _run(db, summary, days_ago=1, document=_doc("Aware", "", ["A"]))

    now = utcnow().replace(tzinfo=None)
    d = digest_for_range(
        summary,
        (now - timedelta(days=5)).replace(tzinfo=timezone.utc),
        (now + timedelta(days=1)).replace(tzinfo=timezone.utc),
    )
    assert [x["headline"] for x in d] == ["Aware"]


def test_cut_due_reviews_reaches_build_with_real_ranges(app, db, user, monkeypatch):
    """End-to-end guard for the same hazard: cut_due_reviews passes
    resolve_review_range's aware output straight through to the digest."""
    summary = _dispatch(db, user, review_period="month")
    start, _end = summarize.resolve_review_range(summary)
    _run(db, summary, days_ago=0, document=_doc("In period", "", ["A"]))
    # Place the edition inside the completed period.
    run = SummaryRun.query.filter_by(summary_id=summary.id, kind="edition").first()
    run.generated_at = start.replace(tzinfo=None) + timedelta(days=1)
    db.session.commit()

    seen = {}
    def _fake_build(s, st, en, **kw):
        from app.services.review_digest import digest_for_range
        seen["n"] = len(digest_for_range(s, st, en))
        return None
    monkeypatch.setattr(summarize, "build_review", _fake_build)

    summarize.cut_due_reviews()
    assert seen.get("n") == 1


def test_empty_review_period_is_skipped_not_alerted(app, db, user, monkeypatch):
    """Reviews turned on mid-month look back at a period that predates the
    Dispatch. That is normal, so it must not alert the owner every tick."""
    from app.models import Alert

    summary = _dispatch(db, user, review_period="month")
    # An edition exists, but in the current period — not the completed one.
    _run(db, summary, days_ago=0)

    called = []
    monkeypatch.setattr(summarize, "build_review", lambda *a, **kw: called.append(1))
    assert summarize.cut_due_reviews() == 0
    assert called == []
    assert Alert.query.filter(Alert.key == f"review:{summary.id}").count() == 0


# ── release time ────────────────────────────────────────────────────────────

def test_review_release_at_uses_the_dispatch_release_time(app, db, user):
    summary = _dispatch(db, user, params={"release_time": "05:00"})
    end = datetime(2026, 8, 1)
    assert summarize.review_release_at(summary, end) == datetime(2026, 8, 1, 5, 0)


def test_review_release_at_defaults_and_survives_junk(app, db, user):
    assert summarize.review_release_at(
        _dispatch(db, user), datetime(2026, 8, 1),
    ) == datetime(2026, 8, 1, 8, 0)
    assert summarize.review_release_at(
        _dispatch(db, user, params={"release_time": "nonsense"}), datetime(2026, 8, 1),
    ) == datetime(2026, 8, 1, 8, 0)


def test_review_waits_for_the_release_time(app, db, user, monkeypatch):
    """The period ends at midnight; the review must not fire until the
    Dispatch's usual release hour that day."""
    summary = _dispatch(db, user, review_period="month",
                        params={"release_time": "05:00"})
    start, end = summarize.resolve_review_range(summary)
    _run(db, summary, days_ago=0, document=_doc("In period", "", ["A"]))
    run = SummaryRun.query.filter_by(summary_id=summary.id, kind="edition").first()
    run.generated_at = start.replace(tzinfo=None) + timedelta(days=1)
    db.session.commit()

    called = []
    monkeypatch.setattr(summarize, "build_review", lambda *a, **kw: called.append(1))

    # Just before the release time on the boundary day: not yet.
    before = summarize.review_release_at(summary, end) - timedelta(minutes=1)
    monkeypatch.setattr(summarize, "utcnow", lambda: before)
    summarize.cut_due_reviews()
    assert called == []

    # Just after: cut.
    monkeypatch.setattr(
        summarize, "utcnow",
        lambda: summarize.review_release_at(summary, end) + timedelta(minutes=1),
    )
    summarize.cut_due_reviews()
    assert called == [1]
