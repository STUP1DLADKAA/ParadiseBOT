"""Useful commands: remind, poll, afk, translate, define."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from functions.helpers import parse_options, shorten

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


class Utility(commands.Cog):
    MAX_REMINDERS = 200
    MAX_PER_USER = 5
    MAX_AFK = 1000

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.reminders: dict[int, asyncio.Task[None]] = {}
        self.reminder_owners: Counter[int] = Counter()
        self._next_reminder = 0

    def cog_unload(self) -> None:
        for task in self.reminders.values():
            task.cancel()

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
        # Old entries are disposable; AFK state is deliberately temporary.
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

    # -- define ------------------------------------------------------------

    @app_commands.command(description="Look up what a word means")
    @app_commands.describe(word="The word to define")
    @cooldown()
    async def define(self, interaction: discord.Interaction, word: str) -> None:
        word = word.strip()[:60]
        if not word or " " in word:
            await interaction.response.send_message(
                "one word at a time. no spaces.", ephemeral=True
            )
            return
        await interaction.response.defer()
        url = f"https://api.dictionaryapi.dev/api/v2/entries/en/{word}"
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as session:
                async with session.get(url) as response:
                    if response.status == 404:
                        await interaction.followup.send(
                            f"no dictionary entry for **{word}**. might not be a real word."
                        )
                        return
                    if response.status != 200:
                        raise aiohttp.ClientResponseError(
                            response.request_info, (), status=response.status
                        )
                    data = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            log.debug("Dictionary lookup failed", exc_info=True)
            await interaction.followup.send(
                "the dictionary isn't responding right now."
            )
            return

        try:
            entry = data[0]
            phonetic = entry.get("phonetic") or next(
                (p.get("text") for p in entry.get("phonetics", []) if p.get("text")), ""
            )
            lines = [f"**{entry.get('word', word)}**{f' {phonetic}' if phonetic else ''}", ""]
            for meaning in entry.get("meanings", [])[:2]:
                part = meaning.get("partOfSpeech", "")
                definition = meaning.get("definitions", [{}])[0]
                lines.append(f"*{part}* — {definition.get('definition', '')}")
                example = definition.get("example")
                if example:
                    lines.append(f"> {shorten(example, 140)}")
                lines.append("")
            text = "\n".join(lines).strip() or "no definitions came back for that."
        except (TypeError, IndexError, AttributeError):
            await interaction.followup.send("that came back weird. try again?")
            return
        await interaction.followup.send(shorten(text, 1900))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Utility(bot))
