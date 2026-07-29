# Review editions

Status: in progress. Measurements come from the production Dispatch (id 3,
"Daily agent") on 2026-07-29.

A Dispatch owner can opt into **review editions**: a slower cadence
(weekly/monthly/quarterly/yearly) that looks back over the period's *editions*
rather than over raw news items, and writes a retrospective.

## Why reviews are runs, not a second Dispatch

A review has to share the Dispatch's followers, publishing state, email
subscribers, podcast feed, calendar and detail page. Modelling it as a second
`Summary` row forks every one of those. So a review is a `SummaryRun` with a
discriminator:

- `SummaryRun.kind` — `'edition' | 'review'`, default `'edition'`, indexed
  together with `summary_id`.
- `Summary.review_period` — nullable; `NULL` means reviews are off. One of
  `week | month | quarter | year`.
- A new `AgentMemory` kind `review_content_config` — the owner's free-text
  description of what a review should contain.

There is deliberately **no `review_interests`**. Reviews read editions, and an
edition already reflects the reader's interests; a second copy would drift.

### The failure mode this creates

Eleven queries across six files fetch "the latest run" ordered by
`generated_at`/`range_end`. Two of them drive scheduling —
`summarize.resolve_range` and `cut_due_editions`'s already-cut guard. If either
sees a review run, it concludes the daily period is already covered and **daily
editions stop being cut, silently**.

Every such query is audited and kind-filtered, and a regression test asserts
that cutting a review does not suppress the next daily edition. This is the
highest-risk part of the change.

## The digest — derived, not editor-written

The review editor must not be handed whole editions. It receives, per edition:
the edition headline and subheader, and the headline of every *featured* item.
Item subheaders, item bodies and quick hits (`more_news`) are excluded. It can
then pull the full text of **one** item at a time with a tool call.

That digest is **computed from the stored block document**, not written by the
original editor at generation time:

- Every field needed already exists in the document — `edition_header.title`
  and `.subtitle`, and `item.headline`. Quick hits are a separate block type,
  so excluding them is a type filter.
- It works **retroactively**, which the first real review requires: no editor
  ever wrote a digest for the editions already in the database.
- An editor-written digest can drift from what was actually published. That is
  exactly the failure mode `docs/story-dedup-spec.md` documents for prose
  headline notes.

Measured over the real July 2026 editions: **14 editions, 180 featured items →
222 lines, 20,063 chars, ≈5,000 tokens.** Affordable at month scale with no
truncation.

A year (~250 editions) would be ~90k tokens and needs tiering. Beyond a
configurable line budget the digest drops to headline-only per edition, and
`log()`s what it dropped — a silently truncated digest would read to the editor
as a complete record of the period.

## Review harness

`run_agent` gains optional `system_prompt` and `tool_specs` parameters,
defaulting to today's behaviour, so review mode swaps both without forking the
loop.

Tools available in review mode:

| Tool | Purpose |
|---|---|
| `list_editions_in_scope` | the digest, each item carrying its block id |
| `get_edition_item(run_id, block_id)` | full text of **one** item |
| editor tools | unchanged (`set_document`, `add_block`, …) |
| `read_memory` / `write_memory` | `review_content_config` only |

`get_edition` is **not** exposed in review mode — it returns whole documents
and would defeat the entire budget. `write_headlines` is dropped too: the next
review receives the previous review's full document, so per-edition notes add
nothing.

The system prompt carries the review role, the review content config, the
available topics, and **the full previous review edition** — with no interests
section.

## Scheduling

`resolve_review_range` aligns to calendar boundaries: a July review spans
1 July → 1 August, not a rolling 30 days. `cut_due_reviews` sits beside
`cut_due_editions` and reuses the same failure-backoff constants.

A completed period ends at midnight, but the review is not cut until the
Dispatch's own `release_time` on that day (`review_release_at`), so a
monthly review lands in the slot readers already expect an edition rather
than at 00:00 on the 1st.

There is deliberately **no on-demand generate button**. Reviews are cut by
the schedule alone; an on-demand path invites cutting a review over a
period that has not finished, which is exactly the thing the calendar
alignment exists to prevent. `manage.py generate-review` remains for ops,
where an explicit range can be given deliberately.

## Interface

- Reviews are marked with a distinct pill and an accent border on cards, so
  they read differently from a daily edition at a glance.
- The calendar renders **one dot per kind** present on a day, rather than a
  single dot for "something happened".
- The frontpage shows the most recent edition of **each** followed Dispatch
  (previously: one hero across all of them), plus an extra card per unread
  review.
- Dispatch settings gains a Review editions card: period select, the content
  textarea with reset-to-default, and a note that interests are inherited
  and when the next review will be cut.

## Decisions

1. Quarterly is offered alongside weekly/monthly/yearly.
2. Reviews get PDF and podcast auto-generation, like editions — same channel
   machinery.
3. Reviews are emailed to the Dispatch's email subscribers, like editions.
4. Reviews appear in the calendar on their generation date, not spread across
   the period they cover.

## Out of scope

- Per-review-period content configs (one config covers whatever cadence is
  set).
- Reviews of reviews.
