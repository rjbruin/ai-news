"""Regression tests for the "phone slept, EventSource reconnected" scenario
on edition revision. Two related fixes:

1. app/services/generation_registry.py already replays every event a late
   subscriber missed (verified directly here) — so a reconnect while the
   revision is still running just works, no server change needed.
2. app/web/routes.py::edition_feedback_stream now checks for an
   already-completed child run when generation_registry has no active
   handle, instead of either erroring or silently starting a second,
   duplicate revision.
"""
import json

from app.models import ApiKey, Summary, SummaryRun, User
from app.services import generation_registry


def test_generation_handle_replays_events_to_late_subscriber():
    handle = generation_registry.start(999999, kind="revise", parent_run_id=1)
    try:
        handle.emit({"type": "llm_call", "step": 1})
        handle.emit({"type": "tool_call", "step": 1, "name": "add_block", "args_preview": ""})

        # A subscriber that attaches AFTER those events were emitted (e.g.
        # a client reconnecting post phone-sleep) still receives them.
        q = handle.subscribe()
        assert q.get(timeout=1) == {"type": "llm_call", "step": 1}
        assert q.get(timeout=1) == {"type": "tool_call", "step": 1, "name": "add_block", "args_preview": ""}

        handle.emit({"type": "done", "run_id": 2})
        assert q.get(timeout=1) == {"type": "done", "run_id": 2}
    finally:
        generation_registry.finish(handle)


def _login(client, email, password):
    client.post(
        "/auth/login",
        data={"email": email, "password": password, "submit": "Sign in"},
        follow_redirects=True,
    )


def _seed_summary_with_run(db):
    u = User(username="revisor", email="revisor@example.com", email_verified=True)
    u.set_password("pw")
    db.session.add(u)
    db.session.commit()
    key = ApiKey(owner_user_id=u.id, label="Test edition key")
    key.set_key("sk-or-test")
    db.session.add(key)
    db.session.commit()
    u.edition_api_key_id = key.id
    summary = Summary(
        user_id=u.id, name="AI Daily", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(summary)
    db.session.commit()
    run = SummaryRun(
        summary_id=summary.id, document=[{"type": "divider"}],
        content="<hr>", revision=1,
    )
    db.session.add(run)
    db.session.commit()
    return u, summary, run


def test_feedback_stream_replays_done_when_job_already_finished(client, db):
    u, summary, run = _seed_summary_with_run(db)
    _login(client, u.email, "pw")

    # Simulate: the revision already completed (and generation_registry
    # already cleared its handle) while this client was disconnected.
    rev = SummaryRun(
        summary_id=summary.id, document=[{"type": "divider"}, {"type": "callout"}],
        content="<hr><div>note</div>", revision=2, parent_run_id=run.id,
    )
    db.session.add(rev)
    db.session.commit()
    assert generation_registry.get(summary.id) is None  # no active job

    resp = client.get(f"/summaries/{summary.id}/editions/{run.id}/feedback/stream")
    assert resp.status_code == 200
    assert resp.mimetype == "text/event-stream"
    body = resp.get_data(as_text=True)
    assert body.count("data:") == 1  # exactly one synthetic event, not an open stream
    event = json.loads(body.strip().removeprefix("data:").strip())
    assert event == {"type": "done", "run_id": rev.id}

    # Must not have started a second, duplicate revision.
    assert SummaryRun.query.filter_by(parent_run_id=run.id).count() == 1


def test_feedback_stream_aborts_when_nothing_pending_and_nothing_completed(client, db):
    u, summary, run = _seed_summary_with_run(db)
    _login(client, u.email, "pw")

    # No stashed feedback in session, no active job, no completed child run.
    resp = client.get(f"/summaries/{summary.id}/editions/{run.id}/feedback/stream")
    assert resp.status_code == 400
