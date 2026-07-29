#!/usr/bin/env python3
"""Measure cross-edition story duplication with TF-IDF similarity.

Answers "is the editor re-reporting stories it already covered?" by scoring
each edition's covered items against the items covered by *earlier* editions,
using the same TF-IDF + cosine approach as app/tagging/nb.py.

This is the calibration tool behind docs/story-dedup-spec.md — run it to pick
or re-check the similarity threshold as the corpus grows. It only reads.

Run against the local DB:
    python scripts/dedup_report.py

...or any other, via the app's normal DATABASE_URL config:
    DATABASE_URL=sqlite:////path/to/ainews.db python scripts/dedup_report.py

Modes:
    (default)          score distribution + the highest-scoring pairs
    --band LO HI       only pairs inside a score band, to inspect the boundary
    --trace TEXT       every item matching TEXT, with a pairwise score matrix
"""
from __future__ import annotations

import argparse
from datetime import timedelta

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from app import create_app
from app.extensions import db
from app.models import NewsItem, Summary, SummaryRun
from app.services.coverage import edition_coverage

# Thresholds the spec settles on; see docs/story-dedup-spec.md for the
# production measurements these came from.
DEFAULT_THRESHOLD = 0.35
DEFAULT_LOOKBACK_DAYS = 14


def item_text(item: NewsItem) -> str:
    """The text a story is matched on: headline plus its one-line gist.

    one_liner is preferred over summary_text — it's the LLM's own compression
    of the story, so it carries the entities that identify it without the
    surrounding prose that dilutes the vector.
    """
    parts = [item.title or ""]
    if item.one_liner:
        parts.append(item.one_liner)
    elif item.summary_text:
        parts.append((item.summary_text or "")[:300])
    return " ".join(parts).strip()


def editions(summary_id: int) -> list[SummaryRun]:
    """Runs with a stored document, oldest first, one per label.

    Revisions share a label (a re-run of "Tuesday July 14" is still that
    edition), so keeping the newest per label avoids scoring an edition
    against its own earlier draft.
    """
    runs = (
        SummaryRun.query.filter_by(summary_id=summary_id)
        .filter(SummaryRun.document.isnot(None))
        .order_by(SummaryRun.generated_at.asc())
        .all()
    )
    by_label: dict[str, SummaryRun] = {}
    for run in runs:
        by_label[run.label] = run
    return sorted(by_label.values(), key=lambda r: r.generated_at)


def covered_items(runs: list[SummaryRun]) -> dict[int, list[NewsItem]]:
    """Map run id → the in-scope items that edition actually cited."""
    return {run.id: edition_coverage(run)["covered"] for run in runs}


def background_corpus() -> list[str]:
    """All item texts, so IDF reflects real news vocabulary at DB scale.

    Same trick as app/tagging/nb.py's Scorer: without it, thresholds drift as
    the corpus grows because IDF is computed from a handful of documents.
    """
    return [t for t in (item_text(i) for i in NewsItem.query.all()) if t]


def score_pairs(
    summary_id: int, lookback_days: int,
) -> tuple[list[tuple], dict[str, list[float]]]:
    """Score every covered item against items covered by earlier editions.

    Returns (pairs, per_edition_scores) where each pair is
    (score, run, candidate_item, best_prior_item, prior_run), sorted by
    descending score.
    """
    runs = editions(summary_id)
    covered = covered_items(runs)
    background = background_corpus()

    pairs: list[tuple] = []
    per_edition: dict[str, list[float]] = {}

    for idx, run in enumerate(runs):
        if idx == 0:
            continue  # nothing precedes the first edition
        floor = run.range_end - timedelta(days=lookback_days)
        prior = [
            (item, prev)
            for prev in runs[:idx]
            if prev.range_end >= floor
            for item in covered[prev.id]
        ]
        candidates = covered[run.id]
        if not prior or not candidates:
            continue

        prior_texts = [item_text(i) for i, _ in prior]
        cand_texts = [item_text(i) for i in candidates]
        vec = TfidfVectorizer(stop_words="english", min_df=1)
        try:
            vec.fit(background + prior_texts + cand_texts)
            sims = cosine_similarity(
                vec.transform(cand_texts), vec.transform(prior_texts)
            )
        except ValueError:
            continue  # empty vocabulary — nothing scoreable this edition

        scores = []
        for i, cand in enumerate(candidates):
            j = int(sims[i].argmax())
            score = float(sims[i][j])
            scores.append(score)
            pairs.append((score, run, cand, prior[j][0], prior[j][1]))
        per_edition[run.label] = scores

    pairs.sort(key=lambda p: -p[0])
    return pairs, per_edition


def print_pair(score: float, run, cand, prior_item, prior_run, *, verbose: bool) -> None:
    print(f"\n[{score:.3f}] {run.label}  <-- prior: {prior_run.label}")
    print(f"   NEW  [{cand.id}] {(cand.title or '')[:96]}")
    if verbose:
        print(f"        {(cand.one_liner or '')[:118]}")
    print(f"   OLD  [{prior_item.id}] {(prior_item.title or '')[:96]}")
    if verbose:
        print(f"        {(prior_item.one_liner or '')[:118]}")


