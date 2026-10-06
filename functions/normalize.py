"""Request normalisation: turn messy human text into routable text.

People do not type like documentation. They type::

    yo bro wats the wthr in delhi rn??
    how many bloody rupees is 50 doller
    dictonry meaning of ephemeral pls
    calc 23% 829

This module is the first stage of the ping pipeline. It rewrites that
kind of input into something the deterministic router and the tolerant
keyword classifier can actually read, without ever changing what the
user meant:

* strips mentions, emoji, control characters and decorative punctuation;
* collapses elongated spellings (``weatherrrr`` → ``weather``);
* expands chat shorthand (``rn`` → ``now``, ``tmr`` → ``tomorrow``,
  ``u`` → ``you``);
* fixes common misspellings of the words that select a tool
  (``wthr``/``wheather`` → ``weather``, ``dictonary`` → ``dictionary``);
* understands light Hinglish scaffolding (``delhi ka weather batao``);
* removes filler and profanity that carry no routing signal
  (``bro``, ``bhai``, ``pls``, ``bloody``);
* rewrites a few spoken maths shapes (``divide 829 by 17`` → ``829 / 17``,
  ``23% 829`` → ``23% of 829``).

Everything here is pure, synchronous and allocation-light: it runs on
every mention, so it must never touch the network or block the loop.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Character level cleanup
# ---------------------------------------------------------------------------

_DISCORD_MENTION = re.compile(r"<@[!&]?\d+>|<#\d+>|<a?:\w+:\d+>")
_ELONGATED = re.compile(r"(.)\1{2,}")
_MULTI_PUNCT = re.compile(r"([!?.,])\1+")
_SPACES = re.compile(r"\s+")
# Keep letters, digits, whitespace and the handful of symbols that carry
# meaning for the tools (maths, currency, degrees, quotes, slashes).
_KEEP = set("+-*/^%()[]<>.,:;'\"!?@&=_°$€£¥₹#")
_QUOTES = {"“": '"', "”": '"', "’": "'", "‘": "'", "–": "-", "—": "-", "−": "-"}


def _strip_decorations(text: str) -> str:
    """Remove emoji/pictographs and anything that is not usable text."""
    out: list[str] = []
    for char in text:
        code = ord(char)
        # Variation selectors / joiners: invisible emoji plumbing.
        if 0xFE00 <= code <= 0xFE0F or code in (0x200D, 0x20E3):
            continue
        if char in _QUOTES:
            out.append(_QUOTES[char])
            continue
        if char.isspace():
            out.append(" ")
            continue
        if char.isalnum() or char in _KEEP:
            out.append(char)
            continue
        category = unicodedata.category(char)
        # Symbols (So/Sk = emoji, modifiers) and private use are decoration.
        if category in ("So", "Sk", "Cs", "Co", "Cf", "Cc"):
            continue
        if category.startswith("P"):
            out.append(" ")
            continue
        out.append(char)
    return "".join(out)


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: Single-token rewrites. Keys are already lowercase.
ALIASES: dict[str, str] = {
    # chat shorthand
    "rn": "now", "atm": "now", "2day": "today", "tdy": "today",
    "tmr": "tomorrow", "tmrw": "tomorrow", "tmrrw": "tomorrow", "tomorow": "tomorrow",
    "tomorrw": "tomorrow", "yest": "yesterday",
    "u": "you", "ur": "your", "urs": "yours", "r": "are", "n": "and",
    "abt": "about", "bcz": "because", "cuz": "because", "coz": "because",
    "pic": "picture", "pics": "pictures", "picz": "pictures", "pictur": "picture",
    "img": "image", "imgs": "images", "photoz": "photos", "fotos": "photos",
    "vid": "video", "vids": "videos",
    "wat": "what", "wats": "what is", "whats": "what is", "wht": "what",
    "whts": "what", "wha": "what", "waht": "what", "wot": "what",
    "hw": "how", "y": "why", "plz": "please", "pls": "please", "plss": "please",
    "thx": "thanks", "ty": "thanks", "k": "ok",
    # tool words and their usual misspellings
    "wthr": "weather", "wetr": "weather", "wether": "weather",
    "wheather": "weather", "weathr": "weather", "wheter": "weather",
    "weater": "weather", "wather": "weather", "whether": "weather",
    "temp": "temperature", "tempature": "temperature", "temprature": "temperature",
    "forcast": "forecast", "forecaste": "forecast",
    "tym": "time", "tim": "time", "clock": "time",
    "dictonary": "dictionary", "dictionry": "dictionary", "dictnary": "dictionary",
    "dictonry": "dictionary", "dictinary": "dictionary", "dict": "dictionary",
    "definiton": "definition", "defination": "definition", "definetion": "definition",
    "defn": "definition", "def": "define", "meanin": "meaning", "mening": "meaning",
    "calc": "calculate", "calcuate": "calculate", "calcualte": "calculate",
    "caluclate": "calculate", "calculte": "calculate", "cal": "calculate",
    "convrt": "convert", "convet": "convert", "covert": "convert",
    "coversion": "conversion", "conver": "convert",
    "translte": "translate", "traslate": "translate", "translat": "translate",
    "tranlate": "translate", "translte,": "translate",
    "srch": "search", "serach": "search", "searh": "search", "seach": "search",
    "googl": "google", "gogle": "google",
    "doller": "dollar", "dollers": "dollars", "dolar": "dollar", "dolars": "dollars",
    "rupes": "rupees", "rupess": "rupees", "rupe": "rupee",
    "kilo": "kg", "kilos": "kg", "kgs": "kg",
    "fahrenheight": "fahrenheit", "farenheit": "fahrenheit", "celcius": "celsius",
    "recipie": "recipe", "recepie": "recipe",
    # light Hinglish scaffolding
    "kya": "what", "kitna": "how much", "kitne": "how much", "batao": "tell",
    "bata": "tell", "dikhao": "show", "dikha": "show", "karo": "do",
    "mausam": "weather", "samay": "time", "matlab": "meaning",
}

#: Tokens that never carry routing signal. Removed wherever they appear.
NOISE_WORDS = frozenset(
    {
        "bro", "bruh", "bhai", "bhaiya", "dude", "mate", "man", "sir", "pls",
        "please", "plz", "kindly", "paradise", "paradisebot", "bot", "hey",
        "yo", "oi", "umm", "uh", "erm", "lol", "lmao", "ok", "okay", "just",
        "basically", "actually", "literally", "quickly", "asap",
        # mild profanity carries no meaning for routing
        "bloody", "damn", "goddamn", "freaking", "frickin", "fricking",
        "fucking", "fuckin", "fkin", "shit", "bs", "wtf", "af",
        # Hinglish particles
        "hai", "hain", "ka", "ki", "ke", "ko", "na", "toh", "yaar", "arre",
    }
)

#: Removed only when they lead or trail the sentence, because in the middle
#: they can be part of the subject ("picture of a man").
EDGE_NOISE = frozenset({"so", "well", "hmm", "umm", "you", "tell", "me", "us", "now"})

#: Vocabulary used for fuzzy repair of words that select a tool.
TOOL_VOCABULARY = (
    "weather", "temperature", "forecast", "humidity", "raining", "rain",
    "time", "timezone", "date", "today", "tomorrow", "yesterday",
    "calculate", "percent", "divide", "multiply", "convert", "conversion",
    "currency", "dollars", "rupees", "euros", "pounds", "kilometers", "miles",
    "define", "definition", "dictionary", "meaning", "spelling", "synonym",
    "translate", "translation", "language",
    "picture", "pictures", "image", "images", "photo", "photos", "wallpaper",
    "search", "google", "news", "latest", "update", "recipe",
)
_VOCAB_BY_LENGTH: dict[int, tuple[str, ...]] = {}
for _word in TOOL_VOCABULARY:
    _VOCAB_BY_LENGTH.setdefault(len(_word), ())
for _length in list(_VOCAB_BY_LENGTH):
    _VOCAB_BY_LENGTH[_length] = tuple(w for w in TOOL_VOCABULARY if len(w) == _length)

_KNOWN_WORDS = frozenset(TOOL_VOCABULARY) | frozenset(ALIASES.values())


def _edit_distance_at_most(a: str, b: str, limit: int = 1) -> bool:
    """Cheap bounded Damerau-ish check: True when ``a`` and ``b`` are close."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > limit:
        return False
    # Classic DP, but the strings here are short tool words (< 16 chars).
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (ca != cb),
                )
            )
        if min(current) > limit:
            return False
        previous = current
    return previous[-1] <= limit


