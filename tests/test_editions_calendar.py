from datetime import datetime

from app.models import Summary, SummaryRun
from app.services.summarize import edition_heads_in_range


def _day_panel(html: str, day_iso: str) -> str:
    """Extract one day's `<div id="day-...">...</div>` panel content from a
    rendered /summaries page (panels are flat siblings, not nested), so the
    text between this day's marker and the next `day-` marker (or end of
    string) is exactly this panel's content, regardless of dict order."""
    marker = f'id="day-{day_iso}"'
    start = html.index(marker)
    rest = html[start:]
    next_marker = rest.find('id="day-', len(marker))
    return rest[:next_marker] if next_marker != -1 else rest


def _dispatch(db, owner, name="Daily"):
    s = Summary(
        user_id=owner.id, name=name, type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(s)
    db.session.commit()
    return s


def _run(db, summary, generated_at, status="ok", label=None):
    r = SummaryRun(
        summary_id=summary.id, label=label, generated_at=generated_at,
        status=status, content="<p>hi</p>",
    )
    db.session.add(r)
    db.session.commit()
    return r


# ── edition_heads_in_range ──────────────────────────────────────────────────

def test_edition_heads_in_range_filters_to_month(db, user):
    summary = _dispatch(db, user)
    before = _run(db, summary, datetime(2026, 2, 28, 23, 59))
    inside = _run(db, summary, datetime(2026, 3, 15, 8, 0))
    at_start = _run(db, summary, datetime(2026, 3, 1, 0, 0))
    at_end = _run(db, summary, datetime(2026, 4, 1, 0, 0))
    after = _run(db, summary, datetime(2026, 4, 2, 0, 0))

    feed = edition_heads_in_range([summary], datetime(2026, 3, 1), datetime(2026, 4, 1))
    run_ids = {r.id for r, _ in feed}
    assert run_ids == {inside.id, at_start.id}
    assert before.id not in run_ids
    assert at_end.id not in run_ids
    assert after.id not in run_ids


def test_edition_heads_in_range_newest_first(db, user):
    summary = _dispatch(db, user)
    earlier = _run(db, summary, datetime(2026, 3, 5))
    later = _run(db, summary, datetime(2026, 3, 20))
    feed = edition_heads_in_range([summary], datetime(2026, 3, 1), datetime(2026, 4, 1))
    assert [r.id for r, _ in feed] == [later.id, earlier.id]


def test_edition_heads_in_range_only_head_of_chain_counted(db, user):
    """A revision chain's head generated_at is what's checked — an older
    revision inside the queried month doesn't leak in if the head moved to
    a later month."""
    summary = _dispatch(db, user)
    original = _run(db, summary, datetime(2026, 3, 30))
    revision = SummaryRun(
        summary_id=summary.id, generated_at=datetime(2026, 4, 1, 12, 0),
        status="ok", content="<p>rev</p>", parent_run_id=original.id, revision=2,
    )
    db.session.add(revision)
    db.session.commit()

    march_feed = edition_heads_in_range([summary], datetime(2026, 3, 1), datetime(2026, 4, 1))
    assert march_feed == []  # the chain's head is now in April, not March

    april_feed = edition_heads_in_range([summary], datetime(2026, 4, 1), datetime(2026, 5, 1))
    assert [r.id for r, _ in april_feed] == [revision.id]


def test_edition_heads_in_range_no_cap(db, user):
    summary = _dispatch(db, user)
    for i in range(60):
        _run(db, summary, datetime(2026, 3, 1, 0, i))
    feed = edition_heads_in_range([summary], datetime(2026, 3, 1), datetime(2026, 4, 1))
    assert len(feed) == 60


# ── /summaries route ─────────────────────────────────────────────────────────

def test_summaries_defaults_to_current_month(auth_client, db, user):
    resp = auth_client.get("/summaries")
    assert resp.status_code == 200


def test_summaries_month_param_and_prev_next_links(auth_client, db, user, admin):
    summary = _dispatch(db, admin)
    user.follow(summary)
    _run(db, summary, datetime(2026, 3, 15))
    db.session.commit()

    resp = auth_client.get("/summaries?year=2026&month=3")
    html = resp.data.decode()
    assert "March 2026" in html
    assert "year=2026&amp;month=2" in html or "year=2026&month=2" in html
    assert "year=2026&amp;month=4" in html or "year=2026&month=4" in html


def test_summaries_december_to_january_rollover(auth_client, db, user):
    resp = auth_client.get("/summaries?year=2026&month=12")
    html = resp.data.decode()
    assert "year=2027&amp;month=1" in html or "year=2027&month=1" in html
    assert "year=2026&amp;month=11" in html or "year=2026&month=11" in html


def test_summaries_january_to_december_rollover(auth_client, db, user):
    resp = auth_client.get("/summaries?year=2026&month=1")
    html = resp.data.decode()
    assert "year=2025&amp;month=12" in html or "year=2025&month=12" in html
    assert "year=2026&amp;month=2" in html or "year=2026&month=2" in html


def test_summaries_highlights_ok_day(auth_client, db, user, admin):
    summary = _dispatch(db, admin)
    user.follow(summary)
    run = _run(db, summary, datetime(2026, 3, 15))
    db.session.commit()

    resp = auth_client.get("/summaries?year=2026&month=3")
    html = resp.data.decode()
    assert "has-edition" in html
    assert f'id="day-2026-03-15"' in html
    assert f"/summaries/{summary.id}/editions/{run.id}" in html


def test_summaries_failed_only_day_gets_distinct_marker(auth_client, db, user, admin):
    summary = _dispatch(db, admin)
    user.follow(summary)
    _run(db, summary, datetime(2026, 3, 16), status="failed")
    db.session.commit()

    resp = auth_client.get("/summaries?year=2026&month=3")
    html = resp.data.decode()
    cell = html.split('data-day-panel="day-2026-03-16"')[0].rsplit("<td", 1)[-1]
    assert "has-failed-only" in cell
    assert "has-edition" not in cell


def test_summaries_mixed_day_gets_normal_highlight(auth_client, db, user, admin):
    summary = _dispatch(db, admin)
    user.follow(summary)
    _run(db, summary, datetime(2026, 3, 17), status="failed")
    _run(db, summary, datetime(2026, 3, 17, 1), status="ok")
    db.session.commit()

    resp = auth_client.get("/summaries?year=2026&month=3")
    html = resp.data.decode()
    assert "has-edition" in html


def test_summaries_two_dispatches_same_day_two_hero_cards(auth_client, db, user, admin):
    d1 = _dispatch(db, user, name="Mine")
    d2 = _dispatch(db, admin, name="Admin's")
    user.follow(d1)
    user.follow(d2)
    r1 = _run(db, d1, datetime(2026, 3, 18), label="One")
    r2 = _run(db, d2, datetime(2026, 3, 18), label="Two")
    db.session.commit()

    resp = auth_client.get("/summaries?year=2026&month=3")
    panel = _day_panel(resp.data.decode(), "2026-03-18")
    assert f"/summaries/{d1.id}/editions/{r1.id}" in panel
    assert f"/summaries/{d2.id}/editions/{r2.id}" in panel


def test_summaries_owner_sees_delete_non_owner_does_not(auth_client, db, user, admin):
    own = _dispatch(db, user, name="Mine")
    others = _dispatch(db, admin, name="Admin's")
    user.follow(own)
    user.follow(others)
    own_run = _run(db, own, datetime(2026, 3, 19), label="Own")
    other_run = _run(db, others, datetime(2026, 3, 20), label="Other")
    db.session.commit()

    html = auth_client.get("/summaries?year=2026&month=3").data.decode()

    own_panel = _day_panel(html, "2026-03-19")
    assert f"/summaries/{own.id}/editions/{own_run.id}/delete" in own_panel

    other_panel = _day_panel(html, "2026-03-20")
    assert f"/summaries/{others.id}/editions/{other_run.id}/delete" not in other_panel
