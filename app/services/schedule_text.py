"""Human-friendly rendering of a Dispatch's release schedule."""
from __future__ import annotations

import calendar


def describe_schedule(period: str, params: dict | None) -> str:
    """'every Monday-Thursday at 08:00 UTC' / 'every Monday and Wednesday at
    07:00 UTC' / 'every day at 08:00 UTC' / 'every Monday at 08:00 UTC' (weekly).

    Mirrors the actual cutoff logic in app.services.summarize.resolve_range:
    weekly cadence hardcodes 08:00 UTC regardless of any release_time param
    (release_time is only read for daily cadence) — a pre-existing quirk this
    formatter reflects rather than fixes.
    """
    params = params or {}

    if period == "week":
        release_day = int(params.get("release_day", 0) or 0) % 7
        return f"every {calendar.day_name[release_day]} at 08:00 UTC"

    days_raw = params.get("release_days") or [0, 1, 2, 3, 4]
    days = sorted(set(int(d) for d in days_raw))
    time_str = params.get("release_time") or "08:00"

    if days == [0, 1, 2, 3, 4, 5, 6]:
        return f"every day at {time_str} UTC"

    runs = []
    for d in days:
        if runs and runs[-1][-1] == d - 1:
            runs[-1].append(d)
        else:
            runs.append([d])

    pieces = [
        f"{calendar.day_name[run[0]]}-{calendar.day_name[run[-1]]}" if len(run) >= 2
        else calendar.day_name[run[0]]
        for run in runs
    ]

    if len(pieces) == 1:
        days_str = pieces[0]
    else:
        days_str = ", ".join(pieces[:-1]) + " and " + pieces[-1]

    return f"every {days_str} at {time_str} UTC"