_SUFFIXES = ("s", "es", "d", "ed", "ing", "ly", "er", "ers", "ion")


def _is_inflection(token: str) -> bool:
    """True when *token* is a normal English inflection of a known word.

    Protects real words from being "corrected" into a tool keyword:
    ``divided`` must not become ``divide`` (the calculator understands
    "divided by"), and ``times`` must never become ``time``.
    """
    for suffix in _SUFFIXES:
        if not token.endswith(suffix) or len(token) <= len(suffix) + 2:
            continue
        stem = token[: -len(suffix)]
        if stem in _KNOWN_WORDS or f"{stem}e" in _KNOWN_WORDS:
            return True
    return False


def repair_token(token: str) -> str:
    """Fuzzy-fix a token against the tool vocabulary (bounded, conservative)."""
    if len(token) < 5 or token in _KNOWN_WORDS or any(ch.isdigit() for ch in token):
        return token
    if _is_inflection(token):
        return token
    for length in (len(token) - 1, len(token), len(token) + 1):
        for candidate in _VOCAB_BY_LENGTH.get(length, ()):  # short lists
            if _edit_distance_at_most(token, candidate, 1):
                return candidate
    return token


# ---------------------------------------------------------------------------
# Phrase level rewrites
# ---------------------------------------------------------------------------

