"""Deterministic coverage tracking: which in-scope items made it into an edition.

After an edition is generated, its stored block ``document`` references the
source items it used — by ``NewsItem`` id (``item_id``) and by article URL
(``sources``, ``more_news`` links, and ``<a href>`` links inside HTML fields).
Comparing that against the edition's full time-window scope tells us, purely
deterministically, which candidate items were left out.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from .summarize import items_in_window

# URLs stop at whitespace, quotes, angle/bracket/paren closers, and backslash
# (JSON escapes the closing quote of an HTML href as \" — exclude the backslash).
_URL_RE = re.compile(r"https?://[^\s\"'<>)\]}\\]+")
_ITEM_ID_RE = re.compile(r'"item_id"\s*:\s*"?(\d+)"?')
_ITEM_IDS_RE = re.compile(r'"item_ids"\s*:\s*\[([^\]]*)\]')
_TRAILING_PUNCT = ".,;:!?)]}\"'"


def _norm_url(url: str) -> str:
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


def _document_references(document) -> tuple[set[int], set[str]]:
    """Extract referenced NewsItem ids and normalized URLs from a block document."""
    blob = json.dumps(document or [])

    ids: set[int] = {int(m) for m in _ITEM_ID_RE.findall(blob)}
    for arr in _ITEM_IDS_RE.findall(blob):
        ids.update(int(x) for x in re.findall(r"\d+", arr))

    urls = {_norm_url(u) for u in _URL_RE.findall(blob)}
    urls.discard("")
    return ids, urls


def edition_coverage(run) -> dict:
    """Determine which in-scope items were and weren't included in ``run``.

    Matches each item in the edition's stored time window against the document
    by ``NewsItem`` id or normalized URL. Returns the scope size, the included
    count, and the omitted items (``NewsItem`` rows, newest first).
    """
    summary = run.summary
    exclude_seed = summary is not None and summary.type_key == "debug_agentic"
    scope = items_in_window(run.range_start, run.range_end, exclude_seed=exclude_seed)

    included_ids, included_urls = _document_references(run.document)

    covered, not_covered = [], []
    for item in scope:
        if item.id in included_ids or (item.url and _norm_url(item.url) in included_urls):
            covered.append(item)
        else:
            not_covered.append(item)

    return {
        "scope_count": len(scope),
        "included_count": len(covered),
        "covered": covered,
        "not_covered": not_covered,
    }
