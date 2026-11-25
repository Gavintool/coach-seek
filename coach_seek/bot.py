"""Discord message and slash command handlers for Coach Seek."""

import asyncio
import logging
import re
import sqlite3
import time
from collections.abc import Iterator

import discord
from discord import app_commands

from .advisor import Advisor
from .digest import DigestStore
from .history import collect_history
from .memory import MAX_FACT, MemoryStore

logger = logging.getLogger(__name__)
DISCORD_LIMIT = 2000
SENTENCE_BREAK = re.compile(r"(?<=[.!?。！？])\s+")


def digest_channel(guild: discord.Guild, override_id: int | None) -> discord.TextChannel | None:
    general = None
    for channel in guild.text_channels:
        if channel.id == override_id:
            return channel
        if channel.name.casefold() == "general" and (
            general is None or (general.name != "general" and channel.name == "general")
        ):
            general = channel
    return general


def split_answer(answer: str) -> Iterator[str]:
    """Preserve the answer verbatim across Discord's message boundaries."""
    while answer:
        # Counting UTF-16 units also keeps emoji-heavy chunks within the limit.
        size = 0
        end = 0
        for char in answer:
            size += 2 if ord(char) > 0xFFFF else 1
            if size > DISCORD_LIMIT:
                break
            end += 1
        if end == len(answer):
            yield answer
            return

        window = answer[:end]
        midpoint = end // 2
        paragraph = window.rfind("\n\n", midpoint)
        newline = window.rfind("\n", midpoint)
        sentence = max((match.end() for match in SENTENCE_BREAK.finditer(window, midpoint)), default=0)
        space = window.rfind(" ", midpoint)
        cut = (
            paragraph + 2 if paragraph >= 0 else
            newline + 1 if newline >= 0 else
            sentence or (space + 1 if space >= 0 else end)
        )
        yield answer[:cut]
        answer = answer[cut:]


