"""Paradise's entertainment discovery system.

A key-less, live-web engine for movies, anime, TV and web series. The
layers, each testable (and replaceable) on its own:

``models``       the shapes every layer agrees on (kinds, ratings, dates)
``http``         one careful HTTP client: timeouts, retries, caps, back-off
``cache``        TTL caches with stable/volatile expiry rules + title memory
``textutil``     HTML/wikitext cleaning, fuzzy matching, date helpers
``sources``      the key-less providers behind one protocol each:
                 IMDb suggestions, TVmaze, Kitsu, AniList, Jikan (MAL),
                 Wikipedia/Wikidata, iTunes, and the shared web search
``resolver``     query → who/what the user means (type-aware, typo-tolerant)
``details``      identity → a fully assembled title (parallel fetch + merge)
``verify``       cross-checks dates/runtimes between sources, flags conflicts
``streaming``    where to watch, region-aware, live-verified
``recommend``    "more like this" with a reason for every pick
``trending``     what's hot right now (read habits, anime trending, charts)
``upcoming``     future releases with honest date precision
``autocomplete`` the /example suggestion engine (local + live, ranked)
``nlu``          plain-chat intent parsing ("recommend anime like X")
``format``       data → clean, scannable Discord cards and lists
``service``      the facade the cog actually talks to

Design rules: no API keys anywhere, every source optional with fallbacks,
never invent data — an unavailable fact is shown as unavailable, and
volatile facts (ratings, streaming, status) are refreshed on short TTLs
while stable ones (cast, synopsis, genres) cache for longer.
"""

from __future__ import annotations

from .models import (
    DateValue,
    Identity,
    MediaKind,
    MediaTitle,
    Person,
    Rating,
    RelatedTitle,
    ReleaseStatus,
    SourceNote,
    StreamingOffer,
    TrendingEntry,
    UpcomingEntry,
    kind_emoji,
    kind_label,
)
from .service import EntertainmentService

__all__ = [
    "DateValue",
    "EntertainmentService",
    "Identity",
    "MediaKind",
    "MediaTitle",
    "Person",
    "Rating",
    "RelatedTitle",
    "ReleaseStatus",
    "SourceNote",
    "StreamingOffer",
    "TrendingEntry",
    "UpcomingEntry",
    "kind_emoji",
    "kind_label",
]
