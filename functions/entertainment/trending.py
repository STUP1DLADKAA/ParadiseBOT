"""The Trending Engine: what is actually hot *right now*.

Trending, popular and highest-rated are different questions and get
different, honestly-labelled answers:

- **trending** — live attention signals: Wikipedia's most-read
  film/series articles yesterday (real reader behaviour) and AniList's
  live trending rank;
- **popular** — current charts: the most-watched airing anime, the
  Apple TV top-movies chart, streamer premieres tonight;
- **highest rated** — Jikan's top airing by MAL score / Kitsu's
  top-rated finished.

Everything is fetched live — no static lists anywhere — and each row
carries its metric and source so the user can see *why* it's listed.
"""

from __future__ import annotations

import logging
from datetime import date

from .models import MediaKind, TrendingEntry
from .sources.anilist import AniListSource, identity_from_media, media_facts
from .sources.itunes import ITunesSource
from .sources.jikan import JikanSource, identity_from_anime, jikan_rating
from .sources.kitsu import KitsuSource, kitsu_rating
from .sources.tvmaze import TvMazeSource, identity_from_show
from .sources.wikipedia import WikipediaSource

log = logging.getLogger(__name__)

TRENDING = "trending"
POPULAR = "popular"
TOP_RATED = "top rated"


class TrendingEngine:
    def __init__(
        self,
        wikipedia: WikipediaSource,
        anilist: AniListSource,
        kitsu: KitsuSource,
        jikan: JikanSource,
        tvmaze: TvMazeSource,
        itunes: ITunesSource,
    ) -> None:
        self.wikipedia = wikipedia
        self.anilist = anilist
        self.kitsu = kitsu
        self.jikan = jikan
        self.tvmaze = tvmaze
        self.itunes = itunes

    async def trending(
        self,
        *,
        category: str = "all",  # all | movies | series | anime | web
        window: str = "",  # "", "this week", "this season"
        mode: str = TRENDING,
        limit: int = 8,
    ) -> tuple[list[TrendingEntry], list[str]]:
        """Returns (entries, source-notes). Entries are pre-deduplicated."""
        category = (category or "all").strip().casefold()
        notes: set[str] = set()
        entries: list[TrendingEntry] = []
        seen: set[str] = set()

        def add(entry: TrendingEntry) -> None:
            key = entry.title.casefold()
            if key and key not in seen:
                seen.add(key)
                entries.append(entry)

        wants_anime = category in ("anime", "all")
        wants_movies = category in ("movies", "movie", "all")
        wants_series = category in ("series", "tv", "shows", "web", "web series", "all")

        # 1. Wikipedia most-read — the genuine buzz signal.
        if (wants_movies or wants_series) and mode in (TRENDING, POPULAR):
            try:
                most_viewed = await self.wikipedia.most_viewed()
            except Exception as error:  # noqa: BLE001 - optional source
                log.debug("Wikipedia most-viewed failed: %s", error)
                most_viewed = []
            for row in most_viewed:
                kind = row["kind"]
                is_movie = kind is MediaKind.MOVIE
                is_show = kind.is_series or kind is MediaKind.MINISERIES
                if not ((wants_movies and is_movie) or (wants_series and is_show)):
                    continue
                add(
                    TrendingEntry(
                        title=row["title"],
                        kind=kind,
                        metric=f"{row['views'] / 1000:.0f}k reads on Wikipedia ({row['date']})",
                        list_kind=TRENDING if mode is TRENDING else POPULAR,
                        url=f"https://en.wikipedia.org/wiki/{row['article']}",
                        source="Wikipedia most-read",
                    )
                )
            if most_viewed:
                notes.add("Wikipedia most-read articles (yesterday's reader behaviour)")

        # 2. AniList trending / Kitsu popular airing / Jikan top airing.
        if wants_anime:
            anime_added = False
            if mode in (TRENDING, POPULAR):
                media = await self.anilist.trending(limit=max(limit, 6))
                for item in media:
                    identity = identity_from_media(item)
                    if not identity.title:
                        continue
                    facts = media_facts(item)
                    anime_added = True
                    add(
                        TrendingEntry(
                            title=identity.title,
                            kind=identity.kind,
                            year=identity.year,
                            metric=f"AniList trending score {facts['trending']}"
                            + (f" · {facts['popularity']:,} tracking" if facts["popularity"] else ""),
                            list_kind=TRENDING,
                            rating=facts["rating"],
                            url=f"https://anilist.co/anime/{identity.anilist_id}/" if identity.anilist_id else None,
                            source="AniList",
                        )
                    )
                if media:
                    notes.add("AniList's live trending ranking")
            if not anime_added:
                airing = await self.kitsu.trending(limit=max(limit, 6))
                for identity, entry in airing:
                    anime_added = True
                    add(
                        TrendingEntry(
                            title=identity.title,
                            kind=identity.kind,
                            year=identity.year,
                            metric="most-tracked airing anime (Kitsu)",
                            list_kind=POPULAR,
                            rating=kitsu_rating(entry),
                            source="Kitsu",
                        )
                    )
                if airing:
                    notes.add("Kitsu's most-tracked currently airing anime")
            if mode is TOP_RATED:
                top = await self.jikan.top_airing(limit=max(limit, 5))
                for anime in top:
                    identity = identity_from_anime(anime)
                    add(
                        TrendingEntry(
                            title=identity.title,
                            kind=identity.kind,
                            year=identity.year,
                            metric="highest-rated airing anime on MAL",
                            list_kind=TOP_RATED,
                            rating=jikan_rating(anime),
                            url=f"https://myanimelist.net/anime/{identity.mal_id}" if identity.mal_id else None,
                            source="MyAnimeList",
                        )
                    )
                if top:
                    notes.add("MyAnimeList top airing (score-ranked)")

        # 3. Apple TV top-movies chart (popularity, not quality).
        if wants_movies and mode in (TRENDING, POPULAR):
            tracks = await self.itunes.top_movies(limit=max(limit, 6))
            for track in tracks:
                name = str(track.get("name") or "")
                if not name:
                    continue
                add(
                    TrendingEntry(
                        title=name,
                        kind=MediaKind.MOVIE,
                        metric="current top movies chart (Apple TV, US)",
                        list_kind=POPULAR,
                        url=str(track.get("url") or "") or None,
                        source="Apple TV charts",
                    )
                )
            if tracks:
                notes.add("Apple TV top-movies chart (US storefront)")

        # 4. What's airing tonight (streamer premieres included).
        if wants_series and mode in (TRENDING, POPULAR):
            tonight = await self._airing_tonight(limit=6)
            for entry in tonight:
                add(entry)
            if tonight:
                notes.add("TVMaze schedule: popular shows with episodes airing today")

        # Order: trending first, then popular; keep only `limit`.
        order = {TRENDING: 0, POPULAR: 1, TOP_RATED: 2}
        entries.sort(key=lambda e: order.get(e.list_kind, 3))
        return entries[:limit], sorted(notes)

    async def _airing_tonight(self, *, limit: int) -> list[TrendingEntry]:
        today = date.today().isoformat()
        entries: list[TrendingEntry] = []
        seen: set[str] = set()
        for web_only in (True, False):
            try:
                items = await self.tvmaze.schedule(day=today, web_only=web_only)
            except Exception as error:  # noqa: BLE001 - optional source
                log.debug("TVMaze airing-today failed: %s", error)
                continue
            for item in items:
                show = (item.get("_embedded") or {}).get("show") or {}
                if not show:
                    continue
                identity = identity_from_show(show)
                if not identity.title or identity.title.casefold() in seen:
                    continue
                # Popular shows only, to keep the list meaningful.
                if identity.popularity < 60:
                    continue
                seen.add(identity.title.casefold())
                episode_label = f"S{item.get('season')}E{item.get('number')}"
                entries.append(
                    TrendingEntry(
                        title=identity.title,
                        kind=identity.kind,
                        year=identity.year,
                        metric=f"new episode today ({episode_label})",
                        list_kind=TRENDING,
                        rating=None,
                        url=str(show.get("url") or "") or None,
                        source="TVMaze",
                    )
                )
                if len(entries) >= limit:
                    return entries
        return entries
