"""The Entertainment Resolver: query → what the user actually means.

Turns messy input ("nar", "one p", "aot s2", "kimetsu no yaiba") into a
ranked list of :class:`Identity` candidates, classifying each as movie /
TV / web series / anime / franchise / person, with typo tolerance via
fuzzy matching and cross-source merging.

Ranking follows the system's contract: **exact match > relevance >
popularity** — with a bias toward screen titles over people and
episodes, and toward the kind of thing people usually mean.
"""

from __future__ import annotations

import logging
from typing import Any

from .models import Identity, MediaKind
from .sources.anilist import AniListSource, identity_from_media
from .sources.imdb import ImdbSource
from .sources.kitsu import KitsuSource
from .sources.tvmaze import TvMazeSource
from .textutil import (
    looks_japanese,
    normalize_title,
    split_season_suffix,
    title_match_score,
)

log = logging.getLogger(__name__)

# Kinds that are almost never what a slash-command user meant.
_DEPRIORITIZED = (MediaKind.PERSON, MediaKind.UNKNOWN)


class Resolver:
    def __init__(
        self,
        imdb: ImdbSource,
        tvmaze: TvMazeSource,
        kitsu: KitsuSource,
        anilist: AniListSource,
    ) -> None:
        self.imdb = imdb
        self.tvmaze = tvmaze
        self.kitsu = kitsu
        self.anilist = anilist

    async def resolve(self, query: str, *, limit: int = 8) -> list[Identity]:
        """A ranked, de-duplicated candidate list for a query."""
        query = (query or "").strip()
        if not query:
            return []

        candidates: list[Identity] = []

        imdb_task = self.imdb.suggest(query, limit=max(limit, 10))
        kitsu_task = self.kitsu.search(query, limit=6)
        tvmaze_task = self.tvmaze.search_shows(query, limit=6)
        anilist_task = self.anilist.search(query)
        results = await _gather(imdb_task, kitsu_task, tvmaze_task, anilist_task)

        imdb_list, kitsu_list, tvmaze_list, anilist_list = results
        for identity in imdb_list or []:
            candidates.append(identity)
        for identity in kitsu_list or []:
            candidates.append(identity)
        for identity, _show in tvmaze_list or []:
            candidates.append(identity)
        for media in anilist_list or []:
            identity = identity_from_media(media)
            if identity.title:
                candidates.append(identity)

        merged = self.merge_and_rank(query, candidates)
        return merged[:limit]

    def merge_and_rank(self, query: str, candidates: list[Identity]) -> list[Identity]:
        """Dedupe candidates across sources and rank for the query."""
        by_key: dict[str, Identity] = {}

        for candidate in candidates:
            if not candidate.title:
                continue
            # Merge strategy 1: the same database id → one entry.
            key = self._key_of(candidate)
            existing = by_key.get(key)
            if existing is not None and not _mergeable(existing, candidate):
                # Same key but incompatible kinds (a person and a show
                # sharing a name, a film vs its series): keep them apart.
                key = f"{key}|{candidate.kind.value}"
                existing = by_key.get(key)
            if existing is None:
                by_key[key] = candidate
                continue
            by_key[key] = _merge_pair(existing, candidate)

        # Merge strategy 2: different databases describing the same work —
        # same normalized title, compatible kinds, and a year that agrees
        # (or is unknown on one side). "One Piece" 1999 ≠ "One Piece" 2023.
        groups: dict[str, list[str]] = {}
        for key, identity in by_key.items():
            groups.setdefault(normalize_title(identity.title), []).append(key)
        for keys in groups.values():
            if len(keys) < 2:
                continue
            base_key = keys[0]
            for other_key in keys[1:]:
                if other_key not in by_key or base_key not in by_key:
                    continue
                if _mergeable(by_key[base_key], by_key[other_key]):
                    by_key[base_key] = _merge_pair(by_key[base_key], by_key[other_key])
                    del by_key[other_key]

        ranked = sorted(
            by_key.values(),
            key=lambda identity: self._score(query, identity),
            reverse=True,
        )
        return ranked

    # -- scoring -------------------------------------------------------------

    @staticmethod
    def _key_of(identity: Identity) -> str:
        if identity.imdb_id:
            return f"tt:{identity.imdb_id}"
        if identity.kitsu_id and identity.kitsu_id.isdigit():
            return f"kitsu:{identity.kitsu_id}"
        if identity.anilist_id:
            return f"al:{identity.anilist_id}"
        if identity.tvmaze_id:
            return f"tv:{identity.tvmaze_id}"
        return f"n:{normalize_title(identity.title)}"

    def _score(self, query: str, identity: Identity) -> float:
        query_norm = normalize_title(query)
        title_norm = normalize_title(identity.title)
        alt_norms = [normalize_title(t) for t in identity.alt_titles if t]

        score = 0.0

        # 1. Exact match wins outright.
        if query_norm and query_norm == title_norm:
            score += 1000.0
        elif any(query_norm == alt for alt in alt_norms if alt):
            score += 900.0
        else:
            # 2. Relevance: fuzzy similarity, including alternates.
            score += title_match_score(query, identity.title, identity.alt_titles) * 4.0

        # 3. Popularity (normalized 0–100 per source).
        score += identity.popularity * 1.5

        # Kind adjustments: people and unknowns rarely win a title query,
        # and franchise hubs make fine answers but lose to concrete titles.
        if identity.kind in _DEPRIORITIZED:
            score -= 320.0
        elif identity.kind is MediaKind.FRANCHISE:
            score -= 60.0

        # Cross-source agreement: if two independent databases list the
        # same title, that's a strong signal the title is real and big.
        # (merge_and_rank folded them, so approximate via alt title count
        # and popularity carried over.)
        if identity.alt_titles:
            score += min(len(identity.alt_titles), 4) * 5.0

        # A romanized/Japanese alias match suggests an anime fan asking.
        if looks_japanese(query) and identity.kind.is_anime:
            score += 40.0

        return score


