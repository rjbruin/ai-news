"""Tagging engine: LLM-first classification with per-topic classifier graduation.

Each topic (Tag) graduates through 3 states based on how many trustworthy
labels (method in llm|manual) it has accumulated:
  llm_only       (< TOPIC_GRADUATION_THRESHOLD_1)  — LLM decides, every call.
  hybrid         (< TOPIC_GRADUATION_THRESHOLD_2)   — TF-IDF tries first; the
                                                        LLM is only asked about
                                                        items TF-IDF didn't
                                                        confidently score.
  classifier_only (>= TOPIC_GRADUATION_THRESHOLD_2) — TF-IDF only, LLM is
                                                        never asked about this
                                                        topic again.

This keeps LLM call *volume* at exactly one call per item regardless of how
many topics or private topics exist — only the taxonomy sent in that one
call shrinks as topics graduate. A topic's applications are global
(NewsItemTag.user_id=NULL) if the tag is scope='global', or scoped to the
tag's owner if scope='user' — the LLM/TF-IDF machinery is identical either
way, only where the resulting rows land differs (see _persist_scores).

TAGGING_MODE remaining set to nb_only/nb_then_llm/llm_only (rather than the
default "graduated") bypasses per-topic graduation entirely and falls back
to the legacy single-mode classify() below — kept as a debugging/test escape
hatch (TestConfig still forces nb_only).
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
    """Build TF-IDF pseudo-documents, training on the LLM's own (and any
    manually confirmed) tag applications — explicitly NOT on the
    classifier's own prior output, which would let it reinforce its own
    noise instead of learning from an independent signal."""
    docs = []
    for t in tags:
        examples_q = (
            t.item_links
            .filter(NewsItemTag.method.in_(["llm", "manual"]))
            .order_by(NewsItemTag.created_at.desc())
            .limit(20)
        )
        # link.item can be None for a stale NewsItemTag row whose NewsItem
        # was deleted without the link being cleaned up — skip rather than
        # crash on an otherwise-routine retag/classification pass.
        examples = [_item_text(link.item) for link in examples_q if link.item is not None]
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


def _graduation_state(label_count: int, threshold_1: int, threshold_2: int) -> str:
    if label_count >= threshold_2:
        return "classifier_only"
    if label_count >= threshold_1:
        return "hybrid"
    return "llm_only"


def classifier_state(tag: Tag, *, threshold_1: int, threshold_2: int) -> str:
    """One topic's current graduation state, computed from its trustworthy
    (llm|manual) label count — not stored, so it can never drift from the
    data it's derived from. Every topic graduates the same way (LLM-first,
    classifier taking over as labels accumulate); there is no per-topic
    override and this state is purely an internal classification-routing
    detail, never surfaced to users."""
    label_count = (
        NewsItemTag.query.filter_by(tag_id=tag.id)
        .filter(NewsItemTag.method.in_(["llm", "manual"]))
        .count()
    )
    return _graduation_state(label_count, threshold_1, threshold_2)


def topic_stats(tags: list[Tag]) -> dict[int, dict]:
    """Item count per topic, in one batched query — used by the admin
    Topics view."""
    from sqlalchemy import func

    tag_ids = [t.id for t in tags]
    if not tag_ids:
        return {}
    counts = dict(
        db.session.query(NewsItemTag.tag_id, func.count(NewsItemTag.id))
        .filter(NewsItemTag.tag_id.in_(tag_ids))
        .group_by(NewsItemTag.tag_id).all()
    )
    return {t.id: {"item_count": counts.get(t.id, 0)} for t in tags}


def item_topic_names(item_ids: list[int], user) -> dict[int, list[str]]:
    """Topic names per item, scoped to what ``user`` can see (global topics
    plus their own private ones) — the same visibility rule as the News page
    filter. No confidence/method filtering: the News page's tag badges show
    every applied tag regardless of confidence, and this matches that."""
    if not item_ids:
        return {}
    rows = (
        db.session.query(NewsItemTag.news_item_id, Tag.name)
        .join(Tag, Tag.id == NewsItemTag.tag_id)
        .filter(
            NewsItemTag.news_item_id.in_(item_ids),
            db.or_(NewsItemTag.user_id.is_(None), NewsItemTag.user_id == user.id),
        )
        .all()
    )
    result: dict[int, list[str]] = {}
    for item_id, name in rows:
        result.setdefault(item_id, []).append(name)
    return {k: sorted(set(v)) for k, v in result.items()}


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
    """Legacy single-mode classifier (nb_only/nb_then_llm/llm_only), kept as
    the TAGGING_MODE override escape hatch. Return {tag_id: (confidence, method)}."""
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


def _classify_graduated(
    item_text: str,
    tags: list[Tag],
    *,
    api_key: str | None = None,
    model: str | None = None,
    usage_hook=None,
) -> dict[int, tuple[float, str]]:
    """Per-topic-graduation classification: exactly one LLM call, covering
    only the topics that still need it (llm_only, plus hybrid topics TF-IDF
    didn't confidently resolve for this item)."""
    if not tags:
        return {}

    t1 = current_app.config.get("TOPIC_GRADUATION_THRESHOLD_1", 20)
    t2 = current_app.config.get("TOPIC_GRADUATION_THRESHOLD_2", 100)
    nb_threshold = current_app.config.get("NB_CONFIDENCE_THRESHOLD", 0.35)

    states = {t.id: classifier_state(t, threshold_1=t1, threshold_2=t2) for t in tags}
    classifier_tags = [t for t in tags if states[t.id] in ("hybrid", "classifier_only")]
    llm_eligible_tags = [t for t in tags if states[t.id] in ("llm_only", "hybrid")]

    result: dict[int, tuple[float, str]] = {}

    if classifier_tags:
        scorer = nb.Scorer(_tag_docs(classifier_tags), background_corpus=_background_corpus())
        nb_scores = scorer.score(item_text)
        for t in classifier_tags:
            score = nb_scores.get(t.id, 0.0)
            if score >= nb_threshold:
                result[t.id] = (score, "nb")

    still_need_llm = [
        t for t in llm_eligible_tags
        if states[t.id] == "llm_only" or t.id not in result
    ]

    if still_need_llm:
        tag_dicts = [
            {"name": t.name, "keywords": t.keyword_list, "explanation": t.explanation}
            for t in still_need_llm
        ]
        llm_scores = llm.score_item(
            item_text, tag_dicts, api_key=api_key, model=model, usage_hook=usage_hook,
        )
        by_name = {t.name: t for t in still_need_llm}
        for name, conf in llm_scores.items():
            t = by_name.get(name)
            if t and conf >= 0.5:
                result[t.id] = (conf, "llm")

    return result


def _persist_scores(item: NewsItem, scores: dict[int, tuple[float, str]], tags: list[Tag]) -> int:
    by_id = {t.id: t for t in tags}
    applied = 0
    for tag_id, (conf, method) in scores.items():
        tag = by_id.get(tag_id)
        if tag is None:
            continue
        owner_id = tag.owner_user_id if tag.scope == "user" else None
        link = item.tag_links.filter_by(tag_id=tag_id, user_id=owner_id).first()
        if link is None:
            link = NewsItemTag(news_item_id=item.id, tag_id=tag_id, user_id=owner_id)
            db.session.add(link)
        if not link.confirmed:  # don't overwrite manual confirmations
            link.confidence = conf
            link.method = method
        applied += 1
    item.status = "tagged"
    db.session.commit()
    return applied


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
    assigned (see services.ingest) — LLM tagging runs on that key rather
    than the global one so cost is attributed correctly.
    """
    if tags is None:
        tags = Tag.query.filter_by(archived_at=None).all()

    mode_override = current_app.config.get("TAGGING_MODE", "graduated")
    if mode_override != "graduated":
        scores = classify(
            _item_text(item), tags, mode=mode_override,
            api_key=api_key, model=model, usage_hook=usage_hook,
        )
    else:
        scores = _classify_graduated(
            _item_text(item), tags, api_key=api_key, model=model, usage_hook=usage_hook,
        )

    return _persist_scores(item, scores, tags)


def preview_iter(name: str, keywords: list[str], explanation: str):
    """Generator that yields SSE-ready dicts, one per item processed.

    Mirrors the nb_then_llm classify loop exactly so preview behaviour
    matches what a newly saved tag would do at ingest time (a brand-new
    candidate topic always has 0 labels, so it's always llm_only under
    graduated mode — this preview intentionally stays on the simpler
    nb_then_llm shape rather than duplicating graduation logic for a
    topic that can't have graduated yet).

    Event types:
      start    — {"type": "start", "total": N}
      checking — {"type": "checking", "current": i, "title": "…"}  (before LLM call)
      match    — {"type": "match", "current": i, "item_id": …, "title": …,
                  "summary": …, "confidence": …, "method": "nb"|"llm"}
      no_match — {"type": "no_match", "current": i}
      done     — {"type": "done"}
    """
    mode = current_app.config.get("TAGGING_MODE", "nb_then_llm")
    if mode == "graduated":
        mode = "nb_then_llm"
    threshold = current_app.config.get("NB_CONFIDENCE_THRESHOLD", 0.30)

    candidate_doc = nb.TagDoc(
        tag_id=-1, name=name, keywords=keywords, explanation=explanation, examples=[]
    )
    all_docs = _tag_docs(Tag.query.filter_by(archived_at=None).all()) + [candidate_doc]
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
