# Dispatch

Dispatch collects AI news from pluggable **sources** (newsletters, RSS/Atom
feeds, …), analyses and **tags** it against a shared taxonomy, and delivers it
as **Dispatches** — followable publications, each cutting **editions**
through a pluggable **summary** format: an in-app page built
deterministically, or an **agentic** edition where an LLM with editor tools
writes the page itself. A podcast/TTS summary and printable export exist
alongside it.

Runs at **dispatch.rjbruintjes.nl** on a single VPS (Flask + gunicorn behind
nginx). See [PLAN.md](PLAN.md) for the original design doc and open
questions; this README describes the app as it exists today.

## Features

- **Dispatches** — a Dispatch is a followable publication (one per owner,
  `Summary.type_key="agentic_page"`). Anyone can browse the public directory
  (`/dispatches`) and follow a **published** Dispatch for free — no API key
  needed. A Dispatch owner can run their own by adding sources and topics,
  tuned to what they care about, and choose to publish it for others to
  follow.
- **Pluggable sources** — drop a `NewsSource` subclass into `app/sources/`.
  Ships with `imap_newsletter` (subscribe a mailbox to AI newsletters; each
  distinct newsletter sender is auto-detected and split into its own
  reviewable/retractable source, see below) and `rss_feed` (poll an RSS/Atom
  feed). All sources — and, for `imap_newsletter`, every newsletter detected
  inside a mailbox — are listed on `/sources` so anyone can see what's
  feeding the shared news pool. Anyone with their own Dispatch can add a
  source, funded by their own API key, and retract it (disable/delete) at
  any time — see **API keys & sources** below.
- **Pluggable summaries** — drop a `NewsSummary` subclass into
  `app/summaries/`. Ships with `app_page` (deterministic in-app overview),
  `agentic_page` (LLM-authored edition, see below), and `printed` (print/PDF
  export).
- **Agentic editions** — for `is_agentic` summary types, an LLM (via
  OpenRouter, using the *owner's own* API key) drives a tool-calling loop that
  reads news items, writes a structured "block document" (headings,
  paragraphs, citations, …), and keeps cross-edition memory of what it already
  covered so it doesn't repeat itself. **Review editions** are an optional,
  slower retrospective cadence (week/month/quarter/year) over a Dispatch's
  own past editions.
- **Tagging** — a global taxonomy (name + keywords + explanation), with a
  per-topic graduation strategy (`app/tagging/engine.py`): a topic starts
  LLM-only, graduates to a hybrid classifier once it has enough confirmed
  labels, then to classifier-only once it has even more — trading cost for
  recall as confidence in a topic's classifier grows. Users can propose tags;
  admins promote them to global. A **try-out page** previews which existing
  items a candidate tag would match before it's saved.
- **Background processing** — an in-process APScheduler polls sources on
  their configured interval (default `POLL_INTERVAL`, overridable per
  source), cuts scheduled/agentic editions when they're due, and prunes old
  agent memory — all without blocking the web process.
- **Auth** — register (username + email), password login, and **magic-link**
  login (signed, short-lived tokens). Admin status is derived from
  `ADMIN_EMAILS` (comma-separated), not stored per-user. Adding sources,
  managing topics, and publishing are all gated on having your own Dispatch
  (or being an admin) — no separate manual-approval step.
- **PWA** — installable on Android/iOS, app icon, offline shell via a service
  worker.
- **LLM / API keys** — one OpenRouter key per user, set on the Your Dispatch
  page, implicitly funding both their own edition generation and every
  source they add — no per-source picker. `OPENROUTER_API_KEY` (settable via
  env var, or by an admin from the Admin Settings page) funds every
  ownerless "system" source instead.
- **Usage analytics** — a cookie-free admin page (`/admin/analytics`) shows
  page visits per page, edition read counts per day/edition/Dispatch,
  per-Dispatch subscriber counts, and how many users run their own Dispatch.
- **AI-generated content disclosure** — every edition, the footer, and the
  frontpage show a badge linking to `/ai-disclosure`, explaining what's
  AI-generated and referencing the EU AI Act's Article 50 transparency
  requirements.

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
`NewsItem`s, and hand off to the tagging engine. An `imap_newsletter` mailbox
is a single `Source`, but a mailbox typically carries many distinct
newsletters — so `ingest_source()` special-cases that type: it groups the
fetched emails by sender and, for each sender not seen before, auto-creates a
child `Source` (`Source.parent_source_id` pointing at the mailbox), inheriting
the mailbox's owner and `ApiKey`. From then on each newsletter is reviewable
and retractable independently on `/sources` — disabling one stops its emails
from being extracted/tagged (saving LLM cost) while still recording that mail
arrived, so re-enabling it later doesn't miss anything. Child sources are
never polled directly (`ingest_all_due` only considers `parent_source_id IS
NULL` sources); polling the mailbox re-syncs all of its newsletters at once.

### API keys & self-service sources

Funding is implicit, derived from ownership rather than assigned per source:
each user has **at most one** `ApiKey` per provider (`app/models.py`,
`owner_user_id` + `provider`, enforced by a partial unique index), and every
`Source` they own is funded by that key — no per-source picker. Ownerless
("system") sources are instead funded by the single `is_global=True` row,
whose `get_key()` prefers a DB-stored secret (settable by any admin from the
Admin Settings page) and falls back to the `OPENROUTER_API_KEY` env var if
none has been set yet.

`services/ingest._resolve_credentials(source)` resolves the owner's (or the
global) key up front and passes the decrypted secret + the system default
model down into the source plugin (`NewsSource.api_key`, threaded through to
`sources/extract.py` and `tagging/llm.py`), and records a per-poll
`ApiKeyUsage` row (tokens + USD cost from OpenRouter's `usage` field) tagged
with both the key and the source — so `Source.usage_tokens` / `usage_cost`
and `ApiKey.total_tokens` / `total_cost` are simple aggregate queries over
that ledger. Removing your key (`/keys/remove`) disables every source you
own that used it; a source's owner (`Source.owner_user_id`, checked via
`Source.can_manage()`) or any admin can retract (disable) or delete it from
`/sources`. Self-service creation (`/sources/new`) is gated on having your
own Dispatch (`User.own_dispatch`, or being an admin) — see `dispatch_required`
in `app/web/routes.py`. Non-admins never see the `seed` (debug fixture)
source type in that form.

