"""In-process state + pub/sub for the admin "re-tag items" background job.

Tracks the single in-flight retag run (there's only ever one at a time on
this single-worker deployment) so the Admin page can both render the
correct button state on a plain page load — no SSE needed for that — and
stream live progress once a client subscribes. A late subscriber (e.g. the
admin reopening the modal mid-run) immediately gets a "state" event with
the current snapshot, unlike app/services/poll_registry.py which is
fire-and-forget events only; retag runs are long enough that this matters.
"""
from __future__ import annotations

import queue
import threading

_lock = threading.Lock()
_subscribers: list[queue.Queue] = []
_state: dict = {
    "running": False,
    "processed": 0,
    "total": 0,
    "start": None,
    "end": None,
    "error": None,
}


def subscribe() -> queue.Queue:
    q: queue.Queue = queue.Queue()
    with _lock:
        _subscribers.append(q)
        snapshot = dict(_state)
    q.put({"type": "state", **snapshot})
    return q


def unsubscribe(q: queue.Queue) -> None:
    with _lock:
        if q in _subscribers:
            _subscribers.remove(q)


def _emit(event: dict) -> None:
    with _lock:
        subs = list(_subscribers)
    for q in subs:
        q.put(event)


def is_running() -> bool:
    with _lock:
        return _state["running"]


def snapshot() -> dict:
    with _lock:
        return dict(_state)


def start(total: int, start_dt: str | None, end_dt: str | None) -> None:
    with _lock:
        _state.update(
            running=True, processed=0, total=total, start=start_dt, end=end_dt, error=None,
        )
    _emit({"type": "started", "total": total, "start": start_dt, "end": end_dt})


def progress(processed: int) -> None:
    with _lock:
        _state["processed"] = processed
        total = _state["total"]
    _emit({"type": "progress", "processed": processed, "total": total})


def done(error: str | None = None) -> None:
    with _lock:
        _state["running"] = False
        _state["error"] = error
    _emit({"type": "done", "error": error})
