"""Management CLI: `python manage.py <command>`.

Commands:
  init-db           Create all tables (quick start without Alembic).
  seed-tags         Insert a small starter global taxonomy.
  poll              Poll all due sources once (force).
  rerender-editions Re-render stored HTML for all agentic editions from their block documents.
  backfill-coverage Persist coverage records for editions generated before they existed.
  collapse-dupes    Merge pre-existing duplicate news items (same title, forked by URL).
  generate-review   Cut a review edition, optionally over an explicit date range.
  run               Run the dev server.
"""
from __future__ import annotations

import sys

from app import create_app
from app.extensions import db


def init_db(app):
    with app.app_context():
        db.create_all()
        print("Database tables created.")


def seed_tags(app):
    from app.models import Tag

    starter = [
        ("LLMs", ["language model", "gpt", "llm", "transformer", "chatbot"],
         "Large language models and chat assistants."),
        ("Robotics", ["robot", "humanoid", "actuator", "embodied"],
         "Physical robots and embodied AI."),
        ("Policy & Regulation", ["regulation", "law", "policy", "eu ai act", "governance"],
         "AI laws, regulation and governance."),
        ("Research", ["paper", "benchmark", "arxiv", "study", "model release"],
         "Research papers, benchmarks and new model releases."),
        ("Funding & Business", ["funding", "raise", "valuation", "startup", "acquisition"],
         "Company funding, M&A and business news."),
    ]
    with app.app_context():
        added = 0
        for name, kw, expl in starter:
            if not Tag.query.filter_by(name=name).first():
                db.session.add(Tag(name=name, keywords=kw, explanation=expl, scope="global"))
                added += 1
        db.session.commit()
        print(f"Seeded {added} global tags.")


def poll(app):
    from app.services import ingest

    with app.app_context():
        print(ingest.ingest_all_due(force=True))


def rerender_editions(app):
    from app.agent.render import render_html
    from app.models import SummaryRun

    with app.app_context():
        runs = SummaryRun.query.filter(SummaryRun.document.isnot(None)).all()
        updated = 0
        for run in runs:
            if not run.document:
                continue
            try:
                run.content = render_html(run.document)
                updated += 1
            except Exception as e:
                print(f"  Run {run.id}: failed — {e}")
        db.session.commit()
        print(f"Re-rendered {updated} editions.")


def backfill_coverage(app):
    """Persist a coverage record for every edition that predates them.

    Cross-edition dedup matches against these records, so without a backfill
    the feature stays blind until a full retention window of new editions has
    accumulated. Idempotent — skips editions that already have one.
    """
    from datetime import timedelta

    from app.agent import memory as agent_memory
    from app.models import SummaryRun, utcnow
    from app.services.coverage import edition_coverage

    with app.app_context():
        # Records older than the dedup lookback are pruned on the next startup,
        # so writing them would just churn rows and make repeat runs look
        # non-idempotent.
        days = app.config.get("AGENT_DEDUP_LOOKBACK_DAYS", 14)
        floor = utcnow().replace(tzinfo=None) - timedelta(days=days)
        runs = (
            SummaryRun.query.filter(SummaryRun.document.isnot(None))
            .filter(SummaryRun.generated_at >= floor)
            .order_by(SummaryRun.generated_at.asc())
            .all()
        )
        written = skipped = empty = 0
        for run in runs:
            if agent_memory.coverage_exists(run.summary_id, run.generated_at):
                skipped += 1
                continue
            summary = run.summary
            if summary is None or summary.user is None:
                skipped += 1
                continue
            try:
                covered = edition_coverage(run)["covered"]
            except Exception as e:  # noqa: BLE001
                print(f"  Run {run.id}: failed — {e}")
                continue
            if not covered:
                empty += 1
                continue
            agent_memory.write_coverage(
                summary.user, summary, run.generated_at,
                [
                    {"item_id": i.id, "title": i.title, "url": i.url, "run_id": run.id}
                    for i in covered
                ],
            )
            written += 1
        print(
            f"Backfilled {written} coverage record(s) from the last {days} day(s); "
            f"{skipped} already present or unusable, {empty} with no cited items."
        )


