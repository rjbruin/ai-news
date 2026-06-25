"""Printed digest summary: renders news items as a PDF using WeasyPrint."""
from __future__ import annotations

import os
import uuid
from datetime import datetime

from flask import current_app, render_template

from .base import Artifact, NewsSummary


class PrintedSummary(NewsSummary):
    type_key = "printed"
    label = "Printed digest (PDF)"
    description = "A print-ready PDF of news items in scope, suitable for download or printing."
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
            "scope": "day",
        },
        "release_day": {
            "type": "select",
            "label": "Weekly release day",
            "default": "0",
            "scope": "week",
            "options": [
                ("0", "Monday"), ("1", "Tuesday"), ("2", "Wednesday"),
                ("3", "Thursday"), ("4", "Friday"), ("5", "Saturday"), ("6", "Sunday"),
            ],
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
        try:
            import weasyprint
        except ImportError as exc:
            raise RuntimeError(
                "weasyprint is not installed — run: pip install weasyprint"
            ) from exc

        group_by_tag = params.get("group_by_tag", True)
        grouped = None
        if group_by_tag:
            grouped = {}
            for item in items:
                links = list(item.tag_links)
                if not links:
                    grouped.setdefault("Untagged", []).append(item)
                else:
                    best = max(links, key=lambda lnk: lnk.confidence or 0)
                    grouped.setdefault(best.tag.name, []).append(item)
            grouped = dict(sorted(grouped.items(), key=lambda kv: kv[0].lower()))

        title = "AI News Digest"
        html = render_template(
            "summaries/printed_content.html",
            title=title,
            items=items,
            grouped=grouped,
            range_start=range_start,
            range_end=range_end,
        )

        artifacts_dir = os.path.join(current_app.static_folder, "artifacts")
        os.makedirs(artifacts_dir, exist_ok=True)
        filename = f"digest_{uuid.uuid4().hex[:12]}.pdf"
        output_path = os.path.join(artifacts_dir, filename)
        weasyprint.HTML(string=html).write_pdf(output_path)

        return Artifact(
            kind="pdf",
            title=title,
            file_path=f"artifacts/{filename}",
            mime="application/pdf",
        )
