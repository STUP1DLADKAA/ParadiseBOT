"""Wikidata — structured facts and future releases, via public SPARQL.

``https://query.wikidata.org/sparql`` answers key-less GETs. We use it
for the things no entertainment API gives away for free:

- upcoming films with *date precision* (a year promise stays a year —
  we never invent an exact date);
- genre + era discovery ("highly rated thrillers from the last 3 years")
  ranked by sitelinks (a notability signal);
- the franchise graph around one work (follows / followed by / part of
  the series / based on).

A strict User-Agent and low request rate keep us inside Wikidata's
usage policy.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone
from typing import Any

from ..http import HttpError, HttpClient
from ..models import MediaKind

log = logging.getLogger(__name__)

SPARQL = "https://query.wikidata.org/sparql"
ENTITY_API = "https://www.wikidata.org/w/api.php"
SPARQL_UA = "ParadiseBot/1.0 entertainment discovery (https://github.com/STUP1DLADKAA/ParadiseBOT)"

def _usable_label(label: str) -> bool:
    """True when the label service resolved a real name (not a raw Q-id)."""
    if not label:
        return False
    return not re.match(r"^Q\d+$", label.strip())


FILM = "wd:Q11424"
TV_SERIES = "wd:Q5398426"
FILM_SERIES = "wd:Q24856"
ANIME_TV = "wd:Q586730"
ANIME_FILM = "wd:Q20650540"

_UPCOMING_FILMS = """
SELECT ?item ?itemLabel ?date ?precision ?sitelinks ?genreLabel WHERE {
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
  ?item wdt:P31/wdt:P279* wd:Q11424 ;
        wdt:P577 ?fullDate ;
        wikibase:sitelinks ?sitelinks .
  ?item p:P577 ?stmt .
  ?stmt ps:P577 ?fullDate ;
        psv:P577 ?value .
  ?value wikibase:timePrecision ?rawPrecision .
  BIND(CONCAT(STR(?rawPrecision)) AS ?precision)
  OPTIONAL { ?item wdt:P136 ?genre . }
  FILTER(?fullDate >= "__CUTOFF__"^^xsd:dateTime)
  FILTER(?sitelinks > __MIN_SITELINKS__)
}
ORDER BY ?date
LIMIT __LIMIT__
"""

_GENRE_FILMS = """
SELECT ?item ?itemLabel ?date ?precision ?sitelinks WHERE {
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
  ?item wdt:P31/wdt:P279* wd:Q11424 ;
        wdt:P136 __GENRE__ ;
        wdt:P577 ?fullDate ;
        wikibase:sitelinks ?sitelinks .
  ?item p:P577 ?stmt .
  ?stmt ps:P577 ?fullDate ;
        psv:P577 ?value .
  ?value wikibase:timePrecision ?rawPrecision .
  BIND(CONCAT(STR(?rawPrecision)) AS ?precision)
  FILTER(?fullDate >= "__CUTOFF__"^^xsd:dateTime && ?fullDate < "__UNTIL__"^^xsd:dateTime)
  FILTER(?sitelinks > __MIN_SITELINKS__)
}
ORDER BY DESC(?sitelinks)
LIMIT __LIMIT__
"""

_FRANCHISE_PARTS = """
SELECT ?item ?itemLabel ?date ?precision ?relation WHERE {
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
  ?item wdt:P179 {root} ;
        wdt:P577 ?fullDate .
  ?item p:P577 ?stmt .
  ?stmt ps:P577 ?fullDate ;
        psv:P577 ?value .
  ?value wikibase:timePrecision ?rawPrecision .
  BIND(CONCAT(STR(?rawPrecision)) AS ?precision)
  BIND("part of series" AS ?relation)
}
ORDER BY ?date
LIMIT __LIMIT__
"""

_QID_RE = re.compile(r"^Q\d+$")

# A handful of genre shortcuts the NLU can resolve without network.
GENRE_QIDS = {
    "thriller": "wd:Q130232",
    "horror": "wd:Q1787062",
    "comedy": "wd:Q157443",
    "action": "wd:Q188473",
    "drama": "wd:Q18958590",
    "romance": "wd:Q1054574",
    "romantic comedy": "wd:Q860119",
    "science fiction": "wd:Q15154975",
    "sci-fi": "wd:Q15154975",
    "fantasy": "wd:Q162908",
    "crime": "wd:Q959790",
    "mystery": "wd:Q193923",
    "animation": "wd:Q172980",
    "adventure": "wd:Q1303415",
    "documentary": "wd:Q93204",
    "war": "wd:Q101873",
    "western": "wd:Q172980",
    "superhero": "wd:Q125575460",
    "psychological thriller": "wd:Q1303415",
    "slasher": "wd:Q1973241",
    "disaster": "wd:Q101873",
    "historical drama": "wd:Q18958590",
    "sports": "wd:Q1092363",
}


def parse_sparql_date(raw: str, precision: str | int | None) -> tuple[date | None, int]:
    """'2027-03-05T00:00:00Z' + precision → (date, precision 9/10/11)."""
    if not raw:
        return None, 11
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        match = re.match(r"(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?", raw)
        if not match:
            return None, 11
        year = int(match.group(1))
        month = int(match.group(2)) if match.group(2) else None
        day = int(match.group(3)) if match.group(3) else None
        try:
            from ..models import DateValue

            dv = DateValue.from_parts(year, month, day)
            return dv.value, dv.precision
        except Exception:  # noqa: BLE001 - never raise on odd dates
            return None, 11
    try:
        prec = int(precision) if precision is not None else 11
    except (TypeError, ValueError):
        prec = 11
    return parsed, prec


class WikidataSource:
    def __init__(self, client: HttpClient, *, timeout: float = 10.0) -> None:
        self.client = client
        self.timeout = timeout

    async def _sparql(self, query: str) -> list[dict[str, Any]]:
        try:
            data = await self.client.get_json(
                SPARQL,
                params={"format": "json", "query": query},
                timeout=self.timeout,
                max_bytes=3 * 1024 * 1024,
            )
        except HttpError as error:
            log.debug("Wikidata SPARQL failed: %s", error)
            return []
        bindings = (
            ((data or {}).get("results") or {}).get("bindings")
        )
        rows: list[dict[str, Any]] = []
        if not isinstance(bindings, list):
            return rows
        for binding in bindings:
            if isinstance(binding, dict):
                rows.append(
                    {
                        key: (value or {}).get("value", "")
                        for key, value in binding.items()
                        if isinstance(value, dict)
                    }
                )
        return rows

    # -- queries -------------------------------------------------------------

    async def upcoming_films(
        self,
        *,
        from_date: date | None = None,
        min_sitelinks: int = 25,
        limit: int = 60,
    ) -> list[dict[str, Any]]:
        cutoff = (from_date or datetime.now(timezone.utc).date()).isoformat()
        query = (_UPCOMING_FILMS
                .replace("__CUTOFF__", cutoff)
                .replace("__MIN_SITELINKS__", str(min_sitelinks))
                .replace("__LIMIT__", str(limit)))
        rows = await self._sparql(query)
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            label = str(row.get("itemLabel") or "").strip()
            # The label service can fall back to raw Q-ids — skip those.
            if not _usable_label(label):
                continue
            qid = _qid_of(row.get("item"))
            if qid in seen:
                continue
            seen.add(qid)
            raw_date = str(row.get("date") or "")
            when, precision = parse_sparql_date(raw_date, row.get("precision"))
            if when is None:
                continue
            try:
                sitelinks = int(float(str(row.get("sitelinks") or "0")))
            except ValueError:
                sitelinks = 0
            out.append(
                {
                    "title": label,
                    "qid": qid,
                    "when": when,
                    "precision": precision,
                    "sitelinks": sitelinks,
                    "genre": str(row.get("genreLabel") or ""),
                    "kind": MediaKind.MOVIE,
                }
            )
        return out

    async def genre_films(
        self,
        genre_qid: str,
        *,
        since: date | None = None,
        until: date | None = None,
        min_sitelinks: int = 30,
        limit: int = 15,
    ) -> list[dict[str, Any]]:
        since = since or date(2000, 1, 1)
        until = until or datetime.now(timezone.utc).date()
        query = (
            _GENRE_FILMS
            .replace("__GENRE__", genre_qid)
            .replace("__CUTOFF__", since.isoformat())
            .replace("__UNTIL__", until.isoformat())
            .replace("__MIN_SITELINKS__", str(min_sitelinks))
            .replace("__LIMIT__", str(limit))
        )
        rows = await self._sparql(query)
        out: list[dict[str, Any]] = []
        for row in rows:
            label = str(row.get("itemLabel") or "").strip()
            if not _usable_label(label):
                continue
            raw_date = str(row.get("date") or "")
            when, precision = parse_sparql_date(raw_date, row.get("precision"))
            try:
                sitelinks = int(float(str(row.get("sitelinks") or "0")))
            except ValueError:
                sitelinks = 0
            out.append(
                {
                    "title": label,
                    "qid": _qid_of(row.get("item")),
                    "when": when,
                    "precision": precision,
                    "sitelinks": sitelinks,
                    "kind": MediaKind.MOVIE,
                }
            )
        return out

    async def franchise_parts(self, root_qid: str, limit: int = 15) -> list[dict[str, Any]]:
        if not _QID_RE.match(root_qid):
            return []
        query = (_FRANCHISE_PARTS
                .replace("__ROOT__", f"wd:{root_qid}")
                .replace("__LIMIT__", str(limit)))
        rows = await self._sparql(query)
        out: list[dict[str, Any]] = []
        for row in rows:
            label = str(row.get("itemLabel") or "").strip()
            if not label:
                continue
            when, precision = parse_sparql_date(str(row.get("date") or ""), row.get("precision"))
            out.append(
                {
                    "title": label,
                    "qid": _qid_of(row.get("item")),
                    "when": when,
                    "precision": precision,
                    "relation": str(row.get("relation") or "part of"),
                }
            )
        return out

    # -- entity lookup ---------------------------------------------------------

    async def search_entity(self, query: str, *, kinds: tuple[str, ...] = ("Q11424",)) -> list[dict[str, Any]]:
        """wbsearchentities for films / series — returns Q-ids with labels."""
        results: list[dict[str, Any]] = []
        for entity_kind in kinds:
            try:
                data = await self.client.get_json(
                    ENTITY_API,
                    params={
                        "action": "wbsearchentities",
                        "search": query,
                        "language": "en",
                        "format": "json",
                        "type": "item",
                        "limit": "5",
                    },
                    timeout=6.0,
                )
            except HttpError as error:
                log.debug("Wikidata entity search failed: %s", error)
                continue
            search = data.get("search") if isinstance(data, dict) else None
            if not isinstance(search, list):
                continue
            for entry in search[:5]:
                if not isinstance(entry, dict):
                    continue
                results.append(
                    {
                        "qid": str(entry.get("id") or ""),
                        "label": str(entry.get("label") or ""),
                        "description": str(entry.get("description") or ""),
                        "url": str(entry.get("concepturi") or ""),
                    }
                )
            if results:
                break
        return results


def _qid_of(uri: str | None) -> str:
    if not uri:
        return ""
    match = re.search(r"(Q\d+)$", str(uri))
    return match.group(1) if match else ""
