"""Kitsu — key-less anime database (part of the Hummingbird ecosystem).

``https://kitsu.app/api/edge`` is a JSON:API: anime search by text,
upcoming / airing filters, its own user ratings and popularity ranks,
multi-language titles (including Japanese), subtype (TV / MOVIE / OVA /
ONA / SPECIAL), episode counts, lengths and the YouTube trailer id.
"""

from __future__ import annotations

import logging
from typing import Any

from ..http import HttpError, HttpClient
from ..models import Identity, MediaKind, Rating, ReleaseStatus
from ..textutil import clean_summary, title_match_score

log = logging.getLogger(__name__)

BASE = "https://kitsu.app/api/edge"

_SUBTYPE_KINDS = {
    "TV": MediaKind.ANIME,
    "MOVIE": MediaKind.ANIME_MOVIE,
    "OVA": MediaKind.OVA,
    "ONA": MediaKind.ONA,
    "SPECIAL": MediaKind.SPECIAL,
    "MUSIC": MediaKind.SPECIAL,
}

_STATUS_MAP = {
    "current": ReleaseStatus.AIRING,
    "finished": ReleaseStatus.COMPLETED,
    "upcoming": ReleaseStatus.UPCOMING,
    "unreleased": ReleaseStatus.UPCOMING,
    "tba": ReleaseStatus.UPCOMING,
}


def kind_from_subtype(subtype: str | None) -> MediaKind:
    return _SUBTYPE_KINDS.get(str(subtype or "").upper(), MediaKind.ANIME)


def map_status(status: str | None) -> ReleaseStatus:
    return _STATUS_MAP.get(str(status or "").strip().lower(), ReleaseStatus.UNKNOWN)


def _popularity(popularity_rank: Any, user_count: Any) -> float:
    """Kitsu popularity rank + user count → a 0–100 score."""
    score = 0.0
    try:
        rank = int(popularity_rank or 0)
        if 0 < rank <= 50:
            score = 100.0
        elif 0 < rank <= 200:
            score = 90.0
        elif 0 < rank <= 800:
            score = 75.0
        elif 0 < rank <= 3000:
            score = 55.0
        elif rank > 0:
            score = 35.0
    except (TypeError, ValueError):
        pass
    try:
        count = int(user_count or 0)
        if count > 50_000:
            score = max(score, 85.0)
        elif count > 10_000:
            score = max(score, 70.0)
        elif count > 2_000:
            score = max(score, 50.0)
    except (TypeError, ValueError):
        pass
    return score


def _titles_of(entry: dict[str, Any]) -> list[str]:
    titles = entry.get("titles") or {}
    out: list[str] = []
    for key in ("en", "en_jp", "en_us", "ja_jp"):
        value = str(titles.get(key) or "").strip()
        if value and value not in out:
            out.append(value)
    canonical = str(entry.get("canonicalTitle") or "").strip()
    if canonical and canonical not in out:
        out.append(canonical)
    for alt in (entry.get("abbreviatedTitles") or []):
        value = str(alt or "").strip()
        if value and value not in out:
            out.append(value)
    return out


def identity_from_entry(entry: dict[str, Any], anime_id: Any = None) -> Identity:
    titles = _titles_of(entry)
    display = (
        entry.get("titles", {}).get("en")
        or entry.get("titles", {}).get("en_jp")
        or entry.get("canonicalTitle")
        or ""
    )
    start = str(entry.get("startDate") or "")
    year = int(start[:4]) if start[:4].isdigit() else None
    return Identity(
        title=str(display),
        kind=kind_from_subtype(entry.get("subtype")),
        year=year,
        kitsu_id=str(anime_id or entry.get("slug") or ""),
        poster=str((entry.get("posterImage") or {}).get("large")
                   or (entry.get("posterImage") or {}).get("original") or ""),
        popularity=_popularity(entry.get("popularityRank"), entry.get("userCount")),
        alt_titles=[t for t in titles if t != display],
        source="Kitsu",
    )


def kitsu_rating(entry: dict[str, Any]) -> Rating | None:
    average = entry.get("averageRating")
    if average is None:
        return None
    try:
        value = float(average)
    except (TypeError, ValueError):
        return None
    users = entry.get("userCount")
    return Rating(
        source="Kitsu",
        value=round(value / 10.0, 1),  # Kitsu is 0–100; show it as /10
        scale="/10",
        votes=int(users) if users else None,
    )


