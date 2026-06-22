"""Debug summary: app-page overview for an explicit time window."""
from __future__ import annotations

from datetime import datetime

from flask import render_template

from .base import Artifact, NewsSummary


class DebugWindowSummary(NewsSummary):
    type_key = "debug_window"
    label = "Debug time window"
    description = "Same overview as the app page, but for an explicit start/end window. Useful for testing."
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
        "group_by_tag": {
            "type": "checkbox",
            "label": "Group items by tag",
            "default": True,
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
        group_by_tag = params.get("group_by_tag", True)
        grouped = None
        if group_by_tag:
            grouped = {}
            for item in items:
                links = list(item.tag_links)
                if not links:
                    grouped.setdefault("Untagged", []).append(item)
                else:
                    best = max(links, key=lambda l: l.confidence or 0)
                    grouped.setdefault(best.tag.name, []).append(item)
            grouped = dict(sorted(grouped.items(), key=lambda kv: kv[0].lower()))

        html = render_template(
            "summaries/app_page.html",
            items=items,
            grouped=grouped,
            range_start=range_start,
            range_end=range_end,
        )
        return Artifact(kind="html", title="Debug overview", html=html)