def _merge_pair(a: Identity, b: Identity) -> Identity:
    """Combine two identities for the same work, keeping the best facts."""
    base, extra = (a, b) if a.popularity >= b.popularity else (b, a)
    merged = Identity(
        title=base.title or extra.title,
        kind=_pick_kind(base.kind, extra.kind),
        year=base.year or extra.year,
        year_end=base.year_end or extra.year_end,
        imdb_id=base.imdb_id or extra.imdb_id,
        tvmaze_id=base.tvmaze_id or extra.tvmaze_id,
        kitsu_id=base.kitsu_id or extra.kitsu_id,
        anilist_id=base.anilist_id or extra.anilist_id,
        mal_id=base.mal_id or extra.mal_id,
        wikidata_id=base.wikidata_id or extra.wikidata_id,
        wiki_title=base.wiki_title or extra.wiki_title,
        poster=base.poster or extra.poster,
        popularity=min(100.0, max(base.popularity, extra.popularity) + 10.0),
        alt_titles=_unique(base.alt_titles + extra.alt_titles + [extra.title])[:8],
        source=base.source if base.source == extra.source else f"{base.source}+{extra.source}",
    )
    return merged


def _pick_kind(a: MediaKind, b: MediaKind) -> MediaKind:
    """When sources disagree on kind, the more specific wins."""
    if a == b:
        return a
    # Anime kinds beat generic TV/Movie kinds (they carry more meaning).
    if a.is_anime and not b.is_anime:
        return a
    if b.is_anime and not a.is_anime:
        return b
    if a is MediaKind.UNKNOWN:
        return b
    if b is MediaKind.UNKNOWN:
        return a
    if a is MediaKind.WEB_SERIES:
        return a
    if b is MediaKind.WEB_SERIES:
        return b
    return a


_KIND_FAMILY = {
    MediaKind.MOVIE: "screen",
    MediaKind.ANIME_MOVIE: "screen",
    MediaKind.SHORT: "screen",
    MediaKind.DOCUMENTARY: "screen",
    MediaKind.TV_SERIES: "serial",
    MediaKind.WEB_SERIES: "serial",
    MediaKind.MINISERIES: "serial",
    MediaKind.ANIME: "serial",
    MediaKind.OVA: "serial",
    MediaKind.ONA: "serial",
    MediaKind.SPECIAL: "serial",
    MediaKind.UNKNOWN: "any",
}


def _mergeable(a: Identity, b: Identity) -> bool:
    """Can two same-titled identities from different databases be one work?

    Kinds must belong to the same family (a film database calling an anime
    a "TV series" is fine; a movie and a series are not the same work), and
    the years must agree or be unknown on one side.
    """
    if a.kind in (MediaKind.FRANCHISE, MediaKind.PERSON) or b.kind in (
        MediaKind.FRANCHISE,
        MediaKind.PERSON,
    ):
        return False
    family_a = _KIND_FAMILY.get(a.kind, "any")
    family_b = _KIND_FAMILY.get(b.kind, "any")
    if "any" not in (family_a, family_b) and family_a != family_b:
        return False
    if a.year and b.year and abs(a.year - b.year) > 1:
        return False
    if a.year_end and b.year_end and abs(a.year_end - b.year_end) > 1:
        return False
    return True


def _unique(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = normalize_title(value)
        if value and key not in seen:
            seen.add(key)
            out.append(value)
    return out


async def _gather(*tasks) -> list[Any]:
    import asyncio

    results = await asyncio.gather(*tasks, return_exceptions=True)
    return [None if isinstance(r, BaseException) else r for r in results]


def strip_query_markers(query: str) -> str:
    """Remove conversational noise from a query's edges."""
    import re

    query = (query or "").strip().strip("\"'“”‘’`")
    query = re.sub(r"\s+", " ", query)
    return query


def parse_query(query: str) -> tuple[str, int | None]:
    """Clean a raw query and pull off a season suffix."""
    cleaned = strip_query_markers(query)
    return split_season_suffix(cleaned)
