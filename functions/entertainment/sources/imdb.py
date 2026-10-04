"""IMDb's public suggestion service — identity, autocomplete, popularity.

``https://v3.sg.media-imdb.com/suggestion/<letter>/<query>.json`` is the
key-less endpoint IMDb's own search box uses. It returns titles (with
IMDb ids, kinds, years and a popularity ``rank``), franchises and people.

It is the fastest, most reliable way to turn "one p" into One Piece, and
the ``rank`` field powers our popularity ordering. The main IMDb title
page can also be fetched (best-effort) for its embedded JSON-LD rating.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..http import HttpError, HttpClient
from ..models import Identity, MediaKind
from ..textutil import looks_japanese

log = logging.getLogger(__name__)

SUGGEST_URL = "https://v3.sg.media-imdb.com/suggestion/{prefix}/{query}.json"
TITLE_URL = "https://www.imdb.com/title/{imdb_id}/"

# IMDb suggestion "qid" values → our kinds. People are deliberately kept
# (the user can ask about a director) but ranked below media.
_QID_KINDS = {
    "movie": MediaKind.MOVIE,
    "tvmovie": MediaKind.MOVIE,
    "video": MediaKind.SPECIAL,
    "tvSeries": MediaKind.TV_SERIES,
    "tvMiniSeries": MediaKind.MINISERIES,
    "short": MediaKind.SHORT,
    "tvShort": MediaKind.SHORT,
    "tvSpecial": MediaKind.SPECIAL,
    "tvEpisode": MediaKind.UNKNOWN,  # deprioritised by the resolver
    "videoGame": MediaKind.UNKNOWN,  # not entertainment media for us
    "podcastSeries": MediaKind.UNKNOWN,
    "musicVideo": MediaKind.SPECIAL,
    "name": MediaKind.PERSON,
    "franchise": MediaKind.FRANCHISE,
    "character": MediaKind.PERSON,
}

_BROWISER_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}


def kind_from_qid(qid: str | None, q_text: str | None = None) -> MediaKind:
    if qid and qid in _QID_KINDS:
        return _QID_KINDS[qid]
    if q_text:
        lowered = q_text.lower()
        if "series" in lowered:
            return MediaKind.TV_SERIES
        if "feature" in lowered or "film" in lowered:
            return MediaKind.MOVIE
        if "game" in lowered:
            return MediaKind.UNKNOWN
    return MediaKind.UNKNOWN


def _popularity_from_rank(rank: int | None) -> float:
    """Map an IMDb popularity rank to a 0–100 score (lower rank = hotter)."""
    if not rank or rank <= 0:
        return 5.0
    if rank <= 50:
        return 100.0
    if rank <= 200:
        return 90.0
    if rank <= 1000:
        return 75.0
    if rank <= 3000:
        return 60.0
    if rank <= 10_000:
        return 45.0
    if rank <= 50_000:
        return 25.0
    return 10.0


def _identity_from_suggestion(item: dict[str, Any]) -> Identity | None:
    imdb_id = str(item.get("id") or "")
    title = str(item.get("l") or "").strip()
    if not imdb_id or not title:
        return None
    if not (imdb_id.startswith(("tt", "nm", "in"))):
        return None

    kind = kind_from_qid(item.get("qid"), item.get("q"))
    if imdb_id.startswith("in"):
        # IMDb "interest" nodes: franchises are useful disambiguators,
        # subgenres/lists are autocomplete noise.
        if "franchise" in str(item.get("s") or "").casefold():
            kind = MediaKind.FRANCHISE
        else:
            return None
    year = item.get("y")
    year_end = None
    yr = str(item.get("yr") or "")
    if "-" in yr or "–" in yr:
        parts = re.split(r"[-–]", yr, maxsplit=1)
        if len(parts) == 2:
            start, end = parts[0].strip(), parts[1].strip()
            if start.isdigit() and end.isdigit():
                year = int(start)
                year_end = int(end)

    identity = Identity(
        title=title,
        kind=kind,
        year=int(year) if isinstance(year, (int, float)) else None,
        year_end=year_end,
        imdb_id=imdb_id if imdb_id.startswith("tt") else "",
        poster=str((item.get("i") or {}).get("imageUrl") or ""),
        popularity=_popularity_from_rank(item.get("rank")),
        source="IMDb",
    )
    stars = str(item.get("s") or "")
    if stars and kind is MediaKind.PERSON and looks_japanese(stars):
        identity.alt_titles.append(stars)
    return identity


class ImdbSource:
    """IMDb suggestions + best-effort title-page rating."""

    def __init__(self, client: HttpClient, *, timeout: float = 5.0) -> None:
        self.client = client
        self.timeout = timeout
        # Set when the last suggest call failed at the transport level.
        # The service uses this to tell "no results" from "network down".
        self.last_error: str = ""

    async def suggest(self, query: str, limit: int = 12) -> list[Identity]:
        query = (query or "").strip()
        if not query:
            return []
        # The path segment must be alphanumeric; IMDb indexes by first char.
        cleaned = re.sub(r"[^a-z0-9]", "", query.casefold()) or "x"
        url = SUGGEST_URL.format(prefix=cleaned[0], query=cleaned)
        try:
            data = await self.client.get_json(url, timeout=self.timeout, max_bytes=512 * 1024)
        except HttpError as error:
            log.debug("IMDb suggest failed for %r: %s", query, error)
            self.last_error = str(error)
            return []
        self.last_error = ""
        items = data.get("d") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        identities: list[Identity] = []
        for item in items[: max(limit * 2, 20)]:
            if not isinstance(item, dict):
                continue
            identity = _identity_from_suggestion(item)
            if identity is not None:
                identities.append(identity)
        return identities[:limit]

    async def title_page_rating(self, imdb_id: str) -> dict[str, Any] | None:
        """Scrape the IMDb title page's JSON-LD for rating + release date.

        Best-effort: IMDb sometimes refuses datacenter traffic; every
        failure just means "no IMDb rating this time".
        """
        if not imdb_id.startswith("tt"):
            return None
        url = TITLE_URL.format(imdb_id=imdb_id)
        try:
            html = await self.client.get_text(
                url, timeout=8.0, max_bytes=3 * 1024 * 1024, headers=_BROWISER_UA
            )
        except HttpError as error:
            log.debug("IMDb page fetch failed for %s: %s", imdb_id, error)
            return None
        return parse_imdb_jsonld(html, imdb_id)


_AGGREGATE_RE = re.compile(
    r'"aggregateRating"\s*:\s*\{[^{}]*?"ratingValue"\s*:\s*"?([\d.]+)"?[^{}]*?'
    r'"ratingCount"\s*:\s*"?([\d,]+)"?',
    re.DOTALL,
)
_DATE_RE = re.compile(r'"datePublished"\s*:\s*"([\d-]{4,10})"')
_CONTENT_RE = re.compile(r'"contentRating"\s*:\s*"([^"]{1,12})"')
_TITLE_RE = re.compile(r'"name"\s*:\s*"([^"]{1,200}?)"')
_GENRE_RE = re.compile(r'"genre"\s*:\s*\[([^\]]{1,300})\]')


def parse_imdb_jsonld(html: str, imdb_id: str) -> dict[str, Any] | None:
    """Extract rating/date/genre from an IMDb page without a full DOM."""
    result: dict[str, Any] = {"imdb_id": imdb_id}
    match = _AGGREGATE_RE.search(html)
    if match:
        try:
            result["rating"] = float(match.group(1))
            result["votes"] = int(match.group(2).replace(",", ""))
        except ValueError:
            pass
    match = _DATE_RE.search(html)
    if match:
        result["date"] = match.group(1)
    match = _CONTENT_RE.search(html)
    if match:
        result["content_rating"] = match.group(1)
    match = _GENRE_RE.search(html)
    if match:
        genres = re.findall(r'"([^"]+)"', match.group(1))
        if genres:
            result["genres"] = genres[:5]
    if not (result.get("rating") or result.get("date") or result.get("genres")):
        return None
    return result


def imdb_url(imdb_id: str) -> str | None:
    return TITLE_URL.format(imdb_id=imdb_id) if imdb_id.startswith("tt") else None
