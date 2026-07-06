"""In-process registry of in-flight podcast generation jobs, keyed by run id.

Mirrors generation_registry (used for editions): a podcast job runs in a
background daemon thread so it keeps going when the user navigates away, and
the podcast page re-attaches to the same job's live event stream on the next
visit instead of starting a duplicate.

Single-process in-memory state — consistent with this app's existing SSE event
queues, and the app runs a single gunicorn worker.
"""
from __future__ import annotations

import queue
import threading
import time


class PodcastJob:
    """A running podcast generation for one edition run.

    kind is one of:
      "script" — generate the script only, then stop at review
      "audio"  — generate audio from the already-saved script
      "full"   — generate the script then the audio in one go (no review)
      "revise" — regenerate the script from feedback, then stop at review
    """

    def __init__(self, run_id: int, kind: str, feedback: str | None = None):
        self.run_id = run_id
        self.kind = kind
        self.feedback = feedback
        self.started_at = time.time()
        self.done = False
        self._lock = threading.Lock()
        self._events: list[dict] = []
        self._subscribers: list[queue.Queue] = []

    def emit(self, event: dict) -> None:
        """Record an event and fan it out to every currently-attached subscriber."""
        with self._lock:
            self._events.append(event)
            if event.get("type") in ("done", "error"):
                self.done = True
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


_active: dict[int, PodcastJob] = {}
_lock = threading.Lock()


def start(run_id: int, kind: str, feedback: str | None = None) -> PodcastJob:
    job = PodcastJob(run_id, kind, feedback)
    with _lock:
        _active[run_id] = job
    return job


def finish(job: PodcastJob) -> None:
    with _lock:
        if _active.get(job.run_id) is job:
            del _active[job.run_id]


def get(run_id: int) -> PodcastJob | None:
    with _lock:
        return _active.get(run_id)
