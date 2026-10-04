"""Natural-language understanding for entertainment chat.

The bot answers plain-chat questions about movies, anime and series the
same way the dictionary answers "define serendipity". This module turns
a message into an intent — or returns ``None`` when the message isn't
clearly an entertainment request (never trigger on ordinary chat).

Supported intents:

    RECOMMEND   "recommend anime like Attack on Titan"
                "what should I watch after Breaking Bad?"
                "give me underrated anime", "best thrillers recently"
    TRENDING    "what's trending right now?", "popular movies this week"
    UPCOMING    "upcoming marvel movies", "anime releasing next season"
                "what comes out this friday?"
    STREAMING   "where can I watch X in india?", "is X on netflix?"
    RATING      "what's the rating of X?", "X rating"
    INFO        "tell me everything about Dune"

Every intent carries the extracted subject, type/genre/region/tone
qualifiers, and a confidence — the cog only acts on confident parses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

RECOMMEND = "recommend"
TRENDING = "trending"
UPCOMING = "upcoming"
STREAMING = "streaming"
RATING = "rating"
INFO = "info"

TYPE_WORDS = {
    "movie": "movies",
    "film": "movies",
    "movies": "movies",
    "anime": "anime",
    "series": "series",
    "show": "series",
    "shows": "series",
    "tv": "series",
    "web series": "web_series",
    "webseries": "web_series",
    "web show": "web_series",
}

STREAMING_PLATFORMS = (
    "netflix", "prime video", "primevideo", "amazon prime", "hotstar", "disney+",
    "disney plus", "hulu", "max", "hbo max", "crunchyroll", "peacock", "paramount+",
    "paramount plus", "apple tv", "jiocinema", "jio cinema", "sonyliv", "sony liv",
    "zee5", "aha", "mx player", "voot",
)

_MAX_MESSAGE = 120  # entertainment questions are short; walls of text are chat

_RECOMMEND_LIKE = re.compile(
    r"(?:recommend|suggest|give me|find me|any)\s+(?:(?P<count>\d{1,2})\s+)?"
    r"(?:(?:some|me|a|an|the|good|new)\s+)*"
    r"(?P<kind>anime|movies?|films?|shows?|series|web\s?series)?\s*"
    r"(?:that\s+)?(?:are\s+)?(?:something\s+)?"
    r"(?:like|similar to|after|in the vein of)\s+(?P<subject>[a-z0-9:'!\- .&]{2,60})",
    re.IGNORECASE,
)
_WATCH_AFTER = re.compile(
    r"what\s+(?:(?:good|new)\s+)?(?P<kind>anime|movies?|films?|shows?|series|web\s?series)?\s*"
    r"(?:should|can|do)\s+i\s+watch\s+(?:after|next|now)\s+"
    r"(?P<subject>[a-z0-9:'!\- .&]{2,60})",
    re.IGNORECASE,
)
_RECOMMEND_VIBE = re.compile(
    r"^(?:recommend|suggest|give me|find me|looking for|need)\s+(?P<subject>(?:some\s+|a\s+|an\s+)?"
    r"(?:underrated|overlooked|short|dark|darker|uplifting|feel[- ]good|new|recent|good|best|highly rated)?"
    r"[\w\s-]*?(?:anime|movies?|films?|shows?|series|web\s?series))"
    r"(?:\s+(?:under\s+\d{1,3}\s*episodes?|for\s+me|please|right now|these days))?\s*\??$",
    re.IGNORECASE,
)
_BEST_OF = re.compile(
    r"^(?:best|top|good|some)\s+(?P<chunk>[a-z0-9\- ]{3,50}?)"
    r"(?:\s+(?:released|out|from)\s+(?P<era>this year|recently|last year|the \d{0,4}s?))?\s*\??$",
    re.IGNORECASE,
)
_TRENDING = re.compile(
    r"^(?:what(?:'s| is| are|s)?|whats|which|show me|got any)\s+"
    r"(?:the\s+|everyone\s+|people\s+|y'?all\s+)?"
    r"((?:.*?\s)?(?:trending|popular|hot|happening)|"
    r"everyone\s+watching|people\s+watching|most\s+watched|"
    r"(?:top|most|highest)[\s-]+(?:rated|popular|watched|anticipated)\s+\S+)",
    re.IGNORECASE,
)
_BARE_TRENDING = re.compile(
    r"^(?:trending|popular|hot|top|most\s+watched|most\s+popular|top\s+rated|highest\s+rated)\b",
    re.IGNORECASE,
)
# Whole-subject words that describe a *class* of titles, never a title
# itself ("upcoming highly rated movies" has no real subject).
_QUALIFIER_SUBJECTS = {
    "", "the", "a", "an", "some", "any", "new", "best", "good", "top",
    "highly rated", "rated", "most anticipated", "exciting", "big",
}

_UPCOMING = re.compile(
    r"(?:upcoming|next|new|future)\s+"
    r"(?P<subject>[a-z0-9:'\- .&]*?)\s*"
    r"(?P<category>movies?|films?|anime|shows?|series|web\s?series|episodes?|seasons?)\b"
    r"(?:\s+(?P<year>20\d{2}))?"
    r"(?:\s+(?:coming|releasing|out|soon|this\s+year))?"
    r"\s*[.!?]?\s*$",
    re.IGNORECASE,
)
_BEST_RELEASES = re.compile(
    r"^(?:best|new|top|any|good)\s+(?:new\s+)?releases?\s*"
    r"(?P<era>this month|this week|this year|recently|this season|soon|next month)?\s*\??$",
    re.IGNORECASE,
)
_CATEGORY_RELEASING = re.compile(
    r"(?P<category>movies?|films?|anime|shows?|series|web\s?series)\s+"
    r"(?:releasing|dropping|coming\s+out)\s+"
    r"(?P<era>this season|next season|this month|next month|this week|next week|soon|this year|next year)",
    re.IGNORECASE,
)
_WHAT_COMES_OUT = re.compile(
    r"what\s+(?:(?P<category1>movies?|films?|anime|shows?|series|web\s?series)\s+)?"
    r"(?:(?:'s|is|are)\s+)?"
    r"(?:comes\s+out|come\s+out|coming\s+out|coming|out|releasing|dropping)\s*"
    r"(?P<when>this week|this month|this friday|tomorrow|today|next week|next month|this year|soon)?"
    r"(?:\s+(?P<category2>movies?|films?|anime|shows?|series|web\s?series))?"
    r"\s*\??\s*$",
    re.IGNORECASE,
)
_WHEN_RELEASE = re.compile(
    r"(?:when|what date)\s+(?:does|is|do|will)\s+(?P<subject>[a-z0-9:'!\- .&]{2,60}?)\s*"
    r"(?:(?:season|part|series)\s*(?P<season>\d{1,2})\s*)?"
    r"(?:come out|coming out|release|releasing|air|airing|premiere|premiering|drop|dropping)",
    re.IGNORECASE,
)
_IS_COMING = re.compile(
    r"is\s+(?P<subject>[a-z0-9:'!\- .&]{2,60}?)\s*"
    r"(?:(?:season|part)\s*(?P<season>\d{1,2})\s*)?"
    r"(?:coming|renewed|happening|returning|getting another season)",
    re.IGNORECASE,
)
_WHERE_WATCH = re.compile(
    r"(?:where\s+(?:can|could|do|does)\s+(?:i|we|you)?\s*(?:watch|stream)|"
    r"how\s+(?:can|do)\s+(?:i|we)\s+watch)\s+(?P<subject>[a-z0-9:'!\- .&]{2,60}?)"
    r"(?:\s+in\s+(?P<region>[a-z ]{2,20}))?\s*\??\s*$",
    re.IGNORECASE,
)
_ON_PLATFORM = re.compile(
    r"(?:is|are)\s+(?P<subject>[a-z0-9:'!\- .&]{2,60}?)\s+"
    r"(?:available\s+)?on\s+(?P<platform>[a-z0-9+ .]{3,20})"
    r"(?:\s+in\s+(?P<region>[a-z ]{2,20}))?\s*\??\s*$",
    re.IGNORECASE,
)
_TRENDING_REGION = re.compile(r"(?:in|for)\s+(?P<region>[a-z ]{3,20})\s*\??\s*$", re.IGNORECASE)
_RATING = re.compile(
    r"(?:what(?:'s| is)\s+the\s+(?:imdb|rotten tomatoes|mal|myanimelist)?\s*rating\s+(?:of|for)\s+"
    r"(?P<subject1>[a-z0-9:'!\- .&]{2,60})|"
    r"(?P<subject2>[a-z0-9:'!\- .&]{2,60}?)\s+(?:imdb\s+)?rating)",
    re.IGNORECASE,
)
_INFO = re.compile(
    r"(?:tell me (?:everything|more|all)? ?about|info(?:rmation)? (?:about|on)|"
    r"details (?:about|on)|what is)\s+(?P<subject>[a-z0-9:'!\- .&?]{2,60})",
    re.IGNORECASE,
)
_TONE_RE = re.compile(
    r"\b(?:but\s+)?(?P<prefix>darker|dark|lighter|uplifting|feel[- ]good|scary|funnier|serious|"
    r"heartbreaking|cozy|mind[- ]bending|short(?:er)?)\b",
    re.IGNORECASE,
)
_UNDER_EPISODES = re.compile(r"under\s+(\d{1,3})\s*episodes?", re.IGNORECASE)
_GENRES = (
    "action", "adventure", "comedy", "crime", "documentary", "drama", "fantasy",
    "historical", "horror", "mystery", "psychological", "romance", "romantic comedy",
    "sci-fi", "science fiction", "slasher", "sports", "superhero", "thriller", "war",
    "western", "shonen", "isekai", "mecha", "slice of life",
)


@dataclass(slots=True)
class ChatIntent:
    kind: str
    subject: str = ""
    raw: str = ""
    media_type: str = ""  # movies / anime / series / web_series
    genre: str = ""
    region: str = ""
    platform: str = ""
    tone: str = ""
    era: str = ""
    season: int | None = None
    max_episodes: int | None = None
    count: int | None = None
    confidence: float = 0.0

    @property
    def is_actionable(self) -> bool:
        if self.kind in (RECOMMEND, STREAMING, RATING) and not self.subject and not self.media_type:
            return False
        return True


def parse_recommend_query(text: str) -> ChatIntent:
    """Parse the free-text argument of /recommend into an intent.

    Accepts everything the chat parser accepts, plus bare titles and
    "like X" shorthand.
    """
    raw = (text or "").strip()
    if not raw:
        return ChatIntent(kind=RECOMMEND, raw=raw)

    intent = match_entertainment_request(raw)
    if intent and intent.kind == RECOMMEND and (intent.subject or intent.media_type or intent.genre):
        return intent

    lowered = raw.casefold()
    # "like Interstellar" / "similar to One Piece"
    match = re.match(
        r"(?:more\s+)?(?:like|similar to|after)\s+(?P<subject>.+)$", lowered
    )
    if match:
        subject = _clean_subject(match.group("subject"))
        if subject and not _is_generic(subject):
            return ChatIntent(kind=RECOMMEND, subject=subject, raw=raw, confidence=0.9)

    built = ChatIntent(kind=RECOMMEND, subject=raw, raw=raw, confidence=0.7)
    _apply_qualifiers(lowered, built)
    if _is_vibe_only_phrase(lowered):
        return ChatIntent(
            kind=RECOMMEND,
            media_type=built.media_type,
            genre=built.genre,
            tone=built.tone or ("underrated" if "underrated" in lowered else ""),
            era=built.era,
            max_episodes=built.max_episodes,
            raw=raw,
            confidence=0.8,
        )
    return built


def _is_vibe_only_phrase(lowered: str) -> bool:
    """True when a phrase is made entirely of qualifiers (no seed title).

    "underrated horror movies" → True; "attack on titan" → False.
    """
    words = re.findall(r"[a-z0-9']+", lowered)
    if not words:
        return False
    qualifiers = {
        "underrated", "overlooked", "dark", "darker", "lighter", "uplifting",
        "feel", "good", "new", "recent", "recently", "best", "top", "highly",
        "rated", "short", "shorter", "movies", "movie", "films", "film",
        "shows", "show", "series", "anime", "web", "the", "a", "an", "some",
        "me", "give", "recommend", "something", "watch", "released", "this",
        "year", "week", "month", "season", "under", "episodes",
    }
    qualifiers |= set(_GENRES) | {genre.replace(" ", "") for genre in _GENRES}
    # Bare numbers are episode counts / years ("under 25 episodes"), not titles.
    return all(word in qualifiers or word.isdigit() for word in words)


def match_entertainment_request(text: str) -> ChatIntent | None:
    """Parse a chat message into an entertainment intent, or None."""
    message = (text or "").strip()
    if not message or len(message) > _MAX_MESSAGE:
        return None
    lowered = message.casefold()

    # An exclamation-heavy or question-less imperative is fine; but a
    # message that's mostly a URL or a mention is not for us.
    if "http://" in lowered or "https://" in lowered:
        return None

    # --- streaming first: most specific phrasings -------------------------
    match = _ON_PLATFORM.match(lowered)
    if match:
        platform = match.group("platform").strip()
        if any(p in platform for p in STREAMING_PLATFORMS) or platform in STREAMING_PLATFORMS:
            subject = _clean_subject(match.group("subject"))
            if subject:
                return ChatIntent(
                    kind=STREAMING,
                    subject=subject,
                    platform=platform.strip(),
                    region=_clean_region(match.group("region")),
                    raw=message,
                    confidence=0.95,
                )
    match = _WHERE_WATCH.search(lowered)
    if match:
        subject = _clean_subject(match.group("subject"))
        if subject:
            return ChatIntent(
                kind=STREAMING,
                subject=subject,
                region=_clean_region(match.group("region")),
                raw=message,
                confidence=0.9,
            )

    # --- upcoming / release dates ------------------------------------------
    match = _WHEN_RELEASE.search(lowered)
    if match:
        subject = _clean_subject(match.group("subject"))
        if subject and not _is_generic(subject):
            return ChatIntent(
                kind=UPCOMING,
                subject=subject,
                season=int(match.group("season")) if match.group("season") else None,
                raw=message,
                confidence=0.95,
            )
    match = _IS_COMING.match(lowered)
    if match:
        subject = _clean_subject(match.group("subject"))
        if subject and not _is_generic(subject):
            return ChatIntent(
                kind=UPCOMING,
                subject=subject,
                season=int(match.group("season")) if match.group("season") else None,
                raw=message,
                confidence=0.85,
            )
    match = _BEST_RELEASES.match(lowered)
    if match:
        return ChatIntent(
            kind=UPCOMING,
            era=match.group("era") or "recently",
            raw=message,
            confidence=0.8,
        )
    match = _WHAT_COMES_OUT.search(lowered)
    if match:
        category = match.group("category1") or match.group("category2") or ""
        return ChatIntent(
            kind=UPCOMING,
            media_type=_category_of(category),
            era=match.group("when") or "this week",
            raw=message,
            confidence=0.9,
        )
    match = _CATEGORY_RELEASING.search(lowered)
    if match:
        return ChatIntent(
            kind=UPCOMING,
            media_type=_category_of(match.group("category") or ""),
            era=match.group("era") or "",
            raw=message,
            confidence=0.85,
        )
    match = _UPCOMING.search(lowered)
    if match:
        subject = _clean_subject(match.group("subject") or "")
        category = _category_of(match.group("category") or "")
        intent = ChatIntent(
            kind=UPCOMING,
            subject="" if subject in ("", "the", "some") or _is_generic(subject) else subject,
            media_type=category,
            raw=message,
            confidence=0.85,
        )
        if match.group("year"):
            intent.era = match.group("year")
        # platform words ("upcoming netflix shows") are filters, not subjects
        for platform in STREAMING_PLATFORMS:
            if platform in (intent.subject or ""):
                intent.platform = platform
                intent.subject = re.sub(
                    rf"\s*(?:on\s+)?{re.escape(platform)}\s*", " ", intent.subject
                ).strip()
                break
        if intent.subject in _QUALIFIER_SUBJECTS:
            intent.subject = ""
        # "next season" → anime season wording
        if "next season" in lowered or "next cour" in lowered:
            intent.era = intent.era or "next season"
            if not intent.media_type and "anime" in lowered:
                intent.media_type = "anime"
        return intent

    # --- trending ------------------------------------------------------------
    trending_match = _TRENDING.search(lowered)
    if trending_match is None and _BARE_TRENDING.match(lowered):
        # Bare "trending anime" / "top web series right now" — but only when
        # an entertainment word is present, so plain "top 10 cooking tips"
        # never triggers.
        has_media_word = any(
            re.search(r"\b" + re.escape(word) + r"\b", lowered) for word in TYPE_WORDS
        )
        has_time_word = any(
            marker in lowered for marker in ("right now", "today", "this week", "this month")
        )
        if has_media_word or has_time_word:
            trending_match = _BARE_TRENDING.match(lowered)
    if trending_match:
        intent = ChatIntent(kind=TRENDING, raw=message, confidence=0.9)
        for word, mapped in TYPE_WORDS.items():
            if re.search(rf"\b{re.escape(word)}\b", lowered):
                intent.media_type = mapped
                break
        intent.era = _window_of(lowered)
        region = _TRENDING_REGION.search(lowered)
        if region:
            candidate = _clean_region(region.group("region"))
            if candidate and candidate.casefold() not in (
                "the mood", "a movie", "something", "a while", "a list", "my area",
            ):
                intent.region = candidate
        for platform in STREAMING_PLATFORMS:
            if f"on {platform}" in lowered or f"in {platform}" in lowered:
                intent.platform = platform
                break
        return intent

    # --- recommendations -------------------------------------------------------
    match = _RECOMMEND_LIKE.search(lowered)
    if match:
        subject = _clean_subject(match.group("subject"))
        if subject and not _is_generic(subject):
            intent = ChatIntent(
                kind=RECOMMEND,
                subject=subject,
                media_type=_category_of(match.group("kind") or ""),
                raw=message,
                confidence=0.95,
            )
            if match.group("count"):
                intent.count = max(1, min(10, int(match.group("count"))))
            _apply_qualifiers(lowered, intent)
            return intent
    match = _WATCH_AFTER.search(lowered)
    if match:
        subject = _clean_subject(match.group("subject"))
        if subject and not _is_generic(subject):
            intent = ChatIntent(
                kind=RECOMMEND,
                subject=subject,
                media_type=_category_of(match.group("kind") or ""),
                raw=message,
                confidence=0.9,
            )
            _apply_qualifiers(lowered, intent)
            return intent
    match = _RECOMMEND_VIBE.search(lowered)
    if match and lowered.startswith(("recommend", "suggest", "give me", "find me", "looking for", "need")):
        chunk = match.group("subject").strip()
        intent = ChatIntent(kind=RECOMMEND, raw=message, confidence=0.8)
        for word, mapped in TYPE_WORDS.items():
            if re.search(rf"\b{re.escape(word)}\b", chunk):
                intent.media_type = mapped
                break
        for genre in _GENRES:
            if genre in chunk:
                intent.genre = genre
                break
        if "underrated" in chunk:
            intent.tone = "underrated"
        _apply_qualifiers(lowered, intent)
        return intent

    # "best thriller movies released recently" — attributes, no seed title
    match = _BEST_OF.match(lowered)
    if match:
        chunk = match.group("chunk").strip()
        if any(
            marker in chunk for marker in ("movie", "film", "anime", "show", "series")
        ):
            intent = ChatIntent(kind=RECOMMEND, raw=message, confidence=0.75)
            for word, mapped in TYPE_WORDS.items():
                if re.search(rf"\b{re.escape(word)}\b", chunk):
                    intent.media_type = mapped
                    break
            for genre in _GENRES:
                if re.search(rf"\b{re.escape(genre)}\b", chunk):
                    intent.genre = genre
                    break
            if match.group("era"):
                intent.era = match.group("era")
            elif "recent" in lowered or "new" in lowered:
                intent.era = "recent"
            if intent.media_type or intent.genre:
                return intent

    # --- ratings -----------------------------------------------------------------
    match = _RATING.search(lowered)
    if match:
        subject = _clean_subject(match.group("subject1") or match.group("subject2") or "")
        if subject and not _is_generic(subject):
            return ChatIntent(kind=RATING, subject=subject, raw=message, confidence=0.9)

    # --- info ---------------------------------------------------------------------
    match = _INFO.search(lowered)
    if match:
        subject = _clean_subject(match.group("subject"))
        if subject and not _is_generic(subject):
            intent = ChatIntent(kind=INFO, subject=subject, raw=message, confidence=0.75)
            return intent

    return None


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

_STOP_PREFIXES = (
    "can you", "could you", "please", "some", "me", "us", "a ", "an ", "the ",
    "good ", "new ", "best ", "more ",
)


def _clean_subject(subject: str) -> str:
    subject = (subject or "").strip().strip("?,.!\"'“”‘’").strip()
    subject = re.sub(r"\s+", " ", subject)
    # trailing qualifiers that are not part of a title
    subject = re.sub(
        r"\s+(?:but\s+)?(?:darker|lighter|short(?:er)?|on netflix|on prime|on hotstar|"
        r"in india|in the us|under \d+ episodes|next season|this season|please)\s*$",
        "",
        subject,
        flags=re.IGNORECASE,
    ).strip()
    subject = re.sub(r"\s+(?:\?+|\.+)$", "", subject)
    changed = True
    while changed and subject:
        changed = False
        for prefix in _STOP_PREFIXES:
            if subject.casefold().startswith(prefix) and len(subject) > len(prefix) + 1:
                subject = subject[len(prefix):].strip()
                changed = True
                break
    return subject.strip()


def _is_generic(subject: str) -> bool:
    """Subjects that are clearly not a title."""
    generic = {
        "a movie", "something", "anything", "it", "this", "that", "one",
        "new season", "season", "the next one", "some", "tonight", "soon",
        "the movie", "the show", "the series", "the anime",
    }
    return subject.strip().casefold() in generic or not subject.strip()


def _category_of(word: str) -> str:
    word = (word or "").strip().casefold()
    if not word:
        return ""
    return TYPE_WORDS.get(word.replace("  ", " "), "")


def _window_of(lowered: str) -> str:
    if "this week" in lowered or "this month" in lowered or "right now" in lowered or "today" in lowered:
        return "now"
    if "this season" in lowered or "this year" in lowered:
        return "season"
    return ""


def _clean_region(region: str | None) -> str:
    region = (region or "").strip().strip("?.").strip()
    if not region:
        return ""
    return region.title() if len(region) > 3 else region.upper()


def _apply_qualifiers(lowered: str, intent: ChatIntent) -> None:
    tone = _TONE_RE.search(lowered)
    if tone:
        intent.tone = tone.group("prefix").casefold()
    episodes = _UNDER_EPISODES.search(lowered)
    if episodes:
        intent.max_episodes = int(episodes.group(1))
    for genre in _GENRES:
        if re.search(rf"\b{re.escape(genre)}\b", lowered):
            intent.genre = genre
            break
    if "recent" in lowered or "recently" in lowered or "new" in lowered:
        intent.era = "recent"
