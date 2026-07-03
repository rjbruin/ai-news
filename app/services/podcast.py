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

DEFAULT_NEWS_PODCAST_FORMAT = """\
# News podcast format

You write scripts for **AI Dispatch News**, a complete read-through of the daily AI and \
technology news digest. The same two hosts alternate between reading sections and \
providing brief analysis.

**Host A (Alex)** — Thoughtful analyst; adds context and perspective when analysing.
**Host B (Sam)** — Engaged enthusiast; brings energy and clarity when reading.

## Structure
The edition is divided into sections. Work through every section in order:
1. One host reads all the items in that section — concisely but completely, covering \
every item.
2. The other host then gives brief analysis of the section (2–4 sentences): key themes, \
what stands out, or what it means in the bigger picture.
3. Roles alternate with every section: if Alex reads section 1, Sam reads section 2, \
Alex reads section 3, and so on. The analyst for each section is always the other host.

## Style
- The reading host sounds like a news anchor: clear, natural, and slightly conversational — \
not robotic. Don't read out sub-headings; weave items together naturally.
- Cover every item in every section. Do not skip or combine items beyond natural phrasing.
- The analysis should add perspective — not repeat what was just read.
- Brief transitions between sections are fine ("Moving to…", "Up next…").
- Open with a short joint intro from both hosts (a few lines).
- Close with a brief sign-off.

## Required script format
Every spoken line must be prefixed with HOST A: or HOST B: (all caps), followed by a space.

HOST A: [dialogue]
HOST B: [dialogue]

Do not include stage directions, sound effects, bracketed notes, section headers as spoken \
lines, or any other markup.
"""


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


def _get_news_podcast_format(user) -> str:
    from ..models import AgentMemory
    row = AgentMemory.query.filter_by(
        user_id=user.id, summary_id=None, kind="news_podcast_format"
    ).first()
    return (row.content or DEFAULT_NEWS_PODCAST_FORMAT) if row else DEFAULT_NEWS_PODCAST_FORMAT


def _set_news_podcast_format(user, content: str) -> None:
    from ..extensions import db
    from ..models import AgentMemory
    row = AgentMemory.query.filter_by(
        user_id=user.id, summary_id=None, kind="news_podcast_format"
    ).first()
    if row is None:
        row = AgentMemory(user_id=user.id, summary_id=None, kind="news_podcast_format")
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
                title = block.get("title") or block.get("headline") or ""
                body = block.get("summary") or block.get("body") or block.get("dek") or ""
                if title:
                    parts.append(f"\n### {_strip_html(str(title))}")
                if body:
                    parts.append(_strip_html(str(body)))
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


def generate_script_revision_stream(run, user, api_key: str, model: str, current_script: str, feedback: str):
    """Generator that yields revised LLM text tokens, given the current script and feedback."""
    from ..llm.openrouter import chat_stream

    podcast_format = _get_podcast_format(user)
    edition_text = edition_to_text(run)
    label = run.label or run.generated_at.strftime("%Y-%m-%d")

    system = (
        "You are a professional podcast scriptwriter.\n\n"
        + podcast_format
    )
    original_request = (
        f"Here is today's AI news digest — \"{label}\".\n\n"
        f"Turn it into a podcast script following the format instructions above.\n\n"
        f"---\n{edition_text}\n---"
    )

    yield from chat_stream(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": original_request},
            {"role": "assistant", "content": current_script},
            {"role": "user", "content": f"Please revise the script based on this feedback:\n\n{feedback}"},
        ],
        api_key=api_key,
        model=model,
        temperature=0.85,
        timeout=300.0,
    )


def generate_news_script_stream(run, user, api_key: str, model: str):
    """Generator that yields LLM tokens for a news-reading podcast script."""
    from ..llm.openrouter import chat_stream

    podcast_format = _get_news_podcast_format(user)
    edition_text = edition_to_text(run)
    label = run.label or run.generated_at.strftime("%Y-%m-%d")

    system = (
        "You are a professional podcast scriptwriter.\n\n"
        + podcast_format
    )
    user_msg = (
        f"Here is today's AI news digest — \"{label}\".\n\n"
        f"Turn it into a complete news podcast script following the format instructions above. "
        f"Cover every section and every item.\n\n"
        f"---\n{edition_text}\n---"
    )

    yield from chat_stream(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ],
        api_key=api_key,
        model=model,
        temperature=0.7,
        timeout=300.0,
    )


