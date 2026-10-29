"""“Hey Paradise play billy jean” → a play command, in English or Hinglish.

People do not type commands when they are in a voice channel, they talk:

    play billy jean by michael jackson
    billy jean baja do
    अगला गाना लगाओ
    pause karo
    awaaz thodi kam karo

This module turns those into one small dataclass. It is pure text handling —
no Discord, no network — so it is cheap to call on every message and easy to
test exhaustively, which matters because a mis-parse here starts a song in
the wrong place or, worse, cuts off music someone is enjoying.

The order of the rules is the design:

1. a bare URL, a volume request or a loop request is unambiguous;
2. a *play verb at the start* wins over everything, so "play don't stop me
   now" is a song and not a "stop" command;
3. otherwise a short utterance is matched against control keywords
   ("skip", "agla", "ruko", "pause karo", …);
4. otherwise a play verb at the *end* is Hindi word order ("billie jean
   bajao").

Anything longer, or anything with no music word in it at all, returns
``None`` and the message goes to the assistant instead.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

PLAY = "play"
PAUSE = "pause"
RESUME = "resume"
SKIP = "skip"
STOP = "stop"
QUEUE = "queue"
NOWPLAYING = "nowplaying"
VOLUME = "volume"
LOOP = "loop"
SHUFFLE = "shuffle"
LEAVE = "leave"

ACTIONS = (
    PLAY, PAUSE, RESUME, SKIP, STOP, QUEUE, NOWPLAYING,
    VOLUME, LOOP, SHUFFLE, LEAVE,
)


@dataclass(frozen=True, slots=True)
class MusicIntent:
    """A parsed request: what to do, and with what."""

    action: str
    query: str = ""
    value: float | None = None
    raw: str = ""
    #: For ``volume``: ``value`` is a level (True) or a delta (False).
    absolute: bool = False

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return bool(self.action)


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

_PLAY_VERBS = (
    "play", "baja", "bajao", "bajaa", "bajado", "bajade", "sunao", "sunado",
    "chalao", "chala", "lagao", "laga", "lagado", "bajana", "start", "shuru",
    "बजाओ", "बजा", "चलाओ", "चला", "सुनाओ", "लगाओ", "शुरू",
)
# Multi-word play verbs, longest first so "put on" wins over "put".
_PLAY_PHRASES = ("put on", "baja do", "bajaa do", "baja de", "chala do", "suna do",
                 "laga do")

_CONTROL_WORDS: dict[str, frozenset[str]] = {
    STOP: frozenset({
        "stop", "band", "bandh", "band karo", "ruko", "ruk", "bas", "kaafi",
        "बंद", "रोको", "रुको", "बस", "रोक",
    }),
    PAUSE: frozenset({"pause", "unpause no", "tham", "tham ja", "tham jao", "hold",
                      "पॉज़", "रोक दो", "थम"}),
    RESUME: frozenset({"resume", "unpause", "continue", "chalu", "chalu karo",
                       "wapas chala", "wapas chalao", "again", "चालू", "फिर से चलाओ"}),
    SKIP: frozenset({"skip", "next", "nextsong", "agla", "agli", "agle", "aage",
                     "forward", "dusra", "change", "अगला", "अगली", "स्किप", "आगे"}),
    NOWPLAYING: frozenset({"np", "nowplaying", "playing", "कौन", "कौनसा", "konsa"}),
    QUEUE: frozenset({"queue", "playlist", "list", "katār", "katar", "कतार", "सूची"}),
    SHUFFLE: frozenset({"shuffle", "random", "randomise", "randomize", "shafal", "शफल"}),
    LEAVE: frozenset({"leave", "disconnect", "quit", "dc", "nikal", "nikalo", "bahar",
                      "निकल", "छोड़", "डिस्कनेक्ट"}),
}
#: Phrases that are decisive on their own (checked as substrings for order).
_CONTROL_PHRASES: tuple[tuple[str, str], ...] = (
    (NOWPLAYING, "now playing"), (NOWPLAYING, "what's playing"),
    (NOWPLAYING, "whats playing"), (NOWPLAYING, "what is playing"),
    (NOWPLAYING, "which song"), (NOWPLAYING, "kaun sa"), (NOWPLAYING, "kaunsa"),
    (NOWPLAYING, "kya chal raha"), (NOWPLAYING, "ye kaun sa"),
    (NOWPLAYING, "क्या चल रहा"), (NOWPLAYING, "कौन सा"),
    (STOP, "band karo"), (STOP, "band kar do"), (STOP, "bas karo"),
    (STOP, "ruk jao"), (STOP, "stop it"), (STOP, "stop the music"),
    (PAUSE, "pause karo"), (PAUSE, "pause kar do"), (PAUSE, "रोक दो"),
    (RESUME, "chalu karo"), (RESUME, "chalu kar do"), (RESUME, "wapas chalao"),
    (RESUME, "play again"), (RESUME, "फिर से चलाओ"),
    (LEAVE, "leave vc"), (LEAVE, "nikal jao"), (LEAVE, "vc se nikal"),
    (SKIP, "next song"), (SKIP, "next track"), (SKIP, "aage badho"),
    (QUEUE, "the queue"), (QUEUE, "song list"),
)

#: "play next" / "अगला गाना लगाओ" are skips, not searches.
_SKIP_PREFIXES = ("next", "agla", "agli", "aage", "अगला", "अगली", "skip", "dusra")

_GENERIC_QUERY_WORDS = frozenset({
    "the", "a", "an", "some", "any", "this", "that", "ye", "yeh", "wo", "voh",
    "koi", "kuch", "ek", "song", "songs", "music", "gaana", "gana", "gaane",
    "sangeet", "track", "me", "for", "please", "now", "here", "gaana.",
    "गाना", "गाने", "संगीत", "मुझे", "कोई", "एक",
})

_FILLER = re.compile(
    r"\b(?:please|pls|plz|zara|yaar|bhai|bro|abhi|thanks|thank\s+you|ok|okay|"
    r"just|kindly)\b",
    re.IGNORECASE,
)
_WAKE_PREFIX = re.compile(
    r"^(?:hey+|hi+|hello+|yo+|ok+|okay+|oye+|oi+|arey+|arre+)\s+paradise(?:\s*bot)?"
    r"\s*[,:;.!?\-–—]*\s*|^paradise(?:\s*bot)?\s*[,:;!\-–—]\s*",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://\S+", re.IGNORECASE)

_VOLUME_WORD = re.compile(
    r"\b(?:volume|vol|awaaz|awaz|aawaz|sound|आवाज़|वॉल्यूम|आवाज)\b", re.IGNORECASE
)
_VOLUME_VALUE = re.compile(r"(\d{1,3})\s*(?:%|percent|pratishat)?", re.IGNORECASE)
_VOLUME_UP = re.compile(r"\b(?:up|badha|badhao|increase|zyada|ऊपर|बढ़ा)\b", re.IGNORECASE)
_VOLUME_DOWN = re.compile(r"\b(?:down|kam|ghata|ghatao|decrease|नीचे|कम)\b", re.IGNORECASE)
_VOLUME_LEVELS = (
    ("full", 1.0), ("max", 1.0), ("maximum", 1.0), ("loud", 1.0),
    ("half", 0.5), ("aadha", 0.5), ("quiet", 0.25), ("low", 0.25),
)
_LOOP_RE = re.compile(r"^(?:loop|repeat|dohra|dohrana|दोहराओ)\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def _normalise(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = (
        value.replace("’", "'").replace("‘", "'")
        .replace("“", '"').replace("”", '"')
    )
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"^[\s\-–—,;:.!?\"'`]+", "", value)
    value = re.sub(r"[\s\-–—,;:!?\"'`]+$", "", value)
    return value


def _strip_wake(text: str) -> str:
    return _WAKE_PREFIX.sub("", text, count=1).strip()


def _without_filler(text: str) -> str:
    return re.sub(r"\s+", " ", _FILLER.sub(" ", text)).strip()


def _clean_query(query: str) -> str:
    """Strip quotes, articles and politeness from a searched phrase."""
    value = re.sub(r"^[\"'`]+|[\"'`]+$", "", _normalise(query)).strip()
    value = _without_filler(value)
    tokens = value.split()
    while tokens and tokens[0].lower().strip(",.") in _GENERIC_QUERY_WORDS:
        tokens.pop(0)
    while tokens and tokens[-1].lower().strip(",.") in _GENERIC_QUERY_WORDS:
        tokens.pop()
    # A query that ends on a Hindi particle is truncated, never a title:
    # "kishore kumar ke gaane" → "kishore kumar".
    while len(tokens) > 1 and tokens[-1].lower() in {"ke", "ka", "ki", "ko", "se", "me", "mein"}:
        tokens.pop()
    value = " ".join(tokens).strip(" -–—,;:\"'")
    if value and all(
        token.lower().strip(",.") in _GENERIC_QUERY_WORDS for token in value.split()
    ):
        return ""
    return value


def _starts_with_play_verb(text: str) -> tuple[bool, str]:
    lowered = text.lower()
    for phrase in _PLAY_PHRASES:
        if lowered.startswith(phrase + " ") or lowered == phrase:
            return True, text[len(phrase):].strip()
    first = lowered.split(" ", 1)[0]
    if first in _PLAY_VERBS:
        rest = text[len(first):].strip() if len(text) > len(first) else ""
        return True, rest
    return False, text


def _control_action(text: str) -> MusicIntent | None:
    """Match short control utterances against keywords and phrases."""
    lowered = f" {text.lower().strip(' .,!?')} "
    for action, phrase in _CONTROL_PHRASES:
        if phrase in lowered:
            return MusicIntent(action=action, raw=text)

    tokens = [token.strip(".,!?;:'\"") for token in lowered.split()]
    tokens = [token for token in tokens if token]
    if not tokens or len(tokens) > 7:
        return None
    skip_count: int | None = None
    for action in (STOP, PAUSE, RESUME, SKIP, NOWPLAYING, QUEUE, SHUFFLE, LEAVE):
        words = _CONTROL_WORDS[action]
        hits = [token for token in tokens if token in words]
        if not hits:
            continue
        if action is SKIP:
            # "skip 2" / "2 songs aage" — a count is allowed.
            numbers = [int(t) for t in tokens if t.isdigit() and 1 <= int(t) <= 20]
            skip_count = numbers[0] if numbers else None
        return MusicIntent(
            action=action,
            value=float(skip_count) if skip_count else None,
            raw=text,
        )
    return None


def _volume_intent(text: str) -> MusicIntent | None:
    if not _VOLUME_WORD.search(text):
        return None
    value: float | None = None
    match = _VOLUME_VALUE.search(text)
    if match is not None:
        value = max(0.0, min(2.0, int(match.group(1)) / 100.0))
    if value is None:
        lowered = text.lower()
        for word, level in _VOLUME_LEVELS:
            if re.search(rf"\b{word}\b", lowered):
                value = level
                break
    absolute = value is not None
    if value is None:
        if _VOLUME_DOWN.search(text):
            value = -0.1
        elif _VOLUME_UP.search(text):
            value = 0.1
    return MusicIntent(action=VOLUME, value=value, raw=text, absolute=absolute)


def _loop_intent(text: str) -> MusicIntent | None:
    if not _LOOP_RE.match(text):
        return None
    lowered = text.lower()
    mode = "cycle"
    if re.search(r"\b(off|stop|band|no)\b", lowered):
        mode = "off"
    elif re.search(r"\b(song|track|this|ek|gaana|गाना)\b", lowered):
        mode = "track"
    elif re.search(r"\b(queue|all|sab|playlist)\b", lowered):
        mode = "queue"
    return MusicIntent(action=LOOP, query=mode, raw=text)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_music_intent(text: str, *, allow_bare_controls: bool = True) -> MusicIntent | None:
    """Parse one utterance. Returns ``None`` when it is not about music."""
    raw = _strip_wake(_normalise(text))
    if not raw:
        return None

    # 1. Unambiguous shapes.
    url = _URL.search(raw)
    if url is not None and len(raw.split()) <= 3:
        return MusicIntent(action=PLAY, query=url.group(0), raw=raw)
    volume = _volume_intent(raw)
    if volume is not None:
        return volume
    loop = _loop_intent(raw)
    if loop is not None:
        return loop

    # 2. A play verb at the start wins: "play don't stop me now".
    is_play, remainder = _starts_with_play_verb(raw)
    if is_play:
        query = _clean_query(remainder)
        lowered = query.lower()
        if query and any(
            lowered == word or lowered.startswith(word + " ")
            for word in _SKIP_PREFIXES
        ):
            return MusicIntent(action=SKIP, value=1.0, raw=raw)
        if lowered in {"again", "it again"}:
            return MusicIntent(action=RESUME, raw=raw)
        return MusicIntent(action=PLAY, query=query, raw=raw)

    # 3. Short control utterances ("skip", "pause karo", "ruko").
    if allow_bare_controls:
        control = _control_action(raw)
        if control is not None:
            return control

    # 4. Hindi word order: a play verb at the end.
    lowered = raw.lower()
    for phrase in _PLAY_PHRASES + _PLAY_VERBS:
        if lowered.endswith(" " + phrase) or lowered.endswith(" " + phrase + " please"):
            query = _clean_query(raw[: len(raw) - len(phrase)])
            if query.lower() in _GENERIC_QUERY_WORDS:
                query = ""
            return MusicIntent(action=PLAY, query=query, raw=raw)

    return None


def is_control_only(text: str) -> bool:
    """True for "skip"/"pause"/"stop" — safe to honour without a wake word."""
    intent = parse_music_intent(text, allow_bare_controls=True)
    return intent is not None and intent.action in {
        SKIP, PAUSE, RESUME, STOP, QUEUE, NOWPLAYING, VOLUME, LOOP, SHUFFLE, LEAVE
    }


__all__ = [
    "ACTIONS",
    "LEAVE",
    "LOOP",
    "MusicIntent",
    "NOWPLAYING",
    "PAUSE",
    "PLAY",
    "QUEUE",
    "RESUME",
    "SHUFFLE",
    "SKIP",
    "STOP",
    "VOLUME",
    "is_control_only",
    "parse_music_intent",
]
