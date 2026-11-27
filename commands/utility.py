"""Useful commands: translate.

This cog also owns the shared engines for weather and conversion. Those no
longer have public slash commands — they are reached by mentioning Paradise —
but the engines stay here so there is exactly one implementation of each and
the assistant can reuse them. (Search-shaped requests are no longer answered
by a web engine at all: the assistant forwards them straight to the AI.)
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from functions.conversion import ConversionEngine
from functions.helpers import shorten
from functions.translate import resolve_language, translate_text
__all__ = ["LANGUAGE_CODES"]  # re-exported for compatibility

from functions.translate import LANGUAGE_CODES  # noqa: F401
from functions.weather import OpenMeteoWeatherService

log = logging.getLogger(__name__)

POLL_EMOJIS = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟")


def cooldown(seconds: float = 5):
    return app_commands.checks.cooldown(1, seconds, key=lambda i: (i.guild_id, i.user.id))


class Utility(commands.Cog):
    MAX_REMINDERS = 200
    MAX_PER_USER = 5

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.reminders: dict[int, asyncio.Task[None]] = {}
        self.reminder_owners: Counter[int] = Counter()
        self._next_reminder = 0
        self.weather_service = OpenMeteoWeatherService()
        self.conversion_engine = ConversionEngine()

    async def cog_unload(self) -> None:
        tasks = tuple(self.reminders.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # -- retired public commands ------------------------------------------
    #
    # /search, /weather, /convert and /time are no longer registered. Their
    # capabilities live on and are reached by simply talking to the bot:
    #
    #   @Paradise search Minecraft 1.21      @Paradise weather in Delhi
    #   @Paradise 100 USD to INR             @Paradise time in Tokyo
    #
    # "search …" (and any question shaped like one) is forwarded straight to
    # the AI by the mention assistant — no web engine is contacted. The
    # other engines below are the single implementation of each capability
    # and are used by the assistant; only the slash-command surface is gone.

    # -- remind -----------------------------------------------------------

    async def create_reminder(
        self, user_id: int, channel_id: int, minutes: int, text: str
    ) -> tuple[bool, str]:
        """Schedule one reminder. Shared by /remind and the mention router.

        Returns (okay, note) — the note is already phrased for chat.
        """
        text = " ".join(str(text or "").split()).strip()[:300]
        if not text:
            return False, "remind you *about* what?"
        minutes = max(1, min(int(minutes), 10_080))
        if (
            len(self.reminders) >= self.MAX_REMINDERS
            or self.reminder_owners[user_id] >= self.MAX_PER_USER
        ):
            return False, "too many reminders in the air right now. try again later."
        self._next_reminder += 1
        reminder_id = self._next_reminder
        self.reminder_owners[user_id] += 1
        task = asyncio.create_task(
            self._deliver_reminder(reminder_id, user_id, channel_id, minutes, text),
            name=f"reminder-{reminder_id}",
        )
        self.reminders[reminder_id] = task
        due = discord.utils.utcnow() + timedelta(minutes=minutes)
        return True, f"got it. I'll remind you here {discord.utils.format_dt(due, 'R')}."

    async def deploy_poll(
        self, channel, question: str, choices: list[str], starter: str
    ) -> bool:
        """Post a reaction poll. Shared by /poll and the mention router."""
        if not isinstance(channel, discord.abc.Messageable):
            return False
        question = str(question or "").strip()[:200]
        choices = [str(choice).strip()[:80] for choice in choices]
        if not question or not 2 <= len(choices) <= 10:
            return False
        lines = [f"📊 **{discord.utils.escape_markdown(question)}**", ""]
        lines.extend(
            f"{POLL_EMOJIS[i]} {discord.utils.escape_markdown(choice)}"
            for i, choice in enumerate(choices)
        )
        lines += ["", f"-# react below to vote · started by {starter}"]
        try:
            poll_message = await channel.send(
                "\n".join(lines), allowed_mentions=discord.AllowedMentions.none()
            )
            for emoji in POLL_EMOJIS[: len(choices)]:
                await poll_message.add_reaction(emoji)
            return True
        except discord.DiscordException:
            log.warning("Poll creation failed", exc_info=True)
            return False

    async def _deliver_reminder(
        self, reminder_id: int, user_id: int, channel_id: int, minutes: int, text: str
    ) -> None:
        try:
            await asyncio.sleep(minutes * 60)
            channel = self.bot.get_channel(channel_id)
            if channel and isinstance(channel, discord.abc.Messageable):
                await asyncio.wait_for(
                    channel.send(
                        f"<@{user_id}> reminder: {text}",
                        # Only the reminder owner may be pinged.
                        allowed_mentions=discord.AllowedMentions(
                            users=[discord.Object(id=user_id)],
                            roles=False,
                            everyone=False,
                        ),
                    ),
                    timeout=15,
                )
        except asyncio.CancelledError:
            raise
        except (discord.DiscordException, asyncio.TimeoutError):
            log.warning("Could not deliver reminder %s", reminder_id, exc_info=True)
        finally:
            self.reminders.pop(reminder_id, None)
            self.reminder_owners[user_id] -= 1
            if self.reminder_owners[user_id] <= 0:
                del self.reminder_owners[user_id]

    # -- translate ---------------------------------------------------------

    @app_commands.command(description="Translate a short piece of text")
    @app_commands.describe(
        text="Text to translate (max 800 characters)",
        target="Target language, e.g. hindi, english, es",
        source="Source language (default: auto-detect)",
    )
    @cooldown()
    async def translate(
        self,
        interaction: discord.Interaction,
        text: app_commands.Range[str, 1, 800],
        target: app_commands.Range[str, 1, 40],
        source: app_commands.Range[str, 1, 40] = "auto",
    ) -> None:
        target_code = resolve_language(target)
        if target_code is None:
            await interaction.response.send_message(
                "I don't know that language. try `hindi`, `english`, `spanish`, "
                "`japanese`… or a code like `hi` / `en`.",
                ephemeral=True,
            )
            return
        source_code = resolve_language(source) or "auto"

        await interaction.response.defer()
        result = await translate_text(text, target_code, source_code)
        if result is None:
            await interaction.followup.send(
                "the translation service isn't responding right now."
            )
            return
        await interaction.followup.send(
            f"> {shorten(result.text, 900)}\n\n-# {result.source} → {target_code} · Google Translate"
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Utility(bot))
