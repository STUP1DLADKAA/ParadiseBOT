"""Useful commands: remind, poll, afk, translate, settimezone.

This cog also owns the shared engines for search, weather and conversion.
Those no longer have public slash commands — they are reached by mentioning
Paradise — but the engines stay here so there is exactly one implementation
of each and the assistant can reuse them.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

from functions.conversion import ConversionEngine
from functions.helpers import parse_options, shorten
from functions.search import AnswerEngine, SearchEngine
from functions.time import (
    CITY_TIMEZONES,
    get_current_time_for_zone,
    resolve_timezone,
)
from functions.translate import resolve_language, translate_text
__all__ = ["LANGUAGE_CODES"]  # re-exported for compatibility

from functions.translate import LANGUAGE_CODES  # noqa: F401
from functions.voice.database import VoiceDatabase
from functions.weather import OpenMeteoWeatherService

log = logging.getLogger(__name__)

POLL_EMOJIS = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟")

POPULAR_TIMEZONES = [
    "Asia/Kolkata", "Europe/London", "America/New_York", "America/Los_Angeles",
    "America/Chicago", "Europe/Paris", "Europe/Berlin", "Asia/Tokyo",
    "Asia/Dubai", "Asia/Singapore", "Australia/Sydney", "America/Toronto",
    "Asia/Hong_Kong", "UTC",
]


@dataclass(slots=True)
class AfkEntry:
    reason: str
    since: datetime


def cooldown(seconds: float = 5):
    return app_commands.checks.cooldown(1, seconds, key=lambda i: (i.guild_id, i.user.id))


async def timezone_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Autocomplete for timezone names and cities."""
    current_clean = current.strip().lower()
    choices: list[app_commands.Choice[str]] = []

    # Match popular first if empty
    if not current_clean:
        for tz in POPULAR_TIMEZONES[:15]:
            choices.append(app_commands.Choice(name=tz, value=tz))
        return choices

    # Check city matches
    for city, tz in CITY_TIMEZONES.items():
        if current_clean in city or current_clean in tz.lower():
            label = f"{city.title()} ({tz})"
            choices.append(app_commands.Choice(name=label[:100], value=tz))
            if len(choices) >= 20:
                return choices

    # Check standard timezone names
    for tz in POPULAR_TIMEZONES:
        if current_clean in tz.lower() and not any(c.value == tz for c in choices):
            choices.append(app_commands.Choice(name=tz, value=tz))
            if len(choices) >= 25:
                return choices

    return choices[:25]