def collapse_dupes(app, apply=False):
    """Merge rows that share a headline but were forked apart by their URLs.

    The ingest-side guard only prevents new forks; rows created before it
    existed stay split. Keeps the copy with a real article link and deletes
    the redundant ones.

    Rows that any past edition counts as covered are never deleted. Edition
    documents reference items as plain data rather than foreign keys, and
    coverage is resolved against a per-edition time window — so a surviving
    twin cannot stand in for a deleted one if it falls outside that window,
    and the historical coverage box would quietly change. Those rows are also
    long outside any live edition's scope, so deleting them buys nothing.

    Dry-run by default: pass --apply to actually write.
    """
    from collections import defaultdict

    from app.models import NewsItem, NewsItemTag, SummaryRun
    from app.services.coverage import edition_coverage
    from app.urls import looks_like_article_url

    with app.app_context():
        cited: set[int] = set()
        for run in SummaryRun.query.filter(SummaryRun.document.isnot(None)).all():
            if run.summary is None:
                continue
            cited |= {i.id for i in edition_coverage(run)["covered"]}

        groups = defaultdict(list)
        for item in NewsItem.query.order_by(NewsItem.id).all():
            key = (item.title or "").strip().lower()
            if key:
                groups[key].append(item)

        merged = removed = protected = 0
        for rows in groups.values():
            if len(rows) < 2:
                continue
            # Leave genuine cross-outlet duplicates alone — every copy having
            # its own real article link means these are different reports of
            # one story, which is story_dedup's call to make at edition time.
            with_url = [r for r in rows if looks_like_article_url(r.url)]
            if len(with_url) == len(rows):
                continue

            losers = [r for r in rows if r.id not in cited]
            if not losers:
                protected += 1
                continue
            keepers = [r for r in rows if r.id in cited]
            if keepers:
                # Something already ran under this headline — keep it, exactly,
                # so every edition's coverage stays byte-identical.
                keeper = next(
                    (r for r in keepers if looks_like_article_url(r.url)), keepers[0]
                )
            else:
                keeper = with_url[0] if with_url else rows[0]
                losers = [r for r in losers if r.id != keeper.id]
            if not losers:
                protected += 1
                continue

            print(f"  keep [{keeper.id}]{' (cited)' if keeper.id in cited else ''} "
                  f"{(keeper.title or '')[:60]}")
            for loser in losers:
                print(f"    drop [{loser.id}] url={(loser.url or '')[:50]!r}")
            merged += 1
            removed += len(losers)

            if not apply:
                continue
            for loser in losers:
                NewsItemTag.query.filter_by(news_item_id=loser.id).delete(
                    synchronize_session=False
                )
                db.session.delete(loser)
            if not looks_like_article_url(keeper.url):
                better = next(
                    (r.url for r in losers if looks_like_article_url(r.url)), None
                )
                if better:
                    keeper.url = better
            keeper.dedup_hash = NewsItem.make_hash(keeper.title, keeper.url)

        if apply:
            db.session.commit()
        verb = "Merged" if apply else "Would merge"
        print(f"\n{verb} {merged} group(s), removing {removed} row(s).")
        if protected:
            print(f"Left {protected} group(s) untouched — every copy is cited "
                  f"by a past edition.")
        if not apply:
            print("(dry run — pass --apply to write)")


def generate_review(app, argv):
    """Cut a review edition for one Dispatch.

    With no dates, covers the most recent *completed* review period. An
    explicit range is needed to review a period that has not ended yet — e.g.
    reviewing July while July is still running.

        python manage.py generate-review 3
        python manage.py generate-review 3 --start 2026-07-01 --end 2026-08-01

    Spends real money on the Dispatch owner's OpenRouter key.
    """
    from datetime import datetime, timezone

    from app.models import Summary
    from app.services import summarize

    if not argv or not argv[0].isdigit():
        print("Usage: generate-review <summary_id> [--start YYYY-MM-DD --end YYYY-MM-DD]")
        sys.exit(1)
    summary_id = int(argv[0])

    def _opt(name):
        if name in argv:
            return datetime.fromisoformat(argv[argv.index(name) + 1]).replace(
                tzinfo=timezone.utc
            )
        return None

    start, end = _opt("--start"), _opt("--end")

    with app.app_context():
        summary = db.session.get(Summary, summary_id)
        if summary is None:
            print(f"No Dispatch with id {summary_id}.")
            sys.exit(1)
        if start is None or end is None:
            start, end = summarize.resolve_review_range(summary)
            if start is None:
                print("Reviews are off for this Dispatch; pass --start/--end to override.")
                sys.exit(1)

        from app.services.review_digest import digest_for_range
        n = len(digest_for_range(summary, start, end))
        print(f"Reviewing {n} edition(s) of {summary.name!r}: "
              f"{start.date()} → {end.date()}")
        if not n:
            print("Nothing to review.")
            sys.exit(1)

        run = summarize.build_review(summary, start, end)
        print(f"Cut review run {run.id}: {run.label!r} — {run.headline!r}")
        print(f"  cost: ${run.agent_cost or 0:.4f}")


def main():
    app = create_app()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "init-db":
        init_db(app)
    elif cmd == "seed-tags":
        seed_tags(app)
    elif cmd == "poll":
        poll(app)
    elif cmd == "rerender-editions":
        rerender_editions(app)
    elif cmd == "backfill-coverage":
        backfill_coverage(app)
    elif cmd == "collapse-dupes":
        collapse_dupes(app, apply="--apply" in sys.argv)
    elif cmd == "generate-review":
        generate_review(app, sys.argv[2:])
    elif cmd == "run":
        app.run(host="0.0.0.0", port=app.config["PORT"], debug=True)
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
