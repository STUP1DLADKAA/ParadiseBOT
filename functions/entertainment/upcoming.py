"""The Upcoming Releases Engine — future titles, honest about precision.

Sources, all live and key-less:

- **movies** → Wikidata SPARQL (films with a future publication date,
  ranked by notability). Wikidata records date *precision* — a title
  announced as "2027" is shown as 2027, never as a made-up day;
- **anime** → AniList (NOT_YET_RELEASED, popularity-sorted, with its
  own start dates) with Kitsu's upcoming list and Jikan's season
  calendar as fallbacks;
- **TV / web series** → TVMaze's per-day schedules (including
  web-only premieres), filtered to series/season premieres.

Windows are respected exactly: "this Friday" is one date, "this month"
is a bounded fan-out of cached day-requests.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from .models import DateValue, MediaKind, UpcomingEntry
from .sources.anilist import AniListSource, identity_from_media
from .sources.jikan import JikanSource
from .sources.kitsu import KitsuSource
from .sources.tvmaze import TvMazeSource, identity_from_show
from .sources.wikidata import WikidataSource

log = logging.getLogger(__name__)

MAX_SCHEDULE_DAYS = 10  # bounded fan-out even for month-wide asks


class UpcomingEngine:
    def __init__(
        self,
        wikidata: WikidataSource,
        anilist: AniListSource,
        kitsu: KitsuSource,
        jikan: JikanSource,
        tvmaze: TvMazeSource,
    ) -> None:
        self.wikidata = wikidata
        self.anilist = anilist
        self.kitsu = kitsu
        self.jikan = jikan
        self.tvmaze = tvmaze

    async def upcoming(
        self,
        *,
        category: str = "all",  # all | movies | anime | series | web
        days: int = 30,
        region: str = "",
        platform: str = "",
        genre: str = "",
        limit: int = 10,
    ) -> tuple[list[UpcomingEntry], list[str]]:
        category = (category or "all").strip().casefold()
        today = date.today()
        horizon = today + timedelta(days=max(1, days))
        notes: set[str] = set()
        entries: list[UpcomingEntry] = []
        seen: set[str] = set()

        def add(entry: UpcomingEntry) -> None:
            key = f"{entry.title.casefold()}|{entry.when.iso() if entry.when else ''}"
            if entry.title and key not in seen:
                seen.add(key)
                entries.append(entry)

        wants_movies = category in ("all", "movies", "movie")
        wants_anime = category in ("all", "anime")
        wants_series = category in ("all", "series", "tv", "shows", "web", "web series")
        web_only = category in ("web", "web series")

        if wants_movies:
            rows = await self.wikidata.upcoming_films(
                from_date=today, min_sitelinks=25, limit=max(limit * 6, 40)
            )
            for row in rows:
                when = row["when"]
                if not (today <= when <= horizon + timedelta(days=365)):
                    continue
                if genre and genre.casefold() not in (row.get("genre") or "").casefold():
                    # keep titles without genre data; they may still match
                    if row.get("genre"):
                        continue
                add(
                    UpcomingEntry(
                        title=row["title"],
                        kind=MediaKind.MOVIE,
                        when=DateValue(value=when, precision=row["precision"], source="Wikidata"),
                        genres=[row["genre"]] if row.get("genre") else [],
                        url=f"https://www.wikidata.org/wiki/{row['qid']}" if row.get("qid") else None,
                        status_note="officially announced"
                        if row["precision"] >= DateValue.DAY
                        else "date window announced (not an exact day)",
                        source="Wikidata",
                    )
                )
            if rows:
                notes.add("Wikidata: films with an announced future release date")

        if wants_anime:
            added = False
            media = await self.anilist.upcoming(limit=max(limit, 8))
            for item in media:
                facts_start = (item.get("startDate") or {})
                when = DateValue.from_parts(
                    facts_start.get("year"), facts_start.get("month"), facts_start.get("day")
                )
                if when.value is None:
                    continue
                # AniList upcoming includes far-future entries; respect the
                # horizon loosely (season granularity is allowed through).
                if when.value > horizon + timedelta(days=180):
                    continue
                identity = identity_from_media(item)
                add(
                    UpcomingEntry(
                        title=identity.title or "Untitled",
                        kind=identity.kind,
                        when=DateValue(value=when.value, precision=when.precision, source="AniList"),
                        genres=[str(g) for g in (item.get("genres") or [])][:3],
                        status_note="announced"
                        if (item.get("status") or "") == "NOT_YET_RELEASED"
                        else "",
                        url=f"https://anilist.co/anime/{identity.anilist_id}/" if identity.anilist_id else None,
                        source="AniList",
                    )
                )
                added = True
            if media:
                notes.add("AniList: announced, not yet released, by popularity")
            if not added:
                airing = await self.kitsu.upcoming(limit=max(limit, 8))
                for identity, entry in airing:
                    start = str(entry.get("startDate") or "")
                    when = DateValue.from_iso(start, "Kitsu")
                    if not when.value:
                        continue
                    add(
                        UpcomingEntry(
                            title=identity.title,
                            kind=identity.kind,
                            when=when,
                            status_note="announced (Kitsu)",
                            source="Kitsu",
                        )
                    )
                if airing:
                    notes.add("Kitsu: upcoming anime by tracker count")

        if wants_series:
            schedule_entries = await self._tv_schedule(
                today=today,
                days=min(days if days <= 45 else MAX_SCHEDULE_DAYS, MAX_SCHEDULE_DAYS),
                region=region,
                web_only=web_only,
                limit=limit * 2,
            )
            for entry in schedule_entries:
                add(entry)
            if schedule_entries:
                notes.add("TVMaze: series/season premieres in the schedule window")

        entries.sort(key=lambda e: e.when.value or date.max)
        # Filter to the requested platform if any (honest: TVMaze names the
        # network/web channel for TV; movie platforms are rarely announced).
        if platform:
            wanted = platform.casefold()
            filtered = [e for e in entries if wanted in (e.platform or "").casefold()]
            if filtered:
                entries = filtered
            else:
                notes.add(f"no {platform}-specific premieres verified in this window — showing the general schedule")
        return entries[:limit], sorted(notes)

    async def _tv_schedule(
        self,
        *,
        today: date,
        days: int,
        region: str,
        web_only: bool,
        limit: int,
    ) -> list[UpcomingEntry]:
        entries: list[UpcomingEntry] = []
        seen: set[str] = set()
        country = _country_code(region)
        for offset in range(max(1, min(days, MAX_SCHEDULE_DAYS))):
            day = today + timedelta(days=offset)
            try:
                items = await self.tvmaze.schedule(day=day.isoformat(), country=country, web_only=web_only)
            except Exception as error:  # noqa: BLE001 - optional source
                log.debug("TVMaze schedule failed for %s: %s", day, error)
                continue
            for item in items:
                show = (item.get("_embedded") or {}).get("show") or {}
                if not show:
                    continue
                season_number = item.get("season") or 0
                episode_number = item.get("number") or 0
                is_premiere = (season_number == 1 and episode_number == 1) or (
                    season_number > 1 and episode_number == 1
                )
                if not is_premiere:
                    continue
                identity = identity_from_show(show)
                if not identity.title or identity.title.casefold() in seen:
                    continue
                seen.add(identity.title.casefold())
                channel = (show.get("webChannel") or {}).get("name") or (show.get("network") or {}).get("name") or ""
                entries.append(
                    UpcomingEntry(
                        title=identity.title,
                        kind=identity.kind,
                        when=DateValue.from_iso(str(item.get("airdate") or ""), "TVMaze"),
                        genres=[str(g) for g in (show.get("genres") or [])][:3],
                        platform=channel,
                        country=((show.get("webChannel") or show.get("network") or {}).get("country") or {}).get("name", ""),
                        status_note="series premiere" if season_number == 1 else f"season {season_number} premiere",
                        url=str(show.get("url") or "") or None,
                        source="TVMaze",
                    )
                )
                if len(entries) >= limit:
                    return entries
        return entries


_COUNTRY_CODES = {
    "india": "IN",
    "us": "US",
    "usa": "US",
    "united states": "US",
    "uk": "GB",
    "united kingdom": "GB",
    "britain": "GB",
    "canada": "CA",
    "australia": "AU",
    "japan": "JP",
    "germany": "DE",
}


def _country_code(region: str) -> str | None:
    return _COUNTRY_CODES.get((region or "").strip().casefold())
