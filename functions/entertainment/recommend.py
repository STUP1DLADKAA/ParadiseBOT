"""The Recommendation Engine: "more like this", with reasons.

Four strategies, blended and always explained:

1. **franchise graph** — sequels, prequels, spin-offs, side stories and
   adaptations of the seed title (AniList relations, Jikan relations,
   Wikipedia's preceded_by/followed_by, Wikidata's series membership);
2. **community recommendations** — what AniList / MAL users who liked
   the seed also liked;
3. **genre + era discovery** — structured Wikidata queries ("notable
   thrillers released since 2023") and AniList attribute queries
   (short anime, underrated picks);
4. **live web mining** — "movies like X" listicles, cross-checked
   against IMDb before a name is believed.

Every pick carries a short *why* (shared genres, same franchise, score,
how often the web names it) — no random popular dumps.
"""

from __future__ import annotations

import logging

from .models import (
    Identity,
    MediaKind,
    Rating,
    RelatedTitle,
)
from .sources.anilist import AniListSource, map_relation
from .sources.imdb import ImdbSource
from .sources.jikan import JikanSource
from .sources.kitsu import KitsuSource
from .sources.websearch import WebSearchSource
from .sources.wikidata import GENRE_QIDS, WikidataSource
from .textutil import normalize_title
from datetime import date, timedelta

log = logging.getLogger(__name__)

# Genre words we can turn into a structured Wikidata query.
_GENRE_ALIASES = {
    "sci-fi": "science fiction",
    "scifi": "science fiction",
    "sf": "science fiction",
    "rom com": "romantic comedy",
    "romcom": "romantic comedy",
    "psychological": "psychological thriller",
    "historical": "historical drama",
}

_ANILIST_ATTR_QUERY = """
query ($perPage: Int, $episodes_lesser: Int, $popularity_lesser: Int, $genre: [String]) {
  Page(perPage: $perPage) {
    media(
      type: ANIME
      sort: SCORE_DESC
      episodes_lesser: $episodes_lesser
      popularity_lesser: $popularity_lesser
      genre_in: $genre
    ) {
      id title { english romaji } format averageScore episodes seasonYear popularity
      genres studios(isMain: true) { nodes { name } }
    }
  }
}
"""


