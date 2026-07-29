"""Deterministic coverage tracking: which in-scope items made it into an edition.

After an edition is generated, its stored block ``document`` references the
source items it used — by ``NewsItem`` id (``item_id``) and by article URL
(``sources``, ``more_news`` links, and ``<a href>`` links inside HTML fields).
Comparing that against the edition's full time-window scope tells us, purely
deterministically, which candidate items were left out.

``document_references`` and ``norm_url`` are public because the same
extraction backs the per-edition coverage record persisted at generation time
(see app/agent/memory.py's ``coverage`` kind), which in turn feeds
cross-edition duplicate detection in app/services/story_dedup.py.
"""
from __future__ import annotations

import json
import re

from ..urls import looks_like_article_url, norm_url  # noqa: F401 (norm_url re-exported)
from .summarize import items_in_window

# URLs stop at whitespace, quotes, angle/bracket/paren closers, and backslash
# (JSON escapes the closing quote of an HTML href as \" — exclude the backslash).
_URL_RE = re.compile(r"https?://[^\s\"'<>)\]}\\]+")
_ITEM_ID_RE = re.compile(r'"item_id"\s*:\s*"?(\d+)"?')
_ITEM_IDS_RE = re.compile(r'"item_ids"\s*:\s*\[([^\]]*)\]')


def document_references(document) -> tuple[set[int], set[str]]:
    """Extract referenced NewsItem ids and normalized URLs from a block document."""
    blob = json.dumps(document or [])

    ids: set[int] = {int(m) for m in _ITEM_ID_RE.findall(blob)}
    for arr in _ITEM_IDS_RE.findall(blob):
        ids.update(int(x) for x in re.findall(r"\d+", arr))

    urls = {norm_url(u) for u in _URL_RE.findall(blob)}
    urls.discard("")
    return ids, urls


def edition_coverage(run) -> dict:
    """Determine which in-scope items were and weren't included in ``run``.

    Matches each item in the edition's stored time window against the document
    by ``NewsItem`` id or normalized URL. Returns the scope size, the included
    count, and the omitted items (``NewsItem`` rows, newest first).

    An item whose own URL is a bare domain can only be matched by id. Newsletter
    extraction sometimes stores the publisher's root (``https://techcrunch.com``)
    instead of the article link, and matching on that marks the item as covered
    by *any* edition that happens to link to that publisher — which inflated
    coverage on this corpus by 17%.
    """
    summary = run.summary
    exclude_seed = summary is not None and summary.type_key == "debug_agentic"
    scope = items_in_window(
        run.range_start, run.range_end, exclude_seed=exclude_seed,
        user=summary.user if summary else None,
    )

    included_ids, included_urls = document_references(run.document)

    covered, not_covered = [], []
    for item in scope:
        matched = item.id in included_ids or (
            looks_like_article_url(item.url)
            and norm_url(item.url) in included_urls
        )
        if matched:
            covered.append(item)
        else:
            not_covered.append(item)

    return {
        "scope_count": len(scope),
        "included_count": len(covered),
        "covered": covered,
        "not_covered": not_covered,
    }
