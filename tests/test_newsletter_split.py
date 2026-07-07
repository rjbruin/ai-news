import pytest

from app.models import Alert, ApiKey, IngestRun, NewsItem, Source, User
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
    assert "2 newsletters checked" in mailbox.last_status


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


# ───────────────────────── subscription confirmation ─────────────────────────
def _queued_chat_json(*responses):
    """Stand-in for openrouter.chat_json that returns pre-programmed responses
    in call order, so a test can script a multi-step LLM conversation."""
    calls = list(responses)

    def _fn(messages, *, schema=None, model=None, api_key=None, temperature=0.2,
            timeout=60.0, usage_hook=None):
        if usage_hook:
            usage_hook({"total_tokens": 10, "cost": 0.0001})
        assert calls, "no more queued chat_json responses"
        return calls.pop(0)

    return _fn


@pytest.fixture
def requester(db):
    u = User(username="requester", email="requester@example.com", email_verified=True, approved=True)
    u.set_password("password123")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def pending_subscription(db, mailbox, requester):
    key = ApiKey(owner_user_id=requester.id, label="Alice's key")
    key.set_key("sk-or-alice")
    db.session.add(key)
    db.session.commit()
    source = Source(
        type_key="imap_newsletter", name="TLDR AI",
        owner_user_id=requester.id, api_key_id=key.id, parent_source_id=mailbox.id,
        config={"newsletter_domain": "tldrnewsletter.com", "newsletter_name": "TLDR AI"},
        subscription_status="waiting_confirmation", enabled=True,
    )
    db.session.add(source)
    db.session.commit()
    return source


def test_no_click_needed_marks_subscribed_and_ingests_content(
    db, app, mailbox, pending_subscription, fake_newsletter_source, monkeypatch,
):
    fake_newsletter_source.docs = [_doc("1", "TLDR AI <news@tldrnewsletter.com>", "Issue #1")]
    monkeypatch.setattr(
        ingest.openrouter, "chat_json",
        _queued_chat_json({"requires_click": False, "confirmation_url": ""}),
    )

    stats = ingest.ingest_source(mailbox)

    db.session.refresh(pending_subscription)
    assert pending_subscription.subscription_status == "subscribed"
    assert pending_subscription.config.get("newsletter_sender") == "news@tldrnewsletter.com"
    assert stats["new_items"] == 1  # the email is also real content, so it's ingested
    assert NewsItem.query.filter_by(source_id=pending_subscription.id).count() == 1

    alert = Alert.query.filter_by(user_id=pending_subscription.owner_user_id).first()
    assert alert is not None
    assert "now active" in alert.message


def test_click_required_and_succeeds(
    db, app, mailbox, pending_subscription, fake_newsletter_source, monkeypatch,
):
    fake_newsletter_source.docs = [_doc("1", "TLDR AI <news@tldrnewsletter.com>", "Please confirm")]
    monkeypatch.setattr(
        ingest.openrouter, "chat_json",
        _queued_chat_json(
            {"requires_click": True, "confirmation_url": "https://confirm.example/abc"},
            {"confirmed": True},
        ),
    )
    monkeypatch.setattr(ingest, "_fetch_confirmation_page", lambda url: "You are now subscribed!")

    stats = ingest.ingest_source(mailbox)

    db.session.refresh(pending_subscription)
    assert pending_subscription.subscription_status == "subscribed"
    assert stats["new_items"] == 0  # the confirmation email itself isn't newsletter content
    alert = Alert.query.filter_by(user_id=pending_subscription.owner_user_id).first()
    assert alert is not None and "now active" in alert.message


def test_click_required_and_fails_notifies_owner_and_admins(
    db, app, admin, mailbox, pending_subscription, fake_newsletter_source, monkeypatch,
):
    fake_newsletter_source.docs = [_doc("1", "TLDR AI <news@tldrnewsletter.com>", "Please confirm")]
    monkeypatch.setattr(
        ingest.openrouter, "chat_json",
        _queued_chat_json(
            {"requires_click": True, "confirmation_url": "https://confirm.example/abc"},
            {"confirmed": False},
        ),
    )
    monkeypatch.setattr(ingest, "_fetch_confirmation_page", lambda url: "Something went wrong")

    ingest.ingest_source(mailbox)

    db.session.refresh(pending_subscription)
    assert pending_subscription.subscription_status == "failed"

    owner_alert = Alert.query.filter_by(user_id=pending_subscription.owner_user_id).first()
    assert owner_alert is not None
    assert "could not confirm" in owner_alert.message

    admin_alert = Alert.query.filter_by(user_id=admin.id).first()
    assert admin_alert is not None
    assert "manual" in admin_alert.message.lower()


