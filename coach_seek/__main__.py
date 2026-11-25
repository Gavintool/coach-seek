"""Run Coach Seek with python -m coach_seek."""

import asyncio
import logging
import sqlite3

import discord
from openai import AsyncOpenAI

from .advisor import Advisor
from .bot import CoachSeek
from .config import Config
from .memory import MemoryStore


async def run(config: Config) -> None:
    try:
        memory = MemoryStore(config.memory_db_path)
    except (OSError, sqlite3.Error, ValueError):
        raise SystemExit("Coach Seek could not initialize memory. Check MEMORY_DB_PATH and the database schema.") from None
    async with AsyncOpenAI(
        api_key=config.deepseek_api_key,
        base_url=config.base_url,
        timeout=240.0,
        max_retries=0,
    ) as client:
        async with CoachSeek(Advisor(client, config), config.channel_id, memory) as bot:
            await bot.start(config.discord_token)


def main() -> None:
    try:
        config = Config.from_env()
    except ValueError as exc:
        raise SystemExit(f"Coach Seek configuration error: {exc}") from None

    logging.basicConfig(level=config.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # SDK debug logging may include request bodies and upstream diagnostics.
    for name in ("openai", "httpx", "httpx2", "httpcore", "httpcore2"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    logging.getLogger("discord").setLevel(logging.INFO)

    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        pass
    except discord.LoginFailure:
        raise SystemExit("Coach Seek could not sign in. Check DISCORD_TOKEN.") from None
    except discord.PrivilegedIntentsRequired:
        raise SystemExit("Coach Seek requires Message Content Intent in the Discord Developer Portal.") from None
    except discord.HTTPException as exc:
        raise SystemExit(f"Coach Seek startup failed (Discord HTTP {exc.status}, code {exc.code}).") from None


if __name__ == "__main__":
    main()