def generate_news_script_revision_stream(
    run, user, api_key: str, model: str, current_script: str, feedback: str
):
    """Generator that yields revised tokens for the news podcast script."""
    from ..llm.openrouter import chat_stream

    podcast_format = _get_news_podcast_format(user)
    edition_text = edition_to_text(run)
    label = run.label or run.generated_at.strftime("%Y-%m-%d")

    system = (
        "You are a professional podcast scriptwriter.\n\n"
        + podcast_format
    )
    original_request = (
        f"Here is today's AI news digest — \"{label}\".\n\n"
        f"Turn it into a complete news podcast script following the format instructions above. "
        f"Cover every section and every item.\n\n"
        f"---\n{edition_text}\n---"
    )

    yield from chat_stream(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": original_request},
            {"role": "assistant", "content": current_script},
            {"role": "user", "content": f"Please revise the script based on this feedback:\n\n{feedback}"},
        ],
        api_key=api_key,
        model=model,
        temperature=0.7,
        timeout=300.0,
    )


def _strip_id3v2(data: bytes) -> bytes:
    """Strip the ID3v2 tag from the start of an MP3 chunk.

    Each ElevenLabs segment carries its own ID3 header with a TLEN tag covering
    only that segment's duration. Concatenating chunks naively leaves the first
    segment's short TLEN in place, so players report the wrong total duration.
    Stripping before joining lets players derive duration from the CBR frame data.
    """
    if len(data) < 10 or data[:3] != b"ID3":
        return data
    # Bytes 6-9 encode the tag body size as a synchsafe integer.
    size = (
        (data[6] & 0x7F) << 21 |
        (data[7] & 0x7F) << 14 |
        (data[8] & 0x7F) << 7 |
        (data[9] & 0x7F)
    )
    has_footer = bool(data[5] & 0x10)
    return data[10 + size + (10 if has_footer else 0):]


def _strip_xing_frame(data: bytes) -> bytes:
    """Strip the Xing/Info CBR/VBR info frame from the first audio frame if present.

    After stripping the ID3 tag, ElevenLabs segments still begin with an 'Info'
    frame that records the frame count for that segment only. Players use it to
    derive duration: removing it forces a full frame scan instead.
    """
    if len(data) < 4:
        return data

    # Locate the first MP3 sync word (0xFF followed by 0xE0–0xFF).
    for i in range(min(len(data) - 4, 256)):
        if data[i] != 0xFF or (data[i + 1] & 0xE0) != 0xE0:
            continue

        # Check for Xing / Info / VBRI marker within this frame's payload.
        search = data[i: i + 200]
        if b"Xing" not in search and b"Info" not in search and b"VBRI" not in search:
            return data  # first sync word has no info frame — nothing to do

        # Parse frame header to compute exact frame size.
        hdr = int.from_bytes(data[i: i + 4], "big")
        mpeg_v  = (hdr >> 19) & 3   # 3 = MPEG1
        layer   = (hdr >> 17) & 3   # 1 = Layer III
        br_idx  = (hdr >> 12) & 0xF
        sr_idx  = (hdr >> 10) & 3
        padding = (hdr >>  9) & 1

        _BITRATES  = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
        _SR_MPEG1  = [44100, 48000, 32000, 0]

        if mpeg_v == 3 and layer == 1 and 0 < br_idx < 15:
            samplerate = _SR_MPEG1[sr_idx]
            if samplerate:
                frame_size = 144 * _BITRATES[br_idx] * 1000 // samplerate + padding
                return data[:i] + data[i + frame_size:]

        # Fallback: skip to the next sync word.
        for j in range(i + 4, len(data) - 1):
            if data[j] == 0xFF and (data[j + 1] & 0xE0) == 0xE0:
                return data[:i] + data[j:]
        return data

    return data


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


def generate_audio_stream(script: str, user):
    """Generate a podcast MP3, yielding progress as each segment is synthesized.

    Yields ``("progress", done, total)`` after every TTS segment, then finally
    ``("done", filename)`` once the joined MP3 is written to the instance folder.
    Raises the same validation errors as ``generate_audio``.

    A long news read-through can take several minutes; emitting per-segment
    events keeps the SSE connection active so proxy/worker read-timeouts
    (nginx/gunicorn, 120s) never fire on a silent long request.
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
    total = len(segments)
    for i, seg in enumerate(segments, 1):
        voice_id = voice_a if seg["host"] == "A" else voice_b
        chunk = _tts_segment(seg["text"], voice_id, api_key, el_model)
        mp3_chunks.append(_strip_xing_frame(_strip_id3v2(chunk)))
        yield ("progress", i, total)

    audio_bytes = b"".join(mp3_chunks)

    podcast_dir = os.path.join(current_app.instance_path, "podcasts")
    os.makedirs(podcast_dir, exist_ok=True)

    filename = f"podcast_{int(time.time())}.mp3"
    path = os.path.join(podcast_dir, filename)
    with open(path, "wb") as f:
        f.write(audio_bytes)

    yield ("done", filename)


def generate_audio(script: str, user) -> tuple[str, str]:
    """Generate podcast MP3 from a script (non-streaming convenience wrapper).

    Returns (absolute_path, web_filename).
    """
    filename = None
    for event in generate_audio_stream(script, user):
        if event[0] == "done":
            filename = event[1]
    path = os.path.join(current_app.instance_path, "podcasts", filename)
    return path, filename
