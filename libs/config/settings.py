from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator, model_validator
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
    webhook_listen_host: str = Field(default="0.0.0.0", alias="WEBHOOK_LISTEN_HOST")
    webhook_listen_port: int = Field(default=8443, alias="WEBHOOK_LISTEN_PORT")
    webhook_path: str = Field(default="telegram/webhook", alias="WEBHOOK_PATH")
    webhook_secret_token: str = Field(
        default="change-me-webhook-secret",
        alias="WEBHOOK_SECRET_TOKEN",
    )
    webhook_tls_cert_path: str | None = Field(default=None, alias="WEBHOOK_TLS_CERT_PATH")
    webhook_tls_key_path: str | None = Field(default=None, alias="WEBHOOK_TLS_KEY_PATH")
    webhook_set_enabled: bool = Field(default=True, alias="WEBHOOK_SET_ENABLED")
    bot_health_host: str = Field(default="0.0.0.0", alias="BOT_HEALTH_HOST")
    bot_health_port: int = Field(default=18080, alias="BOT_HEALTH_PORT")
    admin_telegram_ids: list[int] = Field(default_factory=list, alias="ADMIN_TELEGRAM_IDS")
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

    @field_validator("webhook_listen_host")
    @classmethod
    def validate_webhook_listen_host(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("WEBHOOK_LISTEN_HOST must not be empty")
        return value

    @field_validator("webhook_listen_port")
    @classmethod
    def validate_webhook_listen_port(cls, value: int) -> int:
        if value < 1 or value > 65535:
            raise ValueError("WEBHOOK_LISTEN_PORT must be in range 1..65535")
        return value

    @field_validator("webhook_path")
    @classmethod
    def validate_webhook_path(cls, value: str) -> str:
        normalized = value.strip().strip("/")
        if not normalized:
            raise ValueError("WEBHOOK_PATH must not be empty")
        return normalized

    @field_validator("webhook_secret_token")
    @classmethod
    def validate_webhook_secret_token(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("WEBHOOK_SECRET_TOKEN must not be empty")
        return value

    @field_validator("webhook_tls_cert_path", "webhook_tls_key_path", mode="before")
    @classmethod
    def normalize_optional_tls_path(cls, value):
        if value is None:
            return None
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value

    @model_validator(mode="after")
    def validate_webhook_tls_pair(self):
        if bool(self.webhook_tls_cert_path) != bool(self.webhook_tls_key_path):
            raise ValueError(
                "WEBHOOK_TLS_CERT_PATH and WEBHOOK_TLS_KEY_PATH must be set together",
            )
        return self

    @field_validator("bot_health_host")
    @classmethod
    def validate_bot_health_host(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("BOT_HEALTH_HOST must not be empty")
        return value

    @field_validator("bot_health_port")
    @classmethod
    def validate_bot_health_port(cls, value: int) -> int:
        if value < 1 or value > 65535:
            raise ValueError("BOT_HEALTH_PORT must be in range 1..65535")
        return value

    @field_validator("admin_telegram_ids", mode="before")
    @classmethod
    def parse_admin_telegram_ids(cls, value):
        if value in (None, "", []):
            return []
        if isinstance(value, int):
            return [value]
        if isinstance(value, str):
            raw_items = [item.strip() for item in value.split(",")]
            items = [item for item in raw_items if item]
            return [int(item) for item in items]
        if isinstance(value, list):
            return [int(item) for item in value]
        raise ValueError("ADMIN_TELEGRAM_IDS must be comma-separated integers")

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


class DailyReportScrapperSettings(BaseAppSettings):
    """Settings for hourly WB report scrapper cloud function."""

    token_cipher_key: str = Field(alias="TOKEN_CIPHER_KEY")
    wb_report_api_url: str = Field(
        default="https://statistics-api.wildberries.ru/api/v5/supplier/reportDetailByPeriod",
        alias="WB_REPORT_API_URL",
    )
    wb_report_timeout_seconds: int = Field(default=120, alias="WB_REPORT_TIMEOUT_SECONDS")
    wb_report_concurrency: int = Field(default=4, alias="WB_REPORT_CONCURRENCY")
    wb_report_limit: int = Field(default=100000, alias="WB_REPORT_LIMIT")
    wb_report_days_back: int = Field(default=3, alias="WB_REPORT_DAYS_BACK")
    wb_report_max_retries: int = Field(default=3, alias="WB_REPORT_MAX_RETRIES")
    wb_report_retry_delay_seconds: float = Field(
        default=1.0,
        alias="WB_REPORT_RETRY_DELAY_SECONDS",
    )

    @field_validator("token_cipher_key")
    @classmethod
    def validate_token_cipher_key(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("TOKEN_CIPHER_KEY must not be empty")
        return value

    @field_validator("wb_report_timeout_seconds")
    @classmethod
    def validate_wb_report_timeout(cls, value: int) -> int:
        if value < 1:
            raise ValueError("WB_REPORT_TIMEOUT_SECONDS must be >= 1")
        return value

    @field_validator("wb_report_concurrency")
    @classmethod
    def validate_wb_report_concurrency(cls, value: int) -> int:
        if value < 1:
            raise ValueError("WB_REPORT_CONCURRENCY must be >= 1")
        return value

    @field_validator("wb_report_limit")
    @classmethod
    def validate_wb_report_limit(cls, value: int) -> int:
        if value < 1:
            raise ValueError("WB_REPORT_LIMIT must be >= 1")
        return value

    @field_validator("wb_report_days_back")
    @classmethod
    def validate_wb_report_days_back(cls, value: int) -> int:
        if value < 1:
            raise ValueError("WB_REPORT_DAYS_BACK must be >= 1")
        return value

    @field_validator("wb_report_max_retries")
    @classmethod
    def validate_wb_report_max_retries(cls, value: int) -> int:
        if value < 0:
            raise ValueError("WB_REPORT_MAX_RETRIES must be >= 0")
        return value

    @field_validator("wb_report_retry_delay_seconds")
    @classmethod
    def validate_wb_report_retry_delay_seconds(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("WB_REPORT_RETRY_DELAY_SECONDS must be > 0")
        return value


class OrderTrackerSettings(BaseAppSettings):
    """Settings for 5-minute order tracker cloud function."""

    order_tracker_advisory_lock_id: int = Field(
        default=7006001,
        alias="ORDER_TRACKER_ADVISORY_LOCK_ID",
    )
    order_tracker_reservation_expiry_batch_size: int = Field(
        default=100,
        alias="ORDER_TRACKER_RESERVATION_EXPIRY_BATCH_SIZE",
    )
    order_tracker_wb_event_batch_size: int = Field(
        default=200,
        alias="ORDER_TRACKER_WB_EVENT_BATCH_SIZE",
    )
    order_tracker_delivery_expiry_batch_size: int = Field(
        default=200,
        alias="ORDER_TRACKER_DELIVERY_EXPIRY_BATCH_SIZE",
    )
    order_tracker_unlock_batch_size: int = Field(
        default=200,
        alias="ORDER_TRACKER_UNLOCK_BATCH_SIZE",
    )
    order_tracker_delivery_expiry_days: int = Field(
        default=60,
        alias="ORDER_TRACKER_DELIVERY_EXPIRY_DAYS",
    )
    order_tracker_unlock_days: int = Field(default=15, alias="ORDER_TRACKER_UNLOCK_DAYS")

    @field_validator("order_tracker_advisory_lock_id")
    @classmethod
    def validate_order_tracker_lock_id(cls, value: int) -> int:
        if value < 1:
            raise ValueError("ORDER_TRACKER_ADVISORY_LOCK_ID must be >= 1")
        return value

    @field_validator(
        "order_tracker_reservation_expiry_batch_size",
        "order_tracker_wb_event_batch_size",
        "order_tracker_delivery_expiry_batch_size",
        "order_tracker_unlock_batch_size",
    )
    @classmethod
    def validate_order_tracker_batch_size(cls, value: int) -> int:
        if value < 1:
            raise ValueError("ORDER_TRACKER_*_BATCH_SIZE must be >= 1")
        return value

    @field_validator("order_tracker_delivery_expiry_days", "order_tracker_unlock_days")
    @classmethod
    def validate_order_tracker_days(cls, value: int) -> int:
        if value < 1:
            raise ValueError("ORDER_TRACKER_*_DAYS must be >= 1")
        return value


@lru_cache(maxsize=1)
def get_bot_api_settings() -> BotApiSettings:
    return BotApiSettings()


@lru_cache(maxsize=1)
def get_worker_settings() -> WorkerSettings:
    return WorkerSettings()


@lru_cache(maxsize=1)
def get_daily_report_scrapper_settings() -> DailyReportScrapperSettings:
    return DailyReportScrapperSettings()


@lru_cache(maxsize=1)
def get_order_tracker_settings() -> OrderTrackerSettings:
    return OrderTrackerSettings()
