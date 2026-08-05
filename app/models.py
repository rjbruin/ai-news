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


# Many-to-many: which Dispatches (Summary rows) a user follows. A user reads
# every edition of every Dispatch they follow; they always follow their own.
dispatch_subscriptions = db.Table(
    "dispatch_subscriptions",
    db.Column("user_id", db.Integer, db.ForeignKey("users.id"), primary_key=True),
    db.Column("summary_id", db.Integer, db.ForeignKey("summaries.id"), primary_key=True),
)

# Many-to-many: which followed Dispatches a user has opted into receiving by
# email, at their single `User.newsletter_email`. A row can only exist for a
# summary the user also follows (see dispatch_unfollow / dispatch_publish
# unpublish, which clear rows here alongside/independently of follows).
dispatch_email_subscriptions = db.Table(
    "dispatch_email_subscriptions",
    db.Column("user_id", db.Integer, db.ForeignKey("users.id"), primary_key=True),
    db.Column("summary_id", db.Integer, db.ForeignKey("summaries.id"), primary_key=True),
)


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

    # Podcast export is opt-in per user (admins always have it, and anyone
    # with their own ElevenLabs key self-serves — see has_podcast_access).
    # This flag is now just a legacy/admin-granted override; voice IDs and
    # the model remain global admin settings (see AdminSettings).
    podcast_enabled = db.Column(db.Boolean, default=False, nullable=False, server_default="0")
    podcast_auto_generate = db.Column(db.Boolean, default=False, nullable=False, server_default="0")
    pdf_font_scale = db.Column(db.Integer, default=80, nullable=False, server_default="80")

    # Secret token embedded in the personal podcast RSS feed URL, so podcast
    # apps (which can't do session login) can fetch the feed and its MP3s.
    podcast_feed_token = db.Column(db.String(64), nullable=True, unique=True, index=True)

    # Single email address for edition newsletters, shared across every
    # Dispatch the user opts into by email (see dispatch_email_subscriptions).
    # Changing the address always clears confirmation and requires re-confirm.
    newsletter_email = db.Column(db.String(255), nullable=True)
    newsletter_email_confirmed_at = db.Column(db.DateTime, nullable=True)
    newsletter_email_confirm_token = db.Column(db.String(64), nullable=True, unique=True, index=True)

    # Gate for self-service source/API-key management. Admins are always
    # implicitly approved (see is_approved); this flag is for everyone else.
    approved = db.Column(db.Boolean, default=False, nullable=False, server_default="0")

    # Whether the first-visit onboarding tutorial has been shown. Flipped to
    # True the moment it's shown (not on explicit dismissal) so it reliably
    # only ever appears once, even if the user closes the tab without
    # clicking anything.
    has_seen_onboarding = db.Column(db.Boolean, default=False, nullable=False, server_default="0")

    # Version string of the last release this user was shown the changelog modal
    # for (see app/changelog.py). Compared against the running app_version on
    # every request; flipped the moment a newer entry is shown (or skipped, if
    # a release has no changelog entry) so it can't repeat.
    last_seen_version = db.Column(db.String(32), nullable=True)

    tags = db.relationship("Tag", back_populates="owner", lazy="dynamic")
    summaries = db.relationship(
        "Summary", back_populates="user", lazy="dynamic",
        foreign_keys="Summary.user_id",
    )
    # Dispatches this user follows (reads). Always includes their own, if any.
    subscribed_dispatches = db.relationship(
        "Summary", secondary=dispatch_subscriptions, lazy="dynamic",
    )
    # Followed Dispatches this user has additionally opted into by email.
    email_subscribed_dispatches = db.relationship(
        "Summary", secondary=dispatch_email_subscriptions, lazy="dynamic",
    )

    def follow(self, summary: "Summary") -> None:
        if not self.is_following(summary):
            self.subscribed_dispatches.append(summary)

    def unfollow(self, summary: "Summary") -> None:
        if self.is_following(summary):
            self.subscribed_dispatches.remove(summary)
        self.unsubscribe_email(summary)

    def is_following(self, summary: "Summary") -> bool:
        return self.subscribed_dispatches.filter(
            dispatch_subscriptions.c.summary_id == summary.id
        ).count() > 0

    def subscribe_email(self, summary: "Summary") -> None:
        if not self.is_email_subscribed(summary):
            self.email_subscribed_dispatches.append(summary)

    def unsubscribe_email(self, summary: "Summary") -> None:
        if self.is_email_subscribed(summary):
            self.email_subscribed_dispatches.remove(summary)

    def is_email_subscribed(self, summary: "Summary") -> bool:
        return self.email_subscribed_dispatches.filter(
            dispatch_email_subscriptions.c.summary_id == summary.id
        ).count() > 0

    @property
    def own_dispatch(self) -> "Summary | None":
        """This user's own custom Dispatch (Summary), or None if they only
        read others'. Truthy = "dispatch user" — gates topic management, the
        per-user source toggle, PDF export, and publishing."""
        return Summary.query.filter_by(
            user_id=self.id, type_key="agentic_page"
        ).first()

    @property
    def api_key(self) -> "ApiKey | None":
        """This user's single OpenRouter key, or None if they haven't added
        one. Funds both their own edition generation and every Source they
        own — one key per user, no per-source assignment."""
        return ApiKey.query.filter_by(owner_user_id=self.id, provider="openrouter", is_global=False).first()

    @property
    def elevenlabs_key(self) -> "ApiKey | None":
        """This user's own ElevenLabs key, or None. Funds their own podcast
        generation (script→audio) — set the same way as the OpenRouter key,
        on the Your Dispatch page."""
        return ApiKey.query.filter_by(owner_user_id=self.id, provider="elevenlabs", is_global=False).first()

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
        everyone else self-serves by bringing their own ElevenLabs key (see
        elevenlabs_key), same pattern as the OpenRouter key — an admin can
        also still grant test access via the legacy ``podcast_enabled`` flag."""
        return bool(self.elevenlabs_key) or bool(self.podcast_enabled) or self.is_admin

    @property
    def newsletter_email_is_confirmed(self) -> bool:
        return bool(self.newsletter_email) and self.newsletter_email_confirmed_at is not None


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
    """A credential — OpenRouter (funds a user's own edition generation and
    every Source they own) or ElevenLabs (funds their own podcast audio
    generation) — one row per (owner, provider) (enforced by the partial
    unique index below), plus the single seeded global OpenRouter key.

    ``owner_user_id`` is NULL only for the global key (``is_global=True``) —
    it is conceptually owned by every admin rather than any one user, so any
    admin can view/manage it (see the admin Settings form). It funds every
    ownerless ("system") Source. Its secret is normally set via that form
    (encrypted into ``key_enc``, like any other key); the ``OPENROUTER_API_KEY``
    env var is a deploy-time fallback for when no admin has set one yet.
    """

    __tablename__ = "api_keys"
    __table_args__ = (
        db.Index(
            "uq_api_keys_owner_provider", "owner_user_id", "provider",
            unique=True, sqlite_where=db.text("owner_user_id IS NOT NULL"),
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    owner_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    label = db.Column(db.String(120), nullable=False)
    provider = db.Column(db.String(30), default="openrouter", nullable=False)
    # NULL for the global key until an admin sets one via the Settings form,
    # in which case get_key() falls back to the OPENROUTER_API_KEY env var.
    key_enc = db.Column(db.Text, nullable=True)
    is_global = db.Column(db.Boolean, default=False, nullable=False)
    revoked_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    owner = db.relationship("User", foreign_keys=[owner_user_id])
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
        """Return the usable secret.

        For every key, ``key_enc`` (set via the owner's or admin's form) wins
        when present. The global key alone has a second fallback — the
        ``OPENROUTER_API_KEY`` env var — for deploys where no admin has set
        one via the Settings form yet.
        """
        from .crypto import decrypt

        if self.key_enc:
            return decrypt(self.key_enc)
        if self.is_global:
            from flask import current_app

            return current_app.config.get("OPENROUTER_API_KEY") or None
        return None

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
        privacy stance as owner_display. Funding is implicit from ownership:
        an owned source is always paid for by its owner's one API key
        (global key for ownerless/system sources)."""
        if self.owner_user_id is None:
            return "Included in system"
        if self.owner_user_id == viewer.id:
            return "your API key"
        return "another user's API key"

    def usage_visible_to(self, viewer: "User") -> bool:
        """Only the owner gets to see their own usage/cost — not the
        operator's global spend, not another user's."""
        return self.owner_user_id is not None and self.owner_user_id == viewer.id

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
        """Identity of a story for ingest deduplication.

        The URL is normalized (see app/urls.py) rather than compared raw: the
        same article arriving as ``…/story/`` and ``…/story`` is one story, not
        two. A bare-domain URL is dropped entirely — newsletter extraction
        sometimes yields the publisher's root instead of the article link, and
        hashing that in forks a single story into separate rows.
        """
        from .urls import looks_like_article_url, norm_url

        key = norm_url(url) if looks_like_article_url(url) else ""
        norm = (title or "").strip().lower() + "|" + key
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

    # At most one Summary system-wide should ever have this set — it's the
    # default "System Dispatch" every new user is subscribed to. Enforced in
    # application code (see web.admin's toggle route), not a DB constraint.
    is_system_dispatch = db.Column(db.Boolean, default=False, nullable=False, server_default="0")

    # Publishing: a published Dispatch appears in the /dispatches directory and
    # can be followed by anyone. published_name is the short public title the
    # owner chooses (unique across all Dispatches, ≤25 chars).
    is_published = db.Column(db.Boolean, default=False, nullable=False, server_default="0")
    published_name = db.Column(db.String(25), unique=True, nullable=True)

    # Shown on the /dispatches directory card and the static details page.
    description = db.Column(db.Text, nullable=True)

    # Whether followers (and the owner) may export this Dispatch's editions as
    # PDF. Off by default — owner opts in per Dispatch.
    pdf_export_enabled = db.Column(db.Boolean, default=False, nullable=False, server_default="0")

    # Review editions: a slower retrospective cadence over this Dispatch's own
    # editions (see docs/review-editions-spec.md). NULL = off; otherwise one of
    # week|month|quarter|year. Reviews are SummaryRuns with kind="review" on
    # this same Summary, so they inherit its followers, publishing state, email
    # subscribers and podcast feed rather than forking them.
    review_period = db.Column(db.String(20), nullable=True)

    user = db.relationship(
        "User", back_populates="summaries", foreign_keys=[user_id]
    )
    runs = db.relationship(
        "SummaryRun", back_populates="summary", lazy="dynamic",
        cascade="all, delete-orphan",
    )

    @classmethod
    def get_system_dispatch(cls) -> "Summary | None":
        return cls.query.filter_by(is_system_dispatch=True).first()

    @property
    def email_subscriber_count(self) -> int:
        return db.session.query(dispatch_email_subscriptions).filter_by(summary_id=self.id).count()

    @property
    def follower_count(self) -> int:
        return db.session.query(dispatch_subscriptions).filter_by(summary_id=self.id).count()

    @classmethod
    def published(cls):
        """Query of all published Dispatches (for the directory + onboarding)."""
        return cls.query.filter_by(is_published=True, enabled=True)

    @property
    def display_name(self) -> str:
        """Public title: the published name if set, else the owner's own name."""
        return self.published_name or self.name


