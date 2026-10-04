"""The Data Extraction & assembly layer: identity → full title card.

One lookup fans out to every relevant source *in parallel*, then folds
the answers into a single :class:`MediaTitle`:

- TV/web series → TVMaze (core facts, seasons) + Wikipedia (infobox,
  reception) + anime databases when it turns out to be anime;
- movies → Wikipedia (infobox: director, cast, dates, budget) + IMDb
  page + iTunes for artwork;
- anime → Kitsu + AniList + Jikan (MAL) + Wikipedia + TVMaze;

…plus the live web layer for ratings verification, streaming detection
and a light "similar" row. Every source is optional: a lookup returns
whatever could be verified, with honest notes about the rest.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .models import (
    DateValue,
    Identity,
    MediaKind,
    MediaTitle,
    Person,
    Rating,
    RelatedTitle,
    ReleaseStatus,
    StreamingOffer,
    now_utc,
)
from .sources.anilist import AniListSource, map_relation, media_facts
from .sources.imdb import ImdbSource
from .sources.itunes import ITunesSource
from .sources.jikan import JikanSource, anime_facts
from .sources.kitsu import KitsuSource, entry_facts
from .sources.tvmaze import TvMazeSource, show_facts
from .sources.websearch import WebSearchSource
from .sources.wikipedia import WikipediaSource
from .textutil import (
    normalize_title,
    parse_film_date,
    parse_number,
    parse_runtime,
    similarity,
)
from .verify import dedupe_ratings, rating_sort_key, verify_title

log = logging.getLogger(__name__)

TOTAL_BUDGET = 12.0  # hard ceiling for the whole fan-out


class DetailsBuilder:
    def __init__(
        self,
        imdb: ImdbSource,
        tvmaze: TvMazeSource,
        kitsu: KitsuSource,
        anilist: AniListSource,
        jikan: JikanSource,
        wikipedia: WikipediaSource,
        itunes: ITunesSource,
        web: WebSearchSource,
    ) -> None:
        self.imdb = imdb
        self.tvmaze = tvmaze
        self.kitsu = kitsu
        self.anilist = anilist
        self.jikan = jikan
        self.wikipedia = wikipedia
        self.itunes = itunes
        self.web = web

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def build(self, identity: Identity, *, season: int | None = None) -> MediaTitle:
        title = MediaTitle(title=identity.title, kind=identity.kind, identity=identity)
        async with asyncio.timeout(TOTAL_BUDGET):
            await self._gather_all(identity, title)
        if season:
            title.note(f"showing the series overview — you asked about season {season}")
        self._finalize(identity, title)
        return title

    # ------------------------------------------------------------------
    # The fan-out
    # ------------------------------------------------------------------

    async def _gather_all(self, identity: Identity, title: MediaTitle) -> None:
        tasks: dict[str, Any] = {}

        is_series = identity.kind.is_series or identity.kind in (
            MediaKind.ANIME,
            MediaKind.OVA,
            MediaKind.ONA,
            MediaKind.SPECIAL,
        )
        is_movie = identity.kind in (MediaKind.MOVIE, MediaKind.ANIME_MOVIE, MediaKind.SHORT)

        if is_series:
            tasks["tvmaze"] = self._tvmaze_show(identity)
        if is_movie:
            tasks["itunes"] = self._itunes_movie(identity.title)
        # Anime databases are worth asking for any title — movie or
        # series — because IMDb kinds say nothing about anime-ness.
        tasks["kitsu"] = self.kitsu.best_match(identity.title)
        tasks["anilist"] = self.anilist.best_match(identity.title)
        tasks["wikipedia"] = self._wikipedia_bundle(identity)
        if identity.imdb_id:
            tasks["imdb_page"] = self.imdb.title_page_rating(identity.imdb_id)
        tasks["web_imdb_rating"] = self.web.find_rating(
            identity.title, identity.year, source="imdb"
        )

        results = await safe_gather(tasks)

        tvmaze_facts = results.get("tvmaze")
        kitsu_match = results.get("kitsu")
        anilist_match = results.get("anilist")
        wiki_bundle = results.get("wikipedia") or {}
        imdb_page = results.get("imdb_page")
        web_imdb_rating = results.get("web_imdb_rating")
        itunes_track = (results.get("itunes") or [None])[0] if results.get("itunes") else None

        # Jikan needs a MAL id (AniList gives us one) — ask only when we
        # already know this is anime.
        mal_id = None
        if anilist_match:
            mal_id = anilist_match.get("idMal")
        jikan_anime = None
        if (kitsu_match or anilist_match) and not mal_id:
            # Search Jikan by title only as a fallback.
            jikan_anime = await self.jikan.best_match(identity.title)
            mal_id = jikan_anime.get("mal_id") if jikan_anime else None
        if mal_id:
            jikan_anime = await self.jikan.anime_full(int(mal_id)) or jikan_anime

        # Season count for series (cheap, cached).
        tvmaze_seasons: list[dict[str, Any]] = []
        if tvmaze_facts and tvmaze_facts.get("_show", {}).get("id"):
            tvmaze_seasons = await self.tvmaze.seasons(int(tvmaze_facts["_show"]["id"]))

        # Light "similar" row: franchise relations + anime recommendations.
        pair = await safe_gather(
            {
                "s": self._light_similar(identity, anilist_match, jikan_anime, wiki_bundle),
                "w": self._streaming(identity, anilist_match, jikan_anime, tvmaze_facts),
            }
        )
        similar = pair.get("s") or []
        streaming = pair.get("w") or []

        self._merge(
            title,
            identity=identity,
            tvmaze_facts=tvmaze_facts,
            tvmaze_seasons=tvmaze_seasons,
            kitsu_entry=kitsu_match[1] if kitsu_match else None,
            anilist_media=anilist_match,
            jikan_anime=jikan_anime,
            wiki_bundle=wiki_bundle,
            imdb_page=imdb_page,
            web_imdb_rating=web_imdb_rating,
            itunes_track=itunes_track,
            similar=similar or [],
            streaming=streaming or [],
        )

    # ------------------------------------------------------------------
    # Source-specific fetch helpers
    # ------------------------------------------------------------------

    async def _tvmaze_show(self, identity: Identity) -> dict[str, Any] | None:
        show = None
        if identity.imdb_id:
            show = await self.tvmaze.lookup_by_imdb(identity.imdb_id)
        if show is None and identity.tvmaze_id:
            show = await self.tvmaze.show(identity.tvmaze_id)
        if show is None:
            for candidate, raw in await self.tvmaze.search_shows(identity.title, limit=5):
                if _plausible_series_match(identity, candidate):
                    show = raw
                    break
        if not show:
            return None
        facts = show_facts(show)
        facts["_show"] = show
        return facts

    async def _itunes_movie(self, name: str) -> list[dict[str, Any]] | None:
        tracks = await self.itunes.search_movie(name, limit=5)
        return tracks or None

    async def _wikipedia_bundle(self, identity: Identity) -> dict[str, Any]:
        """Summary + full wikitext for the best-matching article."""
        patterns = _wiki_article_patterns(identity)
        summary = None
        article_title = ""
        for pattern in patterns:
            summary = await self.wikipedia.summary(pattern)
            if summary:
                article_title = summary.get("title") or pattern
                break
        if summary is None:
            # Fall back to search-completion, which handles odd titles.
            for candidate in await self.wikipedia.opensearch(identity.title, limit=6):
                if normalize_title(identity.title) in normalize_title(candidate):
                    summary = await self.wikipedia.summary(candidate)
                    if summary:
                        article_title = summary.get("title") or candidate
                        break
        if summary is None:
            return {}
        wikitext = await self.wikipedia.full_wikitext(article_title)
        infobox = self.wikipedia.parse_film_infobox(wikitext) or self.wikipedia.parse_tv_infobox(wikitext)
        reception = self.wikipedia.reception_ratings(wikitext)
        return {
            "summary": summary,
            "infobox": infobox,
            "reception": reception,
            "article": article_title,
            "wikitext": wikitext,
        }

    async def _light_similar(
        self,
        identity: Identity,
        anilist_media: dict[str, Any] | None,
        jikan_anime: dict[str, Any] | None,
        wiki_bundle: dict[str, Any],
    ) -> list[RelatedTitle]:
        related: list[RelatedTitle] = []

        if anilist_media:
            for edge in ((anilist_media.get("relations") or {}).get("edges") or []):
                if not isinstance(edge, dict):
                    continue
                node = edge.get("node") or {}
                label = (node.get("title") or {}).get("english") or (node.get("title") or {}).get("romaji")
                if not label or str(node.get("type") or "") != "ANIME":
                    continue
                related.append(
                    RelatedTitle(
                        title=str(label),
                        kind=MediaKind.ANIME,
                        year=(node.get("startDate") or {}).get("year"),
                        relation=map_relation(edge.get("relationType")),
                        why="same franchise" if edge.get("relationType") in ("SEQUEL", "PREQUEL", "PARENT", "SPIN_OFF", "SIDE_STORY") else "",
                        url=f"https://anilist.co/anime/{node.get('id')}/" if node.get("id") else None,
                    )
                )
        if jikan_anime:
            for relation in anime_facts(jikan_anime).get("relations") or []:
                for entry in relation.get("entries") or []:
                    if entry.get("type") == "anime" and entry.get("name"):
                        related.append(
                            RelatedTitle(
                                title=str(entry["name"]),
                                kind=MediaKind.ANIME,
                                relation=map_relation_str(relation.get("relation")),
                                why="same franchise",
                            )
                        )
        infobox = wiki_bundle.get("infobox") or {}
        for key, relation in (("followed_by", "sequel"), ("preceded_by", "prequel")):
            if infobox.get(key):
                related.append(
                    RelatedTitle(title=infobox[key], relation=relation, why="direct continuation")
                )

        # Franchise relations first, de-duplicated.
        out: list[RelatedTitle] = []
        seen: set[str] = set()
        for item in related:
            key = normalize_title(item.title)
            if not key or key in seen or key == normalize_title(identity.title):
                continue
            seen.add(key)
            out.append(item)

        # Anime community recommendations fill the "similar" row.
        if anilist_media:
            for node in ((anilist_media.get("recommendations") or {}).get("nodes") or []):
                preview = node.get("mediaPreview") or {}
                label = (preview.get("title") or {}).get("english") or (preview.get("title") or {}).get("romaji")
                if not label:
                    continue
                if normalize_title(label) in seen or normalize_title(label) == normalize_title(identity.title):
                    continue
                seen.add(normalize_title(label))
                score = preview.get("averageScore")
                out.append(
                    RelatedTitle(
                        title=str(label),
                        kind=MediaKind.ANIME,
                        relation="similar",
                        why="recommended by AniList users",
                        rating=Rating(source="AniList", value=round(score / 10, 1), scale="/10")
                        if score
                        else None,
                    )
                )

        # Live-web fill for titles the databases don't cover (most western
        # films have no AniList/Jikan relations row at all).
        if len(out) < 4 and self.web is not None:
            mined = await self.web.mine_similar_titles(identity.title, limit=6)
            mined = [name for name in mined if normalize_title(name) not in seen
                     and normalize_title(name) != normalize_title(identity.title)]
            if mined:
                verified = await _verify_candidates(self.imdb, identity, mined[:6])
                for name, candidate in verified:
                    seen.add(normalize_title(name))
                    out.append(
                        RelatedTitle(
                            title=candidate.title,
                            kind=candidate.kind,
                            year=candidate.year,
                            relation="similar",
                            why="frequently named alongside it on the web",
                            url=f"https://www.imdb.com/title/{candidate.imdb_id}/"
                            if candidate.imdb_id
                            else None,
                        )
                    )
        return out[:8]

    async def _streaming(
        self,
        identity: Identity,
        anilist_media: dict[str, Any] | None,
        jikan_anime: dict[str, Any] | None,
        tvmaze_facts: dict[str, Any] | None,
    ) -> list[StreamingOffer]:
        offers: list[StreamingOffer] = []
        seen: set[str] = set()

        if anilist_media:
            for link in media_facts(anilist_media).get("external_links") or []:
                site = str(link.get("site") or "")
                url = str(link.get("url") or "")
                kind = str(link.get("type") or "").lower()
                if kind != "streaming" or not site:
                    continue
                region = str(link.get("region") or "")
                if site in seen and not region:
                    continue
                seen.add(site)
                offers.append(
                    StreamingOffer(
                        platform=site,
                        region=region,
                        url=url or None,
                        source="AniList",
                    )
                )
        if jikan_anime:
            for entry in anime_facts(jikan_anime).get("streaming") or []:
                name = entry.get("name")
                if name and name not in seen:
                    seen.add(name)
                    offers.append(
                        StreamingOffer(platform=name, url=entry.get("url") or None, source="MyAnimeList")
                    )

        # Live web verification for everything (movies especially).
        web_offers = await self.web.find_streaming(identity.title)
        for offer in web_offers:
            if offer.platform not in seen:
                seen.add(offer.platform)
                offers.append(offer)
        return offers

    # ------------------------------------------------------------------
    # Merging
    # ------------------------------------------------------------------

    def _merge(
        self,
        title: MediaTitle,
        *,
        identity: Identity,
        tvmaze_facts: dict[str, Any] | None,
        tvmaze_seasons: list[dict[str, Any]],
        kitsu_entry: dict[str, Any] | None,
        anilist_media: dict[str, Any] | None,
        jikan_anime: dict[str, Any] | None,
        wiki_bundle: dict[str, Any],
        imdb_page: dict[str, Any] | None,
        web_imdb_rating: Rating | None,
        itunes_track: dict[str, Any] | None,
        similar: list[RelatedTitle],
        streaming: list[StreamingOffer],
    ) -> None:
        kitsu = entry_facts(kitsu_entry) if kitsu_entry else None
        anilist = media_facts(anilist_media) if anilist_media else None
        jikan = anime_facts(jikan_anime) if jikan_anime else None
        wiki = wiki_bundle or {}
        infobox = wiki.get("infobox") or {}
        wiki_summary = (wiki.get("summary") or {}).get("extract") or ""

        # --- kind ---------------------------------------------------------
        # Anime databases are authoritative about anime-ness.
        if anilist and anilist_media:
            title.kind = anilist["kind"]
        elif kitsu:
            title.kind = kitsu["kind"]
        elif tvmaze_facts:
            title.kind = tvmaze_facts["kind"]

        # --- titles ---------------------------------------------------------
        alts: list[str] = []
        for group in (
            (anilist or {}).get("titles") or [],
            (kitsu or {}).get("titles") or [],
            (jikan_anime or {}).get("titles") or [],
            identity.alt_titles,
        ):
            for value in group:
                if isinstance(value, dict):
                    value = value.get("title") or value.get("english") or value.get("romaji") or ""
                value = str(value or "").strip()
                if value and normalize_title(value) != normalize_title(title.title):
                    alts.append(value)
        title.alt_titles = _unique(alts)[:6]

        # --- synopsis -------------------------------------------------------
        title.synopsis = (
            wiki_summary
            or (kitsu or {}).get("synopsis")
            or (anilist or {}).get("synopsis")
            or (tvmaze_facts or {}).get("summary")
            or (jikan or {}).get("synopsis")
            or ((itunes_track or {}).get("description") if itunes_track else "")
            or ""
        ).strip()

        # --- genres -----------------------------------------------------------
        genres: list[str] = []
        for group in (
            (anilist or {}).get("genres") or [],
            (kitsu_entry or {}).get("genres") or [],
            (jikan or {}).get("genres") or [],
            (tvmaze_facts or {}).get("genres") or [],
            [g for g in str(infobox.get("genre") or "").split(",") if g.strip()],
        ):
            for value in group:
                value = str(value or "").strip()
                if value and value.casefold() not in {g.casefold() for g in genres}:
                    genres.append(value)
        title.genres = genres[:6]

        # --- dates ------------------------------------------------------------
        release: DateValue | None = None
        if tvmaze_facts and tvmaze_facts.get("premiered") and tvmaze_facts["premiered"].value:
            release = tvmaze_facts["premiered"]
        if wiki:
            wiki_date = self._wiki_release_date(infobox)
            if wiki_date and wiki_date.value:
                release = wiki_date if release is None else release
        if kitsu and kitsu.get("start_date") and release is None:
            release = DateValue.from_iso(kitsu["start_date"], "Kitsu")
        if anilist and release is None and anilist.get("start"):
            start = anilist["start"]
            release = DateValue.from_parts(start.get("year"), start.get("month"), start.get("day"))
            if release.value:
                release = DateValue(value=release.value, precision=release.precision, source="AniList")
        if imdb_page and imdb_page.get("date") and release is None:
            release = DateValue.from_iso(imdb_page["date"], "IMDb")
        if (itunes_track or {}).get("release_date") and release is None:
            release = DateValue.from_iso(itunes_track["release_date"], "Apple TV")
        title.release = release

        # --- status ------------------------------------------------------------
        anime_status = (anilist or {}).get("status") or (kitsu or {}).get("status") or (jikan or {}).get("status")
        if anime_status and anime_status is not ReleaseStatus.UNKNOWN:
            title.status = anime_status
        elif tvmaze_facts:
            title.status = tvmaze_facts["status"]
        else:
            title.status = self._status_from_dates(release)

        if anilist and anilist.get("next_episode"):
            next_episode = anilist["next_episode"]
            episode_number = next_episode.get("episode")
            airing_at = next_episode.get("airingAt")
            if airing_at:
                from datetime import datetime, timezone

                try:
                    moment = datetime.fromtimestamp(int(airing_at), tz=timezone.utc)
                    title.status_note = f"episode {episode_number} airs {moment.strftime('%-d %b %Y, %H:%M UTC')}"
                    if title.status is ReleaseStatus.UPCOMING:
                        title.status = ReleaseStatus.AIRING
                except (TypeError, ValueError, OSError):
                    pass
        elif tvmaze_facts and tvmaze_facts.get("status") is ReleaseStatus.AIRING:
            title.status_note = "currently airing"

        # --- runtime / episodes / seasons -----------------------------------------
        if title.kind.is_anime:
            title.episodes = (anilist or {}).get("episodes") or (kitsu or {}).get("episodes") or (jikan or {}).get("episodes")
            title.episode_runtime = (anilist or {}).get("episode_length") or (kitsu or {}).get("episode_length") or (jikan or {}).get("episode_length")
            title.runtime_minutes = title.episode_runtime
        if tvmaze_facts:
            if not title.episode_runtime:
                title.episode_runtime = tvmaze_facts.get("runtime")
            if not title.runtime_minutes:
                title.runtime_minutes = tvmaze_facts.get("runtime")
        if itunes_track and not title.runtime_minutes:
            facts = self.itunes.track_facts(itunes_track)
            title.runtime_minutes = facts.get("runtime")
        if infobox.get("runtime") and not title.runtime_minutes:
            title.runtime_minutes = parse_runtime(infobox["runtime"])

        if tvmaze_seasons:
            title.seasons = len(tvmaze_seasons)
            episode_total = sum(int(s.get("episodeOrder") or 0) for s in tvmaze_seasons)
            if episode_total:
                title.episodes = title.episodes or episode_total
        if infobox.get("num_seasons") and not title.seasons:
            title.seasons = parse_number(infobox["num_seasons"])
        if infobox.get("num_episodes") and not title.episodes:
            title.episodes = parse_number(infobox["num_episodes"])
        if title.episodes and not title.seasons and title.kind.is_series and not title.kind.is_anime:
            title.seasons = None  # never guess seasons from episodes

        # --- people -----------------------------------------------------------------
        if infobox.get("starring"):
            title.cast = [Person(name=n.strip()) for n in infobox["starring"].split(",") if n.strip()][:8]
        creators: list[str] = []
        for key in ("director", "creator", "created_by", "writer"):
            if infobox.get(key):
                for name in str(infobox[key]).split(","):
                    name = name.strip()
                    if name and len(name) < 60:
                        creators.append(name)
        title.creators = _unique(creators)[:4]
        if (anilist or {}).get("studios"):
            title.studios = anilist["studios"][:3]
        elif (jikan or {}).get("studios"):
            title.studios = jikan["studios"][:3]
        if infobox.get("studio"):
            title.studios.extend(s.strip() for s in infobox["studio"].split(",") if s.strip())
        title.studios = _unique(title.studios)[:4]

        networks: list[str] = []
        if tvmaze_facts:
            if tvmaze_facts.get("web_channel"):
                networks.append(tvmaze_facts["web_channel"])
            if tvmaze_facts.get("network"):
                networks.append(tvmaze_facts["network"])
        if infobox.get("original_network"):
            networks.extend(n.strip() for n in infobox["original_network"].split(",") if n.strip())
        if infobox.get("network"):
            networks.extend(n.strip() for n in infobox["network"].split(",") if n.strip())
        title.networks = _unique(networks)[:3]

        # --- country / language ----------------------------------------------------
        if infobox.get("country"):
            title.country = infobox["country"]
        elif tvmaze_facts and tvmaze_facts.get("channel_country"):
            title.country = tvmaze_facts["channel_country"]
        if infobox.get("language"):
            title.language = infobox["language"]
        elif tvmaze_facts and tvmaze_facts.get("language"):
            title.language = tvmaze_facts["language"]

        # --- ratings ------------------------------------------------------------------
        ratings: list[Rating] = []
        if (anilist or {}).get("rating"):
            ratings.append(anilist["rating"])
        if (kitsu or {}).get("rating"):
            ratings.append(kitsu["rating"])
        if (jikan or {}).get("rating"):
            ratings.append(jikan["rating"])
        if tvmaze_facts and tvmaze_facts.get("rating"):
            ratings.append(tvmaze_facts["rating"])
        if imdb_page and imdb_page.get("rating") is not None:
            ratings.append(
                Rating(
                    source="IMDb",
                    value=float(imdb_page["rating"]),
                    scale="/10",
                    votes=imdb_page.get("votes"),
                )
            )
        if web_imdb_rating and not any(r.source == "IMDb" for r in ratings):
            ratings.append(web_imdb_rating)
        ratings.extend(wiki.get("reception") or [])
        title.ratings = sorted(dedupe_ratings(ratings), key=rating_sort_key)

        # --- streaming / similar / links -----------------------------------------------
        title.streaming = streaming
        title.similar = similar

        if (anilist or {}).get("trailer"):
            trailer = anilist["trailer"]
            trailer_id = trailer.get("id")
            if trailer_id and str(trailer.get("site") or "").lower() == "youtube":
                title.trailer_url = f"https://www.youtube.com/watch?v={trailer_id}"
        if not title.trailer_url and kitsu and kitsu.get("trailer_youtube"):
            title.trailer_url = f"https://www.youtube.com/watch?v={kitsu['trailer_youtube']}"
        if not title.trailer_url and (jikan or {}).get("trailer"):
            title.trailer_url = jikan["trailer"]

        if (kitsu or {}).get("poster"):
            title.poster = title.poster or kitsu["poster"]
        for candidate in (
            (wiki.get("summary") or {}).get("poster"),
            (wiki.get("summary") or {}).get("thumbnail"),
            (anilist or {}).get("poster"),
            identity.poster,
        ):
            if candidate:
                title.poster = title.poster or candidate
        title.official_site = (tvmaze_facts or {}).get("official_site") or (infobox.get("website") or None)

        if (anilist or {}).get("trending") and anilist["trending"] > 0:
            title.popularity_hint = f"trending on AniList right now (score {anilist['trending']})"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _wiki_release_date(self, infobox: dict[str, str]) -> DateValue | None:
        for key in ("released", "release_date", "first_aired", "air_date", "released date"):
            value = infobox.get(key)
            if not value:
                continue
            triple = parse_film_date(value)
            if triple:
                year, month, day = triple
                return DateValue.from_parts(year, month, day, source="Wikipedia")
            iso = DateValue.from_iso(value[:10], "Wikipedia")
            if iso.value:
                return iso
        return None

    @staticmethod
    def _status_from_dates(release: DateValue | None) -> ReleaseStatus:
        if release and release.value and release.value > now_utc().date():
            return ReleaseStatus.UPCOMING
        if release and release.value:
            return ReleaseStatus.RELEASED
        return ReleaseStatus.UNKNOWN

    def _finalize(self, identity: Identity, title: MediaTitle) -> None:
        sources: list[str] = []
        for name in (
            ("IMDb", identity.imdb_id),
            ("TVMaze", identity.tvmaze_id),
            ("Kitsu", identity.kitsu_id),
            ("AniList", identity.anilist_id),
            ("MyAnimeList", identity.mal_id),
        ):
            if name[1]:
                sources.append(name[0])
        if title.ratings or title.synopsis or title.genres:
            if "Wikipedia" not in sources:
                sources.append("Wikipedia")
        title.sources = _unique(sources + ["live web search" if any(r.via for r in title.ratings) else ""])[:6]
        title.sources = [s for s in title.sources if s]

        for note in verify_title(title):
            title.note(note)

        if identity.kind is MediaKind.FRANCHISE:
            title.note(
                "this is a franchise overview — the individual entries are listed under Similar/Related",
                source="IMDb",
            )

        # An upcoming title should show its date as the upcoming date.
        if title.status is ReleaseStatus.UPCOMING and title.release and title.release.value:
            title.upcoming = title.release

        if not title.ratings:
            title.note("no ratings could be verified from live sources right now")


def _wiki_article_patterns(identity: Identity) -> list[str]:
    title = identity.title
    kind = identity.kind
    if kind in (MediaKind.MOVIE, MediaKind.SHORT, MediaKind.ANIME_MOVIE):
        return [f"{title} (film)", title]
    if kind is MediaKind.MINISERIES:
        return [f"{title} (miniseries)", f"{title} (TV series)", title]
    if kind is MediaKind.WEB_SERIES:
        return [f"{title} (web series)", f"{title} (TV series)", title]
    if kind.is_anime:
        return [f"{title} (anime)", f"{title} (TV series)", title]
    if kind.is_series:
        return [f"{title} (TV series)", f"{title} (web series)", title]
    return [f"{title} (film)", f"{title} (TV series)", title]


def _plausible_series_match(identity: Identity, candidate: Identity) -> bool:
    score = similarity(identity.title, candidate.title)
    if score < 80:
        return False
    if identity.year and candidate.year and abs(identity.year - candidate.year) > 2:
        return False
    return True


def _unique(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = (value or "").strip()
        key = normalize_title(value)
        if value and key and key not in seen:
            seen.add(key)
            out.append(value)
    return out


def map_relation_str(relation: str | None) -> str:
    from .sources.jikan import map_relation as _map

    return _map(relation)


async def safe_gather(tasks: dict[str, Any]) -> dict[str, Any]:
    """Run a dict of coroutines concurrently; failures become None."""
    import asyncio

    if not tasks:
        return {}
    keys = list(tasks.keys())
    coros = list(tasks.values())
    results = await asyncio.gather(*coros, return_exceptions=True)
    out: dict[str, Any] = {}
    for key, result in zip(keys, results):
        if isinstance(result, BaseException):
            log.debug("details task %s failed: %s", key, result)
            out[key] = None
        else:
            out[key] = result
    return out


async def _verify_candidates(
    imdb: ImdbSource,
    identity: Identity,
    names: list[str],
) -> list[tuple[str, Identity]]:
    """Verify web-mined candidate names against the IMDb database.

    A name only survives when IMDb knows a *screen* title that matches it;
    this is what keeps the "similar" row free of hallucinated names.
    """
    import asyncio as _asyncio

    async def one(name: str):
        try:
            suggestions = await imdb.suggest(name)
        except Exception:
            return None
        for candidate in suggestions:
            if candidate.kind in (MediaKind.PERSON, MediaKind.FRANCHISE):
                continue
            if normalize_title(candidate.title) == normalize_title(name):
                return (name, candidate)
        return None

    results = await _asyncio.gather(*(one(name) for name in names))
    return [(name, candidate) for item in results if item for name, candidate in [item]]
