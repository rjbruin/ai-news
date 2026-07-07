"""Cost rollups for the Admin Costs section.

Covers spend on the two *global* credentials the operator pays for
directly: the shared OpenRouter API key (ApiKeyUsage rows against the
global ApiKey) and ElevenLabs TTS (SummaryRun.podcast_cost — there's no
per-user ElevenLabs key, it's always the one global credential). Per-user
OpenRouter keys are excluded: that spend is the user's own, not the
operator's.
"""
from __future__ import annotations

from datetime import timedelta

from ..models import ApiKey, ApiKeyUsage, SummaryRun, utcnow


def _daterange_labels(days: int) -> list[str]:
    today = utcnow().date()
    return [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]


def openrouter_daily_costs(days: int = 30) -> list[dict]:
    """[{date, cost}, ...] for the global OpenRouter key, oldest first."""
    since = utcnow() - timedelta(days=days)
    global_key_ids = [k.id for k in ApiKey.query.filter_by(is_global=True, provider="openrouter")]
    totals: dict[str, float] = {}
    if global_key_ids:
        rows = ApiKeyUsage.query.filter(
            ApiKeyUsage.api_key_id.in_(global_key_ids), ApiKeyUsage.created_at >= since,
        ).all()
        for row in rows:
            day = row.created_at.date().isoformat()
            totals[day] = totals.get(day, 0.0) + (row.cost or 0.0)
    return [{"date": d, "cost": totals.get(d, 0.0)} for d in _daterange_labels(days)]


def elevenlabs_daily_costs(days: int = 30) -> list[dict]:
    """[{date, cost}, ...] for ElevenLabs podcast generation, oldest first."""
    since = utcnow() - timedelta(days=days)
    totals: dict[str, float] = {}
    runs = SummaryRun.query.filter(
        SummaryRun.podcast_cost.isnot(None), SummaryRun.generated_at >= since,
    ).all()
    for run in runs:
        day = run.generated_at.date().isoformat()
        totals[day] = totals.get(day, 0.0) + (run.podcast_cost or 0.0)
    return [{"date": d, "cost": totals.get(d, 0.0)} for d in _daterange_labels(days)]


def openrouter_cost_by_kind(days: int = 30) -> list[dict]:
    """[{kind, cost}, ...] breakdown of global-key OpenRouter spend by
    ApiKeyUsage.kind (ingest/tag/confirm/reindex)."""
    since = utcnow() - timedelta(days=days)
    global_key_ids = [k.id for k in ApiKey.query.filter_by(is_global=True, provider="openrouter")]
    totals: dict[str, float] = {}
    if global_key_ids:
        rows = ApiKeyUsage.query.filter(
            ApiKeyUsage.api_key_id.in_(global_key_ids), ApiKeyUsage.created_at >= since,
        ).all()
        for row in rows:
            totals[row.kind] = totals.get(row.kind, 0.0) + (row.cost or 0.0)
    return [{"kind": k, "cost": v} for k, v in sorted(totals.items(), key=lambda kv: -kv[1])]


def cost_summary(days: int = 30) -> dict:
    openrouter_daily = openrouter_daily_costs(days)
    elevenlabs_daily = elevenlabs_daily_costs(days)
    return {
        "days": days,
        "openrouter_daily": openrouter_daily,
        "elevenlabs_daily": elevenlabs_daily,
        "openrouter_by_kind": openrouter_cost_by_kind(days),
        "openrouter_total": sum(d["cost"] for d in openrouter_daily),
        "elevenlabs_total": sum(d["cost"] for d in elevenlabs_daily),
    }