def test_click_required_without_url_fails(
    db, mailbox, pending_subscription, fake_newsletter_source, monkeypatch,
):
    fake_newsletter_source.docs = [_doc("1", "TLDR AI <news@tldrnewsletter.com>", "Please confirm")]
    monkeypatch.setattr(
        ingest.openrouter, "chat_json",
        _queued_chat_json({"requires_click": True, "confirmation_url": ""}),
    )

    ingest.ingest_source(mailbox)

    db.session.refresh(pending_subscription)
    assert pending_subscription.subscription_status == "failed"


def test_subscribed_child_is_not_reevaluated(
    db, mailbox, pending_subscription, fake_newsletter_source, monkeypatch,
):
    pending_subscription.subscription_status = "subscribed"
    pending_subscription.config = {
        **pending_subscription.config, "newsletter_sender": "news@tldrnewsletter.com",
    }
    db.session.commit()

    fake_newsletter_source.docs = [_doc("1", "TLDR AI <news@tldrnewsletter.com>", "Issue #2")]

    def _explode(*a, **k):
        raise AssertionError("chat_json should not be called for an already-subscribed sender")

    monkeypatch.setattr(ingest.openrouter, "chat_json", _explode)

    stats = ingest.ingest_source(mailbox)
    assert stats["new_items"] == 1  # normal content ingestion, no confirmation check


# ───────────────────────── web: self-service newsletter requests ─────────────────────────
def test_seed_type_hidden_for_non_admin(auth_client, user, db):
    user.approved = True
    db.session.commit()
    resp = auth_client.get("/sources/new")
    assert b'value="seed"' not in resp.data


def test_seed_type_visible_for_admin(admin_client):
    resp = admin_client.get("/sources/new")
    assert b'value="seed"' in resp.data


