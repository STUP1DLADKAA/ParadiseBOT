"""The Autocomplete Engine — the suggestion box under ``/example``.

Requirements it must satisfy under Discord's 3-second interaction
deadline:

- start suggesting as soon as the user types after ``/example``;
- rank: exact match → prefix → relevance (fuzzy, typo-tolerant) →
  popularity, with screen titles preferred over people/episodes;
- ~5–10 suggestions, never a wall of noise;
- instant local answers (every title the server has ever resolved)
  merged with a live IMDb lookup, so even a slow network still shows
  something and typing never feels laggy;
- selecting a suggestion inserts a clean title; the actual lookup after
  Send is always a *fresh* internet search (volatile data refreshes).
"""

from __future__ import annotations

import asyncio
import logging

from .cache import TitleMemory
from .models import Identity, MediaKind, kind_label
from .sources.imdb import ImdbSource
from .textutil import normalize_title

log = logging.getLogger(__name__)

MAX_CHOICES = 10        # Discord allows 25; we deliberately show fewer
MIN_CHOICES = 5
LIVE_BUDGET = 1.4       # seconds the live lookup may take, hard cap
MIN_QUERY_FOR_LIVE = 2  # characters before hitting the network


class AutocompleteEngine:
    def __init__(self, imdb: ImdbSource, memory: TitleMemory) -> None:
        self.imdb = imdb
        self.memory = memory

    async def suggest(self, query: str, *, limit: int = 8) -> list[Identity]:
        """A ranked suggestion list for the current partial input."""
        query = (query or "").strip()
        if not query:
            return []

        local = self.memory.search(query, limit=MAX_CHOICES)
        live: list[Identity] = []
        if len(normalize_title(query)) >= MIN_QUERY_FOR_LIVE:
            try:
                live = await asyncio.wait_for(
                    self.imdb.suggest(query, limit=MAX_CHOICES), timeout=LIVE_BUDGET
                )
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001 - deadline wins
                live = []
            self.memory.remember_identities(live)

        combined = _dedupe(local + live)
        ranked = sorted(combined, key=lambda identity: _score(query, identity), reverse=True)
        return ranked[: max(MIN_CHOICES, limit)]

    def choices(self, identities: list[Identity], limit: int = 8) -> list[tuple[str, str]]:
        """(display, insert-value) pairs ready for Discord's autocomplete."""
        out: list[tuple[str, str]] = []
        seen_values: set[str] = set()
        for identity in identities[:limit]:
            value = identity.title[:100]
            if value.casefold() in seen_values:
                value = _disambiguated_value(identity)
            if value.casefold() in seen_values:
                continue
            seen_values.add(value.casefold())
            out.append((f"{_display(identity)}"[:100], value))
        return out


def _display(identity: Identity) -> str:
    bits = [identity.title]
    if identity.year:
        if identity.year_end and identity.year_end != identity.year:
            bits.append(f"{identity.year}–{identity.year_end}")
        else:
            bits.append(str(identity.year))
    bits.append(kind_label(identity.kind))
    return " · ".join(bits)


def _disambiguated_value(identity: Identity) -> str:
    """Two different titles with the same name? Add the year to one."""
    if identity.year:
        return f"{identity.title} ({identity.year})"[:100]
    return identity.title[:100]


def _dedupe(identities: list[Identity]) -> list[Identity]:
    seen: set[str] = set()
    out: list[Identity] = []
    for identity in identities:
        key = identity.imdb_id or f"{normalize_title(identity.title)}|{identity.year}"
        if key in seen or not identity.title:
            continue
        seen.add(key)
        out.append(identity)
    return out


def _score(query: str, identity: Identity) -> float:
    """Exact > prefix > word-prefix > fuzzy > popularity; titles > people."""
    query_norm = normalize_title(query)
    title_norm = normalize_title(identity.title)
    score = 0.0

    if query_norm == title_norm:
        score += 1000.0
    elif title_norm.startswith(query_norm):
        score += 700.0 - min(len(title_norm) - len(query_norm), 25)
    elif f" {query_norm}" in f" {title_norm}":
        score += 420.0
    else:
        from .textutil import title_match_score

        fuzzy = title_match_score(query, identity.title, identity.alt_titles)
        if fuzzy >= 85.0:  # typo-tolerant matches only; no noise below
            score += fuzzy * 3.0
        else:
            score += fuzzy * 0.5  # weak matches sink but stay findable

    score += identity.popularity * 1.2

    # Screen titles are what a /example user means; people and
    # franchises stay reachable but lose ties.
    if identity.kind is MediaKind.PERSON:
        score -= 400.0
    elif identity.kind is MediaKind.FRANCHISE:
        score -= 90.0
    elif identity.kind is MediaKind.UNKNOWN:
        score -= 300.0

    # A franchise query deserves franchise results.
    if identity.kind is MediaKind.FRANCHISE and query_norm == title_norm:
        score += 150.0

    return score
