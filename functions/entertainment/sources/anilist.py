"""AniList's public GraphQL API — enrichment for anime queries.

``https://graphql.anilist.co`` answers plain POSTs with no key. It is our
best source for *live* anime signals: a real trending rank, the next
airing episode countdown, region-tagged external streaming links,
franchise relations and community recommendations.

It is treated as optional everywhere: if AniList is slow or rate-limited
(their limits have tightened over the years), Kitsu/TVMaze/Wikipedia
still produce a complete card.
"""

from __future__ import annotations

import logging
from typing import Any

from ..http import HttpError, HttpClient
from ..models import Identity, MediaKind, Rating, ReleaseStatus
from ..textutil import clean_summary, title_match_score

log = logging.getLogger(__name__)

ENDPOINT = "https://graphql.anilist.co"

_FORMAT_KINDS = {
    "TV": MediaKind.ANIME,
    "TV_SHORT": MediaKind.ANIME,
    "MOVIE": MediaKind.ANIME_MOVIE,
    "SPECIAL": MediaKind.SPECIAL,
    "OVA": MediaKind.OVA,
    "ONA": MediaKind.ONA,
    "MUSIC": MediaKind.SPECIAL,
}

_STATUS_MAP = {
    "RELEASING": ReleaseStatus.AIRING,
    "FINISHED": ReleaseStatus.COMPLETED,
    "NOT_YET_RELEASED": ReleaseStatus.UPCOMING,
    "CANCELLED": ReleaseStatus.CANCELLED,
    "HIATUS": ReleaseStatus.HIATUS,
}

_RELATION_MAP = {
    "SEQUEL": "sequel",
    "PREQUEL": "prequel",
    "SPIN_OFF": "spin-off",
    "SIDE_STORY": "side story",
    "PARENT": "part of",
    "ALTERNATIVE": "alternative version",
    "ADAPTATION": "adaptation",
    "SUMMARY": "summary",
    "SOURCE": "adaptation",
    "OTHER": "related",
    "CHARACTER": "related",
    "CONTAINS": "related",
}

_MEDIA_FIELDS = """
  id
  idMal
  title { romaji english native }
  synonyms
  format
  status
  episodes
  duration
  season
  seasonYear
  startDate { year month day }
  endDate { year month day }
  genres
  description(asHtml: false)
  averageScore
  popularity
  trending
  favourites
  studios(isMain: true) { nodes { name } }
  nextAiringEpisode { episode airingAt }
  externalLinks { site url type language }
  trailer { id site }
  coverImage { extraLarge }
  rankings { rank type context year allTime }
"""

_SEARCH_QUERY = (
    "query ($search: String) { Page(perPage: 10) { media(search: $search, type: ANIME) {"
    + _MEDIA_FIELDS
    + "relations { edges { relationType node { id type format title { romaji english } startDate { year } } } }"
    + "recommendations(perPage: 8, sort: RATING_DESC) { nodes { rating mediaPreview { id title { english romaji } format averageScore seasonYear } } }"
    + " } } }"
)

_TRENDING_QUERY = (
    "query ($perPage: Int) { Page(perPage: $perPage) {"
    + " media(type: ANIME, sort: TRENDING_DESC) {" + _MEDIA_FIELDS + " } } }"
)

_UPCOMING_QUERY = (
    "query ($perPage: Int, $greater: FuzzyDateInt, $sort: [MediaSort]) { Page(perPage: $perPage) {"
    " media(type: ANIME, status: NOT_YET_RELEASED, startDate_greater: $greater, sort: $sort) {"
    + _MEDIA_FIELDS + " } } }"
)


def kind_from_format(fmt: str | None) -> MediaKind:
    return _FORMAT_KINDS.get(str(fmt or "").upper(), MediaKind.ANIME)


def map_status(status: str | None) -> ReleaseStatus:
    return _STATUS_MAP.get(str(status or "").upper(), ReleaseStatus.UNKNOWN)


def map_relation(relation: str | None) -> str:
    return _RELATION_MAP.get(str(relation or "").upper(), "related")


def _title(media: dict[str, Any]) -> str:
    titles = media.get("title") or {}
    return str(titles.get("english") or titles.get("romaji") or titles.get("native") or "")


