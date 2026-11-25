"""Environment settings for Coach Seek."""

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


def setting(name: str, default: str = "") -> str:
    return os.getenv(name, "").strip() or default


def channel_setting(name: str) -> int | None:
    value = setting(name)
    if not value:
        return None
    if not value.isascii() or not value.isdecimal() or len(value) > 20:
        raise ValueError(f"{name} must be a numeric Discord channel ID.")
    channel_id = int(value)
    if not 0 < channel_id < 2**64:
        raise ValueError(f"{name} must be a valid Discord channel ID.")
    return channel_id


def digest_duration(name: str, default: str, *, allow_zero: bool = False) -> float:
    try:
        value = float(setting(name, default))
    except ValueError:
        value = math.nan
    if not math.isfinite(value * 3600) or value < 0 or (value == 0 and not allow_zero):
        minimum = "nonnegative" if allow_zero else "positive"
        raise ValueError(f"{name} must be a finite {minimum} number.")
    return value


@dataclass(frozen=True)
class Config:
    discord_token: str = field(repr=False)
    deepseek_api_key: str = field(repr=False)
    model: str
    base_url: str
    channel_id: int | None
    reasoning_effort: str
    thinking: str
    max_tokens: int
    log_level: str
    web_search: bool = True
    safety_level: str = "high"
    memory_db_path: Path = Path(__file__).resolve().parent.parent / "data/memory.sqlite"
    digest_channel_id: int | None = None
    digest_enabled: bool = True
    digest_interval_hours: float = 24
    digest_initial_delay_seconds: float = 60
    digest_db_path: Path = memory_db_path.with_name("digest.sqlite")

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv(Path.cwd() / ".env", interpolate=False)
        token = setting("DISCORD_TOKEN")
        key = setting("DEEPSEEK_API_KEY")
        missing = [
            name for name, value, placeholder in (
                ("DISCORD_TOKEN", token, "your-discord-bot-token"),
                ("DEEPSEEK_API_KEY", key, "your-deepseek-api-key"),
            )
            if not value or value == placeholder
        ]
        if missing:
            raise ValueError("Set " + " and ".join(missing) + " in the environment or .env.")

        channel_id = channel_setting("DISCORD_CHANNEL_ID") or channel_setting("ADVISOR_CHANNEL_ID")

        thinking = setting("DEEPSEEK_THINKING", "enabled").lower()
        if thinking not in {"enabled", "disabled"}:
            raise ValueError("DEEPSEEK_THINKING must be enabled or disabled.")
        effort = setting("DEEPSEEK_REASONING_EFFORT", "high").lower()
        if effort not in {"low", "medium", "high", "max"}:
            raise ValueError("DEEPSEEK_REASONING_EFFORT must be low, medium, high, or max.")
        try:
            max_tokens = int(setting("DEEPSEEK_MAX_TOKENS", "16000"))
        except ValueError:
            max_tokens = 0
        if not 1 <= max_tokens <= 393216:
            raise ValueError("DEEPSEEK_MAX_TOKENS must be an integer from 1 to 393216.")

        base_url = setting("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
        try:
            url = urlsplit(base_url)
            valid_url = (
                url.scheme == "https" and url.hostname
                and url.username is None and not url.query and not url.fragment
            )
            _ = url.port
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ValueError("DEEPSEEK_BASE_URL must be an HTTPS URL without credentials, a query, or a fragment.")

        log_level = setting("LOG_LEVEL", "INFO").upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL.")
        if setting("DEEPSEEK_MODEL", "deepseek-flash") != "deepseek-flash":
            raise ValueError("DEEPSEEK_MODEL must be deepseek-flash.")
        web_search = setting("DEEPSEEK_WEB_SEARCH", "enabled").lower()
        if web_search not in {"enabled", "disabled"}:
            raise ValueError("DEEPSEEK_WEB_SEARCH must be enabled or disabled.")
        safety = setting("DEEPSEEK_SAFETY_LEVEL", "high").lower()
        if safety not in {"low", "high"}:
            raise ValueError("DEEPSEEK_SAFETY_LEVEL must be low or high.")
        memory = Path(setting("MEMORY_DB_PATH", str(cls.memory_db_path))).expanduser()
        digest_enabled = setting("DIGEST_ENABLED", "enabled").lower()
        if digest_enabled not in {"enabled", "disabled"}:
            raise ValueError("DIGEST_ENABLED must be enabled or disabled.")
        digest_db = Path(setting("DIGEST_DB_PATH", str(cls.digest_db_path))).expanduser().resolve()
        if digest_db.is_dir() or digest_db in {
            memory.resolve(), cls.memory_db_path.resolve(), (Path.cwd() / ".env").resolve(),
        }:
            raise ValueError("DIGEST_DB_PATH must point to a separate SQLite database file.")
        return cls(token, key, "deepseek-flash", base_url, channel_id, effort,
                   thinking, max_tokens, log_level, web_search == "enabled", safety, memory,
                   digest_channel_id=channel_setting("DISCORD_DIGEST_CHANNEL_ID"),
                   digest_enabled=digest_enabled == "enabled",
                   digest_interval_hours=digest_duration("DIGEST_INTERVAL_HOURS", str(cls.digest_interval_hours)),
                   digest_initial_delay_seconds=digest_duration(
                       "DIGEST_INITIAL_DELAY_SECONDS", "60", allow_zero=True,
                   ),
                   digest_db_path=digest_db)
