"""Channel renderers for the agentic summary document IR.

The agent produces one block document (see app.agent.blocks); the system
renders it to whichever output channel a summary targets. HTML (the in-app
page) is implemented here; email / podcast renderers will reuse the same IR.
"""
from __future__ import annotations

from flask import render_template

from .blocks import validate_document


def render_html(document: list[dict], *, validate: bool = True) -> str:
    """Render a block document to an HTML fragment for the in-app page channel."""
    blocks = validate_document(document) if validate else (document or [])
    return render_template("summaries/agentic_page.html", blocks=blocks)
