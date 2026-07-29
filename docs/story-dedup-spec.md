# Cross-edition story deduplication — spec for parts A and B

Status: **implemented**. Measurements in this document come from the production
Dispatch (id 3, "Daily agent") on 2026-07-29 — 1516 items, 13 editions with
coverage, 456 scored pairs. Reproduce with `python scripts/dedup_report.py`.

Two things changed during implementation, both found by running against a copy
of production; see "Deltas from the original spec" at the end.

## Problem

Subsequent editions re-report stories that earlier editions already covered.
The current defence is `RECENT HEADLINES (last 7 days — do not re-report)`: a
wall of the agent's own freeform prose, assembled in
`app/agent/prompt.py:compose_system_prompt`, plus an optional `read_headlines`
tool. Recall against it is fuzzy and, when it fails, silent.

### The mechanism

Ingest creates several `NewsItem` rows for one story. Items **341**, **395**
and **447** carry byte-identical titles ("Anthropic Brings Claude Cowork to Web
and Mobile for Max Subscribers"); `NewsItem.dedup_hash` was a SHA of the raw
`title|url`, so a differing URL made each a distinct row. A later edition is
then handed a fresh id, never covered, absent from every record the system
keeps — by every available signal it *is* new, and only the story is old.

> **Correction (2026-07-29).** This document originally claimed 341 ran in the
> July 9 edition and 447 again on July 13, making it a confirmed double-report.
> That was wrong. The July 9 document contains no mention of Cowork; 341 was
> counted as covered only because its stored URL was the bare domain
> `https://techcrunch.com`, which matched an unrelated link in that edition.
> The Cowork story ran **once**. See "The bare-domain coverage defect" below —
> the row-forking mechanism is real and verified, that particular instance of
> double-reporting was not.

Confirmed double-reports do exist, in periods with no such contamination —
Claude Opus 5 (July 27 → 28, 0.783), the Claude-chats-in-Google-search leak
(July 28 → 29, 0.633), GPT-Red (July 16 → 20, 0.593).

Two consequences drive this design:

1. **Item-id deduplication cannot fix this.** The duplicates are genuinely
   different rows. Identity has to be established at the story level.
2. **The agent is not being careless.** It is handed an item that looks new.
   The fix is to give it a signal, not a sterner instruction.

### The bare-domain coverage defect

`edition_coverage` matched an item to an edition by id *or* normalized URL.
Newsletter extraction sometimes stores the publisher's homepage rather than the
article link — 206 of 1516 items — and matching on that marked an item as
covered by any edition linking to that publisher anywhere. 145 of those 206
were matched to at least one edition they never appeared in, inflating recorded
coverage by 17% (688 → 569 entries) and concentrated entirely in July 9–20.

Fixed by requiring `looks_like_article_url` before a URL match. All figures in
this document are post-fix.

### Measured signal

TF-IDF + cosine over `title + one_liner`, 14-day lookback, IDF fit on the full
corpus (the same approach as `app/tagging/nb.py`):

| Band | Pairs | Hand-labelled precision |
|---|---:|---|
| ≥ 0.52 | 7 | 7/7 — all genuine re-reports |
| 0.40–0.52 | 10 | ~65% — real duplicates mixed with same-topic-different-story |
| 0.30–0.40 | 27 | ~50% — genuinely ambiguous |
| < 0.30 | 412 | mostly noise |

Nothing above 0.52 was a false positive. Below ~0.45 the failures are topic
collisions rather than story collisions ("Claude Opus 5 matches Fable 5 at half
the price" vs. "Grok 4.5 matches Claude Opus at one-quarter the price", 0.423).

Similarity is a strong **retriever** and a weak **judge**. The Cowork trace
shows why: against item 447, the pure repeat scores 0.788, a widened-availability
follow-up 0.358, a new feature 0.223, and an unrelated security story 0.19.
Ranking is correct; no single cut separates "nothing new" from "new development",
because that distinction is semantic, not lexical. Parts A and B therefore aim
to *surface* prior coverage reliably and leave the judgment with the editor.

## Part A — persist and surface what was actually covered

`app/services/coverage.py:edition_coverage` already extracts, deterministically,
which in-scope items an edition cited (by `item_id` and by normalized URL). Its
only consumers today are two template variables in `app/web/routes.py`; the
agent never sees it.

### A1. Promote the reference extractor

Rename `coverage._document_references` → `document_references` (public) and
`_norm_url` → `norm_url`. Update the two internal call sites. `story_dedup`
(part B) and the write path below both need them.

### A2. Persist a coverage record per edition

Add a `coverage` kind to `app/agent/memory.py`, mirroring the existing
`quick_hits` pattern exactly — same `AgentMemory` table, same JSON-in-`content`
shape, same `edition_ts` scoping. **No migration required.**

```python
def write_coverage(user, summary, edition_ts, items: list[dict]) -> AgentMemory | None
def recent_coverage(user, summary, *, days: int) -> list[dict]
def prune_coverage(*, days: int) -> int
```

Each record: `{"item_id": int, "title": str, "url": str | None}`. Title and URL
are denormalized so a record survives item deletion and so part B needs no join
to build its corpus.

Write it in `app/services/summarize.py:build_summary`, directly alongside the
existing `write_quick_hits` call (~line 285), where both `items` (the scope) and
`document` are already in hand:

```python
ids, urls = document_references(document)
covered = [
    it for it in items
    if it.id in ids or (it.url and norm_url(it.url) in urls)
]
agent_memory.write_coverage(summary.user, summary, run.generated_at, [...])
```

Register `prune_coverage` next to the existing headline/quick-hit pruning in the
agent maintenance job, on the same retention window.

### A3. Backfill

Add `backfill-coverage` to `manage.py`: for every run with a document and no
coverage record, compute via `edition_coverage(run)` and persist. Makes the
feature effective on day one instead of after a retention window has elapsed.

### A4. Surface it to the agent

Replace the prose-only `RECENT HEADLINES` section with a structured companion
(keep the prose — it carries the agent's own editorial reasoning, which the item
list does not):

```
===== ALREADY COVERED (last 14 days) =====
[2026-07-13] Anthropic Brings Claude Cowork to Web and Mobile… (item 447)
[2026-07-09] Anthropic Brings Claude Cowork to Web and Mobile… (item 341)
```

Add a `read_coverage(days)` tool alongside `read_headlines` in
`app/agent/tools.py` for on-demand lookup.

## Part B — push similarity flags into the item list

The key design decision: **flags are computed before the run and attached to
every scope item**, not exposed as a tool the agent may forget to call. An
optional tool reproduces the silent-recall failure this whole change exists to
remove. Missing a duplicate should require ignoring a visible flag.

### B1. New service

`app/services/story_dedup.py`:

```python
@dataclass
class PriorMatch:
    item_id: int
    title: str
    edition_ts: datetime
    score: float
    tier: str  # "likely_duplicate" | "possible_follow_up"

def find_prior_coverage(
    user, summary, candidates: list[NewsItem], *,
    threshold: float = 0.35, lookback_days: int = 14, limit: int = 3,
) -> dict[int, list[PriorMatch]]
```

Implementation notes:

- Prior corpus from `memory.recent_coverage(...)`; return `{}` when empty
  (a first edition has nothing to match against).
- Match text = `title + " " + one_liner`, falling back to `summary_text[:300]`.
  `one_liner` is the LLM's own compression and carries the identifying entities
  without diluting prose.
- Fit `TfidfVectorizer(stop_words="english", min_df=1)` on a background corpus
  of recent item texts plus both sides, so IDF reflects real news vocabulary at
  DB scale — the `Scorer` trick from `app/tagging/nb.py`. Cap the background at
  the last ~90 days so fit time stays flat as the corpus grows.
- Return at most `limit` matches per candidate, best first.
- Wrap the whole thing so any failure degrades to `{}`. A broken deduper must
  never block edition generation.

### B2. Tiers

| Tier | Rule | Instruction to the editor |
|---|---|---|
| `likely_duplicate` | cosine ≥ **0.52**, or title-only cosine ≥ **0.90** | Do not re-report unless there is a concrete new development; if there is, write only what changed. |
| `possible_follow_up` | **0.35** ≤ cosine < 0.52 | Same story or same topic — check the prior item. If the story is the same, cover only what is new. |

0.52 is where measured precision hit 10/10. The title-only rule exists because
the worst real case (341/447) scores **1.000** on titles alone and warrants no
ambiguity. 0.35 flags ≈2.2 items per edition (6.1% of covered items, worst
edition 8) — small enough to state inline without crowding the prompt, which
answers the "too much information" concern: total context goes *down* versus
today's 7-day prose dump, because only matched items carry any extra text.

### B3. Wiring

- `app/agent/context.py`: add `prior_coverage: dict[int, list[PriorMatch]]` to
  `AgentSession`, populated once when the session is built.
- `app/agent/tools.py:_item_brief`: add a `prior_coverage` key when the item has
  matches, omitted entirely otherwise so unflagged items cost no tokens:
  ```json
  "prior_coverage": [
    {"tier": "likely_duplicate", "score": 0.79,
     "covered_on": "2026-07-09", "title": "Anthropic Brings Claude Cowork…"}
  ]
  ```
- `app/agent/prompt.py`: extend workflow step 2 to explain the two tiers and
  what each requires.

### B4. Configuration

Add to `app/config.py`, following the existing `AGENT_*` convention:

```python
AGENT_DEDUP_ENABLED = _bool(os.environ.get("AGENT_DEDUP_ENABLED"), True)
AGENT_DEDUP_THRESHOLD = float(os.environ.get("AGENT_DEDUP_THRESHOLD", "0.35"))
AGENT_DEDUP_CERTAIN_THRESHOLD = float(os.environ.get("AGENT_DEDUP_CERTAIN_THRESHOLD", "0.52"))
AGENT_DEDUP_LOOKBACK_DAYS = int(os.environ.get("AGENT_DEDUP_LOOKBACK_DAYS", "14"))
```

14 days rather than the current 7: the Cowork story recurred over a 13-day span,
and a 7-day window cannot see a weekly edition's previous issue at all. Widening
is cheap now that matching runs against a compact item index instead of stuffing
more prose into the prompt.

## Tests

- `document_references` / `norm_url` still behave after being made public
  (existing coverage tests should cover this).
- `write_coverage` / `recent_coverage` round-trip; `prune_coverage` respects the
  window — mirror the existing `quick_hits` tests.
- `build_summary` persists a coverage record listing exactly the cited items.
- `find_prior_coverage`: identical titles land in `likely_duplicate`; unrelated
  items produce no match; an empty prior corpus returns `{}`; an exception in
  vectorization degrades to `{}` rather than propagating.
- `_item_brief` omits `prior_coverage` entirely when there are no matches.
- A regression fixture built from the real 341/447 pair.

## Explicitly out of scope

- **Part C** (a cheap LLM arbiter over flagged pairs, returning same-story yes/no
  plus a "what's new" delta) is deferred until A+B are measured in production.
  B's output is deliberately shaped as its input: ≈2.5 pairs per edition.
- **Story entities / clustering** (part D) is not attempted.
- **Ingest-time duplicate collapse** shipped separately (see `app/urls.py`,
  `NewsItem.make_hash` and `_find_untitled_url_twin` in `services/ingest.py`).
  It needs no similarity at all: every observed cluster collapses under URL
  normalization plus ignoring bare-domain URLs. It reduces the input noise this
  spec's part B otherwise has to compensate for, but does not replace it — a
  story genuinely re-reported a week later by a different outlet still needs
  edition-time matching.

## Deltas from the original spec

Both surfaced by running the implementation against a copy of production.

**Coverage records carry `run_id`, and revisions exclude their own chain.**
`revise_edition` re-runs the agent over the *same* window, and by then the
parent edition's coverage record exists — so every item the parent featured
came back flagged as a duplicate of itself, which would have told the agent to
drop the entire edition. Records now carry `run_id`, and `find_prior_coverage`
takes `exclude_run_ids`, which `revise_edition` populates from
`revision_chain(parent_run)`. A revision replaces its parent, so that parent's
coverage is not already-covered ground.

**Coverage retention is the dedup lookback, not the headline window, and the
backfill respects it.** Pruning coverage on `AGENT_HEADLINES_RETENTION_DAYS`
(7) would delete half the corpus the 14-day matcher needs, so both prune call
sites use `AGENT_DEDUP_LOOKBACK_DAYS`. Relatedly, the backfill originally wrote
records for every edition ever generated; startup pruning then deleted the
older ones, so re-running it looked non-idempotent (9 written, 9 skipped, on
every run). It now only considers editions inside the retention window and is
genuinely idempotent.

## Known caveats

- **Multilingual corpus.** `stop_words="english"` leaves Dutch stopwords in, which
  inflates similarity between any two Dutch texts (Tweakers.net). The one Dutch
  pair observed at 0.449 was a true positive, so this is not currently harmful,
  but it should be re-checked as Dutch volume grows.
- **Thresholds are corpus-dependent.** Re-run `scripts/dedup_report.py` after
  significant source changes; the background-corpus IDF fit keeps them stable
  against pure size growth, not against vocabulary shifts.
- **Coverage extraction is regex-based** over the serialized document. It is
  already the basis of the on-screen coverage box, so accuracy problems there are
  pre-existing rather than introduced here.
