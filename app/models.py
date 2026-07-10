"""SQLAlchemy models for Dispatch."""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from flask_login import UserMixin

from .extensions import db

_log = logging.getLogger(__name__)

_ph = PasswordHasher()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JSONEncodedDict(db.TypeDecorator):
    """Stores a dict/list as a JSON string column (Postgres-portable)."""

    impl = db.Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return json.dumps(value) if value is not None else None

    def process_result_value(self, value, dialect):
        return json.loads(value) if value else None


# ─────────────────────────────── Users ───────────────────────────────
class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=True)  # nullable = link-only
    email_verified = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    last_login = db.Column(db.DateTime, nullable=True)

    # Which of this user's ApiKey rows (see ApiKey, api_keys.py) pays for the
    # agentic summary pipeline. Editions and sources now share one API key
    # system instead of separate per-feature credentials.
    edition_api_key_id = db.Column(db.Integer, db.ForeignKey("api_keys.id"), nullable=True)

    # Podcast export is opt-in per user (admins always have it — see
    # has_podcast_access); the ElevenLabs credential/voice/model themselves
    # are global admin settings now, not per-user (see AdminSettings).
    podcast_enabled = db.Column(db.Boolean, default=False, nullable=False, server_default="0")
    podcast_auto_generate = db.Column(db.Boolean, default=False, nullable=False, server_default="0")
    pdf_font_scale = db.Column(db.Integer, default=80, nullable=False, server_default="80")

    # Secret token embedded in the personal podcast RSS feed URL, so podcast
    # apps (which can't do session login) can fetch the feed and its MP3s.
    podcast_feed_token = db.Column(db.String(64), nullable=True, unique=True, index=True)

    featured_summary_id = db.Column(
        db.Integer, db.ForeignKey("summaries.id"), nullable=True
    )

    # Gate for self-service source/API-key management. Admins are always
    # implicitly approved (see is_approved); this flag is for everyone else.
    approved = db.Column(db.Boolean, default=False, nullable=False, server_default="0")

    # Whether the first-visit onboarding tutorial has been shown. Flipped to
    # True the moment it's shown (not on explicit dismissal) so it reliably
    # only ever appears once, even if the user closes the tab without
    # clicking anything.
    has_seen_onboarding = db.Column(db.Boolean, default=False, nullable=False, server_default="0")

    # Prepaid balance in whole USD cents, topped up via Lemon Squeezy (see
    # BalanceTransaction for the audit ledger). Not yet spendable anywhere —
    # see TODO_PAYMENT_PHASE4.md for wiring this into ingestion/editions/
    # podcasts.
    balance_cents = db.Column(db.Integer, default=0, nullable=False, server_default="0")

    tags = db.relationship("Tag", back_populates="owner", lazy="dynamic")
    summaries = db.relationship(
        "Summary", back_populates="user", lazy="dynamic",
        foreign_keys="Summary.user_id",
    )
    edition_recipients = db.relationship(
        "EditionRecipient", back_populates="user", lazy="dynamic",
        cascade="all, delete-orphan",
    )
    featured_summary = db.relationship("Summary", foreign_keys=[featured_summary_id])
    edition_api_key = db.relationship("ApiKey", foreign_keys=[edition_api_key_id])
    balance_transactions = db.relationship(
        "BalanceTransaction", back_populates="user", lazy="dynamic",
        foreign_keys="BalanceTransaction.user_id",
    )

    def set_password(self, password: str) -> None:
        self.password_hash = _ph.hash(password)

    def get_or_create_feed_token(self) -> str:
        """Return the podcast-feed token, generating and persisting one if absent."""
        import secrets

        if not self.podcast_feed_token:
            self.podcast_feed_token = secrets.token_urlsafe(32)
            db.session.commit()
        return self.podcast_feed_token

    def reset_feed_token(self) -> str:
        """Rotate the podcast-feed token, invalidating any existing feed URL."""
        import secrets

        self.podcast_feed_token = secrets.token_urlsafe(32)
        db.session.commit()
        return self.podcast_feed_token

    def check_password(self, password: str) -> bool:
        if not self.password_hash:
            return False
        try:
            return _ph.verify(self.password_hash, password)
        except VerifyMismatchError:
            return False

    @property
    def is_admin(self) -> bool:
        from flask import current_app

        return self.email.lower() in current_app.config.get("ADMIN_EMAILS", [])

    @property
    def is_approved(self) -> bool:
        """Whether this user may add their own sources / API keys.

        Admins are always approved; everyone else needs the ``approved`` flag
        set by an admin.
        """
        return bool(self.approved) or self.is_admin

    @property
    def has_podcast_access(self) -> bool:
        """Whether this user may generate/export podcasts. Admins always do;
        everyone else needs the ``podcast_enabled`` flag set by an admin."""
        return bool(self.podcast_enabled) or self.is_admin


