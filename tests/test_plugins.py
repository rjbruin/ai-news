from datetime import datetime

from app.sources import registry as source_registry
from app.sources.base import NewsSource, RawDocument
from app.sources.imap_newsletter import html_to_text
from app.summaries import registry as summary_registry
from app.summaries.base import NewsSummary


def test_source_registry_discovers_imap(app):
    types = source_registry.all_types()
    assert "imap_newsletter" in types
    assert issubclass(types["imap_newsletter"], NewsSource)


def test_summary_registry_discovers_app_page(app):
    types = summary_registry.all_types()
    assert "app_page" in types
    assert issubclass(types["app_page"], NewsSummary)


def test_source_create_returns_instance(app):
    src = source_registry.create("imap_newsletter", {"host": "x"})
    assert src is not None
    assert src.config["host"] == "x"


def test_unknown_type_returns_none(app):
    assert source_registry.create("nope") is None
    assert summary_registry.create("nope") is None


def test_html_to_text_strips_markup():
    html = "<html><body><h1>Hi</h1><script>bad()</script><p>News here</p></body></html>"
    text = html_to_text(html)
    assert "Hi" in text and "News here" in text
    assert "bad()" not in text


def test_app_page_summary_builds_html(app, sample_items):
    plugin = summary_registry.create("app_page")
    artifact = plugin.build(
        sample_items, {"group_by_tag": False}, range_end=datetime(2026, 1, 1)
    )
    assert artifact.kind == "html"
    assert "OpenAI releases new GPT model" in artifact.html
