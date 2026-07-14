"""Regression tests for app.services.podcast._doc_to_text — converting the
agent's block document into a plain-text script for the LLM podcast writer.
Every block type must strip HTML before it reaches the script; two block
types (quick_hits, more_news) were missing this, either leaking literal
<strong>/<em> markup into the spoken script (quick_hits) or silently
dropping every item because of a stale field-name mismatch (more_news)."""
from app.services.podcast import _doc_to_text


def test_quick_hits_strips_html_tags():
    doc = [{"type": "quick_hits", "items": [
        {"text": "OpenAI ships <strong>GPT-6</strong> today."},
        {"text": "See <em>the announcement</em>."},
    ]}]
    text = _doc_to_text(doc)
    assert "<strong>" not in text and "</strong>" not in text
    assert "<em>" not in text and "</em>" not in text
    assert "OpenAI ships GPT-6 today." in text
    # _strip_html replaces each tag with a space before collapsing
    # whitespace (matching existing behavior for every other block type),
    # so the closing </em> leaves a space before the final period.
    assert "See the announcement ." in text


def test_quick_hits_skips_empty_items():
    doc = [{"type": "quick_hits", "items": [{"text": ""}, {"text": "Real hit"}]}]
    text = _doc_to_text(doc)
    lines = [l for l in text.splitlines() if l.strip()]
    assert lines == ["Quick hits:", "- Real hit"]


def test_more_news_uses_headline_field():
    doc = [{"type": "more_news", "items": [
        {"headline": "Minor release", "url": "https://x.test"},
        {"headline": "Another <em>update</em>"},
    ]}]
    text = _doc_to_text(doc)
    assert "- Minor release" in text
    assert "- Another update" in text
    assert "<em>" not in text


def test_more_news_skips_items_with_no_headline():
    doc = [{"type": "more_news", "items": [{"headline": ""}, {"headline": "Kept"}]}]
    text = _doc_to_text(doc)
    lines = [l for l in text.splitlines() if l.strip()]
    assert lines == ["- Kept"]
