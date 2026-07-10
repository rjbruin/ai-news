from datetime import datetime, timedelta

from app.models import NewsItem, NewsItemTag, Tag
from app.services import retag_registry


def _reset_retag_registry():
    retag_registry._subscribers.clear()
    retag_registry._state.update(
        running=False, processed=0, total=0, start=None, end=None, error=None,
    )


# ───────────────────────── retag_registry ─────────────────────────
def test_retag_registry_pubsub_and_state():
    _reset_retag_registry()
    q = retag_registry.subscribe()
    # A fresh subscriber gets an immediate snapshot event.
    snap = q.get(timeout=1)
    assert snap == {
        "type": "state", "running": False, "processed": 0, "total": 0,
        "start": None, "end": None, "error": None,
    }

    retag_registry.start(2, "2024-01-01T00:00", None)
    assert retag_registry.is_running()
    event = q.get(timeout=1)
    assert event == {"type": "started", "total": 2, "start": "2024-01-01T00:00", "end": None}

    retag_registry.progress(1)
    event = q.get(timeout=1)
    assert event == {"type": "progress", "processed": 1, "total": 2}

    retag_registry.done()
    assert not retag_registry.is_running()
    event = q.get(timeout=1)
    assert event == {"type": "done", "error": None}

    retag_registry.unsubscribe(q)


def test_retag_registry_late_subscriber_gets_current_snapshot():
    _reset_retag_registry()
    retag_registry.start(5, None, None)
    retag_registry.progress(3)

    q = retag_registry.subscribe()
    snap = q.get(timeout=1)
    assert snap["type"] == "state"
    assert snap["running"] is True
    assert snap["processed"] == 3
    assert snap["total"] == 5
    retag_registry.unsubscribe(q)
    _reset_retag_registry()


# ───────────────────────── admin routes ─────────────────────────
def _run_threads_synchronously(monkeypatch):
    """Mirrors tests/test_admin_poll_progress.py's helper: the route under
    test starts a daemon thread whose db.session lives on a separate
    SQLite :memory: connection in tests, so make it run inline instead."""
    import threading as threading_module

    class _SyncThread:
        def __init__(self, target=None, daemon=None, **kw):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(threading_module, "Thread", _SyncThread)


def test_retag_start_requires_admin(auth_client):
    _reset_retag_registry()
    resp = auth_client.post("/admin/retag/start")
    assert resp.status_code == 403


def test_retag_start_runs_in_background_and_completes(admin_client, db, sample_tags, sample_items, monkeypatch):
    _reset_retag_registry()
    _run_threads_synchronously(monkeypatch)
    monkeypatch.setattr(
        "app.tagging.engine.llm.score_item",
        lambda text, tags, **kw: {},
    )

    resp = admin_client.post("/admin/retag/start")
    assert resp.status_code == 202
    assert resp.get_json()["total"] == len(sample_items)

    snap = retag_registry.snapshot()
    assert snap["running"] is False
    assert snap["processed"] == len(sample_items)
    assert snap["error"] is None


def test_retag_start_already_running_does_not_spawn_another(admin_client, monkeypatch):
    _reset_retag_registry()
    retag_registry.start(10, None, None)

    spawned = []
    monkeypatch.setattr(
        "threading.Thread",
        lambda target=None, daemon=None, **kw: spawned.append(target) or type("T", (), {"start": lambda self: None})(),
    )

    resp = admin_client.post("/admin/retag/start")
    assert resp.status_code == 202
    assert resp.get_json()["status"] == "already_running"
    assert spawned == []
    _reset_retag_registry()


def test_retag_start_filters_by_time_range(admin_client, db, sample_tags, monkeypatch):
    _reset_retag_registry()
    _run_threads_synchronously(monkeypatch)
    monkeypatch.setattr("app.tagging.engine.llm.score_item", lambda text, tags, **kw: {})

    old_item = NewsItem(
        dedup_hash="h-old", title="Old item", url="http://x/old",
        fetched_at=datetime(2020, 1, 1),
    )
    new_item = NewsItem(
        dedup_hash="h-new", title="New item", url="http://x/new",
        fetched_at=datetime.utcnow(),
    )
    db.session.add_all([old_item, new_item])
    db.session.commit()

    resp = admin_client.post(
        "/admin/retag/start",
        data={"start": (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")},
    )
    assert resp.status_code == 202
    assert resp.get_json()["total"] == 1  # only the new item is in range


def test_retag_events_stream_is_sse(admin_client, monkeypatch):
    _reset_retag_registry()
    from app.web import admin as admin_module

    monkeypatch.setattr(admin_module, "_POLL_SSE_HEARTBEAT_SECONDS", 0.01)
    resp = admin_client.get("/admin/retag/events")
    assert resp.status_code == 200
    assert resp.mimetype == "text/event-stream"
    resp.close()
