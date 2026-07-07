"""In-process pub/sub for admin source-polling progress.

Lets the Admin page show live per-source progress (start/done/error) while
"Poll all sources now" or an individual "Poll now" runs in a background
thread, instead of the request hanging until every IMAP/RSS fetch completes.

Single-process in-memory state — consistent with this app's existing SSE
event queues (see app/services/generation_registry.py) — the app runs a
single gunicorn worker.
"""
from __future__ import annotations

import queue
import threading

_lock = threading.Lock()
_subscribers: list[queue.Queue] = []
_active_source_ids: set[int] = set()


def subscribe() -> queue.Queue:
    q: queue.Queue = queue.Queue()
    with _lock:
        _subscribers.append(q)
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


def is_polling(source_id: int) -> bool:
    with _lock:
        return source_id in _active_source_ids


def source_started(source_id: int, name: str) -> None:
    with _lock:
        _active_source_ids.add(source_id)
    _emit({"type": "source_start", "source_id": source_id, "name": name})


def source_done(source_id: int, status_text: str, error: bool) -> None:
    with _lock:
        _active_source_ids.discard(source_id)
    _emit({"type": "source_done", "source_id": source_id, "status_text": status_text, "error": error})


def batch_done(totals: dict) -> None:
    _emit({"type": "batch_done", "totals": totals})
