from __future__ import annotations

import re
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{16,256}$")


class Settings(BaseSettings):
    """Validated runtime configuration loaded exclusively from environment variables."""

    bot_token: SecretStr
    database_url: str
    webhook_secret: SecretStr

    channel_username: str = "@ShadowTalkCF"
    owner_id: int = 6289461565
    moderator_ids_csv: str = "6289461565"

    webhook_base_url: str | None = None
    render_external_url: str | None = None
    log_level: str = "INFO"
    max_webhook_body_bytes: int = Field(default=2_000_000, ge=16_384, le=10_000_000)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("channel_username")
    @classmethod
    def validate_channel_username(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith("@") or len(value) < 6:
            raise ValueError("CHANNEL_USERNAME must be a Telegram @username")
        return value

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("DATABASE_URL cannot be empty")
        if not value.startswith(("postgres://", "postgresql://", "postgresql+asyncpg://")):
            raise ValueError("DATABASE_URL must point to PostgreSQL")
        return value

    @field_validator("webhook_secret")
    @classmethod
    def validate_webhook_secret(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not _SECRET_RE.fullmatch(raw):
            raise ValueError(
                "WEBHOOK_SECRET must be 16-256 chars using letters, digits, '_' or '-'"
            )
        return value

    @property
    def moderator_ids(self) -> frozenset[int]:
        ids: set[int] = {self.owner_id}
        for raw in self.moderator_ids_csv.split(","):
            raw = raw.strip()
            if not raw:
                continue
            try:
                parsed = int(raw)
            except ValueError as exc:
                raise ValueError(f"Invalid moderator Telegram ID: {raw!r}") from exc
            if parsed <= 0:
                raise ValueError(f"Moderator Telegram ID must be positive: {parsed}")
            ids.add(parsed)
        return frozenset(ids)

    @property
    def sqlalchemy_database_url(self) -> str:
        """Return an asyncpg-compatible URL, stripping libpq-only query arguments."""
        url = self.database_url
        if url.startswith("postgres://"):
            url = "postgresql+asyncpg://" + url.removeprefix("postgres://")
        elif url.startswith("postgresql://"):
            url = "postgresql+asyncpg://" + url.removeprefix("postgresql://")

        parts = urlsplit(url)
        filtered = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key not in {"channel_binding", "sslmode"}
        ]
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(filtered), parts.fragment)
        )

    @property
    def public_base_url(self) -> str:
        raw = self.webhook_base_url or self.render_external_url
        if not raw:
            raise ValueError(
                "WEBHOOK_BASE_URL or RENDER_EXTERNAL_URL is required for webhook mode"
            )
        return raw.rstrip("/")

    @property
    def webhook_url(self) -> str:
        return f"{self.public_base_url}/telegram/webhook"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
