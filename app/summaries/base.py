"""Base classes for pluggable news summaries."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Artifact:
    """Result of building a summary.

    kind: "html" (rendered inline), "file" (downloadable), or "audio".
    """

    kind: str
    title: str
    html: str | None = None
    file_path: str | None = None
    mime: str | None = None


class NewsSummary(ABC):
    """A pluggable summary format.

    Subclasses set ``type_key`` / ``label`` and implement ``build``.
    """

    type_key: str = ""
    label: str = ""
    description: str = ""
    # Declarative param schema for the config form.
    param_schema: dict = {}

    @abstractmethod
    def build(
        self,
        items: list,
        params: dict,
        *,
        range_start: datetime | None = None,
        range_end: datetime | None = None,
    ) -> Artifact:
        raise NotImplementedError
