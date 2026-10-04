"""Jikan — the key-less MyAnimeList scraper API (best-effort source).

``https://api.jikan.moe/v4`` provides the *actual* MyAnimeList score,
rank, member count, studios, broadcast, streaming links and the
franchise/relations graph. It is rate-limited fairly tightly (~3 req/s),
so this source is deliberately the last anime opinion asked — every
failure is fine and simply means the MAL line is absent from the card.
"""

from __future__ import annotations

import logging
from typing import Any

from ..http import HttpError, HttpClient
from ..models import Identity, MediaKind, Rating, ReleaseStatus
from ..textutil import clean_summary, title_match_score

log = logging.getLogger(__name__)

BASE = "https://api.jikan.moe/v4"

_TYPE_KINDS = {
    "TV": MediaKind.ANIME,
    "Movie": MediaKind.ANIME_MOVIE,
    "OVA": MediaKind.OVA,
    "ONA": MediaKind.ONA,
    "Special": MediaKind.SPECIAL,
    "Music": MediaKind.SPECIAL,
}

_STATUS_MAP = {
    "Currently Airing": ReleaseStatus.AIRING,
    "Finished Airing": ReleaseStatus.COMPLETED,
    "Not yet aired": ReleaseStatus.UPCOMING,
}

_RELATION_MAP = {
    "sequel": "sequel",
    "prequel": "prequel",
    "spin-off": "spin-off",
    "side story": "side story",
    "parent story": "part of",
    "alternative version": "alternative version",
    "adaptation": "adaptation",
    "summary": "summary",
    "character": "related",
    "other": "related",
    "alternative setting": "alternative version",
    "full story": "part of",
    "same setting": "related",
}


def kind_from_type(type_text: str | None) -> MediaKind:
    return _TYPE_KINDS.get(str(type_text or "").strip(), MediaKind.ANIME)


def map_status(status: str | None) -> ReleaseStatus:
    return _STATUS_MAP.get(str(status or "").strip(), ReleaseStatus.UNKNOWN)


def map_relation(relation: str | None) -> str:
    return _RELATION_MAP.get(str(relation or "").strip().lower(), "related")


def identity_from_anime(anime: dict[str, Any]) -> Identity:
    titles = [str(anime.get(k) or "") for k in ("title_english", "title_japanese")]
    titles += [str(t.get("title") or "") for t in (anime.get("titles") or []) if isinstance(t, dict)]
    display = str(anime.get("title") or "") or next((t for t in titles if t), "")
    aired = (anime.get("aired") or {}).get("from") or ""
    year = int(aired[:4]) if aired[:4].isdigit() else None

    popularity = 0.0
    try:
        members = int(anime.get("members") or 0)
        if members > 1_000_000:
            popularity = 100.0
        elif members > 400_000:
            popularity = 88.0
        elif members > 100_000:
            popularity = 72.0
        elif members > 20_000:
            popularity = 55.0
        elif members:
            popularity = 35.0
    except (TypeError, ValueError):
        pass

    seen: set[str] = set()
    alt: list[str] = []
    for title in titles:
        if title and title != display and title not in seen:
            seen.add(title)
            alt.append(title)

    return Identity(
        title=display,
        kind=kind_from_type(anime.get("type")),
        year=year,
        mal_id=anime.get("mal_id"),
        poster=str(((anime.get("images") or {}).get("jpg") or {}).get("large_image_url") or ""),
        popularity=popularity,
        alt_titles=alt[:6],
        source="MyAnimeList",
    )


