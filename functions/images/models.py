"""Data models for web image search results.

Deliberately tiny: Paradise never stores image *bytes*, only short
strings describing where an image lives on the web. A whole cached
response is a few hundred bytes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse


@dataclass(slots=True)
class ImageResult:
    """One image found on the web.

    ``image_url`` is the direct, remotely hosted file that Discord embeds.
    ``source_url`` is the page it came from, shown as attribution when the
    provider supplies one. Nothing is ever downloaded to produce these.
    """

    title: str
    image_url: str
    source_url: str = ""
    source: str = ""
    provider: str = ""
    width: int = 0
    height: int = 0
    mime: str = ""
    attribution: str = ""

    def __post_init__(self) -> None:
        if not self.source:
            self.source = _domain_of(self.source_url or self.image_url)

    @property
    def has_source_page(self) -> bool:
        return bool(self.source_url)


@dataclass(slots=True)
class ImageResponse:
    """The (small) set of images found for one query."""

    query: str
    results: list[ImageResult] = field(default_factory=list)
    provider: str = ""
    cached: bool = False

    @property
    def has_results(self) -> bool:
        return bool(self.results)

    @property
    def best(self) -> ImageResult | None:
        return self.results[0] if self.results else None


def _domain_of(url: str) -> str:
    try:
        netloc = urlparse(str(url)).netloc.lower()
    except (ValueError, AttributeError):
        return "web"
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc or "web"


__all__ = ["ImageResponse", "ImageResult"]