class SummaryRun(db.Model):
    __tablename__ = "summary_runs"

    id = db.Column(db.Integer, primary_key=True)
    summary_id = db.Column(db.Integer, db.ForeignKey("summaries.id"), nullable=False)
    # "edition" (the normal cadence) or "review" (the slower retrospective over
    # a period's editions — see docs/review-editions-spec.md). Anything asking
    # for "the latest run" as a proxy for "the latest edition" MUST filter on
    # this: an unfiltered lookup lets a review convince the scheduler that the
    # daily period is already cut, which silently stops daily editions.
    kind = db.Column(
        db.String(16), default="edition", nullable=False, server_default="edition"
    )
    generated_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    range_start = db.Column(db.DateTime, nullable=True)
    range_end = db.Column(db.DateTime, nullable=True)
    item_count = db.Column(db.Integer, default=0)
    label = db.Column(db.String(120), nullable=True)   # e.g. "Tuesday June 22"
    # The edition's actual newsworthy headline (e.g. "OpenAI ships GPT-6 with
    # native tool use") — extracted from the agent's edition_header block at
    # build time, kept as its own field so surfaces like the homepage can
    # show it without rendering/parsing the whole document. Distinct from
    # `label`, which is just the date.
    headline = db.Column(db.String(300), nullable=True)
    content = db.Column(db.Text, nullable=True)         # rendered HTML artifact
    artifact_ref = db.Column(db.String(500), nullable=True)
    status = db.Column(db.String(20), default="ok")

    # Set when status == "failed": the exception message shown on the
    # edition's page, and (for a failed revision) enough context to retry
    # without the reader retyping their feedback — see
    # app.services.summarize._persist_failed_run / web.edition_retry.
    error_message = db.Column(db.Text, nullable=True)
    retry_context = db.Column(JSONEncodedDict, nullable=True)

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

    share_token = db.Column(db.String(64), nullable=True, unique=True, index=True)

    summary = db.relationship("Summary", back_populates="runs")
    revisions = db.relationship(
        "SummaryRun",
        backref=db.backref("parent", remote_side=[id]),
        lazy="dynamic",
    )

    __table_args__ = (
        # Every "latest run for this Dispatch" lookup is now also filtered by
        # kind — see the column's note.
        db.Index("ix_summary_runs_summary_kind", "summary_id", "kind"),
    )

    @property
    def is_review(self) -> bool:
        return self.kind == "review"

    def read_at_for(self, user) -> "datetime | None":
        row = EditionRead.query.filter_by(user_id=user.id, run_id=self.id).first()
        return row.read_at if row else None

    def is_read_by(self, user) -> bool:
        return self.read_at_for(user) is not None