### Newsletter subscription requests

A Dispatch owner can't configure the shared mailbox directly, so requesting
a newsletter via `/sources/new` instead asks for just the newsletter's name
and its sending domain — funding comes from their own key automatically (see
above). That creates a child `Source` under the mailbox with
`subscription_status="waiting_confirmation"`
(no `newsletter_sender` yet — the exact address isn't known until mail
arrives) and redirects to `/sources` with a modal showing the mailbox address
to subscribe with and a rate-limited (10s) "Check now" button
(`/sources/<id>/poll-confirmation`) that re-polls the mailbox on demand.

Regular mailbox polling (`_ingest_newsletter_mailbox` in
`app/services/ingest.py`) matches incoming mail against pending subscriptions
by sender **domain** (exact-address matching, used for already-`subscribed`
newsletters, can't apply yet) — normalized and subdomain-tolerant via
`normalize_domain()`/`_find_pending_by_domain()`, which are also used by
mailbox **reindexing** (below) so a self-service request isn't orphaned if an
admin reindexes before the first matching email is polled normally. A match
is handed to `_handle_pending_confirmation`, which classifies the email — using the
*requesting user's own* API key, not the mailbox's — into either "needs a
confirmation-link click" or "no action needed" (structured-output LLM call).
A required link is "clicked" with a plain `httpx.get` (a second LLM call
judges whether the resulting page indicates success); most double opt-in
confirmations are simple GET links, so no form/JS automation is attempted.
Outcomes:

- No click needed → `subscribed` immediately, and — since it's genuine
  newsletter content, not a system email — this same message is also run
  through normal extraction/tagging.
- Click succeeds → `subscribed`; the confirmation email itself is not
  ingested as content.
- Click fails, or a click was expected but no link was found → `failed`; an
  `Alert` (`app/models.py`) is pushed to the requesting user *and* every
  admin, since a human has to finish it manually (`/admin` → "Mark
  subscribed" sets `subscribed` and notifies the owner).

`ApiKeyUsage` rows from this flow use `kind="confirm"`, distinct from
ordinary ingestion (`kind="ingest"`) and admin reindexing (`kind="reindex"`,
see below).

### Reindexing an existing mailbox

Per-newsletter splitting only discovers a sender the first time it emails
*after* the mailbox is polled — a mailbox's pre-existing history doesn't
retroactively populate the newsletter list. `admin.source_reindex_newsletters`
(admin-only, "Reindex newsletters" button on a top-level `imap_newsletter`
source) calls `ingest.reindex_newsletter_mailbox()`, which scans every
message's sender + subject (headers only, via `NewsSource.scan_senders()` —
no bodies, no items ingested), classifies which senders are genuine
newsletters via a batched LLM call on the **global** API key (a one-off admin
action, not per-source ingestion), and creates any missing child `Source`
rows as already-`subscribed`.

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
2. Resolves credentials via `app/agent/creds.py`, which reads the user's own
   `User.api_key` — agentic runs deliberately do **not** fall back to the
   global key, so a missing key surfaces as an actionable error ("Add your
   API key on the Your Dispatch page"), not a silent charge to the shared
   account.
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
Podcast generation is funded the same way as editions: a Dispatch owner's own
`User.elevenlabs_key` (set on the Your Dispatch page) if they have one,
falling back to the global `ELEVENLABS_API_KEY` env var. Voice/model config
(`AdminSettings`, editable from the Admin page's "Admin Settings" section)
stays global regardless of which key pays for generation.
`User.has_podcast_access` (`bool(elevenlabs_key) or podcast_enabled flag or
is_admin`) gates the feature per user — a Dispatch owner self-serves by
adding their own key, or an admin can grant access via the `podcast_enabled`
flag from the Admin page's users table; the podcast page hides all
generation controls for users without access (see
`app/templates/summaries/podcast.html`), though an owner can always still
view/listen to a podcast they already generated even if they later lose
access.

### Data model

Key tables (`app/models.py`): `User` (auth, admin derived from
`ADMIN_EMAILS`; `own_dispatch`/`api_key`/`elevenlabs_key` are properties, not
columns — they look up the user's Summary/ApiKey rows on demand),
`ApiKey` (owner + provider, or `is_global`; one non-global row per
owner+provider, encrypted secret with an env-backed fallback for the global
key) / `ApiKeyUsage` (per-poll tokens + cost ledger, keyed by key and
source), `Source` (plugin type + config JSON + poll interval + owner +
`parent_source_id`/`subscription_status` for newsletter subscriptions —
funding is derived from the owner, not stored on the row), `IgnoredSender`
(per-mailbox senders an admin has marked as not newsletters), `IngestRun`
(per-poll audit trail), `NewsItem` (cleaned text, dedup hash, one-liner,
status), `Tag` / `NewsItemTag` (taxonomy + per-item matches with
confidence/method), `Summary` (a Dispatch or a configured summary — type,
scope mode, schedule, params, `is_published`/`published_name`/`description`
for the public directory, `review_period` for review editions) /
`SummaryRun` (one generated edition — content, artifacts, agent cost/log,
`kind` distinguishing a normal edition from a review), `dispatch_subscriptions`
/ `dispatch_email_subscriptions` (many-to-many follow / email-opt-in tables),
`EditionRead` (per-user read state, also the source for the analytics page's
read counts), `PageVisit` (cookie-free per-endpoint-per-day visit counter),
`Alert` (in-app notifications), `AgentMemory` (the HEADLINES dedup store),
and `AdminSettings` (singleton row via `AdminSettings.get()` — currently
holds the global ElevenLabs voice/model config).
Migrations are managed with Alembic under [migrations/](migrations).

### Untrusted content and prompt injection

Newsletter emails and RSS/Atom feed entries are attacker-reachable text —
anyone who can get mail delivered to the ingest mailbox, or control a feed a
source polls, can put arbitrary content in front of the LLM. `app/llm/prompt_safety.py`
provides `wrap_untrusted()` (delimits external content with
`<untrusted_content>` tags) and `ANTI_INJECTION_NOTE` (a system-prompt
instruction to treat that content as data, never as instructions), applied
everywhere untrusted content reaches a `chat_json` call: item extraction
(`app/sources/extract.py`), tagging (`app/tagging/llm.py`), and the
newsletter subscription-confirmation flow (`app/services/ingest.py`).
The confirmation flow also fetches a URL the LLM extracts from an email, so
it's additionally guarded against SSRF: `_is_safe_external_url()` resolves
the hostname and rejects private/loopback/link-local/reserved addresses, and
`_fetch_confirmation_page()` re-validates every redirect hop rather than
trusting `httpx`'s built-in redirect following. See
`tests/test_prompt_injection.py` for example (harmless) injection payloads
exercised through these paths.

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
| `OPENROUTER_API_KEY` / `OPENROUTER_MODEL` | fallback global LLM key (an admin can instead set one from the Admin Settings page) for ownerless-source ingestion + tagging |
| `TAGGING_MODE` | `nb_only` \| `nb_then_llm` \| `llm_only` |
| `NB_CONFIDENCE_THRESHOLD` | NB confidence floor before falling back to LLM |
| `IMAP_*` | newsletter mailbox (Gmail needs a 16-char **App Password**) |
| `SMTP_*` / `MAIL_FROM` | outgoing mail for magic links |
| `ELEVENLABS_API_KEY` | global TTS key for podcast summaries (voice/model config lives in the Admin page, not env vars) |
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
   certbot --nginx -d dispatch.rjbruintjes.nl
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
