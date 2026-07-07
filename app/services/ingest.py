"""Ingestion service: poll sources, extract items, persist, and tag.

Kept free of Flask request context so the scheduler can call it directly
(inside an app context).
"""
from __future__ import annotations

import logging
from email.utils import parseaddr

from ..extensions import db
from ..llm import openrouter
from ..llm import prompt_safety
from ..models import (
    Alert,
    ApiKey,
    ApiKeyUsage,
    IgnoredSender,
    IngestRun,
    NewsItem,
    Source,
    Tag,
    User,
    utcnow,
)
from ..sources import registry as source_registry
from ..tagging import engine as tagging_engine

logger = logging.getLogger(__name__)

# Sources of this type poll a single mailbox but represent many distinct
# newsletters — see _ingest_newsletter_mailbox.
_SPLITTING_TYPES = {"imap_newsletter"}


def _empty_stats() -> dict:
    return {"fetched": 0, "new_items": 0, "tagged": 0, "skipped": 0, "errors": 0, "error_log": []}


def _merge_stats(into: dict, other: dict) -> None:
    for key in ("fetched", "new_items", "tagged", "skipped", "errors"):
        into[key] += other[key]
    into["error_log"].extend(other["error_log"])


def _format_poll_status(new_items: int, fetched: int, skipped: int, errors: int) -> str:
    """Human-readable poll summary, e.g. "3 new items (5 checked, 2 already
    seen)". Kept free of jargon like "docs" — "checked" covers whatever the
    source fetched (emails, feed entries, ...). Callers render this as a
    success/error-colored badge based on whether "error" appears in it."""
    detail = [f"{fetched} checked"]
    if skipped:
        detail.append(f"{skipped} already seen")
    if errors:
        detail.append(f"{errors} error{'s' if errors != 1 else ''}")
    return f"{new_items} new item{'s' if new_items != 1 else ''} ({', '.join(detail)})"


def _format_newsletter_status(new_items: int, fetched: int, errors: int) -> str:
    """Deliberately terser than _format_poll_status — a per-newsletter status
    is read much more often (one row per subscription) so it stays to just
    the two numbers that matter, plus an error count when something broke."""
    text = f"{new_items} new item{'s' if new_items != 1 else ''}, {fetched} checked"
    if errors:
        text += f", {errors} error{'s' if errors != 1 else ''}"
    return text


def ingest_source(source: Source) -> dict:
    """Fetch + extract + persist + tag for a single source. Returns a stat dict."""
    if source.type_key in _SPLITTING_TYPES and source.parent_source_id is None:
        return _ingest_newsletter_mailbox(source)
    return _ingest_plain_source(source)


def default_newsletter_mailbox() -> Source | None:
    """The mailbox that self-service newsletter subscription requests attach
    to. Approved users can't configure their own mailbox — there's one shared
    inbox, set up by an admin — so we just pick the oldest one."""
    return (
        Source.query.filter_by(type_key="imap_newsletter", parent_source_id=None)
        .order_by(Source.created_at)
        .first()
    )


def _resolve_credentials(source: Source):
    """Returns (api_key_row, secret, model, error_message). error_message is
    None on success; on failure the other three are None."""
    api_key_row = source.api_key
    if api_key_row is None or not api_key_row.active:
        return None, None, None, "error: no active API key assigned to this source"
    secret = api_key_row.get_key()
    if not secret:
        return None, None, None, "error: assigned API key has no usable credential"
    return api_key_row, secret, api_key_row.resolved_model(), None


