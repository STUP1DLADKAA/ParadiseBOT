"""Data models for web search results."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse


@dataclass(slots=True)
class SearchResult:
    """One individual search result item."""

    title: str
    url: str
    snippet: str
    source: str = ""
    authoritative: bool = False

    def __post_init__(self) -> None:
        if not self.source and self.url:
            try:
                parsed = urlparse(self.url)
                netloc = parsed.netloc.lower()
                if netloc.startswith("www."):
                    netloc = netloc[4:]
                self.source = netloc or "web"
            except Exception:
                self.source = "web"


#: Outcome of a retrieval attempt. The caller must be able to tell these
#: apart: "nothing matched" is a useful answer, "the provider is down" is
#: an apology, and both are different from a parsing bug on our side.
OK = "ok"
EMPTY = "empty"
PROVIDER_ERROR = "provider_error"
PARSE_ERROR = "parse_error"


@dataclass(slots=True)
class SearchResponse:
    """Collection of search results for a query."""

    query: str
    results: list[SearchResult] = field(default_factory=list)
    engine: str = "Web"
    cached: bool = False
    status: str = OK

    @property
    def has_results(self) -> bool:
        return bool(self.results)

    @property
    def failed(self) -> bool:
        """True when no provider could be reached (not merely empty)."""
        return self.status in (PROVIDER_ERROR, PARSE_ERROR)

    @property
    def total_count(self) -> int:
        return len(self.results)


@dataclass(slots=True)
class SearchAnswer:
    """A search engine answer: summary first, sources as evidence.

    The whole point of the upgraded search UX: a user gets the answer
    *before* needing to open a link. ``summary`` and ``key_points`` are built
    deterministically from retrieved snippets and never invented.
    """

    query: str
    direct_answer: str = ""        # encyclopedic instant answer, when found
    summary: str = ""              # the short synthesized paragraph
    key_points: list[str] = field(default_factory=list)
    sources: list[SearchResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)  # conflicts / staleness
    answer_source: str = ""        # where the direct answer came from
    # Kept for compatibility with serialized cards from older releases.
    # New answers are always generated locally from retrieved evidence.
    generated_by: str = "local"
    engine: str = "Web"
    fresh: bool = False
    cached: bool = False
    status: str = OK

    @property
    def failed(self) -> bool:
        """True when retrieval itself failed, so there is nothing to show."""
        return self.status in (PROVIDER_ERROR, PARSE_ERROR) and not self.has_sources

    @property
    def has_answer(self) -> bool:
        return bool(self.direct_answer or self.summary or self.key_points)

    @property
    def has_sources(self) -> bool:
        return bool(self.sources)