class KitsuSource:
    def __init__(self, client: HttpClient, *, timeout: float = 6.0) -> None:
        self.client = client
        self.timeout = timeout

    async def _list(self, params: dict[str, Any], limit: int = 10) -> list[dict[str, Any]]:
        params = dict(params)
        params.setdefault("page[limit]", str(limit))
        params.setdefault("page[offset]", "0")
        try:
            data = await self.client.get_json(BASE + "/anime", params=params, timeout=self.timeout)
        except HttpError as error:
            log.debug("Kitsu request failed (%s): %s", params, error)
            return []
        entries = data.get("data") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            return []
        out: list[dict[str, Any]] = []
        for item in entries:
            if isinstance(item, dict) and isinstance(item.get("attributes"), dict):
                out.append(item["attributes"])
        return out

    async def search(self, query: str, limit: int = 8) -> list[Identity]:
        out: list[Identity] = []
        for anime_id, entry in await self._raw_search(query, limit=limit):
            identity = identity_from_entry(entry, anime_id)
            if identity.title:
                out.append(identity)
        return out

    async def best_match(self, query: str, threshold: float = 82.0) -> tuple[Identity, dict[str, Any]] | None:
        """Search Kitsu and return the entry that actually matches the query."""
        raw = await self._raw_search(query)
        best: tuple[float, Identity, dict[str, Any]] | None = None
        for anime_id, entry in raw:
            identity = identity_from_entry(entry, anime_id)
            if not identity.title:
                continue
            score = title_match_score(query, identity.title, identity.alt_titles)
            if best is None or score > best[0]:
                best = (score, identity, entry)
        if best is None or best[0] < threshold:
            return None
        return best[1], best[2]

    async def _raw_search(self, query: str, limit: int = 8) -> list[tuple[str, dict[str, Any]]]:
        params: dict[str, Any] = {"filter[text]": query, "page[limit]": str(limit)}
        try:
            data = await self.client.get_json(BASE + "/anime", params=params, timeout=self.timeout)
        except HttpError as error:
            log.debug("Kitsu search failed for %r: %s", query, error)
            return []
        entries = data.get("data") if isinstance(data, dict) else None
        out: list[tuple[str, dict[str, Any]]] = []
        if isinstance(entries, list):
            for item in entries:
                if isinstance(item, dict) and isinstance(item.get("attributes"), dict):
                    out.append((str(item.get("id") or ""), item["attributes"]))
        return out

    async def trending(self, limit: int = 10) -> list[tuple[Identity, dict[str, Any]]]:
        """Most-popular currently airing anime (Kitsu's own ranking)."""
        entries = await self._list({"filter[status]": "current", "sort": "-userCount"}, limit=limit)
        return [
            (identity_from_entry(e), e) for e in entries if identity_from_entry(e).title
        ]

    async def upcoming(self, limit: int = 10) -> list[tuple[Identity, dict[str, Any]]]:
        entries = await self._list({"filter[status]": "upcoming", "sort": "-userCount"}, limit=limit)
        return [
            (identity_from_entry(e), e) for e in entries if identity_from_entry(e).title
        ]

    async def top_rated(self, limit: int = 10) -> list[tuple[Identity, dict[str, Any]]]:
        entries = await self._list({"sort": "-averageRating", "filter[status]": "finished"}, limit=limit)
        return [
            (identity_from_entry(e), e) for e in entries if identity_from_entry(e).title
        ]


def entry_facts(entry: dict[str, Any]) -> dict[str, Any]:
    """Flatten a Kitsu anime into the fields the details builder wants."""
    rating = kitsu_rating(entry)
    return {
        "titles": _titles_of(entry),
        "kind": kind_from_subtype(entry.get("subtype")),
        "status": map_status(entry.get("status")),
        "synopsis": clean_summary(entry.get("synopsis") or ""),
        "episodes": entry.get("episodeCount"),
        "episode_length": entry.get("episodeLength"),
        "total_length": entry.get("totalLength"),
        "start_date": str(entry.get("startDate") or ""),
        "end_date": str(entry.get("endDate") or ""),
        "rating": rating,
        "rating_rank": entry.get("ratingRank"),
        "popularity_rank": entry.get("popularityRank"),
        "age_rating": entry.get("ageRatingGuide") or entry.get("ageRating") or "",
        "trailer_youtube": entry.get("youtubeVideoId") or "",
        "poster": (entry.get("posterImage") or {}).get("large")
        or (entry.get("posterImage") or {}).get("original") or "",
        "subtype": entry.get("subtype") or "",
    }
