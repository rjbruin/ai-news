"""Management CLI: `python manage.py <command>`.

Commands:
  init-db           Create all tables (quick start without Alembic).
  seed-tags         Insert a small starter global taxonomy.
  poll              Poll all due sources once (force).
  rerender-editions Re-render stored HTML for all agentic editions from their block documents.
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
    elif cmd == "run":
        app.run(host="0.0.0.0", port=app.config["PORT"], debug=True)
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
