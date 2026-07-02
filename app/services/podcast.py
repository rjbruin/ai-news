"""Podcast export: LLM script generation + ElevenLabs TTS."""
from __future__ import annotations

import html
import os
import re
import struct
import time

import httpx
from flask import current_app

DEFAULT_PODCAST_FORMAT = """\
# Podcast format

You write scripts for **AI Dispatch**, a daily podcast about AI and technology news. \
The show is hosted by two friends who discuss the day's most interesting stories.

**Host A (Alex)** — The analyst. Thoughtful, provides context, connects dots, \
references broader trends and research.
**Host B (Sam)** — The enthusiast. Curious and energetic, asks the questions \
listeners are thinking, occasionally plays devil's advocate.

## Style
- Sound like a real conversation between two people who genuinely find this interesting.
- Use natural speech: contractions, short sentences, the occasional filler ("right", \
"exactly", "huh"), and moments where they finish each other's thoughts.
- The hosts should push back on each other sometimes — mild disagreement is good.
- Pick 3–5 stories to discuss in depth. Skip minor items rather than racing through everything.
- Open with a hook that draws the listener in immediately — not "Welcome to the show".
- Close with a brief, warm sign-off that feels like wrapping up a coffee chat.

## Required script format
Every spoken line must be prefixed with HOST A: or HOST B: (all caps), followed by a space.

HOST A: [dialogue]
HOST B: [dialogue]

Do not include stage directions, sound effects, bracketed notes, or any other markup.
Aim for 5–10 minutes of audio (roughly 900–1,500 words of dialogue).
"""

ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
DEFAULT_ELEVENLABS_MODEL = "eleven_multilingual_v2"


def _get_podcast_format(user) -> str:
    from ..models import AgentMemory
    row = AgentMemory.query.filter_by(
        user_id=user.id, summary_id=None, kind="podcast_format"
    ).first()
    return (row.content or DEFAULT_PODCAST_FORMAT) if row else DEFAULT_PODCAST_FORMAT


def _set_podcast_format(user, content: str) -> None:
    from ..extensions import db
    from ..models import AgentMemory
    row = AgentMemory.query.filter_by(
        user_id=user.id, summary_id=None, kind="podcast_format"
    ).first()
    if row is None:
        row = AgentMemory(user_id=user.id, summary_id=None, kind="podcast_format")
        db.session.add(row)
    row.content = content
    db.session.commit()


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _doc_to_text(document: list) -> str:
    parts = []

    def _walk(blocks):
        for block in (blocks or []):
            t = block.get("type", "")
            if t == "section":
                if block.get("title"):
                    parts.append(f"\n## {block['title']}")
                _walk(block.get("blocks", []))
            elif t in ("item", "story"):
                if block.get("title"):
                    parts.append(f"\n### {block['title']}")
                if block.get("summary"):
                    parts.append(_strip_html(str(block["summary"])))
            elif t == "trend":
                if block.get("headline"):
                    parts.append(f"Trend: {block['headline']}")
                if block.get("text"):
                    parts.append(_strip_html(str(block["text"])))
            elif t == "more_news":
                for item in block.get("items", []):
                    parts.append(f"- {item.get('title', '')} {item.get('summary', '')}")
            elif t == "callout":
                if block.get("text"):
                    parts.append(_strip_html(str(block["text"])))
            elif t == "quick_hits":
                parts.append("Quick hits:")
                for item in block.get("items", []):
                    parts.append(f"- {item.get('text', '')}")
            elif t in ("quote", "intro"):
                if block.get("text"):
                    parts.append(_strip_html(str(block["text"])))

    _walk(document)
    return "\n".join(parts)


def edition_to_text(run) -> str:
    if run.document:
        return _doc_to_text(run.document)
    if run.content:
        return _strip_html(run.content)
    return ""


def generate_script_stream(run, user, api_key: str, model: str):
    """Generator that yields LLM text tokens for the podcast script."""
    from ..llm.openrouter import chat_stream

    podcast_format = _get_podcast_format(user)
    edition_text = edition_to_text(run)
    label = run.label or run.generated_at.strftime("%Y-%m-%d")

    system = (
        "You are a professional podcast scriptwriter.\n\n"
        + podcast_format
    )
    user_msg = (
        f"Here is today's AI news digest — \"{label}\".\n\n"
        f"Turn it into a podcast script following the format instructions above.\n\n"
        f"---\n{edition_text}\n---"
    )

    yield from chat_stream(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ],
        api_key=api_key,
        model=model,
        temperature=0.85,
        timeout=300.0,
    )


def parse_segments(script: str) -> list[dict]:
    """Parse a HOST A/HOST B script into a list of {host, text} dicts."""
    segments = []
    current_host = None
    current_lines = []

    for line in script.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.upper().startswith("HOST A:"):
            if current_host and current_lines:
                segments.append({"host": current_host, "text": " ".join(current_lines).strip()})
            current_host = "A"
            current_lines = [line[7:].strip()]
        elif line.upper().startswith("HOST B:"):
            if current_host and current_lines:
                segments.append({"host": current_host, "text": " ".join(current_lines).strip()})
            current_host = "B"
            current_lines = [line[7:].strip()]
        elif current_host:
            current_lines.append(line)

    if current_host and current_lines:
        segments.append({"host": current_host, "text": " ".join(current_lines).strip()})

    return [s for s in segments if s["text"]]


def _tts_segment(text: str, voice_id: str, api_key: str, el_model: str) -> bytes:
    """Call ElevenLabs TTS and return MP3 bytes for one segment."""
    url = ELEVENLABS_TTS_URL.format(voice_id=voice_id)
    resp = httpx.post(
        url,
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
        json={
            "text": text,
            "model_id": el_model,
            "output_format": "mp3_44100_128",
        },
        timeout=120.0,
    )
    resp.raise_for_status()
    return resp.content


def generate_audio(script: str, user) -> tuple[str, str]:
    """Generate podcast MP3 from a script.

    Returns (absolute_path, web_filename) where web_filename can be used in
    url_for('static', filename=f'podcasts/{filename}').
    """
    api_key = user.get_elevenlabs_key()
    if not api_key:
        raise ValueError("No ElevenLabs API key configured. Add it in Settings.")

    voice_a = (user.elevenlabs_voice_host_a or "").strip()
    voice_b = (user.elevenlabs_voice_host_b or "").strip()
    if not voice_a or not voice_b:
        raise ValueError(
            "Both Host A and Host B voice IDs must be configured in Settings."
        )

    el_model = (user.elevenlabs_model or "").strip() or DEFAULT_ELEVENLABS_MODEL
    segments = parse_segments(script)
    if not segments:
        raise ValueError("No HOST A/HOST B lines found in the script.")

    mp3_chunks = []
    for seg in segments:
        voice_id = voice_a if seg["host"] == "A" else voice_b
        chunk = _tts_segment(seg["text"], voice_id, api_key, el_model)
        mp3_chunks.append(chunk)

    audio_bytes = b"".join(mp3_chunks)

    static_dir = os.path.join(current_app.root_path, "static", "podcasts")
    os.makedirs(static_dir, exist_ok=True)

    filename = f"podcast_{int(time.time())}.mp3"
    path = os.path.join(static_dir, filename)
    with open(path, "wb") as f:
        f.write(audio_bytes)

    return path, filename
