# AI News

Collects AI news from pluggable **sources**, analyses & **tags** it against a
shared taxonomy, and delivers it through pluggable **summary** formats
(in-app page now; printable and AI podcast next).

Runs at **ainews.rjbruintjes.nl** on a VPS (Flask + gunicorn behind nginx, port 5090).

See [PLAN.md](PLAN.md) for the full architecture and roadmap.

## Features (v1)

- **Pluggable sources** — drop a `NewsSource` subclass into `app/sources/`. Ships with `imap_newsletter` (subscribe a mailbox to AI newsletters; an LLM splits each email into discrete items).
- **Pluggable summaries** — drop a `NewsSummary` subclass into `app/summaries/`. Ships with `app_page` (in-app overview).
- **Tagging** — global taxonomy (name + keywords + explanation). Three modes (`TAGGING_MODE`): `nb_only`, `nb_then_llm`, `llm_only`. Users add tags; admins promote them to global. A **try-out page** previews matches before saving.
- **Background processing** — APScheduler polls sources hourly (overridable per source) and tags new items in the background.
- **Auth** — register (username + email), password login, and **magic-link** login. Admins are set by `ADMIN_EMAILS` (comma-separated).
- **PWA** — installable on Android/iOS, newsletter app icon, offline shell.
- **LLM** — one global OpenRouter key powers extraction and tagging.

## Local development

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # then fill in secrets
python manage.py init-db      # or: flask db upgrade
python manage.py seed-tags    # optional starter taxonomy
python manage.py run          # http://localhost:5090
```

Run the tests:

```bash
pytest
```

Regenerate app icons after editing `app/static/icons/icon.svg`:

```bash
python scripts/gen_icons.py
```

## Configuration

All config is via environment variables (see `.env.example`). Key ones:

| Var | Purpose |
|---|---|
| `SECRET_KEY` | session signing + token signing |
| `ADMIN_EMAILS` | comma-separated admin emails |
| `OPENROUTER_API_KEY` / `OPENROUTER_MODEL` | global LLM for extraction + tagging |
| `TAGGING_MODE` | `nb_only` \| `nb_then_llm` \| `llm_only` |
| `IMAP_*` | newsletter mailbox (Gmail needs a 16-char **App Password**) |
| `SMTP_*` / `MAIL_FROM` | outgoing mail for magic links |
| `ELEVENLABS_API_KEY` | TTS (for the upcoming podcast summary) |
| `POLL_INTERVAL` | default source poll interval (seconds, default 3600) |

## Deployment (VPS)

Directory layout: `/opt/ainews/{shared,releases,current}` (see `scripts/update.sh`).

1. Put secrets in `/opt/ainews/shared/.env` and create `/opt/ainews/shared/instance/`.
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

Bump `VERSION`, commit, tag `vX.Y.Z`, and push the tag — then the VPS can pull it
via `update.sh`. (Ask and I'll cut the release.)

## Project layout

```
app/
  sources/     # source plugins (base, registry, extract, imap_newsletter)
  summaries/   # summary plugins (base, registry, app_page)
  tagging/     # nb.py, llm.py, engine.py
  llm/         # openrouter client
  scheduler/   # background jobs
  auth/ web/   # blueprints
deploy/        # systemd + nginx
scripts/       # update.sh, gen_icons.py
tests/         # pytest suite
```
