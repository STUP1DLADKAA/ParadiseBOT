from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)
POLL_EMOJIS = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟")


@dataclass(slots=True)
class AfkEntry:
    reason: str
    since: datetime


def cooldown(seconds: float = 5):
    return app_commands.checks.cooldown(
        1, seconds, key=lambda i: (i.guild_id, i.user.id)
    )


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
        await interaction.response.send_message(
            f"got it. I'll remind you here in {minutes} minute{'s' if minutes != 1 else ''}.",
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
                        # Only the reminder owner may be pinged; mentions inside their text stay inert.
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

    @app_commands.command(description="Start a reaction poll")
    @app_commands.describe(
        question="The question", options="Separate 2–10 options with |"
    )
    @cooldown(10)
    async def poll(
        self, interaction: discord.Interaction, question: str, options: str
    ) -> None:
        choices = [item.strip() for item in options.split("|") if item.strip()]
        question = question.strip()
        if not question or not 2 <= len(choices) <= 10:
            await interaction.response.send_message(
                "give me a question and 2–10 options separated by `|`.", ephemeral=True
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
        lines = [f"**{discord.utils.escape_markdown(question)}**"]
        lines.extend(
            f"{POLL_EMOJIS[i]} {discord.utils.escape_markdown(choice)}"
            for i, choice in enumerate(choices)
        )
        try:
            poll_message = await interaction.channel.send(
                "\n\n".join(lines), allowed_mentions=discord.AllowedMentions.none()
            )
            for emoji in POLL_EMOJIS[: len(choices)]:
                await poll_message.add_reaction(emoji)
        except discord.DiscordException:
            log.warning("Poll creation failed", exc_info=True)
            await interaction.followup.send(
                "the poll fell over. permissions, probably.", ephemeral=True
            )

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
            f"AFK set: {discord.utils.escape_markdown(reason)}", ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Utility(bot))
