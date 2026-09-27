"""Voice commands: /say."""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from functions.helpers import TTSError

log = logging.getLogger(__name__)


class Voice(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(description="Have Paradise speak a short message in voice")
    @app_commands.guild_only()
    @app_commands.describe(text="What to say (short — TTS limits apply)")
    @app_commands.checks.cooldown(1, 5, key=lambda i: (i.guild_id, i.user.id))
    async def say(
        self, interaction: discord.Interaction, text: app_commands.Range[str, 1, 400]
    ) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "that only works in a server.", ephemeral=True
            )
            return
        if not self.bot.music.configured:
            await interaction.response.send_message(
                "TTS needs a music node, and none is configured.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            item = await self.bot.tts.say(
                guild,
                member,
                text,
                channel=interaction.channel,
                interaction=interaction,
            )
        except TTSError as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        except Exception:
            log.exception("/say failed")
            await interaction.followup.send(
                "that broke on my end. it's been logged.", ephemeral=True
            )
            return

        # Wait for the clip to actually play so real errors surface here.
        try:
            await asyncio.wait_for(item.done.wait(), timeout=90)
        except asyncio.TimeoutError:
            await interaction.followup.send(
                "it's queued — I'll say it as soon as the line clears.", ephemeral=True
            )
            return
        if item.error:
            await interaction.followup.send(item.error, ephemeral=True)
            return
        await interaction.followup.send("🔊 said it.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Voice(bot))
