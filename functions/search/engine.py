"""Web search engine supporting keyless lookups with robust fallbacks and caching."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from urllib.parse import parse_qs, unquote, urlparse

import aiohttp

from .models import EMPTY, OK, PROVIDER_ERROR, SearchResponse, SearchResult

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Known authoritative domains get slight ranking boost
AUTHORITATIVE_DOMAINS = {
    "wikipedia.org", "en.wikipedia.org", "developer.mozilla.org", "docs.python.org",
    "github.com", "stackoverflow.com", "w3schools.com", "britannica.com",
    "arxiv.org", "nature.com", "nih.gov", "nasa.gov", "who.int", "bbc.com", "reuters.com",
}


def _strip_html(text: str) -> str:
    """Strip HTML tags and unescape entities."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _unwrap_ddg_url(raw_url: str) -> str:
    """Unwrap DuckDuckGo click-tracking redirect URL."""
    if not raw_url:
        return ""
    if raw_url.startswith("//"):
        raw_url = "https:" + raw_url
    if "duckduckgo.com/l/?" in raw_url:
        try:
            parsed = urlparse(raw_url)
            params = parse_qs(parsed.query)
            if "uddg" in params and params["uddg"]:
                return unquote(params["uddg"][0])
        except Exception:
            pass
    return raw_url


