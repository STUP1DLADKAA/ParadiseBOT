"""AI commands: /summarize."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from functions.helpers import ParadiseError, shorten

log = logging.getLogger(__name__)

MAX_SUMMARY_MESSAGES = 80
MAX_SUMMARY_CHARS = 6000


class AI(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(description="Summarize recent conversation in this channel")
    @app_commands.guild_only()
    @app_commands.describe(
        count="How many recent messages to include (10–80, default 40)"
    )
    @app_commands.checks.cooldown(1, 30, key=lambda i: (i.guild_id, i.user.id))
    async def summarize(
        self,
        interaction: discord.Interaction,
        count: app_commands.Range[int, 10, MAX_SUMMARY_MESSAGES] = 40,
    ) -> None:
        ai = self.bot.ai
        if not ai.available:
            await interaction.response.send_message(
                "the AI isn't configured right now, so no summaries either. "
                "someone needs to set AI_API_KEY.",
                ephemeral=True,
            )
            return
        channel = interaction.channel
        if channel is None or not isinstance(channel, discord.abc.Messageable):
            await interaction.response.send_message(
                "I need a normal text channel for that.", ephemeral=True
            )
            return

        await interaction.response.defer()
        try:
            messages = []
            async for message in channel.history(limit=count * 3):
                if message.author.bot or not message.content.strip():
                    continue
                messages.append(message)
                if len(messages) >= count:
                    break
        except discord.DiscordException:
            log.exception("Could not read channel history")
            await interaction.followup.send(
                "I couldn't read this channel's history — permissions, probably.",
                ephemeral=True,
            )
            return

        if len(messages) < 3:
            await interaction.followup.send(
                "there isn't enough recent conversation here to summarize.",
                ephemeral=True,
            )
            return

        # Oldest first, bounded.
        messages.reverse()
        lines = []
        total = 0
        for message in messages:
            line = f"{message.author.display_name}: {message.content[:220]}"
            if total + len(line) > MAX_SUMMARY_CHARS:
                lines.append("(older messages trimmed)")
                break
            lines.append(line)
            total += len(line) + 1

        try:
            summary = await ai.summarize(lines, getattr(channel, "name", "this channel"))
        except ParadiseError as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        except Exception:
            log.exception("/summarize failed")
            await interaction.followup.send(
                "that broke on my end. it's been logged.", ephemeral=True
            )
            return

        header = f"📋 **recap of the last {len(messages)} messages in #{getattr(channel, 'name', 'here')}:**"
        await interaction.followup.send(
            f"{header}\n\n{shorten(summary, 1600)}"
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AI(bot))
