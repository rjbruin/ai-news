"""Management CLI: `python manage.py <command>`.

Commands:
  init-db           Create all tables (quick start without Alembic).
  seed-tags         Insert a small starter global taxonomy.
  poll              Poll all due sources once (force).
  rerender-editions Re-render stored HTML for all agentic editions from their block documents.
  backfill-coverage Persist coverage records for editions generated before they existed.
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
    elif cmd == "run":
        app.run(host="0.0.0.0", port=app.config["PORT"], debug=True)
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
