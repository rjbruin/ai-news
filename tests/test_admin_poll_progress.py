from app.models import ApiKey, Source
from app.services import ingest, poll_registry


def _seed_source(db, **kw):
    key = ApiKey(label="Test key", provider="openrouter")
    key.set_key("sk-or-test")
    db.session.add(key)
    db.session.commit()
    source = Source(type_key="seed", name="Debug Seed Data", enabled=True, api_key_id=key.id, **kw)
    db.session.add(source)
    db.session.commit()
    return source


# ───────────────────────── poll_registry ─────────────────────────
def test_poll_registry_pubsub_and_active_state():
    poll_registry._subscribers.clear()
    poll_registry._active_source_ids.clear()

    q = poll_registry.subscribe()
    assert not poll_registry.is_polling(42)

    poll_registry.source_started(42, "My Source")
    assert poll_registry.is_polling(42)
    event = q.get(timeout=1)
    assert event == {"type": "source_start", "source_id": 42, "name": "My Source"}

    poll_registry.source_done(42, "1 new item, 1 checked", False)
    assert not poll_registry.is_polling(42)
    event = q.get(timeout=1)
    assert event == {
        "type": "source_done", "source_id": 42,
        "status_text": "1 new item, 1 checked", "error": False,
    }

    poll_registry.unsubscribe(q)
    poll_registry.batch_done({"sources": 1})
    assert q.empty()


# ───────────────────────── ingest_all_due progress_hook ─────────────────────────
def test_ingest_all_due_calls_progress_hook(db, app):
    _seed_source(db)
    events = []

    def hook(source, phase, stats=None):
        events.append((source.name, phase, stats["new_items"] if stats else None))

    with app.app_context():
        ingest.ingest_all_due(force=True, progress_hook=hook)

    assert events[0] == ("Debug Seed Data", "start", None)
    assert events[1][0] == "Debug Seed Data"
    assert events[1][1] == "done"
    assert events[1][2] is not None


# ───────────────────────── admin routes ─────────────────────────
def test_source_poll_start_requires_admin(auth_client, db):
    source = _seed_source(db)
    resp = auth_client.post(f"/admin/sources/{source.id}/poll/start")
    assert resp.status_code == 403


def test_source_poll_start_rejects_newsletter_children(admin_client, db):
    mailbox = Source(type_key="imap_newsletter", name="Mailbox", config={}, enabled=True)
    db.session.add(mailbox)
    db.session.commit()
    child = Source(
        type_key="imap_newsletter", name="TLDR AI", parent_source_id=mailbox.id,
        config={"newsletter_sender": "x@y.com"}, enabled=True,
    )
    db.session.add(child)
    db.session.commit()

    resp = admin_client.post(f"/admin/sources/{child.id}/poll/start")
    assert resp.status_code == 400


def _run_threads_synchronously(monkeypatch):
    """The route under test starts a daemon thread whose db.session lives on
    a separate SQLite :memory: connection in tests (each thread gets its own
    empty in-memory DB, unlike production's shared file-based DB) — so make
    the thread run inline instead, which is deterministic and still exercises
    the exact same target function."""
    import threading as threading_module

    class _SyncThread:
        def __init__(self, target=None, daemon=None, **kw):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(threading_module, "Thread", _SyncThread)


def test_source_poll_start_runs_source_in_background(admin_client, db, monkeypatch):
    _run_threads_synchronously(monkeypatch)
    source = _seed_source(db)
    resp = admin_client.post(f"/admin/sources/{source.id}/poll/start")
    assert resp.status_code == 202
    db.session.refresh(source)
    assert source.last_polled_at is not None


def test_poll_all_start_polls_due_sources_in_background(admin_client, db, monkeypatch):
    _run_threads_synchronously(monkeypatch)
    source = _seed_source(db)
    resp = admin_client.post("/admin/poll-all/start")
    assert resp.status_code == 202
    db.session.refresh(source)
    assert source.last_polled_at is not None


def test_poll_events_stream_is_sse(admin_client, monkeypatch):
    from app.web import admin as admin_module

    monkeypatch.setattr(admin_module, "_POLL_SSE_HEARTBEAT_SECONDS", 0.01)
    resp = admin_client.get("/admin/poll-events")
    assert resp.status_code == 200
    assert resp.mimetype == "text/event-stream"
    resp.close()
