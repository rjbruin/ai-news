from app.models import ApiKey, IngestRun, NewsItem, NewsItemTag, Source, Tag
from app.services import ingest


def _seed_source_with_items(db):
    key = ApiKey(label="Test key", provider="openrouter")
    key.set_key("sk-or-test")
    db.session.add(key)
    db.session.commit()
    source = Source(type_key="rss", name="Test Source", enabled=True, api_key_id=key.id)
    db.session.add(source)
    db.session.commit()

    tag = Tag(name="Reset Test Tag", scope="global")
    db.session.add(tag)
    db.session.commit()

    item = NewsItem(
        dedup_hash="h-reset-1", title="Item to be reset away", url="http://x/reset1",
        source_id=source.id,
    )
    db.session.add(item)
    db.session.commit()
    db.session.add(NewsItemTag(news_item_id=item.id, tag_id=tag.id, user_id=None, method="llm"))
    run = IngestRun(source_id=source.id, external_id="reset-1")
    db.session.add(run)
    db.session.commit()

    return source, item, tag


def test_source_reset_deletes_tag_links_before_items(admin_client, db, monkeypatch):
    source, item, tag = _seed_source_with_items(db)
    item_id = item.id

    monkeypatch.setattr(
        ingest, "ingest_source",
        lambda src: {"fetched": 0, "new_items": 0, "tagged": 0, "skipped": 0, "errors": 0},
    )

    resp = admin_client.post(f"/admin/sources/{source.id}/reset", follow_redirects=True)
    assert resp.status_code == 200

    assert NewsItem.query.filter_by(id=item_id).first() is None
    assert IngestRun.query.filter_by(source_id=source.id).count() == 0
    # The bug this guards against: a bulk NewsItem delete bypasses the ORM
    # cascade, leaving NewsItemTag rows whose news_item_id no longer
    # matches any NewsItem — source_reset must delete them explicitly.
    assert NewsItemTag.query.filter_by(tag_id=tag.id).count() == 0
