"""Canonical URL handling, shared by ingest dedup, coverage and block validation.

These two functions decide, across the whole app, when two links mean the same
thing and when a link is worth keeping at all. They live here rather than in
any one consumer because ``models.NewsItem.make_hash`` needs them and cannot
import from ``services`` without a cycle.
"""
from __future__ import annotations

from urllib.parse import urlsplit, urlparse

_TRAILING_PUNCT = ".,;:!?)]}\"'"


def norm_url(url: str | None) -> str:
    """Normalize a URL for comparison.

    Drops the scheme and fragment, lowercases the host, strips a leading
    ``www.`` and any trailing slash/punctuation, and keeps the path + query
    (which often carry the article identity, e.g. ``watch?v=…``).
    """
    url = (url or "").strip()
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url.lower().rstrip("/")
    host = (parts.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/")
    query = f"?{parts.query}" if parts.query else ""
    return (host + path + query).rstrip(_TRAILING_PUNCT)


def looks_like_article_url(url: str | None) -> bool:
    """True for a URL that plausibly points at a specific article rather than
    a bare homepage (e.g. 'https://theverge.com/' or 'https://theverge.com').

    Two things produce bare-domain links. The agent sometimes hand-types a
    source it doesn't have the article link for, guessing the site root —
    misleading, since the reader clicks through to a homepage rather than the
    story. Newsletter extraction does the same: for one story it may yield the
    publisher's root on one poll and the newsletter's permalink on the next,
    which forks a single story into two NewsItem rows. Both cases want the
    same answer — this is not a usable article link.
    """
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False
    return parsed.path not in ("", "/")
