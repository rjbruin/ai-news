from datetime import timedelta

from app.models import ApiKey, NewsItem, Source, Summary
from app.services import ingest, summarize
from app.sources import registry as source_registry
from app.sources.base import ExtractedItem, NewsSource, RawDocument


class FakeSource(NewsSource):
    type_key = "fake"
    label = "Fake source"

    def fetch(self, since):
        return [RawDocument(external_id="1", text="body", subject="subj")]

    def extract(self, doc):
        return [
            ExtractedItem(title="Fake LLM story", summary="About gpt chatbots", url="http://f/1"),
            ExtractedItem(title="Fake robot story", summary="About a humanoid robot", url="http://f/2"),
        ]


def _make_api_key(db) -> ApiKey:
    key = ApiKey(label="Test key", provider="openrouter")
    key.set_key("sk-or-test")
    db.session.add(key)
    db.session.commit()
    return key


def test_ingest_source_creates_and_tags_items(app, db, sample_tags):
    app.config["TAGGING_MODE"] = "nb_only"
    app.config["NB_CONFIDENCE_THRESHOLD"] = 0.05
    source_registry.register(FakeSource)
    api_key = _make_api_key(db)
    source = Source(type_key="fake", name="Fake", config={}, api_key_id=api_key.id)
    db.session.add(source)
    db.session.commit()

    stats = ingest.ingest_source(source)
    assert stats["new_items"] == 2
    assert NewsItem.query.count() == 2


def test_ingest_dedups(app, db):
    source_registry.register(FakeSource)
    api_key = _make_api_key(db)
    source = Source(type_key="fake", name="Fake", config={}, api_key_id=api_key.id)
    db.session.add(source)
    db.session.commit()
    ingest.ingest_source(source)
    ingest.ingest_source(source)  # same items again
    assert NewsItem.query.count() == 2  # no duplicates


def test_ingest_source_without_api_key_fails_gracefully(app, db):
    source_registry.register(FakeSource)
    source = Source(type_key="fake", name="Fake", config={})
    db.session.add(source)
    db.session.commit()

    stats = ingest.ingest_source(source)
    assert stats["new_items"] == 0
    assert "no active api key" in source.last_status.lower()


def test_revoked_api_key_blocks_ingest(app, db):
    from app.models import utcnow

    source_registry.register(FakeSource)
    api_key = _make_api_key(db)
    api_key.revoked_at = utcnow()
    db.session.commit()
    source = Source(type_key="fake", name="Fake", config={}, api_key_id=api_key.id)
    db.session.add(source)
    db.session.commit()

    stats = ingest.ingest_source(source)
    assert stats["new_items"] == 0
    assert "no active api key" in source.last_status.lower()


def test_summary_fixed_period_scope(app, db, sample_items):
    summary = Summary(
        user_id=1, name="Daily", type_key="app_page",
        scope_mode="fixed_period", period="day", params={"group_by_tag": False},
    )
    db.session.add(summary)
    db.session.commit()

    # Backdate items to comfortably inside the resolved window, rather than a
    # fixed offset from wall-clock now() — the daily cutoff's own day-rollover
    # logic also depends on now(), so a hardcoded offset can land outside the
    # window depending on what time of day the test happens to run.
    _, end = summarize.resolve_range(summary)
    for item in sample_items:
        item.fetched_at = (end - timedelta(hours=2)).replace(tzinfo=None)
    db.session.commit()

    items = summarize.items_in_scope(summary)
    assert len(items) == 2


def test_resolve_range_ignores_failed_run_end(app, db):
    from app.models import SummaryRun, utcnow

    summary = Summary(
        user_id=1, name="Daily", type_key="app_page",
        scope_mode="fixed_period", period="day", params={"group_by_tag": False},
    )
    db.session.add(summary)
    db.session.commit()

    now = utcnow()
    successful = SummaryRun(
        summary_id=summary.id, status="ok", content="<p>ok</p>",
        range_end=(now - timedelta(hours=6)).replace(tzinfo=None),
    )
    db.session.add(successful)
    db.session.commit()

    # A failed retry attempt with a more recent range_end must not become the
    # start of the next window — that would shrink scope down to whatever's
    # arrived since the failure (often nothing), which is the exact bug this
    # guards against.
    failed = SummaryRun(
        summary_id=summary.id, status="failed", error_message="boom",
        range_end=(now - timedelta(minutes=1)).replace(tzinfo=None),
    )
    db.session.add(failed)
    db.session.commit()

    start, _end = summarize.resolve_range(summary)
    assert start == successful.range_end.replace(tzinfo=start.tzinfo)


def test_cut_due_editions_does_not_skip_when_latest_run_failed(app, db):
    from app.services.summarize import cut_due_editions
    from app.models import SummaryRun, utcnow

    summary = Summary(
        user_id=1, name="Daily", type_key="app_page",
        scope_mode="fixed_period", period="day", params={"group_by_tag": False},
    )
    db.session.add(summary)
    db.session.commit()

    _, expected_end = summarize.resolve_range(summary)
    failed = SummaryRun(
        summary_id=summary.id, status="failed", error_message="boom",
        range_end=expected_end.replace(tzinfo=None),
    )
    db.session.add(failed)
    db.session.commit()

    cut = cut_due_editions(force=True)
    assert cut == 1
    assert SummaryRun.query.filter_by(summary_id=summary.id, status="ok").count() == 1


def test_build_summary_records_run(app, db, sample_items):
    summary = Summary(
        user_id=1, name="Daily", type_key="app_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(summary)
    db.session.commit()
    artifact, items, run = summarize.build_summary(summary)
    assert artifact.kind == "html"
    assert summary.runs.count() == 1