def _ingest_plain_source(source: Source) -> dict:
    api_key_row, secret, model, err = _resolve_credentials(source)
    if err:
        source.last_status = err
        db.session.commit()
        return _empty_stats()

    usage_totals = {"tokens": 0, "cost": 0.0}

    def _usage_hook(usage: dict) -> None:
        usage_totals["tokens"] += int(usage.get("total_tokens") or 0)
        usage_totals["cost"] += float(usage.get("cost") or 0.0)

    plugin = source_registry.create(
        source.type_key, source.config or {},
        api_key=secret, model=model, usage_hook=_usage_hook,
    )
    if plugin is None:
        source.last_status = f"error: unknown source type '{source.type_key}'"
        db.session.commit()
        return _empty_stats()

    try:
        docs = plugin.fetch(source.last_polled_at)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Fetch failed for source %s", source.id)
        msg = f"fetch error: {exc}"
        source.last_status = msg
        source.last_polled_at = utcnow()
        db.session.commit()
        stats = _empty_stats()
        stats["errors"] = 1
        stats["error_log"] = [msg]
        return stats

    all_tags = Tag.query.all()
    stats = _ingest_docs_for_source(source, plugin, docs, all_tags)
    stats["fetched"] = len(docs)

    source.last_polled_at = utcnow()
    source.last_status = _format_poll_status(
        stats["new_items"], stats["fetched"], stats["skipped"], stats["errors"],
    )
    if usage_totals["tokens"] or usage_totals["cost"]:
        db.session.add(ApiKeyUsage(
            api_key_id=api_key_row.id,
            source_id=source.id,
            kind="ingest",
            tokens=usage_totals["tokens"],
            cost=usage_totals["cost"],
        ))
    db.session.commit()
    return stats


def _ingest_docs_for_source(source: Source, plugin, docs: list, all_tags: list[Tag]) -> dict:
    """Dedup, extract, persist and tag ``docs`` against ``source``. Shared by
    plain sources and by each newsletter subscription split out of a mailbox."""
    stats = _empty_stats()
    new_items: list[NewsItem] = []

    for doc in docs:
        # Server-side dedup: skip documents whose external_id we have seen before.
        if doc.external_id and IngestRun.query.filter_by(
            source_id=source.id, external_id=doc.external_id
        ).first():
            stats["skipped"] += 1
            continue

        run = IngestRun(
            source_id=source.id,
            external_id=doc.external_id or None,
            subject=doc.subject,
            sender=(doc.meta or {}).get("from"),
            raw_body=doc.text,
        )
        db.session.add(run)
        db.session.flush()  # populate run.id before linking items

        try:
            extracted = plugin.extract(doc)
        except Exception as exc:  # noqa: BLE001
            msg = f"extraction error for '{doc.subject or doc.external_id}': {exc}"
            logger.exception("Extraction failed for doc %s", doc.external_id)
            stats["errors"] += 1
            stats["error_log"].append(msg)
            continue

        if not extracted:
            stats["error_log"].append(
                f"no items extracted from '{doc.subject or doc.external_id}' (LLM returned empty or failed)"
            )

        for ex in extracted:
            dedup = NewsItem.make_hash(ex.title, ex.url)
            if NewsItem.query.filter_by(dedup_hash=dedup).first():
                stats["skipped"] += 1
                continue
            item = NewsItem(
                source_id=source.id,
                ingest_run_id=run.id,
                dedup_hash=dedup,
                title=ex.title[:500],
                url=ex.url,
                summary_text=ex.summary,
                one_liner=ex.one_liner,
                item_type=ex.item_type,
                full_text=ex.full_text,
                published_at=ex.published_at,
                status="parsed",
            )
            db.session.add(item)
            new_items.append(item)

    db.session.flush()
    stats["new_items"] = len(new_items)

    for item in new_items:
        try:
            tagging_engine.apply_to_item(
                item, all_tags, api_key=plugin.api_key, model=plugin.model,
                usage_hook=plugin.usage_hook,
            )
            stats["tagged"] += 1
        except Exception as exc:  # noqa: BLE001
            msg = f"tagging error for '{item.title[:60]}': {exc}"
            logger.exception("Tagging failed for item %s", item.id)
            item.status = "error"
            stats["errors"] += 1
            stats["error_log"].append(msg)

    return stats


def _sender_key(sender: str | None) -> tuple[str, str]:
    """Returns (address, display_name) for grouping newsletters by sender."""
    display_name, addr = parseaddr(sender or "")
    addr = (addr or sender or "").strip().lower()
    return addr or "unknown-sender", display_name.strip()


