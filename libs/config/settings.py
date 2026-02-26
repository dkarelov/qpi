from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class BaseAppSettings(BaseSettings):
    """Base settings shared by all runtime services."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    app_env: str = Field(default="dev", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    database_url: str = Field(alias="DATABASE_URL")
    db_pool_min_size: int = Field(default=1, alias="DB_POOL_MIN_SIZE")
    db_pool_max_size: int = Field(default=10, alias="DB_POOL_MAX_SIZE")
    db_statement_timeout_ms: int = Field(default=5000, alias="DB_STATEMENT_TIMEOUT_MS")

    @field_validator("db_pool_min_size")
    @classmethod
    def validate_min_pool_size(cls, value: int) -> int:
        if value < 1:
            raise ValueError("DB_POOL_MIN_SIZE must be >= 1")
        return value

    @field_validator("db_pool_max_size")
    @classmethod
    def validate_max_pool_size(cls, value: int) -> int:
        if value < 1:
            raise ValueError("DB_POOL_MAX_SIZE must be >= 1")
        return value

    @field_validator("db_statement_timeout_ms")
    @classmethod
    def validate_statement_timeout(cls, value: int) -> int:
        if value < 100:
            raise ValueError("DB_STATEMENT_TIMEOUT_MS must be >= 100")
        return value


class BotApiSettings(BaseAppSettings):
    """Settings for the webhook bot API process."""

    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_bot_username: str = Field(default="qpi_marketplace_bot", alias="TELEGRAM_BOT_USERNAME")
    token_cipher_key: str = Field(default="dev-insecure-key", alias="TOKEN_CIPHER_KEY")
    webhook_base_url: str | None = Field(default=None, alias="WEBHOOK_BASE_URL")
    wb_ping_timeout_seconds: int = Field(default=10, alias="WB_PING_TIMEOUT_SECONDS")
    wb_ping_rate_limit_count: int = Field(default=3, alias="WB_PING_RATE_LIMIT_COUNT")
    wb_ping_rate_limit_window_seconds: int = Field(
        default=30,
        alias="WB_PING_RATE_LIMIT_WINDOW_SECONDS",
    )

    @field_validator("token_cipher_key")
    @classmethod
    def validate_token_cipher_key(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("TOKEN_CIPHER_KEY must not be empty")
        return value

    @field_validator("wb_ping_timeout_seconds")
    @classmethod
    def validate_wb_ping_timeout(cls, value: int) -> int:
        if value < 1:
            raise ValueError("WB_PING_TIMEOUT_SECONDS must be >= 1")
        return value

    @field_validator("wb_ping_rate_limit_count")
    @classmethod
    def validate_wb_ping_rate_limit_count(cls, value: int) -> int:
        if value < 1:
            raise ValueError("WB_PING_RATE_LIMIT_COUNT must be >= 1")
        return value

    @field_validator("wb_ping_rate_limit_window_seconds")
    @classmethod
    def validate_wb_ping_rate_limit_window(cls, value: int) -> int:
        if value < 1:
            raise ValueError("WB_PING_RATE_LIMIT_WINDOW_SECONDS must be >= 1")
        return value


class WorkerSettings(BaseAppSettings):
    """Settings for the background worker process."""

    worker_poll_interval_seconds: int = Field(default=30, alias="WORKER_POLL_INTERVAL_SECONDS")
    worker_reservation_expiry_batch_size: int = Field(
        default=100,
        alias="WORKER_RESERVATION_EXPIRY_BATCH_SIZE",
    )

    @field_validator("worker_poll_interval_seconds")
    @classmethod
    def validate_poll_interval(cls, value: int) -> int:
        if value < 1:
            raise ValueError("WORKER_POLL_INTERVAL_SECONDS must be >= 1")
        return value

    @field_validator("worker_reservation_expiry_batch_size")
    @classmethod
    def validate_expiry_batch_size(cls, value: int) -> int:
        if value < 1:
            raise ValueError("WORKER_RESERVATION_EXPIRY_BATCH_SIZE must be >= 1")
        return value


@lru_cache(maxsize=1)
def get_bot_api_settings() -> BotApiSettings:
    return BotApiSettings()


@lru_cache(maxsize=1)
def get_worker_settings() -> WorkerSettings:
    return WorkerSettings()
