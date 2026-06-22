# AI News — Implementation Plan

**Domain:** ainews.rjbruintjes.nl · **Port:** 5090 · **Stack:** Python / Flask / Bootstrap / SQLite (→ Postgres optional) · **LLM:** OpenRouter

---

## 1. Goal & Guiding Principles

A web app that **collects** AI news from pluggable sources, **analyses & tags** it against a shared taxonomy, and **delivers** it through pluggable summary formats (in-app page, printable collection, AI podcast, …).

Design principles:

1. **Modularity first.** Sources and summaries are plugins discovered at startup. Adding a new one = drop in one Python class, no core changes.
2. **Background-heavy.** Polling, parsing, tagging, and summary generation happen in the background so the UI is always fast.
3. **Per-user LLM.** Each user supplies their own OpenRouter API key; the app never pays for inference.
4. **Cheap-first tagging.** Try a classical classifier (Naive Bayes) before spending LLM tokens; fall back to LLM only when needed.

---

## 2. High-Level Architecture

```
                 ┌──────────────────────────────────────────────┐
                 │                  Flask app                     │
                 │  ┌─────────┐  ┌──────────┐  ┌───────────────┐  │
  Browser / PWA ─┼─▶│  Web UI │  │  Auth    │  │  REST/JSON     │ │
                 │  └─────────┘  └──────────┘  └───────────────┘  │
                 └───────┬───────────────┬───────────────┬───────┘
                         │               │               │
                  ┌──────▼─────┐  ┌──────▼──────┐  ┌──────▼──────┐
                  │ Source     │  │ Tagging     │  │ Summary     │
                  │ plugins    │  │ engine      │  │ plugins     │
                  └──────┬─────┘  └──────┬──────┘  └──────┬──────┘
                         │               │               │
                 ┌───────▼───────────────▼───────────────▼───────┐
                 │              Database (SQLAlchemy)              │
                 └────────────────────────────────────────────────┘
                         ▲
                  ┌──────┴───────┐
                  │  Scheduler   │  APScheduler: poll sources, tag, build summaries
                  └──────────────┘
```

**Process model:** A single Flask process plus an in-process **APScheduler** background scheduler (simplest for a single VPS). A `WORKER_ENABLED` flag lets us split the scheduler into its own systemd service later if load grows. (Alternative: Celery + Redis — heavier, deferred unless needed.)

---

## 3. Tech Choices

| Concern | Choice | Notes |
|---|---|---|
| Web framework | Flask + Blueprints | |
| ORM / migrations | SQLAlchemy + Alembic | |
| DB | SQLite default, Postgres-ready | single file, easy backup; swap via `DATABASE_URL` |
| Auth | Flask-Login + itsdangerous | password (Argon2/bcrypt) + signed magic-link tokens |
| Forms/CSRF | Flask-WTF | |
| Scheduler | APScheduler | in-process, persistent jobstore in DB |
| LLM | OpenRouter via `httpx` | structured outputs (JSON schema / tool calling) |
| Classifier | scikit-learn (MultinomialNB) | trained per-tag from keywords + confirmed items |
| Email ingest | IMAP (`imaplib`/`imap-tools`) | dedicated mailbox subscribed to newsletters |
| Frontend | Bootstrap 5 + Jinja2 | business theme, minimal custom CSS |
| PWA | manifest + service worker | installable, offline shell |
| Podcast TTS | OpenRouter-routed or external TTS | **open question — see §12** |
| Tests | pytest + pytest-flask | fixtures, factory app, in-memory SQLite |
| Packaging | GitHub Releases (tarball) | consumed by update script |

---

## 4. Data Model (initial)

- **User** — id, username, email (unique), password_hash (nullable for link-only), is_admin (derived from `ADMIN_EMAILS` env list), email_verified, created_at, last_login. (No per-user LLM key — analysis uses the global key.)
- **Source** — id, type (plugin key), name, owner_user_id (nullable = global), config_json, poll_interval_override, enabled, last_polled_at, last_status.
- **NewsItem** — id, source_id, external_id/hash (dedup), title, url, raw_content, clean_text, published_at, fetched_at, summary_text (LLM extract), status (new/parsed/tagged/error).
- **Tag** — id, name (unique), keywords (list), explanation, scope (global/user), owner_user_id, created_at.
- **NewsItemTag** — news_item_id, tag_id, confidence, method (nb/llm/manual), confirmed (bool).
- **Summary (config)** — id, user_id, name, type (plugin key), scope_mode (`since_last` | `fixed_period`), period (day/week/custom), start/end override, params_json (type-specific), enabled, schedule.
- **SummaryRun** — id, summary_id, generated_at, item range, artifact_ref (file/audio/html), status.
- **AuthToken** — for magic links / email verification: token hash, user_id, purpose, expires_at, used.

