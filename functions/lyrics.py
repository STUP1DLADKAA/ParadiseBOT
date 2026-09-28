"""Lyrics for the currently playing track.

Provider-based and optional. Default provider is the free, key-less
api.lyrics.ovh. Every failure degrades to a clean "no lyrics" answer —
raw provider errors are never shown to users.
"""

from __future__ import annotations

import logging
import re

import aiohttp
import discord

from functions.helpers import Colour, shorten

log = logging.getLogger(__name__)

_PAGE_CHARS = 1800
_MAX_PAGES = 5


def clean_query(title: str, artist: str) -> tuple[str, str]:
    """Strip the usual "(Official Video)" noise before searching."""

    def clean(value: str) -> str:
        value = re.sub(r"\s*[(\[{][^)\]}]*[)\]}]", " ", value)
        value = re.sub(r"\s*-?\s*topic$", " ", value, flags=re.IGNORECASE)
        value = re.sub(r"\s{2,}", " ", value)
        return value.strip(" -–—") or value.strip()

    return clean(title), clean(artist)


async def fetch_lyrics(artist: str, title: str) -> str | None:
    """Return lyrics text, or None when unavailable. Never raises."""
    import asyncio

    url = f"https://api.lyrics.ovh/v1/{artist}/{title}"
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        ) as session:
            async with session.get(url) as response:
                if response.status == 404:
                    return None
                if response.status != 200:
                    log.debug("Lyrics provider returned %s", response.status)
                    return None
                data = await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        log.debug("Lyrics fetch failed", exc_info=True)
        return None
    text = (data or {}).get("lyrics") or ""
    text = text.replace("\r\n", "\n").replace("\n\n\n", "\n\n").strip()
    return text or None


def paginate(text: str) -> list[str]:
    """Split lyrics into embed-sized pages at line boundaries."""
    pages: list[str] = []
    current: list[str] = []
    length = 0
    for line in text.split("\n"):
        if length + len(line) + 1 > _PAGE_CHARS and current:
            pages.append("\n".join(current).strip())
            current, length = [], 0
        current.append(line)
        length += len(line) + 1
    if current:
        pages.append("\n".join(current).strip())
    if len(pages) > _MAX_PAGES:
        pages = pages[:_MAX_PAGES]
        pages[-1] += "\n\n-# (truncated — the rest didn't fit)"
    return pages or [""]


def render_lyrics_page(pages: list[str], page: int, title: str, artist: str) -> discord.Embed:
    page = max(0, min(page, len(pages) - 1))
    embed = discord.Embed(
        title=f"📜 {shorten(title, 80)}",
        description=pages[page] or "…",
        colour=Colour.IDLE,
    )
    embed.set_footer(
        text=f"{shorten(artist, 60)} · page {page + 1}/{len(pages)} · lyrics via lyrics.ovh"
    )
    return embed


class LyricsPaginator(discord.ui.View):
    def __init__(self, pages: list[str]):
        super().__init__(timeout=300)
        self.pages = pages
        self.page = 0
        self.title = ""
        self.artist = ""

    def bind(self, title: str, artist: str) -> "LyricsPaginator":
        self.title = title
        self.artist = artist
        self._update()
        return self

    def _update(self) -> None:
        self.previous_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= len(self.pages) - 1

    async def _show(self, interaction: discord.Interaction) -> None:
        self._update()
        try:
            await interaction.response.edit_message(
                embed=render_lyrics_page(self.pages, self.page, self.title, self.artist),
                view=self,
            )
        except discord.DiscordException:
            pass

    @discord.ui.button(label="Previous", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def previous_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.page = max(0, self.page - 1)
        await self._show(interaction)

    @discord.ui.button(label="Next", emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.page = min(len(self.pages) - 1, self.page + 1)
        await self._show(interaction)