class CoachSeek(discord.Client):
    def __init__(self, advisor: Advisor, channel_id: int | None, memory: MemoryStore) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.dm_messages = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.advisor = advisor
        self.channel_id = channel_id
        self.memory = memory
        self.history_fetches = asyncio.Semaphore(3)
        self.extractions: set[asyncio.Task[None]] = set()
        self.digest_store = DigestStore(advisor.config.digest_db_path)
        self.digest_task: asyncio.Task[None] | None = None
        self.tree = app_commands.CommandTree(self)
        self.tree.add_command(app_commands.Command(
            name="ask", description="Ask Coach Seek a robotics question.", callback=self.ask,
        ))
        for name, description, callback in (
            ("remember", "Save a durable team robotics fact in this server.", self.remember),
            ("forget", "Remove server memories by ID or text.", self.forget),
            ("memories", "Show the most recent team memory entries.", self.memories),
        ):
            self.tree.add_command(app_commands.Command(
                name=name, description=description, callback=callback,
            ))
        self.tree.on_error = self.on_command_error

    async def setup_hook(self) -> None:
        await self.tree.sync()
        logger.info("Synced Coach Seek's slash commands")
        if self.advisor.config.digest_enabled and self.digest_task is None:
            self.digest_task = asyncio.create_task(self._digest_loop())

    async def _digest_loop(self) -> None:
        config = self.advisor.config
        interval = config.digest_interval_hours * 3600
        await self.wait_until_ready()
        await asyncio.sleep(config.digest_initial_delay_seconds)
        while True:
            delay = interval
            try:
                last_run = await asyncio.to_thread(self.digest_store.last_run)
                remaining = last_run + interval - time.time() if last_run is not None else 0
                if remaining > 0:
                    logger.info("Skipping FTC digest because the last run is recent; next check in %.0f seconds",
                                remaining)
                    delay = remaining
                else:
                    await self._run_digest()
            except Exception as exc:
                logger.warning("FTC digest failed (%s)", type(exc).__name__)
            await asyncio.sleep(delay)

    async def _run_digest(self) -> None:
        config = self.advisor.config
        if not config.web_search:
            logger.info("Skipping FTC digest because DeepSeek web search is disabled")
            return
        if not self.guilds:
            logger.info("Skipping FTC digest because Coach Seek is in no servers")
            return
        channels = []
        for guild in self.guilds:
            channel = digest_channel(guild, config.digest_channel_id)
            if channel is None:
                logger.info("Skipping FTC digest in guild %s because no digest channel was found", guild.id)
                continue
            if guild.me is None:
                logger.info("Skipping FTC digest in guild %s because bot permissions are unavailable", guild.id)
                continue
            permissions = channel.permissions_for(guild.me)
            if not (permissions.view_channel and permissions.send_messages):
                logger.info("Skipping FTC digest in guild %s because the channel is inaccessible", guild.id)
                continue
            channels.append(channel)
        if not channels:
            return

        candidates = await self.advisor.research_digest()
        items = await asyncio.to_thread(self.digest_store.unseen, candidates)
        if not items:
            logger.info("FTC digest found nothing new to post")
            await asyncio.to_thread(self.digest_store.record_run, time.time())
            return

        lines = ["**FTC BIOBUZZ digest**"]
        for item in items:
            title = discord.utils.escape_markdown(item.title)
            summary = discord.utils.escape_markdown(item.summary)
            lines.append(f"**{item.category} · {title}** — {summary}\n<{item.url}>")
        chunks = list(split_answer("\n\n".join(lines)))
        posted = False
        for channel in channels:
            try:
                for chunk in chunks:
                    await channel.send(chunk, mention_author=False, allowed_mentions=self.allowed_mentions)
                posted = True
            except discord.HTTPException as exc:
                logger.warning("FTC digest send failed in channel %s (HTTP %d, code %d)",
                               channel.id, exc.status, exc.code)
        if posted:
            await asyncio.to_thread(self.digest_store.mark_seen, items)
        else:
            logger.warning("FTC digest was not delivered to any channel")
        await asyncio.to_thread(self.digest_store.record_run, time.time())

    async def on_ready(self) -> None:
        logger.info("Coach Seek is connected (Discord user ID %s)", self.user.id if self.user else "unknown")

    def _mention_pattern(self) -> re.Pattern[str]:
        assert self.user is not None
        return re.compile(rf"<@!?{self.user.id}>")

    def _question_from_message(self, content: str, mentioned: bool) -> str:
        mention = self._mention_pattern()
        if not mentioned:
            return mention.sub("", content).strip()
        match = mention.search(content)
        if match is None:
            return mention.sub("", content).strip()
        after = mention.sub("", content[match.end():]).strip()
        if after:
            return after
        # Mentions at the end still keep the preceding question text.
        return mention.sub("", content).strip()

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.webhook_id or self.user is None:
            return

        guild_id = message.guild.id if message.guild is not None else None
        # Inspect explicit mention syntax so a reply ping alone does not trigger the bot.
        mentioned = self._mention_pattern().search(message.content) is not None
        if guild_id is not None and not mentioned and message.channel.id != self.channel_id:
            return

        question = self._question_from_message(message.content, mentioned)
        if not question:
            return

        try:
            async with message.channel.typing():
                history = await self._history(message.channel, message.guild, message.author, message.id)
                answer, revision = await self._answer(question, guild_id, history)
            chunks = iter(split_answer(answer))
            await message.reply(next(chunks), mention_author=False)
            for chunk in chunks:
                await message.channel.send(chunk)
            if guild_id is not None:
                self._schedule_extraction(guild_id, question, revision)
        except discord.HTTPException as exc:
            logger.warning("Discord message operation failed (HTTP %d, code %d)", exc.status, exc.code)

    @app_commands.describe(question="Your question for Coach Seek")
    @app_commands.allowed_contexts(guilds=True, dms=True)
    async def ask(self, interaction: discord.Interaction, question: str) -> None:
        await interaction.response.defer(thinking=True)
        question = question.strip()
        if not question:
            await interaction.edit_original_response(content="Enter a question for Coach Seek.")
            return
        history = ""
        if interaction.guild_id is None or interaction.guild is not None:
            history = await self._history(interaction.channel, interaction.guild, interaction.user)
        answer, revision = await self._answer(question, interaction.guild_id, history)
        chunks = iter(split_answer(answer))
        await interaction.edit_original_response(
            content=next(chunks), allowed_mentions=self.allowed_mentions,
        )
        for chunk in chunks:
            await interaction.followup.send(chunk, allowed_mentions=self.allowed_mentions)

        self._schedule_extraction(interaction.guild_id, question, revision)

    async def _history(self, channel, guild, asker, skip_id: int | None = None) -> str:
        if self.user is None:
            return ""
        return await collect_history(
            channel, guild, asker, self.user.id, skip_id, self.advisor.redact, self.history_fetches,
        )

    async def _answer(self, question: str, guild_id: int | None, history: str) -> tuple[str, int | None]:
        facts, revision = [], None
        if guild_id is not None:
            try:
                revision = await asyncio.to_thread(self.memory.revision, guild_id)
                facts = await asyncio.to_thread(self.memory.relevant, guild_id, question)
            except sqlite3.Error:
                logger.warning("Could not read guild memory")
        return await self.advisor.answer(question, facts, history=history), revision

    def _schedule_extraction(
        self, guild_id: int | None, question: str, revision: int | None,
    ) -> None:
        if guild_id is None or revision is None or len(self.extractions) >= 4:
            return
        task = asyncio.create_task(self._extract(guild_id, question, revision))
        self.extractions.add(task)
        task.add_done_callback(self.extractions.discard)

    async def _extract(self, guild_id: int, question: str, revision: int) -> None:
        try:
            facts = await self.advisor.extract_facts(question)
            if facts:
                await asyncio.to_thread(self.memory.remember, guild_id, facts, revision)
        except Exception as exc:
            logger.warning("Memory extraction failed (%s)", type(exc).__name__)

    async def close(self) -> None:
        tasks = tuple(self.extractions)
        if self.digest_task is not None:
            tasks += (self.digest_task,)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await super().close()

    @app_commands.describe(fact="A team robotics fact to share with this server")
    @app_commands.guild_only()
    async def remember(self, interaction: discord.Interaction, fact: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        assert interaction.guild_id is not None
        ids = []
        if self.advisor.redact(fact) == fact:
            ids = await asyncio.to_thread(self.memory.remember, interaction.guild_id, [fact])
        reply = (
            f"Saved as memory #{ids[0]}." if ids else
            f"Enter a team robotics fact of 1–{MAX_FACT} characters without credentials."
        )
        await interaction.edit_original_response(content=reply)

    @app_commands.describe(query="A memory ID (such as #12) or case-insensitive text to remove")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def forget(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        assert interaction.guild_id is not None
        ids = await asyncio.to_thread(self.memory.forget, interaction.guild_id, query)
        examples = ", ".join(f"#{memory_id}" for memory_id in ids[:15])
        if len(ids) > 15:
            examples += ", …"
        reply = f"Removed {len(ids)} matching memories ({examples})." if ids else "No matching memories."
        await interaction.edit_original_response(content=reply)

    @app_commands.guild_only()
    async def memories(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        assert interaction.guild_id is not None
        rows = await asyncio.to_thread(self.memory.recent, interaction.guild_id)
        reply = "\n".join(f"#{memory_id} · {fact[:139]}{'…' if len(fact) > 139 else ''}"
                          for memory_id, fact in rows)
        await interaction.edit_original_response(content=reply or "No memories saved in this server.")

    async def on_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("You need Manage Server to remove memories.", ephemeral=True)
            return
        cause = getattr(error, "original", error)
        logger.error("Coach Seek's slash command failed (%s)", type(cause).__name__)
        message = "Coach Seek couldn't complete this request. Please try again."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException as exc:
            logger.warning("Could not report the slash command error (HTTP %d)", exc.status)

    async def on_error(self, event_method: str, *args: object, **kwargs: object) -> None:
        # Exceptions from events can contain user content or upstream response bodies.
        logger.error("Coach Seek encountered an error in %s", event_method)
