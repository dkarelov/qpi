import re
from dataclasses import dataclass
from os import environ


@dataclass
class BotConfig:
    """
    Data class representing the configuration for the bot.

    Attributes:
    - TOKEN (str): The bot token.
    - DEV_IDS (list[int]): The developer/admin user IDs (first one is primary).
    - GROUP_ID (int): The group chat ID.
    - BOT_EMOJI_ID (str): The custom emoji ID for the group's topic.
    """

    TOKEN: str
    DEV_IDS: list[int]
    GROUP_ID: int
    BOT_EMOJI_ID: str | None = None

    @property
    def DEV_ID(self) -> int:
        """Primary developer ID (kept for single-recipient notifications)."""
        return self.DEV_IDS[0]


@dataclass
class RedisConfig:
    """
    Data class representing the configuration for Redis (FSM + apscheduler only).

    Attributes:
    - HOST (str): The Redis host.
    - PORT (int): The Redis port.
    - DB (int): The Redis database number.
    - PASSWORD (str): The Redis password (empty when the instance has no auth).
    """

    HOST: str
    PORT: int
    DB: int
    PASSWORD: str = ""

    def dsn(self) -> str:
        """
        Generates a Redis connection DSN using host, port, db and optional password.

        :return: The generated DSN.
        """
        auth = f":{self.PASSWORD}@" if self.PASSWORD else ""
        return f"redis://{auth}{self.HOST}:{self.PORT}/{self.DB}"


@dataclass
class DatabaseConfig:
    """
    Data class representing the configuration for PostgreSQL (user layer + subscribers).

    Attributes:
    - URL (str): asyncpg DSN, e.g. ``postgresql://user:pass@host:5432/db``.
    """

    URL: str
    SCHEMA: str = "support_bot"


@dataclass
class TelegramConfig:
    """Telegram Bot API transport settings."""

    PROXY_URL: str | None = None


@dataclass
class PolicyConfig:
    """
    Data class representing the configuration for the optional policy engine.

    Attributes:
    - ENABLED (bool): Whether the declarative policy engine is active.
    - PATH (str): Path to the policy YAML file.
    - INLINE_B64 (str): Base64-encoded YAML used when PATH does not exist
      (handy for platforms where mounting a file is awkward).
    """

    ENABLED: bool
    PATH: str
    INLINE_B64: str = ""


@dataclass
class AIConfig:
    """
    Data class representing the configuration for the optional LLM provider.

    Attributes:
    - PROVIDER (str): "none" disables the LLM; "openai_compatible" enables it.
    - BASE_URL (str): OpenAI-compatible base URL (OpenRouter, OpenAI, local, ...).
    - API_KEY (str): API key; empty disables the provider.
    - MODEL (str): Model identifier.
    - SYSTEM_PROMPT_PATH (str): Path to the system prompt file.
    - TIMEOUT_S (int): Per-request timeout in seconds.
    """

    PROVIDER: str
    BASE_URL: str
    API_KEY: str
    MODEL: str
    SYSTEM_PROMPT_PATH: str
    TIMEOUT_S: int
    SYSTEM_PROMPT_B64: str = ""


@dataclass
class Config:
    """
    Data class representing the overall configuration for the application.

    Attributes:
    - bot (BotConfig): The bot configuration.
    - redis (RedisConfig): The Redis configuration (FSM + scheduler).
    - db (DatabaseConfig): The PostgreSQL configuration (user layer + subscribers).
    - policy (PolicyConfig): The policy engine configuration.
    - ai (AIConfig): The LLM provider configuration.
    """

    bot: BotConfig
    redis: RedisConfig
    db: DatabaseConfig
    telegram: TelegramConfig
    policy: PolicyConfig
    ai: AIConfig


def _env(name: str, default: str | None = None) -> str:
    value = environ.get(name)
    if value is None:
        if default is None:
            raise RuntimeError(f"{name} is required")
        return default
    return value


def _first_present(*names: str, default: str | None = None) -> str:
    for name in names:
        value = environ.get(name)
        if value not in (None, ""):
            assert value is not None
            return value
    if default is None:
        joined = " or ".join(names)
        raise RuntimeError(f"{joined} is required")
    return default


def _csv_ints(value: str) -> list[int]:
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def select_first_proxy(proxy_urls: str | None) -> str | None:
    """Return the first configured Telegram proxy URL from comma/newline-separated input."""
    if not proxy_urls:
        return None
    for item in re.split(r"[,\n]+", proxy_urls):
        item = item.strip()
        if item:
            return item
    return None


def load_config() -> Config:
    """
    Load the configuration from environment variables and return a Config object.

    :return: The Config object with loaded configuration.
    """
    dev_ids_raw = _first_present("SUPPORT_BOT_DEV_IDS", "BOT_DEV_IDS", default="")
    if dev_ids_raw:
        dev_ids = _csv_ints(dev_ids_raw)
    else:
        dev_ids = [int(_first_present("SUPPORT_BOT_OWNER_ID", "BOT_DEV_ID"))]

    return Config(
        bot=BotConfig(
            TOKEN=_first_present("SUPPORT_BOT_TELEGRAM_BOT_TOKEN", "BOT_TOKEN"),
            DEV_IDS=dev_ids,
            GROUP_ID=int(_first_present("SUPPORT_BOT_GROUP_ID", "BOT_GROUP_ID")),
            BOT_EMOJI_ID=_first_present("SUPPORT_BOT_TOPIC_EMOJI_ID", "BOT_EMOJI_ID", default="") or None,
        ),
        redis=RedisConfig(
            HOST=_env("REDIS_HOST", "support-bot-redis"),
            PORT=int(_env("REDIS_PORT", "6379")),
            DB=int(_env("REDIS_DB", "7")),
            PASSWORD=_env("REDIS_PASSWORD", ""),
        ),
        db=DatabaseConfig(
            URL=_env("DATABASE_URL"),
            SCHEMA=_env("SUPPORT_BOT_DB_SCHEMA", "support_bot"),
        ),
        telegram=TelegramConfig(
            PROXY_URL=select_first_proxy(environ.get("TELEGRAM_API_PROXY_URLS")),
        ),
        policy=PolicyConfig(
            ENABLED=_env("POLICY_ENABLED", "false").lower() in {"1", "true", "yes", "on"},
            PATH=_env("POLICY_CONFIG_PATH", "config/policy.yaml"),
            INLINE_B64=_env("POLICY_YAML_B64", ""),
        ),
        ai=AIConfig(
            PROVIDER=_env("AI_PROVIDER", "none"),
            BASE_URL=_env("AI_BASE_URL", "https://openrouter.ai/api/v1"),
            API_KEY=_env("AI_API_KEY", ""),
            MODEL=_env("AI_MODEL", "openai/gpt-5.4-nano"),
            SYSTEM_PROMPT_PATH=_env("AI_SYSTEM_PROMPT_PATH", "config/system_prompt.txt"),
            TIMEOUT_S=int(_env("AI_TIMEOUT_S", "8")),
            SYSTEM_PROMPT_B64=_env("AI_SYSTEM_PROMPT_B64", ""),
        ),
    )
