"""Useful commands: remind, poll, afk, translate, search, weather, convert, time, settimezone."""

from __future__ import annotations

import asyncio
import io
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from functions.conversion import (
    ConversionEngine,
    build_conversion_embed,
    parse_conversion_query,
)
from functions.helpers import Colour, ParadiseError, parse_options, shorten
from functions.search import SearchEngine, SearchPaginationView, build_search_embed
from functions.time import (
    CITY_TIMEZONES,
    TZ_ABBREVIATIONS,
    build_time_embed,
    get_current_time_for_zone,
    resolve_timezone,
)
from functions.voice.database import VoiceDatabase
from functions.weather import OpenMeteoWeatherService, build_weather_embed, render_weather_card

log = logging.getLogger(__name__)

POLL_EMOJIS = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟")

LANGUAGE_CODES = {
    "hindi": "hi", "english": "en", "spanish": "es", "french": "fr",
    "german": "de", "japanese": "ja", "korean": "ko", "chinese": "zh-CN",
    "arabic": "ar", "russian": "ru", "portuguese": "pt", "italian": "it",
    "turkish": "tr", "urdu": "ur", "punjabi": "pa", "marathi": "mr",
    "tamil": "ta", "telugu": "te", "bengali": "bn", "gujarati": "gu",
    "kannada": "kn", "malayalam": "ml", "dutch": "nl", "greek": "el",
    "hebrew": "he", "persian": "fa", "indonesian": "id", "thai": "th",
    "vietnamese": "vi", "polish": "pl", "ukrainian": "uk", "swedish": "sv",
    "nepali": "ne", "sinhala": "si",
}

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


