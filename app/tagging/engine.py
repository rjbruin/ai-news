"""Tagging engine: orchestrates NB + LLM according to the configured mode.

Modes (global, TAGGING_MODE):
  nb_only      — keyword/NB scorer only.
  nb_then_llm  — NB first; if results are weak/ambiguous, refine with the LLM.
  llm_only     — LLM only.

Exposes:
  classify(item_text, tags, mode=None, threshold=None) -> {tag_id: (confidence, method)}
  apply_to_item(item) — compute tags for a NewsItem and persist NewsItemTag rows.
  preview(name, keywords, explanation, limit) — dry-run a candidate tag.
"""
from __future__ import annotations

import logging

from flask import current_app

from ..extensions import db
from ..models import NewsItem, NewsItemTag, Tag
from . import llm, nb

logger = logging.getLogger(__name__)


def _item_text(item: NewsItem) -> str:
    return f"{item.title}\n{item.summary_text or ''}".strip()


def _tag_docs(tags: list[Tag]) -> list[nb.TagDoc]:
    docs = []
    for t in tags:
        examples = [
            _item_text(link.item)
            for link in t.item_links.filter(NewsItemTag.confirmed.is_(True)).limit(20)
        ]
        docs.append(
            nb.TagDoc(
                tag_id=t.id,
                name=t.name,
                keywords=t.keyword_list,
                explanation=t.explanation or "",
                examples=examples,
            )
        )
    return docs


def classify(
    item_text: str,
    tags: list[Tag],
    *,
    mode: str | None = None,
    threshold: float | None = None,
) -> dict[int, tuple[float, str]]:
    """Return {tag_id: (confidence, method)} for tags judged to apply."""
    if not tags:
        return {}
    mode = mode or current_app.config.get("TAGGING_MODE", "nb_then_llm")
    threshold = (
        threshold
        if threshold is not None
        else current_app.config.get("NB_CONFIDENCE_THRESHOLD", 0.35)
    )
    by_id = {t.id: t for t in tags}
    by_name = {t.name: t for t in tags}
    result: dict[int, tuple[float, str]] = {}

    if mode in ("nb_only", "nb_then_llm"):
        nb_scores = nb.score_item(item_text, _tag_docs(tags))
        for tag_id, score in nb_scores.items():
            if score >= threshold:
                result[tag_id] = (score, "nb")

    need_llm = mode == "llm_only" or (
        mode == "nb_then_llm" and not result  # NB found nothing confident
    )
    if need_llm:
        tag_dicts = [
            {"name": t.name, "keywords": t.keyword_list, "explanation": t.explanation}
            for t in tags
        ]
        llm_scores = llm.score_item(item_text, tag_dicts)
        for name, conf in llm_scores.items():
            tag = by_name.get(name)
            if tag and conf >= 0.5:
                result[tag.id] = (conf, "llm")

    return {tid: v for tid, v in result.items() if tid in by_id}


def apply_to_item(item: NewsItem, tags: list[Tag] | None = None) -> int:
    """Classify a NewsItem and persist tag links. Returns number of tags applied."""
    if tags is None:
        tags = Tag.query.all()
    scores = classify(_item_text(item), tags)

    applied = 0
    for tag_id, (conf, method) in scores.items():
        link = item.tag_links.filter_by(tag_id=tag_id).first()
        if link is None:
            link = NewsItemTag(news_item_id=item.id, tag_id=tag_id)
            db.session.add(link)
        if not link.confirmed:  # don't overwrite manual confirmations
            link.confidence = conf
            link.method = method
        applied += 1

    item.status = "tagged"
    db.session.commit()
    return applied


def preview(
    name: str, keywords: list[str], explanation: str, limit: int = 50
) -> list[dict]:
    """Dry-run a candidate tag against existing items. Returns matching items."""
    threshold = current_app.config.get("NB_CONFIDENCE_THRESHOLD", 0.35)
    candidate = nb.TagDoc(
        tag_id=-1, name=name, keywords=keywords, explanation=explanation, examples=[]
    )
    matches = []
    for item in NewsItem.query.order_by(NewsItem.fetched_at.desc()).limit(500):
        text = _item_text(item)
        scores = nb.score_item(text, [candidate])
        score = scores.get(-1, 0.0)
        if score >= threshold:
            matches.append({"item": item, "confidence": round(score, 3)})
    matches.sort(key=lambda m: m["confidence"], reverse=True)
    return matches[:limit]