def _domain_of(addr: str) -> str:
    return addr.rsplit("@", 1)[-1] if "@" in addr else ""


def normalize_domain(raw: str | None) -> str:
    """Normalize a user-typed or sender-derived domain for matching: strips
    scheme/path/whitespace/leading '@' and a leading 'www.', lowercases."""
    d = (raw or "").strip().lower()
    if "://" in d:
        d = d.split("://", 1)[1]
    d = d.split("/", 1)[0]
    d = d.lstrip("@")
    if d.startswith("www."):
        d = d[4:]
    return d


def _pending_children_by_domain(mailbox: Source) -> dict[str, Source]:
    """Newsletter subscriptions still waiting on confirmation, keyed by the
    domain the requesting user typed in (normalized) — their exact sender
    address isn't known yet, so they can't be matched by children_by_sender."""
    result: dict[str, Source] = {}
    for c in mailbox.children:
        if c.subscription_status != "waiting_confirmation":
            continue
        domain = normalize_domain((c.config or {}).get("newsletter_domain"))
        if domain:
            result[domain] = c
    return result


def _find_pending_by_domain(pending_by_domain: dict[str, Source], addr: str) -> Source | None:
    """Match a sender address against pending subscriptions by domain,
    tolerating either side being a subdomain of the other (e.g. a user types
    "example.com" but the newsletter actually sends from "news.example.com",
    or vice versa)."""
    domain = normalize_domain(_domain_of(addr))
    if not domain:
        return None
    exact = pending_by_domain.get(domain)
    if exact is not None:
        return exact
    for pending_domain, child in pending_by_domain.items():
        if domain.endswith("." + pending_domain) or pending_domain.endswith("." + domain):
            return child
    return None


def _ignored_addresses(mailbox: Source) -> set[str]:
    """Sender addresses an admin has confirmed aren't newsletters for this
    mailbox — skipped during polling and reindexing."""
    return {
        row.email for row in
        IgnoredSender.query.filter_by(mailbox_source_id=mailbox.id).all()
    }


def _get_or_create_newsletter_child(
    mailbox: Source, children_by_sender: dict, addr: str, display_name: str
) -> tuple[Source, bool]:
    """Find (or create) the subscription Source for one sender, detected while
    polling the mailbox. Auto-created children inherit the mailbox's owner and
    API key so they need no separate configuration, and start out already
    ``subscribed`` — they were detected from mail already flowing through the
    mailbox, so there's no confirmation step to wait for."""
    existing = children_by_sender.get(addr)
    if existing is not None:
        return existing, False

    child = Source(
        type_key=mailbox.type_key,
        name=(display_name or addr)[:120],
        owner_user_id=mailbox.owner_user_id,
        api_key_id=mailbox.api_key_id,
        parent_source_id=mailbox.id,
        config={"newsletter_sender": addr, "newsletter_sender_name": display_name},
        subscription_status="subscribed",
        enabled=True,
    )
    db.session.add(child)
    db.session.flush()
    children_by_sender[addr] = child
    return child, True


