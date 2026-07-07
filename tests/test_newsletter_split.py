import pytest

from app.models import ApiKey, IngestRun, NewsItem, Source
from app.services import ingest
from app.sources import registry as source_registry
from app.sources.base import ExtractedItem, NewsSource, RawDocument


class FakeNewsletterSource(NewsSource):
    """Stands in for ImapNewsletterSource: fetch() returns whatever the test
    stashed on the class, extract() is a trivial 1:1 mapping (no LLM)."""

    type_key = "imap_newsletter"
    label = "Fake newsletter mailbox"

    docs: list[RawDocument] = []
    scan_pairs: list[tuple[str, str]] = []

    def fetch(self, since):
        return list(FakeNewsletterSource.docs)

    def extract(self, doc):
        return [ExtractedItem(title=doc.subject, summary=doc.text, url=f"http://x/{doc.external_id}")]

    def scan_senders(self):
        return list(FakeNewsletterSource.scan_pairs)


@pytest.fixture
def fake_newsletter_source():
    """Registers FakeNewsletterSource under the real 'imap_newsletter' key for
    the duration of a test, restoring the real plugin class afterwards so
    other test modules aren't affected by the shared registry global."""
    original = source_registry.get("imap_newsletter")
    source_registry.register(FakeNewsletterSource)
    FakeNewsletterSource.docs = []
    FakeNewsletterSource.scan_pairs = []
    yield FakeNewsletterSource
    if original is not None:
        source_registry.register(original)


@pytest.fixture
def mailbox(db, fake_newsletter_source):
    key = ApiKey(label="Test key", provider="openrouter")
    key.set_key("sk-or-test")
    db.session.add(key)
    db.session.commit()
    source = Source(
        type_key="imap_newsletter", name="Newsletters mailbox",
        config={"host": "x"}, api_key_id=key.id, enabled=True,
    )
    db.session.add(source)
    db.session.commit()
    return source


def _doc(external_id, sender, subject="A story"):
    return RawDocument(
        external_id=external_id, text=f"body for {subject}", subject=subject,
        meta={"from": sender},
    )


def test_mailbox_splits_into_newsletter_children(db, mailbox, fake_newsletter_source):
    fake_newsletter_source.docs = [
        _doc("1", "TLDR AI <news@tldrnewsletter.com>", "Story A"),
        _doc("2", "Import AI <import-ai@substack.com>", "Story B"),
    ]

    stats = ingest.ingest_source(mailbox)

    children = mailbox.children.all()
    assert len(children) == 2
    names = {c.name for c in children}
    assert names == {"TLDR AI", "Import AI"}
    assert all(c.parent_source_id == mailbox.id for c in children)
    assert all(c.api_key_id == mailbox.api_key_id for c in children)
    assert stats["new_items"] == 2
    assert NewsItem.query.count() == 2

    tldr = next(c for c in children if c.name == "TLDR AI")
    item = NewsItem.query.filter_by(source_id=tldr.id).first()
    assert item is not None
    assert item.title == "Story A"
    assert "2 newsletter(s) seen" in mailbox.last_status


def test_second_poll_dedups_and_reuses_children(db, mailbox, fake_newsletter_source):
    fake_newsletter_source.docs = [_doc("1", "TLDR AI <news@tldrnewsletter.com>")]
    ingest.ingest_source(mailbox)
    assert mailbox.children.count() == 1

    # Same email seen again (e.g. IMAP re-fetch within the lookback window).
    stats = ingest.ingest_source(mailbox)
    assert mailbox.children.count() == 1  # no duplicate subscription
    assert stats["new_items"] == 0
    assert NewsItem.query.count() == 1


def test_disabled_child_is_skipped_without_new_items(db, mailbox, fake_newsletter_source):
    fake_newsletter_source.docs = [_doc("1", "TLDR AI <news@tldrnewsletter.com>")]
    ingest.ingest_source(mailbox)
    child = mailbox.children.first()
    child.enabled = False
    db.session.commit()

    fake_newsletter_source.docs = [_doc("2", "TLDR AI <news@tldrnewsletter.com>", "Story B")]
    stats = ingest.ingest_source(mailbox)

    assert stats["new_items"] == 0
    assert NewsItem.query.count() == 1  # only the pre-retraction item
    assert IngestRun.query.filter_by(source_id=child.id, external_id="2").first() is not None
    assert "retracted" in child.last_status


def test_children_are_not_independently_polled(db, mailbox, fake_newsletter_source):
    fake_newsletter_source.docs = [_doc("1", "TLDR AI <news@tldrnewsletter.com>")]
    ingest.ingest_source(mailbox)
    child = mailbox.children.first()

    due = Source.query.filter_by(enabled=True, parent_source_id=None).all()
    assert mailbox in due
    assert child not in due

    totals = ingest.ingest_all_due(force=True)
    assert totals["sources"] == 1  # only the mailbox counted, not the child


