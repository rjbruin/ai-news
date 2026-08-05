"""Turn per-item reader votes into signals the editor can act on.

Two layers, deliberately kept separate because they answer different
questions and become useful at very different data volumes:

**Layer 1 — topic aggregation** (``topic_suggestions``). Votes land on items,
items carry Topics, so votes aggregate to per-topic sentiment with no model
at all. Useful from the very first vote and completely explainable ("7 of 9
items tagged X were downvoted"). Surfaced as a *suggestion* against the topic
tier picker rather than applied automatically: the tiers are the owner's
explicit editorial control, and silently rewriting them would be surprising.

**Layer 2 — per-item affinity** (``score_items``). Once enough votes exist,
TF-IDF the upvoted and downvoted item texts into two pseudo-documents and
score in-scope items against both. This catches preferences that don't line
up with the topic taxonomy at all. Reuses the tagging TF-IDF scorer
(app/tagging/nb.py) rather than introducing a second modelling approach.

Only the Dispatch *owner's* votes feed either layer. Followers' votes are
recorded (see models.ItemFeedback) as an engagement signal, but a published
Dispatch's editorial direction belongs to the person who runs and pays for
it.

Two properties both layers must preserve:

* **Absence is not dislike.** Most items are never voted on. Only explicit
  votes count; silence is never read as negative.
* **A hint, never a filter.** The agent is given every in-scope item with no
  pre-filtering (see the prompt's opening line) precisely so a big story can
  never be silently dropped. These signals ride along with items; they never
  remove one from scope.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ..extensions import db
from ..models import ItemFeedback, NewsItem, NewsItemTag, SummaryRun, Tag
from ..tagging import nb

logger = logging.getLogger(__name__)

# Layer 1: don't suggest anything about a topic until there's enough signal
# that it isn't just one annoyed click, and only when sentiment is lopsided.
MIN_TOPIC_VOTES = 4
TOPIC_SUGGEST_RATIO = 0.7

# Layer 2: below this many votes on each side the two pseudo-documents are
# too thin for cosine similarity to mean anything.
MIN_VOTES_PER_SIDE = 8
# Score magnitude below which we say nothing at all — the point is to flag a
# handful of clear cases per edition, not to annotate everything.
SIGNAL_THRESHOLD = 0.08
# How many liked/disliked examples feed each pseudo-document. Recent votes
# reflect current taste; older ones are kept out so the signal can move.
MAX_EXAMPLES_PER_SIDE = 60


def _item_text(item: NewsItem) -> str:
    return f"{item.title}\n{item.summary_text or ''}".strip()


def _owner_votes(summary):
    """The owner's votes on their own Dispatch's editions, newest first,
    with the voted item eagerly available. Votes whose item has since been
    deleted (item_id NULL) are dropped — there's no text left to learn from.
    """
    return (
        db.session.query(ItemFeedback, NewsItem)
        .join(SummaryRun, ItemFeedback.run_id == SummaryRun.id)
        .join(NewsItem, ItemFeedback.item_id == NewsItem.id)
        .filter(
            SummaryRun.summary_id == summary.id,
            ItemFeedback.user_id == summary.user_id,
        )
        .order_by(ItemFeedback.created_at.desc())
        .all()
    )


# ── Layer 1: topic aggregation ──────────────────────────────────────────────

@dataclass
class TopicSuggestion:
    tag_id: int
    tag_name: str
    up: int
    down: int
    suggested_tier: str  # "complete" | "highlights" | "none"
    current_tier: str

    @property
    def total(self) -> int:
        return self.up + self.down

    @property
    def reason(self) -> str:
        if self.suggested_tier == "none":
            return f"You downvoted {self.down} of {self.total} items tagged {self.tag_name}."
        return f"You upvoted {self.up} of {self.total} items tagged {self.tag_name}."


def _current_tier(summary, tag_id: int) -> str:
    tiers = (summary.params or {}).get("topic_tiers") or {}
    if tag_id in (tiers.get("none") or []):
        return "none"
    if tag_id in (tiers.get("highlights") or []):
        return "highlights"
    return "complete"


def topic_suggestions(summary) -> list[TopicSuggestion]:
    """Per-topic tier changes the owner's votes argue for.

    Only returns a topic when the vote count clears MIN_TOPIC_VOTES, the
    split is at least TOPIC_SUGGEST_RATIO one way, and the implied tier is
    not where the topic already sits — so an owner who has already acted on
    a suggestion stops being nagged about it.
    """
    rows = _owner_votes(summary)
    if not rows:
        return []

    item_ids = {item.id for _, item in rows}
    vote_by_item = {fb.item_id: fb.vote for fb, _ in rows}

    # A private topic's applications are scoped to its owner; global ones
    # have user_id NULL. Both are legitimate signal for this owner.
    links = (
        db.session.query(NewsItemTag.news_item_id, Tag)
        .join(Tag, NewsItemTag.tag_id == Tag.id)
        .filter(
            NewsItemTag.news_item_id.in_(item_ids),
            Tag.archived_at.is_(None),
            db.or_(NewsItemTag.user_id.is_(None), NewsItemTag.user_id == summary.user_id),
        )
        .all()
    )

    tally: dict[int, dict] = {}
    for news_item_id, tag in links:
        vote = vote_by_item.get(news_item_id)
        if vote is None:
            continue
        entry = tally.setdefault(tag.id, {"name": tag.name, "up": 0, "down": 0})
        entry["up" if vote > 0 else "down"] += 1

    out: list[TopicSuggestion] = []
    for tag_id, entry in tally.items():
        total = entry["up"] + entry["down"]
        if total < MIN_TOPIC_VOTES:
            continue
        if entry["down"] / total >= TOPIC_SUGGEST_RATIO:
            suggested = "none"
        elif entry["up"] / total >= TOPIC_SUGGEST_RATIO:
            suggested = "complete"
        else:
            continue
        current = _current_tier(summary, tag_id)
        if suggested == current:
            continue
        out.append(TopicSuggestion(
            tag_id=tag_id, tag_name=entry["name"],
            up=entry["up"], down=entry["down"],
            suggested_tier=suggested, current_tier=current,
        ))

    # Strongest signal first — most votes, then most lopsided.
    out.sort(key=lambda s: (s.total, abs(s.up - s.down)), reverse=True)
    return out


# ── Layer 2: per-item affinity ──────────────────────────────────────────────

# Synthetic tag ids for the two pseudo-documents. nb.Scorer keys its output by
# TagDoc.tag_id; these never touch the tags table.
_LIKED = -101
_DISLIKED = -102


def score_items(summary, items: list) -> dict[int, dict]:
    """Map item_id -> {"score", "basis"} for items the owner's past votes say
    something about. Items with no clear signal are absent from the result,
    so they cost the agent no tokens at all — same contract as
    services/story_dedup's prior_coverage.

    Score is (similarity to liked corpus - similarity to disliked corpus),
    so it is positive for "looks like things they liked", negative for the
    reverse, and near zero when an item resembles both or neither.
    """
    if not items:
        return {}

    rows = _owner_votes(summary)
    liked = [_item_text(item) for fb, item in rows if fb.vote > 0][:MAX_EXAMPLES_PER_SIDE]
    disliked = [_item_text(item) for fb, item in rows if fb.vote < 0][:MAX_EXAMPLES_PER_SIDE]
    if len(liked) < MIN_VOTES_PER_SIDE or len(disliked) < MIN_VOTES_PER_SIDE:
        # One-sided feedback can't produce a contrast, and scoring against a
        # single corpus would just measure "is this a news item".
        return {}

    scope_texts = [_item_text(i) for i in items]
    docs = [
        nb.TagDoc(tag_id=_LIKED, name="", keywords=[], explanation="", examples=liked),
        nb.TagDoc(tag_id=_DISLIKED, name="", keywords=[], explanation="", examples=disliked),
    ]
    # Background corpus = the items being scored, so IDF reflects this
    # edition's actual vocabulary rather than the whole archive's.
    scorer = nb.Scorer(docs, background_corpus=scope_texts)

    out: dict[int, dict] = {}
    for item, text in zip(items, scope_texts):
        sims = scorer.score(text)
        if not sims:
            continue
        score = sims.get(_LIKED, 0.0) - sims.get(_DISLIKED, 0.0)
        if abs(score) < SIGNAL_THRESHOLD:
            continue
        out[item.id] = {
            "score": round(score, 3),
            "basis": (
                "resembles stories this reader upvoted" if score > 0
                else "resembles stories this reader downvoted"
            ),
        }
    return out