class EditionRead(db.Model):
    """Per-user read state for an edition — a Dispatch can be followed by
    multiple readers, each with their own read/unread marker (unlike
    everything else about a SummaryRun, which is shared/owner-controlled)."""

    __tablename__ = "edition_reads"

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), primary_key=True)
    run_id = db.Column(db.Integer, db.ForeignKey("summary_runs.id"), primary_key=True)
    read_at = db.Column(db.DateTime, default=utcnow, nullable=False)


class ItemFeedback(db.Model):
    """A reader's up/down vote on one item block within one edition.

    Anchored on ``block_id`` (the vote is on *this writeup*, which is what the
    reader actually saw) but also stores ``item_id`` where the block cites one
    — that's the part that generalises, since the same NewsItem can be written
    up again in a later edition. ``item_id`` is nullable because an item block
    covering several sources has no single item to point at.

    Votes are recorded for every reader (followers included) as an engagement
    signal, but only the Dispatch *owner's* votes steer generation — see
    app/services/reader_feedback.py. Owner-only is deliberate: they pay for
    generation and control editorial direction.
    """

    __tablename__ = "item_feedback"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    run_id = db.Column(db.Integer, db.ForeignKey("summary_runs.id"), nullable=False, index=True)
    block_id = db.Column(db.String(64), nullable=False)
    # NULL when the block cites no single NewsItem (multi-source writeup), or
    # when the cited item has since been deleted.
    item_id = db.Column(
        db.Integer, db.ForeignKey("news_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    vote = db.Column(db.Integer, nullable=False)  # +1 or -1
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("user_id", "run_id", "block_id", name="uq_item_feedback_user_block"),
    )

    @classmethod
    def record(cls, user_id: int, run_id: int, block_id: str, item_id, vote: int):
        """Set, flip, or clear one vote. Re-sending the same vote clears it
        (the badge is a toggle), so a reader can undo a misclick without a
        separate control. Returns the resulting vote (+1/-1) or None."""
        row = cls.query.filter_by(
            user_id=user_id, run_id=run_id, block_id=block_id
        ).first()
        if row is None:
            db.session.add(cls(
                user_id=user_id, run_id=run_id, block_id=block_id,
                item_id=item_id, vote=vote,
            ))
            return vote
        if row.vote == vote:
            db.session.delete(row)
            return None
        row.vote = vote
        row.item_id = item_id
        return vote


class PageVisit(db.Model):
    """Anonymous, cookie-free page-visit counter.

    One row per (endpoint, day) — every matching request increments the
    counter in place rather than logging a row per hit, so this stays cheap
    to store and query even at high traffic. No visitor identity is kept;
    this can only ever answer "how many times was this page requested", not
    "how many distinct people visited"."""

    __tablename__ = "page_visits"

    id = db.Column(db.Integer, primary_key=True)
    endpoint = db.Column(db.String(120), nullable=False)
    date = db.Column(db.Date, nullable=False)
    count = db.Column(db.Integer, default=0, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("endpoint", "date", name="uq_page_visits_endpoint_date"),
    )

    @classmethod
    def record(cls, endpoint: str, day) -> None:
        row = cls.query.filter_by(endpoint=endpoint, date=day).first()
        if row is None:
            row = cls(endpoint=endpoint, date=day, count=0)
            db.session.add(row)
        row.count += 1


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
      quick_hits      — per-summary, one row per edition (edition_ts set);
                        system-derived (not agent-written) JSON list of
                        {item_id, headline} for more_news entries that cited
                        an in-scope item — lets a later edition see a story
                        as an escalation candidate (see app.agent.memory)
    """

    __tablename__ = "agent_memory"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    summary_id = db.Column(
        db.Integer, db.ForeignKey("summaries.id"), nullable=True, index=True
    )
    kind = db.Column(db.String(32), nullable=False)  # interests|content_config|history|headlines|quick_hits
    edition_ts = db.Column(db.DateTime, nullable=True)  # set only for headlines/quick_hits
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
    "UserDisabledSource",
    "Invite",
]