def identity_from_media(media: dict[str, Any]) -> Identity:
    start = media.get("startDate") or {}
    year = start.get("year")
    alt: list[str] = []
    titles = media.get("title") or {}
    for key in ("romaji", "native"):
        value = str(titles.get(key) or "").strip()
        if value and value != _title(media):
            alt.append(value)
    for synonym in media.get("synonyms") or []:
        value = str(synonym or "").strip()
        if value and value not in alt:
            alt.append(value)

    popularity = 0.0
    if media.get("trending"):
        popularity = 100.0
    pop = media.get("popularity") or 0
    if pop > 200_000:
        popularity = max(popularity, 95.0)
    elif pop > 60_000:
        popularity = max(popularity, 80.0)
    elif pop > 20_000:
        popularity = max(popularity, 65.0)
    elif pop:
        popularity = max(popularity, 40.0)

    return Identity(
        title=_title(media),
        kind=kind_from_format(media.get("format")),
        year=int(year) if year else None,
        anilist_id=media.get("id"),
        mal_id=media.get("idMal"),
        poster=str((media.get("coverImage") or {}).get("extraLarge") or ""),
        popularity=popularity,
        alt_titles=alt[:6],
        source="AniList",
    )


def anilist_rating(media: dict[str, Any]) -> Rating | None:
    score = media.get("averageScore")
    if score is None:
        return None
    try:
        return Rating(source="AniList", value=round(float(score) / 10.0, 1), scale="/10")
    except (TypeError, ValueError):
        return None


class AniListSource:
    def __init__(self, client: HttpClient, *, timeout: float = 7.0) -> None:
        self.client = client
        self.timeout = timeout

    async def raw_query(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Run any GraphQL query against AniList (used by the recommender)."""
        return await self._query(query, variables)

    async def _query(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any] | None:
        try:
            data = await self.client.post_json(
                ENDPOINT,
                {"query": query, "variables": variables or {}},
                timeout=self.timeout,
            )
        except HttpError as error:
            log.debug("AniList query failed: %s", error)
            return None
        if not isinstance(data, dict):
            return None
        if data.get("errors"):
            log.debug("AniList returned errors: %s", data["errors"][:1])
        return data.get("data") or {}

    async def search(self, query: str) -> list[dict[str, Any]]:
        data = await self._query(_SEARCH_QUERY, {"search": query})
        page = (data or {}).get("Page") or {}
        media = page.get("media")
        return [m for m in media if isinstance(m, dict)] if isinstance(media, list) else []

    async def best_match(self, query: str, threshold: float = 82.0) -> dict[str, Any] | None:
        """The AniList media that actually matches the query, with relations."""
        for media in await self.search(query):
            identity = identity_from_media(media)
            score = title_match_score(query, identity.title, identity.alt_titles)
            if score >= threshold:
                return media
        return None

    async def trending(self, limit: int = 10) -> list[dict[str, Any]]:
        data = await self._query(_TRENDING_QUERY, {"perPage": limit})
        page = (data or {}).get("Page") or {}
        media = page.get("media")
        return [m for m in media if isinstance(m, dict)] if isinstance(media, list) else []

    async def upcoming(self, limit: int = 10, greater: int = 20_000_000) -> list[dict[str, Any]]:
        data = await self._query(
            _UPCOMING_QUERY,
            {"perPage": limit, "greater": greater, "sort": ["POPULARITY_DESC"]},
        )
        page = (data or {}).get("Page") or {}
        media = page.get("media")
        return [m for m in media if isinstance(m, dict)] if isinstance(media, list) else []


def media_facts(media: dict[str, Any]) -> dict[str, Any]:
    """Flatten AniList media into the fields the details builder wants."""
    start = media.get("startDate") or {}
    end = media.get("endDate") or {}
    rankings = media.get("rankings") or []
    highest_rated_rank = None
    for ranking in rankings:
        if str(ranking.get("type") or "") == "RATED":
            highest_rated_rank = ranking.get("rank")
            break
    return {
        "titles": [
            t
            for t in (
                (media.get("title") or {}).get("romaji"),
                (media.get("title") or {}).get("native"),
                *(media.get("synonyms") or []),
            )
            if t
        ],
        "kind": kind_from_format(media.get("format")),
        "status": map_status(media.get("status")),
        "episodes": media.get("episodes"),
        "episode_length": media.get("duration"),
        "start": start,
        "end": end,
        "genres": [str(g) for g in (media.get("genres") or [])],
        "synopsis": clean_summary(str(media.get("description") or "")),
        "rating": anilist_rating(media),
        "trending": media.get("trending") or 0,
        "popularity": media.get("popularity") or 0,
        "highest_rated_rank": highest_rated_rank,
        "studios": [
            str(node.get("name"))
            for node in ((media.get("studios") or {}).get("nodes") or [])
            if isinstance(node, dict)
        ],
        "next_episode": media.get("nextAiringEpisode") or None,
        "external_links": [
            link
            for link in (media.get("externalLinks") or [])
            if isinstance(link, dict)
        ],
        "trailer": media.get("trailer") or None,
        "poster": str((media.get("coverImage") or {}).get("extraLarge") or ""),
    }
