"""Regression coverage for "podcast generation/revision keeps running when
the user navigates away" — the background job must not depend on anyone
still listening to its SSE event queue."""
import threading
import time
import unittest.mock as mock

from app.models import Summary, SummaryRun, User
from app.services import podcast
from app.services.podcast_registry import PodcastJob


def _fake_chat_stream(messages, **kw):
    for tok in ["HOST A: ", "Revised ", "script."]:
        time.sleep(0.02)
        yield tok


def test_podcast_revision_completes_after_all_subscribers_disconnect(app, db):
    with app.app_context():
        user = User(username="pod2", email="pod2@example.com", email_verified=True, podcast_enabled=True)
        user.set_password("password123")
        db.session.add(user)
        db.session.commit()

        from app.models import ApiKey
        key = ApiKey(owner_user_id=user.id, label="k")
        key.set_key("sk-or-test")
        db.session.add(key)
        db.session.commit()
        user.edition_api_key_id = key.id
        db.session.commit()

        summary = Summary(
            user_id=user.id, name="Daily", type_key="agentic_page",
            scope_mode="fixed_period", period="day", params={},
        )
        db.session.add(summary)
        db.session.commit()
        run = SummaryRun(summary_id=summary.id, news_podcast_script="HOST A: Hi.")
        db.session.add(run)
        db.session.commit()
        run_id, user_id = run.id, user.id

    job = PodcastJob(run_id=run_id, kind="revise", feedback="be shorter")

    # Simulate a browser tab watching the SSE stream, then navigating away
    # (unsubscribing) while the job is still mid-generation.
    q = job.subscribe()

    def _run():
        with mock.patch("app.llm.openrouter.chat_stream", lambda *a, **kw: _fake_chat_stream(*a, **kw)):
            podcast.run_podcast_job(app, job, run_id, user_id)

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    # Drain a couple of events, then simulate the client disconnecting —
    # exactly what happens when a user navigates away mid-generation.
    q.get(timeout=5)  # phase
    job.unsubscribe(q)

    t.join(timeout=10)
    assert not t.is_alive(), "background job did not finish after the only subscriber left"

    with app.app_context():
        refreshed = db.session.get(SummaryRun, run_id)
        assert refreshed.news_podcast_script == "HOST A: Revised script."
