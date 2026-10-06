"""The answer layer on top of raw web search.

Raw search gives links; people want the answer. This module implements
the pipeline:

    1. understand the query (topic, freshness, question form)
    2. retrieve from multiple key-less sources in parallel
       (ranked web results, DuckDuckGo instant answers, Wikipedia)
    3. extract the useful bits (facts, numbers, dates, context)
    4. synthesize a short honest summary from what was retrieved
    5. attach sources as evidence, flag disagreements between sources

This is a native search feature. Its summaries are deterministic and are
built only from retrieved evidence; it never calls an AI provider. Free-form
AI responses are handled by the assistant separately, so a search request
cannot accidentally become a search-then-AI request.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from urllib.parse import quote

import aiohttp

from functions.helpers import shorten
from .engine import SearchEngine, USER_AGENT
from .models import EMPTY, OK, PROVIDER_ERROR, SearchAnswer, SearchResult

log = logging.getLogger(__name__)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(“\"'])")
_YEAR_CLAIM = re.compile(
    r"(?:released?|launch(?:ed|es)?|announced|expected|scheduled|coming|out|arrives?|"
    r"available|set for|planned for|due)\D{0,25}?\b(20\d{2})\b",
    re.IGNORECASE,
)
_BARE_YEAR = re.compile(r"\b(20\d{2})\b")
_MONEY_CLAIM = re.compile(
    r"(?P<cur>[$₹€£]|USD|INR|EUR|GBP)\s?(?P<amt>\d[\d,]*(?:\.\d+)?(?:\s?(?:billion|million|thousand|crore|lakh))?)",
    re.IGNORECASE,
)
_NOISE_PHRASES = re.compile(
    r"(?:click here|read more|learn more|subscribe|sign up|advertisement|"
    r"all rights reserved|terms of service|cookie)",
    re.IGNORECASE,
)
_CURRENT_YEAR = time.gmtime().tm_year

_DDGS = "https://api.duckduckgo.com/"
_WIKI_SEARCH = "https://en.wikipedia.org/w/api.php"
_WIKI_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/"

_CACHE_TTL = 900
_CACHE_TTL_FRESH = 240


class AnswerEngine:
    """Turns a query into a :class:`SearchAnswer` with sources."""

    def __init__(
        self,
        engine: SearchEngine | None = None,
        ai=None,
        *,
        cache_ttl: int = _CACHE_TTL,
        synthesize: bool | None = None,
    ) -> None:
        self.engine = engine or SearchEngine()
        self.cache_ttl = cache_ttl
        # Production callers do not pass an AI service. Keep an explicit,
        # source-compatible escape hatch for integrations from older releases,
        # but never wire it from the bot itself.
        self._legacy_ai = ai if ai is not None and synthesize is not False else None
        self._cache: dict[str, tuple[float, SearchAnswer]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def answer(
        self, query: str, *, fresh: bool | None = None, reason_complex: bool = False
    ) -> SearchAnswer:
        """Answer *query* with a summary plus sourced evidence. Never raises."""
        query = " ".join(str(query).split()).strip()
        if not query:
            return SearchAnswer(query=query)

        ttl = self.cache_ttl
        if fresh is None:
            fresh = self._detect_freshness(query)
        if fresh:
            ttl = min(ttl, _CACHE_TTL_FRESH)

        cache_key = query.casefold()
        now = time.monotonic()
        cached = self._cache.get(cache_key)
        if cached and now - cached[0] < ttl:
            answer = cached[1]
            return SearchAnswer(
                query=answer.query,
                direct_answer=answer.direct_answer,
                summary=answer.summary,
                key_points=list(answer.key_points),
                sources=list(answer.sources),
                warnings=list(answer.warnings),
                answer_source=answer.answer_source,
                generated_by=answer.generated_by,
                engine=answer.engine,
                fresh=answer.fresh,
                cached=True,
            )

        try:
            built = await self._build_answer(query, fresh=fresh, reason_complex=reason_complex)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a search must never explode in chat
            log.exception("Answer pipeline failed for %r", query)
            built = SearchAnswer(query=query, fresh=fresh)

        if built.has_answer or built.has_sources:
            self._cache[cache_key] = (now, built)
            if len(self._cache) > 256:
                oldest = min(self._cache, key=lambda k: self._cache[k][0])
                self._cache.pop(oldest, None)
        return built

    # ------------------------------------------------------------------
    # Step 1+2: understand + retrieve (parallel)
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_freshness(query: str) -> bool:
        return bool(
            re.search(
                r"\b(?:latest|news|today|recent(?:ly)?|updated?|now|current(?:ly)?|"
                r"this (?:week|month|year|season)|breaking|upcoming|announc|"
                r"release|patch|price|score|match|won|election)\b",
                query,
                re.IGNORECASE,
            )
        )

    async def _build_answer(self, query: str, *, fresh: bool, reason_complex: bool) -> SearchAnswer:
        search_task = asyncio.create_task(self.engine.search(query, limit=8))
        instant_task = asyncio.create_task(self._instant_answer(query))
        wiki_task = asyncio.create_task(self._wikipedia_summary(query))

        response, instant, wiki = await asyncio.gather(
            search_task, instant_task, wiki_task, return_exceptions=True
        )
        if isinstance(response, BaseException):
            log.debug("Search leg failed: %s", response)
            response = None
        if isinstance(instant, BaseException):
            log.debug("Instant-answer leg failed: %s", instant)
            instant = None
        if isinstance(wiki, BaseException):
            log.debug("Wikipedia leg failed: %s", wiki)
            wiki = None

        results = list(response.results) if response is not None else []
        answer = SearchAnswer(
            query=query,
            engine=(response.engine if response is not None else "Web"),
            fresh=fresh,
            status=(response.status if response is not None else PROVIDER_ERROR),
        )

        # Merge wiki into sources when it isn't already there. For fresh /
        # news queries it joins the back of the evidence list instead of
        # leading it — encyclopedias aren't the freshest voices.
        if wiki is not None and wiki.get("url"):
            wiki_result = SearchResult(
                title=wiki.get("title") or query,
                url=wiki["url"],
                snippet=shorten(wiki.get("extract", ""), 240),
                source="wikipedia.org",
                authoritative=True,
            )
            normalised = wiki_result.url.rstrip("/")
            if not any(r.url.rstrip("/") == normalised for r in results):
                if fresh:
                    results.append(wiki_result)
                else:
                    results.insert(0, wiki_result)

        # ------------------------------------------------------------------
        # Step 3: extract the answer skeleton from what we retrieved
        # ------------------------------------------------------------------
        snippet_pool = [r for r in results if r.snippet]

        if fresh:
            # News-style answers lead with the freshest-looking snippets.
            candidate_sentences = self._rank_sentences(
                self._collect_sentences(snippet_pool), query, limit=3
            )
            if candidate_sentences:
                answer.direct_answer = candidate_sentences[0]
                answer.answer_source = self._source_for(candidate_sentences[0], snippet_pool)
        elif wiki is not None and wiki.get("extract"):
            extract = self._first_sentences(wiki["extract"], count=2, limit=420)
            if extract:
                answer.direct_answer = extract
                answer.answer_source = "Wikipedia"
        if not answer.direct_answer and instant is not None:
            abstract = self._first_sentences(instant.get("abstract", ""), count=2, limit=420)
            if abstract:
                answer.direct_answer = abstract
                answer.answer_source = instant.get("source") or "DuckDuckGo"
                if instant.get("url"):
                    instant_result = SearchResult(
                        title=instant.get("heading") or query,
                        url=instant["url"],
                        snippet=shorten(instant.get("abstract", ""), 240),
                        source=instant.get("source") or "duckduckgo.com",
                        authoritative=True,
                    )
                    if not any(r.url.rstrip("/") == instant_result.url.rstrip("/") for r in results):
                        results.insert(0, instant_result)
        if not answer.direct_answer and snippet_pool:
            # Deterministic fallback: the single best snippet sentence pair.
            sentences = self._rank_sentences(
                self._collect_sentences(snippet_pool[:4]), query, limit=2
            )
            if sentences:
                answer.direct_answer = " ".join(sentences)
                answer.answer_source = self._source_for(sentences[0], snippet_pool)

        answer.summary = self._synthesize_summary(query, results, instant, wiki)
        answer.key_points = self._key_points(query, results, wiki, instant)
        if answer.direct_answer and answer.key_points:
            # The quick answer already said it — don't repeat it as a point.
            anchor = answer.direct_answer.casefold()
            answer.key_points = [
                point
                for point in answer.key_points
                if point.rstrip(".").casefold()[:60] not in anchor
            ][:4]
        answer.warnings = self._detect_warnings(query, results, fresh=fresh)
        answer.sources = results[:6]
        # Final status: something usable, nothing matched, or retrieval down.
        if answer.has_answer or answer.has_sources:
            answer.status = OK
        elif answer.status == OK:
            answer.status = EMPTY

        # The bot never enables this compatibility path. It exists only so
        # external callers upgrading from the old API can opt in explicitly;
        # normal native search remains deterministic and single-pass.
        if self._legacy_ai is not None and self._should_legacy_synthesize(
            query, answer, reason_complex
        ):
            polished = await self._legacy_ai_summary(query, answer)
            if polished:
                answer.summary = polished
                answer.generated_by = "gemini"
        return answer

    # ------------------------------------------------------------------
    # Retrieval helpers (key-less)
    # ------------------------------------------------------------------

    async def _instant_answer(self, query: str) -> dict | None:
        params = {"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"}
        try:
            # Reuse the search engine's keep-alive session: one connector
            # for every outbound retrieval leg.
            session = await self.engine.session()
            async with session.get(
                _DDGS, params=params, headers={"User-Agent": USER_AGENT}
            ) as response:
                if response.status != 200:
                    return None
                data = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            log.debug("DDG instant answer failed", exc_info=True)
            return None

        abstract = (data.get("AbstractText") or "").strip()
        answer_text = (data.get("Answer") or "").strip()
        if not abstract and not answer_text:
            return None
        return {
            "abstract": abstract or answer_text,
            "url": data.get("AbstractURL") or "",
            "source": data.get("AbstractSource") or "",
            "heading": data.get("Heading") or "",
        }

    async def _wikipedia_summary(self, query: str) -> dict | None:
        headers = {"User-Agent": USER_AGENT}
        try:
            session = await self.engine.session()
            async with session.get(
                _WIKI_SEARCH,
                params={
                    "action": "opensearch", "search": query, "limit": "1",
                    "namespace": "0", "format": "json",
                },
                headers=headers,
            ) as response:
                if response.status != 200:
                    return None
                data = await response.json(content_type=None)
            if not (isinstance(data, list) and len(data) >= 4 and data[1]):
                return None
            title = data[1][0]
            async with session.get(
                _WIKI_SUMMARY + quote(title.replace(" ", "_")),
                headers=headers,
            ) as response:
                if response.status != 200:
                    return None
                payload = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            log.debug("Wikipedia summary failed", exc_info=True)
            return None
        extract = (payload.get("extract") or "").strip()
        if not extract:
            return None
        return {
            "title": payload.get("title") or title,
            "extract": extract,
            "url": (payload.get("content_urls", {}).get("desktop", {}) or {}).get("page", ""),
        }

    # ------------------------------------------------------------------
    # Step 3 internals: extraction, ranking, warnings
    # ------------------------------------------------------------------

    @staticmethod
    def _collect_sentences(results: list[SearchResult]) -> list[tuple[str, SearchResult]]:
        pool: list[tuple[str, SearchResult]] = []
        for result in results:
            for sentence in _SENTENCE_SPLIT.split(result.snippet):
                sentence = sentence.strip()
                if 24 <= len(sentence) <= 400 and not _NOISE_PHRASES.search(sentence):
                    pool.append((sentence, result))
        return pool

    def _rank_sentences(
        self, pool: list[tuple[str, SearchResult]], query: str, *, limit: int
    ) -> list[str]:
        terms = {t.casefold() for t in re.findall(r"[a-z0-9]{3,}", query)}
        scored: list[tuple[float, str]] = []
        for sentence, result in pool:
            words = {w.casefold() for w in re.findall(r"[a-z0-9]{3,}", sentence)}
            overlap = len(terms & words)
            score = overlap * 2.0
            if result.authoritative:
                score += 1.5
            if _BARE_YEAR.search(sentence):
                score += 0.8
            if re.search(r"\d", sentence):
                score += 0.4
            score -= len(sentence) / 600.0  # shorter is punchier
            scored.append((score, sentence))
        scored.sort(key=lambda item: item[0], reverse=True)

        picked: list[str] = []
        seen_words: list[set[str]] = []
        for _, sentence in scored:
            words = {w.casefold() for w in re.findall(r"[a-z0-9]{3,}", sentence)}
            if any(len(words & prev) / max(1, len(words | prev)) > 0.55 for prev in seen_words):
                continue  # near-duplicate of an already-picked sentence
            picked.append(sentence)
            seen_words.append(words)
            if len(picked) >= limit:
                break
        return picked

    @staticmethod
    def _first_sentences(text: str, *, count: int, limit: int) -> str:
        sentences = [s.strip() for s in _SENTENCE_SPLIT.split(text.strip()) if s.strip()]
        out: list[str] = []
        total = 0
        for sentence in sentences:
            if len(out) >= count or total + len(sentence) > limit:
                break
            out.append(sentence)
            total += len(sentence) + 1
        return " ".join(out)[:limit]

    @staticmethod
    def _source_for(sentence: str, pool: list[SearchResult]) -> str:
        needle = sentence[:60]
        for result in pool:
            if needle in result.snippet:
                return result.source
        return pool[0].source if pool else ""

    def _synthesize_summary(
        self,
        query: str,
        results: list[SearchResult],
        instant: dict | None,
        wiki: dict | None,
    ) -> str:
        """A short paragraph built only from retrieved evidence."""
        facts: list[str] = []
        if wiki is not None and wiki.get("extract"):
            rest = self._first_sentences(wiki["extract"], count=4, limit=600)
            if rest:
                facts.append(rest)
        if instant is not None and instant.get("abstract"):
            facts.append(self._first_sentences(instant["abstract"], count=3, limit=500))

        pool = self._collect_sentences([r for r in results if r.snippet])
        facts.extend(self._rank_sentences(pool, query, limit=3))

        parts: list[str] = []
        seen: set[str] = set()
        for fact in facts:
            key = fact[:80].casefold()
            if key in seen:
                continue
            seen.add(key)
            parts.append(fact)
            if sum(len(p) for p in parts) > 480:
                break
        return " ".join(parts)[:600].strip()

    def _key_points(
        self,
        query: str,
        results: list[SearchResult],
        wiki: dict | None,
        instant: dict | None,
    ) -> list[str]:
        pool: list[tuple[str, SearchResult]] = []
        if wiki is not None:
            pseudo = SearchResult(title="wiki", url="", snippet=wiki.get("extract", ""), authoritative=True)
            pool.extend(self._collect_sentences([pseudo]))
        if instant is not None:
            pseudo = SearchResult(title="ddg", url="", snippet=instant.get("abstract", ""), authoritative=True)
            pool.extend(self._collect_sentences([pseudo]))
        pool.extend(self._collect_sentences([r for r in results if r.snippet]))
        ranked = self._rank_sentences(pool, query, limit=5)
        return [shorten(sentence, 140) for sentence in ranked[:4]]

    def _detect_warnings(self, query: str, results: list[SearchResult], *, fresh: bool) -> list[str]:
        warnings: list[str] = []
        snippets = [r.snippet for r in results if r.snippet]
        if len(snippets) < 2:
            return warnings

        # Conflicting timeline claims ("coming 2025" vs "released in 2026").
        years = {match.group(1) for snippet in snippets for match in _YEAR_CLAIM.finditer(snippet)}
        if len(years) >= 2:
            ordered = sorted(years)
            warnings.append(
                f"Sources currently disagree on the timing — some say {ordered[0]}, "
                f"others say {ordered[-1]}. Check the sources below."
            )

        # Conflicting money claims with the same currency marker.
        money = {
            (m.group("cur").casefold(), m.group("amt").replace(",", "").casefold())
            for snippet in snippets
            for m in _MONEY_CLAIM.finditer(snippet)
            if len(m.group("amt")) >= 3
        }
        currencies = {entry[0] for entry in money}
        for currency in currencies:
            amounts = {amt for cur, amt in money if cur == currency}
            if len(amounts) >= 3:
                warnings.append(
                    "Sources quote different figures for this — treat the numbers "
                    "below as approximate and check the sources."
                )
                break

        if fresh and not warnings:
            years_seen = {int(y) for snippet in snippets for y in _BARE_YEAR.findall(snippet)}
            if years_seen and max(years_seen) <= _CURRENT_YEAR - 2:
                warnings.append(
                    "The freshest sources I could verify aren't brand-new — "
                    "double-check the latest links below."
                )
        return warnings[:2]


    def _should_legacy_synthesize(
        self, query: str, answer: SearchAnswer, reason_complex: bool
    ) -> bool:
        """Compatibility-only AI polish for callers that explicitly opt in."""
        if not getattr(self._legacy_ai, "available", False):
            return False
        if reason_complex:
            return answer.has_answer
        if re.match(
            r"^(?:who|what|when|where|which|why|how|is|are|does|do|did|can|will|should)\b",
            query,
            re.IGNORECASE,
        ) and len(answer.sources) >= 2:
            return True
        return answer.fresh and len(answer.sources) >= 3 and len(answer.key_points) >= 2

    async def _legacy_ai_summary(self, query: str, answer: SearchAnswer) -> str:
        facts: list[str] = []
        if answer.direct_answer:
            facts.append(answer.direct_answer)
        facts.extend(answer.key_points[:4])
        for result in answer.sources[:3]:
            if result.snippet:
                facts.append(f"({result.source}) {result.snippet}")
        try:
            polished = await asyncio.wait_for(
                self._legacy_ai.reason(query, facts, max_tokens=260), timeout=18
            )
        except Exception:  # noqa: BLE001 - compatibility path is optional
            log.debug("Legacy AI synthesis skipped", exc_info=True)
            return ""
        polished = " ".join(str(polished).split())
        if len(polished) < 40 or polished.casefold().startswith(
            ("i don't", "sorry", "the facts", "verified facts")
        ):
            return ""
        return polished[:600]


__all__ = ["AnswerEngine"]