def test_admin_cannot_poll_or_reset_a_newsletter_child(admin_client, db, mailbox, fake_newsletter_source):
    fake_newsletter_source.docs = [_doc("1", "TLDR AI <news@tldrnewsletter.com>")]
    ingest.ingest_source(mailbox)
    child = mailbox.children.first()

    resp = admin_client.post(f"/admin/sources/{child.id}/poll", follow_redirects=True)
    assert resp.status_code == 200
    assert b"mailbox instead" in resp.data

    resp = admin_client.post(f"/admin/sources/{child.id}/reset", follow_redirects=True)
    assert resp.status_code == 200
    assert b"mailbox instead" in resp.data


# ───────────────────────── reindex ─────────────────────────
def _fake_chat_json(newsletter_addrs):
    """Returns a chat_json stand-in that always classifies the given
    addresses as newsletters and records that it was called with usage."""
    def _fn(messages, *, schema=None, model=None, api_key=None, temperature=0.2,
            timeout=60.0, usage_hook=None):
        if usage_hook:
            usage_hook({"total_tokens": 42, "cost": 0.001})
        return {"newsletters": list(newsletter_addrs)}
    return _fn


def test_reindex_scans_whole_mailbox_and_creates_children(
    db, app, mailbox, fake_newsletter_source, monkeypatch
):
    app.config["OPENROUTER_API_KEY"] = "sk-or-global-test"
    fake_newsletter_source.scan_pairs = [
        ("TLDR AI <news@tldrnewsletter.com>", "Issue #1"),
        ("TLDR AI <news@tldrnewsletter.com>", "Issue #2"),
        ("Import AI <import-ai@substack.com>", "Weekly digest"),
        ("Some Person <person@example.com>", "Re: dinner plans"),
    ]
    monkeypatch.setattr(
        ingest.openrouter, "chat_json",
        _fake_chat_json(["news@tldrnewsletter.com", "import-ai@substack.com"]),
    )

    stats = ingest.reindex_newsletter_mailbox(mailbox)

    assert stats["messages_scanned"] == 4
    assert stats["unique_senders"] == 3
    assert stats["newsletters_detected"] == 2
    assert stats["new_subscriptions"] == 2

    children = {c.name for c in mailbox.children}
    assert children == {"TLDR AI", "Import AI"}
    # No items were ingested — this is a headers-only discovery pass.
    assert NewsItem.query.count() == 0

    from app.models import ApiKeyUsage
    global_key = ApiKey.query.filter_by(is_global=True).first()
    usage = ApiKeyUsage.query.filter_by(source_id=mailbox.id, kind="reindex").first()
    assert usage is not None
    assert usage.api_key_id == global_key.id
    assert usage.tokens == 42


def test_reindex_is_idempotent(db, app, mailbox, fake_newsletter_source, monkeypatch):
    app.config["OPENROUTER_API_KEY"] = "sk-or-global-test"
    fake_newsletter_source.scan_pairs = [("TLDR AI <news@tldrnewsletter.com>", "Issue #1")]
    monkeypatch.setattr(
        ingest.openrouter, "chat_json", _fake_chat_json(["news@tldrnewsletter.com"]),
    )

    first = ingest.reindex_newsletter_mailbox(mailbox)
    assert first["new_subscriptions"] == 1

    second = ingest.reindex_newsletter_mailbox(mailbox)
    assert second["new_subscriptions"] == 0
    assert mailbox.children.count() == 1


def test_reindex_rejects_non_mailbox_sources(db, mailbox, fake_newsletter_source):
    fake_newsletter_source.scan_pairs = [("TLDR AI <news@tldrnewsletter.com>", "Issue #1")]
    child = Source(
        type_key="imap_newsletter", name="Child", parent_source_id=mailbox.id,
        api_key_id=mailbox.api_key_id, config={"newsletter_sender": "x"},
    )
    db.session.add(child)
    db.session.commit()

    with pytest.raises(ValueError):
        ingest.reindex_newsletter_mailbox(child)


def test_admin_reindex_route(admin_client, db, app, mailbox, fake_newsletter_source, monkeypatch):
    app.config["OPENROUTER_API_KEY"] = "sk-or-global-test"
    fake_newsletter_source.scan_pairs = [
        ("TLDR AI <news@tldrnewsletter.com>", "Issue #1"),
    ]
    monkeypatch.setattr(
        ingest.openrouter, "chat_json", _fake_chat_json(["news@tldrnewsletter.com"]),
    )

    resp = admin_client.post(
        f"/admin/sources/{mailbox.id}/reindex-newsletters", follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Reindexed" in resp.data
    assert mailbox.children.count() == 1


def test_admin_reindex_rejects_child_source(admin_client, db, mailbox, fake_newsletter_source):
    child = Source(
        type_key="imap_newsletter", name="Child", parent_source_id=mailbox.id,
        api_key_id=mailbox.api_key_id, config={"newsletter_sender": "x"}, enabled=True,
    )
    db.session.add(child)
    db.session.commit()

    resp = admin_client.post(
        f"/admin/sources/{child.id}/reindex-newsletters", follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"top-level" in resp.data
