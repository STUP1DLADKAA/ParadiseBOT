"""The lookup pipeline.

One entry point — :meth:`DictionaryService.lookup` — with a deliberate
order of attempts:

1. normalise the query and reject anything that is not a word;
2. answer from cache if we already know it;
3. coalesce with any identical lookup already in flight;
4. ask the primary provider, and in parallel ask Datamuse for synonyms
   (they need the same term, so waiting for one to ask the other would
   just be slower);
5. on "no such word", retry the plausible base forms (``running`` → ``run``)
   and say so in the result;
6. if the primary provider is *down* rather than empty, fall back to
   Datamuse definitions and mark the answer as partial;
7. if there is still nothing, rank close lexical spellings with
   Damerau-Levenshtein: verify a high-confidence correction, otherwise ask.

Failures are contained: a dead provider degrades the answer, it never
raises into the cog, and nothing unverified is ever invented to fill a gap.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from functions.helpers import ParadiseError

from .cache import SingleFlight, TTLCache
from .http import HttpClient, HttpError, NotFoundError, ThrottledError
from .models import LookupResult, WordEntry, WordForm
from .normalize import (
    MAX_TERM_LENGTH,
    FuzzyMatch,
    base_forms,
    cache_key,
    candidate_forms,
    is_lookupable,
    normalize_query,
    fuzzy_confidence,
    rank_fuzzy_matches,
    should_auto_correct,
)
from .sources import DatamuseProvider, Enrichment, FreeDictionaryProvider

log = logging.getLogger(__name__)

ENTRY_TTL = 6 * 3600.0
MISS_TTL = 600.0          # a miss might be a provider hiccup; re-check sooner
PARTIAL_TTL = 300.0       # thin answers should heal quickly
DETAIL_TTL = 6 * 3600.0
MAX_BASE_FORM_TRIES = 3
MAX_SUGGESTIONS = 4

EMPTY_QUERY_MESSAGE = "give me a word to look up, like `/dictionary serendipity`."
BAD_QUERY_MESSAGE = (
    "that doesn't look like a word I can look up. letters only — "
    "quotes are fine for a phrase."
)
LONG_QUERY_MESSAGE = f"that's a bit long. keep it under {MAX_TERM_LENGTH} characters."


@dataclass(frozen=True, slots=True)
class WordDetails:
    """The extra material behind the "More" button."""

    related: tuple[str, ...] = ()
    forms: tuple[WordForm, ...] = ()


class DictionaryService:
    """Data retrieval and normalisation. Knows nothing about Discord."""

    def __init__(
        self,
        *,
        client: HttpClient | None = None,
        primary=None,
        secondary=None,
        timeout: float = 8.0,
        cache_size: int = 512,
        cache_ttl: float = ENTRY_TTL,
    ) -> None:
        self._owns_client = client is None
        self.client = client or HttpClient(timeout=timeout)
        self.primary = primary if primary is not None else FreeDictionaryProvider(self.client)
        self.secondary = secondary if secondary is not None else DatamuseProvider(self.client)
        self.timeout = float(timeout)
        self._cache: TTLCache[str, LookupResult] = TTLCache(
            max_items=cache_size, ttl=cache_ttl
        )
        self._details: TTLCache[str, WordDetails] = TTLCache(max_items=256, ttl=DETAIL_TTL)
        self._flight: SingleFlight = SingleFlight()
        self._detail_flight: SingleFlight = SingleFlight()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def cache_stats(self) -> str:
        stats = self._cache.stats
        return (
            f"entries={len(self._cache)} hits={stats.hits} misses={stats.misses} "
            f"evictions={stats.evictions} inflight={self._flight.in_flight}"
        )

    def validate(self, raw: str) -> str:
        """Normalise and sanity-check a query, or raise a friendly error."""
        if raw is None or not str(raw).strip():
            raise ParadiseError(EMPTY_QUERY_MESSAGE)
        if len(str(raw).strip()) > MAX_TERM_LENGTH * 2:
            raise ParadiseError(LONG_QUERY_MESSAGE)
        term = normalize_query(raw)
        if not term:
            raise ParadiseError(EMPTY_QUERY_MESSAGE)
        if not is_lookupable(term):
            raise ParadiseError(BAD_QUERY_MESSAGE)
        return term

    async def lookup(
        self, raw: str, *, language: str = "en", fuzzy: bool = True
    ) -> LookupResult:
        """Look a word up, optionally stopping before spelling correction.

        ``fuzzy=False`` powers the explicit "Search exactly what I typed"
        action.  It has a distinct cache key so a user's deliberate exact
        search can never be answered with an earlier automatic correction.
        """
        term = self.validate(raw)
        base_key = cache_key(term, language)
        key = base_key + ("" if fuzzy else "#exact")

        cached = self._cache.get(key)
        if cached is not None:
            return cached
        if not fuzzy:
            # A canonical direct hit may already have been fetched for another
            # user. Reuse it before making a second provider request, while
            # never reusing an auto-corrected result for "exactly what I
            # typed" (that would defeat the button's purpose).
            canonical = self._cache.get(base_key)
            if (
                canonical is not None
                and canonical.found
                and canonical.entry is not None
                and not canonical.auto_corrected
                and canonical.entry.word.casefold() == term.casefold()
            ):
                return canonical

        async def run() -> LookupResult:
            result = await self._lookup_uncached(term, language, fuzzy=fuzzy)
            ttl = ENTRY_TTL if result.found else MISS_TTL
            if result.found and result.partial:
                ttl = PARTIAL_TTL
            self._cache.put(key, result, ttl=ttl)
            return result

        return await self._flight.run(key, run)

    async def details(self, entry: WordEntry, *, language: str = "en") -> WordDetails:
        """Related words and confirmed inflections. Best effort, cached."""
        key = cache_key(entry.word, language) + "#details"
        cached = self._details.get(key)
        if cached is not None:
            return cached

        async def run() -> WordDetails:
            details = await self._fetch_details(entry)
            self._details.put(key, details)
            return details

        try:
            return await self._detail_flight.run(key, run)
        except Exception:  # noqa: BLE001 - extras are always optional
            log.debug("Detail fetch failed for %r", entry.word, exc_info=True)
            return WordDetails()

    async def close(self) -> None:
        self._cache.clear()
        self._details.clear()
        if self._owns_client:
            await self.client.close()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _lookup_uncached(
        self, term: str, language: str, *, fuzzy: bool = True
    ) -> LookupResult:
        # This first pass is always exact (after input normalisation).  Fuzzy
        # work is deliberately held until it has genuinely missed.
        entry, enrichment, primary_failed = await self._primary_with_enrichment(term, language)
        resolved_from = ""
        partial = False
        providers: list[str] = []

        # 1. The word as typed might be an inflection of the head-word.
        if entry is None and not primary_failed and " " not in term:
            entry, resolved_from = await self._try_base_forms(term, language)

        # 2. The primary provider being *down* is different from "no entry".
        if entry is None and primary_failed:
            entry = await self._safe(self.secondary.lookup(term, language))
            if entry is not None:
                partial = True
                providers.append(getattr(self.secondary, "name", "secondary"))
        elif entry is not None:
            providers.append(getattr(self.primary, "name", "primary"))

        if entry is None:
            if fuzzy:
                return await self._fuzzy_result(
                    term,
                    language,
                    partial=primary_failed,
                    providers=tuple(providers),
                )
            # A user can explicitly decline the correction. Do not make a
            # second spelling request or offer the same guess again.
            return LookupResult(
                query=term,
                entry=None,
                partial=primary_failed,
                providers=tuple(providers),
            )

        if enrichment is None:
            enrichment = await self._safe(self._enrich(entry.word))
        if enrichment is not None and not enrichment.is_empty:
            entry = entry.with_extras(
                synonyms=enrichment.synonyms,
                antonyms=enrichment.antonyms,
                related=enrichment.related,
            )
            if enrichment.provider and enrichment.provider not in providers:
                providers.append(enrichment.provider)

        return LookupResult(
            query=term,
            entry=entry,
            resolved_from=resolved_from,
            partial=partial,
            providers=tuple(providers),
        )

    async def _primary_with_enrichment(
        self, term: str, language: str
    ) -> tuple[WordEntry | None, Enrichment | None, bool]:
        """Ask the primary provider and Datamuse at the same time."""
        primary_task = asyncio.ensure_future(
            self._call(self.primary.lookup(term, language))
        )
        enrich_task = asyncio.ensure_future(self._call(self._enrich(term)))
        primary, enrichment = await asyncio.gather(
            primary_task, enrich_task, return_exceptions=True
        )

        failed = False
        entry: WordEntry | None = None
        if isinstance(primary, BaseException):
            if not isinstance(primary, NotFoundError):
                failed = True
                log.info("Dictionary primary provider unavailable: %s", primary)
        else:
            entry = primary

        extras = None if isinstance(enrichment, BaseException) else enrichment
        return entry, extras, failed

    async def _enrich(self, term: str) -> Enrichment:
        enrich = getattr(self.secondary, "enrich", None)
        if enrich is None:
            return Enrichment()
        return await enrich(term)

    async def _try_base_forms(self, term: str, language: str) -> tuple[WordEntry | None, str]:
        for candidate in base_forms(term)[:MAX_BASE_FORM_TRIES]:
            entry = await self._safe(self._call(self.primary.lookup(candidate, language)))
            if entry is not None:
                return entry, term
        return None, ""

    async def _fuzzy_result(
        self,
        term: str,
        language: str,
        *,
        partial: bool,
        providers: tuple[str, ...],
    ) -> LookupResult:
        """Turn close spellings into either a verified result or a question.

        Datamuse supplies a broad, real-word candidate pool.  The top match is
        only used automatically after it clears our conservative confidence
        gate *and* a normal exact lookup verifies that a dictionary entry is
        available.  That makes a correction useful without silently inventing
        a meaning for an ambiguous query.
        """
        matches = await self._fuzzy_matches(term)
        if not matches:
            return LookupResult(query=term, partial=partial, providers=providers)

        top = matches[0]
        runner_up = matches[1] if len(matches) > 1 else None
        confidence = fuzzy_confidence(top, runner_up)
        suggestions = tuple(match.word for match in matches[:MAX_SUGGESTIONS])

        if should_auto_correct(top, runner_up):
            # This reuses the same provider/cache pipeline as a normal search.
            # The strict mode prevents a bad candidate from cascading into a
            # second correction if the primary source does not recognise it.
            corrected = await self.lookup(top.word, language=language, fuzzy=False)
            if corrected.found and corrected.entry is not None:
                # Promote the verified canonical result as well. A later
                # direct lookup of the corrected word is now a pure cache hit.
                self._cache.put(
                    cache_key(top.word, language),
                    corrected,
                    ttl=PARTIAL_TTL if corrected.partial else ENTRY_TTL,
                )
                return LookupResult(
                    query=term,
                    entry=corrected.entry,
                    suggestions=suggestions,
                    suggested_word=top.word,
                    suggestion_confidence=confidence,
                    auto_corrected=True,
                    resolved_from=term,
                    partial=partial or corrected.partial,
                    providers=corrected.providers or providers,
                )

        # A medium confidence candidate earns a focused confirmation prompt;
        # lower-confidence candidates remain ordinary alternatives, not a
        # misleading "Did you mean" claim.
        suggested_word = top.word if confidence >= 0.52 else ""
        return LookupResult(
            query=term,
            suggestions=suggestions,
            suggested_word=suggested_word,
            suggestion_confidence=confidence,
            partial=partial,
            providers=providers,
        )

    async def _fuzzy_matches(self, term: str) -> tuple[FuzzyMatch, ...]:
        suggest = getattr(self.secondary, "suggest", None)
        if suggest is None:
            return ()
        candidates = await self._safe(self._call(suggest(term)))
        if not candidates:
            return ()
        return rank_fuzzy_matches(term, candidates, limit=MAX_SUGGESTIONS)

    async def _fetch_details(self, entry: WordEntry) -> WordDetails:
        wanted = candidate_forms(entry.word, entry.parts_of_speech)
        verify = getattr(self.secondary, "verify_forms", None)
        enrich = getattr(self.secondary, "enrich", None)

        jobs: list = []
        jobs.append(
            self._call(verify(entry.word, wanted)) if verify is not None else _none()
        )
        jobs.append(
            self._call(enrich(entry.word, include_related=True))
            if enrich is not None and not entry.related
            else _none()
        )
        forms_result, enrich_result = await asyncio.gather(*jobs, return_exceptions=True)

        forms: tuple[WordForm, ...] = ()
        if isinstance(forms_result, tuple):
            forms = forms_result
        related: tuple[str, ...] = entry.related
        if isinstance(enrich_result, Enrichment):
            related = related or enrich_result.related
        return WordDetails(related=related, forms=forms)

    # -- plumbing ----------------------------------------------------------

    async def _call(self, awaitable):
        """Every provider call gets its own hard deadline."""
        return await asyncio.wait_for(awaitable, timeout=self.timeout)

    @staticmethod
    async def _safe(awaitable):
        """Run something optional; swallow anything that is not fatal."""
        try:
            return await awaitable
        except (HttpError, NotFoundError, ThrottledError, asyncio.TimeoutError) as error:
            log.debug("Optional dictionary call failed: %s", error)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never let extras break a lookup
            log.warning("Unexpected dictionary provider error", exc_info=True)
        return None


async def _none():
    return None
