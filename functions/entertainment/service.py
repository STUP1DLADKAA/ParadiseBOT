"""The EntertainmentService — the one object the cog talks to.

Owns the HTTP session, every source, the caches and the engines, and
exposes the small set of operations the Discord layer needs:

    autocomplete(query)        live suggestions while typing
    lookup(query)              the /example card (found/ambiguous/miss)
    trending_report(...)       what's hot, honestly labelled
    upcoming_report(...)       future releases, precision-safe
    recommend_report(...)      "more like this", with reasons
    streaming_report(...)      region-aware where-to-watch
    rating_report(...)         named-source ratings only
    parse_chat(text)           natural-language intents, or None

Freshness rules: volatile facts (ratings, streaming, status, trending)
are re-verified at most every 45 minutes; stable facts (synopsis, cast,
genres) cache for a day; a cached stable card is served *only* as a
stale fallback when the live refresh fails, and says so.
"""

from __future__ import annotations

import logging
import time

from functions.helpers import ParadiseError
from functions.search.engine import SearchEngine

from . import nlu
from .autocomplete import AutocompleteEngine
from .cache import (
    NEGATIVE_TTL,
    SEARCH_TTL,
    TTLCache,
    MediaCache,
    TitleMemory,
)
from .details import DetailsBuilder
from .models import (
    Identity,
    MediaKind,
    MediaTitle,
    ReleaseStatus,
    now_utc,
)
from .recommend import RecommendationEngine
from .resolver import Resolver
from .sources.anilist import AniListSource
from .sources.imdb import ImdbSource
from .sources.itunes import ITunesSource
from .sources.jikan import JikanSource
from .sources.kitsu import KitsuSource
from .sources.tvmaze import TvMazeSource
from .sources.websearch import WebSearchSource
from .sources.wikipedia import WikipediaSource
from .sources.wikidata import WikidataSource
from .streaming import StreamingEngine
from .textutil import normalize_title, title_match_score
from .trending import POPULAR, TOP_RATED, TRENDING, TrendingEngine
from .upcoming import UpcomingEngine

log = logging.getLogger(__name__)

FOUND = "found"
AMBIGUOUS = "ambiguous"
NOT_FOUND = "not_found"

# Below these similarity scores we don't trust a single candidate enough
# to auto-pick it.
MATCH_THRESHOLD = 78.0
# How close the runner-up may be before we ask the user to choose.
AMBIGUITY_MARGIN = 120.0
SHORT_QUERY = 4  # queries this short get extra scrutiny


class LookupOutcome:
    """What a /example lookup produced."""

    def __init__(
        self,
        status: str,
        *,
        query: str = "",
        title: MediaTitle | None = None,
        candidates: list[Identity] | None = None,
        stale: bool = False,
    ) -> None:
        self.status = status
        self.query = query
        self.title = title
        self.candidates = candidates or []
        self.stale = stale

    @property
    def found(self) -> bool:
        return self.status == FOUND and self.title is not None