---

## 5. Modular Plugin System

### 5.1 Source plugins
Abstract base:

```python
class NewsSource(ABC):
    type_key: str            # "imap_newsletter"
    config_schema: dict      # for the admin/user config form
    def fetch(self, since) -> list[RawItem]: ...
    def parse(self, raw) -> NewsItem | list[NewsItem]: ...   # may call LLM
```

- Discovered via entry-points / package scan in `sources/`.
- **First implementation: `imap_newsletter`** — connects to a configured mailbox, pulls unread newsletter emails, strips HTML, and uses the **NL-extraction step (§6)** to split one email into multiple discrete news items.
- Future sources (RSS, web scraper, X/Twitter, Reddit) just add a class.

### 5.2 Summary plugins
Abstract base:

```python
class NewsSummary(ABC):
    type_key: str            # "app_page"
    param_schema: dict
    def build(self, items, params) -> Artifact: ...   # html / pdf / audio
```

- **`app_page`** — renders an in-app overview (filter by tag, source, period).
- **`printable`** — generates a print-optimised HTML/PDF collection (page format param).
- **`podcast`** — LLM writes a script from items → TTS → audio file (length param).

---

## 6. News Collection from Natural Language

Pipeline per fetched email/document:

1. **Clean** — strip HTML, boilerplate, unsubscribe footers (readability/bleach).
2. **Segment & extract (LLM, OpenRouter, structured output)** — prompt returns a JSON array of items: `{title, summary, url, entities, published_hint}`. One newsletter → N news items.
3. **Dedup** — hash on normalized title+url; skip seen items.
4. **Persist** as NewsItem(status=parsed) → queue for tagging.

LLM calls use the **owning user's** OpenRouter key. For global/shared sources we need a key policy — **open question §12**.

---

## 7. Tagging / Analysis

Taxonomy: each **Tag** = name + keywords[] + explanation, scope global or user (user tags shareable globally by admin/promotion).

**Two-tier tagging:**

1. **Naive Bayes (cheap, default).** Build a text representation of each tag from its keywords (+ confirmed example items over time). Vectorise news item text (TF-IDF) and score against tags. Apply tags above a confidence threshold. Retrainable as users confirm/reject tags.
2. **LLM fallback (structured output).** When NB confidence is low/ambiguous, send item text + candidate tags' (name, keywords, explanation) and ask the model to return which tags apply, with confidence, as strict JSON.

Configurable: confidence thresholds, "NB only" vs "NB+LLM" mode (cost control).

**Tag try-out page:** user enters a candidate tag (name/keywords/explanation) → run it (NB and/or LLM) against existing NewsItems → show which items would be tagged, before saving the tag.

---

## 8. Summaries & Scheduling

Each summary config has:
- **type** (plugin), **scope_mode**: `since_last` (everything since last consumed) or `fixed_period` (day/week/custom).
- **type-specific params** (printable: page format; podcast: target length).
- optional generation **schedule** (e.g., build the daily podcast at 7:00).

**Scheduler (APScheduler) jobs:**
- `poll_sources` — every `POLL_INTERVAL` (default 1h), respecting per-source overrides.
- `process_items` — clean → extract → tag newly fetched items.
- `build_scheduled_summaries` — generate due summary artifacts ahead of time.

Global config (admin): default poll interval, default tagging mode, thresholds.

---

## 9. Auth & User Management

- **Register:** username + email (+ optional password). Email verification via link.
- **Login:** (a) password, (b) **magic link** — enter email, receive signed one-time login link (itsdangerous, short TTL).
- **Admin:** any user whose email == `ADMIN_EMAIL` (server-side secret env). Admin manages global sources, global taxonomy, promotes user tags to global, global config.
- **Secrets:** OpenRouter API keys encrypted at rest (Fernet with `SECRET_KEY`-derived key). Sent emails via SMTP (config in env).

---

## 10. PWA & Styling

- Bootstrap 5 business theme (clean, neutral, card-based).
- `manifest.webmanifest` + service worker (offline app shell, installable on Android/iOS).
- **Icon:** newsletter "📰/✉️"-style image rendered to PNG sets (192/512/maskable) + Apple touch icon.

---

## 11. Ops: Deploy, Update, Tests, Git

- **Config files:** `deploy/ainews.service` (systemd, gunicorn on :5090), optional `deploy/ainews-worker.service`, `deploy/nginx.conf` (reverse proxy, TLS via certbot, websocket-safe).
- **Update script:** `scripts/update.sh <version>` — download GitHub release tarball, unpack, create/refresh venv, `pip install`, run Alembic migrations, restart systemd service, health-check, rollback on failure.
- **Config:** `.env` (SECRET_KEY, ADMIN_EMAIL, DATABASE_URL, SMTP_*, IMAP_*, POLL_INTERVAL, …) + `.env.example`.
- **Testing:** pytest from day one; every new feature ships with tests. CI-ready (GitHub Actions optional).
- **Git:** init repo now, commit per change. On "new version" request: bump version, tag, push to remote, cut a GitHub Release.

