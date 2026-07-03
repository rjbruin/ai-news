"""Build the per-user podcast RSS feed from generated audio editions."""
from __future__ import annotations

import os
from datetime import timezone
from email.utils import format_datetime

from flask import current_app, url_for
from markupsafe import escape
from sqlalchemy import or_

from ..models import Summary, SummaryRun

# ElevenLabs output is CBR MP3 at 128 kbps, so duration ≈ bytes * 8 / bitrate.
_BITRATE_BPS = 128_000

_TYPE_LABELS = {
    "discussion": "Discussion",
    "news": "News read-through",
}


def _podcast_dir() -> str:
    return os.path.join(current_app.instance_path, "podcasts")


def _rfc822(dt) -> str:
    """Format a (possibly naive-UTC) datetime as an RFC 822 date for RSS."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return format_datetime(dt.astimezone(timezone.utc))


def _hms(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _duration_str(size_bytes: int) -> str:
    return _hms(size_bytes * 8 / _BITRATE_BPS)


def _description_text(label: str, type_label: str, chapters, edition_url: str) -> str:
    """Plain-text show notes: summary, chapter list with timestamps, edition link."""
    lines = [f"{type_label} podcast for the “{label}” edition."]
    if chapters:
        lines.append("")
        lines.append("Chapters:")
        lines.extend(f"{_hms(ch['start'])} {ch['title']}" for ch in chapters)
    lines.append("")
    lines.append(f"Full edition: {edition_url}")
    return "\n".join(lines)


def _description_html(label: str, type_label: str, chapters, edition_url: str) -> str:
    """HTML show notes (rendered inside CDATA) so line breaks and the link display."""
    parts = [f"<p>{escape(type_label)} podcast for the “{escape(label)}” edition.</p>"]
    if chapters:
        rows = "".join(
            f"{_hms(ch['start'])} {escape(ch['title'])}<br>" for ch in chapters
        )
        parts.append(f"<p><strong>Chapters</strong><br>{rows}</p>")
    parts.append(f'<p><a href="{escape(edition_url)}">View the full edition</a></p>')
    return "".join(parts)


def owns_audio(user, filename: str) -> bool:
    """True if ``filename`` is an audio file produced by one of ``user``'s runs."""
    return (
        db_session_query(user)
        .filter(
            or_(
                SummaryRun.podcast_audio == filename,
                SummaryRun.news_podcast_audio == filename,
            )
        )
        .first()
        is not None
    )


def db_session_query(user):
    return (
        SummaryRun.query.join(Summary, SummaryRun.summary_id == Summary.id)
        .filter(Summary.user_id == user.id)
    )


def build_episodes(user) -> list[dict]:
    """Return newest-first episode dicts for every generated audio file the user has.

    Each run can yield up to two episodes (discussion + news). Episodes whose
    MP3 is missing on disk are skipped so the feed never advertises a dead file.
    """
    runs = (
        db_session_query(user)
        .filter(
            or_(
                SummaryRun.podcast_audio.isnot(None),
                SummaryRun.news_podcast_audio.isnot(None),
            )
        )
        .order_by(SummaryRun.generated_at.desc())
        .all()
    )

    podcast_dir = _podcast_dir()
    episodes = []
    for run in runs:
        label = run.label or run.generated_at.strftime("%Y-%m-%d")
        edition_url = url_for(
            "web.edition_view",
            summary_id=run.summary_id, run_id=run.id, _external=True,
        )
        for ptype, filename, chapters in (
            ("discussion", run.podcast_audio, run.podcast_chapters),
            ("news", run.news_podcast_audio, run.news_podcast_chapters),
        ):
            if not filename:
                continue
            path = os.path.join(podcast_dir, filename)
            if not os.path.isfile(path):
                continue
            size_bytes = os.path.getsize(path)
            type_label = _TYPE_LABELS[ptype]
            chapters = chapters or []
            episodes.append({
                "title": f"{label} — {type_label}",
                "description_text": _description_text(label, type_label, chapters, edition_url),
                "description_html": _description_html(label, type_label, chapters, edition_url),
                "filename": filename,
                "size_bytes": size_bytes,
                "duration": _duration_str(size_bytes),
                "pub_date": _rfc822(run.generated_at),
                "guid": filename,
            })
    return episodes
