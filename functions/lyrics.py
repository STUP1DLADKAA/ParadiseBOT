"""Lyrics retrieval, caching and Discord-native presentation.

The provider is deliberately small and optional.  Music commands and the panel
share :class:`LyricsCache`, so opening lyrics twice does not make two provider
requests.  Rendering never performs network I/O.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

import aiohttp
import discord

from functions.helpers import Colour, shorten

log = logging.getLogger(__name__)

_PAGE_CHARS = 1800
_MAX_PAGES = 5
_CACHE_TTL = 60 * 60
_MISSING_TTL = 5 * 60


def clean_query(title: str, artist: str) -> tuple[str, str]:
    """Strip common video-title noise before searching."""

    def clean(value: str) -> str:
        value = re.sub(r"\s*[(\[{][^)\]}]*[)\]}]", " ", value)
        value = re.sub(r"\s*-?\s*topic$", " ", value, flags=re.IGNORECASE)
        value = re.sub(r"\s{2,}", " ", value)
        return value.strip(" -–—") or value.strip()

    return clean(title), clean(artist)


def lyrics_key(title: str, artist: str, identifier: str | None = None) -> str:
    """Return a stable, source-aware key for one track."""
    title, artist = clean_query(title, artist)
    identity = identifier.strip().casefold() if identifier else ""
    return "\x1f".join((identity, artist.casefold(), title.casefold()))


async def fetch_lyrics(artist: str, title: str) -> str | None:
    """Return lyrics text, or ``None`` when unavailable. Never raises."""
    url = f"https://api.lyrics.ovh/v1/{artist}/{title}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
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


@dataclass(slots=True)
class LyricsEntry:
    text: str | None
    expires_at: float


class LyricsCache:
    """Small TTL cache with per-track in-flight request de-duplication."""

    def __init__(self, *, ttl: float = _CACHE_TTL, missing_ttl: float = _MISSING_TTL):
        self.ttl = ttl
        self.missing_ttl = missing_ttl
        self._entries: dict[str, LyricsEntry] = {}
        self._inflight: dict[str, asyncio.Task[str | None]] = {}

    def peek(self, key: str) -> tuple[bool, str | None]:
        entry = self._entries.get(key)
        if entry is None:
            return False, None
        if entry.expires_at <= time.monotonic():
            self._entries.pop(key, None)
            return False, None
        return True, entry.text

    async def get(
        self,
        key: str,
        artist: str,
        title: str,
        *,
        fetcher: Callable[[str, str], Awaitable[str | None]] = fetch_lyrics,
    ) -> str | None:
        hit, value = self.peek(key)
        if hit:
            return value
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(fetcher(artist, title))
            self._inflight[key] = task
        try:
            value = await task
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("Lyrics provider failed", exc_info=True)
            value = None
        finally:
            if self._inflight.get(key) is task:
                self._inflight.pop(key, None)
        lifetime = self.ttl if value else self.missing_ttl
        self._entries[key] = LyricsEntry(value, time.monotonic() + lifetime)
        return value

    def clear(self) -> None:
        self._entries.clear()


CACHE = LyricsCache()


def paginate(text: str) -> list[str]:
    """Split lyrics at line boundaries for readable mobile pages."""
    pages: list[str] = []
    current: list[str] = []
    length = 0
    for raw_line in text.split("\n"):
        # A provider can return a single pathological line. Split it safely.
        lines = [raw_line[i : i + _PAGE_CHARS] for i in range(0, len(raw_line), _PAGE_CHARS)] or [""]
        for line in lines:
            if length + len(line) + 1 > _PAGE_CHARS and current:
                pages.append("\n".join(current).strip())
                current, length = [], 0
            current.append(line)
            length += len(line) + 1
    if current:
        pages.append("\n".join(current).strip())
    if len(pages) > _MAX_PAGES:
        pages = pages[:_MAX_PAGES]
        pages[-1] = shorten(pages[-1], _PAGE_CHARS - 45) + "\n\n*(lyrics truncated to fit Discord)*"
    return pages or [""]


def render_lyrics_page(pages: list[str], page: int, title: str, artist: str) -> discord.Embed:
    """Legacy embed renderer retained for command/API compatibility."""
    page = max(0, min(page, len(pages) - 1))
    embed = discord.Embed(
        title=f"Lyrics · {shorten(title, 80)}",
        description=pages[page] or "…",
        colour=Colour.IDLE,
    )
    embed.set_footer(text=f"{shorten(artist, 60)} · page {page + 1}/{len(pages)} · lyrics.ovh")
    return embed


class LyricsPaginator(discord.ui.LayoutView):
    """Components V2 lyrics reader which edits one response in place."""

    def __init__(self, pages: list[str]):
        super().__init__(timeout=300)
        self.pages = pages
        self.page = 0
        self.title = ""
        self.artist = ""

    def bind(self, title: str, artist: str) -> "LyricsPaginator":
        self.title = title
        self.artist = artist
        self._build()
        return self

    def _build(self) -> None:
        self.clear_items()
        page = max(0, min(self.page, len(self.pages) - 1))
        container = discord.ui.Container(accent_colour=Colour.ACCENT)
        container.add_item(discord.ui.TextDisplay(
            f"### LYRICS\n**{shorten(self.title, 90)}**\n-# {shorten(self.artist, 70)}"
        ))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(self.pages[page] or "*No lyrics to display.*"))
        container.add_item(discord.ui.Separator())
        row = discord.ui.ActionRow()
        previous = discord.ui.Button(
            label="Previous", emoji="◀", custom_id="paradise:lyrics:previous",
            disabled=page <= 0,
        )
        next_button = discord.ui.Button(
            label="Next", emoji="▶", custom_id="paradise:lyrics:next",
            disabled=page >= len(self.pages) - 1,
        )
        previous.callback = self._previous
        next_button.callback = self._next
        row.add_item(previous)
        row.add_item(discord.ui.Button(
            label=f"{page + 1} / {len(self.pages)}", disabled=True,
            custom_id="paradise:lyrics:page",
        ))
        row.add_item(next_button)
        container.add_item(row)
        container.add_item(discord.ui.TextDisplay("-# Lyrics provided by lyrics.ovh"))
        self.add_item(container)

    async def _edit(self, interaction: discord.Interaction) -> None:
        self._build()
        try:
            await interaction.response.edit_message(view=self)
        except discord.DiscordException:
            pass

    async def _previous(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        await self._edit(interaction)

    async def _next(self, interaction: discord.Interaction) -> None:
        self.page = min(len(self.pages) - 1, self.page + 1)
        await self._edit(interaction)
