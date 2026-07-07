# Dispatch

Dispatch collects AI news from pluggable **sources** (newsletters, RSS/Atom
feeds, …), analyses and **tags** it against a shared taxonomy, and delivers it
through pluggable **summary** formats — an in-app page built deterministically,
or an **agentic** edition where an LLM with editor tools writes the page
itself. A podcast/TTS summary and printable export exist alongside it.

Runs at **ainews.rjbruintjes.nl** on a single VPS (Flask + gunicorn behind
nginx, port 5090). See [PLAN.md](PLAN.md) for the original design doc and
open questions; this README describes the app as it exists today.

## Features

- **Pluggable sources** — drop a `NewsSource` subclass into `app/sources/`.
  Ships with `imap_newsletter` (subscribe a mailbox to AI newsletters; an LLM
  splits each email into discrete items) and `rss_feed` (poll an RSS/Atom
  feed).
- **Pluggable summaries** — drop a `NewsSummary` subclass into
  `app/summaries/`. Ships with `app_page` (deterministic in-app overview),
  `agentic_page` (LLM-authored edition, see below), and `printed` (print/PDF
  export).
- **Agentic editions** — for `is_agentic` summary types, an LLM (via
  OpenRouter, using the *user's own* API key) drives a tool-calling loop that
  reads news items, writes a structured "block document" (headings,
  paragraphs, citations, …), and keeps cross-edition memory of what it already
  covered so it doesn't repeat itself.
- **Tagging** — a global taxonomy (name + keywords + explanation). Three
  configurable modes (`TAGGING_MODE`): `nb_only` (Naive Bayes classifier
  only), `nb_then_llm` (NB first, LLM fallback when confidence is low), or
  `llm_only`. Users can propose tags; admins promote them to global. A
  **try-out page** previews which existing items a candidate tag would match
  before it's saved.
- **Background processing** — an in-process APScheduler polls sources on
  their configured interval (default `POLL_INTERVAL`, overridable per
  source), cuts scheduled/agentic editions when they're due, and prunes old
  agent memory — all without blocking the web process.
- **Auth** — register (username + email), password login, and **magic-link**
  login (signed, short-lived tokens). Admin status is derived from
  `ADMIN_EMAILS` (comma-separated), not stored per-user.
- **PWA** — installable on Android/iOS, app icon, offline shell via a service
  worker.
- **LLM** — a global OpenRouter key powers ingestion (newsletter extraction)
  and tagging; agentic summary generation is billed to each user's own
  OpenRouter key (configured in Settings), so the app itself never pays for
  the more expensive per-edition generation.

## Architecture

### Request/process model

A single Flask application (application factory in
[app/\_\_init\_\_.py](app/__init__.py)) serves the web UI, auth, and a small
JSON API, and — in the same process by default — runs an in-process
**APScheduler** background scheduler (`WORKER_ENABLED=true`). There is no
separate task queue (no Celery/Redis): everything from polling sources to
generating summary editions happens as scheduled jobs inside the Flask
process. `WORKER_ENABLED=false` lets a second, web-only node run behind the
same load balancer without double-polling, and a `deploy/ainews-worker.service`
unit exists for splitting the worker out if load ever requires it.

```
Browser / PWA
     │
     ▼
Flask app  (app/__init__.py — application factory)
 ├─ auth/        Blueprint: register, password + magic-link login
 ├─ web/         Blueprint: pages (dashboard, news, search, settings, admin)
 ├─ api/         Blueprint: JSON endpoints (agent run status/streaming, etc.)
 └─ on startup:
      sources/registry.discover()    → scans app/sources/ for NewsSource subclasses
      summaries/registry.discover()  → scans app/summaries/ for NewsSummary subclasses
      scheduler/jobs.start_scheduler → APScheduler, in-process

            ┌──────────────┐      ┌───────────────┐      ┌──────────────────┐
            │ Source       │      │ Tagging       │      │ Summary          │
            │ plugins      │      │ engine        │      │ plugins          │
            │ (app/sources)│      │ (app/tagging) │      │ (app/summaries)  │
            └──────┬───────┘      └──────┬────────┘      └──────┬───────────┘
                   │                     │                       │
                   ▼                     ▼                       ▼
            services/ingest.py   services/summarize.py explicitly
            orchestrates: fetch → extract → dedup → tag → persist → build editions
                   │
                   ▼
            SQLAlchemy models (app/models.py) ── SQLite (default) / Postgres
                   ▲
            APScheduler jobs (app/scheduler/jobs.py):
              poll_sources (tick ≤ 60s, ingest.ingest_all_due enforces real intervals)
              cut_editions (every 60s, summarize.cut_due_editions)
              agent_maintenance (hourly, prunes old agent memory files)
```

### Sources: fetch → extract → dedup

`app/sources/base.py` defines the plugin contract: a `NewsSource` subclass
implements `fetch(since) -> list[RawDocument]`; a shared, LLM-backed
`extract(doc) -> list[ExtractedItem]` (in `app/sources/extract.py`) turns one
raw document (e.g. one newsletter email) into zero or more discrete items via
a structured-output OpenRouter call — one email routinely contains several
distinct news items. `app/sources/registry.py` discovers all `NewsSource`
subclasses under `app/sources/` at startup so adding a source is "drop in one
class, no core changes." Shipped sources:

- **`imap_newsletter`** ([app/sources/imap_newsletter.py](app/sources/imap_newsletter.py)) — connects to an IMAP mailbox, pulls new mail, strips HTML/boilerplate, and extracts items.
- **`rss_feed`** ([app/sources/rss_feed.py](app/sources/rss_feed.py)) — polls an RSS/Atom feed URL via `feedparser`; entries typically map 1:1 to items and skip the LLM extraction step.
- **`seed`** ([app/sources/seed.py](app/sources/seed.py)) — fixture data used by `DEBUG_SEED` mode and tests.

`app/services/ingest.py` orchestrates a poll: fetch new `RawDocument`s,
extract items, hash-dedup on normalized title+URL (`dedup_hash`), persist as
`NewsItem`s, and hand off to the tagging engine.

### Tagging

`app/tagging/engine.py` implements the two-tier strategy described in
[PLAN.md](PLAN.md) §7: a cheap **Naive Bayes** classifier
(`app/tagging/nb.py`, scikit-learn `MultinomialNB` over TF-IDF, trained from
each tag's keywords plus confirmed items) runs first; below
`NB_CONFIDENCE_THRESHOLD` (or in `llm_only` mode) it falls back to an LLM
structured-output call (`app/tagging/llm.py`) that's given the item text plus
candidate tags' name/keywords/explanation and returns which apply. Global
`TAGGING_MODE` (`nb_only` | `nb_then_llm` | `llm_only`) controls cost vs.
recall. Matches are stored as `NewsItemTag` rows with `confidence` and
`method` (`nb`/`llm`/`manual`).

### Summaries and the agentic pipeline

`app/summaries/base.py` defines the `NewsSummary` plugin contract:
`build(items, params, range_start, range_end) -> Artifact`. Deterministic
types (`app_page`, `printed`) render templates directly from tagged items.
Types with `is_agentic = True` (`agentic_page`) instead go through
`app/services/summarize.py`, which:

1. Builds an `AgentSession` (`app/agent/context.py`) scoping the in-window
   `NewsItem`s and any prior block document (for feedback-driven revisions).
2. Resolves the *user's own* OpenRouter key/model (`app/agent/creds.py`) —
   agentic runs deliberately do **not** fall back to the global key, so a
   missing key surfaces as an actionable error, not a silent charge to the
   shared account.
3. Drives the tool-calling loop in `app/agent/runner.py`: composes a system
   prompt (`app/agent/prompt.py`) from the user's content configuration and
   interests, then repeatedly calls the model with the tool specs from
   `app/agent/tools.py` (things like `get_item`, `list_scope_items`,
   editor tools to append/edit blocks, and `write_headlines` to finish) until
   the model stops calling tools, hits `AGENT_MAX_STEPS`, or an optional
   token cap (`AGENT_MAX_TOKENS`).
4. Persists the resulting block document; `app/agent/render.py` renders
   blocks to the sanitized HTML stored on `SummaryRun.content`.
5. `app/agent/memory.py` maintains a per-user "HEADLINES" file so the agent
   avoids covering the same story across successive editions; entries older
   than `AGENT_HEADLINES_RETENTION_DAYS` are pruned hourly by the scheduler.

`app/services/podcast.py` / `podcast_feed.py` turn a summary into narrated
audio via ElevenLabs TTS and expose it as a per-user podcast RSS feed
(`app/templates/podcast_feed.xml`); `app/services/coverage.py` and
`search.py` back the dashboard's coverage stats and the search page.

### Data model

Key tables (`app/models.py`): `User` (auth, admin derived from
`ADMIN_EMAILS`, encrypted OpenRouter key), `Source` (plugin type + config
JSON + poll interval), `IngestRun` (per-poll audit trail), `NewsItem`
(cleaned text, dedup hash, one-liner, status), `Tag` / `NewsItemTag`
(taxonomy + per-item matches with confidence/method), `Summary` (a
configured summary — type, scope mode, schedule, params) / `SummaryRun` (one
generated edition — content, artifacts, agent cost/log), `Alert` (in-app
notifications), and `AgentMemory` (the HEADLINES dedup store). Migrations are
managed with Alembic under [migrations/](migrations).

### Auth

Flask-Login sessions plus `itsdangerous`-signed tokens for magic links and
email verification (`app/auth/`). Passwords hashed with Argon2. Per-user
secrets (the OpenRouter key) are encrypted at rest with Fernet
(`app/crypto.py`, key from `FERNET_SECRET`, defaulting to `SECRET_KEY`).

## Local development

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # then fill in secrets
python manage.py init-db      # or: flask db upgrade
python manage.py seed-tags    # optional starter taxonomy
python manage.py run          # http://localhost:5090
```

No credentials to hand? Set `DEBUG_SEED=true` (or `FLASK_CONFIG=app.config.DebugConfig`)
to auto-populate the DB with fixture news items and force-cut summary
editions at startup.

Run the tests:

```bash
pytest
```

Regenerate app icons after editing `app/static/icons/icon.svg`:

```bash
python scripts/gen_icons.py
```

Other `manage.py` commands: `poll` (force-poll all due sources once),
`rerender-editions` (re-render stored HTML for agentic editions from their
block documents — useful after a block-renderer change).

## Configuration

All config is via environment variables (see [.env.example](.env.example)).
Key ones:

| Var | Purpose |
|---|---|
| `SECRET_KEY` | session signing + token signing |
| `FERNET_SECRET` | encrypts stored per-user secrets (defaults to `SECRET_KEY`) |
| `DATABASE_URL` | SQLite by default; set a `postgresql+psycopg://...` URL to switch |
| `ADMIN_EMAILS` | comma-separated admin emails |
| `OPENROUTER_API_KEY` / `OPENROUTER_MODEL` | global LLM for ingestion + tagging |
| `TAGGING_MODE` | `nb_only` \| `nb_then_llm` \| `llm_only` |
| `NB_CONFIDENCE_THRESHOLD` | NB confidence floor before falling back to LLM |
| `IMAP_*` | newsletter mailbox (Gmail needs a 16-char **App Password**) |
| `SMTP_*` / `MAIL_FROM` | outgoing mail for magic links |
| `ELEVENLABS_API_KEY` / `ELEVENLABS_VOICE_ID` | TTS for podcast summaries |
| `POLL_INTERVAL` | default source poll interval (seconds, default 3600) |
| `WORKER_ENABLED` | run the in-process scheduler on this node |
| `AGENT_ENABLED` | enable agentic summary generation |
| `AGENT_MAX_STEPS` / `AGENT_MAX_TOKENS` | per-run tool-call step cap / token cap (0 = uncapped) |
| `AGENT_HEADLINES_RETENTION_DAYS` | how long agent dedup memory is kept |
| `DEBUG_SEED` | seed fixture data + force-cut editions at startup (local dev only) |

## Deployment (VPS)

Directory layout: `/opt/ainews/{shared,releases,current}` (see
[scripts/update.sh](scripts/update.sh)).

1. Put secrets in `/opt/ainews/shared/.env` and create
   `/opt/ainews/shared/instance/`.
2. Install configs:
   ```bash
   cp deploy/ainews.service /etc/systemd/system/
   cp deploy/nginx.conf /etc/nginx/sites-available/ainews
   ln -s /etc/nginx/sites-available/ainews /etc/nginx/sites-enabled/
   certbot --nginx -d ainews.rjbruintjes.nl
   systemctl daemon-reload && systemctl enable --now ainews
   ```
3. Deploy / update to a released version:
   ```bash
   sudo ./scripts/update.sh 0.1.0
   ```
   The script downloads the GitHub release, builds a venv, runs migrations,
   flips the `current` symlink, restarts the service, health-checks it, and
   rolls back automatically on failure.

## Releasing

All changes land via feature branch + PR (no direct commits to `main`).
Release flow: merge the feature PR, then a version-bump PR (bump `VERSION`),
tag `vX.Y.Z`, cut a GitHub release, then on the VPS: SSH in and run
`scripts/update.sh <version>` (aliased as `ainews-update`).

## Project layout

```
app/
  sources/     # source plugins (base, registry, extract, imap_newsletter, rss_feed, seed)
  summaries/   # summary plugins (base, registry, app_page, agentic_page, printed)
  agent/       # agentic pipeline: context, prompt, tools, runner, memory, blocks, render
  tagging/     # nb.py, llm.py, engine.py
  llm/         # openrouter client
  services/    # ingest, summarize, tagging orchestration, podcast, search, coverage
  scheduler/   # background jobs (APScheduler)
  auth/ web/ api/  # blueprints
  templates/ static/  # Jinja2 templates, CSS/JS, PWA manifest + service worker
migrations/    # Alembic
deploy/        # systemd + nginx
scripts/       # update.sh, gen_icons.py
tests/         # pytest suite
```
