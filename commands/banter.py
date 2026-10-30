"""Banter commands: /roast, /meme and /gif.

The passive engine (:mod:`functions.personality`) decides *when* to be funny.
These three let a person ask for it directly, which is most of the fun in a
friends-only server.

Every command is cheap, never blocks the event loop on a slow API for long,
and degrades instead of failing: no internet means a locally rendered meme,
no GIF key means a meme with a one-line note about how to turn GIFs on.
"""

from __future__ import annotations

import asyncio
import io
import logging

import discord
from discord import app_commands
from discord.ext import commands

from functions.meme_feed import GifSearch, MemeFeed, OnlineMeme
from functions.memes import render_meme_png
from functions.roasts import (
    build_roast,
    clean_name,
    looks_sensitive,
    sanitize_intensity,
)

log = logging.getLogger(__name__)

_NO_GIF_KEY = (
    "gifs aren't switched on — the owner can add a free `TENOR_API_KEY` "
    "(or `GIPHY_API_KEY`) to `.env`. here's a meme instead:"
)
_GIF_EMPTY = "that search came back empty. try different words."
_MEME_EMPTY = "couldn't reach the meme mines. try again in a minute."


def build_roast_line(name: str, *, intensity: str = "spicy") -> str:
    """The exact text /roast sends. Pure, so it is easy to test."""
    target = clean_name(name, fallback="you")
    return build_roast(target, intensity=sanitize_intensity(intensity))


def meme_embed(meme: OnlineMeme) -> discord.Embed:
    """A neutral embed that lets the image do the talking."""
    embed = discord.Embed(title=meme.title or None)
    embed.set_image(url=meme.url)
    source = f"r/{meme.source}" if meme.source else ""
    footer = " · ".join(part for part in (source, meme.page_url) if part)
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
        self._fallback_feed: MemeFeed | None = None
        self._fallback_gifs: GifSearch | None = None

    # -- lazily reach the personality engine's services when it has them --

    def _personality(self):
        return getattr(self.bot, "personality", None)

    @property
    def meme_feed(self) -> MemeFeed:
        engine = self._personality()
        feed = getattr(engine, "meme_feed", None)
        if feed is not None:
            return feed
        if self._fallback_feed is None:
            self._fallback_feed = MemeFeed()
        return self._fallback_feed

    @property
    def gif_search(self) -> GifSearch:
        engine = self._personality()
        search = getattr(engine, "gif_search", None)
        if search is not None:
            return search
        if self._fallback_gifs is None:
            self._fallback_gifs = GifSearch()
        return self._fallback_gifs

    @property
    def intensity(self) -> str:
        engine = self._personality()
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

    @app_commands.command(description="A random meme from the internet")
    @app_commands.describe(topic="Optional: what the meme should be about")
    @gif_cooldown()
    async def meme(
        self, interaction: discord.Interaction, topic: str | None = None
    ) -> None:
        await interaction.response.defer(thinking=True)
        meme = await self._fetch_meme()
        if meme is not None:
            await interaction.followup.send(
                embed=meme_embed(meme),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        # No internet, no problem: draw one.
        author = clean_name(
            getattr(interaction.user, "display_name", None), fallback="someone"
        )
        try:
            png = render_topic_meme(
                topic or "the meme mines are closed today", author=author
            )
        except Exception:  # noqa: BLE001 - rendering must never crash a command
            log.debug("Topic meme render failed", exc_info=True)
            await interaction.followup.send(_MEME_EMPTY)
            return
        await interaction.followup.send(
            file=discord.File(io.BytesIO(png), filename="paradise_meme.png"),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(description="A reaction GIF (needs a free Tenor or Giphy key)")
    @app_commands.describe(query="What kind of reaction, e.g. 'bruh' or 'this is fine'")
    @gif_cooldown()
    async def gif(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer(thinking=True)
        search = self.gif_search
        if not search.available:
            meme = await self._fetch_meme()
            if meme is not None:
                await interaction.followup.send(
                    _NO_GIF_KEY,
                    embed=meme_embed(meme),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            await interaction.followup.send(_NO_GIF_KEY)
            return

        gif = await self._search_gif(search, query)
        if gif is not None:
            await interaction.followup.send(
                embed=meme_embed(gif),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.followup.send(_GIF_EMPTY)

    # -- internals ------------------------------------------------------

    async def _fetch_meme(self) -> OnlineMeme | None:
        try:
            return await asyncio.wait_for(self.meme_feed.pick(), timeout=8.0)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            log.debug("Meme fetch failed", exc_info=True)
            return None

    async def _search_gif(
        self, search: GifSearch, query: str
    ) -> OnlineMeme | None:
        try:
            return await asyncio.wait_for(search.search(query), timeout=8.0)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            log.debug("GIF search failed", exc_info=True)
            return None


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Banter(bot))
