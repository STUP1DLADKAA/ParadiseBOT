"""Apple's iTunes Search service — key-less movie metadata, best-effort.

``https://itunes.apple.com/search`` and the RSS "top movies" feed work
without any key. For films this adds artwork, a short description, the
genre and the digital release date. The movie storefront is picky about
traffic, so every failure here is fine: Wikipedia and the web layer
already carry the card.
"""

from __future__ import annotations

import logging
from typing import Any

from ..http import HttpError, HttpClient
from ..models import Identity, MediaKind

log = logging.getLogger(__name__)

SEARCH_URL = "https://itunes.apple.com/search"
RSS_URL = "https://rss.itunes.apple.com/api/v1/{country}/movies/top-movies/all/{limit}/explicit.json"

_KIND_FROM_WRAPPER = {
    "movie": MediaKind.MOVIE,
}


def identity_from_track(track: dict[str, Any]) -> Identity | None:
    name = str(track.get("trackName") or "").strip()
    if not name:
        return None
    year = None
    released = str(track.get("releaseDate") or "")
    if released[:4].isdigit():
        year = int(released[:4])
    return Identity(
        title=name,
        kind=MediaKind.MOVIE,
        year=year,
        poster=str(track.get("artworkUrl100") or "").replace("100x100bb", "600x600bb"),
        popularity=30.0,  # present in the storefront, modestly popular
        alt_titles=[str(track.get("artistName") or "")] if track.get("artistName") else [],
        source="Apple TV",
    )


class ITunesSource:
    def __init__(self, client: HttpClient, *, timeout: float = 6.0) -> None:
        self.client = client
        self.timeout = timeout

    async def search_movie(self, title: str, *, country: str = "US", limit: int = 5) -> list[dict[str, Any]]:
        try:
            data = await self.client.get_json(
                SEARCH_URL,
                params={
                    "term": title,
                    "media": "movie",
                    "entity": "movie",
                    "limit": str(limit),
                    "country": country.upper(),
                },
                timeout=self.timeout,
            )
        except HttpError as error:
            log.debug("iTunes movie search failed for %r: %s", title, error)
            return []
        results = data.get("results") if isinstance(data, dict) else None
        return [r for r in results if isinstance(r, dict)] if isinstance(results, list) else []

    async def top_movies(self, *, country: str = "US", limit: int = 15) -> list[dict[str, Any]]:
        """The current Apple TV 'top movies' chart — a live popularity list."""
        try:
            data = await self.client.get_json(
                RSS_URL.format(country=country.lower(), limit=max(10, min(50, limit))),
                timeout=self.timeout,
            )
        except HttpError as error:
            log.debug("iTunes top movies failed: %s", error)
            return []
        feed = data.get("feed") if isinstance(data, dict) else None
        results = (feed or {}).get("results") if isinstance(feed, dict) else None
        return [r for r in results if isinstance(r, dict)] if isinstance(results, list) else []

    @staticmethod
    def track_facts(track: dict[str, Any]) -> dict[str, Any]:
        runtime_ms = track.get("trackTimeMillis")
        runtime = None
        if isinstance(runtime_ms, (int, float)) and runtime_ms > 0:
            runtime = int(runtime_ms / 60000)
        return {
            "title": str(track.get("trackName") or ""),
            "genre": str(track.get("primaryGenreName") or ""),
            "description": str(track.get("longDescription") or track.get("shortDescription") or ""),
            "release_date": str(track.get("releaseDate") or "")[:10],
            "runtime": runtime,
            "poster": str(track.get("artworkUrl100") or "").replace("100x100bb", "600x600bb"),
            "director": str(track.get("artistName") or ""),
            "url": str(track.get("trackViewUrl") or "") or None,
            "content_rating": str(track.get("contentAdvisoryRating") or ""),
        }
