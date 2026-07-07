"""Base classes and data structures for pluggable news sources."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class RawDocument:
    """A raw item fetched from a source, before NL extraction."""

    external_id: str
    text: str
    received_at: datetime | None = None
    subject: str | None = None
    meta: dict = field(default_factory=dict)


@dataclass
class ExtractedItem:
    """A discrete news item produced from a RawDocument."""

    title: str
    summary: str
    url: str | None = None
    published_at: datetime | None = None
    item_type: str | None = None       # paper|announcement|blog|news|tool|opinion|other
    one_liner: str | None = None       # single-sentence LLM summary
    full_text: str | None = None       # full text when no URL (offline items)


class NewsSource(ABC):
    """A pluggable news source.

    Subclasses set ``type_key`` and ``label`` and implement ``fetch``.
    Extraction of natural-language documents into discrete items is handled
    centrally (see ``app.sources.extract``); a source may override ``extract``
    if it already returns structured items.
    """

    type_key: str = ""
    label: str = ""
    description: str = ""
    # Declarative config schema: {field: {"type", "label", "required", "secret"}}
    config_schema: dict = {}

    def __init__(
        self,
        config: dict | None = None,
        *,
        api_key: str | None = None,
        model: str | None = None,
        usage_hook=None,
    ):
        self.config = config or {}
        # Credentials from the Source's assigned ApiKey (see services.ingest),
        # used by the default LLM-based extract() below. usage_hook, if set,
        # is called with each LLM response's usage dict for cost accounting.
        self.api_key = api_key
        self.model = model
        self.usage_hook = usage_hook

    @abstractmethod
    def fetch(self, since: datetime | None) -> list[RawDocument]:
        """Return raw documents available since ``since`` (None = all/new)."""
        raise NotImplementedError

    def extract(self, doc: RawDocument) -> list[ExtractedItem]:
        """Turn one raw document into discrete news items.

        Default implementation uses the shared LLM-based extractor.
        """
        from .extract import extract_items

        return extract_items(
            doc, api_key=self.api_key, model=self.model, usage_hook=self.usage_hook
        )
