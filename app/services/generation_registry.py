"""In-process registry of in-flight agentic generation runs.

Lets the summaries list page show "Generating…" with Logs/Cancel for editions
currently being produced by a background thread (started from the
generate/stream or feedback/stream SSE endpoints), and lets a second page
visit (or the same visit after navigating away and back) re-attach to the
same run's live event stream instead of starting a duplicate one.

Single-process in-memory state — consistent with this app's existing SSE
event queues, and the app runs a single gunicorn worker.
"""
from __future__ import annotations

import queue
import threading
import time


class GenerationHandle:
    def __init__(self, summary_id: int, kind: str, parent_run_id: int | None = None):
        self.summary_id = summary_id
        self.kind = kind  # "generate" | "revise"
        self.parent_run_id = parent_run_id
        self.cancel_event = threading.Event()
        self.started_at = time.time()
        self._lock = threading.Lock()
        self._events: list[dict] = []
        self._subscribers: list[queue.Queue] = []

    def emit(self, event: dict) -> None:
        """Record an event and fan it out to every currently-attached subscriber."""
        with self._lock:
            self._events.append(event)
            subs = list(self._subscribers)
        for q in subs:
            q.put(event)

    def subscribe(self) -> queue.Queue:
        """Attach a new subscriber, replaying events emitted before it joined."""
        q: queue.Queue = queue.Queue()
        with self._lock:
            for ev in self._events:
                q.put(ev)
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)


_active: dict[int, GenerationHandle] = {}
_lock = threading.Lock()


def start(summary_id: int, kind: str, parent_run_id: int | None = None) -> GenerationHandle:
    handle = GenerationHandle(summary_id, kind, parent_run_id)
    with _lock:
        _active[summary_id] = handle
    return handle


def finish(handle: GenerationHandle) -> None:
    with _lock:
        if _active.get(handle.summary_id) is handle:
            del _active[handle.summary_id]


def get(summary_id: int) -> GenerationHandle | None:
    with _lock:
        return _active.get(summary_id)


def cancel(summary_id: int) -> bool:
    handle = get(summary_id)
    if handle is None:
        return False
    handle.cancel_event.set()
    return True
