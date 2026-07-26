"""Renders an edition to PDF and persists it as that edition's PDF channel.

Shared between the on-demand download route (app/web/routes.py's
edition_export) and automatic generation right after an edition is cut
(see summarize.py's _maybe_autogenerate_pdf) — both just differ in how they
get a request context to resolve static asset URLs from.
"""
from __future__ import annotations

import mimetypes
import os
import re
from urllib.parse import urlparse

from flask import current_app, render_template

from ..extensions import db
from ..summaries import registry as summary_registry


def generate_and_store_pdf(summary, run, *, font_scale: int | None = None, base_url: str = "/") -> bytes:
    from weasyprint import HTML as WPHtml
    from weasyprint.urls import default_url_fetcher

    plugin = summary_registry.get(summary.type_key)
    is_agentic = bool(plugin and getattr(plugin, "is_agentic", False))

    html_str = render_template(
        "summaries/print.html",
        summary=summary, run=run, is_agentic=is_agentic,
        font_scale=max(50, min(150, font_scale or 80)),
    )

    static_folder = current_app.static_folder
    static_url_path = current_app.static_url_path  # '/static'

    def _url_fetcher(url):
        parsed = urlparse(url)
        if parsed.path.startswith(static_url_path + "/"):
            rel = parsed.path[len(static_url_path) + 1:]
            abs_path = os.path.join(static_folder, rel)
            if os.path.exists(abs_path):
                mime = mimetypes.guess_type(abs_path)[0] or "application/octet-stream"
                with open(abs_path, "rb") as fh:
                    return {"content": fh.read(), "mime_type": mime}
        return default_url_fetcher(url)

    pdf_bytes = WPHtml(
        string=html_str,
        base_url=base_url,
        url_fetcher=_url_fetcher,
    ).write_pdf()

    pdf_dir = os.path.join(current_app.instance_path, "pdfs")
    os.makedirs(pdf_dir, exist_ok=True)
    stored_name = f"edition_{run.id}.pdf"
    with open(os.path.join(pdf_dir, stored_name), "wb") as fh:
        fh.write(pdf_bytes)
    if run.pdf_file != stored_name:
        run.pdf_file = stored_name
        db.session.commit()

    return pdf_bytes


def pdf_download_filename(run) -> str:
    label = run.label or run.generated_at.strftime("%Y-%m-%d")
    safe_label = re.sub(r"[^\w\s.-]", "_", label).strip("_")
    return f"{safe_label}.pdf"
