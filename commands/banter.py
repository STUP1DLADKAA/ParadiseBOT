"""Banter commands: /roast.

The passive engine (:mod:`functions.personality`) decides *when* to be funny.
This lets a person ask for it directly.

The command is cheap, never blocks the event loop on a slow API for long,
and degrades instead of failing.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from functions.memes import render_meme_png
from functions.roasts import (
    build_roast,
    clean_name,
    looks_sensitive,
    sanitize_intensity,
)

log = logging.getLogger(__name__)

_NO_GIF_KEY = (
    "gifs are being moody — the owner can add a free `TENOR_API_KEY` "
    "(or `GIPHY_API_KEY`) to `.env` for the good ones. here's a meme instead:"
)
_MEME_EMPTY = "couldn't reach the meme mines. try again in a minute."
_GIF_EMPTY = "that search came back empty. try different words."


def build_roast_line(name: str, *, intensity: str = "spicy") -> str:
    """The exact text /roast sends. Pure, so it is easy to test."""
    target = clean_name(name, fallback="you")
    return build_roast(target, intensity=sanitize_intensity(intensity))


def meme_embed(meme: object) -> discord.Embed:
    """A neutral embed that lets the image do the talking."""
    embed = discord.Embed(title=getattr(meme, "title", None) or None)
    embed.set_image(url=getattr(meme, "url", ""))
    source = getattr(meme, "source", None) or ""
    page_url = getattr(meme, "page_url", None) or ""
    footer = " · ".join(part for part in (source, page_url) if part)
    if footer:
        embed.set_footer(text=discord.utils.escape_markdown(footer)[:200])
    return embed


def render_topic_meme(topic: str, *, author: str = "someone") -> bytes:
    """A locally drawn meme card about *topic* — the offline answer."""
    png, _template = render_meme_png(topic or "say something funny", author)
    return png


def gif_cooldown():
    return app_commands.checks.cooldown(1, 4, key=lambda i: (i.guild_id, i.user.id))


class Banter(commands.Cog):
    """Deliberately mean, on request."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def intensity(self) -> str:
        engine = getattr(self.bot, "personality", None)
        return sanitize_intensity(getattr(engine, "roast_intensity", "spicy"))

    # -- commands -------------------------------------------------------

    @app_commands.command(description="Get roasted. /roast @someone to pass it on")
    @app_commands.describe(member="Who to roast (default: you)")
    @gif_cooldown()
    async def roast(
        self, interaction: discord.Interaction, member: discord.Member | None = None
    ) -> None:
        target = member or interaction.user
        self_roast = target.id == interaction.user.id
        name = clean_name(
            getattr(target, "display_name", None) or getattr(target, "name", None),
            fallback="you",
        )

        # Never roast a message that reads like real distress — but a command
        # has no message, so this only trips when the *name* itself is odd.
        if looks_sensitive(name):
            await interaction.response.send_message(
                "not that one. pick someone having a better day.", ephemeral=True
            )
            return

        line = build_roast_line(name, intensity=self.intensity)
        prefix = "roasting myself, since you asked: " if self_roast else ""
        await interaction.response.send_message(
            f"{prefix}{line}",
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Banter(bot))
