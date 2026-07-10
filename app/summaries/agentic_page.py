"""Agentic daily page summary.

Produced by the LLM agent runner (see app.services.summarize). The agent
receives ALL in-scope items (no tagging), decides what to feature and how, and
assembles a block document. This plugin owns rendering that document to the
in-app page channel and declares the config form.
"""
from __future__ import annotations

from datetime import datetime

from ..agent.render import render_html
from .base import Artifact, NewsSummary


class AgenticDailyPageSummary(NewsSummary):
    type_key = "agentic_page"
    label = "Agentic daily page"
    description = (
        "An LLM editor curates and lays out a daily page from all news in scope, "
        "using your interests and past editions. Runs on your OpenRouter key."
    )
    is_agentic = True
    param_schema = {
        "release_time": {
            "type": "time",
            "label": "Daily release time (HH:MM, UTC)",
            "default": "08:00",
            "scope": "day",
        },
        "release_days": {
            "type": "checkboxes",
            "label": "Release days",
            "default": [0, 1, 2, 3, 4],
            "scope": "day",
            "options": [
                (0, "Mon"), (1, "Tue"), (2, "Wed"),
                (3, "Thu"), (4, "Fri"), (5, "Sat"), (6, "Sun"),
            ],
        },
        "send_email": {
            "type": "checkbox",
            "label": "Send as email newsletter when edition is cut",
            "default": False,
        },
        "max_steps": {
            "type": "number",
            "label": "Max agent steps per edition",
            "default": 50,
            "min": 1,
            "max": 200,
        },
        "topic_tiers": {
            "type": "topic_tiers",
            "label": "Topic emphasis",
            "default": {},
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
        """Render a block document (passed via params['_document']) to HTML.

        The agent run happens in services.summarize, which injects the resulting
        document here so this plugin remains the single rendering authority
        (also used to re-render stored editions).
        """
        document = params.get("_document") or []
        html = render_html(document) if document else ""
        return Artifact(kind="html", title="AI Daily", html=html)
