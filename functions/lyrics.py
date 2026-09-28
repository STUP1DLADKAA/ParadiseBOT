"""Lyrics retrieval, caching and Discord-native presentation.

Providers are a deliberate fallback chain, strongest first:

    LRCLIB  (free, no key, duration-aware matching)
      ↓
    lyrics.ovh (free, no key)
      ↓
    Genius  (only when GENIUS_API_KEY is configured — never mandatory)

Music commands and the panel share :class:`LyricsCache`, so opening lyrics
twice does not make two provider requests.  Rendering never performs network
I/O.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import logging
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import quote, urlencode

import aiohttp
import discord

from functions.helpers import Colour, shorten, strip_title_noise

log = logging.getLogger(__name__)

_PAGE_CHARS = 1800
_MAX_PAGES = 12
_CACHE_TTL = 60 * 60
_MISSING_TTL = 5 * 60
_TIMEOUT = aiohttp.ClientTimeout(total=15)

# Wired from Config at startup; Genius is skipped entirely when unset.
GENIUS_API_KEY: str | None = None


def clean_query(title: str, artist: str) -> tuple[str, str]:
    """Strip common video-title noise before searching.

    ``Artist - Song (Official Video)`` and ``Artist - Song [Official Audio]``
    both reduce to the same searchable query, while meaningful segments
    (Live, Remix, …) are preserved.
    """
    return strip_title_noise(title or ""), strip_title_noise(artist or "")


def lyrics_key(title: str, artist: str, identifier: str | None = None) -> str:
    """Return a stable, source-aware key for one track."""
    title, artist = clean_query(title, artist)
    identity = identifier.strip().casefold() if identifier else ""
    return "\x1f".join((identity, artist.casefold(), title.casefold()))


@dataclass(frozen=True, slots=True)
class LyricsResult:
    text: str
    source: str


def _tidy(text: str) -> str | None:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip(" \t\n")
    return text or None


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"\w+", (text or "").casefold()))


def _overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / max(len(a), len(b))


async def _fetch_lrclib(
    artist: str, title: str, duration_ms: int | None = None
) -> LyricsResult | None:
    """LRCLIB search with real matching: closest artist/title/duration wins."""
    params = {"q": f"{artist} {title}".strip()} if not artist else {
        "track_name": title,
        "artist_name": artist,
    }
    url = "https://lrclib.net/api/search?" + urlencode(params)
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.get(url) as response:
                if response.status == 404:
                    return None
                if response.status != 200:
                    log.debug("LRCLIB returned %s", response.status)
                    return None
                data = await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        log.debug("LRCLIB fetch failed", exc_info=True)
        return None
    if not isinstance(data, list) or not data:
        return None

    query_title, query_artist = _tokens(title), _tokens(artist)
    best, best_score = None, 0.0
    for candidate in data:
        if not isinstance(candidate, dict):
            continue
        if candidate.get("instrumental"):
            continue
        text = candidate.get("plainLyrics") or ""
        if not text:
            continue
        title_score = _overlap(query_title, _tokens(str(candidate.get("trackName", ""))))
        artist_score = _overlap(query_artist, _tokens(str(candidate.get("artistName", ""))))
        duration_score = 0.0
        if duration_ms:
            candidate_duration = candidate.get("duration")
            if isinstance(candidate_duration, (int, float)):
                delta = abs(candidate_duration * 1000 - duration_ms)
                if delta <= 2_000:
                    duration_score = 1.0
                elif delta <= 6_000:
                    duration_score = 0.5
        score = 2.0 * title_score + artist_score + duration_score
        if score > best_score:
            best, best_score = candidate, score
    if best is None or best_score < 0.9:
        return None  # nothing matched well enough — let the next provider try
    text = _tidy(str(best.get("plainLyrics") or ""))
    return LyricsResult(text, "LRCLIB") if text else None


async def _fetch_lyrics_ovh(
    artist: str, title: str, duration_ms: int | None = None
) -> LyricsResult | None:
    url = f"https://api.lyrics.ovh/v1/{quote(artist)}/{quote(title)}"
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.get(url) as response:
                if response.status == 404:
                    return None
                if response.status != 200:
                    log.debug("lyrics.ovh returned %s", response.status)
                    return None
                data = await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        log.debug("lyrics.ovh fetch failed", exc_info=True)
        return None
    text = _tidy((data or {}).get("lyrics") or "")
    return LyricsResult(text, "lyrics.ovh") if text else None


_GENIUS_LYRICS_BLOCK = re.compile(
    r'<div[^>]*data-lyrics-container="true"[^>]*>(.*?)</div>', re.DOTALL
)
_GENIUS_TAG = re.compile(r"<[^>]+>")


async def _fetch_genius(
    artist: str, title: str, duration_ms: int | None = None
) -> LyricsResult | None:
    """Best-effort Genius fallback. Needs GENIUS_API_KEY; never fatal."""
    if not GENIUS_API_KEY:
        return None
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            search_url = "https://api.genius.com/search?" + urlencode(
                {"q": f"{artist} {title}".strip()}
            )
            async with session.get(
                search_url, headers={"Authorization": f"Bearer {GENIUS_API_KEY}"}
            ) as response:
                if response.status != 200:
                    log.debug("Genius search returned %s", response.status)
                    return None
                data = await response.json(content_type=None)
            hits = (data or {}).get("response", {}).get("hits") or []
            if not hits:
                return None
            path = hits[0].get("result", {}).get("path")
            if not path:
                return None
            async with session.get(f"https://genius.com{path}") as response:
                if response.status != 200:
                    log.debug("Genius page returned %s", response.status)
                    return None
                page = await response.text()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        log.debug("Genius fetch failed", exc_info=True)
        return None

    blocks = _GENIUS_LYRICS_BLOCK.findall(page)
    if not blocks:
        return None
    text = "\n\n".join(
        html_lib.unescape(_GENIUS_TAG.sub("", block.replace("<br>", "\n").replace("<br/>", "\n")))
        for block in blocks
    )
    text = _tidy(text)
    return LyricsResult(text, "Genius") if text else None


Provider = Callable[[str, str, "int | None"], Awaitable[LyricsResult | None]]


async def fetch_lyrics(artist: str, title: str, duration_ms: int | None = None) -> LyricsResult | None:
    """Try every provider in order; first real lyrics win. Never raises."""
    for provider in (_fetch_lrclib, _fetch_lyrics_ovh, _fetch_genius):
        try:
            result = await provider(artist, title, duration_ms)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("Lyrics provider %s failed", provider.__name__, exc_info=True)
            continue
        if result is not None and result.text:
            return result
    return None


@dataclass(slots=True)
class LyricsEntry:
    text: str | None
    source: str | None
    expires_at: float


class LyricsCache:
    """Small TTL cache with per-track in-flight request de-duplication."""

    def __init__(self, *, ttl: float = _CACHE_TTL, missing_ttl: float = _MISSING_TTL):
        self.ttl = ttl
        self.missing_ttl = missing_ttl
        self._entries: dict[str, LyricsEntry] = {}
        self._inflight: dict[str, asyncio.Task[LyricsResult | None]] = {}

    def peek(self, key: str) -> tuple[bool, str | None]:
        entry = self._entries.get(key)
        if entry is None:
            return False, None
        if entry.expires_at <= time.monotonic():
            self._entries.pop(key, None)
            return False, None
        return True, entry.text

    def peek_result(self, key: str) -> tuple[bool, LyricsResult | None]:
        hit, text = self.peek(key)
        if not hit or text is None:
            return hit, None
        entry = self._entries.get(key)
        return True, LyricsResult(text, entry.source if entry else None)

    async def get(
        self,
        key: str,
        artist: str,
        title: str,
        duration_ms: int | None = None,
        *,
        fetcher: Callable[[str, str, "int | None"], Awaitable[LyricsResult | None]] = fetch_lyrics,
    ) -> LyricsResult | None:
        hit, cached = self.peek_result(key)
        if hit:
            return cached
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(fetcher(artist, title, duration_ms))
            self._inflight[key] = task
        try:
            result = await task
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("Lyrics provider failed", exc_info=True)
            result = None
        finally:
            if self._inflight.get(key) is task:
                self._inflight.pop(key, None)
        if result is not None:
            entry = LyricsEntry(result.text, result.source, time.monotonic() + self.ttl)
        else:
            entry = LyricsEntry(None, None, time.monotonic() + self.missing_ttl)
        self._entries[key] = entry
        return result

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


def render_lyrics_page(
    pages: list[str], page: int, title: str, artist: str, source: str | None = None
) -> discord.Embed:
    """Legacy embed renderer retained for command/API compatibility."""
    page = max(0, min(page, len(pages) - 1))
    embed = discord.Embed(
        title=f"Lyrics · {shorten(title, 80)}",
        description=pages[page] or "…",
        colour=Colour.IDLE,
    )
    footer = shorten(artist, 60)
    footer += f" · page {page + 1}/{len(pages)}"
    if source:
        footer += f" · {source}"
    embed.set_footer(text=footer)
    return embed


class LyricsPaginator(discord.ui.LayoutView):
    """Components V2 lyrics reader which edits one response in place."""

    def __init__(self, pages: list[str]):
        super().__init__(timeout=600)
        self.pages = pages
        self.page = 0
        self.title = ""
        self.artist = ""
        self.source: str | None = None

    def bind(self, title: str, artist: str, source: str | None = None) -> "LyricsPaginator":
        self.title = title
        self.artist = artist
        self.source = source
        self._build()
        return self

    def _build(self) -> None:
        self.clear_items()
        page = max(0, min(self.page, len(self.pages) - 1))
        container = discord.ui.Container(accent_colour=Colour.ACCENT)
        heading = shorten(
            discord.utils.escape_markdown(self.title or "Unknown track"), 90
        )
        artist = shorten(discord.utils.escape_markdown(self.artist or ""), 70)
        container.add_item(discord.ui.TextDisplay(
            f"### LYRICS\n**{heading}**\n-# {artist}" if artist else f"### LYRICS\n**{heading}**"
        ))
        container.add_item(discord.ui.Separator())
        body = self.pages[page]
        container.add_item(discord.ui.TextDisplay(body or "*No lyrics to display.*"))
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
            label=f"Page {page + 1} / {len(self.pages)}", disabled=True,
            custom_id="paradise:lyrics:page",
        ))
        row.add_item(next_button)
        container.add_item(row)
        footer = f"-# Source: {self.source}" if self.source else "-# Lyrics"
        container.add_item(discord.ui.TextDisplay(footer))
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
