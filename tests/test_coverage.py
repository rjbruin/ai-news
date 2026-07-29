"""Deterministic edition coverage — which in-scope items an edition cited.

The bare-domain case below was a real defect: ~17% of recorded coverage on the
production corpus was an item being matched to an edition it never appeared in,
because the item's stored URL was a publisher homepage that the edition
happened to link to elsewhere.
"""
from datetime import timedelta

from app.models import NewsItem, Summary, SummaryRun, utcnow
from app.services.coverage import document_references, edition_coverage


def _dispatch(db, user):
    s = Summary(
        user_id=user.id, name="Daily", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(s)
    db.session.commit()
    return s


def _item(db, title, url=None, hours_ago=1):
    it = NewsItem(
        dedup_hash=NewsItem.make_hash(title, url),
        title=title, url=url,
        fetched_at=utcnow().replace(tzinfo=None) - timedelta(hours=hours_ago),
    )
    db.session.add(it)
    db.session.commit()
    return it


def _run(db, summary, document):
    now = utcnow().replace(tzinfo=None)
    r = SummaryRun(
        summary_id=summary.id, label="Today", status="ok", document=document,
        range_start=now - timedelta(days=1), range_end=now + timedelta(hours=1),
    )
    db.session.add(r)
    db.session.commit()
    return r


def test_document_references_extracts_ids_and_urls():
    ids, urls = document_references([
        {"type": "item", "item_id": 7, "sources": ["https://a.example/story"]},
    ])
    assert 7 in ids
    assert "a.example/story" in urls


def test_item_matched_by_id(app, db, user):
    summary = _dispatch(db, user)
    item = _item(db, "Something happened")
    run = _run(db, summary, [{"type": "item", "item_id": item.id}])

    result = edition_coverage(run)
    assert [i.id for i in result["covered"]] == [item.id]


def test_item_matched_by_url_ignoring_trailing_slash(app, db, user):
    summary = _dispatch(db, user)
    item = _item(db, "Something happened", "https://a.example/story")
    run = _run(db, summary, [
        {"type": "item", "sources": ["https://a.example/story/"]},
    ])

    assert [i.id for i in edition_coverage(run)["covered"]] == [item.id]


def test_bare_domain_url_does_not_match(app, db, user):
    """An item stored with a publisher homepage must not be counted as covered
    just because the edition links to that publisher somewhere else."""
    summary = _dispatch(db, user)
    item = _item(db, "Never actually reported", "https://techcrunch.com")
    run = _run(db, summary, [
        {"type": "item", "item_id": 999,
         "sources": ["https://techcrunch.com", "https://techcrunch.com/2026/other"]},
    ])

    result = edition_coverage(run)
    assert result["covered"] == []
    assert [i.id for i in result["not_covered"]] == [item.id]


def test_bare_domain_item_still_matched_by_id(app, db, user):
    """Dropping the URL match must not hide a genuine citation by id."""
    summary = _dispatch(db, user)
    item = _item(db, "Genuinely reported", "https://techcrunch.com")
    run = _run(db, summary, [{"type": "item", "item_id": item.id}])

    assert [i.id for i in edition_coverage(run)["covered"]] == [item.id]


def test_uncited_item_reported_as_not_covered(app, db, user):
    summary = _dispatch(db, user)
    cited = _item(db, "Ran", "https://a.example/one")
    skipped = _item(db, "Did not run", "https://a.example/two")
    run = _run(db, summary, [{"type": "item", "item_id": cited.id}])

    result = edition_coverage(run)
    assert result["scope_count"] == 2
    assert result["included_count"] == 1
    assert [i.id for i in result["not_covered"]] == [skipped.id]