def _ingest_newsletter_mailbox(mailbox: Source) -> dict:
    """Poll a mailbox source, then split its emails into one Source per
    detected newsletter (sender), so each newsletter can be reviewed and
    retracted independently. New senders are auto-registered as new,
    already-``subscribed`` Sources; disabled subscriptions are skipped
    without spending any LLM tokens (but their emails are still recorded, so
    re-enabling doesn't lose history).

    Mail matching a user-requested subscription still ``waiting_confirmation``
    (matched by domain, since its exact sender address isn't known yet) is
    routed to _handle_pending_confirmation instead of being auto-ingested —
    see that function for the confirmation-detection flow.
    """
    api_key_row, secret, model, err = _resolve_credentials(mailbox)
    if err:
        mailbox.last_status = err
        db.session.commit()
        return _empty_stats()

    plugin = source_registry.create(mailbox.type_key, mailbox.config or {}, api_key=secret, model=model)
    if plugin is None:
        mailbox.last_status = f"error: unknown source type '{mailbox.type_key}'"
        db.session.commit()
        return _empty_stats()

    try:
        docs = plugin.fetch(mailbox.last_polled_at)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Fetch failed for mailbox source %s", mailbox.id)
        msg = f"fetch error: {exc}"
        mailbox.last_status = msg
        mailbox.last_polled_at = utcnow()
        db.session.commit()
        stats = _empty_stats()
        stats["errors"] = 1
        stats["error_log"] = [msg]
        return stats

    stats = _empty_stats()
    stats["fetched"] = len(docs)
    new_subscriptions = 0

    children_by_sender = {
        (c.config or {}).get("newsletter_sender"): c for c in mailbox.children
    }
    pending_by_domain = _pending_children_by_domain(mailbox)
    ignored = _ignored_addresses(mailbox)

    docs_by_child: dict[int, list] = {}
    child_by_id: dict[int, Source] = {}
    for doc in docs:
        addr, display_name = _sender_key((doc.meta or {}).get("from"))
        if addr in ignored:
            stats["skipped"] += 1
            continue
        child = children_by_sender.get(addr)

        if child is None:
            pending_match = _find_pending_by_domain(pending_by_domain, addr)
            if pending_match is not None:
                outcome = _handle_pending_confirmation(pending_match, doc)
                if pending_match.subscription_status != "waiting_confirmation":
                    pending_by_domain = {
                        d: c for d, c in pending_by_domain.items() if c.id != pending_match.id
                    }
                if outcome == "consumed":
                    stats["skipped"] += 1
                    continue
                # outcome == "content": treat the subscription as active and
                # also process this email as real newsletter content below.
                child = pending_match
                child.config = {
                    **(child.config or {}), "newsletter_sender": addr,
                    "newsletter_sender_name": display_name,
                }
                children_by_sender[addr] = child

        if child is None:
            child, created = _get_or_create_newsletter_child(mailbox, children_by_sender, addr, display_name)
            if created:
                new_subscriptions += 1

        docs_by_child.setdefault(child.id, []).append(doc)
        child_by_id[child.id] = child

    all_tags = Tag.query.all()
    for child_id, child_docs in docs_by_child.items():
        child = child_by_id[child_id]

        if not child.enabled:
            # Retracted newsletter: record that mail arrived (so it isn't
            # reprocessed if re-enabled later) without spending any LLM tokens.
            skipped_here = 0
            for doc in child_docs:
                if doc.external_id and IngestRun.query.filter_by(
                    source_id=child.id, external_id=doc.external_id
                ).first():
                    continue
                db.session.add(IngestRun(
                    source_id=child.id,
                    external_id=doc.external_id or None,
                    subject=doc.subject,
                    sender=(doc.meta or {}).get("from"),
                    raw_body=doc.text,
                ))
                skipped_here += 1
            stats["skipped"] += skipped_here
            child.last_status = "retracted: newsletter disabled, emails recorded but not processed"
            continue

        child_api_key_row, child_secret, child_model, child_err = _resolve_credentials(child)
        if child_err:
            child.last_status = child_err
            continue

        child_usage = {"tokens": 0, "cost": 0.0}

        def _child_hook(usage: dict, _acc=child_usage) -> None:
            _acc["tokens"] += int(usage.get("total_tokens") or 0)
            _acc["cost"] += float(usage.get("cost") or 0.0)

        child_plugin = source_registry.create(
            mailbox.type_key, mailbox.config or {},
            api_key=child_secret, model=child_model, usage_hook=_child_hook,
        )
        child_stats = _ingest_docs_for_source(child, child_plugin, child_docs, all_tags)
        _merge_stats(stats, child_stats)

        child.last_polled_at = utcnow()
        child.last_status = _format_newsletter_status(
            child_stats["new_items"], len(child_docs), child_stats["errors"],
        )
        if child_usage["tokens"] or child_usage["cost"]:
            db.session.add(ApiKeyUsage(
                api_key_id=child_api_key_row.id,
                source_id=child.id,
                kind="ingest",
                tokens=child_usage["tokens"],
                cost=child_usage["cost"],
            ))

    mailbox.last_polled_at = utcnow()
    summary = f"{stats['new_items']} new item{'s' if stats['new_items'] != 1 else ''} ({len(docs_by_child)} newsletter{'s' if len(docs_by_child) != 1 else ''} checked"
    if new_subscriptions:
        summary += f", {new_subscriptions} new subscription{'s' if new_subscriptions != 1 else ''}"
    if stats["errors"]:
        summary += f", {stats['errors']} error{'s' if stats['errors'] != 1 else ''}"
    summary += ")"
    mailbox.last_status = summary
    db.session.commit()
    return stats


