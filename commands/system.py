"""System commands: help."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from functions.helpers import Colour

log = logging.getLogger(__name__)

# Only commands that are actually registered belong here; /help filters
# against the live tree, and the ping assistant covers everything else.
HELP_BLURBS = {
    "ping": "am I alive (with the real latency)",
    "coinflip": "a very serious coin toss",
    "choose": "decisions, outsourced",
    "remind": "nudge you later in this channel",
    "poll": "reaction poll",
    "afk": "temporary AFK status",
    "translate": "translate a short text",
    "settimezone": "configure your personal timezone",
    "vc": "voice activity tracking and leaderboard card",
    "clip": "save a recent voice clip via DM",
    "edit": "adjust voice activity time for a user (authorized user only)",
    "minigames": "automated Paradise games and leaderboard",
    "recipe": "recipes from ingredients you have",
    "summarize": "recap recent chat in this channel",
    "voicewake": "spoken wake word status, pause and resume",
    "help": "this list",
}

HELP_GROUPS = {
    "🎲 fun": ("ping", "coinflip", "choose"),
    "🧰 useful": ("remind", "poll", "afk", "translate", "settimezone", "recipe"),
    "🎙️ voice": ("vc", "clip", "edit"),
    "🎙 listening": ("voicewake",),
    "🎲 community": ("minigames",),
    "🤖 ai": ("summarize",),
}

#: Capabilities that no longer have a slash command. ``/help`` advertises
#: the sentence that reaches each one instead.
MENTION_CAPABILITIES = (
    ("weather", "@Paradise weather in Delhi"),
    ("search", "@Paradise search the latest Minecraft update"),
    ("convert", "@Paradise 100 USD to INR"),
    ("time", "@Paradise what time is it in Tokyo"),
    ("define", "@Paradise define ephemeral"),
    ("images", "@Paradise show me pictures of a red panda"),
    ("maths", "@Paradise what is 15% of 920"),
)


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
            name="💬 just mention me",
            value="\n".join(f"`{example}`" for _, example in MENTION_CAPABILITIES)
            + "\n`@Paradise translate hello into Japanese`",
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
