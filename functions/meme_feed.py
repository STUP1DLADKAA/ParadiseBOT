"""Memes and reaction GIFs pulled from the internet, with the guard rails on.

Two things live here:

* :class:`MemeFeed` — actual memes from actual Reddit communities, through
  `meme-api.com`, which needs **no API key**. This is what makes "random meme
  from the internet" work on a fresh `.env`.
* :class:`GifSearch` — reaction GIFs through Tenor or Giphy, which both need a
  free key. Without one it simply reports unavailable and the caller falls back
  to the rendered meme cards instead of posting a broken link.

Everything that comes back from the internet is *untrusted*: it is validated
before it is ever shown. The rules are boring and deliberate:

* https only, and the host must be one of the image CDNs we recognise;
* the URL must end in an image extension (a `.mp4` or an HTML page is not a
  picture, and Discord will not render it as one);
* NSFW / spoiler posts are dropped unless the operator explicitly opts in;
* titles are flattened to one line, stripped of mentions and markdown, and
  capped, so nothing can ping a role or break the message layout;
* every failure is a `None`, never an exception — a dead meme API must never
  take a conversation down with it.

Network access is injected (``fetch_json``), which is what makes this testable
without going online.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import urlparse

log = logging.getLogger(__name__)

#: ``fetch_json(url, params, timeout) -> dict | None``
JsonFetcher = Callable[[str, dict, float], Awaitable[object]]

#: Image CDNs we are willing to show. Anything else is dropped, no exceptions.
ALLOWED_HOSTS: frozenset[str] = frozenset(
    {
        "i.redd.it",
        "preview.redd.it",
        "external-preview.redd.it",
        "i.imgur.com",
        "i.ibb.co",
        "media.tenor.com",
        "media1.tenor.com",
        "c.tenor.com",
        "media.giphy.com",
        "i.giphy.com",
        "cdn.discordapp.com",
        "media.discordapp.net",
    }
)

_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp")
_GIF_SUFFIXES = (".gif",)
_MENTION_RE = re.compile(r"<@[!&]?\d+>|@everyone|@here")
_MARKDOWN_RE = re.compile(r"[*_`~|>\\]")
_WHITESPACE_RE = re.compile(r"\s+")

MEME_API_BASE = "https://meme-api.com/gimme"
TENOR_SEARCH = "https://tenor.googleapis.com/v2/search"
GIPHY_SEARCH = "https://api.giphy.com/v1/gifs/search"


@dataclass(frozen=True, slots=True)
class OnlineMeme:
    """One validated, postable meme."""

    url: str
    title: str = ""
    source: str = ""
    page_url: str = ""
    is_gif: bool = False


def clean_title(title: str | None, *, limit: int = 140) -> str:
    """One line, no mentions, no markdown, no 400-word Reddit title."""
    text = _MENTION_RE.sub("", title or "")
    text = _MARKDOWN_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def is_postable_image(url: str, *, gifs_only: bool = False) -> bool:
    """Is this a URL Discord will actually render inside an embed?"""
    if not url or not isinstance(url, str):
        return False
    parsed = urlparse(url.strip())
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        return False
    path = parsed.path.lower()
    suffixes = _GIF_SUFFIXES if gifs_only else _IMAGE_SUFFIXES
    return path.endswith(suffixes)


def parse_meme(payload: object, *, source: str = "", allow_nsfw: bool = False) -> OnlineMeme | None:
    """Turn one meme-api.com response into a validated :class:`OnlineMeme`."""
    if not isinstance(payload, dict):
        return None
    if payload.get("nsfw") and not allow_nsfw:
        return None
    if payload.get("spoiler") and not allow_nsfw:
        return None
    url = payload.get("url")
    if not is_postable_image(url):
        return None
    page = payload.get("postLink")
    return OnlineMeme(
        url=url.strip(),
        title=clean_title(payload.get("title")),
        source=clean_title(payload.get("subreddit") or source, limit=40),
        page_url=page.strip() if isinstance(page, str) and page.startswith("https://") else "",
        is_gif=url.lower().split("?")[0].endswith(_GIF_SUFFIXES),
    )


def parse_tenor(payload: object) -> OnlineMeme | None:
    """Pick one GIF out of a Tenor v2 search response."""
    if not isinstance(payload, dict):
        return None
    results = payload.get("results")
    if not isinstance(results, list):
        return None
    for entry in results:
        if not isinstance(entry, dict):
            continue
        formats = entry.get("media_formats")
        if not isinstance(formats, dict):
            continue
        for key in ("gif", "mediumgif", "tinygif", "nanogif"):
            media = formats.get(key)
            if isinstance(media, dict) and is_postable_image(media.get("url"), gifs_only=True):
                return OnlineMeme(
                    url=media["url"].strip(),
                    title=clean_title(entry.get("content_description") or ""),
                    source="tenor",
                    page_url=(entry.get("itemurl") or "").strip()
                    if isinstance(entry.get("itemurl"), str)
                    else "",
                    is_gif=True,
                )
    return None


def parse_giphy(payload: object) -> OnlineMeme | None:
    """Pick one GIF out of a Giphy search response."""
    if not isinstance(payload, dict):
        return None
    entries = payload.get("data")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        images = entry.get("images")
        if not isinstance(images, dict):
            continue
        original = images.get("original")
        if isinstance(original, dict) and is_postable_image(original.get("url"), gifs_only=True):
            return OnlineMeme(
                url=original["url"].strip(),
                title=clean_title(entry.get("title") or ""),
                source="giphy",
                page_url=(entry.get("url") or "").strip()
                if isinstance(entry.get("url"), str)
                else "",
                is_gif=True,
            )
    return None


async def _http_get_json(
    session_factory,
    url: str,
    params: dict,
    timeout: float,
    *,
    headers: dict[str, str] | None = None,
) -> object:
    """GET a JSON document through a pooled session; `None` on any problem."""
    try:
        session = session_factory("memes", timeout=timeout, headers=headers)
    except Exception:  # noqa: BLE001 - a missing session is just "no internet"
        log.debug("Could not obtain an HTTP session for %s", url, exc_info=True)
        return None
    try:
        async with session.get(url, params=params, timeout=timeout) as response:
            if getattr(response, "status", 0) != 200:
                log.debug("%s returned %s", url, getattr(response, "status", "?"))
                return None
            return await response.json(content_type=None)
    except asyncio.TimeoutError:
        log.debug("%s timed out", url)
        return None
    except Exception:  # noqa: BLE001 - the internet is not a guarantee
        log.debug("%s failed", url, exc_info=True)
        return None


class MemeFeed:
    """Key-less random memes, cached and rate-limit aware.

    ``fetch_json`` is injected so tests never touch the network; production
    passes a pooled-session fetcher.
    """

    def __init__(
        self,
        *,
        sources: tuple[str, ...] = ("dankmemes", "memes", "me_irl", "wholesomememes"),
        api_base: str = MEME_API_BASE,
        allow_nsfw: bool = False,
        timeout: float = 6.0,
        cache_size: int = 6,
        retry_after: float = 90.0,
        fetch_json: JsonFetcher | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.sources = tuple(source for source in sources if source) or ("memes",)
        self.api_base = api_base.rstrip("/")
        self.allow_nsfw = bool(allow_nsfw)
        self.timeout = float(timeout)
        self.retry_after = float(retry_after)
        self._cache: deque[OnlineMeme] = deque(maxlen=max(1, int(cache_size)))
        self._clock = clock
        self._cooldown_until = 0.0
        self._lock = asyncio.Lock()
        self._fetch_json = fetch_json or self._default_fetcher()

    def _default_fetcher(self) -> JsonFetcher:
        from functions.perf import shared_session

        async def fetcher(url: str, params: dict, timeout: float) -> object:
            return await _http_get_json(shared_session, url, params, timeout)

        return fetcher

    # -- cache ---------------------------------------------------------

    def take(self) -> OnlineMeme | None:
        """Pop a cached meme, if one is already sitting there."""
        return self._cache.popleft() if self._cache else None

    def __len__(self) -> int:
        return len(self._cache)

    # -- network -------------------------------------------------------

    async def fetch_one(self, source: str | None = None) -> OnlineMeme | None:
        """Ask the API for one meme from one community."""
        community = source or random.choice(self.sources)
        try:
            payload = await self._fetch_json(
                f"{self.api_base}/{community}", {}, self.timeout
            )
        except Exception:  # noqa: BLE001 - the internet is never guaranteed
            log.debug("Meme fetch raised", exc_info=True)
            return None
        meme = parse_meme(payload, source=community, allow_nsfw=self.allow_nsfw)
        if meme is None and payload is not None:
            log.debug("meme-api returned an unusable post for %s", community)
        return meme

    async def prefetch(self, count: int = 2) -> int:
        """Fill the cache in the background; returns how many arrived."""
        filled = 0
        async with self._lock:
            for _ in range(max(0, int(count))):
                if len(self._cache) >= self._cache.maxlen:
                    break
                if self._clock() < self._cooldown_until:
                    break
                meme = await self.fetch_one()
                if meme is None:
                    # Back off: a dead API should not be hammered all evening.
                    self._cooldown_until = self._clock() + self.retry_after
                    break
                self._cache.append(meme)
                filled += 1
        return filled

    async def pick(self) -> OnlineMeme | None:
        """One meme, from the cache when possible, from the internet otherwise."""
        cached = self.take()
        if cached is not None:
            # Top the cache back up without making the caller wait.
            asyncio.create_task(self._quiet_prefetch())
            return cached
        if self._clock() < self._cooldown_until:
            return None
        meme = await self.fetch_one()
        if meme is None:
            self._cooldown_until = self._clock() + self.retry_after
            return None
        asyncio.create_task(self._quiet_prefetch())
        return meme

    async def _quiet_prefetch(self) -> None:
        try:
            await self.prefetch(1)
        except Exception:  # noqa: BLE001 - a prefetch is never allowed to matter
            log.debug("Meme prefetch failed", exc_info=True)


class GifSearch:
    """Reaction GIFs through Tenor or Giphy. Needs a free key; without one it
    reports :attr:`available` as ``False`` and every call is a quiet ``None``."""

    def __init__(
        self,
        *,
        tenor_key: str = "",
        giphy_key: str = "",
        timeout: float = 6.0,
        rating: str = "pg-13",
        fetch_json: JsonFetcher | None = None,
    ) -> None:
        self.tenor_key = (tenor_key or "").strip()
        self.giphy_key = (giphy_key or "").strip()
        self.timeout = float(timeout)
        self.rating = rating or "pg-13"
        self._fetch_json = fetch_json or self._default_fetcher()

    def _default_fetcher(self) -> JsonFetcher:
        from functions.perf import shared_session

        async def fetcher(url: str, params: dict, timeout: float) -> object:
            return await _http_get_json(shared_session, url, params, timeout)

        return fetcher

    @property
    def provider(self) -> str:
        if self.tenor_key:
            return "tenor"
        if self.giphy_key:
            return "giphy"
        return ""

    @property
    def available(self) -> bool:
        return bool(self.provider)

    async def _search_json(self, url: str, params: dict, timeout: float) -> object:
        try:
            return await self._fetch_json(url, params, timeout)
        except Exception:  # noqa: BLE001
            log.debug("GIF search raised", exc_info=True)
            return None

    async def search(self, query: str) -> OnlineMeme | None:
        """One GIF matching *query*, or ``None``."""
        text = clean_title(query, limit=80)
        if not text:
            return None
        if self.tenor_key:
            payload = await self._search_json(
                TENOR_SEARCH,
                {
                    "q": text,
                    "key": self.tenor_key,
                    "limit": 20,
                    "contentfilter": "high",
                    "media_filter": "gif,tinygif",
                },
                self.timeout,
            )
            meme = parse_tenor(payload)
            if meme is not None:
                return meme
        if self.giphy_key:
            payload = await self._search_json(
                GIPHY_SEARCH,
                {
                    "q": text,
                    "api_key": self.giphy_key,
                    "limit": 20,
                    "rating": self.rating,
                },
                self.timeout,
            )
            return parse_giphy(payload)
        return None


__all__ = [
    "ALLOWED_HOSTS",
    "GifSearch",
    "MemeFeed",
    "OnlineMeme",
    "clean_title",
    "is_postable_image",
    "parse_giphy",
    "parse_meme",
    "parse_tenor",
]
