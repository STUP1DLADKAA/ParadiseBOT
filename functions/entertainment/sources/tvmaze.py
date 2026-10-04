"""TVMaze — the key-less TV & web-series database.

``https://api.tvmaze.com`` gives us search, show details (genres, status,
runtime, network vs. streaming web channel, user rating), season lists,
and — crucially — a lookup straight from an IMDb id, plus per-day
schedules including web-only premieres. No key, generous limits.
"""

from __future__ import annotations

import logging
from typing import Any

from ..http import HttpError, HttpClient
from ..models import DateValue, Identity, MediaKind, Rating, ReleaseStatus
from ..textutil import strip_html

log = logging.getLogger(__name__)

BASE = "https://api.tvmaze.com"

_STATUS_MAP = {
    "running": ReleaseStatus.AIRING,
    "ended": ReleaseStatus.COMPLETED,
    "canceled": ReleaseStatus.CANCELLED,
    "in development": ReleaseStatus.UPCOMING,
    "to be determined": ReleaseStatus.UPCOMING,
}


def map_status(status: str | None) -> ReleaseStatus:
    if not status:
        return ReleaseStatus.UNKNOWN
    return _STATUS_MAP.get(status.strip().lower(), ReleaseStatus.UNKNOWN)


def kind_of_show(show: dict[str, Any]) -> MediaKind:
    """A TVMaze show is a *web series* when it lives on a streamer."""
    web_channel = show.get("webChannel")
    network = show.get("network")
    show_type = str(show.get("type") or "").lower()
    genres = {str(g).lower() for g in (show.get("genres") or [])}
    if "anime" in genres and network is None:
        # Anime on a web channel keeps its anime identity (handled by the
        # resolver too); TVMaze still supplies episode data.
        return MediaKind.WEB_SERIES
    if web_channel and not network:
        return MediaKind.WEB_SERIES
    if show_type == "documentary":
        return MediaKind.DOCUMENTARY
    if web_channel and network:
        return MediaKind.TV_SERIES
    if web_channel:
        return MediaKind.WEB_SERIES
    return MediaKind.TV_SERIES


def _rating(show: dict[str, Any]) -> Rating | None:
    average = ((show.get("rating") or {}).get("average")) if isinstance(show.get("rating"), dict) else None
    if average is None:
        return None
    try:
        return Rating(source="TVmaze", value=float(average), scale="/10")
    except (TypeError, ValueError):
        return None


def identity_from_show(show: dict[str, Any]) -> Identity:
    externals = show.get("externals") or {}
    premiered = str(show.get("premiered") or "")
    year = int(premiered[:4]) if premiered[:4].isdigit() else None
    web_channel = show.get("webChannel")
    popularity = _weight_to_popularity(show.get("weight"))
    if web_channel and not show.get("network"):
        popularity = max(popularity, 55.0)  # streamer originals trend high
    return Identity(
        title=str(show.get("name") or ""),
        kind=kind_of_show(show),
        year=year,
        imdb_id=str(externals.get("imdb") or ""),
        tvmaze_id=show.get("id"),
        poster=str(((show.get("image") or {}).get("original")) or ""),
        popularity=popularity,
        source="TVMaze",
    )


def _weight_to_popularity(weight: Any) -> float:
    try:
        weight = int(weight or 0)
    except (TypeError, ValueError):
        weight = 0
    # TVMaze weights run 0–100.
    return max(0.0, min(100.0, float(weight)))


class TvMazeSource:
    def __init__(self, client: HttpClient, *, timeout: float = 6.0) -> None:
        self.client = client
        self.timeout = timeout

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self.client.get_json(
            f"{BASE}{path}", params=params, timeout=self.timeout
        )

    async def search_shows(self, query: str, limit: int = 8) -> list[tuple[Identity, dict[str, Any]]]:
        try:
            data = await self._get_json("/search/shows", {"q": query})
        except HttpError as error:
            log.debug("TVMaze search failed for %r: %s", query, error)
            return []
        results: list[tuple[Identity, dict[str, Any]]] = []
        if not isinstance(data, list):
            return results
        for item in data[:limit]:
            if isinstance(item, dict) and isinstance(item.get("show"), dict):
                show = item["show"]
                identity = identity_from_show(show)
                if identity.title:
                    results.append((identity, show))
        return results

    async def lookup_by_imdb(self, imdb_id: str) -> dict[str, Any] | None:
        if not imdb_id.startswith("tt"):
            return None
        try:
            return await self._get_json("/lookup/shows", {"imdb": imdb_id})
        except HttpError as error:
            log.debug("TVMaze imdb lookup failed for %s: %s", imdb_id, error)
            return None

    async def show(self, tvmaze_id: int) -> dict[str, Any] | None:
        try:
            return await self._get_json(f"/shows/{int(tvmaze_id)}")
        except (HttpError, TypeError, ValueError) as error:
            log.debug("TVMaze show fetch failed: %s", error)
            return None

    async def seasons(self, tvmaze_id: int) -> list[dict[str, Any]]:
        try:
            data = await self._get_json(f"/shows/{int(tvmaze_id)}/seasons")
        except (HttpError, TypeError, ValueError) as error:
            log.debug("TVMaze seasons fetch failed: %s", error)
            return []
        return [season for season in data if isinstance(season, dict)] if isinstance(data, list) else []

    async def schedule(
        self,
        *,
        day: str,
        country: str | None = None,
        web_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Episodes airing on a given ISO date. ``web_only`` → streamer premieres."""
        path = "/schedule/web" if web_only else "/schedule"
        params: dict[str, Any] = {"date": day}
        if country and not web_only:
            params["country"] = country.upper()
        try:
            data = await self._get_json(path, params)
        except HttpError as error:
            log.debug("TVMaze schedule failed: %s", error)
            return []
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def show_facts(show: dict[str, Any]) -> dict[str, Any]:
    """Flatten a TVMaze show into the fields the details builder wants."""
    network = show.get("network") or {}
    web_channel = show.get("webChannel") or {}
    channel = network or web_channel
    channel_country = (channel.get("country") or {}).get("name") or ""
    premiered = DateValue.from_iso(str(show.get("premiered") or ""), "TVMaze")
    ended = DateValue.from_iso(str(show.get("ended") or ""), "TVMaze")
    return {
        "name": show.get("name") or "",
        "kind": kind_of_show(show),
        "genres": [str(g) for g in (show.get("genres") or [])],
        "status": map_status(show.get("status")),
        "runtime": show.get("averageRuntime") or show.get("runtime"),
        "premiered": premiered,
        "ended": ended,
        "rating": _rating(show),
        "summary": strip_html(show.get("summary") or ""),
        "poster": (show.get("image") or {}).get("original") or "",
        "network": network.get("name") or "",
        "web_channel": web_channel.get("name") or "",
        "channel_country": channel_country,
        "language": show.get("language") or "",
        "official_site": show.get("officialSite") or None,
        "type": show.get("type") or "",
        "imdb_id": (show.get("externals") or {}).get("imdb") or "",
    }