_CONFIRMATION_EMAIL_SYSTEM = (
    "You are looking at one email received after someone tried to subscribe to a "
    "newsletter. Determine whether completing the subscription requires clicking a "
    "link in THIS email (a double opt-in confirmation step), or whether no action is "
    "needed — e.g. it's a welcome message, a regular newsletter issue, or a notice "
    "that the subscription is already active.\n\n"
    'Respond ONLY with valid JSON in this exact format: '
    '{"requires_click": true or false, "confirmation_url": "https://... or empty string"}'
    "\n\n" + prompt_safety.ANTI_INJECTION_NOTE
)

_CONFIRMATION_RESULT_SYSTEM = (
    "You are looking at the text content of a web page reached by following a "
    "newsletter subscription confirmation link. Decide whether the page indicates "
    "the subscription was confirmed/activated successfully.\n\n"
    'Respond ONLY with valid JSON in this exact format: {"confirmed": true or false}'
    "\n\n" + prompt_safety.ANTI_INJECTION_NOTE
)


def _handle_pending_confirmation(pending_child: Source, doc) -> str:
    """Try to resolve one newsletter subscription that's waiting_confirmation
    against a matched email, using the requesting user's own API key.

    Returns "consumed" (this email was a subscription/confirmation email and
    should not also be treated as newsletter content) or "content" (no click
    was needed, the subscription is now considered active, and this email
    should additionally be processed as a normal newsletter item).
    """
    api_key_row, secret, model, err = _resolve_credentials(pending_child)
    if err:
        logger.warning(
            "Cannot evaluate pending newsletter confirmation for source %s: %s",
            pending_child.id, err,
        )
        return "consumed"

    usage_totals = {"tokens": 0, "cost": 0.0}

    def _hook(usage: dict) -> None:
        usage_totals["tokens"] += int(usage.get("total_tokens") or 0)
        usage_totals["cost"] += float(usage.get("cost") or 0.0)

    try:
        classification = openrouter.chat_json(
            [
                {"role": "system", "content": _CONFIRMATION_EMAIL_SYSTEM},
                {"role": "user", "content": prompt_safety.wrap_untrusted(
                    f"Subject: {doc.subject or '(none)'}\n\nBody:\n{doc.text[:6000]}"
                )},
            ],
            schema={
                "type": "object",
                "properties": {
                    "requires_click": {"type": "boolean"},
                    "confirmation_url": {"type": "string"},
                },
                "required": ["requires_click", "confirmation_url"],
                "additionalProperties": False,
            },
            api_key=secret, model=model, usage_hook=_hook,
        ) or {}
    except openrouter.LLMError:
        logger.exception("Confirmation-email classification failed for source %s", pending_child.id)
        _record_confirm_usage(api_key_row, pending_child, usage_totals)
        return "consumed"

    if not classification.get("requires_click"):
        _mark_subscribed(pending_child)
        _record_confirm_usage(api_key_row, pending_child, usage_totals)
        return "content"

    url = (classification.get("confirmation_url") or "").strip()
    if not url:
        _mark_failed(pending_child, "no confirmation link could be found in the email")
        _record_confirm_usage(api_key_row, pending_child, usage_totals)
        return "consumed"

    try:
        page_text = _fetch_confirmation_page(url)
        confirmed = bool((openrouter.chat_json(
            [
                {"role": "system", "content": _CONFIRMATION_RESULT_SYSTEM},
                {"role": "user", "content": prompt_safety.wrap_untrusted(page_text or "(empty page)")},
            ],
            schema={
                "type": "object",
                "properties": {"confirmed": {"type": "boolean"}},
                "required": ["confirmed"],
                "additionalProperties": False,
            },
            api_key=secret, model=model, usage_hook=_hook,
        ) or {}).get("confirmed"))
    except Exception:  # noqa: BLE001
        logger.exception("Confirmation link click failed for source %s", pending_child.id)
        confirmed = False

    _record_confirm_usage(api_key_row, pending_child, usage_totals)
    if confirmed:
        _mark_subscribed(pending_child)
    else:
        _mark_failed(pending_child, "clicking the confirmation link did not indicate success")
    return "consumed"


