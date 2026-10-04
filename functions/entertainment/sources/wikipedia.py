"""Wikipedia / Wikimedia — synopsis, infobox facts, and real buzz.

Four key-less endpoints, all officially public:

- REST summary: one clean paragraph + poster for a known article title;
- action API ``opensearch``: title completion (resolver fallback);
- action API revisions: raw wikitext → we parse the film/TV infobox
  (director, cast, release dates, runtime, seasons, episodes, budget…)
  and the Reception section (Rotten Tomatoes / Metacritic sentences);
- pageviews ``top``: the most-read articles of a day — a genuine,
  measurable "what is everyone looking at right now" signal.
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from typing import Any

from ..http import HttpError, HttpClient
from ..models import MediaKind, Rating
from ..textutil import clean_summary, parse_infobox, shorten, strip_html

log = logging.getLogger(__name__)

REST_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
ACTION_API = "https://en.wikipedia.org/w/api.php"
PAGEVIEWS_TOP = (
    "https://wikimedia.org/api/rest_v1/metrics/pageviews/top/"
    "en.wikipedia/all-access/{year}/{month:02d}/{day:02d}"
)

# Article titles that are obviously not one title (utility/service pages).
_ARTICLE_DENY = re.compile(
    r"^(Main_Page|Special:|Wikipedia:|Portal:|Help:|File:|Category:|Talk:|Template:|"
    r"Deaths?_in_|List_of_|Index_of_|Outline_of_|Glossary_of_|"
    r".*_\(disambiguation\))",
    re.IGNORECASE,
)

# Parenthetical disambiguators that mark an entertainment article.
_MEDIA_SUFFIX = re.compile(
    r"\((?:[^()]*\s)?(?:film|movie|series|miniseries|web\s?series|anime|manga|"
    r"franchise|media franchise|TV program|show|season \d+|season|episode|OVA|"
    r"video game|soundtrack|character|novel|short story)(?:\s[^()]*)?\)$",
    re.IGNORECASE,
)
# …but these disambiguators mean it is NOT about a screen title itself.
_NON_SCREEN = re.compile(
    r"\((?:novel|manga|video game|soundtrack|short story|song|album|play|book|"
    r"character|band|soundtrack album|franchise|media franchise)\)",
    re.IGNORECASE,
)

_RT_PATTERNS = [
    re.compile(
        r"Rotten Tomatoes[^.]{0,200}?(\d{1,3})%(?:[^.]{0,120}?(\d[\d,]*)\s*(?:of\s*)?reviews?)?",
        re.IGNORECASE,
    ),
    re.compile(
        r"(\d{1,3})%\s*(?:of\s*)?(?:the\s*)?(\d[\d,]*)?\s*reviews?[^.]{0,120}?Rotten Tomatoes",
        re.IGNORECASE,
    ),
]
_METACRITIC_RE = re.compile(
    r"Metacritic[^.]{0,160}?(\d{1,3})(?:\s*(?:out of|/)\s*100)?", re.IGNORECASE
)
_IMDB_WIKI_RE = re.compile(
    r"IMDb[^.]{0,120}?([\d.]{1,4})\s*/\s*10", re.IGNORECASE
)


def article_is_media(article: str) -> bool:
    """Does a most-read article look like a movie/show/anime page?"""
    if _ARTICLE_DENY.search(article):
        return False
    # Article titles use underscores where prose would use spaces
    # ("Digger_(2026_film)").
    spaced = article.replace("_", " ")
    if not _MEDIA_SUFFIX.search(spaced):
        return False
    if _NON_SCREEN.search(spaced):
        return False
    return True


def display_title(article: str) -> str:
    """'Digger_(2026_film)' → 'Digger'; keeps qualifiers in parentheses."""
    return article.replace("_", " ").strip()


def kind_from_article(article: str) -> MediaKind:
    spaced = article.replace("_", " ").lower()
    if "web series" in spaced:
        return MediaKind.WEB_SERIES
    if "miniseries" in spaced:
        return MediaKind.MINISERIES
    if "anime" in spaced:
        return MediaKind.ANIME
    if "season" in spaced:
        return MediaKind.TV_SERIES
    if re.search(r"\((?:tv|television)\b[^()]*\)", spaced):
        return MediaKind.TV_SERIES
    if "film" in spaced or "movie" in spaced:
        return MediaKind.MOVIE
    if "franchise" in spaced:
        return MediaKind.FRANCHISE
    return MediaKind.UNKNOWN


class WikipediaSource:
    def __init__(self, client: HttpClient, *, timeout: float = 7.0) -> None:
        self.client = client
        self.timeout = timeout

    async def _get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        return await self.client.get_json(url, params=params, timeout=self.timeout)

    async def summary(self, article_title: str) -> dict[str, Any] | None:
        """REST summary for a (possibly underscored) article title."""
        slug = article_title.strip().replace(" ", "_")
        if not slug:
            return None
        url = REST_SUMMARY.format(title=slug)
        try:
            data = await self._get_json(url)
        except HttpError as error:
            log.debug("Wikipedia summary failed for %r: %s", article_title, error)
            return None
        if not isinstance(data, dict) or data.get("type") in ("https://mediawiki.org/wiki/HyperSwitch/errors/not_found", "404"):
            return None
        if not data.get("extract"):
            return None
        return {
            "title": data.get("title") or article_title,
            "extract": clean_summary(str(data.get("extract") or "")),
            "description": str(data.get("description") or ""),
            "thumbnail": str((data.get("thumbnail") or {}).get("source") or ""),
            "poster": str((data.get("originalimage") or {}).get("source") or ""),
            "url": str((data.get("content_urls") or {}).get("desktop", {}).get("page") or ""),
            "wikibase": str(data.get("wikibase_item") or ""),
        }

    async def opensearch(self, query: str, limit: int = 8) -> list[str]:
        try:
            data = await self._get_json(
                ACTION_API,
                {
                    "action": "opensearch",
                    "search": query,
                    "limit": str(limit),
                    "namespace": "0",
                    "format": "json",
                },
            )
        except HttpError as error:
            log.debug("Wikipedia opensearch failed for %r: %s", query, error)
            return []
        if isinstance(data, list) and len(data) >= 2 and isinstance(data[1], list):
            return [str(t) for t in data[1]]
        return []

    async def wikitext(self, article_title: str) -> str:
        """Raw wikitext of a page (section 0 = infobox) — best-effort."""
        try:
            data = await self._get_json(
                ACTION_API,
                {
                    "action": "query",
                    "prop": "revisions",
                    "rvprop": "content",
                    "rvslots": "main",
                    "rvsection": "0",
                    "format": "json",
                    "formatversion": "2",
                    "redirects": "1",
                    "titles": article_title,
                },
            )
        except HttpError as error:
            log.debug("Wikipedia wikitext failed for %r: %s", article_title, error)
            return ""
        try:
            pages = data["query"]["pages"]
            for page in pages:
                revision = (page.get("revisions") or [{}])[0]
                content = (revision.get("slots") or {}).get("main", {}).get("content")
                if content:
                    return str(content)
        except (KeyError, IndexError, TypeError):
            pass
        return ""

    async def full_wikitext(self, article_title: str) -> str:
        """Whole-article wikitext (used for Reception-section ratings)."""
        try:
            data = await self._get_json(
                ACTION_API,
                {
                    "action": "query",
                    "prop": "revisions",
                    "rvprop": "content",
                    "rvslots": "main",
                    "format": "json",
                    "formatversion": "2",
                    "redirects": "1",
                    "titles": article_title,
                },
            )
        except HttpError as error:
            log.debug("Wikipedia full wikitext failed for %r: %s", article_title, error)
            return ""
        try:
            pages = data["query"]["pages"]
            for page in pages:
                revision = (page.get("revisions") or [{}])[0]
                content = (revision.get("slots") or {}).get("main", {}).get("content")
                if content:
                    return str(content)
        except (KeyError, IndexError, TypeError):
            pass
        return ""

    async def most_viewed(self, *, day: date | None = None, max_age_days: int = 3) -> list[dict[str, Any]]:
        """The top ~1000 most-read English Wikipedia articles for a day.

        Tries yesterday, then further back (pageviews lag), up to
        ``max_age_days``. Returns [{article, views, rank, kind, title}].
        """
        day = day or (date.today() - timedelta(days=1))
        for offset in range(max_age_days):
            target = day - timedelta(days=offset)
            url = PAGEVIEWS_TOP.format(year=target.year, month=target.month, day=target.day)
            try:
                data = await self._get_json(url)
            except HttpError as error:
                log.debug("Wikipedia pageviews failed for %s: %s", target, error)
                continue
            items = data.get("items") if isinstance(data, dict) else None
            if not items or not isinstance(items[0].get("articles"), list):
                continue
            out = []
            for entry in items[0]["articles"]:
                article = str(entry.get("article") or "")
                if article_is_media(article):
                    out.append(
                        {
                            "article": article,
                            "title": display_title(article),
                            "views": int(entry.get("views") or 0),
                            "rank": int(entry.get("rank") or 0),
                            "kind": kind_from_article(article),
                            "date": target.isoformat(),
                        }
                    )
            if out:
                return out
        return []

    # -- infobox parsing ----------------------------------------------------

    def parse_film_infobox(self, wikitext: str) -> dict[str, str]:
        for template in ("Infobox film", "Infobox movie"):
            params = parse_infobox(wikitext, template)
            if params:
                return params
        return {}

    def parse_tv_infobox(self, wikitext: str) -> dict[str, str]:
        for template in (
            "Infobox television",
            "Infobox television series",
            "Infobox animanga/Video",
            "Infobox anime",
        ):
            params = parse_infobox(wikitext, template)
            if params:
                return params
        return {}

    @staticmethod
    def reception_ratings(wikitext: str) -> list[Rating]:
        """Rotten Tomatoes / Metacritic / IMDb as stated in the article.

        These are live quotes from the article's Reception section —
        attributed to Wikipedia citing the original source, never
        presented as a direct pull from the rating service.
        """
        ratings: list[Rating] = []
        text = strip_html(wikitext)
        if not text:
            return ratings

        for pattern in _RT_PATTERNS:
            match = pattern.search(text)
            if match:
                try:
                    value = float(match.group(1))
                except (TypeError, ValueError):
                    continue
                if 0 <= value <= 100:
                    votes = None
                    if match.group(2):
                        try:
                            votes = int(match.group(2).replace(",", ""))
                        except ValueError:
                            votes = None
                    ratings.append(
                        Rating(
                            source="Rotten Tomatoes",
                            value=value,
                            scale="%",
                            votes=votes,
                            via="Wikipedia (citing Rotten Tomatoes)",
                        )
                    )
                break

        match = _METACRITIC_RE.search(text)
        if match:
            try:
                value = float(match.group(1))
                if 0 <= value <= 100:
                    ratings.append(
                        Rating(
                            source="Metacritic",
                            value=value,
                            scale="/100",
                            via="Wikipedia (citing Metacritic)",
                        )
                    )
            except (TypeError, ValueError):
                pass

        match = _IMDB_WIKI_RE.search(text)
        if match:
            try:
                value = float(match.group(1))
                if 0 <= value <= 10:
                    ratings.append(
                        Rating(
                            source="IMDb",
                            value=value,
                            scale="/10",
                            via="Wikipedia",
                        )
                    )
            except (TypeError, ValueError):
                pass
        return ratings

    @staticmethod
    def shorten_extract(text: str, limit: int = 500) -> str:
        return shorten(text, limit)