---

## 11b. Decisions Locked In (2026-06-22)

- **LLM key:** **One global OpenRouter key** (`OPENROUTER_API_KEY` in env). All analysis/extraction/tagging is global — there are **no per-user LLM keys**. (Supersedes earlier per-user design.)
- **Admins:** **Multiple admins** via `ADMIN_EMAILS` (comma-separated list of emails). Any user whose email is in the list gets admin.
- **TTS:** **ElevenLabs** (pluggable TTS abstraction; podcast summary is v2). `ELEVENLABS_API_KEY` in env.
- **Database:** **SQLite** for v1, code kept Postgres-compatible (`DATABASE_URL`).
- **Audience:** **Private** — me / a few trusted users.
- **Git remote:** `git@github.com:rjbruin/ai-news.git` (public, empty).
- **Mail:** **Gmail** account `ain99853@gmail.com` used for both **SMTP** (magic links/verification) and **IMAP** (newsletter ingestion). Requires a Gmail **App Password** (2FA). All creds in gitignored `.env`. Swappable to a self-hosted server via env.
- **Tagging modes (configurable, global):** `nb_only` | `nb_then_llm` (NB with LLM fallback) | `llm_only`.
- **Stored content (Q7):** Store **only the newsletter-provided summary text + the URL to the source article** — no full article scraping/storage.

## 12. Open Questions (need your input)

1. **Shared-source LLM key:** A mailbox subscribed to newsletters is inherently *shared*, but LLM keys are *per-user*. Whose OpenRouter key pays for ingesting/extracting shared newsletter emails? Options: (a) admin key for global ingestion, per-user key only for personal sources; (b) each user adds their own mailbox; (c) global ingest is keyless until tagging.
2. **Podcast TTS:** OpenRouter is LLM-only (no first-class TTS). For the AI podcast audio, which provider? (e.g., OpenAI TTS, ElevenLabs, local Piper/Coqui). Or defer podcast to v2 and ship app_page + printable first?
3. **Email sending (magic links, verification):** Which SMTP? (your own mail server, a provider like Postmark/SES, or Gmail SMTP?)
4. **Mailbox access:** Will you provision a dedicated IMAP mailbox for newsletters? Which host (so I size the IMAP source config)?
5. **Multi-tenant vs personal:** Is this primarily for you (a few trusted users) or open public registration? Affects rate-limiting, abuse, and isolation depth.
6. **Database:** SQLite fine for v1 (single VPS, easy backups), or do you want Postgres from the start?
7. **News item content & copyright:** Store full article text or just newsletter-provided summaries + links? (Affects storage and any re-publishing concerns.)
8. **GitHub remote:** What is the repo URL/owner for releases and the update script? Public or private?
9. **Scope of v1:** Proposed minimal first version — see §13. Confirm or adjust.

---

## 13. Proposed v1 Scope (for confirmation)

A walking skeleton that proves the architecture end-to-end:

1. Flask app skeleton, config, DB models, Alembic, pytest harness.
2. Auth: register, password login, magic-link login, admin via env.
3. Plugin framework for sources + summaries (with registry/discovery).
4. One source: `imap_newsletter` + LLM NL-extraction.
5. Taxonomy CRUD + Naive Bayes tagging + LLM fallback + tag try-out page.
6. One summary: `app_page` overview. (printable next, podcast deferred per Q2.)
7. Scheduler: poll + process in background.
8. PWA manifest/service worker + newsletter icon + Bootstrap business theme.
9. systemd + nginx + update script + `.env.example`.
10. Tests across auth, plugins, tagging, scheduling.

---

## 14. Proposed Repository Layout

```
ai-news/
├─ app/
│  ├─ __init__.py        # app factory
│  ├─ config.py
│  ├─ models/
│  ├─ auth/              # blueprint
│  ├─ web/               # blueprint (pages)
│  ├─ api/               # blueprint (json)
│  ├─ sources/           # plugin base + imap_newsletter
│  ├─ summaries/         # plugin base + app_page
│  ├─ tagging/           # nb.py, llm.py, engine.py
│  ├─ llm/               # openrouter client
│  ├─ scheduler/         # jobs
│  ├─ templates/
│  └─ static/            # css, js, manifest, icons, service worker
├─ migrations/           # alembic
├─ tests/
├─ deploy/               # systemd, nginx
├─ scripts/              # update.sh, gen_icons.py
├─ .env.example
├─ requirements.txt
├─ VERSION
└─ wsgi.py
```
```
```