def _record_confirm_usage(api_key_row, source: Source, usage_totals: dict) -> None:
    if usage_totals["tokens"] or usage_totals["cost"]:
        db.session.add(ApiKeyUsage(
            api_key_id=api_key_row.id, source_id=source.id, kind="confirm",
            tokens=usage_totals["tokens"], cost=usage_totals["cost"],
        ))


_MAX_CONFIRMATION_REDIRECTS = 5


def _is_safe_external_url(url: str) -> bool:
    """SSRF guard: only allow http(s) URLs whose host resolves exclusively to
    public IP addresses.

    ``confirmation_url`` is extracted by an LLM from an untrusted email — a
    malicious sender could try to get the model to "extract" an internal URL
    (e.g. a cloud metadata endpoint, or another service on the local network)
    so our server fetches it on their behalf. This is checked independently
    of the anti-injection prompt wording, since that's a mitigation for the
    model's behavior, not a guarantee.
    """
    import ipaddress
    import socket
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False  # userinfo in the URL is never needed here and often abused
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        ):
            return False
    return True


def _fetch_confirmation_page(url: str) -> str:
    """"Clicks" a confirmation link: a plain GET, which is how virtually all
    double opt-in confirmation links work (no JS/form submission needed).

    Redirects are followed manually (rather than httpx's follow_redirects)
    so every hop is re-validated by the SSRF guard — checking only the first
    URL wouldn't stop a malicious server from redirecting to an internal one.
    """
    import httpx
    from bs4 import BeautifulSoup

    current = url
    headers = {"User-Agent": "Dispatch/1.0 (+https://github.com/rjbruin/ai-news)"}
    for _ in range(_MAX_CONFIRMATION_REDIRECTS + 1):
        if not _is_safe_external_url(current):
            raise ValueError(f"Refusing to fetch an unsafe or internal URL: {current}")
        resp = httpx.get(current, timeout=20.0, follow_redirects=False, headers=headers)
        if resp.is_redirect:
            location = resp.headers.get("location")
            if not location:
                resp.raise_for_status()
                break
            current = str(httpx.URL(current).join(location))
            continue
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        return soup.get_text(separator=" ", strip=True)[:4000]
    raise ValueError("Too many redirects while fetching the confirmation link")


def _mark_subscribed(child: Source) -> None:
    was_pending = child.subscription_status != "subscribed"
    child.subscription_status = "subscribed"
    # Alert.push() does its own defensive rollback+commit, which would wipe
    # this uncommitted change if it ran first — commit it before pushing.
    db.session.commit()
    if was_pending and child.owner_user_id:
        Alert.push(
            child.owner_user_id,
            key=f"newsletter_subscribed_{child.id}",
            message=f'Your newsletter subscription to "{child.name}" is now active.',
            level="success",
        )


