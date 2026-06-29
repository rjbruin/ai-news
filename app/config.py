"""Application configuration, loaded from environment variables."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env from the project root if present.
load_dotenv(BASE_DIR / ".env")


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _emails(value: str | None) -> list[str]:
    if not value:
        return []
    return [e.strip().lower() for e in value.split(",") if e.strip()]


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-secret")
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_SAMESITE = "Lax"

    # Database
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{BASE_DIR / 'instance' / 'ainews.db'}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"timeout": 30}}

    # Admins
    ADMIN_EMAILS = _emails(os.environ.get("ADMIN_EMAILS"))

    # LLM (global OpenRouter key)
    OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
    OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-haiku-4.5")
    OPENROUTER_BASE_URL = os.environ.get(
        "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
    )

    # Tagging
    TAGGING_MODE = os.environ.get("TAGGING_MODE", "nb_then_llm")
    NB_CONFIDENCE_THRESHOLD = float(os.environ.get("NB_CONFIDENCE_THRESHOLD", "0.30"))

    # TTS
    ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
    ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "")

    # SMTP
    SMTP_HOST = os.environ.get("SMTP_HOST", "")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USE_TLS = _bool(os.environ.get("SMTP_USE_TLS"), True)
    SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
    MAIL_FROM = os.environ.get("MAIL_FROM", "AI News <noreply@example.com>")

    # IMAP (default newsletter mailbox)
    IMAP_HOST = os.environ.get("IMAP_HOST", "")
    IMAP_PORT = int(os.environ.get("IMAP_PORT", "993"))
    IMAP_USERNAME = os.environ.get("IMAP_USERNAME", "")
    IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD", "")
    IMAP_FOLDER = os.environ.get("IMAP_FOLDER", "INBOX")

    # Scheduler
    POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "3600"))
    WORKER_ENABLED = _bool(os.environ.get("WORKER_ENABLED"), True)

    # App
    PORT = int(os.environ.get("PORT", "5090"))
    PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:5090")

    # Debug / local dev
    DEBUG_SEED = _bool(os.environ.get("DEBUG_SEED"), False)

    # Agentic summary pipeline
    AGENT_ENABLED = _bool(os.environ.get("AGENT_ENABLED"), True)
    AGENT_MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "24"))
    AGENT_MAX_TOKENS = int(os.environ.get("AGENT_MAX_TOKENS", "0"))  # 0 = no cap
    AGENT_HEADLINES_RETENTION_DAYS = int(
        os.environ.get("AGENT_HEADLINES_RETENTION_DAYS", "7")
    )


class DevConfig(Config):
    SESSION_COOKIE_SECURE = False


class DebugConfig(DevConfig):
    """Local debug mode: seeds fake news items and force-cuts summary editions at startup."""
    DEBUG_SEED = True


class IntegrationTestConfig(Config):
    """Like TestConfig but keeps the real OPENROUTER_API_KEY so LLM calls work.

    Used by tests/test_extraction_integration.py — the tests skip themselves
    when the key is absent, so this config is safe to use unconditionally.
    """
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    SESSION_COOKIE_SECURE = False
    WORKER_ENABLED = False
    SECRET_KEY = "test-secret"
    ADMIN_EMAILS = ["admin@example.com"]
    TAGGING_MODE = "nb_only"
    # OPENROUTER_API_KEY is intentionally NOT overridden — inherits from Config


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    SESSION_COOKIE_SECURE = False
    WORKER_ENABLED = False
    SECRET_KEY = "test-secret"
    ADMIN_EMAILS = ["admin@example.com"]
    OPENROUTER_API_KEY = ""
    TAGGING_MODE = "nb_only"
