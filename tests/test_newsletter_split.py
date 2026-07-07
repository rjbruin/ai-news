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

    def fetch(self, since):
        return list(FakeNewsletterSource.docs)

    def extract(self, doc):
        return [ExtractedItem(title=doc.subject, summary=doc.text, url=f"http://x/{doc.external_id}")]


@pytest.fixture
def fake_newsletter_source():
    """Registers FakeNewsletterSource under the real 'imap_newsletter' key for
    the duration of a test, restoring the real plugin class afterwards so
    other test modules aren't affected by the shared registry global."""
    original = source_registry.get("imap_newsletter")
    source_registry.register(FakeNewsletterSource)
    FakeNewsletterSource.docs = []
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