def _mark_failed(child: Source, reason: str) -> None:
    child.subscription_status = "failed"
    db.session.commit()
    if child.owner_user_id:
        Alert.push(
            child.owner_user_id,
            key=f"newsletter_failed_{child.id}",
            message=(
                f'We received a confirmation email for "{child.name}" but could not confirm it '
                f'automatically ({reason}). An admin will complete this manually.'
            ),
            level="warning",
        )
    for admin_user in User.query.all():
        if not admin_user.is_admin:
            continue
        Alert.push(
            admin_user.id,
            key=f"newsletter_needs_manual_confirm_{child.id}",
            message=(
                f'Newsletter "{child.name}" (requested by '
                f'{child.owner.username if child.owner else "an unknown user"}) needs manual '
                f'subscription confirmation — see Sources.'
            ),
            level="warning",
        )


_REINDEX_BATCH_SIZE = 40
_REINDEX_EXAMPLE_SUBJECTS = 5


def reindex_newsletter_mailbox(mailbox: Source) -> dict:
    """Admin utility: scan every message in a mailbox (not just mail since the
    last poll) to discover newsletter subscriptions up front, instead of
    waiting for each one to send a new email.

    Only reads headers (sender + subject), never bodies, and never ingests
    items — it just finds-or-creates the per-newsletter Source rows so they
    show up for review. Classification runs on the shared/global API key
    regardless of the mailbox's own assigned key, since this is a one-off
    admin action rather than per-source ingestion.
    """
    if mailbox.type_key not in _SPLITTING_TYPES or mailbox.parent_source_id is not None:
        raise ValueError("Reindexing is only available for a top-level newsletter mailbox source.")

    plugin = source_registry.create(mailbox.type_key, mailbox.config or {})
    scan = getattr(plugin, "scan_senders", None)
    if plugin is None or scan is None:
        raise ValueError(f"Source type '{mailbox.type_key}' does not support reindexing.")

    pairs = scan()
    ignored = _ignored_addresses(mailbox)

    senders: dict[str, dict] = {}
    for sender, subject in pairs:
        addr, display_name = _sender_key(sender)
        if addr in ignored:
            continue
        info = senders.setdefault(addr, {"display_name": display_name, "subjects": []})
        if display_name and not info["display_name"]:
            info["display_name"] = display_name
        if len(info["subjects"]) < _REINDEX_EXAMPLE_SUBJECTS:
            info["subjects"].append((subject or "").strip()[:140])

    global_key = ApiKey.get_or_create_global()
    secret = global_key.get_key()
    if not secret:
        raise ValueError("The global API key has no usable credential (set OPENROUTER_API_KEY).")
    model = global_key.resolved_model()

    usage_totals = {"tokens": 0, "cost": 0.0}

    def _usage_hook(usage: dict) -> None:
        usage_totals["tokens"] += int(usage.get("total_tokens") or 0)
        usage_totals["cost"] += float(usage.get("cost") or 0.0)

    newsletter_addrs = _classify_newsletter_senders(
        senders, api_key=secret, model=model, usage_hook=_usage_hook,
    )

    children_by_sender = {
        (c.config or {}).get("newsletter_sender"): c for c in mailbox.children
    }
    pending_by_domain = _pending_children_by_domain(mailbox)
    new_subscriptions = 0
    for addr in newsletter_addrs:
        info = senders.get(addr)
        if info is None:
            continue

        # A user may already have requested this exact newsletter (by domain)
        # before it was ever detected — link into that pending subscription
        # instead of creating a duplicate Source. Its confirmation status is
        # left alone; the next regular poll (which has the actual email body,
        # unlike this headers-only scan) resolves it properly.
        pending_match = _find_pending_by_domain(pending_by_domain, addr)
        if pending_match is not None and children_by_sender.get(addr) is None:
            pending_match.config = {
                **(pending_match.config or {}), "newsletter_sender": addr,
                "newsletter_sender_name": info["display_name"],
            }
            children_by_sender[addr] = pending_match
            continue

        _, created = _get_or_create_newsletter_child(
            mailbox, children_by_sender, addr, info["display_name"]
        )
        if created:
            new_subscriptions += 1

    if usage_totals["tokens"] or usage_totals["cost"]:
        db.session.add(ApiKeyUsage(
            api_key_id=global_key.id,
            source_id=mailbox.id,
            kind="reindex",
            tokens=usage_totals["tokens"],
            cost=usage_totals["cost"],
        ))
    db.session.commit()

    return {
        "messages_scanned": len(pairs),
        "unique_senders": len(senders),
        "newsletters_detected": len(newsletter_addrs),
        "new_subscriptions": new_subscriptions,
    }