class Utility(commands.Cog):
    MAX_REMINDERS = 200
    MAX_PER_USER = 5
    MAX_AFK = 1000

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.reminders: dict[int, asyncio.Task[None]] = {}
        self.reminder_owners: Counter[int] = Counter()
        self._next_reminder = 0
        self.search_engine = SearchEngine()
        # Search is a native feature. It builds a concise answer from its
        # retrieved evidence and never makes an AI call or a second search.
        self.answer_engine = AnswerEngine(self.search_engine)
        self.weather_service = OpenMeteoWeatherService()
        self.conversion_engine = ConversionEngine()
        self.db = VoiceDatabase()

    async def cog_unload(self) -> None:
        for task in self.reminders.values():
            task.cancel()
        # Release the shared search session so shutdown leaves no connector.
        try:
            await self.search_engine.close()
        except Exception:  # noqa: BLE001 - shutdown is best effort
            log.debug("Could not close the search session", exc_info=True)

    # -- retired public commands ------------------------------------------
    #
    # /search, /weather, /convert and /time are no longer registered. Their
    # capabilities live on and are reached by simply talking to the bot:
    #
    #   @Paradise search Minecraft 1.21      @Paradise weather in Delhi
    #   @Paradise 100 USD to INR             @Paradise time in Tokyo
    #
    # The engines below are the single implementation of each capability and
    # are used by the mention assistant (commands/assistant.py). Nothing was
    # duplicated or deleted — only the slash-command surface was removed.

    # -- settimezone ------------------------------------------------------

    @app_commands.command(description="Set your personal timezone for time lookups and reports")
    @app_commands.describe(timezone="Your timezone or city (e.g. Asia/Kolkata, London, EST)")
    @app_commands.autocomplete(timezone=timezone_autocomplete)
    @cooldown(3)
    async def settimezone(
        self,
        interaction: discord.Interaction,
        timezone: str,
    ) -> None:
        resolved = resolve_timezone(timezone)
        if not resolved:
            await interaction.response.send_message(
                f"Couldn't recognize timezone `{timezone}`. Try selecting one from the autocomplete list.",
                ephemeral=True,
            )
            return

        self.db.set_user_timezone(interaction.user.id, resolved)
        info = get_current_time_for_zone(resolved, label=interaction.user.display_name)
        await interaction.response.send_message(
            f"✅ Timezone set to **{resolved}** (`{info.utc_offset}`).\n"
            f"Your current local time is **{info.formatted_12h}** ({info.formatted_date}).\n"
            "-# Others can check your local time by asking me: "
            "`@Paradise what time is it for {}`".format(interaction.user.display_name),
            ephemeral=True,
        )

    # -- remind -----------------------------------------------------------

    @app_commands.command(description="Get reminded in this channel later")
    @app_commands.describe(
        minutes="1–10,080 minutes", message="What to remind you about"
    )
    @cooldown()
    async def remind(
        self,
        interaction: discord.Interaction,
        minutes: app_commands.Range[int, 1, 10_080],
        message: app_commands.Range[str, 1, 300],
    ) -> None:
        if interaction.channel_id is None:
            await interaction.response.send_message(
                "I need a channel for that.", ephemeral=True
            )
            return
        if (
            len(self.reminders) >= self.MAX_REMINDERS
            or self.reminder_owners[interaction.user.id] >= self.MAX_PER_USER
        ):
            await interaction.response.send_message(
                "too many reminders in the air right now. try again later.",
                ephemeral=True,
            )
            return

        self._next_reminder += 1
        reminder_id = self._next_reminder
        self.reminder_owners[interaction.user.id] += 1
        task = asyncio.create_task(
            self._deliver_reminder(
                reminder_id,
                interaction.user.id,
                interaction.channel_id,
                minutes,
                message,
            ),
            name=f"reminder-{reminder_id}",
        )
        self.reminders[reminder_id] = task
        due = discord.utils.utcnow() + timedelta(minutes=minutes)
        await interaction.response.send_message(
            f"got it. I'll remind you here {discord.utils.format_dt(due, 'R')}.",
            ephemeral=True,
        )

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

    # -- poll -------------------------------------------------------------

    @app_commands.command(description="Start a reaction poll")
    @app_commands.describe(
        question="The question", options="Separate 2–10 options with | or ,"
    )
    @cooldown(10)
    async def poll(
        self, interaction: discord.Interaction, question: str, options: str
    ) -> None:
        question = question.strip()
        choices = parse_options(options, limit=10)
        if not question or not 2 <= len(choices) <= 10:
            await interaction.response.send_message(
                "give me a question and 2–10 options, separated by `|` or `,`.",
                ephemeral=True,
            )
            return
        if len(question) > 200 or any(len(item) > 80 for item in choices):
            await interaction.response.send_message(
                "keep the question under 200 characters and each option under 80.",
                ephemeral=True,
            )
            return
        if not interaction.channel or not isinstance(
            interaction.channel, discord.abc.Messageable
        ):
            await interaction.response.send_message(
                "polls need a normal channel.", ephemeral=True
            )
            return

        await interaction.response.send_message("poll deployed.", ephemeral=True)
        lines = [f"📊 **{discord.utils.escape_markdown(question)}**", ""]
        lines.extend(
            f"{POLL_EMOJIS[i]} {discord.utils.escape_markdown(choice)}"
            for i, choice in enumerate(choices)
        )
        lines += ["", f"-# react below to vote · started by {interaction.user.display_name}"]
        try:
            poll_message = await interaction.channel.send(
                "\n".join(lines), allowed_mentions=discord.AllowedMentions.none()
            )
            for emoji in POLL_EMOJIS[: len(choices)]:
                await poll_message.add_reaction(emoji)
        except discord.DiscordException:
            log.warning("Poll creation failed", exc_info=True)
            await interaction.followup.send(
                "the poll fell over. permissions, probably.", ephemeral=True
            )

    # -- afk --------------------------------------------------------------

    @app_commands.command(description="Set a temporary AFK status")
    @app_commands.describe(reason="Optional short reason")
    @cooldown()
    async def afk(
        self, interaction: discord.Interaction, reason: str = "away for a bit"
    ) -> None:
        reason = reason.strip()[:120] or "away for a bit"
        afk: dict[tuple[int, int], AfkEntry] = self.bot.afk_states  # type: ignore[attr-defined]
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "AFK only really makes sense in a server.", ephemeral=True
            )
            return
        cutoff = datetime.now(UTC) - timedelta(days=7)
        stale = [key for key, entry in afk.items() if entry.since < cutoff]
        for key in stale:
            afk.pop(key, None)
        if (
            len(afk) >= self.MAX_AFK
            and (interaction.guild_id, interaction.user.id) not in afk
        ):
            await interaction.response.send_message(
                "the AFK cupboard is full. weird.", ephemeral=True
            )
            return
        afk[(interaction.guild_id, interaction.user.id)] = AfkEntry(
            reason, datetime.now(UTC)
        )
        await interaction.response.send_message(
            f"AFK set: {discord.utils.escape_markdown(reason)}\n"
            "-# I'll mention it if someone pings you, and clear it when you're back.",
            ephemeral=True,
        )

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