def report(args) -> None:
    pairs, per_edition = score_pairs(args.summary_id, args.lookback)
    if not pairs:
        print("No scoreable pairs — need at least two editions with coverage.")
        return

    print(f"scored {len(pairs)} covered-item vs prior-coverage pairs "
          f"across {len(per_edition)} editions (lookback {args.lookback}d)\n")

    print("score distribution (max similarity per covered item):")
    buckets = [(0.6, 1.01), (0.5, 0.6), (0.4, 0.5), (0.3, 0.4),
               (0.2, 0.3), (0.1, 0.2), (0.0, 0.1)]
    for lo, hi in buckets:
        n = sum(1 for s, *_ in pairs if lo <= s < hi)
        print(f"  {lo:.1f}-{min(hi, 1.0):.1f}  {n:4d}  {'#' * min(60, n)}")

    print(f"\nflagged per edition at threshold {args.threshold}:")
    print(f"  {'edition':<24}{'covered':>8}{'flagged':>9}")
    total_cov = total_flag = 0
    for label, scores in per_edition.items():
        flagged = sum(1 for s in scores if s >= args.threshold)
        total_cov += len(scores)
        total_flag += flagged
        print(f"  {label:<24}{len(scores):>8}{flagged:>9}")
    print(f"  {'TOTAL':<24}{total_cov:>8}{total_flag:>9}")
    if per_edition:
        print(f"  ≈{total_flag / len(per_edition):.1f} flagged per edition "
              f"({total_flag / total_cov * 100:.1f}% of covered items)")

    print("\n" + "=" * 76)
    print(f"TOP {args.top} PAIRS")
    print("=" * 76)
    for p in pairs[:args.top]:
        print_pair(*p, verbose=args.verbose)


def band(args) -> None:
    lo, hi = args.band
    pairs, _ = score_pairs(args.summary_id, args.lookback)
    selected = [p for p in pairs if lo <= p[0] < hi]
    print(f"{len(selected)} pairs scoring {lo}–{hi} "
          f"(of {len(pairs)}); showing up to {args.top}\n")
    print("=" * 76)
    for p in selected[:args.top]:
        print_pair(*p, verbose=args.verbose)


def trace(args) -> None:
    needle = args.trace.lower()
    runs = editions(args.summary_id)
    covered = covered_items(runs)
    run_of = {
        item.id: run.label for run in runs for item in covered[run.id]
    }

    hits = [
        i for i in NewsItem.query.order_by(NewsItem.id).all()
        if needle in (i.title or "").lower() or needle in (i.one_liner or "").lower()
    ]
    print(f"items mentioning {args.trace!r}: {len(hits)}\n")
    for item in hits:
        label = run_of.get(item.id)
        when = item.published_at or item.fetched_at
        mark = f"COVERED in {label}" if label else "not covered"
        print(f"  [{item.id}] {when:%m-%d}  {mark}")
        print(f"        {(item.title or '')[:94]}")

    shown = [i for i in hits if i.id in run_of]
    if len(shown) < 2:
        print("\n(need two or more covered items for a score matrix)")
        return

    vec = TfidfVectorizer(stop_words="english", min_df=1)
    vec.fit(background_corpus())
    matrix = vec.transform([item_text(i) for i in shown])
    sims = cosine_similarity(matrix, matrix)
    print("\npairwise similarity between the COVERED items:")
    print("        " + "".join(f"{i.id:>8}" for i in shown))
    for i, item in enumerate(shown):
        cells = "".join(
            "     -- " if i == j else f"{sims[i][j]:>8.3f}"
            for j in range(len(shown))
        )
        print(f"  [{item.id:>4}]{cells}   ({run_of[item.id]})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--summary-id", type=int, default=None,
                        help="Dispatch to analyse (default: the first agentic one)")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help=f"Flag threshold (default {DEFAULT_THRESHOLD})")
    parser.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK_DAYS,
                        help=f"Days of prior coverage (default {DEFAULT_LOOKBACK_DAYS})")
    parser.add_argument("--top", type=int, default=30, help="Pairs to print")
    parser.add_argument("--band", type=float, nargs=2, metavar=("LO", "HI"),
                        help="Print only pairs scoring in [LO, HI)")
    parser.add_argument("--trace", metavar="TEXT",
                        help="Trace one story: items matching TEXT + score matrix")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Include one-liners in pair output")
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        if args.summary_id is None:
            first = Summary.query.filter_by(type_key="agentic_page").first()
            if first is None:
                parser.error("No agentic Dispatch found; pass --summary-id.")
            args.summary_id = first.id
            summary = first
        else:
            summary = db.session.get(Summary, args.summary_id)
            if summary is None:
                parser.error(f"No Dispatch with id {args.summary_id}.")
        print(f"Dispatch {summary.id}: {summary.name!r} (period={summary.period})\n")

        if args.trace:
            trace(args)
        elif args.band:
            band(args)
        else:
            report(args)


if __name__ == "__main__":
    main()