_REINDEX_SYSTEM = (
    'Respond ONLY with valid JSON in this exact format: {"newsletters": ["sender@address", ...]}'
    " Only ever return addresses that were listed in the input — never invent one."
    "\n\n" + prompt_safety.ANTI_INJECTION_NOTE
)


def _classify_newsletter_senders(
    senders: dict[str, dict], *, api_key: str, model: str, usage_hook
) -> set[str]:
    """Ask the LLM which of ``senders`` (addr -> {display_name, subjects}) are
    genuine recurring newsletters, in batches, and return the matching
    addresses. A batch that fails classification is treated as no matches
    rather than aborting the whole reindex."""
    addrs = list(senders.keys())
    detected: set[str] = set()

    for i in range(0, len(addrs), _REINDEX_BATCH_SIZE):
        batch = addrs[i:i + _REINDEX_BATCH_SIZE]
        lines = []
        for addr in batch:
            info = senders[addr]
            subs = "; ".join(s for s in info["subjects"] if s) or "(no subject)"
            lines.append(f"- {addr} ({info['display_name'] or 'no display name'}): {subs}")
        user_content = (
            "This mailbox is dedicated to newsletter subscriptions, but may also contain "
            "one-off personal mail, transactional emails (receipts, security alerts, calendar "
            "invites, sign-in codes) or spam. For each sender below, with a few example subject "
            "lines, decide whether it is a genuine recurring newsletter or publication.\n\n"
            + prompt_safety.wrap_untrusted("\n".join(lines))
        )
        schema = {
            "type": "object",
            "properties": {
                "newsletters": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["newsletters"],
            "additionalProperties": False,
        }
        try:
            result = openrouter.chat_json(
                [
                    {"role": "system", "content": _REINDEX_SYSTEM},
                    {"role": "user", "content": user_content},
                ],
                schema=schema, api_key=api_key, model=model, usage_hook=usage_hook,
            )
        except openrouter.LLMError:
            logger.exception("Newsletter classification failed for a batch of %d senders", len(batch))
            continue
        for addr in (result or {}).get("newsletters", []):
            if addr in senders:
                detected.add(addr)

    return detected


def ingest_all_due(force: bool = False) -> dict:
    """Ingest every enabled source whose poll interval has elapsed.

    Newsletter subscriptions (children of a mailbox source) are never polled
    directly — they have no fetch credentials of their own, and are updated as
    a side effect of polling their parent mailbox.
    """
    from flask import current_app

    default_interval = current_app.config.get("POLL_INTERVAL", 3600)
    totals = {"sources": 0, "new_items": 0, "tagged": 0, "errors": 0}

    for source in Source.query.filter_by(enabled=True, parent_source_id=None):
        interval = source.poll_interval_override or default_interval
        if not force and source.last_polled_at is not None:
            elapsed = (utcnow() - _aware(source.last_polled_at)).total_seconds()
            if elapsed < interval:
                continue
        stats = ingest_source(source)
        totals["sources"] += 1
        totals["new_items"] += stats["new_items"]
        totals["tagged"] += stats["tagged"]
        totals["errors"] += stats["errors"]
    return totals


def _aware(dt):
    """Treat naive datetimes (SQLite) as UTC."""
    from datetime import timezone

    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def retag_all() -> int:
    """Re-run tagging over all items (e.g. after taxonomy changes)."""
    tags = Tag.query.all()
    count = 0
    for item in NewsItem.query.all():
        tagging_engine.apply_to_item(item, tags)
        count += 1
    return count