def test_newsletter_request_creates_pending_subscription(auth_client, db, user, mailbox):
    user.approved = True
    db.session.commit()
    key = ApiKey(owner_user_id=user.id, label="Mine")
    key.set_key("sk-or-x")
    db.session.add(key)
    db.session.commit()

    resp = auth_client.post(
        "/sources/new",
        data={
            "type_key": "imap_newsletter",
            "newsletter_name": "Import AI",
            "newsletter_domain": "substack.com",
            "api_key_id": str(key.id),
        },
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/sources?subscribe=")

    child = Source.query.filter_by(name="Import AI").first()
    assert child is not None
    assert child.parent_source_id == mailbox.id
    assert child.owner_user_id == user.id
    assert child.subscription_status == "waiting_confirmation"
    assert child.config["newsletter_domain"] == "substack.com"


def test_newsletter_request_without_mailbox_flashes_error(auth_client, db, user):
    user.approved = True
    db.session.commit()
    key = ApiKey(owner_user_id=user.id, label="Mine")
    key.set_key("sk-or-x")
    db.session.add(key)
    db.session.commit()

    resp = auth_client.post(
        "/sources/new",
        data={
            "type_key": "imap_newsletter", "newsletter_name": "X", "newsletter_domain": "x.com",
            "api_key_id": str(key.id),
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"No newsletter mailbox is configured" in resp.data
    assert Source.query.filter_by(name="X").first() is None


def test_poll_confirmation_route_rate_limited(auth_client, db, user, pending_subscription, monkeypatch):
    pending_subscription.owner_user_id = user.id
    db.session.commit()

    calls = {"n": 0}
    def _fake_ingest_source(source):
        calls["n"] += 1
        return {}
    monkeypatch.setattr(ingest, "ingest_source", _fake_ingest_source)

    resp = auth_client.post(f"/sources/{pending_subscription.id}/poll-confirmation")
    assert resp.status_code == 200
    assert calls["n"] == 1

    # Second call right away should be rate-limited server-side and not re-poll.
    from app.models import utcnow
    pending_subscription.parent_source.last_polled_at = utcnow()
    db.session.commit()
    resp2 = auth_client.post(f"/sources/{pending_subscription.id}/poll-confirmation")
    assert resp2.status_code == 200
    assert calls["n"] == 1


def test_poll_confirmation_requires_ownership(client, db, admin, pending_subscription):
    other = User(username="bob", email="bob@example.com", email_verified=True)
    other.set_password("password123")
    db.session.add(other)
    db.session.commit()
    client.post(
        "/auth/login",
        data={"email": other.email, "password": "password123", "submit": "Sign in"},
        follow_redirects=True,
    )
    resp = client.post(f"/sources/{pending_subscription.id}/poll-confirmation")
    assert resp.status_code == 403


def test_admin_mark_subscribed(admin_client, db, pending_subscription):
    resp = admin_client.post(
        f"/admin/sources/{pending_subscription.id}/mark-subscribed", follow_redirects=True,
    )
    assert resp.status_code == 200
    db.session.refresh(pending_subscription)
    assert pending_subscription.subscription_status == "subscribed"
    alert = Alert.query.filter_by(user_id=pending_subscription.owner_user_id).first()
    assert alert is not None


# ───────────────────────── ignored senders ─────────────────────────
def test_ignored_sender_skipped_during_polling(db, mailbox, fake_newsletter_source):
    from app.models import IgnoredSender

    db.session.add(IgnoredSender(mailbox_source_id=mailbox.id, email="spam@example.com"))
    db.session.commit()

    fake_newsletter_source.docs = [_doc("1", "Spam Co <spam@example.com>", "Buy now")]
    stats = ingest.ingest_source(mailbox)

    assert stats["new_items"] == 0
    assert stats["skipped"] == 1
    assert mailbox.children.count() == 0


def test_ignored_sender_excluded_from_reindex(db, app, mailbox, fake_newsletter_source, monkeypatch):
    from app.models import IgnoredSender

    app.config["OPENROUTER_API_KEY"] = "sk-or-global-test"
    db.session.add(IgnoredSender(mailbox_source_id=mailbox.id, email="spam@example.com"))
    db.session.commit()
    fake_newsletter_source.scan_pairs = [
        ("Spam Co <spam@example.com>", "Buy now"),
        ("TLDR AI <news@tldrnewsletter.com>", "Issue #1"),
    ]

    def _classify_all(messages, *, schema=None, model=None, api_key=None, temperature=0.2,
                       timeout=60.0, usage_hook=None):
        # Would wrongly classify both as newsletters if the ignored sender
        # weren't filtered out before this call.
        return {"newsletters": ["spam@example.com", "news@tldrnewsletter.com"]}

    monkeypatch.setattr(ingest.openrouter, "chat_json", _classify_all)

    stats = ingest.reindex_newsletter_mailbox(mailbox)
    assert stats["unique_senders"] == 1  # spam@example.com filtered before classification
    assert mailbox.children.count() == 1
    assert mailbox.children.first().name == "TLDR AI"


def test_admin_ignore_deletes_source_and_records_sender(admin_client, db, mailbox, fake_newsletter_source):
    from app.models import IgnoredSender

    fake_newsletter_source.docs = [_doc("1", "Spam Co <spam@example.com>", "Buy now")]
    ingest.ingest_source(mailbox)
    child = mailbox.children.first()
    assert child is not None

    resp = admin_client.post(f"/admin/sources/{child.id}/ignore", follow_redirects=True)
    assert resp.status_code == 200
    assert b"will be ignored" in resp.data

    assert db.session.get(Source, child.id) is None
    row = IgnoredSender.query.filter_by(mailbox_source_id=mailbox.id, email="spam@example.com").first()
    assert row is not None
    assert row.display_name == "Spam Co"


def test_admin_ignore_rejects_non_subscription(admin_client, db, mailbox):
    resp = admin_client.post(f"/admin/sources/{mailbox.id}/ignore", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Only newsletter subscriptions" in resp.data


def test_admin_ignore_rejects_pending_without_sender(admin_client, db, pending_subscription):
    resp = admin_client.post(f"/admin/sources/{pending_subscription.id}/ignore", follow_redirects=True)
    assert resp.status_code == 200
    assert b"hasn&#39;t received any mail yet" in resp.data or b"hasn't received any mail yet" in resp.data


def test_admin_un_ignore(admin_client, db, mailbox):
    from app.models import IgnoredSender

    row = IgnoredSender(mailbox_source_id=mailbox.id, email="spam@example.com")
    db.session.add(row)
    db.session.commit()
    row_id = row.id

    resp = admin_client.post(f"/admin/ignored-senders/{row_id}/delete", follow_redirects=True)
    assert resp.status_code == 200
    assert db.session.get(IgnoredSender, row_id) is None


# ───────────────────────── domain matching (reindex + regular poll) ─────────────────────────
def test_regular_poll_matches_pending_by_subdomain(
    db, app, mailbox, pending_subscription, fake_newsletter_source, monkeypatch,
):
    """The user typed 'tldrnewsletter.com' but the newsletter actually sends
    from a subdomain — should still match the pending request, not create a
    second source."""
    fake_newsletter_source.docs = [_doc("1", "TLDR AI <news@mail.tldrnewsletter.com>", "Issue #1")]
    monkeypatch.setattr(
        ingest.openrouter, "chat_json",
        _queued_chat_json({"requires_click": False, "confirmation_url": ""}),
    )

    ingest.ingest_source(mailbox)

    assert mailbox.children.count() == 1
    db.session.refresh(pending_subscription)
    assert pending_subscription.subscription_status == "subscribed"
    assert pending_subscription.config.get("newsletter_sender") == "news@mail.tldrnewsletter.com"


def test_reindex_matches_pending_subscription_instead_of_duplicating(
    db, app, mailbox, pending_subscription, fake_newsletter_source, monkeypatch,
):
    """Regression test: reindexing a mailbox used to ignore pending
    subscriptions entirely and always create a brand-new Source, producing a
    duplicate ("name@example.com") alongside the user's original request."""
    app.config["OPENROUTER_API_KEY"] = "sk-or-global-test"
    fake_newsletter_source.scan_pairs = [
        ("TLDR AI <news@tldrnewsletter.com>", "Issue #1"),
    ]
    monkeypatch.setattr(
        ingest.openrouter, "chat_json", _fake_chat_json(["news@tldrnewsletter.com"]),
    )

    stats = ingest.reindex_newsletter_mailbox(mailbox)

    assert mailbox.children.count() == 1  # no duplicate created
    assert stats["new_subscriptions"] == 0  # matched into the existing pending one
    db.session.refresh(pending_subscription)
    assert pending_subscription.config.get("newsletter_sender") == "news@tldrnewsletter.com"
    # Reindex only has headers, not the email body — it can't run confirmation
    # detection, so status is deliberately left alone for the next real poll.
    assert pending_subscription.subscription_status == "waiting_confirmation"


def test_domain_normalization_tolerates_protocol_and_www(db, mailbox, requester):
    from app.models import ApiKey, Source as SourceModel

    api_key = ApiKey(owner_user_id=requester.id, label="k")
    api_key.set_key("sk-or-x")
    db.session.add(api_key)
    db.session.commit()
    pending = SourceModel(
        type_key="imap_newsletter", name="Weird Domain", owner_user_id=requester.id,
        api_key_id=api_key.id, parent_source_id=mailbox.id,
        config={"newsletter_domain": ingest.normalize_domain("https://www.example.com/")},
        subscription_status="waiting_confirmation", enabled=True,
    )
    db.session.add(pending)
    db.session.commit()

    pending_by_domain = ingest._pending_children_by_domain(mailbox)
    match = ingest._find_pending_by_domain(pending_by_domain, "news@example.com")
    assert match is not None and match.id == pending.id


# ───────────────────────── UI: instructions button + status wording ─────────────────────────
def test_instructions_button_hidden_once_subscribed(admin_client, db, pending_subscription):
    resp = admin_client.get("/sources")
    assert b"Instructions" in resp.data

    pending_subscription.subscription_status = "subscribed"
    db.session.commit()

    resp = admin_client.get("/sources")
    assert b"Instructions" not in resp.data


def test_newsletter_status_wording_is_terse(db, mailbox, fake_newsletter_source):
    fake_newsletter_source.docs = [_doc("1", "TLDR AI <news@tldrnewsletter.com>", "Issue #1")]
    ingest.ingest_source(mailbox)
    child = mailbox.children.first()
    assert child.last_status == "1 new item, 1 checked"
    assert "already seen" not in child.last_status
    assert "docs" not in child.last_status
