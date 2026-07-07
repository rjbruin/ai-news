"""Tagging engine: orchestrates NB + LLM according to the configured mode.

Modes (global, TAGGING_MODE):
  nb_only      — keyword/NB scorer only.
  nb_then_llm  — NB first; if results are weak/ambiguous, refine with the LLM.
  llm_only     — LLM only.

Exposes:
  classify(item_text, tags, mode=None, threshold=None) -> {tag_id: (confidence, method)}
  apply_to_item(item) — compute tags for a NewsItem and persist NewsItemTag rows.
  preview_iter(name, keywords, explanation) — generator yielding SSE-ready dicts.
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


def _background_corpus() -> list[str]:
    return [_item_text(item) for item in NewsItem.query.limit(2000).all()]


def classify(
    item_text: str,
    tags: list[Tag],
    *,
    mode: str | None = None,
    threshold: float | None = None,
    api_key: str | None = None,
    model: str | None = None,
    usage_hook=None,
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
        scorer = nb.Scorer(_tag_docs(tags), background_corpus=_background_corpus())
        nb_scores = scorer.score(item_text)
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
        llm_scores = llm.score_item(
            item_text, tag_dicts, api_key=api_key, model=model, usage_hook=usage_hook,
        )
        for name, conf in llm_scores.items():
            tag = by_name.get(name)
            if tag and conf >= 0.5:
                result[tag.id] = (conf, "llm")

    return {tid: v for tid, v in result.items() if tid in by_id}


def apply_to_item(
    item: NewsItem,
    tags: list[Tag] | None = None,
    *,
    api_key: str | None = None,
    model: str | None = None,
    usage_hook=None,
) -> int:
    """Classify a NewsItem and persist tag links. Returns number of tags applied.

    ``api_key``/``model`` are the credentials the item's owning Source was
    assigned (see services.ingest) — LLM tagging fallback runs on that key
    rather than the global one so cost is attributed correctly.
    """
    if tags is None:
        tags = Tag.query.all()
    scores = classify(
        _item_text(item), tags, api_key=api_key, model=model, usage_hook=usage_hook,
    )

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


def preview_iter(name: str, keywords: list[str], explanation: str):
    """Generator that yields SSE-ready dicts, one per item processed.

    Mirrors the nb_then_llm classify loop exactly so preview behaviour
    matches what a newly saved tag would do at ingest time.

    Event types:
      start    — {"type": "start", "total": N}
      checking — {"type": "checking", "current": i, "title": "…"}  (before LLM call)
      match    — {"type": "match", "current": i, "item_id": …, "title": …,
                  "summary": …, "confidence": …, "method": "nb"|"llm"}
      no_match — {"type": "no_match", "current": i}
      done     — {"type": "done"}
    """
    mode = current_app.config.get("TAGGING_MODE", "nb_then_llm")
    threshold = current_app.config.get("NB_CONFIDENCE_THRESHOLD", 0.30)

    candidate_doc = nb.TagDoc(
        tag_id=-1, name=name, keywords=keywords, explanation=explanation, examples=[]
    )
    all_docs = _tag_docs(Tag.query.all()) + [candidate_doc]
    scorer = nb.Scorer(all_docs, background_corpus=_background_corpus())
    candidate_dict = {"name": name, "keywords": keywords, "explanation": explanation}

    items = NewsItem.query.order_by(NewsItem.fetched_at.desc()).limit(100).all()
    yield {"type": "start", "total": len(items)}

    for i, item in enumerate(items, 1):
        text = _item_text(item)
        nb_hit = False

        if mode in ("nb_only", "nb_then_llm"):
            score = scorer.score(text).get(-1, 0.0)
            if score >= threshold:
                nb_hit = True
                yield {
                    "type": "match", "current": i,
                    "item_id": item.id, "title": item.title,
                    "summary": item.summary_text or "",
                    "confidence": round(score, 3), "method": "nb",
                }

        if not nb_hit and mode in ("nb_then_llm", "llm_only"):
            yield {"type": "checking", "current": i, "title": item.title}
            conf = llm.score_item(text, [candidate_dict]).get(name, 0.0)
            if conf >= 0.5:
                yield {
                    "type": "match", "current": i,
                    "item_id": item.id, "title": item.title,
                    "summary": item.summary_text or "",
                    "confidence": round(conf, 3), "method": "llm",
                }
            else:
                yield {"type": "no_match", "current": i}
        elif not nb_hit:
            yield {"type": "no_match", "current": i}

    yield {"type": "done"}
