"""Read app settings (other than LLM keys, see core/llm/select.py) from the environment."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from erranderpy.core.llm import ConfigError
from erranderpy.core.messages import SUPPORTED_LANGUAGES

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    timezone: ZoneInfo
    language: str
    db_path: Path

    @property
    def user_agents_dir(self) -> Path:
        """Agents the bot creates for this user: private, next to the database, git-ignored."""
        return self.db_path.parent / "agents"


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError("TELEGRAM_BOT_TOKEN is empty in .env. Get one from @BotFather on Telegram.")

    tz_name = env.get("TIMEZONE", "").strip() or "Europe/Istanbul"
    try:
        timezone = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"TIMEZONE '{tz_name}' is not a valid time zone (example: Europe/Istanbul).") from exc

    language = env.get("BOT_LANGUAGE", "").strip().lower() or "tr"
    if language not in SUPPORTED_LANGUAGES:
        raise ConfigError(f"BOT_LANGUAGE must be one of {', '.join(SUPPORTED_LANGUAGES)}, got '{language}'.")

    db_path = Path(env.get("DB_PATH", "").strip() or ROOT / "data" / "assistant.db")
    return Settings(token, timezone, language, db_path)
