"""Collect bounded, permission-checked Discord history for an answer."""

import asyncio
import logging
from collections.abc import Callable
from datetime import timezone

import discord

logger = logging.getLogger(__name__)
PRIMARY_MESSAGES = 30
OTHER_MESSAGES = 15
OTHER_CHANNELS = 12
HISTORY_CHARS = 16_000
FETCH_TIMEOUT = 5

GUILD_HISTORY_TYPES = {
    discord.ChannelType.text, discord.ChannelType.news, discord.ChannelType.news_thread,
    discord.ChannelType.public_thread, discord.ChannelType.private_thread,
}


def _permissions(channel, bot, asker):
    try:
        permissions = [channel.permissions_for(member) for member in (bot, asker)]
    except discord.ClientException:
        # An uncached thread parent prevents a reliable permission check.
        return []
    if all(p.view_channel and p.read_message_history for p in permissions):
        return permissions
    return []


async def _read(channel, limit, bot_id, skip_id, redact, semaphore, private_members=()):
    lines = []
    try:
        async with semaphore:
            async with asyncio.timeout(FETCH_TIMEOUT):
                for member in private_members:
                    # Thread.permissions_for inherits parent permissions and does not
                    # check invitations. Verify membership before reading private chat.
                    await channel.fetch_member(member.id)

                name = "DM" if channel.type == discord.ChannelType.private else f"#{channel.name}"
                # Scan a bounded recent window so bot-heavy channels are not fully paginated.
                scan = min(150, max(limit * 4, 80))
                async for message in channel.history(limit=scan, oldest_first=False):
                    if message.id == skip_id or message.webhook_id is not None:
                        continue
                    if message.author.bot and message.author.id != bot_id:
                        continue
                    if not message.content.strip():
                        continue
                    when = message.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                    line = redact(f"[{when} | {name} | {message.author.display_name}] {message.content}")
                    lines.append(" ".join(line.split()))
                    if len(lines) == limit:
                        break
    except (discord.HTTPException, OSError, TimeoutError) as exc:
        logger.warning("Discord history unavailable for channel %s (%s)", channel.id, type(exc).__name__)
    return lines


async def collect_history(
    channel, guild, asker, bot_id: int, skip_id: int | None,
    redact: Callable[[str], str], semaphore: asyncio.Semaphore,
) -> str:
    if channel is None:
        return ""
    if guild is None:
        if channel.type != discord.ChannelType.private:
            return ""
        candidates = [(channel, PRIMARY_MESSAGES, ())]
    else:
        bot = guild.me
        if bot is None or not isinstance(asker, discord.Member) or asker.guild.id != guild.id:
            return ""
        # The asking channel can be an archived thread absent from guild.threads.
        channels = {item.id: item for item in (*guild.text_channels, *guild.threads)}
        channels.pop(channel.id, None)
        others = sorted(
            channels.values(), key=lambda item: item.last_message_id or item.id, reverse=True,
        )
        candidates = []
        other_count = 0
        for item in (channel, *others):
            if item.type not in GUILD_HISTORY_TYPES or item.guild.id != guild.id:
                continue
            permissions = _permissions(item, bot, asker)
            if not permissions:
                continue
            private_members = ()
            if item.type == discord.ChannelType.private_thread:
                private_members = tuple(
                    member for member, perms in zip((bot, asker), permissions) if not perms.manage_threads
                )
            limit = PRIMARY_MESSAGES if item.id == channel.id else OTHER_MESSAGES
            candidates.append((item, limit, private_members))
            other_count += item.id != channel.id
            if other_count == OTHER_CHANNELS:
                break

    windows = await asyncio.gather(*(
        _read(item, limit, bot_id, skip_id, redact, semaphore, private_members)
        for item, limit, private_members in candidates
    ))
    lines = []
    remaining = HISTORY_CHARS
    for window in windows:
        for line in window:
            if len(line) + bool(lines) > remaining:
                return "\n".join(lines)
            remaining -= len(line) + bool(lines)
            lines.append(line)
    return "\n".join(lines)
