import pytest

from app.agent import render
from app.agent.blocks import (
    BlockValidationError,
    find_block,
    validate_document,
)


SAMPLE_DOC = [
    {"type": "edition_header", "title": "AI Daily", "subtitle": "What moved today", "date": "Monday June 29"},
    {"type": "intro", "markdown": "A *busy* day across labs and tooling."},
    {"type": "section", "title": "Models", "description": "New releases"},
    {
        "type": "story",
        "headline": "Claude 4 ships",
        "dek": "Extended thinking mode arrives.",
        "body": "Anthropic released **Claude 4** with a new reasoning mode.",
        "url": "https://example.com/claude4",
        "source": "Anthropic",
        "emphasis": "lead",
    },
    {"type": "callout", "variant": "trend", "title": "Reasoning everywhere", "markdown": "Three labs shipped reasoning modes this week."},
    {"type": "quick_hits", "title": "Also notable", "items": ["Plain string hit", {"text": "Linked hit", "url": "https://x.test"}]},
    {"type": "divider"},
    {"type": "quote", "text": "AI is eating software.", "attribution": "Someone"},
]


def test_validate_assigns_ids_and_defaults():
    doc = validate_document(SAMPLE_DOC)
    assert all(b["id"] for b in doc)
    story = next(b for b in doc if b["type"] == "story")
    assert story["emphasis"] == "lead"
    # quick_hits string normalised to {text, url}
    qh = next(b for b in doc if b["type"] == "quick_hits")
    assert qh["items"][0] == {"text": "Plain string hit", "url": ""}
    assert qh["items"][1]["url"] == "https://x.test"


def test_validate_preserves_existing_ids():
    doc = validate_document([{"type": "divider", "id": "keepme"}])
    assert doc[0]["id"] == "keepme"


def test_unknown_block_type_rejected():
    with pytest.raises(BlockValidationError):
        validate_document([{"type": "carousel", "title": "x"}])


def test_missing_required_field_rejected():
    with pytest.raises(BlockValidationError):
        validate_document([{"type": "story"}])  # missing headline


def test_bad_enum_rejected():
    with pytest.raises(BlockValidationError):
        validate_document([{"type": "story", "headline": "h", "emphasis": "huge"}])


def test_item_sources_drops_bare_homepage_url():
    """A source URL with no path (e.g. the site's homepage) is almost never
    the actual article — the agent sometimes guesses it when it doesn't
    have the real link. Drop it rather than citing something misleading."""
    doc = validate_document([{
        "type": "item", "headline": "h", "subheader": "s", "summary": "body",
        "sources": ["https://theverge.com/", "https://theverge.com", "https://example.com/2026/07/20/real-article"],
    }])
    urls = [s["url"] for s in doc[0]["sources"]]
    assert urls == ["https://example.com/2026/07/20/real-article"]


def test_more_news_item_url_drops_bare_homepage():
    doc = validate_document([{
        "type": "more_news",
        "items": [{"headline": "Some update", "url": "https://theverge.com/"}],
    }])
    assert doc[0]["items"][0]["url"] == ""


def test_more_news_item_carries_item_id():
    doc = validate_document([{
        "type": "more_news",
        "items": [
            {"headline": "Plain string hit"},
            {"headline": "Tagged hit", "item_id": 42},
        ],
    }])
    items = doc[0]["items"]
    assert items[0]["item_id"] is None
    assert items[1]["item_id"] == 42


def test_item_escalated_from_quick_hit_flag():
    doc = validate_document([{
        "type": "item", "headline": "h", "subheader": "s", "summary": "body",
        "escalated_from_quick_hit": True,
    }])
    assert doc[0]["escalated_from_quick_hit"] is True
    # Defaults to False when omitted.
    doc2 = validate_document([{"type": "item", "headline": "h", "subheader": "s", "summary": "body"}])
    assert doc2[0]["escalated_from_quick_hit"] is False


def test_find_block():
    doc = validate_document([{"type": "divider", "id": "d1"}, {"type": "divider", "id": "d2"}])
    assert find_block(doc, "d2") == 1
    assert find_block(doc, "nope") is None


def test_render_html_produces_expected_markup(app):
    with app.app_context():
        html = render.render_html(SAMPLE_DOC)
    assert "AI Daily" in html
    assert 'href="https://example.com/claude4"' in html
    assert "Reasoning everywhere" in html
    assert "<blockquote" in html
    # markdown rendered: bold tag present
    assert "<strong>Claude 4</strong>" in html or "<b>Claude 4</b>" in html


def test_more_news_headline_strips_disallowed_markup(app):
    """Regression test: more_news headlines come from agent-generated text,
    which is ultimately derived from attacker-reachable ingested news
    content (see the prompt-injection hardening in app/llm/prompt_safety.py).
    A headline must never be able to inject a live element or leak an
    event-handler attribute — disallowed tags are stripped outright (not
    merely escaped to visible text, and not kept with only some attributes
    dropped, which `md`/`mdinline` would do since they allow <img>)."""
    doc = validate_document([
        {"type": "more_news", "items": [
            {"headline": "<img src=x onerror=alert(1)>", "url": "https://example.com/x"},
        ]},
    ])
    with app.app_context():
        html = render.render_html(doc)
    assert "<img" not in html
    assert "onerror" not in html


def test_more_news_headline_renders_emphasis(app):
    """The bug this guards against: more_news headlines are documented to
    allow <em>/<strong> for emphasis, but the field had no rendering filter
    at all, so Jinja's default autoescaping showed the literal tag text
    instead of bold/italic."""
    doc = validate_document([
        {"type": "more_news", "items": [
            {"headline": "OpenAI ships <strong>GPT-6</strong> today."},
        ]},
    ])
    with app.app_context():
        html = render.render_html(doc)
    assert "<strong>GPT-6</strong>" in html


def test_quick_hits_renders_inline_html_formatting(app):
    """Regression test: quick_hits item text previously went through Jinja's
    default auto-escaping with no filter at all, so agent-authored <strong>/
    <em> tags rendered as literal text instead of bold/italic."""
    doc = validate_document([
        {"type": "quick_hits", "items": [
            {"text": "OpenAI ships <strong>GPT-6</strong> today."},
            {"text": "See <em>the announcement</em>.", "url": "https://example.com/y"},
        ]},
    ])
    with app.app_context():
        html = render.render_html(doc)
    assert "<strong>GPT-6</strong>" in html
    assert "<em>the announcement</em>" in html
    # Must not leak a stray wrapping <p> into the <li>/<a> (invalid nesting).
    assert "<p>" not in html


def test_quick_hits_sanitizes_disallowed_tags(app):
    """The mdinline filter must still run agent text through bleach, exactly
    like the block-level `md` filter — inline formatting is allowed, a
    script tag is not (its markup is stripped, even though bleach leaves
    the now-inert text content behind)."""
    doc = validate_document([
        {"type": "quick_hits", "items": [
            {"text": "<script>alert(1)</script>Hi"},
        ]},
    ])
    with app.app_context():
        html = render.render_html(doc)
    assert "<script>" not in html
    assert "<script" not in html
