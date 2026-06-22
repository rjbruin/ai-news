"""In-app overview summary: renders matching items as an HTML fragment."""
from __future__ import annotations

from datetime import datetime

from flask import render_template

from .base import Artifact, NewsSummary


class AppPageSummary(NewsSummary):
    type_key = "app_page"
    label = "In-app overview page"
    description = "An overview page of news items in scope, grouped and tagged."
    param_schema = {
        "group_by_tag": {
            "type": "checkbox",
            "label": "Group items by tag",
            "default": True,
        },
        "release_time": {
            "type": "time",
            "label": "Daily release time (HH:MM, UTC)",
            "default": "08:00",
            "scope": "day",   # only shown for daily summaries
        },
        "release_day": {
            "type": "select",
            "label": "Weekly release day",
            "default": "0",
            "scope": "week",  # only shown for weekly summaries
            "options": [
                ("0", "Monday"), ("1", "Tuesday"), ("2", "Wednesday"),
                ("3", "Thursday"), ("4", "Friday"), ("5", "Saturday"), ("6", "Sunday"),
            ],
        },
        "send_email": {
            "type": "checkbox",
            "label": "Send as email newsletter when edition is cut",
            "default": False,
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
        return Artifact(kind="html", title="News overview", html=html)
