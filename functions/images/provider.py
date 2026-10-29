"""Real web image search — never AI image generation.

When someone asks Paradise for "a picture of a red panda" they mean a
photograph that already exists on the web, so this module queries actual
image-search providers and hands Discord the remote URL. Nothing is
generated, and image bytes are never downloaded or stored.

Provider chain (first ones that produce usable results win):

1. **Google Programmable Search** — optional, only when
   ``IMAGE_SEARCH_API_KEY`` *and* ``IMAGE_SEARCH_ENGINE_ID`` are set.
   Best general web coverage; 100 free queries/day.
2. **Wikipedia page images** — key-less, extremely high precision for
   notable subjects (landmarks, animals, people, products).
3. **Wikimedia Commons** — key-less, broad freely-licensed photo library.
4. **Openverse** — key-less Creative Commons index; used last because
   anonymous callers get a small daily quota.

Every result is URL-validated before it is shown (§ image safety): only
well-formed public HTTP(S) URLs survive, and anything pointing at a
private/loopback host is dropped so a compromised provider can never turn
the bot into an SSRF probe.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import logging
import re
from typing import Any, Iterable
from urllib.parse import urlparse

import aiohttp

from functions.perf import shared_session

from functions.cache import SingleFlight, TTLCache
from .models import ImageResponse, ImageResult

log = logging.getLogger(__name__)

# How long the preferred image source gets before the next one is started as
# well. Small enough to hide a slow provider, large enough not to fan out on
# every ordinary lookup.
HEDGE_DELAY = 0.35

USER_AGENT = (
    "ParadiseBot/1.0 (+https://github.com/STUP1DLADKAA/ParadiseBOT) image-search"
)

_CSE_ENDPOINT = "https://www.googleapis.com/customsearch/v1"
_WIKI_ENDPOINT = "https://en.wikipedia.org/w/api.php"
_COMMONS_ENDPOINT = "https://commons.wikimedia.org/w/api.php"
_OPENVERSE_ENDPOINT = "https://api.openverse.org/v1/images/"

_MAX_RESULTS = 5
_MAX_URL_LENGTH = 2048
_MAX_PROBES = 3
_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")

# Commons indexes a lot of diagrams, logos, flags and maps. For "show me a
# picture of X" a photograph is almost always what was meant.
_COMMONS_NOISE = re.compile(
    r"\b(?:logo|icon|coat of arms|flag of|map of|diagram|chart|signature|"
    r"barnstar|stub|disambig|wikimedia|wikipedia|commons-logo|blank|template)\b",
    re.IGNORECASE,
)
_TAG = re.compile(r"<[^>]+>")


class ImageSearchError(Exception):
    """Internal: a provider could not be reached. Never shown to users."""


def _strip_html(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub("", str(value or "")))).strip()


def is_safe_image_url(url: str) -> bool:
    """True when *url* is a well-formed, publicly routable HTTP(S) URL.

    Used as the single gate before any image URL is embedded or probed.
    Rejects malformed input, non-web schemes (``data:``, ``file:``,
    ``javascript:``), embedded credentials, control characters and hosts
    that resolve to private, loopback or link-local address literals.
    """
    candidate = str(url or "").strip()
    if not candidate or len(candidate) > _MAX_URL_LENGTH:
        return False
    if any(ch.isspace() or ord(ch) < 32 for ch in candidate):
        return False
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return False
    if parsed.scheme.lower() not in ("http", "https"):
        return False
    # Credentials in a URL are never legitimate for a provider image.
    if parsed.username or parsed.password or "@" in (parsed.netloc or ""):
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host or host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "." in host  # a normal domain name must have a dot
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def _looks_like_image(url: str, mime: str) -> bool:
    if str(mime or "").lower().startswith("image/"):
        return True
    path = urlparse(url).path.lower()
    return path.endswith(_IMAGE_EXTENSIONS)


def _session(timeout: float) -> aiohttp.ClientSession:
    """The pooled HTTP session used for image lookups.

    A single place to swap sessions is also what the offline tests patch, so
    the behaviour under test is the real code path.
    """
    return shared_session(
        "images",
        timeout=timeout,
        headers={"User-Agent": USER_AGENT},
    )


class ImageSearchService:
    """Searches the web for images, with caching and graceful degradation."""

    def __init__(self, config: Any = None) -> None:
        self._config = config
        self.timeout = max(3.0, float(getattr(config, "timeout", 10.0) or 10.0))
        self._api_key = str(getattr(config, "api_key", "") or "").strip()
        self._engine_id = str(getattr(config, "engine_id", "") or "").strip()
        self._enabled = bool(getattr(config, "enabled", True))
        self._cache: TTLCache[ImageResponse] = TTLCache(
            ttl=max(60.0, float(getattr(config, "cache_ttl", 1800) or 1800)),
            max_entries=max(8, int(getattr(config, "cache_size", 128) or 128)),
        )
        self._inflight = SingleFlight()

    # ------------------------------------------------------------------
    # Configuration surface
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def has_api_provider(self) -> bool:
        """True when the optional Google Programmable Search upgrade is set."""
        return bool(self._api_key and self._engine_id)

    def describe(self) -> str:
        chain = ["wikipedia", "commons", "openverse"]
        if self.has_api_provider:
            chain.insert(0, "google-cse")
        return " → ".join(chain)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def search(self, query: str, limit: int = 1) -> ImageResponse:
        """Find up to *limit* usable web images for *query*. Never raises."""
        cleaned = " ".join(str(query or "").split()).strip()
        if not cleaned or not self._enabled:
            return ImageResponse(query=cleaned)
        if len(cleaned) > 200:
            cleaned = cleaned[:200].rstrip()
        wanted = max(1, min(int(limit or 1), _MAX_RESULTS))

        # One cache entry per query. A cached entry only answers a request
        # it can actually fill, so asking for one picture never pays for
        # five — and never fans out to every provider to top the cache up.
        key = cleaned.casefold()
        cached = self._cache.get(key)
        if cached is not None and len(cached.results) >= wanted:
            return ImageResponse(
                query=cached.query,
                results=list(cached.results[:wanted]),
                provider=cached.provider,
                cached=True,
            )

        try:
            response = await self._inflight.run(
                f"{key}|{wanted}", lambda: self._search_uncached(cleaned, wanted)
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - image search never breaks a reply
            log.warning("Image search failed for %r", cleaned, exc_info=True)
            return ImageResponse(query=cleaned)

        return ImageResponse(
            query=response.query,
            results=list(response.results[:wanted]),
            provider=response.provider,
            cached=response.cached,
        )

    # ------------------------------------------------------------------
    # Provider chain
    # ------------------------------------------------------------------

    async def _search_uncached(self, query: str, limit: int) -> ImageResponse:
        providers: list[tuple[str, Any]] = []
        if self.has_api_provider:
            providers.append(("google", self._search_google))
        providers.extend(
            (
                ("wikipedia", self._search_wikipedia),
                ("commons", self._search_commons),
                ("openverse", self._search_openverse),
            )
        )

        collected: list[ImageResult] = []
        seen: set[str] = set()
        used: list[str] = []

        def _merge(name: str, found: list[ImageResult]) -> None:
            """Add a provider's results, de-duplicated and capped."""
            added = False
            for item in found:
                if len(collected) >= limit:
                    break
                fingerprint = item.image_url.rstrip("/").casefold()
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                collected.append(item)
                added = True
            if added and name not in used:
                used.append(name)

        session = _session(self.timeout)
        pending: dict[asyncio.Task, str] = {}

        def _harvest(task: asyncio.Task) -> None:
            name = pending.pop(task)
            if task.cancelled():
                return
            try:
                found = task.result()
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                log.debug("Image provider %s unreachable", name)
                return
            except Exception:  # noqa: BLE001 - one bad provider never wins
                log.debug("Image provider %s failed", name, exc_info=True)
                return
            _merge(name, list(found or []))

        # Hedged, priority-ordered fetch: the preferred source is asked first
        # and gets a short head start. Only when it is slow or comes back thin
        # is the next source started — in parallel with whatever is still
        # running — so an ordinary lookup is one request and a bad day never
        # means waiting for every provider in turn.
        try:
            for name, provider in providers:
                if len(collected) >= limit:
                    break
                pending[asyncio.ensure_future(provider(session, query, limit))] = name
                done, _ = await asyncio.wait(
                    set(pending), timeout=HEDGE_DELAY, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    _harvest(task)

            # Give anything still in flight one more short grace period, in
            # priority order — but never wait on a straggler once the answer is
            # already rich enough. The rest is cancelled below.
            for task in list(pending):
                if len(collected) >= limit:
                    break
                if not task.done():
                    await asyncio.wait({task}, timeout=HEDGE_DELAY)
                if task.done():
                    _harvest(task)
        finally:
            for task in pending:
                if not task.done():
                    task.cancel()

        usable = await self._keep_embeddable(session, collected, limit)

        response = ImageResponse(
            query=query, results=usable, provider="+".join(used) if used else ""
        )
        if usable:
            key = query.casefold()
            previous = self._cache.get(key)
            # Never downgrade a richer cached result with a smaller one.
            if previous is None or len(usable) >= len(previous.results):
                self._cache.set(key, response)
        return response

    # -- Google Programmable Search (optional) ----------------------------

    async def _search_google(
        self, session: aiohttp.ClientSession, query: str, limit: int
    ) -> list[ImageResult]:
        params = {
            "key": self._api_key,
            "cx": self._engine_id,
            "q": query,
            "searchType": "image",
            "num": str(max(1, min(limit, 10))),
            "safe": "active",
        }
        payload = await self._get_json(session, _CSE_ENDPOINT, params, provider="google")
        results: list[ImageResult] = []
        for item in _iter_dicts(payload.get("items")):
            image_meta = item.get("image") if isinstance(item.get("image"), dict) else {}
            built = self._build(
                title=_strip_html(item.get("title") or query),
                image_url=item.get("link") or "",
                source_url=str(image_meta.get("contextLink") or ""),
                provider="Google",
                mime=str(item.get("mime") or ""),
                width=_as_int(image_meta.get("width")),
                height=_as_int(image_meta.get("height")),
            )
            if built is not None:
                results.append(built)
        return results

    # -- Wikipedia page images (key-less) ---------------------------------

    async def _search_wikipedia(
        self, session: aiohttp.ClientSession, query: str, limit: int
    ) -> list[ImageResult]:
        params = {
            "action": "query",
            "generator": "search",
            "gsrsearch": query,
            "gsrnamespace": "0",
            "gsrlimit": str(max(1, min(limit + 2, 6))),
            "prop": "pageimages|info",
            "piprop": "original|thumbnail",
            "pithumbsize": "1280",
            "inprop": "url",
            "format": "json",
            "formatversion": "2",
            "origin": "*",
        }
        payload = await self._get_json(session, _WIKI_ENDPOINT, params, provider="wikipedia")
        pages = (payload.get("query") or {}).get("pages")
        results: list[ImageResult] = []
        for page in _iter_dicts(pages):
            original = page.get("original") if isinstance(page.get("original"), dict) else {}
            thumbnail = page.get("thumbnail") if isinstance(page.get("thumbnail"), dict) else {}
            image_url = str(original.get("source") or thumbnail.get("source") or "")
            if not image_url:
                continue
            built = self._build(
                title=_strip_html(page.get("title") or query),
                image_url=image_url,
                source_url=str(page.get("fullurl") or ""),
                provider="Wikipedia",
                width=_as_int(original.get("width") or thumbnail.get("width")),
                height=_as_int(original.get("height") or thumbnail.get("height")),
            )
            if built is not None:
                results.append(built)
        return results

    # -- Wikimedia Commons (key-less) --------------------------------------

    async def _search_commons(
        self, session: aiohttp.ClientSession, query: str, limit: int
    ) -> list[ImageResult]:
        params = {
            "action": "query",
            "generator": "search",
            # ``filetype:bitmap`` keeps SVG icons and PDFs out of the results.
            "gsrsearch": f"{query} filetype:bitmap",
            "gsrnamespace": "6",
            "gsrlimit": str(max(2, min(limit + 4, 10))),
            "prop": "imageinfo",
            "iiprop": "url|mime|size|extmetadata",
            "iiurlwidth": "1280",
            "format": "json",
            "formatversion": "2",
            "origin": "*",
        }
        payload = await self._get_json(session, _COMMONS_ENDPOINT, params, provider="commons")
        pages = (payload.get("query") or {}).get("pages")
        results: list[ImageResult] = []
        for page in _iter_dicts(pages):
            info_list = page.get("imageinfo")
            info = next(iter(_iter_dicts(info_list)), None)
            if info is None:
                continue
            title = _strip_html(str(page.get("title") or "")).removeprefix("File:")
            if _COMMONS_NOISE.search(title):
                continue
            metadata = info.get("extmetadata") if isinstance(info.get("extmetadata"), dict) else {}
            artist = _strip_html(str((metadata.get("Artist") or {}).get("value", "")))
            built = self._build(
                title=title or query,
                # Prefer the resized thumbnail: full-size Commons originals
                # are routinely 20 MB+, which Discord refuses to embed.
                image_url=str(info.get("thumburl") or info.get("url") or ""),
                source_url=str(info.get("descriptionurl") or ""),
                provider="Wikimedia Commons",
                mime=str(info.get("thumbmime") or info.get("mime") or ""),
                width=_as_int(info.get("thumbwidth") or info.get("width")),
                height=_as_int(info.get("thumbheight") or info.get("height")),
                attribution=artist[:80],
            )
            if built is not None:
                results.append(built)
        return results

    # -- Openverse (key-less, small anonymous quota) ------------------------

    async def _search_openverse(
        self, session: aiohttp.ClientSession, query: str, limit: int
    ) -> list[ImageResult]:
        params = {
            "q": query,
            "page_size": str(max(1, min(limit + 2, 8))),
            "mature": "false",
        }
        payload = await self._get_json(
            session, _OPENVERSE_ENDPOINT, params, provider="openverse"
        )
        results: list[ImageResult] = []
        for item in _iter_dicts(payload.get("results")):
            built = self._build(
                title=_strip_html(item.get("title") or query),
                image_url=str(item.get("url") or ""),
                source_url=str(item.get("foreign_landing_url") or ""),
                provider="Openverse",
                width=_as_int(item.get("width")),
                height=_as_int(item.get("height")),
                attribution=_strip_html(str(item.get("creator") or ""))[:80],
            )
            if built is not None:
                results.append(built)
        return results

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build(self, *, image_url: str, **fields: Any) -> ImageResult | None:
        """Validate the URL and construct a result, or return ``None``."""
        url = str(image_url or "").strip()
        if not is_safe_image_url(url):
            return None
        source_url = str(fields.pop("source_url", "") or "")
        if source_url and not is_safe_image_url(source_url):
            source_url = ""
        return ImageResult(image_url=url, source_url=source_url, **fields)

    async def _get_json(
        self,
        session: aiohttp.ClientSession,
        url: str,
        params: dict[str, str],
        *,
        provider: str,
    ) -> dict[str, Any]:
        """One normalized, timeout-bounded JSON GET. Keys never get logged."""
        async with session.get(url, params=params) as response:
            if response.status != 200:
                # The URL can carry an API key for Google CSE, so only the
                # provider label and status are ever logged.
                log.debug("Image provider %s returned HTTP %s", provider, response.status)
                raise ImageSearchError(provider)
            payload = await response.json(content_type=None)
        return payload if isinstance(payload, dict) else {}

    async def _keep_embeddable(
        self,
        session: aiohttp.ClientSession,
        results: Iterable[ImageResult],
        limit: int,
    ) -> list[ImageResult]:
        """Drop results Discord would fail to render, with bounded probing.

        Almost every provider declares a MIME type or returns a URL with a
        normal image extension, so this usually costs zero extra requests.
        Unknown candidates get at most ``_MAX_PROBES`` cheap HEAD requests —
        a failed one is dropped, never retried.
        """
        kept: list[ImageResult] = []
        probes = 0
        for item in results:
            if len(kept) >= limit:
                break
            if _looks_like_image(item.image_url, item.mime):
                kept.append(item)
                continue
            if probes >= _MAX_PROBES:
                continue
            probes += 1
            if await self._probe(session, item):
                kept.append(item)
        return kept

    async def _probe(self, session: aiohttp.ClientSession, item: ImageResult) -> bool:
        """HEAD a candidate to confirm it is really an image. Never downloads."""
        try:
            async with session.head(
                item.image_url,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=min(5.0, self.timeout)),
            ) as response:
                if response.status != 200:
                    return False
                content_type = str(response.headers.get("Content-Type", "")).lower()
                if not content_type.startswith("image/"):
                    return False
                length = _as_int(response.headers.get("Content-Length"))
                if length and length > 20 * 1024 * 1024:
                    return False
                item.mime = content_type.split(";")[0].strip()
                return True
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - an unprobeable URL is simply skipped
            log.debug("Image probe failed", exc_info=True)
            return False


def _iter_dicts(value: Any) -> list[dict[str, Any]]:
    """Normalize MediaWiki/REST collections (list *or* keyed object)."""
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [item for item in value.values() if isinstance(item, dict)]
    return []


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


__all__ = ["ImageSearchError", "ImageSearchService", "is_safe_image_url"]
