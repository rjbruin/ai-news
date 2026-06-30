"""Debug agentic summary: agent run over an explicit time window.

Same LLM pipeline as the agentic daily page, but without a fixed schedule —
you specify the start/end window manually. Useful for testing the agent
against a particular batch of items without waiting for the scheduler.
"""
from __future__ import annotations

from datetime import datetime

from ..agent.render import render_html
from .base import Artifact, NewsSummary


class DebugAgenticSummary(NewsSummary):
    type_key = "debug_agentic"
    label = "Debug agentic window"
    description = (
        "Same LLM-driven curation as the agentic daily page, but for an explicit "
        "start/end window. Useful for testing without waiting for the scheduler."
    )
    is_agentic = True
    param_schema = {
        "range_start": {
            "type": "datetime-local",
            "label": "Start (UTC)",
            "default": "",
        },
        "range_end": {
            "type": "datetime-local",
            "label": "End (UTC)",
            "default": "",
        },
        "max_steps": {
            "type": "number",
            "label": "Max agent steps per edition",
            "default": 50,
            "min": 1,
            "max": 200,
        },
    }

    def build(
        self,
        items: list,
        params: dict,
        *,
        range_start: datetime | None = None,
        range_end: datetime | None = None,
    ) -> Artifact:
        document = params.get("_document") or []
        html = render_html(document) if document else ""
        return Artifact(kind="html", title="Debug AI Daily", html=html)
