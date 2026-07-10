# Payment Phase 4 — spending the prepaid balance

Phases 1–3 (ledger, admin-managed Lemon Squeezy top-up products, checkout +
webhook, Payment page UI) are done — see `app/services/balance.py`,
`app/services/payment.py`, `app/web/payment.py`, and the "Payment products"
section on `/admin/`. Users can top up and see a balance, but **nothing
spends it yet**. This file describes what's left, based on the original
design research (kept here so the next session doesn't have to re-derive it).

## Accepted product decisions (do not relitigate)

- **Overshoot handling**: accept small overshoot, then hard-stop. A single
  balance-funded run (an agent step, a podcast phase, an ingestion poll) may
  push the balance slightly negative before the app reacts, since exact LLM
  cost is only known after the call completes. Debit what's available, then
  disable further balance-funded runs for that user/source until topped up —
  do not attempt to claw back or pre-cap token budgets to bound this tighter.
- **Funding model**: supplement, not replacement. Users keep the option to
  bring their own API key; balance-funding is a second, explicit, opt-in
  path (per source, and separately for editions/podcasts).

## Schema additions still needed

```python
# User
fund_editions_from_balance = db.Column(db.Boolean, default=False, nullable=False, server_default="0")

# Source
funded_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
```

`Source.funded_by_user_id` is additive metadata alongside the existing
`api_key_id` (which must point at `ApiKey.get_or_create_global()` when
balance-funded — the actual OpenRouter credential used doesn't change,
`_resolve_credentials()` in `ingest.py` needs no changes). It just tells the
billing branch after each LLM call whose balance to debit instead of/
alongside the shared `ApiKeyUsage` row.

## `app/agent/creds.py` — 3-tuple signature change

`resolve(user)` currently returns `(api_key, model)`. Change to
`(api_key, model, is_balance_funded)`:

```python
def resolve(user) -> tuple[str, str, bool]:
    if user.fund_editions_from_balance:
        if user.balance_cents <= 0:
            raise InsufficientBalance("Your balance is empty. Top up or switch to your own API key.")
        global_key = ApiKey.get_or_create_global()
        secret = global_key.get_key()
        if not secret:
            raise MissingCredentials("The shared key has no usable credential (admin issue).")
        return secret, global_key.resolved_model(), True
    # ...existing own-key logic, returning (..., ..., False)
```

This preserves the existing safety property (global key is never *silently*
selectable for editions) — it's now only used when the user explicitly
opted in AND can currently afford it.

**Both call sites need to unpack the 3rd value** — verified via grep, these
are the only two:
- `app/services/summarize.py::_build_agentic()`
- `app/services/podcast.py::run_podcast_job()`

## `app/agent/runner.py` — mid-run balance check

Add a `budget_check` parameter to `run_agent()`:

```python
def run_agent(session, *, api_key, model, ..., budget_check=None) -> list[dict]:
```

Before each step's `openrouter.chat()` call, if `budget_check is not None and
not budget_check()`, emit a `{"type": "stop", "reason": "balance_exhausted"}`
event and break the loop — same code path as the existing `max_steps`/
`token_limit` stops (graceful "pause," not a hard failure). After the loop,
`_build_agentic()` debits `session.cost_used` via
`balance.debit(user_id, session.cost_used, kind="spend", usage_kind="agent",
summary_run_id=run.id)` — catch `InsufficientBalance` here too (the run
already happened; log it and flip `user.fund_editions_from_balance = False`
with a flash-equivalent notification so the *next* run doesn't silently
proceed with no funding).

## `app/services/podcast.py::run_podcast_job()`

Two distinct costs:
- **Audio (ElevenLabs)**: cost is fully computable *before* any TTS call,
  since `parse_script_parts(script)` gives the character count. Do a true
  pre-flight `balance.has_sufficient()` check before starting
  `generate_audio_stream()` — if insufficient, emit an error event with the
  estimated cost and return, no partial spend.
- **Script (LLM)**: same shape as agent generation — cost known only after
  the call. Apply the same check-before/debit-after-with-soft-overshoot
  pattern as `_build_agentic()`.

## `app/services/ingest.py` — per-source balance billing

Touch points (all already identified in the original research):
- `_ingest_plain_source()`, `_ingest_newsletter_mailbox()` — after the
  existing `ApiKeyUsage(kind="ingest")` row is created, if
  `source.funded_by_user_id` is set, also call `balance.debit(...,
  usage_kind="ingest", source_id=source.id)`. On `InsufficientBalance`, set
  `source.enabled = False` and a status message prompting a top-up — mirrors
  the existing auto-disable-on-key-revocation pattern in
  `app/web/routes.py::api_key_revoke()`.
- `_record_confirm_usage()` — same pattern, `usage_kind="confirm"`.
- `_reindex_newsletter_mailbox()` — **no change**, this is always
  admin/global-billed, never a user balance.

## UI additions once the above lands

- `app/templates/keys.html` — a "Fund my editions & podcasts from my
  balance" checkbox near the existing edition-key-selection UI, disabled
  with a "top up first" hint at $0 balance.
- `app/templates/sources_new.html` — a "Fund from my balance" option next to
  the API-key `<select>`, only offered to approved users, disabled/hinted at
  $0 balance (same UX pattern as above, for consistency).
- `app/templates/sources.html` — a small badge on sources where
  `funded_by_user_id` is set (privacy-preserving, same spirit as
  `owner_display()` — never expose another user's identity directly).

## Tests to add

- Extend `tests/test_balance.py` or a new `tests/test_balance_funding_ingest.py`
  for the ingest billing branch and auto-disable-on-exhaustion behavior.
- Extend the existing agent test file (check current name — was
  `tests/test_agentic_summary.py`/`tests/test_agent_core.py` as of Phase 1-3)
  for the `budget_check` mid-run stop and post-run debit/overshoot handling.
- Extend `tests/test_podcast_cost.py` for the audio pre-flight block and
  script soft-overshoot handling.

## Deployment note

Production `.env` needs real `LEMONSQUEEZY_API_KEY` /
`LEMONSQUEEZY_STORE_ID` / `LEMONSQUEEZY_WEBHOOK_SECRET` values once the
Lemon Squeezy store is activated — this is a deployment step, not a code
change, and can happen independently of Phase 4 landing (Phases 1-3 are
already live-checkout-capable once those env vars are set and at least one
`LemonsqueezyProduct` is configured in `/admin/`).
