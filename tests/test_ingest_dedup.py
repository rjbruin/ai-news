"""Ingest-time duplicate collapse.

Production evidence for the cases below: one newsletter source emitted the
same headline three times over two days, because its extractor alternated
between the publisher's bare homepage and the newsletter permalink, and once
between the permalink with and without a trailing slash. dedup_hash compared
URLs raw, so that became three NewsItem rows for one story — and two of them
were separately reported in two editions.
"""
from datetime import timedelta
from types import SimpleNamespace

from app.models import NewsItem, utcnow
from app.services.ingest import _find_untitled_url_twin, _merge_into_twin
from app.urls import looks_like_article_url, norm_url

TITLE = "Anthropic Brings Claude Cowork to Web and Mobile for Max Subscribers"
PERMALINK = "https://aiweekly.co/alerts/anthropic-opens-claude-cowork/"


def _item(db, title, url=None, days_ago=0, one_liner="", summary_text=""):
    it = NewsItem(
        dedup_hash=NewsItem.make_hash(title, url),
        title=title, url=url, one_liner=one_liner, summary_text=summary_text,
        fetched_at=utcnow().replace(tzinfo=None) - timedelta(days=days_ago),
    )
    db.session.add(it)
    db.session.commit()
    return it


def _extracted(title, url=None, one_liner="", summary=""):
    return SimpleNamespace(
        title=title, url=url, one_liner=one_liner, summary=summary,
        item_type="news", full_text=None, published_at=None,
    )


# ── URL helpers ─────────────────────────────────────────────────────────────

def test_norm_url_collapses_trailing_slash_and_www():
    assert norm_url("https://a.example/story/") == norm_url("https://a.example/story")
    assert norm_url("https://www.a.example/x") == norm_url("https://a.example/x")
    assert norm_url("https://a.example/x#frag") == norm_url("https://a.example/x")
    assert norm_url(None) == ""


def test_looks_like_article_url_rejects_bare_domains():
    assert looks_like_article_url("https://techcrunch.com/2026/07/story") is True
    assert looks_like_article_url("https://techcrunch.com") is False
    assert looks_like_article_url("https://techcrunch.com/") is False
    assert looks_like_article_url("not-a-url") is False
    assert looks_like_article_url(None) is False


# ── Rule 1: hashing ─────────────────────────────────────────────────────────

def test_hash_ignores_trailing_slash(app):
    assert NewsItem.make_hash(TITLE, PERMALINK) == NewsItem.make_hash(
        TITLE, PERMALINK.rstrip("/")
    )


def test_hash_ignores_bare_domain_url(app):
    """A bare homepage carries no story identity, so it must not affect the hash."""
    assert NewsItem.make_hash(TITLE, "https://techcrunch.com") == NewsItem.make_hash(
        TITLE, None
    )


def test_hash_still_separates_genuinely_different_articles(app):
    assert NewsItem.make_hash(TITLE, "https://a.example/one") != NewsItem.make_hash(
        TITLE, "https://b.example/two"
    )
    assert NewsItem.make_hash("Story A", None) != NewsItem.make_hash("Story B", None)


# ── Rule 2: same-headline twin ──────────────────────────────────────────────

def test_twin_found_when_existing_row_has_only_a_bare_domain(app, db):
    """The exact production failure: bare-domain row first, permalink next day."""
    existing = _item(db, TITLE, "https://techcrunch.com", days_ago=1)
    assert _find_untitled_url_twin(TITLE, PERMALINK) is existing


def test_twin_found_when_incoming_has_no_usable_url(app, db):
    existing = _item(db, TITLE, PERMALINK, days_ago=1)
    assert _find_untitled_url_twin(TITLE, "https://techcrunch.com") is existing


def test_two_real_article_urls_are_not_twins(app, db):
    """Distinct outlets under one headline is a real cross-source duplicate —
    story_dedup judges that at edition time, with context this lacks."""
    _item(db, TITLE, "https://a.example/story-one", days_ago=1)
    assert _find_untitled_url_twin(TITLE, "https://b.example/story-two") is None


def test_twin_lookup_respects_the_window(app, db):
    _item(db, TITLE, "https://techcrunch.com", days_ago=30)
    assert _find_untitled_url_twin(TITLE, PERMALINK) is None


def test_twin_lookup_is_case_insensitive_on_title(app, db):
    existing = _item(db, TITLE, "https://techcrunch.com", days_ago=1)
    assert _find_untitled_url_twin(TITLE.upper(), PERMALINK) is existing


def test_twin_lookup_disabled_by_config(app, db):
    _item(db, TITLE, "https://techcrunch.com", days_ago=1)
    app.config["INGEST_DEDUP_TITLE_WINDOW_DAYS"] = 0
    assert _find_untitled_url_twin(TITLE, PERMALINK) is None


def test_different_titles_are_not_twins(app, db):
    _item(db, TITLE, "https://techcrunch.com", days_ago=1)
    assert _find_untitled_url_twin("Something else entirely", PERMALINK) is None


# ── Merging ─────────────────────────────────────────────────────────────────

def test_merge_upgrades_bare_domain_to_real_link(app, db):
    existing = _item(db, TITLE, "https://techcrunch.com", days_ago=1)
    old_hash = existing.dedup_hash

    _merge_into_twin(existing, _extracted(TITLE, PERMALINK))
    db.session.commit()

    assert existing.url == PERMALINK
    assert existing.dedup_hash != old_hash
    assert existing.dedup_hash == NewsItem.make_hash(TITLE, PERMALINK)


def test_merge_keeps_the_better_link_it_already_has(app, db):
    existing = _item(db, TITLE, PERMALINK, days_ago=1)
    _merge_into_twin(existing, _extracted(TITLE, "https://techcrunch.com"))
    db.session.commit()
    assert existing.url == PERMALINK


def test_merge_fills_in_missing_text(app, db):
    existing = _item(db, TITLE, PERMALINK, days_ago=1)
    _merge_into_twin(
        existing, _extracted(TITLE, PERMALINK, one_liner="The gist.", summary="Longer."),
    )
    db.session.commit()
    assert existing.one_liner == "The gist."
    assert existing.summary_text == "Longer."


def test_merge_does_not_overwrite_existing_text(app, db):
    existing = _item(db, TITLE, PERMALINK, days_ago=1, one_liner="Original.")
    _merge_into_twin(existing, _extracted(TITLE, PERMALINK, one_liner="Replacement."))
    db.session.commit()
    assert existing.one_liner == "Original."


def test_full_production_sequence_yields_one_row(app, db):
    """All three real rows, in the order they actually arrived, collapse to one
    row carrying the good permalink."""
    first = _item(db, TITLE, "https://techcrunch.com", days_ago=1)

    # Second poll: permalink with trailing slash. Hash differs, twin found.
    twin = _find_untitled_url_twin(TITLE, PERMALINK)
    assert twin is first
    _merge_into_twin(twin, _extracted(TITLE, PERMALINK))
    db.session.commit()

    # Third poll: same permalink without the slash — now caught by hash alone.
    assert NewsItem.make_hash(TITLE, PERMALINK.rstrip("/")) == first.dedup_hash
    assert NewsItem.query.filter_by(title=TITLE).count() == 1
    assert first.url == PERMALINK
