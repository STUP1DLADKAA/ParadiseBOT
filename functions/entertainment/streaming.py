"""Where-to-watch, region-aware, live-verified.

Streaming availability is the fastest-rotating fact in entertainment, so
this engine only trusts two kinds of evidence:

1. **structured links** — a database entry that points at an official
   platform page (AniList/MyAnimeList for anime, TVMaze's web channel
   for streamer originals);
2. **live web search** — a platform's *own domain* showing up in search
   results for the title (optionally with region words).

It never guesses from "similar titles are on Netflix", and it always
states the region it checked.
"""

from __future__ import annotations

import logging

from .models import Identity, StreamingOffer
from .sources.anilist import AniListSource, media_facts
from .sources.jikan import JikanSource, anime_facts
from .sources.tvmaze import TvMazeSource, show_facts
from .sources.websearch import WebSearchSource

log = logging.getLogger(__name__)


class StreamingEngine:
    def __init__(
        self,
        anilist: AniListSource,
        jikan: JikanSource,
        tvmaze: TvMazeSource,
        web: WebSearchSource,
    ) -> None:
        self.anilist = anilist
        self.jikan = jikan
        self.tvmaze = tvmaze
        self.web = web

    async def where_to_watch(
        self,
        identity: Identity,
        *,
        region: str = "",
        platform_filter: str = "",
    ) -> tuple[list[StreamingOffer], list[str]]:
        """Returns (offers, notes) for one identity in one region."""
        offers: list[StreamingOffer] = []
        notes: list[str] = []
        seen: set[str] = set()

        def add(platform: str, source: str, url: str | None = None, offer_region: str = "") -> None:
            key = f"{platform.casefold()}|{offer_region.casefold()}"
            if platform and key not in seen:
                seen.add(key)
                offers.append(
                    StreamingOffer(platform=platform, region=offer_region, url=url, source=source)
                )

        # 1. Anime structured links.
        media = await self.anilist.best_match(identity.title)
        if media:
            for link in media_facts(media).get("external_links") or []:
                if str(link.get("type") or "").lower() == "streaming":
                    add(
                        str(link.get("site") or ""),
                        "AniList",
                        str(link.get("url") or "") or None,
                        str(link.get("region") or ""),
                    )
            if media.get("idMal"):
                anime = await self.jikan.anime_full(int(media["idMal"]))
                if anime:
                    for entry in anime_facts(anime).get("streaming") or []:
                        add(entry.get("name", ""), "MyAnimeList", entry.get("url"))

        # 2. Streamer originals from TVMaze ("made by Netflix" etc.).
        if identity.kind.is_series:
            show = None
            if identity.imdb_id:
                show = await self.tvmaze.lookup_by_imdb(identity.imdb_id)
            if show is None and identity.tvmaze_id:
                show = await self.tvmaze.show(identity.tvmaze_id)
            if show:
                facts = show_facts(show)
                if facts.get("web_channel"):
                    add(facts["web_channel"], "TVMaze")
                    notes.append(
                        f"{facts['web_channel']} is the original network/streamer (per TVMaze)"
                    )

        # 3. The live web, region-scoped.
        web_offers = await self.web.find_streaming(identity.title, region=region)
        for offer in web_offers:
            add(offer.platform, offer.source, offer.url, offer.region or region)

        if platform_filter:
            wanted = platform_filter.casefold().replace("+", "").replace(" ", "")
            offers = [
                offer
                for offer in offers
                if wanted in offer.platform.casefold().replace(" ", "")
            ]
            if not offers:
                notes.append(
                    f"couldn't verify {platform_filter} availability from live sources"
                    + (f" in {region}" if region else "")
                    + " — availability changes by region and date"
                )

        if region:
            notes.append(f"region checked: {region}")
        else:
            notes.append("availability differs by region — name one (e.g. \"in India\") to narrow it down")

        return offers, notes
