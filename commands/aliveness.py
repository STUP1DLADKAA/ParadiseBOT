"""The aliveness watcher — posts one situation-aware line when the server goes quiet.

A cog around :class:`functions.aliveness.AlivenessEngine`:

* ``on_message`` feeds the engine ("a human spoke at …").
* a slow loop (:attr:`Alive.watch`) snapshots each guild and, when the verdict
  is "dead" or "everyone is in a voice call", asks the AI for one short
  in-character line about the actual numbers and names, then posts it to the
  configured channel. When the AI is down, a canned situational line is used
  instead — the bot stays alive either way.

Everything is defensive: a watcher that crashes must never take the bot down,
and a Discord outage must never spam the channel. The post cooldown lives in
the engine; this cog just obeys it.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

import discord
from discord.ext import commands, tasks

from functions.aliveness import (
    AlivenessEngine,
    AlivenessSignals,
    DEAD_REASON,
    EVERYONE_IN_VC_REASON,
)
from functions.helpers import shorten

log = logging.getLogger(__name__)

#: How long to wait for the AI when composing an aliveness line.
AI_TIMEOUT_SECONDS = 25.0

#: Hard ceiling for a posted line — two sentences, never a paragraph.
MAX_LINE_CHARS = 400

#: Both known verdicts (imported for tests and introspection).
REASONS = (DEAD_REASON, EVERYONE_IN_VC_REASON)


class Alive(commands.Cog):
    """Watches the server and speaks up when it goes quiet."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        config = getattr(bot, "config", None)
        aliveness_cfg = getattr(config, "aliveness", None)
        interval = max(30, int(getattr(aliveness_cfg, "interval", 150) or 150))
        presences = bool(
            getattr(getattr(bot, "intents", None), "presences", False)
        )
        self.config = aliveness_cfg
        self.engine = AlivenessEngine(
            aliveness_cfg, presences_enabled=presences
        )
        self.channel_id = int(getattr(aliveness_cfg, "channel_id", 0) or 0)
        try:
            self.watch.change_interval(seconds=interval)
        except Exception:  # noqa: BLE001 - interval tuning is best effort
            log.debug("Could not set the aliveness interval", exc_info=True)

    async def cog_load(self) -> None:
        if not bool(getattr(self.config, "enabled", True)):
            log.info("Aliveness watcher disabled by config")
            return
        self.watch.start()

    async def cog_unload(self) -> None:
        self.watch.cancel()

    # ------------------------------------------------------------------
    # The loop
    # ------------------------------------------------------------------

    @tasks.loop(seconds=150)
    async def watch(self) -> None:
        for guild in list(getattr(self.bot, "guilds", []) or []):
            try:
                await self._check_guild(guild)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one guild must never stop the rest
                log.warning("Aliveness check failed in %s", guild.id, exc_info=True)

    @watch.before_loop
    async def before_watch(self) -> None:
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:
            # Offline test harnesses load cogs without logging the client in.
            self.watch.cancel()

    @commands.Cog.listener("on_message")
    async def _track_human_chat(self, message: discord.Message) -> None:
        self.engine.note_message(message)

    # ------------------------------------------------------------------
    # One guild, one verdict
    # ------------------------------------------------------------------

    async def _check_guild(self, guild: discord.Guild) -> None:
        state_guild_id = int(getattr(guild, "id", 0) or 0)
        if not self._primed(state_guild_id):
            await self._prime_from_history(guild, state_guild_id)

        signals = self.engine.snapshot(guild)
        reason = self.engine.decision(signals)
        if reason is None:
            return

        channel = self._target_channel()
        if channel is None:
            return
        line = await self._compose(signals, reason)
        if not line:
            return
        try:
            await asyncio.wait_for(
                channel.send(
                    shorten(line, MAX_LINE_CHARS),
                    allowed_mentions=discord.AllowedMentions.none(),
                ),
                timeout=15,
            )
        except (discord.DiscordException, asyncio.TimeoutError):
            log.debug("Aliveness post failed", exc_info=True)

    def _primed(self, guild_id: int) -> bool:
        return self.engine.is_primed(guild_id)

    async def _prime_from_history(
        self, guild: discord.Guild, guild_id: int
    ) -> None:
        """Seed "last chat" from the target channel so a fresh start isn't 'dead'.

        Silence that began before the bot came online is invisible to
        ``on_message``. The engine also refuses to call a server dead before
        ``dead_after`` minutes of its own uptime, so this only refines the
        estimate — it can never make the bot shout at a lively server.
        """
        last_message_at = None
        channel = self._target_channel()
        if channel is not None:
            try:
                async for message in channel.history(limit=1):
                    if (
                        not getattr(message.author, "bot", False)
                        and message.created_at is not None
                    ):
                        age = (
                            discord.utils.utcnow() - message.created_at
                        ).total_seconds()
                        last_message_at = time.monotonic() - max(0.0, age)
            except Exception:  # noqa: BLE001 - priming is a nicety, not a need
                log.debug("Aliveness priming failed", exc_info=True)
        self.engine.prime(guild_id, last_message_at)

    def _target_channel(self) -> discord.abc.Messageable | None:
        if not self.channel_id:
            return None
        channel = self.bot.get_channel(self.channel_id)
        if channel is None:
            return None
        send = getattr(channel, "send", None)
        return channel if callable(send) else None

    async def _compose(self, signals: AlivenessSignals, reason: str) -> str:
        """One line for this moment: AI when possible, canned otherwise."""
        ai = getattr(self.bot, "ai", None)
        if ai is not None and getattr(ai, "available", False):
            try:
                prompt = self.engine.build_prompt(signals, reason)
                text = await asyncio.wait_for(
                    ai.improvise(prompt, max_tokens=180), timeout=AI_TIMEOUT_SECONDS
                )
            except Exception:  # noqa: BLE001 - the fallback always exists
                log.debug("Aliveness AI line failed; using fallback", exc_info=True)
                text = ""
            if text and str(text).strip():
                candidate = str(text).strip()
                # The AI is told not to mention anyone; if it tries anyway,
                # that line does not go out.
                if "<@" not in candidate and "@everyone" not in candidate:
                    return candidate
        return self._fallback(signals, reason)

    def _fallback(self, signals: AlivenessSignals, reason: str) -> str:
        try:
            return self.engine.fallback_line(signals, reason)
        except Exception:  # noqa: BLE001 - never post nothing by accident
            log.debug("Aliveness fallback failed", exc_info=True)
            return random.choice(
                (
                    "it's quiet in here. suspiciously quiet.",
                    "anyone alive? just me? cool.",
                )
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Alive(bot))
