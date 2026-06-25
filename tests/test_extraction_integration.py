"""Integration tests for newsletter extraction.

These tests require a real OPENROUTER_API_KEY (read from .env) and at least one
saved newsletter email in tests/fixtures/newsletters/.

How to add fixtures
-------------------
Save newsletter emails as .eml files (File → Save As in most email clients, or
drag-and-drop from Mail.app to Finder) into tests/fixtures/newsletters/.  Plain
.txt files are also accepted — the entire file is treated as the email body.

How to run
----------
    pytest tests/test_extraction_integration.py -v -s

The -s flag prints each extracted item to stdout so you can inspect quality.
To skip these tests in CI (no API key), use:
    pytest -m "not integration"
"""
from __future__ import annotations

import email as _email
import email.policy
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "newsletters"
VALID_TYPES = {"paper", "announcement", "blog", "news", "tool", "opinion", "other"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fixture_paths() -> list[Path]:
    if not FIXTURES_DIR.exists():
        return []
    return sorted(
        p for p in FIXTURES_DIR.iterdir()
        if p.suffix in {".eml", ".txt"} and not p.name.startswith(".")
    )


def _load_doc(path: Path):
    """Parse an .eml or .txt file into a RawDocument."""
    from datetime import datetime, timezone
    from app.sources.base import RawDocument

    if path.suffix == ".eml":
        msg = _email.message_from_bytes(path.read_bytes(), policy=_email.policy.default)
        subject = str(msg.get("subject", path.stem))
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    body = part.get_content()
                    break
            if not body:
                for part in msg.walk():
                    if part.get_content_type() == "text/html":
                        from bs4 import BeautifulSoup
                        body = BeautifulSoup(part.get_content(), "html.parser").get_text(" ")
                        break
        else:
            body = msg.get_content()
    else:
        body = path.read_text(encoding="utf-8", errors="replace")
        subject = path.stem

    return RawDocument(
        external_id=path.name,
        text=body.strip(),
        subject=subject,
        received_at=datetime.now(tz=timezone.utc),
    )


# ---------------------------------------------------------------------------
# App fixture (keeps the real API key; skips if absent)
# ---------------------------------------------------------------------------

@pytest.fixture
def integration_app():
    from app import create_app
    from app.config import IntegrationTestConfig
    from app.extensions import db as _db

    app = create_app(IntegrationTestConfig)
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

_paths = _fixture_paths()

pytestmark = pytest.mark.integration


@pytest.mark.skipif(not _paths, reason="No newsletter fixtures in tests/fixtures/newsletters/")
@pytest.mark.parametrize("eml_path", _paths, ids=[p.name for p in _paths])
def test_extract_items_from_newsletter(integration_app, eml_path):
    """LLM extracts structurally valid items from each fixture email."""
    from app.llm import openrouter
    from app.sources.extract import extract_items

    if not openrouter.is_configured():
        pytest.skip("OPENROUTER_API_KEY not configured")

    doc = _load_doc(eml_path)
    items = extract_items(doc)

    print(f"\n── {eml_path.name}: {len(items)} item(s) ──")
    for item in items:
        print(f"  [{item.item_type:12s}] {item.title}")
        print(f"    one_liner : {item.one_liner}")
        print(f"    summary   : {(item.summary or '')[:120]}...")

    assert items, f"No items extracted from {eml_path.name}"
    for item in items:
        assert item.title.strip(), \
            f"Item has empty title in {eml_path.name}"
        assert item.item_type in VALID_TYPES, \
            f"item_type {item.item_type!r} is not one of {VALID_TYPES} in {eml_path.name}"
        assert item.one_liner and item.one_liner.strip(), \
            f"Missing one_liner for '{item.title}' in {eml_path.name}"
        assert item.summary and item.summary.strip(), \
            f"Missing summary for '{item.title}' in {eml_path.name}"
        assert item.one_liner.strip().lower() != item.title.strip().lower(), \
            f"one_liner just repeats the title for '{item.title}' in {eml_path.name}"


@pytest.mark.skipif(not _paths, reason="No newsletter fixtures in tests/fixtures/newsletters/")
def test_full_ingest_pipeline_from_newsletter(integration_app):
    """Full pipeline (fetch → LLM extract → persist → tag) using the first fixture."""
    from app.llm import openrouter
    from app.extensions import db
    from app.models import NewsItem, Source, Tag
    from app.services import ingest as ingest_svc
    from app.sources import registry as source_registry
    from app.sources.base import NewsSource

    if not openrouter.is_configured():
        pytest.skip("OPENROUTER_API_KEY not configured")

    eml_path = _paths[0]
    doc = _load_doc(eml_path)

    class _FixtureSource(NewsSource):
        type_key = "_fixture_integration"
        label = "Fixture (integration test)"

        def fetch(self, since):
            return [doc]

    tags = [
        Tag(name="LLMs", keywords=["language model", "gpt", "llm", "transformer"],
            explanation="Large language models.", scope="global"),
        Tag(name="Research", keywords=["paper", "arxiv", "study", "benchmark"],
            explanation="Research papers.", scope="global"),
        Tag(name="Tools", keywords=["tool", "open source", "release", "sdk"],
            explanation="Tools and libraries.", scope="global"),
    ]
    db.session.add_all(tags)
    db.session.flush()

    source_registry.register(_FixtureSource)
    source = Source(type_key="_fixture_integration", name="Fixture", config={})
    db.session.add(source)
    db.session.commit()

    stats = ingest_svc.ingest_source(source)

    print(f"\n── Full ingest of {eml_path.name} ──")
    print(f"  fetched={stats['fetched']}  new_items={stats['new_items']}"
          f"  tagged={stats['tagged']}  errors={stats['errors']}")
    for msg in stats.get("error_log", []):
        print(f"  ERROR: {msg}")
    for item in NewsItem.query.all():
        tag_names = [lnk.tag.name for lnk in item.tag_links]
        print(f"  [{item.item_type:12s}] {item.title[:80]}  tags={tag_names}")

    assert stats["errors"] == 0, f"Ingest errors: {stats['error_log']}"
    assert stats["new_items"] > 0, f"No items ingested from {eml_path.name}"
    assert NewsItem.query.count() == stats["new_items"]
