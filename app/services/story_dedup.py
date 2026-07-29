"""Flags in-scope items that continue a story an earlier edition already ran.

The editor is otherwise blind to this. Ingest routinely creates several
NewsItem rows for one story — different sources, differing URLs, so
``dedup_hash`` (a SHA of title|url) keeps them distinct — and a later edition
is handed a row that is genuinely new by every signal the system records while
the *story* is old. See docs/story-dedup-spec.md for the production
measurements behind the thresholds.

Matching is TF-IDF + cosine, the same classical approach as app/tagging/nb.py:
same-story items overwhelmingly share proper nouns (company, product, model and
person names), which is exactly what TF-IDF weights highly. No LLM, so this
costs nothing per edition.

Results are *pushed* onto every item in list_scope_items rather than offered as
a tool the agent may call. An optional lookup reproduces the silent-recall
failure this exists to remove: missing a duplicate should require ignoring a
visible flag, not merely forgetting to ask.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta

from flask import current_app

from ..agent import memory as agent_memory
from ..models import NewsItem, utcnow

logger = logging.getLogger(__name__)

# Tier names, as they appear to the agent.
TIER_LIKELY = "likely_duplicate"
TIER_FOLLOW_UP = "possible_follow_up"

# Cap on the background corpus used to fit IDF. Keeps fit time flat as the
# item table grows while still reflecting current news vocabulary.
_BACKGROUND_DAYS = 90


@dataclass
class PriorMatch:
    """One earlier-covered item that a candidate resembles."""

    item_id: int
    title: str
    covered_on: str          # ISO date of the edition that ran it
    score: float
    tier: str

    def as_dict(self) -> dict:
        d = asdict(self)
        d["score"] = round(self.score, 3)
        return d


def match_text(title: str | None, one_liner: str | None, summary_text: str | None = None) -> str:
    """The text a story is identified by.

    one_liner is preferred over summary_text: it is the LLM's own one-sentence
    compression, so it carries the identifying entities without the surrounding
    prose that dilutes the vector.
    """
    parts = [title or ""]
    if one_liner:
        parts.append(one_liner)
    elif summary_text:
        parts.append(summary_text[:300])
    return " ".join(parts).strip()


def _item_match_text(item: NewsItem) -> str:
    return match_text(item.title, item.one_liner, item.summary_text)


def _background_corpus() -> list[str]:
    """Recent item texts, so IDF reflects real news vocabulary at DB scale.

    Without this the same threshold means different things in a small and a
    large database — the trick app/tagging/nb.py's Scorer uses for the same
    reason.
    """
    floor = utcnow().replace(tzinfo=None) - timedelta(days=_BACKGROUND_DAYS)
    rows = (
        NewsItem.query.filter(NewsItem.fetched_at >= floor)
        .with_entities(NewsItem.title, NewsItem.one_liner)
        .all()
    )
    return [t for t in (match_text(title, one_liner) for title, one_liner in rows) if t]


def _tier(score: float, title_score: float, certain: float, title_cut: float) -> str:
    """Which tier a match falls in, or '' if it does not qualify.

    A near-identical title is decisive on its own: the worst real case observed
    (items 341/447, byte-identical headlines four days apart) scores 1.000 on
    titles alone, and deserves no ambiguity even though the fuller text scores
    lower.
    """
    if score >= certain or title_score >= title_cut:
        return TIER_LIKELY
    return TIER_FOLLOW_UP


def find_prior_coverage(
    user, summary, candidates: list[NewsItem], *,
    threshold: float | None = None,
    lookback_days: int | None = None,
    limit: int = 3,
    exclude_run_ids: set[int] | None = None,
) -> dict[int, list[PriorMatch]]:
    """Map candidate item id → earlier-covered items it resembles, best first.

    ``exclude_run_ids`` drops coverage produced by those runs. Revisions pass
    their own edition chain: a revision *replaces* its parent, so the parent's
    coverage is not already-covered ground — without this every item the parent
    featured would come back flagged as a duplicate of itself.

    Returns ``{}`` — never raises — when the feature is disabled, when there is
    no prior coverage to match against, or on any internal failure. A broken
    deduper must degrade to today's behaviour, never block edition generation.
    """
    cfg = current_app.config
    if not cfg.get("AGENT_DEDUP_ENABLED", True):
        return {}
    if not candidates:
        return {}

    threshold = cfg.get("AGENT_DEDUP_THRESHOLD", 0.35) if threshold is None else threshold
    lookback_days = (
        cfg.get("AGENT_DEDUP_LOOKBACK_DAYS", 14) if lookback_days is None else lookback_days
    )
    certain = cfg.get("AGENT_DEDUP_CERTAIN_THRESHOLD", 0.52)
    title_cut = cfg.get("AGENT_DEDUP_TITLE_THRESHOLD", 0.90)

    try:
        prior = agent_memory.recent_coverage(user, summary, days=lookback_days)
    except Exception:  # noqa: BLE001
        logger.exception("Dedup: failed to read prior coverage for summary %s", summary.id)
        return {}
    if exclude_run_ids:
        prior = [p for p in prior if p.get("run_id") not in exclude_run_ids]
    if not prior:
        return {}

    # An item that is itself already recorded as covered is not a duplicate of
    # itself — drop those pairings up front (they'd score 1.0 and drown the
    # genuine matches).
    covered_ids = {p["item_id"] for p in prior}

    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity

        prior_texts = [match_text(p.get("title"), None) for p in prior]
        cand_texts = [_item_match_text(c) for c in candidates]
        prior_titles = [(p.get("title") or "") for p in prior]
        cand_titles = [(c.title or "") for c in candidates]

        background = _background_corpus()
        vec = TfidfVectorizer(stop_words="english", min_df=1)
        vec.fit(background + prior_texts + cand_texts)
        sims = cosine_similarity(vec.transform(cand_texts), vec.transform(prior_texts))

        tvec = TfidfVectorizer(stop_words="english", min_df=1)
        tvec.fit(background + prior_titles + cand_titles)
        tsims = cosine_similarity(tvec.transform(cand_titles), tvec.transform(prior_titles))
    except Exception:  # noqa: BLE001
        logger.exception("Dedup: similarity scoring failed for summary %s", summary.id)
        return {}

    out: dict[int, list[PriorMatch]] = {}
    for i, cand in enumerate(candidates):
        matches = []
        for j, rec in enumerate(prior):
            if rec["item_id"] == cand.id:
                continue  # same row, already covered — not a duplicate story
            score = float(sims[i][j])
            title_score = float(tsims[i][j])
            if score < threshold and title_score < title_cut:
                continue
            matches.append(
                PriorMatch(
                    item_id=rec["item_id"],
                    title=rec.get("title") or "",
                    covered_on=_iso_date(rec.get("edition_ts")),
                    score=max(score, title_score if title_score >= title_cut else score),
                    tier=_tier(score, title_score, certain, title_cut),
                )
            )
        if matches:
            matches.sort(key=lambda m: -m.score)
            out[cand.id] = matches[:limit]

    if out:
        logger.info(
            "Dedup: flagged %d of %d in-scope item(s) against %d prior-covered item(s)",
            len(out), len(candidates), len(covered_ids),
        )
    return out


def _iso_date(value) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    return str(value or "")
