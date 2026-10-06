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
    "example": "look up any movie, anime or series — live, no keys",
    "trending": "what's trending / popular / top-rated right now",
    "upcoming": "upcoming releases with honest dates",
    "recommend": "personal picks with reasons ('like Interstellar')",
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
                "part search engine, part toolbox, part movie-buff.\n"
                "**the easiest way: just mention me.** tools answer first, "
                "AI only when something genuinely needs it."
            ),
            colour=Colour.ACCENT,
        )
        embed.add_field(
            name="💬 mention me like…",
            value=(
                "`@Paradise define photosynthesis`\n"
                "`@Paradise what's 15% of 500` · `@Paradise 100 USD to INR`\n"
                "`@Paradise weather in Delhi` · `@Paradise time in Tokyo`\n"
                "`@Paradise latest Minecraft news` · `@Paradise search Mars photos`\n"
                "`@Paradise recommend a movie like Interstellar`\n"
                "`@Paradise translate hello into Japanese`"
            ),
            inline=False,
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
                value += "\nmention me to talk naturally (needs an AI key set by the owner)."
            embed.add_field(name=group, value=value, inline=False)
        embed.set_footer(text="quiet by design · it watches, occasionally comments")
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(System(bot))
