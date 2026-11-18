"""Runtime settings, read from environment variables."""
import os
from dataclasses import dataclass


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


@dataclass(frozen=True)
class Settings:
    secret_key: str = _env("SECRET_KEY")
    # PostgreSQL connection string, e.g. postgresql://user:password@host:5432/beacon
    database_url: str = _env("DATABASE_URL")
    timezone: str = _env("TIMEZONE", "UTC")
    cookie_secure: bool = _env("COOKIE_SECURE", "0") == "1"
    fail_threshold: int = int(_env("FAIL_THRESHOLD", "2"))
    renotify_minutes: int = int(_env("RENOTIFY_MINUTES", "60"))
    ssl_warn_days: int = int(_env("SSL_WARN_DAYS", "14"))
    history_days: int = int(_env("HISTORY_DAYS", "90"))
    invite_days: int = int(_env("INVITE_DAYS", "7"))
    # First owner, created at startup only when the database has no users yet.
    initial_org_name: str = _env("INITIAL_ORG_NAME")
    initial_admin_username: str = _env("INITIAL_ADMIN_USERNAME")
    initial_admin_password: str = _env("INITIAL_ADMIN_PASSWORD")
    # Platform-wide Telegram bot. Customers bind their own chats to it.
    telegram_bot_token: str = _env("TELEGRAM_BOT_TOKEN")
    telegram_bot_username: str = _env("TELEGRAM_BOT_USERNAME")
    # Secret path segment for the webhook URL: /telegram/webhook/<secret>
    telegram_webhook_secret: str = _env("TELEGRAM_WEBHOOK_SECRET")


settings = Settings()