class RecommendationEngine:
    def __init__(
        self,
        imdb: ImdbSource,
        anilist: AniListSource,
        jikan: JikanSource,
        kitsu: KitsuSource,
        wikidata: WikidataSource,
        web: WebSearchSource,
    ) -> None:
        self.imdb = imdb
        self.anilist = anilist
        self.jikan = jikan
        self.kitsu = kitsu
        self.wikidata = wikidata
        self.web = web

    # ------------------------------------------------------------------
    # Seed-title recommendations
    # ------------------------------------------------------------------

    async def recommend_like(
        self,
        identity: Identity,
        *,
        tone: str = "",
        limit: int = 6,
        seed_genres: list[str] | None = None,
    ) -> list[RelatedTitle]:
        picks: list[RelatedTitle] = []
        seen: set[str] = {normalize_title(identity.title)}

        def offer(item: RelatedTitle) -> RelatedTitle | None:
            key = normalize_title(item.title)
            if not key or key in seen:
                return None
            seen.add(key)
            return item

        # 1 & 2 — anime community + franchise graph.
        media = await self.anilist.best_match(identity.title)
        if media:
            for edge in ((media.get("relations") or {}).get("edges") or []):
                node = edge.get("node") or {}
                label = (node.get("title") or {}).get("english") or (node.get("title") or {}).get("romaji")
                if label and str(node.get("type") or "") == "ANIME":
                    pick = offer(
                        RelatedTitle(
                            title=str(label),
                            kind=MediaKind.ANIME,
                            year=(node.get("startDate") or {}).get("year"),
                            relation=map_relation(edge.get("relationType")),
                            why="same franchise",
                            url=f"https://anilist.co/anime/{node.get('id')}/" if node.get("id") else None,
                        )
                    )
                    if pick:
                        picks.append(pick)
            for node in ((media.get("recommendations") or {}).get("nodes") or []):
                preview = node.get("mediaPreview") or {}
                label = (preview.get("title") or {}).get("english") or (preview.get("title") or {}).get("romaji")
                if not label:
                    continue
                score = preview.get("averageScore")
                why = "recommended by AniList users"
                if tone:
                    why += f", matches your “{tone}” ask"
                pick = offer(
                    RelatedTitle(
                        title=str(label),
                        kind=MediaKind.ANIME,
                        year=preview.get("seasonYear"),
                        relation="similar",
                        why=why,
                        rating=Rating(source="AniList", value=round(score / 10, 1), scale="/10") if score else None,
                    )
                )
                if pick:
                    picks.append(pick)

        if media and media.get("idMal"):
            for entry in await self.jikan.recommendations(int(media["idMal"]), limit=8):
                score = entry.get("score")
                pick = offer(
                    RelatedTitle(
                        title=str(entry.get("title") or ""),
                        kind=MediaKind.ANIME,
                        relation="similar",
                        why="frequently recommended by MyAnimeList users",
                        url=str(entry.get("url") or "") or None,
                        rating=None,
                    )
                )
                if pick:
                    picks.append(pick)

        # 3 — genre + era discovery through Wikidata for live-action picks.
        if len(picks) < limit and not identity.kind.is_anime:
            picks.extend(
                await self._genre_discovery(identity, seed_genres or [], limit=limit - len(picks))
            )

        # 4 — the live web ("movies like X" listicles), verified on IMDb.
        if len(picks) < limit:
            picks.extend(await self._web_mined(identity, limit=limit - len(picks)))

        return picks[:limit]

    # ------------------------------------------------------------------
    # Attribute-based recommendations (no seed title)
    # ------------------------------------------------------------------

    async def recommend_by_attributes(
        self,
        *,
        kind: MediaKind | None = None,
        genre: str = "",
        era: str = "",  # "recent", "this year", "classic", "2020s"
        tone: str = "",  # "darker", "uplifting", "underrated", "short"
        max_episodes: int | None = None,
        limit: int = 6,
    ) -> tuple[list[RelatedTitle], str]:
        """Returns (picks, explanation-of-strategy)."""
        picks: list[RelatedTitle] = []
        strategy_bits: list[str] = []

        wants_anime = kind is not None and kind.is_anime
        wants_live_action = kind is not None and not kind.is_anime

        if wants_anime or (kind is None and (max_episodes or genre in {"shonen", "isekai"} or "anime" in (tone + genre))):
            strategy_bits.append("anime databases filtered by your criteria")
            picks.extend(
                await self._anilist_by_attributes(
                    genre=genre, tone=tone, max_episodes=max_episodes, limit=limit
                )
            )

        if (wants_live_action or kind is None) and len(picks) < limit:
            genre_key = self._genre_key(genre)
            if genre_key:
                strategy_bits.append(f"Wikidata's most notable {genre_key} films of the era")
                years = _era_window(era)
                rows = await self.wikidata.genre_films(
                    GENRE_QIDS[genre_key],
                    since=years[0],
                    until=years[1],
                    limit=max(limit * 3, 12),
                )
                for row in rows:
                    if len(picks) >= limit:
                        break
                    picks.append(
                        RelatedTitle(
                            title=row["title"],
                            kind=MediaKind.MOVIE,
                            year=row["when"].year if row["when"] else None,
                            relation="similar",
                            why=f"highly notable {genre_key} film"
                            + (f" ({row['when'].year})" if row["when"] else ""),
                        )
                    )

        if len(picks) < limit and not genre:
            strategy_bits.append("what the live web rates highly right now")
            picks.extend(await self._web_top_picks(limit=limit - len(picks)))

        explanation = "; ".join(strategy_bits) or "live web picks"
        return picks[:limit], explanation

    async def _anilist_by_attributes(
        self,
        *,
        genre: str = "",
        tone: str = "",
        max_episodes: int | None = None,
        limit: int = 6,
    ) -> list[RelatedTitle]:
        genre_filter = [genre.title()] if genre and genre.casefold() != "anime" else None
        popularity_cap = 60_000 if "underrated" in (tone or "").casefold() else None
        try:
            data = await self.anilist.raw_query(
                _ANILIST_ATTR_QUERY,
                {
                    "perPage": max(limit * 2, 12),
                    "episodes_lesser": (max_episodes + 1) if max_episodes else None,
                    "popularity_lesser": popularity_cap,
                    "genre": genre_filter,
                },
            )
        except Exception as error:  # noqa: BLE001 - AniList is optional
            log.debug("AniList attribute query failed: %s", error)
            return []
        media = ((data or {}).get("Page") or {}).get("media") or []
        picks: list[RelatedTitle] = []
        for item in media:
            if not isinstance(item, dict):
                continue
            label = (item.get("title") or {}).get("english") or (item.get("title") or {}).get("romaji")
            if not label:
                continue
            score = item.get("averageScore")
            why = "highly rated on AniList"
            if popularity_cap:
                why = "highly rated but relatively under-watched (AniList)"
            if max_episodes:
                why = f"short series ({item.get('episodes')} episodes), rated {round((score or 0) / 10, 1)}/10 on AniList"
            studios = ((item.get("studios") or {}).get("nodes") or [])
            if studios and isinstance(studios[0], dict):
                why += f" · studio: {studios[0].get('name')}"
            picks.append(
                RelatedTitle(
                    title=str(label),
                    kind=MediaKind.ANIME,
                    year=item.get("seasonYear"),
                    relation="similar",
                    why=why,
                    rating=Rating(source="AniList", value=round(score / 10, 1), scale="/10") if score else None,
                )
            )
            if len(picks) >= limit:
                break
        return picks

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _genre_discovery(
        self, identity: Identity, seed_genres: list[str], *, limit: int
    ) -> list[RelatedTitle]:
        genre_key = ""
        for candidate in seed_genres:
            key = self._genre_key(candidate)
            if key:
                genre_key = key
                break
        if not genre_key:
            return []
        years = _era_window("recent")
        rows = await self.wikidata.genre_films(
            GENRE_QIDS[genre_key],
            since=years[0],
            until=years[1],
            limit=max(limit * 3, 9),
        )
        picks: list[RelatedTitle] = []
        for row in rows:
            picks.append(
                RelatedTitle(
                    title=row["title"],
                    kind=MediaKind.MOVIE,
                    year=row["when"].year if row["when"] else None,
                    relation="similar",
                    why=f"notable {genre_key} release",
                )
            )
            if len(picks) >= limit:
                break
        return picks

    async def _web_mined(self, identity: Identity, *, limit: int) -> list[RelatedTitle]:
        candidates = await self.web.mine_similar_titles(identity.title, limit=8)
        if not candidates:
            return []
        picks: list[RelatedTitle] = []
        verified: list[RelatedTitle] = []
        import asyncio

        async def check(name: str) -> RelatedTitle | None:
            results = await self.imdb.suggest(name, limit=3)
            for result in results:
                if result.is_media and normalize_title(result.title) == normalize_title(name):
                    return RelatedTitle(
                        title=result.title,
                        kind=result.kind,
                        year=result.year,
                        relation="similar",
                        why="frequently named alongside it on the web",
                        rating=None,
                        popularity=result.popularity,
                        url=f"https://www.imdb.com/title/{result.imdb_id}/" if result.imdb_id else None,
                    )
            return None

        found = await asyncio.gather(*[check(name) for name in candidates[:6]], return_exceptions=True)
        for item in found:
            if isinstance(item, RelatedTitle):
                verified.append(item)
        verified.sort(key=lambda p: p.popularity, reverse=True)
        seen: set[str] = set()
        for pick in verified:
            key = normalize_title(pick.title)
            if key in seen:
                continue
            seen.add(key)
            picks.append(pick)
            if len(picks) >= limit:
                break
        return picks

    async def _web_top_picks(self, *, limit: int) -> list[RelatedTitle]:
        results = await self.web.find_links("best movies and shows to watch right now", limit=6)
        picks: list[RelatedTitle] = []
        for result in results[:limit]:
            picks.append(
                RelatedTitle(
                    title=result.title,
                    relation="similar",
                    why="from live web best-of lists",
                    url=result.url,
                )
            )
        return picks

    @staticmethod
    def _genre_key(genre: str) -> str | None:
        text = (genre or "").strip().casefold()
        if not text:
            return None
        text = _GENRE_ALIASES.get(text, text)
        for key in GENRE_QIDS:
            if text in (key, key + "s") or key in text:
                return key
        return None


def _era_window(era: str) -> tuple[date, date]:
    today = date.today()
    text = (era or "").casefold()
    if "recent" in text or "new" in text:
        return today - timedelta(days=3 * 365), today + timedelta(days=1)
    if "this year" in text:
        return date(today.year, 1, 1), today + timedelta(days=1)
    if "classic" in text or "old" in text:
        return date(1960, 1, 1), date(2000, 1, 1)
    if "2020" in text:
        return date(2020, 1, 1), today + timedelta(days=1)
    return today - timedelta(days=6 * 365), today + timedelta(days=1)
