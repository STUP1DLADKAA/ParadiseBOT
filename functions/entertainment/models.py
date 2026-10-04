"""The immutable shapes every entertainment layer agrees on.

Nothing here touches Discord, HTTP or the cache — just data, plus the
small pure helpers (labels, emoji, display text) built on top of it.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date, datetime, timezone


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class MediaKind(enum.Enum):
    """What a query resolved to. Ordered vaguely by how specific it is."""

    MOVIE = "movie"
    TV_SERIES = "tv_series"
    WEB_SERIES = "web_series"
    MINISERIES = "miniseries"
    ANIME = "anime"
    ANIME_MOVIE = "anime_movie"
    OVA = "ova"
    ONA = "ona"
    SPECIAL = "special"
    DOCUMENTARY = "documentary"
    SHORT = "short"
    FRANCHISE = "franchise"
    PERSON = "person"
    UNKNOWN = "unknown"

    @property
    def is_series(self) -> bool:
        return self in _SERIES_KINDS

    @property
    def is_anime(self) -> bool:
        return self in _ANIME_KINDS


_SERIES_KINDS = frozenset(
    {
        MediaKind.TV_SERIES,
        MediaKind.WEB_SERIES,
        MediaKind.MINISERIES,
        MediaKind.ANIME,
        MediaKind.DOCUMENTARY,
    }
)
_ANIME_KINDS = frozenset(
    {
        MediaKind.ANIME,
        MediaKind.ANIME_MOVIE,
        MediaKind.OVA,
        MediaKind.ONA,
        MediaKind.SPECIAL,
    }
)


class ReleaseStatus(enum.Enum):
    """Where a title is in its life cycle."""

    UPCOMING = "upcoming"
    AIRING = "airing"
    RELEASED = "released"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    RENEWED = "renewed"
    HIATUS = "hiatus"
    UNKNOWN = "unknown"


_KIND_LABELS: dict[MediaKind, str] = {
    MediaKind.MOVIE: "Movie",
    MediaKind.TV_SERIES: "TV Series",
    MediaKind.WEB_SERIES: "Web Series",
    MediaKind.MINISERIES: "Miniseries",
    MediaKind.ANIME: "Anime",
    MediaKind.ANIME_MOVIE: "Anime Movie",
    MediaKind.OVA: "OVA",
    MediaKind.ONA: "ONA",
    MediaKind.SPECIAL: "Special",
    MediaKind.DOCUMENTARY: "Documentary",
    MediaKind.SHORT: "Short",
    MediaKind.FRANCHISE: "Franchise",
    MediaKind.PERSON: "Person",
    MediaKind.UNKNOWN: "Title",
}

# The emoji vocabulary used across the cards (kept in one place so the
# formatter and tests agree on it).
_KIND_EMOJI: dict[MediaKind, str] = {
    MediaKind.MOVIE: "🎬",
    MediaKind.TV_SERIES: "📺",
    MediaKind.WEB_SERIES: "📺",
    MediaKind.MINISERIES: "📺",
    MediaKind.ANIME: "🌸",
    MediaKind.ANIME_MOVIE: "🌸",
    MediaKind.OVA: "🌸",
    MediaKind.ONA: "🌸",
    MediaKind.SPECIAL: "🌸",
    MediaKind.DOCUMENTARY: "🎥",
    MediaKind.SHORT: "🎬",
    MediaKind.FRANCHISE: "🗂️",
    MediaKind.PERSON: "🧑‍🎨",
    MediaKind.UNKNOWN: "🎬",
}


def kind_label(kind: MediaKind) -> str:
    return _KIND_LABELS.get(kind, "Title")


def kind_emoji(kind: MediaKind) -> str:
    return _KIND_EMOJI.get(kind, "🎬")


# ---------------------------------------------------------------------------
# Dates — with honesty about precision
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DateValue:
    """A release date that knows how precise it is.

    Wikidata (and announcements) sometimes only promise a year or a
    month. We never upgrade a fuzzy date into a fake exact one: ``human``
    renders exactly what is known.
    """

    # Wikidata time precision convention: 9 = year, 10 = month, 11 = day.
    YEAR = 9
    MONTH = 10
    DAY = 11

    value: date | None = None
    precision: int = DAY
    source: str = ""
    note: str = ""  # e.g. "US theatrical" / "reported, not official"

    @property
    def known(self) -> bool:
        return self.value is not None

    def human(self) -> str:
        if self.value is None:
            return "unannounced"
        if self.precision <= self.YEAR:
            return str(self.value.year)
        if self.precision == self.MONTH:
            return self.value.strftime("%B %Y")
        return self.value.strftime("%-d %B %Y") if hasattr(self.value, "strftime") else str(self.value)

    def iso(self) -> str | None:
        return self.value.isoformat() if self.value else None

    @staticmethod
    def from_iso(raw: str | None, source: str = "", precision: int = DAY) -> "DateValue":
        if not raw:
            return DateValue(source=source)
        try:
            parsed = datetime.fromisoformat(str(raw)[:10]).date()
        except ValueError:
            return DateValue(source=source)
        return DateValue(value=parsed, precision=precision, source=source)

    @staticmethod
    def from_parts(
        year: int | None,
        month: int | None = None,
        day: int | None = None,
        source: str = "",
    ) -> "DateValue":
        if not year:
            return DateValue(source=source)
        if month is None:
            return DateValue(value=date(int(year), 1, 1), precision=DateValue.YEAR, source=source)
        if day is None:
            return DateValue(value=date(int(year), int(month), 1), precision=DateValue.MONTH, source=source)
        try:
            return DateValue(value=date(int(year), int(month), int(day)), source=source)
        except ValueError:
            return DateValue(value=date(int(year), 1, 1), precision=DateValue.YEAR, source=source)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Ratings, streaming, people
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Rating:
    """One rating from one named source. Never combined by the engine."""

    source: str  # "IMDb", "MyAnimeList", "Rotten Tomatoes", ...
    value: float
    scale: str = "/10"  # "/10", "%", "/100", "/5"
    votes: int | None = None
    via: str = ""  # e.g. "live web search" when scraped rather than read from a database
    url: str | None = None
    volatile: bool = True  # ratings change; they cache on the short TTL

    def display(self) -> str:
        text = f"{self.source}: {self.value:g}{self.scale}"
        if self.votes:
            text += f" ({_compact_count(self.votes)} votes)"
        if self.via:
            text += f" · via {self.via}"
        return text

    @property
    def normalized(self) -> float:
        """A 0–100 score for internal ranking only, never displayed as a rating."""
        if self.scale == "%":
            return max(0.0, min(100.0, self.value))
        if self.scale == "/5":
            return max(0.0, min(100.0, self.value * 20.0))
        if self.scale == "/100":
            return max(0.0, min(100.0, self.value))
        return max(0.0, min(100.0, self.value * 10.0))


def _compact_count(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.0f}k"
    return str(count)


@dataclass(frozen=True, slots=True)
class StreamingOffer:
    """One place a title was verifiably found streaming."""

    platform: str
    region: str = ""  # "" = not region-specific
    url: str | None = None
    source: str = ""  # "Kitsu", "AniList", "live web search", ...
    kind: str = ""  # "subscription", "free", "rent/buy" when known


@dataclass(frozen=True, slots=True)
class Person:
    name: str
    role: str = ""  # Director, Creator, Studio, Cast...

    def display(self) -> str:
        return f"{self.name} ({self.role})" if self.role else self.name


# ---------------------------------------------------------------------------
# Identities (resolver output / autocomplete candidates)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Identity:
    """One resolved candidate for a query — 'who did you mean'."""

    title: str
    kind: MediaKind = MediaKind.UNKNOWN
    year: int | None = None
    year_end: int | None = None
    imdb_id: str = ""
    tvmaze_id: int | None = None
    kitsu_id: str = ""
    anilist_id: int | None = None
    mal_id: int | None = None
    wikidata_id: str = ""
    wiki_title: str = ""
    poster: str = ""
    popularity: float = 0.0  # 0–100, used only for ranking
    alt_titles: list[str] = field(default_factory=list)
    source: str = ""

    @property
    def is_media(self) -> bool:
        return self.kind not in (MediaKind.PERSON, MediaKind.UNKNOWN)

    def label(self) -> str:
        bits = [self.title]
        if self.year:
            span = f"{self.year}" if not self.year_end or self.year_end == self.year else f"{self.year}–{self.year_end}"
            bits.append(span)
        bits.append(kind_label(self.kind))
        return " · ".join(bits)


@dataclass(frozen=True, slots=True)
class SourceNote:
    """Provenance and honesty footnotes attached to a card."""

    text: str
    source: str = ""
    unconfirmed: bool = False

    def display(self) -> str:
        return self.text


# ---------------------------------------------------------------------------
# Related titles (franchise graph + recommendations)
# ---------------------------------------------------------------------------

# The franchise-relation vocabulary we understand, normalised.
RELATION_SEQUEL = "sequel"
RELATION_PREQUEL = "prequel"
RELATION_SPINOFF = "spin-off"
RELATION_SIDE_STORY = "side story"
RELATION_ADAPTATION = "adaptation"
RELATION_REMAKE = "remake"
RELATION_SUMMARY = "summary"  # recap/compilation films
RELATION_ALTERNATIVE = "alternative version"
RELATION_PARENT = "part of"
RELATION_SIMILAR = "similar"
RELATION_OTHER = "related"


@dataclass(slots=True)
class RelatedTitle:
    title: str
    kind: MediaKind = MediaKind.UNKNOWN
    year: int | None = None
    relation: str = RELATION_OTHER
    why: str = ""  # one short reason shown to the user
    rating: Rating | None = None
    url: str | None = None
    popularity: float = 0.0

    def headline(self) -> str:
        text = self.title
        if self.year:
            text += f" ({self.year})"
        return text


# ---------------------------------------------------------------------------
# The assembled title
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MediaTitle:
    """Everything we could verify about one title, with sources."""

    title: str
    kind: MediaKind = MediaKind.UNKNOWN
    alt_titles: list[str] = field(default_factory=list)
    status: ReleaseStatus = ReleaseStatus.UNKNOWN
    status_note: str = ""  # e.g. "renewed for season 2 (Netflix)"
    genres: list[str] = field(default_factory=list)
    synopsis: str = ""
    release: DateValue | None = None  # first/original release
    upcoming: DateValue | None = None  # next release when not yet out
    runtime_minutes: int | None = None
    episodes: int | None = None
    episode_runtime: int | None = None
    seasons: int | None = None
    country: str = ""
    language: str = ""
    cast: list[Person] = field(default_factory=list)
    creators: list[str] = field(default_factory=list)  # director / creator
    studios: list[str] = field(default_factory=list)
    networks: list[str] = field(default_factory=list)  # where it airs
    ratings: list[Rating] = field(default_factory=list)
    streaming: list[StreamingOffer] = field(default_factory=list)
    related: list[RelatedTitle] = field(default_factory=list)
    similar: list[RelatedTitle] = field(default_factory=list)
    trailer_url: str | None = None
    poster: str = ""
    official_site: str | None = None
    popularity_hint: str = ""  # e.g. "#3 most-read film on Wikipedia yesterday"
    identity: Identity | None = None
    notes: list[SourceNote] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    # -- conveniences -----------------------------------------------------

    def primary_rating(self) -> Rating | None:
        return self.ratings[0] if self.ratings else None

    def note(self, text: str, source: str = "", unconfirmed: bool = False) -> None:
        self.notes.append(SourceNote(text=text, source=source, unconfirmed=unconfirmed))

    def genre_line(self, limit: int = 5) -> str:
        return ", ".join(self.genres[:limit])

    def cast_names(self, limit: int = 5) -> list[str]:
        return [p.name for p in self.cast[:limit]]

    @property
    def is_upcoming(self) -> bool:
        if self.status == ReleaseStatus.UPCOMING:
            return True
        release = self.release or self.upcoming
        if release and release.value is not None:
            return release.value > now_utc().date()
        return False

    def runtime_text(self) -> str:
        if not self.runtime_minutes:
            return ""
        hours, minutes = divmod(int(self.runtime_minutes), 60)
        if hours and minutes:
            return f"{hours}h {minutes}m"
        if hours:
            return f"{hours}h"
        return f"{minutes}m"


# ---------------------------------------------------------------------------
# Trending & upcoming entries
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TrendingEntry:
    title: str
    kind: MediaKind = MediaKind.UNKNOWN
    metric: str = ""  # e.g. "238k Wikipedia reads yesterday"
    list_kind: str = "trending"  # trending / popular / top-rated — not the same thing!
    rating: Rating | None = None
    year: int | None = None
    url: str | None = None
    source: str = ""

    def headline(self) -> str:
        text = self.title
        if self.year:
            text += f" ({self.year})"
        return text


@dataclass(slots=True)
class UpcomingEntry:
    title: str
    kind: MediaKind = MediaKind.UNKNOWN
    when: DateValue | None = None
    genres: list[str] = field(default_factory=list)
    platform: str = ""
    country: str = ""
    status_note: str = ""  # e.g. "officially announced"
    url: str | None = None
    source: str = ""

    def when_text(self) -> str:
        if self.when is None:
            return "date unannounced"
        return self.when.human()