class _ProviderStatus(Exception):
    """A provider answered with a non-200 status (rate limit, block, outage)."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


class SearchEngine:
    """Searches the web via multiple reliable fallbacks."""

    def __init__(self, cache_ttl: int = 900, *, timeout: float = 8.0) -> None:
        self.cache_ttl = cache_ttl
        self.timeout = max(2.0, float(timeout))
        self._cache: dict[str, tuple[float, SearchResponse]] = {}
        # Identical requests arriving together share one fallback chain rather
        # than opening several sessions and hitting the providers repeatedly.
        self._inflight: dict[str, asyncio.Task[SearchResponse]] = {}
        # One shared keep-alive session for every provider call. Opening a
        # session per request wastes a TLS handshake on each lookup and
        # leaks connectors under load.
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Shared HTTP session
    # ------------------------------------------------------------------

    async def session(self) -> aiohttp.ClientSession:
        """The shared session, created on first use."""
        if self._session is not None and not self._session.closed:
            return self._session
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                    headers={"User-Agent": USER_AGENT},
                )
        return self._session

    async def close(self) -> None:
        """Release the shared session (called on shutdown)."""
        session, self._session = self._session, None
        if session is not None and not session.closed:
            try:
                await session.close()
            except Exception:  # noqa: BLE001 - shutdown is best effort
                log.debug("Could not close the search session", exc_info=True)

    async def search(self, query: str, limit: int = 8) -> SearchResponse:
        """Search the web for query, returning ranked results."""
        query = query.strip()
        if not query:
            return SearchResponse(query=query, results=[])

        cache_key = query.lower()
        now = time.monotonic()
        cached = self._cache.get(cache_key)
        if cached is not None:
            cached_time, cached_resp = cached
            if now - cached_time < self.cache_ttl:
                return SearchResponse(
                    query=cached_resp.query,
                    results=cached_resp.results[:limit],
                    engine=cached_resp.engine,
                    cached=True,
                    status=cached_resp.status,
                )

        task = self._inflight.get(cache_key)
        if task is None:
            task = asyncio.create_task(
                self._search_uncached(query, cache_key, now, limit),
                name=f"search:{cache_key[:40]}",
            )
            self._inflight[cache_key] = task

            def clear_inflight(done: asyncio.Task[SearchResponse]) -> None:
                if self._inflight.get(cache_key) is done:
                    self._inflight.pop(cache_key, None)
                # A cancelled caller can be the only waiter. Observe a task
                # failure here so asyncio does not emit an unhelpful warning.
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(clear_inflight)

        # Shielding keeps one cancelled Discord interaction from cancelling
        # the provider request for other identical waiters.
        response = await asyncio.shield(task)
        return SearchResponse(
            query=response.query,
            results=response.results[:limit],
            engine=response.engine,
            cached=response.cached,
            status=response.status,
        )

    async def _search_uncached(
        self, query: str, cache_key: str, started_at: float, limit: int
    ) -> SearchResponse:
        """Run one provider fallback chain for a cache key."""
        # Provider fallback chain. Each failure is recorded so the caller
        # can tell "nothing matched" apart from "every provider is down".
        results: list[SearchResult] = []
        engine_name = "Web"
        providers = (
            ("DuckDuckGo", self._search_duckduckgo_lite),
            ("DuckDuckGo", self._search_duckduckgo_html),
            ("DuckDuckGo Instant", self._search_duckduckgo_api),
            ("Wikipedia", self._search_wikipedia),
        )
        reachable = False
        for name, provider in providers:
            if results:
                break
            try:
                found = await provider(query)
                reachable = True
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - try the next provider
                # Technical detail stays in the log; users never see it.
                log.debug("%s search failed: %s", name, err)
                continue
            if found:
                results = found
                engine_name = name

        # Post-process, rank and deduplicate
        cleaned = self._clean_and_rank(results, query, limit)
        if cleaned:
            status = OK
        elif reachable:
            status = EMPTY
        else:
            status = PROVIDER_ERROR
        response = SearchResponse(
            query=query, results=cleaned, engine=engine_name, status=status
        )

        if cleaned:
            self._cache[cache_key] = (started_at, response)
            # Bound cache size
            if len(self._cache) > 256:
                oldest = min(self._cache.keys(), key=lambda k: self._cache[k][0])
                self._cache.pop(oldest, None)

        return response

    async def _search_duckduckgo_lite(self, query: str) -> list[SearchResult]:
        """Fetch results from DuckDuckGo Lite."""
        url = "https://lite.duckduckgo.com/lite/"
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {"q": query}
        session = await self.session()
        async with session.post(url, data=data, headers=headers) as resp:
            if resp.status != 200:
                raise _ProviderStatus(resp.status)
            html_text = await resp.text(errors="replace")

        return self._parse_ddg_lite_html(html_text)

    def _parse_ddg_lite_html(self, html_text: str) -> list[SearchResult]:
        results: list[SearchResult] = []
        # In lite.duckduckgo.com, results are in table rows.
        # Links have class "result-link" or appear in <td><a class="result-link" href="...">...</a>
        # Snippets have class "result-snippet"
        link_matches = re.finditer(
            r'<a[^>]+class=[\'"]result-link[\'"][^>]+href=[\'"]([^\'"]+)[\'"][^>]*>(.*?)</a>',
            html_text,
            re.IGNORECASE | re.DOTALL,
        )
        snippet_matches = re.findall(
            r'<td[^>]+class=[\'"]result-snippet[\'"][^>]*>(.*?)</td>',
            html_text,
            re.IGNORECASE | re.DOTALL,
        )

        links = list(link_matches)
        for i, link_match in enumerate(links):
            raw_url = link_match.group(1)
            raw_title = link_match.group(2)
            url = _unwrap_ddg_url(raw_url)
            title = _strip_html(raw_title)
            snippet = ""
            if i < len(snippet_matches):
                snippet = _strip_html(snippet_matches[i])

            if url and title and url.startswith("http"):
                results.append(SearchResult(title=title, url=url, snippet=snippet))

        return results

    async def _search_duckduckgo_html(self, query: str) -> list[SearchResult]:
        """Fetch results from DuckDuckGo HTML endpoint."""
        url = "https://html.duckduckgo.com/html/"
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {"q": query, "b": ""}
        session = await self.session()
        async with session.post(url, data=data, headers=headers) as resp:
            if resp.status != 200:
                raise _ProviderStatus(resp.status)
            html_text = await resp.text(errors="replace")

        return self._parse_ddg_html(html_text)

    def _parse_ddg_html(self, html_text: str) -> list[SearchResult]:
        results: list[SearchResult] = []
        # HTML results contain blocks: <div class="result ..."> ... <a class="result__url" ...>
        # <a class="result__a" href="...">title</a>
        # <a class="result__snippet" ...>snippet</a>
        blocks = re.findall(
            r'<div[^>]+class=[\'"][^\'"]*result\b[^\'"]*[\'"][^>]*>(.*?)</div>\s*(?=<div[^>]+class=[\'"][^\'"]*result\b|$)',
            html_text,
            re.IGNORECASE | re.DOTALL,
        )
        for block in blocks:
            url_match = re.search(
                r'<a[^>]+class=[\'"][^\'"]*result__a[^\'"]*[\'"][^>]+href=[\'"]([^\'"]+)[\'"][^>]*>(.*?)</a>',
                block,
                re.IGNORECASE | re.DOTALL,
            )
            if not url_match:
                continue

            raw_url = url_match.group(1)
            raw_title = url_match.group(2)
            url = _unwrap_ddg_url(raw_url)
            title = _strip_html(raw_title)

            snippet = ""
            snip_match = re.search(
                r'<a[^>]+class=[\'"][^\'"]*result__snippet[^\'"]*[\'"][^>]*>(.*?)</a>',
                block,
                re.IGNORECASE | re.DOTALL,
            )
            if snip_match:
                snippet = _strip_html(snip_match.group(1))

            if url and title and url.startswith("http"):
                results.append(SearchResult(title=title, url=url, snippet=snippet))

        return results

    async def _search_duckduckgo_api(self, query: str) -> list[SearchResult]:
        """Fetch instant answer / related topics from DuckDuckGo JSON API."""
        url = "https://api.duckduckgo.com/"
        params = {"q": query, "format": "json", "no_html": "1", "skip_disambig": "0"}
        headers = {"User-Agent": USER_AGENT}
        session = await self.session()
        async with session.get(url, params=params, headers=headers) as resp:
            if resp.status != 200:
                raise _ProviderStatus(resp.status)
            data = await resp.json(content_type=None)

        results: list[SearchResult] = []
        # Check abstract
        abstract = data.get("AbstractText", "")
        abstract_url = data.get("AbstractURL", "")
        abstract_source = data.get("AbstractSource", "")
        heading = data.get("Heading", query)
        if abstract and abstract_url:
            results.append(
                SearchResult(
                    title=f"{heading} ({abstract_source})" if abstract_source else heading,
                    url=abstract_url,
                    snippet=abstract,
                    source=abstract_source,
                    authoritative=True,
                )
            )

        # Check RelatedTopics
        for topic in data.get("RelatedTopics", []):
            if isinstance(topic, dict) and "FirstURL" in topic and "Text" in topic:
                first_url = topic.get("FirstURL", "")
                text = topic.get("Text", "")
                if first_url and text:
                    # Often format is "Title - Snippet"
                    parts = text.split(" - ", 1)
                    title = parts[0] if len(parts) > 1 else text[:60]
                    snippet = parts[1] if len(parts) > 1 else text
                    results.append(
                        SearchResult(title=title, url=first_url, snippet=snippet)
                    )

        return results

    async def _search_wikipedia(self, query: str) -> list[SearchResult]:
        """Fetch encyclopedia summary using Wikipedia API."""
        url = "https://en.wikipedia.org/w/api.php"
        params = {
            "action": "opensearch",
            "search": query,
            "limit": "5",
            "namespace": "0",
            "format": "json",
        }
        headers = {"User-Agent": USER_AGENT}
        session = await self.session()
        async with session.get(url, params=params, headers=headers) as resp:
            if resp.status != 200:
                raise _ProviderStatus(resp.status)
            data = await resp.json(content_type=None)

        results: list[SearchResult] = []
        # Format: [query, [titles], [descriptions], [urls]]
        if isinstance(data, list) and len(data) >= 4:
            titles = data[1]
            descriptions = data[2]
            urls = data[3]
            for i in range(min(len(titles), len(urls))):
                title = titles[i]
                desc = descriptions[i] if i < len(descriptions) else ""
                w_url = urls[i]
                if title and w_url:
                    results.append(
                        SearchResult(
                            title=title,
                            url=w_url,
                            snippet=desc or f"Wikipedia article on {title}",
                            source="wikipedia.org",
                            authoritative=True,
                        )
                    )
        return results

    def _clean_and_rank(
        self, results: list[SearchResult], query: str, limit: int
    ) -> list[SearchResult]:
        """Deduplicate, mark authoritative, and limit."""
        seen_urls: set[str] = set()
        cleaned: list[SearchResult] = []

        for item in results:
            if not item.url or not item.title:
                continue
            normalized_url = item.url.rstrip("/")
            if normalized_url in seen_urls:
                continue
            seen_urls.add(normalized_url)

            # Check if domain is authoritative
            source_lower = item.source.lower()
            if any(auth in source_lower for auth in AUTHORITATIVE_DOMAINS):
                item.authoritative = True

            # Shorten snippet if too long
            if len(item.snippet) > 300:
                item.snippet = item.snippet[:297].rstrip() + "…"

            cleaned.append(item)

        # Authoritative items slightly prioritized if query matches
        return cleaned[:limit]
