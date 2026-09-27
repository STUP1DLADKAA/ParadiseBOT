"""Fun commands: ping, 8ball, coinflip, choose."""

from __future__ import annotations

import asyncio
import logging
import math
import random

import discord
from discord import app_commands
from discord.ext import commands

from functions.helpers import parse_options

log = logging.getLogger(__name__)

PING_QUIPS = (
    "alive unfortunately.",
    "yep. still here.",
    "pong.",
    "I have no idea why you checked.",
    "alive 👍",
    "present, more or less.",
    "still online somehow.",
    "reporting for duty, loosely.",
)

BALL_ANSWERS = (
    "yeah, probably.",
    "absolutely not.",
    "ask again when the vibes improve.",
    "signs point to yes.",
    "I wouldn't bet on it.",
    "weirdly, yes.",
    "that's between you and the universe.",
    "give it a minute.",
    "nope.",
    "the council says maybe.",
    "outlook: suspicious.",
    "I have no idea, but now I'm curious.",
    "yes, and honestly? good for you.",
    "the orb is buffering. try later.",
)

COIN_FLAVOURS = (
    "",
    "\nthe coin has spoken.",
    "\nno take-backs.",
    "\nfate is real now.",
    "\nthat's just how it landed.",
    "\ndestiny is a flat disc, apparently.",
)


def build_ping_message(latency: float | None, quip: str | None = None) -> str:
    """Random quip plus the real, clearly labelled latency."""
    quip = quip or random.choice(PING_QUIPS)
    if latency is None or math.isnan(latency):
        return f"{quip}\n\nLatency: still measuring…"
    return f"{quip}\n\nLatency: {round(latency * 1000)}ms"


def flip_coin() -> str:
    return random.choice(("Heads", "Tails"))


def cooldown():
    return app_commands.checks.cooldown(1, 4, key=lambda i: (i.guild_id, i.user.id))


class Fun(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(description="Check if Paradise is still alive")
    @cooldown()
    async def ping(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            build_ping_message(self.bot.latency),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="8ball", description="Ask the questionably mystical orb")
    @app_commands.describe(question="The thing keeping you awake")
    @cooldown()
    async def eight_ball(self, interaction: discord.Interaction, question: str) -> None:
        question = question.strip()
        if not question:
            await interaction.response.send_message(
                "you do have to ask something.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"🎱 {random.choice(BALL_ANSWERS)}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(description="Make a deeply important binary decision")
    @cooldown()
    async def coinflip(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message("🪙 flipping…")
        try:
            await asyncio.sleep(0.7)
            await interaction.edit_original_response(content="🪙 • • •")
            await asyncio.sleep(0.7)
            side = flip_coin()
            await interaction.edit_original_response(
                content=f"🪙 **{side}**{random.choice(COIN_FLAVOURS)}"
            )
        except discord.DiscordException:
            # The animation is cosmetic; never crash over a failed edit.
            log.debug("Coinflip animation edit failed", exc_info=True)

    @app_commands.command(description="Let Paradise choose so you don't have to")
    @app_commands.describe(options="Separate choices with | or , (2–20)")
    @cooldown()
    async def choose(self, interaction: discord.Interaction, options: str) -> None:
        choices = parse_options(options)
        if not 2 <= len(choices) <= 20:
            await interaction.response.send_message(
                "give me 2–20 choices, separated by `|` or `,`.", ephemeral=True
            )
            return
        if any(len(item) > 100 for item in choices):
            await interaction.response.send_message(
                "one of those choices is an entire novel. keep each under 100 characters.",
                ephemeral=True,
            )
            return
        pick = random.choice(choices)
        await interaction.response.send_message(
            f"**{discord.utils.escape_markdown(pick)}**.\n\nthe council has spoken.",
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Fun(bot))
