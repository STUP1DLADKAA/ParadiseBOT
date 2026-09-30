"""System commands: help."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from functions.helpers import Colour

log = logging.getLogger(__name__)

HELP_BLURBS = {
    "ping": "am I alive (with the real latency)",
    "coinflip": "a very serious coin toss",
    "choose": "decisions, outsourced",
    "remind": "nudge you later in this channel",
    "poll": "reaction poll",
    "afk": "temporary AFK status",
    "translate": "translate a short text",
    "search": "clean web search results",
    "weather": "current weather and forecast",
    "convert": "units and live currency exchange rates",
    "time": "local time anywhere or for a member",
    "settimezone": "configure your personal timezone",
    "vc": "voice activity tracking and leaderboard card",
    "dictionary": "meaning, pronunciation, synonyms — as a card",
    "define": "same thing, shorter name",
    "recipe": "recipes from ingredients you have",
    "summarize": "recap recent chat in this channel",
}

HELP_GROUPS = {
    "🎲 fun": ("ping", "coinflip", "choose"),
    "🧰 useful": (
        "remind",
        "poll",
        "afk",
        "translate",
        "search",
        "weather",
        "convert",
        "time",
        "settimezone",
        "dictionary",
        "define",
        "recipe",
    ),
    "🎙️ voice": ("vc",),
    "🤖 ai": ("summarize",),
}


def cooldown():
    return app_commands.checks.cooldown(1, 5, key=lambda i: (i.guild_id, i.user.id))


class System(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # -- help ---------------------------------------------------------------

    @app_commands.command(description="What Paradise can actually do")
    @cooldown()
    async def help(self, interaction: discord.Interaction) -> None:
        registered = {command.name for command in self.bot.tree.get_commands()}
        embed = discord.Embed(
            title="🌴 Paradise",
            description=(
                "a small bot for a small server.\n"
                "mention me any time to talk — I answer when it makes sense."
            ),
            colour=Colour.ACCENT,
        )
        for group, names in HELP_GROUPS.items():
            lines = []
            for name in names:
                if name not in registered:
                    continue  # never advertise unregistered commands
                lines.append(f"`/{name}` — {HELP_BLURBS.get(name, '')}")
            if not lines:
                continue
            value = "\n".join(lines)
            if group == "🤖 ai":
                value += "\ntalk to me naturally by mentioning me."
            embed.add_field(name=group, value=value, inline=False)
        embed.set_footer(text="quiet by design · it watches, occasionally comments")
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(System(bot))
