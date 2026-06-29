"""Mutable state for a single agent run."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class AgentSession:
    """Holds everything an agent run reads from and writes to.

    The runner constructs this, the tools mutate ``document`` and
    ``pending_headlines``, and the caller persists the result afterwards.
    """

    user: object                      # User
    summary: object                   # Summary
    items: list                       # in-scope NewsItem list (ALL items, no tagging)
    range_start: datetime | None
    range_end: datetime | None
    document: list[dict] = field(default_factory=list)
    pending_headlines: str | None = None   # set via write_headlines tool
    tokens_used: int = 0

    def item_by_id(self, item_id):
        for it in self.items:
            if it.id == item_id:
                return it
        return None
