from app.sources import registry as source_registry
from app.sources.base import NewsSource

_RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Example Blog</title>
  <item>
    <title>First post</title>
    <link>https://example.com/first-post</link>
    <guid>https://example.com/first-post</guid>
    <description>&lt;p&gt;Hello &lt;b&gt;world&lt;/b&gt;.&lt;/p&gt;</description>
    <pubDate>Mon, 06 Jul 2026 12:00:00 GMT</pubDate>
  </item>
  <item>
    <title>No-link post</title>
    <description>Just some text, no link or guid at all.</description>
    <pubDate>Tue, 07 Jul 2026 08:00:00 GMT</pubDate>
  </item>
</channel></rss>
"""

_NOT_A_FEED = "<html><body>Fish & Chips <br> are great</body></html>"


class _FakeResponse:
    def __init__(self, content: str):
        self.content = content.encode("utf-8")

    def raise_for_status(self):
        pass


def test_source_registry_discovers_rss_feed(app):
    types = source_registry.all_types()
    assert "rss_feed" in types
    assert issubclass(types["rss_feed"], NewsSource)


def test_fetch_requires_url(app):
    src = source_registry.create("rss_feed", {})
    try:
        src.fetch(since=None)
        assert False, "expected RuntimeError for missing url"
    except RuntimeError as exc:
        assert "not configured" in str(exc)


def test_fetch_parses_entries(app, monkeypatch):
    monkeypatch.setattr(
        "app.sources.rss_feed.httpx.get",
        lambda *a, **k: _FakeResponse(_RSS_XML),
    )
    src = source_registry.create("rss_feed", {"url": "https://example.com/feed.xml"})
    docs = src.fetch(since=None)

    assert len(docs) == 2
    first, second = docs

    assert first.external_id == "https://example.com/first-post"
    assert first.subject == "First post"
    assert first.meta["link"] == "https://example.com/first-post"
    assert first.received_at is not None

    # Entry with no <guid>/<link> falls back to using its title as external_id.
    assert second.external_id == "No-link post"
    assert second.meta["link"] is None


def test_extract_builds_item_with_defaults(app, monkeypatch):
    monkeypatch.setattr(
        "app.sources.rss_feed.httpx.get",
        lambda *a, **k: _FakeResponse(_RSS_XML),
    )
    src = source_registry.create("rss_feed", {"url": "https://example.com/feed.xml"})
    doc, doc_no_link = src.fetch(since=None)

    item = src.extract(doc)[0]
    assert item.title == "First post"
    assert item.url == "https://example.com/first-post"
    assert item.item_type == "news"
    assert "Hello world." in item.summary
    assert "<b>" not in item.summary
    assert item.full_text is None  # has a URL, so no full_text needed

    item2 = src.extract(doc_no_link)[0]
    assert item2.url is None
    assert item2.full_text == item2.summary  # URL-less items keep full text


def test_extract_honors_item_type_override(app, monkeypatch):
    monkeypatch.setattr(
        "app.sources.rss_feed.httpx.get",
        lambda *a, **k: _FakeResponse(_RSS_XML),
    )
    src = source_registry.create(
        "rss_feed", {"url": "https://example.com/feed.xml", "item_type": "blog"}
    )
    doc = src.fetch(since=None)[0]
    assert src.extract(doc)[0].item_type == "blog"


def test_extract_clamps_invalid_item_type(app, monkeypatch):
    monkeypatch.setattr(
        "app.sources.rss_feed.httpx.get",
        lambda *a, **k: _FakeResponse(_RSS_XML),
    )
    src = source_registry.create(
        "rss_feed", {"url": "https://example.com/feed.xml", "item_type": "nonsense"}
    )
    doc = src.fetch(since=None)[0]
    assert src.extract(doc)[0].item_type == "news"


def test_fetch_raises_on_non_feed_content(app, monkeypatch):
    monkeypatch.setattr(
        "app.sources.rss_feed.httpx.get",
        lambda *a, **k: _FakeResponse(_NOT_A_FEED),
    )
    src = source_registry.create("rss_feed", {"url": "https://example.com/not-a-feed"})
    try:
        src.fetch(since=None)
        assert False, "expected RuntimeError for unparsable content"
    except RuntimeError as exc:
        assert "Could not parse feed" in str(exc)


def test_entries_are_capped(app, monkeypatch):
    many_items = "".join(
        f"<item><title>Post {i}</title><link>https://example.com/{i}</link>"
        f"<guid>https://example.com/{i}</guid></item>"
        for i in range(80)
    )
    xml = f"<?xml version='1.0'?><rss version='2.0'><channel>{many_items}</channel></rss>"
    monkeypatch.setattr(
        "app.sources.rss_feed.httpx.get",
        lambda *a, **k: _FakeResponse(xml),
    )
    src = source_registry.create("rss_feed", {"url": "https://example.com/feed.xml"})
    docs = src.fetch(since=None)
    assert len(docs) == 50
