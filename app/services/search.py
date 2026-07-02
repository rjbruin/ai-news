"""Search across news items and user editions."""
from __future__ import annotations

import html as _html
import re

from ..extensions import db
from ..models import NewsItem, Summary, SummaryRun

PER_PAGE = 10
WINDOW = 130  # chars of context around the match


# ─────────────────────────── text helpers ───────────────────────────

def _strip_html(text: str) -> str:
    text = re.sub(r'<[^>]+>', ' ', text or '')
    text = _html.unescape(text)
    return re.sub(r'\s+', ' ', text).strip()


def _doc_to_text(document) -> str:
    """Flatten a block-IR document list to searchable plain text."""
    if not isinstance(document, list):
        return ''
    parts = []
    text_fields = ('title', 'subtitle', 'markdown', 'description',
                   'headline', 'subheader', 'summary', 'dek', 'body',
                   'text', 'attribution')
    item_fields = ('headline', 'text', 'summary')
    for block in document:
        for f in text_fields:
            val = block.get(f)
            if val:
                parts.append(str(val))
        for item in block.get('items', []):
            for f in item_fields:
                val = item.get(f)
                if val:
                    parts.append(str(val))
    return ' '.join(parts)


def _excerpt(plain: str, query: str) -> str:
    """Return an HTML-safe excerpt with the query term wrapped in <mark>."""
    lo = plain.lower()
    qlo = query.lower()
    pos = lo.find(qlo)
    if pos == -1:
        snippet = plain[:WINDOW * 2]
        return _html.escape(snippet) + ('…' if len(plain) > WINDOW * 2 else '')

    start = max(0, pos - WINDOW)
    end = min(len(plain), pos + len(query) + WINDOW)
    snippet = plain[start:end]
    pre = '…' if start > 0 else ''
    post = '…' if end < len(plain) else ''

    # locate inside snippet
    slo = snippet.lower()
    idx = slo.find(qlo)
    if idx != -1:
        matched = snippet[idx: idx + len(query)]
        return (
            pre
            + _html.escape(snippet[:idx])
            + '<mark>' + _html.escape(matched) + '</mark>'
            + _html.escape(snippet[idx + len(query):])
            + post
        )
    return pre + _html.escape(snippet) + post


# ─────────────────────────── edition search ───────────────────────────

def _best_edition_plain(run: SummaryRun, query: str) -> str:
    """Return the plain-text source most likely to contain the match."""
    qlo = query.lower()
    if run.label and qlo in run.label.lower():
        return run.label
    if run.content:
        stripped = _strip_html(run.content)
        if qlo in stripped.lower():
            return stripped
    if run.document:
        doc = _doc_to_text(run.document)
        if qlo in doc.lower():
            return doc
    # Fallback: best available text
    return _strip_html(run.content) if run.content else _doc_to_text(run.document)


def search_editions(query: str, user_id: int, page: int, per_page: int = PER_PAGE):
    q = f'%{query}%'
    user_summary_ids = (
        db.session.query(Summary.id).filter_by(user_id=user_id).subquery()
    )
    base = (
        SummaryRun.query
        .filter(SummaryRun.summary_id.in_(user_summary_ids))
        .filter(
            db.or_(
                SummaryRun.label.ilike(q),
                SummaryRun.content.ilike(q),
                SummaryRun.document.ilike(q),
            )
        )
        .order_by(SummaryRun.generated_at.desc())
    )
    total = base.count()
    runs = base.offset((page - 1) * per_page).limit(per_page).all()
    results = [
        {
            'run': run,
            'summary': run.summary,
            'excerpt': _excerpt(_best_edition_plain(run, query), query),
        }
        for run in runs
    ]
    return results, total


# ─────────────────────────── news search ───────────────────────────

def _best_news_plain(item: NewsItem, query: str) -> str:
    qlo = query.lower()
    for field in (item.summary_text, item.one_liner, item.title):
        if field and qlo in field.lower():
            return field
    return item.title or ''


def search_news(query: str, page: int, per_page: int = PER_PAGE):
    q = f'%{query}%'
    base = (
        NewsItem.query
        .filter(
            db.or_(
                NewsItem.title.ilike(q),
                NewsItem.summary_text.ilike(q),
                NewsItem.one_liner.ilike(q),
            )
        )
        .order_by(NewsItem.fetched_at.desc())
    )
    total = base.count()
    items = base.offset((page - 1) * per_page).limit(per_page).all()
    results = [
        {
            'item': item,
            'excerpt': _excerpt(_best_news_plain(item, query), query),
        }
        for item in items
    ]
    return results, total