def resolve_language(raw: str) -> str | None:
    raw = raw.strip().lower()
    if not raw:
        return None
    if raw in LANGUAGE_CODES:
        return LANGUAGE_CODES[raw]
    if raw in LANGUAGE_CODES.values() or raw in ("zh-cn", "zh-tw", "zh"):
        return raw
    if len(raw) == 2 and raw.isalpha():
        return raw
    return None


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
        self.weather_service = OpenMeteoWeatherService()
        self.conversion_engine = ConversionEngine()
        self.db = VoiceDatabase()

    def cog_unload(self) -> None:
        for task in self.reminders.values():
            task.cancel()

    # -- search -----------------------------------------------------------

    @app_commands.command(description="Search the web cleanly")
    @app_commands.describe(query="What to search for")
    @cooldown(5)
    async def search(
        self,
        interaction: discord.Interaction,
        query: app_commands.Range[str, 1, 300],
    ) -> None:
        await interaction.response.defer()
        try:
            response = await self.search_engine.search(query, limit=8)
        except Exception as err:
            log.warning("Search failed: %s", err, exc_info=True)
            await interaction.followup.send(
                "the search engine is not responding right now. give it a moment."
            )
            return

        embed = build_search_embed(response, current_index=0)
        view = SearchPaginationView(response, author_id=interaction.user.id)
        msg = await interaction.followup.send(embed=embed, view=view)
        view.message = msg

    # -- weather ----------------------------------------------------------

    @app_commands.command(description="Check current weather and forecast for any location")
    @app_commands.describe(location="City, region, or country (e.g. Delhi, London, Tokyo)")
    @cooldown(5)
    async def weather(
        self,
        interaction: discord.Interaction,
        location: app_commands.Range[str, 1, 100],
    ) -> None:
        await interaction.response.defer()
        try:
            data = await self.weather_service.get_weather(location)
        except ParadiseError as err:
            await interaction.followup.send(str(err))
            return
        except Exception as err:
            log.warning("Weather fetch failed: %s", err, exc_info=True)
            await interaction.followup.send(
                "the weather service is unavailable right now. try again in a bit."
            )
            return

        # Rendering is isolated from the Discord event loop and has a defensive
        # text fallback so optional font/image failures never break the command.
        try:
            image = await asyncio.to_thread(render_weather_card, data)
            await interaction.followup.send(
                content=f"Weather for {data.location.name}",
                file=discord.File(io.BytesIO(image), filename="weather.png"),
            )
        except Exception as err:
            log.warning("Weather card rendering failed: %s", err, exc_info=True)
            await interaction.followup.send(embed=build_weather_embed(data))

    # -- convert ----------------------------------------------------------

    @app_commands.command(description="Convert units or live currency exchange rates")
    @app_commands.describe(
        query="Natural query, e.g. '10 km to miles', '100 USD to INR', '25 celsius to fahrenheit'",
        value="Optional direct number value",
        from_unit="Optional source unit/currency",
        to_unit="Optional target unit/currency",
    )
    @cooldown(3)
    async def convert(
        self,
        interaction: discord.Interaction,
        query: str | None = None,
        value: float | None = None,
        from_unit: str | None = None,
        to_unit: str | None = None,
    ) -> None:
        if query is None and (value is None or from_unit is None or to_unit is None):
            await interaction.response.send_message(
                "give me something to convert, e.g.:\n"
                "• `/convert query: 10 km to miles`\n"
                "• `/convert query: 100 USD to INR`\n"
                "• `/convert query: 25 celsius to fahrenheit`\n"
                "• `/convert query: 5 kg to lb`",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        try:
            if value is not None and from_unit is not None and to_unit is not None:
                result = await self.conversion_engine.convert(value, from_unit, to_unit)
            elif query:
                result = await self.conversion_engine.convert_query(query)
            else:
                raise ParadiseError("Please provide all conversion units.")
        except ParadiseError as err:
            await interaction.followup.send(str(err))
            return
        except Exception as err:
            log.warning("Conversion failed: %s", err, exc_info=True)
            await interaction.followup.send("couldn't perform that conversion.")
            return

        embed = build_conversion_embed(result)
        await interaction.followup.send(embed=embed)

    # -- time -------------------------------------------------------------

    @app_commands.command(description="Check current local time for a location or member")
    @app_commands.describe(
        location="City, timezone, or region (e.g. Delhi, London, New York)",
        member="Optional server member to check configured time for",
    )
    @app_commands.autocomplete(location=timezone_autocomplete)
    @cooldown(3)
    async def time(
        self,
        interaction: discord.Interaction,
        location: str | None = None,
        member: discord.Member | None = None,
    ) -> None:
        if member is not None:
            # Check configured timezone for member
            tz_str = self.db.get_user_timezone(member.id)
            if not tz_str:
                if member.id == interaction.user.id:
                    await interaction.response.send_message(
                        "You haven't set your timezone yet. Set it with `/settimezone <timezone>`.",
                        ephemeral=True,
                    )
                else:
                    await interaction.response.send_message(
                        f"**{member.display_name}** hasn't configured their timezone yet.\n"
                        "They can set it using `/settimezone`.",
                        ephemeral=True,
                    )
                return
            try:
                info = get_current_time_for_zone(tz_str, label=member.display_name)
            except ParadiseError as err:
                await interaction.response.send_message(str(err), ephemeral=True)
                return
            embed = build_time_embed(info, user_target=member)
            await interaction.response.send_message(embed=embed)
            return

        if not location:
            # If no location or member provided, check if the caller has set their own timezone
            user_tz = self.db.get_user_timezone(interaction.user.id)
            if user_tz:
                info = get_current_time_for_zone(user_tz, label=interaction.user.display_name)
                embed = build_time_embed(info, user_target=interaction.user)
                await interaction.response.send_message(embed=embed)
                return
            await interaction.response.send_message(
                "provide a location, timezone, or @member (e.g. `/time Delhi`, `/time London`, `/time @User`).",
                ephemeral=True,
            )
            return

        resolved_tz = resolve_timezone(location)
        if not resolved_tz:
            await interaction.response.send_message(
                f"I don't recognize the timezone or location **{location}**.\n"
                "Try a major city name like `Delhi`, `London`, `Tokyo`, `New York`, or an IANA timezone like `Asia/Kolkata`.",
                ephemeral=True,
            )
            return

        try:
            info = get_current_time_for_zone(resolved_tz, label=location.title())
        except ParadiseError as err:
            await interaction.response.send_message(str(err), ephemeral=True)
            return

        embed = build_time_embed(info)
        await interaction.response.send_message(embed=embed)

    # -- settimezone ------------------------------------------------------

    @app_commands.command(description="Set your personal timezone for /time and reports")
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
            "-# Other members can now check your local time with `/time member:@{}`".format(
                interaction.user.display_name
            ),
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
        params = {
            "client": "gtx",
            "sl": source_code,
            "tl": target_code,
            "dt": "t",
            "q": text,
        }
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as session:
                async with session.get(
                    "https://translate.googleapis.com/translate_a/single", params=params
                ) as response:
                    if response.status != 200:
                        raise aiohttp.ClientResponseError(
                            response.request_info, (), status=response.status
                        )
                    data = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            log.debug("Translation failed", exc_info=True)
            await interaction.followup.send(
                "the translation service isn't responding right now."
            )
            return

        try:
            translated = "".join(part[0] for part in data[0] if part and part[0])
            detected = data[2] if len(data) > 2 and isinstance(data[2], str) else source_code
        except (TypeError, IndexError):
            await interaction.followup.send(
                "that translation came back weird. try again?"
            )
            return
        if not translated:
            await interaction.followup.send("couldn't translate that one.")
            return
        await interaction.followup.send(
            f"> {shorten(translated, 900)}\n\n-# {detected} → {target_code} · Google Translate"
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Utility(bot))
