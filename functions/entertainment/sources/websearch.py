"""The live-web layer: verification, streaming detection, "more like this".

This wraps the bot's existing key-less search engine (DuckDuckGo with
Wikipedia fallback — the same one ``/search`` uses) and adds the
entertainment-specific reading of its results:

- **rating hunt** — pull an IMDb / Rotten Tomatoes score out of search
  snippets, always attributed as "via live web search", only ever from
  the matching domain (never from a random page);
- **streaming detection** — only name a platform when its *official*
  domain shows up in results for the title (+ optional region words);
- **similarity mining** — harvest candidate titles from "movies like X"
  listicles and cross-check each against IMDb before believing it.

If the web is unreachable, every function quietly returns empty results
and the caller reports that the fact could not be verified right now.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any

from functions.search.engine import SearchEngine

from ..models import Rating, StreamingOffer
from ..textutil import normalize_title

log = logging.getLogger(__name__)

# domain → display name, for streaming detection. Only official domains
# count — we never guess a platform from a blog's mention.
PLATFORM_DOMAINS = {
    "netflix.com": "Netflix",
    "primevideo.com": "Prime Video",
    "amazon.com": "Prime Video",
    "disneyplus.com": "Disney+",
    "hotstar.com": "Disney+ Hotstar",
    "jiohotstar.com": "JioHotstar",
    "max.com": "Max",
    "hbomax.com": "Max",
    "hulu.com": "Hulu",
    "peacocktv.com": "Peacock",
    "paramountplus.com": "Paramount+",
    "appletv.com": "Apple TV+",
    "tv.apple.com": "Apple TV+",
    "crunchyroll.com": "Crunchyroll",
    "hidive.com": "HIDIVE",
    "funimation.com": "Crunchyroll (formerly Funimation)",
    "sonycrackle.com": "Crackle",
    "tubitv.com": "Tubi",
    "pluto.tv": "Pluto TV",
    "plex.tv": "Plex",
    "youtube.com": "YouTube",
    "rottentomatoes.com": "Rotten Tomatoes",
    "sonyliv.com": "SonyLIV",
    "zee5.com": "ZEE5",
    "aha.video": "Aha",
    "mxplayer.in": "MX Player",
    "jiocinema.com": "JioCinema",
    "voot.com": "Voot",
    "mubi.com": "MUBI",
    "stan.com.au": "Stan",
    "crave.ca": "Crave",
    "bet.plus": "BET+",
    "acorn.tv": "Acorn TV",
    "britbox.com": "BritBox",
    "iq.com": "iQIYI",
    "viki.com": "Rakuten Viki",
    "viz.com": "VIZ",
    "adultswim.com": "Adult Swim",
    "cc.com": "Comedy Central",
    "amcplus.com": "AMC+",
    "shudder.com": "Shudder",
    "criterionchannel.com": "The Criterion Channel",
    "discoveryplus.com": "discovery+",
    "espn.com": "ESPN",
}

REGION_HINTS = {
    "india": ("india", "in", "hindi"),
    "us": ("united states", "usa", "us", "america"),
    "uk": ("united kingdom", "uk", "britain"),
    "canada": ("canada", "ca"),
    "australia": ("australia", "au"),
    "germany": ("germany", "de"),
    "japan": ("japan", "jp"),
}

_IMDB_SNIPPET_RE = re.compile(r"(?:rating|rated|score)?\s*:?\s*([0-9]\.\d)\s*/\s*10", re.IGNORECASE)
_RT_SNIPPET_RE = re.compile(r"([0-9]{1,3})\s*%", re.IGNORECASE)

_RATING_SOURCES = ("imdb.com", "rottentomatoes.com", "metacritic.com")


def _trim_candidate(chunk: str) -> str:
    """Cut a chunk at the first noise word, keeping the title head.

    "The Martian top this list of mind-bending sci-fi" → "The Martian".
    The IMDb verification step discards heads that aren't real titles.
    """
    words = chunk.split()
    for index, word in enumerate(words):
        if word.lower() in (
            "top", "best", "that", "which", "this", "these", "those",
            "including", "plus", "with", "from", "fans", "you", "if",
            "on", "here", "check", "watch",
        ) and index > 0:
            return " ".join(words[:index])
    return chunk


class WebSearchSource:
    """Reading entertainment facts out of the live web, carefully."""

    def __init__(self, engine: SearchEngine | None = None) -> None:
        self.engine = engine or SearchEngine()

    async def _search(self, query: str, limit: int = 8) -> list[Any]:
        try:
            response = await self.engine.search(query, limit=limit)
            return list(response.results)
        except Exception as error:  # noqa: BLE001 - the web is optional
            log.debug("Web search failed for %r: %s", query, error)
            return []

    # -- ratings ---------------------------------------------------------------

    async def find_rating(self, title: str, year: int | None = None, *, source: str = "imdb") -> Rating | None:
        """Look for one named rating in live search snippets.

        Only snippets from the rating service's own domain are trusted,
        and the result is always labelled 'via live web search'.
        """
        source = source.lower()
        if source == "imdb":
            domain = "imdb.com"
            query = f"{title} {year or ''} imdb rating".strip()
        elif source in ("rotten tomatoes", "rt", "rottentomatoes"):
            domain = "rottentomatoes.com"
            query = f"{title} {year or ''} rotten tomatoes rating".strip()
        else:
            return None

        for result in await self._search(query, limit=6):
            host = (result.source or "").lower()
            if domain not in host:
                continue
            snippet = f"{result.title} {result.snippet}"
            if source == "imdb":
                match = _IMDB_SNIPPET_RE.search(snippet)
                if match:
                    try:
                        value = float(match.group(1))
                    except ValueError:
                        continue
                    if 0 < value <= 10:
                        return Rating(
                            source="IMDb",
                            value=value,
                            scale="/10",
                            via="live web search",
                            url=result.url,
                        )
            else:
                match = _RT_SNIPPET_RE.search(snippet)
                if match:
                    try:
                        value = float(match.group(1))
                    except ValueError:
                        continue
                    if 0 < value <= 100:
                        return Rating(
                            source="Rotten Tomatoes",
                            value=value,
                            scale="%",
                            via="live web search",
                            url=result.url,
                        )
        return None

    # -- streaming ---------------------------------------------------------------

    async def find_streaming(self, title: str, *, region: str = "") -> list[StreamingOffer]:
        """Platforms whose official domains appear in live search results.

        ``region`` narrows the query ("India") and is echoed back in the
        offer so the card can state exactly what was checked.
        """
        region_words = f" in {region}" if region else ""
        query = f"where to watch {title}{region_words} streaming"
        results = await self._search(query, limit=10)
        offers: list[StreamingOffer] = []
        seen: set[str] = set()
        for result in results:
            host = (result.source or "").lower()
            for domain, name in PLATFORM_DOMAINS.items():
                if domain in host and name not in seen:
                    seen.add(name)
                    offers.append(
                        StreamingOffer(
                            platform=name,
                            region=region,
                            url=result.url,
                            source="live web search",
                        )
                    )
        # Also count official pages that appear in titles/snippets even
        # when the search engine's source field missed the domain.
        if not offers:
            for result in results:
                haystack = f"{result.url} {result.title}"
                for domain, name in PLATFORM_DOMAINS.items():
                    if domain in haystack.lower() and name not in seen:
                        seen.add(name)
                        offers.append(
                            StreamingOffer(
                                platform=name,
                                region=region,
                                url=result.url if domain in result.url.lower() else None,
                                source="live web search",
                            )
                        )
        return offers

    # -- "more like this" -------------------------------------------------------

    async def mine_similar_titles(self, title: str, limit: int = 12) -> list[str]:
        """Harvest candidate titles from 'similar to X' pages.

        Returns raw candidate names; the caller verifies each against
        IMDb before showing anything.
        """
        results = await self._search(f"movies and shows like {title}", limit=10)
        candidates: Counter[str] = Counter()
        stop = {normalize_title(title), "the"}
        for result in results:
            text = f"{result.title} {result.snippet}"
            # Phrases like "10 Movies Like Interstellar" or list snippets
            # "Shows like Breaking Bad: Better Call Saul, …"
            for chunk in re.split(r"[|•·,;:!?()\[\]\"']", text):
                chunk = chunk.strip().lstrip("(-").rstrip(").")
                if not chunk or chunk.isdigit():
                    continue
                chunk = _trim_candidate(chunk)
                if not 3 <= len(chunk) <= 60:
                    continue
                lowered = chunk.lower()
                if any(
                    bad in lowered
                    for bad in (
                        "like ", "movies", "shows", "series", "best", "top", "ranker",
                        " imdb", "watch", "streaming", "similar", "list", "films",
                        "https", "www", ".com", "reviews", "guide", "picks", "bored",
                        "staring", "starring", "cast", "plot", "trailer", "if you",
                        "you'll", "loved", "fans of", "updated", "december", "january",
                        "february", "march", "april", "may", "june", "july", "august",
                        "september", "october", "november",
                    )
                ):
                    continue
                if chunk.isdigit():
                    continue
                norm = normalize_title(chunk)
                if norm in stop or not norm:
                    continue
                candidates[chunk] += 1
        return [name for name, _ in candidates.most_common(limit)]

    # -- general live answers ------------------------------------------------------

    async def find_links(self, query: str, limit: int = 5) -> list[Any]:
        """Plain results, used for 'is X renewed' and news-y questions."""
        return await self._search(query, limit=limit)