class EntertainmentService:
    def __init__(self, config=None, client=None) -> None:
        timeout = float(getattr(config, "timeout", 8.0) or 8.0)
        self.client = client or _Client(timeout=timeout)

        self.imdb = ImdbSource(self.client)
        self.tvmaze = TvMazeSource(self.client)
        self.kitsu = KitsuSource(self.client)
        self.anilist = AniListSource(self.client)
        self.jikan = JikanSource(self.client)
        self.wikipedia = WikipediaSource(self.client)
        self.wikidata = WikidataSource(self.client)
        self.itunes = ITunesSource(self.client)
        self.web = WebSearchSource(SearchEngine())

        self.resolver = Resolver(self.imdb, self.tvmaze, self.kitsu, self.anilist)
        self.details = DetailsBuilder(
            self.imdb, self.tvmaze, self.kitsu, self.anilist, self.jikan,
            self.wikipedia, self.itunes, self.web,
        )
        self.recommender = RecommendationEngine(
            self.imdb, self.anilist, self.jikan, self.kitsu, self.wikidata, self.web
        )
        self.trend_engine = TrendingEngine(
            self.wikipedia, self.anilist, self.kitsu, self.jikan, self.tvmaze, self.itunes
        )
        self.upcoming_engine = UpcomingEngine(
            self.wikidata, self.anilist, self.kitsu, self.jikan, self.tvmaze
        )
        self.streaming_engine = StreamingEngine(self.anilist, self.jikan, self.tvmaze, self.web)

        self.memory = TitleMemory()
        self.autocomplete_engine = AutocompleteEngine(self.imdb, self.memory)
        self.media_cache = MediaCache(
            max_items=int(getattr(config, "cache_size", 256) or 256),
            stable_ttl=float(getattr(config, "stable_cache_ttl", 24 * 3600) or 24 * 3600),
            volatile_ttl=float(getattr(config, "volatile_cache_ttl", 45 * 60) or 45 * 60),
        )
        self._search_cache = TTLCache[str, list[Identity]](128, SEARCH_TTL)
        self._negative_cache = TTLCache[str, float](128, NEGATIVE_TTL)

    async def close(self) -> None:
        await self.client.close()

    def describe(self) -> str:
        return (
            "key-less entertainment discovery — IMDb · TVMaze · Kitsu · AniList · "
            "MyAnimeList · Wikipedia · Wikidata · Apple TV · live web search"
        )

    # ------------------------------------------------------------------
    # Autocomplete
    # ------------------------------------------------------------------

    async def autocomplete(self, query: str) -> list[tuple[str, str]]:
        """(display, insert-value) choices for the /example input box."""
        identities = await self.autocomplete_engine.suggest(query)
        return self.autocomplete_engine.choices(identities)

    # ------------------------------------------------------------------
    # The main lookup
    # ------------------------------------------------------------------

    async def lookup(self, query: str, *, detailed: bool = False) -> LookupOutcome:
        from .resolver import parse_query

        query = (query or "").strip()
        if not query:
            return LookupOutcome(NOT_FOUND, query=query)
        base, season = parse_query(query)
        key = normalize_title(base)

        if self._negative_cache.get(key) is not None:
            return LookupOutcome(NOT_FOUND, query=query)

        cached = self.media_cache.get_stable(key)
        if cached is not None and self.media_cache.volatile_fresh(key):
            return LookupOutcome(FOUND, query=query, title=cached)

        ranked = self._search_cache.get(key)
        if ranked is None:
            ranked = await self.resolver.resolve(base)
            if ranked:
                self._search_cache.put(key, ranked)

        if not ranked:
            if cached is not None:
                return LookupOutcome(FOUND, query=query, title=cached, stale=True)
            if self.imdb.last_error:
                # Sources are unreachable — very different from "not found".
                raise ParadiseError(
                    "I couldn't verify the latest information right now — "
                    "the sources I check are unreachable. please try again in a bit."
                )
            self._negative_cache.put(key, time.monotonic())
            return LookupOutcome(NOT_FOUND, query=query)

        choice, needs_choice = self._choose(base, ranked)
        if needs_choice:
            return LookupOutcome(AMBIGUOUS, query=query, candidates=ranked[:4])

        try:
            title = await self._build_for(choice, season=season)
        except Exception as error:  # noqa: BLE001 - a lookup never crashes a channel
            log.warning("Lookup of %r failed: %s", query, error, exc_info=True)
            if cached is not None:
                return LookupOutcome(FOUND, query=query, title=cached, stale=True)
            raise ParadiseError(
                "I couldn't verify the latest information right now. please try again in a bit."
            ) from error

        if not title.synopsis and not title.ratings and not title.genres:
            # Nothing verifiable came back — don't pretend it did.
            if cached is not None:
                return LookupOutcome(FOUND, query=query, title=cached, stale=True)
            self._negative_cache.put(key, time.monotonic())
            return LookupOutcome(NOT_FOUND, query=query)

        self.memory.remember_title(title)
        self.memory.remember_identities(ranked)
        self.media_cache.put_stable(key, title)
        self.media_cache.mark_volatile(key)
        return LookupOutcome(FOUND, query=query, title=title)

    async def _build_for(self, identity: Identity, *, season: int | None) -> MediaTitle:
        if identity.kind is MediaKind.PERSON:
            return await self._build_person(identity)
        if identity.kind is MediaKind.FRANCHISE:
            return await self._build_franchise(identity)
        return await self.details.build(identity, season=season)

    async def build_card(self, identity: Identity, *, season: int | None = None) -> MediaTitle:
        """Assemble a full card for a known identity (buttons use this)."""
        title = await self._build_for(identity, season=season)
        self.memory.remember_title(title)
        self.media_cache.put_stable(normalize_title(identity.title), title)
        self.media_cache.mark_volatile(normalize_title(identity.title))
        return title

    def _choose(self, query: str, ranked: list[Identity]) -> tuple[Identity, bool]:
        """Pick the winner, or flag that the user should choose."""
        best = ranked[0]
        # Strong exact/near-exact title matches are safe to auto-pick,
        # except for very short queries where several titles tie (e.g. "it"
        # → IT (2017), It Chapter Two, It: Welcome to Derry).
        similarity = title_match_score(query, best.title, best.alt_titles)
        runner_up = ranked[1] if len(ranked) > 1 else None

        if similarity >= 92.0 and best.is_media:
            if len(query.strip()) <= SHORT_QUERY and runner_up is not None:
                query_norm = normalize_title(query)
                runner_sim = title_match_score(query, runner_up.title, runner_up.alt_titles)
                runner_prefix = normalize_title(runner_up.title).startswith(query_norm)
                if runner_up.is_media and (runner_sim >= 90.0 or runner_prefix):
                    return best, True
            return best, False
        if similarity >= MATCH_THRESHOLD and best.is_media:
            return best, False
        if runner_up is not None and best.is_media:
            # Scores come from the resolver (popularity-weighted); when two
            # candidates are nearly tied, both are plausible answers.
            best_score = self.resolver._score(query, best)
            runner_score = self.resolver._score(query, runner_up)
            if best_score - runner_score < AMBIGUITY_MARGIN:
                return best, True
            return best, False
        # People and unknowns only win when they're the only option.
        if len(ranked) == 1:
            return best, False
        return best, True

    # -- special builds ------------------------------------------------------

    async def _build_person(self, identity: Identity) -> MediaTitle:
        title = MediaTitle(title=identity.title, kind=MediaKind.PERSON, identity=identity)
        for candidate in (identity.title, f"{identity.title} (actor)"):
            summary = await self.wikipedia.summary(candidate)
            if summary:
                title.synopsis = summary["extract"]
                title.poster = summary.get("poster") or summary.get("thumbnail") or identity.poster
                title.sources = ["Wikipedia", "IMDb"]
                title.note("that's a person — showing their overview; their works are linked from here")
                return title
        title.synopsis = ""
        title.note("couldn't verify details about this person right now", unconfirmed=True)
        return title

    async def _build_franchise(self, identity: Identity) -> MediaTitle:
        title = MediaTitle(title=identity.title, kind=MediaKind.FRANCHISE, identity=identity)
        summary = await self.wikipedia.summary(identity.title)
        if summary:
            title.synopsis = summary["extract"]
            title.poster = summary.get("poster") or summary.get("thumbnail") or identity.poster
            title.sources = ["Wikipedia"]

        # The franchise's main entries come from IMDb suggestions.
        entries = await self.imdb.suggest(identity.title, limit=12)
        seen: set[str] = set()
        related = []
        for entry in entries:
            if entry.is_media and entry.kind is not MediaKind.FRANCHISE:
                key = normalize_title(entry.title)
                if key not in seen and key != normalize_title(identity.title):
                    seen.add(key)
                    related.append(entry)
        title.similar = []  # filled below as related
        from .models import RelatedTitle

        title.similar = [
            RelatedTitle(
                title=entry.title,
                kind=entry.kind,
                year=entry.year,
                relation="part of the franchise",
                why="",
                popularity=entry.popularity,
            )
            for entry in related[:6]
        ]
        title.note("this is a franchise overview — pick any entry for its own card")
        return title

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------

    async def trending_report(
        self, *, category: str = "all", window: str = "", mode: str = TRENDING,
        platform: str = "",
    ) -> tuple[str, list, list[str]]:
        category = _normalize_category(category)
        entries, notes = await self.trend_engine.trending(
            category=category, window=window, mode=mode
        )
        if platform:
            # Platform charts aren't in the free databases — offer the
            # honest hybrid: what's hot + what's new on that platform.
            premieres, _ = await self.upcoming_engine.upcoming(
                category="series", days=21, platform=platform
            )
            from .models import TrendingEntry

            for entry in premieres[:4]:
                entries.append(
                    TrendingEntry(
                        title=entry.title,
                        kind=entry.kind,
                        year=None,
                        metric=f"new on {platform.title()}: {entry.when_text()}",
                        list_kind="popular",
                        url=entry.url,
                        source=entry.source,
                    )
                )
            notes.append(f"platform-specific chart not available key-less — showing new {platform.title()} releases instead")
        heading = {
            TRENDING: "Trending right now",
            POPULAR: "Popular right now",
            TOP_RATED: "Highest rated (currently airing / listed)",
        }[mode]
        if category not in ("", "all"):
            heading += f" — {category}"
        if platform:
            heading += f" · {platform.title()}"
        if window:
            heading += f" ({window})"
        return heading, entries, notes

    async def upcoming_report(
        self,
        *,
        category: str = "all",
        days: int = 30,
        region: str = "",
        platform: str = "",
        subject: str = "",
        season: int | None = None,
        media_type: str = "",
    ) -> tuple[str, list, list[str]] | tuple[str, str]:
        """Subject-specific asks ("next season of X") get a live answer."""
        category = _normalize_category(category or media_type)
        if subject:
            if season or not category:
                return await self._release_answer(subject, season=season)
            return await self._franchise_upcoming(subject, category)

        entries, notes = await self.upcoming_engine.upcoming(
            category=category, days=days, region=region, platform=platform
        )
        heading = "Coming up"
        if category not in ("", "all"):
            heading += f" — {category}"
        if platform:
            heading += f" on {platform}"
        if region:
            heading += f" ({region})"
        return heading, entries, notes

    async def _franchise_upcoming(
        self, subject: str, category: str
    ) -> tuple[str, list, list[str]]:
        """'Upcoming Marvel movies' — franchise parts + live web links."""
        notes: list[str] = []
        entries: list = []
        heading = f"Coming up — {subject} {category}".strip()

        # Try the structured route: franchise article → Wikidata → future parts.
        ranked = await self.resolver.resolve(subject)
        franchise = next(
            (item for item in ranked if item.kind is MediaKind.FRANCHISE), None
        )
        if franchise is None and ranked:
            franchise = ranked[0]
        if franchise is not None:
            summary = await self.wikipedia.summary(franchise.title)
            qid = (summary or {}).get("wikibase") or franchise.wikidata_id
            if qid:
                parts = await self.wikidata.franchise_parts(qid, limit=40)
                today = now_utc().date()
                future = [
                    part for part in parts if part["when"] and part["when"] >= today
                ]
                from .models import DateValue, UpcomingEntry

                for part in future[:10]:
                    entries.append(
                        UpcomingEntry(
                            title=part["title"],
                            kind=MediaKind.MOVIE,
                            when=DateValue(
                                value=part["when"], precision=part["precision"], source="Wikidata"
                            ),
                            status_note="announced"
                            if part["precision"] >= 11
                            else "window announced",
                            url=f"https://www.wikidata.org/wiki/{part['qid']}" if part.get("qid") else None,
                            source="Wikidata",
                        )
                    )
                if future:
                    notes.append("Wikidata: announced future entries of the franchise")

        if not entries:
            # Live web fallback, with citations.
            results = await self.web.find_links(
                f"upcoming {subject} {category} release dates", limit=6
            )
            from .models import UpcomingEntry

            for result in results:
                entries.append(
                    UpcomingEntry(
                        title=result.title,
                        when=None,
                        status_note="see source for dates",
                        url=result.url,
                        source=result.source,
                    )
                )
            if results:
                notes.append("live web sources — dates and details behind the links")

        return heading, entries, notes

    async def _release_answer(self, subject: str, *, season: int | None) -> tuple[str, str]:
        """'When does X come out?' — live, with sources."""
        ranked = await self.resolver.resolve(subject)
        media = [identity for identity in ranked if identity.is_media]
        if not media:
            return "Release lookup", (
                f"I couldn't find a reliable match for “{subject}”."
            )
        identity = media[0]
        try:
            title = await self.details.build(identity, season=season)
        except Exception as error:  # noqa: BLE001
            log.warning("Release lookup failed for %r: %s", subject, error)
            raise ParadiseError(
                "I couldn't verify the latest information right now. please try again in a bit."
            )
        lines = [f"🗓️ **{identity.title}**"]
        if title.upcoming and title.upcoming.value:
            lines.append(f"Next release: **{title.upcoming.human()}** ({title.upcoming.source})")
            if title.upcoming.precision < 11:
                lines.append("-# that's an announced window, not an exact day")
        elif title.release and title.release.value:
            state = "already out" if title.release.value <= now_utc().date() else "releases"
            lines.append(f"{state}: **{title.release.human()}** ({title.release.source})")
            if title.status_note:
                lines.append(f"-# {title.status_note}")
        if title.status is ReleaseStatus.AIRING:
            lines.append("Status: currently airing")
        elif title.status is ReleaseStatus.UPCOMING:
            lines.append("Status: not released yet")
        if not any("release" in line.casefold() for line in lines[1:]):
            lines.append("no verified release date found — nothing official yet")
        if title.identity and title.identity.imdb_id:
            lines.append(
                f"-# sources: {', '.join(title.sources) or 'live web'}"
            )
        return "Release lookup", "\n".join(lines)

    async def recommend_report(
        self,
        *,
        subject: str = "",
        media_type: str = "",
        genre: str = "",
        tone: str = "",
        era: str = "",
        max_episodes: int | None = None,
        limit: int = 6,
    ) -> tuple[str, list, str]:
        """(heading, picks, intro) for recommendation requests."""
        if subject:
            ranked = await self.resolver.resolve(subject)
            media = [identity for identity in ranked if identity.is_media]
            if not media:
                return "Recommendations", [], (
                    f"I couldn't find a reliable match for “{subject}”, so no recommendations yet."
                )
            identity = media[0]
            seed_genres: list[str] = []
            try:
                seed = await self.details.build(identity)
                seed_genres = seed.genres
            except Exception:  # noqa: BLE001 - recommendations don't need the full card
                pass
            picks = await self.recommender.recommend_like(
                identity, tone=tone, limit=limit, seed_genres=seed_genres
            )
            intro = f"because you liked **{identity.title}**" + (
                f" and asked for something {tone}" if tone else ""
            )
            return f"If you liked {identity.title}", picks, intro

        kind = _kind_of_word(media_type)
        picks, explanation = await self.recommender.recommend_by_attributes(
            kind=kind, genre=genre, era=era, tone=tone, max_episodes=max_episodes, limit=limit
        )
        heading_bits = [part for part in (tone, genre, media_type or "picks") if part]
        heading = " ".join(heading_bits).strip().title() or "Picks for you"
        return heading, picks, f"picked from {explanation}"

    async def recommend_for_identity(self, identity: Identity, *, limit: int = 6):
        picks = await self.recommender.recommend_like(identity, limit=limit)
        return picks

    async def streaming_report(
        self, *, subject: str = "", region: str = "", platform: str = "", identity: Identity | None = None
    ) -> tuple[str, str]:
        """(heading, body) for where-to-watch questions."""
        if identity is None:
            if not subject:
                return "Where to watch", "name a title and I'll check where it's streaming."
            ranked = await self.resolver.resolve(subject)
            media = [item for item in ranked if item.is_media]
            if not media:
                return "Where to watch", (
                    f"I couldn't find a reliable match for “{subject}”."
                )
            identity = media[0]
        offers, notes = await self.streaming_engine.where_to_watch(
            identity, region=region, platform_filter=platform
        )
        from .format import format_streaming_reply

        heading = f"Where to watch {identity.title}"
        if region:
            heading += f" ({region})"
        return heading, format_streaming_reply(identity, offers, notes)

    async def rating_report(self, subject: str) -> tuple[str, str]:
        """(heading, body) — ratings with their sources, or an honest miss."""
        ranked = await self.resolver.resolve(subject)
        media = [item for item in ranked if item.is_media]
        if not media:
            return "Rating", f"I couldn't find a reliable match for “{subject}”."
        identity = media[0]
        try:
            title = await self.details.build(identity)
        except Exception as error:  # noqa: BLE001
            raise ParadiseError(
                "I couldn't verify the latest information right now. please try again in a bit."
            ) from error
        if not title.ratings:
            return f"{identity.title} — ratings", (
                f"no ratings for **{identity.title}** could be verified from live sources "
                "right now — it may be too new or too obscure for the databases I check."
            )
        lines = [f"⭐ **{identity.title}** — ratings from live sources", ""]
        for rating in title.ratings:
            lines.append(f"• {rating.display()}")
        lines.append("")
        lines.append("-# different rating systems measure different audiences — compare like with like")
        return f"{identity.title} — ratings", "\n".join(lines)

    # ------------------------------------------------------------------
    # Chat
    # ------------------------------------------------------------------

    @staticmethod
    def parse_chat(text: str) -> nlu.ChatIntent | None:
        return nlu.match_entertainment_request(text)


def _normalize_category(word: str) -> str:
    word = (word or "").strip().casefold()
    return {
        "web_series": "web",
        "web": "web",
        "web series": "web",
        "movies": "movies",
        "movie": "movies",
        "films": "movies",
        "film": "movies",
        "anime": "anime",
        "series": "series",
        "tv": "series",
        "shows": "series",
        "show": "series",
        "all": "all",
        "": "all",
    }.get(word, "all")


def _kind_of_word(word: str):
    word = (word or "").strip().casefold()
    if word == "movies":
        return MediaKind.MOVIE
    if word == "anime":
        return MediaKind.ANIME
    if word in ("series", "web_series"):
        return MediaKind.TV_SERIES
    return None


# Local alias so the import at the top stays tidy.
from .http import HttpClient as _Client  # noqa: E402
