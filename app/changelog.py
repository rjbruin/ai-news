"""Hand-maintained per-release changelog, shown once to users after an update.

Add an entry here whenever a release has something worth telling users about.
`summary` is shown to everyone; `admin_extra` is appended only for admins.
Skip a release entirely (no entry) if it has nothing user-facing to report.
"""
from __future__ import annotations

ENTRIES = [
    {
        "version": "0.24.0",
        "date": "2026-07-14",
        "summary": [
            "Feedback revisions can now start a clean rewrite instead of editing the "
            "existing draft — tick \"Start this revision from scratch\" on the feedback "
            "form.",
            "You'll see a short summary like this one after future updates.",
        ],
        "admin_extra": [
            "New User.last_seen_version column drives the changelog-modal gating.",
        ],
    },
    {
        "version": "0.24.1",
        "date": "2026-07-15",
        "summary": [
            "The model used to write your editions is now its own setting on the "
            "Settings page, independent of which API key pays for it.",
        ],
        "admin_extra": [
            "creds.resolve() now takes an optional summary and reads "
            "summary.params[\"model\"]; ApiKey.model is unchanged and still governs "
            "per-source ingestion/tagging model choice only.",
        ],
    },
    {
        "version": "0.25.0",
        "date": "2026-07-15",
        "summary": [
            "If an edition fails to generate (e.g. your OpenRouter account runs out "
            "of credits), it now shows up as a failed edition with the error message "
            "and a Retry button, instead of silently disappearing.",
        ],
        "admin_extra": [
            "SummaryRun gained status=\"failed\"/error_message/retry_context; "
            "_purge_empty_editions() was also fixed to stop deleting these on "
            "restart (it previously treated any content-less row as stale).",
        ],
    },
    {
        "version": "0.25.1",
        "date": "2026-07-15",
        "summary": [
            "Fixed a bug where retrying a failed edition (or the next scheduled "
            "edition after one failed) could come up with no news items in scope.",
        ],
        "admin_extra": [
            "resolve_range()/cut_due_editions() now exclude status=\"failed\" runs "
            "when finding \"the latest run\" — a failed run's range_end was being "
            "used as the next window's start, and as an \"already cut\" signal.",
        ],
    },
]


def _version_tuple(version: str) -> tuple[int, ...]:
    parts = []
    for p in version.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def entries_since(version: str) -> list[dict]:
    """Entries strictly newer than `version`, oldest first, numerically compared."""
    baseline = _version_tuple(version)
    return [
        e for e in ENTRIES
        if _version_tuple(e["version"]) > baseline
    ]


def latest_version() -> str | None:
    return ENTRIES[-1]["version"] if ENTRIES else None
