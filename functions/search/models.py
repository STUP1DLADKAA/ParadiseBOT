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


@dataclass(slots=True)
class SearchResponse:
    """Collection of search results for a query."""

    query: str
    results: list[SearchResult] = field(default_factory=list)
    engine: str = "Web"
    cached: bool = False

    @property
    def has_results(self) -> bool:
        return bool(self.results)

    @property
    def total_count(self) -> int:
        return len(self.results)