class EditionRecipient(db.Model):
    """One email address that should receive edition emails for a user.

    Starts seeded with just the account's own email (auto-confirmed — no
    need to re-verify an address the account itself already owns). Any
    additional address needs to click a confirmation link before it starts
    receiving mail, and gets a notification when removed.
    """

    __tablename__ = "edition_recipients"
    __table_args__ = (db.UniqueConstraint("user_id", "email", name="uq_edition_recipient"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    email = db.Column(db.String(255), nullable=False)
    confirmed_at = db.Column(db.DateTime, nullable=True)
    confirm_token = db.Column(db.String(64), nullable=True, unique=True, index=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    user = db.relationship("User", back_populates="edition_recipients")

    @property
    def is_confirmed(self) -> bool:
        return self.confirmed_at is not None


class UserDisabledSource(db.Model):
    """Marks that a user has turned a (shared) source off for their own
    editions. Absence of a row means the source is on for that user — every
    source is on by default; this table only tracks the exceptions."""

    __tablename__ = "user_disabled_sources"
    __table_args__ = (db.UniqueConstraint("user_id", "source_id", name="uq_user_disabled_source"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    source_id = db.Column(db.Integer, db.ForeignKey("sources.id"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    user = db.relationship("User")
    source = db.relationship("Source")


class AuthToken(db.Model):
    """Single-use signed-token records for magic-link login / verification."""

    __tablename__ = "auth_tokens"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    purpose = db.Column(db.String(32), nullable=False)  # login | verify
    token_hash = db.Column(db.String(128), nullable=False, index=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    used = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    user = db.relationship("User")


# ─────────────────────────────── API keys ───────────────────────────────
class ApiKey(db.Model):
    """A credential (currently always OpenRouter) usable to run a Source's
    ingestion + tagging.

    ``owner_user_id`` is NULL only for the single seeded global key
    (``is_global=True``), whose secret lives in the ``OPENROUTER_API_KEY`` env
    var rather than in this row — it is conceptually owned by every admin
    rather than any one user, so any admin can view/manage/revoke it.
    """

    __tablename__ = "api_keys"

    id = db.Column(db.Integer, primary_key=True)
    owner_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    label = db.Column(db.String(120), nullable=False)
    provider = db.Column(db.String(30), default="openrouter", nullable=False)
    key_enc = db.Column(db.Text, nullable=True)  # NULL for the global key (read from env)
    model = db.Column(db.String(120), nullable=True)  # optional per-key model override
    is_global = db.Column(db.Boolean, default=False, nullable=False)
    revoked_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    owner = db.relationship("User", foreign_keys=[owner_user_id])
    sources = db.relationship("Source", back_populates="api_key", lazy="dynamic")
    usage_entries = db.relationship(
        "ApiKeyUsage", back_populates="api_key", cascade="all, delete-orphan", lazy="dynamic",
    )

    @property
    def active(self) -> bool:
        return self.revoked_at is None

    def set_key(self, plaintext: str | None) -> None:
        from .crypto import encrypt

        self.key_enc = encrypt(plaintext) if plaintext else None

    def get_key(self) -> str | None:
        """Return the usable secret: the env var for the global key, else the
        decrypted per-user secret."""
        if self.is_global:
            from flask import current_app

            return current_app.config.get("OPENROUTER_API_KEY") or None
        from .crypto import decrypt

        return decrypt(self.key_enc) if self.key_enc else None

    def resolved_model(self) -> str | None:
        from flask import current_app

        return self.model or current_app.config.get("OPENROUTER_MODEL")

    def can_manage(self, user: "User") -> bool:
        if self.is_global:
            return user.is_admin
        return self.owner_user_id == user.id or user.is_admin

    @property
    def total_requests(self) -> int:
        return self.usage_entries.count()

    @property
    def total_tokens(self) -> int:
        return int(self.usage_entries.with_entities(db.func.sum(ApiKeyUsage.tokens)).scalar() or 0)

    @property
    def total_cost(self) -> float:
        return float(self.usage_entries.with_entities(db.func.sum(ApiKeyUsage.cost)).scalar() or 0.0)

    @property
    def last_used_at(self):
        return self.usage_entries.with_entities(
            db.func.max(ApiKeyUsage.created_at)
        ).scalar()

    @classmethod
    def manageable_by(cls, user: "User") -> list["ApiKey"]:
        """Keys ``user`` may pick for a source / manage: their own, plus the
        shared global key if they're an admin."""
        keys = cls.query.filter_by(owner_user_id=user.id).order_by(cls.created_at).all()
        if user.is_admin:
            keys = [cls.get_or_create_global()] + keys
        return keys

    @classmethod
    def get_or_create_global(cls) -> "ApiKey":
        """Return the singleton global key row, creating it if absent."""
        key = cls.query.filter_by(is_global=True).first()
        if key is None:
            key = cls(
                label="Global OpenRouter key (shared by admins)",
                provider="openrouter",
                is_global=True,
                owner_user_id=None,
            )
            db.session.add(key)
            db.session.commit()
        return key


class ApiKeyUsage(db.Model):
    """One row per ingestion poll that spent LLM tokens, for cost tracking."""

    __tablename__ = "api_key_usage"

    id = db.Column(db.Integer, primary_key=True)
    api_key_id = db.Column(db.Integer, db.ForeignKey("api_keys.id"), nullable=False, index=True)
    source_id = db.Column(db.Integer, db.ForeignKey("sources.id"), nullable=True, index=True)
    kind = db.Column(db.String(20), default="ingest", nullable=False)  # ingest|tag
    tokens = db.Column(db.Integer, default=0, nullable=False)
    cost = db.Column(db.Float, default=0.0, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    api_key = db.relationship("ApiKey", back_populates="usage_entries")
    source = db.relationship("Source", back_populates="usage_entries")


# ─────────────────────────────── Sources ───────────────────────────────
class IngestRun(db.Model):
    """One record per raw document (e.g. email) fetched from a Source."""

    __tablename__ = "ingest_runs"

    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(db.Integer, db.ForeignKey("sources.id"), nullable=False)
    external_id = db.Column(db.String(500), nullable=True, index=True)
    fetched_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    subject = db.Column(db.String(500), nullable=True)
    sender = db.Column(db.String(255), nullable=True)
    raw_body = db.Column(db.Text, nullable=True)

    source = db.relationship("Source", back_populates="ingest_runs")
    items = db.relationship("NewsItem", back_populates="ingest_run", lazy="dynamic")


class Source(db.Model):
    __tablename__ = "sources"

    id = db.Column(db.Integer, primary_key=True)
    type_key = db.Column(db.String(64), nullable=False)  # plugin key
    name = db.Column(db.String(120), nullable=False)
    owner_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    api_key_id = db.Column(db.Integer, db.ForeignKey("api_keys.id"), nullable=True)
    # Set only for auto-detected newsletter subscriptions (see services.ingest):
    # the mailbox Source they were split out of. NULL for everything else,
    # including the mailbox itself.
    parent_source_id = db.Column(db.Integer, db.ForeignKey("sources.id"), nullable=True, index=True)
    # Set only for newsletter subscriptions (children of a mailbox source).
    # waiting_confirmation | failed | subscribed. NULL for everything else.
    subscription_status = db.Column(db.String(20), nullable=True)
    config = db.Column(JSONEncodedDict, default=dict)
    poll_interval_override = db.Column(db.Integer, nullable=True)  # seconds
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    last_polled_at = db.Column(db.DateTime, nullable=True)
    last_status = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    owner = db.relationship("User", foreign_keys=[owner_user_id])
    api_key = db.relationship("ApiKey", back_populates="sources")
    parent_source = db.relationship("Source", remote_side=[id], backref=db.backref(
        "children", lazy="dynamic", order_by="Source.name",
        cascade="all, delete-orphan",
    ))
    items = db.relationship("NewsItem", back_populates="source", lazy="dynamic")
    ingest_runs = db.relationship(
        "IngestRun", back_populates="source", lazy="dynamic",
        cascade="all, delete-orphan",
    )
    usage_entries = db.relationship(
        "ApiKeyUsage", back_populates="source", lazy="dynamic",
    )

    @property
    def is_newsletter_subscription(self) -> bool:
        return self.parent_source_id is not None

    @property
    def type_label(self) -> str:
        """Friendly plugin label (e.g. "RSS / Atom feed") for display instead
        of the raw type_key."""
        from .sources import registry as source_registry

        cls = source_registry.get(self.type_key)
        return cls.label if cls else self.type_key

    def can_manage(self, user: "User") -> bool:
        """Whether ``user`` may retract/delete/reconfigure this source."""
        if user.is_admin:
            return True
        return self.owner_user_id is not None and self.owner_user_id == user.id

    def owner_display(self, viewer: "User") -> str:
        """Privacy-preserving owner label for the shared /sources page: never
        reveal another user's identity, just that it's someone else's."""
        if self.owner_user_id is None:
            return "global"
        if self.owner_user_id == viewer.id:
            return "you"
        return "other user"

    def payment_label(self, viewer: "User") -> str:
        """Who's actually paying for this source's usage, from ``viewer``'s
        point of view — deliberately vague about anyone else's key, same
        privacy stance as owner_display."""
        if self.api_key is None:
            return "none assigned"
        if self.api_key.is_global:
            return "Included in system"
        if self.api_key.owner_user_id == viewer.id:
            return "your API key"
        return "another user's API key"

    def usage_visible_to(self, viewer: "User") -> bool:
        """Only the key's own owner gets to see its usage/cost — not the
        operator's global spend, not another user's."""
        return bool(
            self.api_key is not None
            and not self.api_key.is_global
            and self.api_key.owner_user_id == viewer.id
        )

    @property
    def usage_tokens(self) -> int:
        return int(self.usage_entries.with_entities(db.func.sum(ApiKeyUsage.tokens)).scalar() or 0)

    @property
    def usage_cost(self) -> float:
        return float(self.usage_entries.with_entities(db.func.sum(ApiKeyUsage.cost)).scalar() or 0.0)

    def usage_cost_since(self, cutoff) -> float:
        return float(
            self.usage_entries.filter(ApiKeyUsage.created_at >= cutoff)
            .with_entities(db.func.sum(ApiKeyUsage.cost)).scalar() or 0.0
        )


class IgnoredSender(db.Model):
    """A sender address an admin has confirmed is NOT a newsletter (e.g. a
    misclassified personal thread), so it's skipped during that mailbox's
    polling and reindexing instead of continually being re-detected."""

    __tablename__ = "ignored_senders"

    id = db.Column(db.Integer, primary_key=True)
    mailbox_source_id = db.Column(db.Integer, db.ForeignKey("sources.id"), nullable=False, index=True)
    email = db.Column(db.String(255), nullable=False)
    display_name = db.Column(db.String(255), nullable=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    mailbox = db.relationship("Source", foreign_keys=[mailbox_source_id])
    created_by = db.relationship("User", foreign_keys=[created_by_user_id])

    __table_args__ = (
        db.UniqueConstraint("mailbox_source_id", "email", name="uq_ignored_sender"),
    )


class NewsItem(db.Model):
    __tablename__ = "news_items"

    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(db.Integer, db.ForeignKey("sources.id"), nullable=True)
    dedup_hash = db.Column(db.String(64), unique=True, nullable=False, index=True)
    title = db.Column(db.String(500), nullable=False)
    url = db.Column(db.String(2000), nullable=True)
    summary_text = db.Column(db.Text, nullable=True)
    one_liner = db.Column(db.Text, nullable=True)
    full_text = db.Column(db.Text, nullable=True)  # stored for URL-less offline items
    item_type = db.Column(db.String(30), nullable=True)  # paper|announcement|blog|news|tool|opinion|other
    published_at = db.Column(db.DateTime, nullable=True)
    fetched_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    status = db.Column(db.String(20), default="parsed", nullable=False)  # parsed|tagged|error

    ingest_run_id = db.Column(
        db.Integer, db.ForeignKey("ingest_runs.id"), nullable=True, index=True
    )

    source = db.relationship("Source", back_populates="items")
    ingest_run = db.relationship("IngestRun", back_populates="items")
    tag_links = db.relationship(
        "NewsItemTag", back_populates="item", lazy="dynamic",
        cascade="all, delete-orphan",
    )

    @staticmethod
    def make_hash(title: str, url: str | None) -> str:
        norm = (title or "").strip().lower() + "|" + (url or "").strip().lower()
        return hashlib.sha256(norm.encode("utf-8")).hexdigest()

    @property
    def newsletter_domain(self) -> str | None:
        """For newsletter-sourced items, the sender's email domain.

        e.g. an item extracted from a "TLDR" email sent by news@tldrnewsletter.com
        returns 'tldrnewsletter.com'. Returns None when there is no sender on the
        originating ingest run (non-newsletter sources, or legacy items).
        """
        from email.utils import parseaddr

        run = self.ingest_run
        if run is None or not run.sender:
            return None
        addr = parseaddr(run.sender)[1] or run.sender
        if "@" not in addr:
            return None
        domain = addr.rsplit("@", 1)[1].strip().lower()
        return domain or None


# ─────────────────────────────── Tags ───────────────────────────────
class Tag(db.Model):
    """A "Topic" in the UI — kept as `Tag` internally for schema continuity.

    ``scope='global'`` topics are admin-managed and apply to everyone;
    ``scope='user'`` topics are private to ``owner_user_id`` but still get
    full LLM/classifier treatment (see app/tagging/engine.py), just scoped
    to that owner via NewsItemTag.user_id.
    """

    __tablename__ = "tags"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    keywords = db.Column(JSONEncodedDict, default=list)  # list[str]
    explanation = db.Column(db.Text, nullable=True)
    scope = db.Column(db.String(10), default="user", nullable=False)  # global|user
    owner_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    # Admin override of the automatic per-topic graduation state (see
    # app/tagging/engine.py::classifier_state). NULL means "automatic" —
    # the state is computed from label-count thresholds as usual. Any of
    # CLASSIFIER_MODES pins the topic to that state regardless of label count.
    classifier_mode = db.Column(db.String(20), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    # Soft delete: an archived topic stops being offered for new
    # classification/selection, but its historical NewsItemTag rows and
    # stats remain intact — mirrors ApiKey.revoked_at's revoke/reactivate
    # shape rather than a hard, data-losing delete.
    archived_at = db.Column(db.DateTime, nullable=True)

    owner = db.relationship("User", back_populates="tags")
    item_links = db.relationship(
        "NewsItemTag", back_populates="tag", lazy="dynamic",
        cascade="all, delete-orphan",
    )

    @property
    def keyword_list(self) -> list[str]:
        return self.keywords or []

    @property
    def is_active(self) -> bool:
        return self.archived_at is None


CLASSIFIER_MODES = ("llm_only", "hybrid", "classifier_only")


class NewsItemTag(db.Model):
    __tablename__ = "news_item_tags"

    id = db.Column(db.Integer, primary_key=True)
    news_item_id = db.Column(
        db.Integer, db.ForeignKey("news_items.id"), nullable=False
    )
    tag_id = db.Column(db.Integer, db.ForeignKey("tags.id"), nullable=False)
    # NULL = this application is global (visible to everyone); set = this
    # application is scoped to a private topic and only ever surfaced to
    # that owner (see the News-page filter and the picker's available_topics
    # query, both of which enforce this on the read side too).
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    confidence = db.Column(db.Float, default=0.0)
    method = db.Column(db.String(10), default="nb")  # nb|llm|manual
    confirmed = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    item = db.relationship("NewsItem", back_populates="tag_links")
    tag = db.relationship("Tag", back_populates="item_links")
    user = db.relationship("User")

    __table_args__ = (
        # Backstop for the non-NULL (private-topic) case — SQLite (and
        # standard SQL) treats every NULL as distinct, so this alone does
        # NOT stop duplicate (item, tag, user_id=NULL) rows; see the partial
        # index below for the actual global-row guarantee.
        db.UniqueConstraint("news_item_id", "tag_id", "user_id", name="uq_item_tag_user"),
        db.Index(
            "uq_item_tag_global", "news_item_id", "tag_id",
            unique=True, sqlite_where=db.text("user_id IS NULL"),
        ),
    )


# ─────────────────────────────── Summaries ───────────────────────────────
class Summary(db.Model):
    __tablename__ = "summaries"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    type_key = db.Column(db.String(64), nullable=False)  # summary plugin key
    scope_mode = db.Column(db.String(20), default="fixed_period")  # since_last|fixed_period
    period = db.Column(db.String(20), default="day")  # day|week (for fixed_period)
    params = db.Column(JSONEncodedDict, default=dict)  # type-specific params
    last_consumed_at = db.Column(db.DateTime, nullable=True)
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    user = db.relationship(
        "User", back_populates="summaries", foreign_keys=[user_id]
    )
    runs = db.relationship(
        "SummaryRun", back_populates="summary", lazy="dynamic",
        cascade="all, delete-orphan",
    )


class SummaryRun(db.Model):
    __tablename__ = "summary_runs"

    id = db.Column(db.Integer, primary_key=True)
    summary_id = db.Column(db.Integer, db.ForeignKey("summaries.id"), nullable=False)
    generated_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    range_start = db.Column(db.DateTime, nullable=True)
    range_end = db.Column(db.DateTime, nullable=True)
    item_count = db.Column(db.Integer, default=0)
    label = db.Column(db.String(120), nullable=True)   # e.g. "Tuesday June 22"
    content = db.Column(db.Text, nullable=True)         # rendered HTML artifact
    artifact_ref = db.Column(db.String(500), nullable=True)
    status = db.Column(db.String(20), default="ok")

    # Agentic pipeline: structured block document (IR) + revision chain.
    document = db.Column(JSONEncodedDict, nullable=True)
    revision = db.Column(db.Integer, default=1, nullable=False)
    parent_run_id = db.Column(
        db.Integer, db.ForeignKey("summary_runs.id"), nullable=True, index=True
    )

    # Agentic pipeline: recorded run log (list of event dicts) + total USD cost.
    agent_log = db.Column(JSONEncodedDict, nullable=True)
    agent_cost = db.Column(db.Float, nullable=True)

    podcast_script = db.Column(db.Text, nullable=True)
    news_podcast_script = db.Column(db.Text, nullable=True)
    podcast_audio = db.Column(db.Text, nullable=True)
    news_podcast_audio = db.Column(db.Text, nullable=True)
    podcast_chapters = db.Column(JSONEncodedDict, nullable=True)
    news_podcast_chapters = db.Column(JSONEncodedDict, nullable=True)
    podcast_cost = db.Column(db.Float, nullable=True)  # USD, ElevenLabs TTS characters billed

    # Persisted PDF export (filename under instance/pdfs), so PDF counts as a
    # "created" channel for this edition once it has been generated.
    pdf_file = db.Column(db.Text, nullable=True)

    read_at = db.Column(db.DateTime, nullable=True)
    share_token = db.Column(db.String(64), nullable=True, unique=True, index=True)

    summary = db.relationship("Summary", back_populates="runs")
    revisions = db.relationship(
        "SummaryRun",
        backref=db.backref("parent", remote_side=[id]),
        lazy="dynamic",
    )


class Alert(db.Model):
    """User-visible alert for background job failures.

    At most one undismissed alert per (user_id, key) at any time.
    After dismissal, the same key can resurface on the next failure.
    """

    __tablename__ = "alerts"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    key = db.Column(db.String(128), nullable=False)
    message = db.Column(db.Text, nullable=False)
    level = db.Column(db.String(16), default="danger", nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    dismissed_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship("User")

    @classmethod
    def push(cls, user_id: int, key: str, message: str, level: str = "danger") -> None:
        """Create an alert unless one with the same key is already undismissed.

        Rolls back any failed transaction first — safe to call from exception handlers.
        """
        try:
            db.session.rollback()
            existing = (
                cls.query
                .filter_by(user_id=user_id, key=key)
                .filter(cls.dismissed_at.is_(None))
                .first()
            )
            if not existing:
                db.session.add(cls(user_id=user_id, key=key, message=message, level=level))
                db.session.commit()
        except Exception:
            db.session.rollback()
            _log.exception("Alert.push failed for user %d key %r", user_id, key)


class AgentMemory(db.Model):
    """File-like memory for the agentic summary pipeline.

    Stored in the DB (not on disk) so the system stays multi-server safe.
    Kinds:
      interests       — per-user (summary_id NULL); evolving user interests
      content_config  — per-summary; structure/content prefs for that type
      history         — per-summary; running notes for trend-spotting
      headlines       — per-summary, one row per edition (edition_ts set);
                        brief notes on items covered, to avoid duplicate reporting
    """

    __tablename__ = "agent_memory"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    summary_id = db.Column(
        db.Integer, db.ForeignKey("summaries.id"), nullable=True, index=True
    )
    kind = db.Column(db.String(32), nullable=False)  # interests|content_config|history|headlines
    edition_ts = db.Column(db.DateTime, nullable=True)  # set only for headlines
    content = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        db.Index("ix_agent_memory_lookup", "user_id", "summary_id", "kind"),
    )


class AdminSettings(db.Model):
    """Single-row table for admin-managed global settings that aren't tied to
    any one user — currently just the shared podcast voice profile. The
    ElevenLabs credential itself stays a plain env var (``ELEVENLABS_API_KEY``),
    like the pre-ApiKey-system global OpenRouter key: one shared secret, not a
    per-row DB record."""

    __tablename__ = "admin_settings"

    id = db.Column(db.Integer, primary_key=True)
    elevenlabs_voice_host_a = db.Column(db.String(120), nullable=True)
    elevenlabs_voice_host_b = db.Column(db.String(120), nullable=True)
    elevenlabs_model = db.Column(db.String(120), nullable=True)
    # Whether anyone can self-register without an invite. Off by default —
    # registration is invite-only until an admin explicitly opts in.
    registration_open = db.Column(db.Boolean, default=False, nullable=False, server_default="0")

    @classmethod
    def get(cls) -> "AdminSettings":
        row = cls.query.first()
        if row is None:
            row = cls()
            db.session.add(row)
            db.session.commit()
        return row


class Invite(db.Model):
    """An admin-created invite link, redeemable up to ``max_uses`` times to
    register an account while registration is otherwise closed."""

    __tablename__ = "invites"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), nullable=False, unique=True, index=True)
    max_uses = db.Column(db.Integer, nullable=False, default=1)
    uses_count = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    revoked_at = db.Column(db.DateTime, nullable=True)

    created_by = db.relationship("User", foreign_keys=[created_by_user_id])

    @property
    def is_usable(self) -> bool:
        return self.revoked_at is None and self.uses_count < self.max_uses


class BalanceTransaction(db.Model):
    """Immutable audit log of every change to a User.balance_cents — one row
    per event, never updated in place. Powers the transaction history shown
    on the Payment page and is the idempotency guard for Lemon Squeezy
    webhook redelivery (see ls_event_id)."""

    __tablename__ = "balance_transactions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    kind = db.Column(db.String(20), nullable=False)  # topup|spend|adjustment
    amount_cents = db.Column(db.Integer, nullable=False)  # signed: +credit, -debit
    balance_after_cents = db.Column(db.Integer, nullable=False)
    # Set for kind="spend" once Phase 4 wires in actual spending.
    source_id = db.Column(db.Integer, db.ForeignKey("sources.id"), nullable=True, index=True)
    summary_run_id = db.Column(db.Integer, db.ForeignKey("summary_runs.id"), nullable=True, index=True)
    usage_kind = db.Column(db.String(20), nullable=True)  # ingest|confirm|agent|podcast_script|podcast_audio
    # Set for kind="topup": Lemon Squeezy references for support lookups.
    # ls_event_id has a unique index — it's the idempotency key that stops a
    # webhook redelivery from double-crediting the same order.
    ls_order_id = db.Column(db.String(64), nullable=True, index=True)
    ls_event_id = db.Column(db.String(64), nullable=True, unique=True, index=True)
    note = db.Column(db.String(255), nullable=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    user = db.relationship("User", foreign_keys=[user_id], back_populates="balance_transactions")
    created_by = db.relationship("User", foreign_keys=[created_by_user_id])


class LemonsqueezyProduct(db.Model):
    """Admin-managed mapping from a Lemon Squeezy variant to the USD amount
    credited to the buyer's balance on purchase. The Lemon Squeezy checkout
    price itself is set higher than credited_amount_cents in the Lemon
    Squeezy dashboard, to cover Lemon Squeezy's processing fee — this app
    never computes fee math, it trusts this row as the source of truth for
    what to credit."""

    __tablename__ = "lemonsqueezy_products"

    id = db.Column(db.Integer, primary_key=True)
    variant_id = db.Column(db.String(64), nullable=False, unique=True, index=True)
    label = db.Column(db.String(120), nullable=False)
    credited_amount_cents = db.Column(db.Integer, nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False, server_default="1")
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)


# Convenience export used by the factory.
__all__ = [
    "User",
    "AuthToken",
    "Alert",
    "ApiKey",
    "ApiKeyUsage",
    "IngestRun",
    "Source",
    "IgnoredSender",
    "NewsItem",
    "Tag",
    "NewsItemTag",
    "Summary",
    "SummaryRun",
    "AgentMemory",
    "AdminSettings",
    "EditionRecipient",
    "UserDisabledSource",
    "Invite",
    "BalanceTransaction",
    "LemonsqueezyProduct",
]