_NUM = r"\d+(?:[.,]\d+)?"
_PHRASES: tuple[tuple[re.Pattern[str], str], ...] = (
    # spoken maths
    (re.compile(rf"\bdivide\s+({_NUM})\s+by\s+({_NUM})\b"), r"\1 / \2"),
    (re.compile(rf"\bmultiply\s+({_NUM})\s+(?:by|with|and)\s+({_NUM})\b"), r"\1 * \2"),
    (re.compile(rf"\badd\s+({_NUM})\s+(?:to|and|\+)\s+({_NUM})\b"), r"\1 + \2"),
    (re.compile(rf"\bsubtract\s+({_NUM})\s+from\s+({_NUM})\b"), r"\2 - \1"),
    (re.compile(rf"\bsquare\s+root\s+(?:of\s+)?({_NUM})\b"), r"sqrt(\1)"),
    # "23% 829" and "23 % of 829"
    (re.compile(rf"({_NUM})\s*%\s+({_NUM})\b"), r"\1% of \2"),
    (re.compile(rf"({_NUM})\s*percent\s+(?:of\s+)?({_NUM})\b"), r"\1% of \2"),
    # question shorthands
    (re.compile(r"\bwhat\s+is\s+is\b"), "what is"),
    (re.compile(r"\bwhats\b"), "what is"),
    (re.compile(r"\bhows\b"), "how is"),
    (re.compile(r"\bwhat\s+time\s+(?:is\s+)?(?:it\s+)?in\b"), "time in"),
    # Hinglish possessive: "delhi ka weather" → "weather delhi"
    (re.compile(r"\b([a-z][a-z .'-]{1,30}?)\s+(?:ka|ki|ke)\s+(weather|time|temperature)\b"), r"\2 \1"),
)


@dataclass(slots=True)
class NormalizedRequest:
    """The cleaned forms of one user request."""

    raw: str
    text: str                      # cleaned, lowercase, de-slanged sentence
    tokens: list[str] = field(default_factory=list)
    changed: bool = False

    @property
    def token_set(self) -> frozenset[str]:
        return frozenset(self.tokens)

    def without(self, drop: set[str]) -> str:
        """The sentence minus a set of tokens (used to extract subjects)."""
        return " ".join(token for token in self.tokens if token not in drop).strip()


def normalize_request(text: str) -> NormalizedRequest:
    """Normalise *text* for routing. Never raises; always returns a result."""
    raw = " ".join(str(text or "").split())
    working = _DISCORD_MENTION.sub(" ", raw)
    working = _strip_decorations(working).lower()
    working = _MULTI_PUNCT.sub(r"\1", working)
    working = _ELONGATED.sub(r"\1", working)
    working = _SPACES.sub(" ", working).strip()

    for pattern, replacement in _PHRASES:
        working = pattern.sub(replacement, working)

    tokens: list[str] = []
    for token in working.split():
        bare = token.strip(".,;:!?\"'")
        if not bare:
            continue
        mapped = ALIASES.get(bare, bare)
        if mapped != bare:
            tokens.extend(mapped.split())
            continue
        tokens.append(repair_token(bare))

    # Drop noise. Edge noise only at the boundaries.
    tokens = [token for token in tokens if token not in NOISE_WORDS]
    while tokens and tokens[0] in EDGE_NOISE:
        tokens.pop(0)
    while tokens and tokens[-1] in EDGE_NOISE:
        tokens.pop()

    cleaned = " ".join(tokens)
    for pattern, replacement in _PHRASES:  # aliases can expose new shapes
        cleaned = pattern.sub(replacement, cleaned)
    cleaned = _SPACES.sub(" ", cleaned).strip()

    return NormalizedRequest(
        raw=raw,
        text=cleaned,
        tokens=cleaned.split(),
        changed=cleaned != raw.lower().strip(),
    )


__all__ = [
    "ALIASES",
    "EDGE_NOISE",
    "NOISE_WORDS",
    "NormalizedRequest",
    "normalize_request",
    "repair_token",
]
