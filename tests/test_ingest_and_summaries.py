from datetime import datetime, timedelta, timezone

from app.models import NewsItem, Source, Summary
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


def test_ingest_source_creates_and_tags_items(app, db, sample_tags):
    app.config["TAGGING_MODE"] = "nb_only"
    app.config["NB_CONFIDENCE_THRESHOLD"] = 0.05
    source_registry.register(FakeSource)
    source = Source(type_key="fake", name="Fake", config={})
    db.session.add(source)
    db.session.commit()

    stats = ingest.ingest_source(source)
    assert stats["new_items"] == 2
    assert NewsItem.query.count() == 2


def test_ingest_dedups(app, db):
    source_registry.register(FakeSource)
    source = Source(type_key="fake", name="Fake", config={})
    db.session.add(source)
    db.session.commit()
    ingest.ingest_source(source)
    ingest.ingest_source(source)  # same items again
    assert NewsItem.query.count() == 2  # no duplicates


def test_summary_fixed_period_scope(app, db, sample_items):
    # Backdate items to yesterday so they fall before the daily release cutoff (08:00 UTC).
    yesterday = datetime.now(timezone.utc) - timedelta(hours=20)
    for item in sample_items:
        item.fetched_at = yesterday.replace(tzinfo=None)
    db.session.commit()

    summary = Summary(
        user_id=1, name="Daily", type_key="app_page",
        scope_mode="fixed_period", period="day", params={"group_by_tag": False},
    )
    db.session.add(summary)
    db.session.commit()
    items = summarize.items_in_scope(summary)
    assert len(items) == 2


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
