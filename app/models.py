"""SQLAlchemy models for AI News."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from flask_login import UserMixin

from .extensions import db

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

    tags = db.relationship("Tag", back_populates="owner", lazy="dynamic")
    summaries = db.relationship("Summary", back_populates="user", lazy="dynamic")

    def set_password(self, password: str) -> None:
        self.password_hash = _ph.hash(password)

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
    config = db.Column(JSONEncodedDict, default=dict)
    poll_interval_override = db.Column(db.Integer, nullable=True)  # seconds
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    last_polled_at = db.Column(db.DateTime, nullable=True)
    last_status = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    items = db.relationship("NewsItem", back_populates="source", lazy="dynamic")
    ingest_runs = db.relationship(
        "IngestRun", back_populates="source", lazy="dynamic",
        cascade="all, delete-orphan",
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


# ─────────────────────────────── Tags ───────────────────────────────
class Tag(db.Model):
    __tablename__ = "tags"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    keywords = db.Column(JSONEncodedDict, default=list)  # list[str]
    explanation = db.Column(db.Text, nullable=True)
    scope = db.Column(db.String(10), default="user", nullable=False)  # global|user
    owner_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    owner = db.relationship("User", back_populates="tags")
    item_links = db.relationship(
        "NewsItemTag", back_populates="tag", lazy="dynamic",
        cascade="all, delete-orphan",
    )

    @property
    def keyword_list(self) -> list[str]:
        return self.keywords or []


class NewsItemTag(db.Model):
    __tablename__ = "news_item_tags"

    id = db.Column(db.Integer, primary_key=True)
    news_item_id = db.Column(
        db.Integer, db.ForeignKey("news_items.id"), nullable=False
    )
    tag_id = db.Column(db.Integer, db.ForeignKey("tags.id"), nullable=False)
    confidence = db.Column(db.Float, default=0.0)
    method = db.Column(db.String(10), default="nb")  # nb|llm|manual
    confirmed = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    item = db.relationship("NewsItem", back_populates="tag_links")
    tag = db.relationship("Tag", back_populates="item_links")

    __table_args__ = (
        db.UniqueConstraint("news_item_id", "tag_id", name="uq_item_tag"),
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

    user = db.relationship("User", back_populates="summaries")
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

    summary = db.relationship("Summary", back_populates="runs")
    revisions = db.relationship(
        "SummaryRun",
        backref=db.backref("parent", remote_side=[id]),
        lazy="dynamic",
    )


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


# Convenience export used by the factory.
__all__ = [
    "User",
    "AuthToken",
    "IngestRun",
    "Source",
    "NewsItem",
    "Tag",
    "NewsItemTag",
    "Summary",
    "SummaryRun",
    "AgentMemory",
]