class JikanSource:
    def __init__(self, client: HttpClient, *, timeout: float = 7.0) -> None:
        self.client = client
        self.timeout = timeout

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        data = await self.client.get_json(
            f"{BASE}{path}", params=params, timeout=self.timeout
        )
        return data.get("data") if isinstance(data, dict) else None

    async def search(self, query: str, limit: int = 6) -> list[dict[str, Any]]:
        try:
            data = await self._get("/anime", {"q": query, "limit": limit, "sfw": "true"})
        except HttpError as error:
            log.debug("Jikan search failed for %r: %s", query, error)
            return []
        return [a for a in data if isinstance(a, dict)] if isinstance(data, list) else []

    async def best_match(self, query: str, threshold: float = 82.0) -> dict[str, Any] | None:
        for anime in await self.search(query):
            identity = identity_from_anime(anime)
            score = title_match_score(query, identity.title, identity.alt_titles)
            if score >= threshold:
                return anime
        return None

    async def anime_full(self, mal_id: int) -> dict[str, Any] | None:
        try:
            return await self._get(f"/anime/{int(mal_id)}/full")
        except (HttpError, TypeError, ValueError) as error:
            log.debug("Jikan full fetch failed: %s", error)
            return None

    async def recommendations(self, mal_id: int, limit: int = 8) -> list[dict[str, Any]]:
        try:
            data = await self._get(f"/anime/{int(mal_id)}/recommendations")
        except (HttpError, TypeError, ValueError) as error:
            log.debug("Jikan recommendations failed: %s", error)
            return []
        if not isinstance(data, list):
            return []
        out = []
        for item in data[:limit]:
            entry = item.get("entry") if isinstance(item, dict) else None
            if isinstance(entry, dict):
                out.append(entry)
        return out

    async def top_airing(self, limit: int = 10) -> list[dict[str, Any]]:
        try:
            data = await self._get("/top/anime", {"filter": "airing", "limit": limit, "sfw": "true"})
        except HttpError as error:
            log.debug("Jikan top airing failed: %s", error)
            return []
        return [a for a in data if isinstance(a, dict)] if isinstance(data, list) else []

    async def season_upcoming(self, limit: int = 10) -> list[dict[str, Any]]:
        try:
            data = await self._get("/seasons/upcoming", {"limit": limit, "sfw": "true"})
        except HttpError as error:
            log.debug("Jikan upcoming season failed: %s", error)
            return []
        return [a for a in data if isinstance(a, dict)] if isinstance(data, list) else []


def jikan_rating(anime: dict[str, Any]) -> Rating | None:
    score = anime.get("score")
    if score is None:
        return None
    try:
        return Rating(
            source="MyAnimeList",
            value=float(score),
            scale="/10",
            votes=anime.get("scored_by"),
        )
    except (TypeError, ValueError):
        return None


def anime_facts(anime: dict[str, Any]) -> dict[str, Any]:
    """Flatten a Jikan anime into the fields the details builder wants."""
    aired = anime.get("aired") or {}
    return {
        "kind": kind_from_type(anime.get("type")),
        "status": map_status(anime.get("status")),
        "episodes": anime.get("episodes"),
        "duration_text": str(anime.get("duration") or ""),
        "start": str(aired.get("from") or ""),
        "end": str(aired.get("to") or ""),
        "synopsis": clean_summary(anime.get("synopsis") or ""),
        "rating": jikan_rating(anime),
        "rank": anime.get("rank"),
        "popularity_rank": anime.get("popularity"),
        "members": anime.get("members"),
        "season": anime.get("season") or "",
        "year": anime.get("year"),
        "studios": [str(s.get("name") or "") for s in (anime.get("studios") or []) if isinstance(s, dict)],
        "producers": [str(p.get("name") or "") for p in (anime.get("producers") or []) if isinstance(p, dict)],
        "licensors": [str(l.get("name") or "") for l in (anime.get("licensors") or []) if isinstance(l, dict)],
        "genres": [str(g.get("name") or "") for g in (anime.get("genres") or []) if isinstance(g, dict)],
        "themes": [str(t.get("name") or "") for t in (anime.get("themes") or []) if isinstance(t, dict)],
        "streaming": [
            {"name": str(s.get("name") or ""), "url": str(s.get("url") or "")}
            for s in (anime.get("streaming") or [])
            if isinstance(s, dict)
        ],
        "relations": [
            {
                "relation": str(r.get("relation") or ""),
                "entries": [
                    {"name": str(e.get("name") or ""), "type": str(e.get("type") or ""), "mal_id": e.get("mal_id")}
                    for e in (r.get("entry") or [])
                    if isinstance(e, dict)
                ],
            }
            for r in (anime.get("relations") or [])
            if isinstance(r, dict)
        ],
        "trailer": (anime.get("trailer") or {}).get("url") or None
        if isinstance(anime.get("trailer"), dict)
        else None,
        "poster": str(((anime.get("images") or {}).get("jpg") or {}).get("large_image_url") or ""),
    }
