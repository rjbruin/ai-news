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
