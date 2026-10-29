"""Finding "hey paradise" in what a speech-to-text engine *thinks* it heard.

A transcript of spontaneous speech is not clean text. The same phrase comes
back as "hey paradise", "Hey, Paradise!", "he paradise", "hey pair of dice"
and, in Hindi, "अरे पैराडाइज़". So matching here is deliberately more forgiving
than the typed-command matcher in ``commands/wake.py``:

* punctuation and case are removed first;
* a greeting must be *immediately* followed by the bot's name (within one
  token), which is what keeps "I said hey to paradise" from triggering;
* the name is matched fuzzily — one edit distance for "paradise", and a small
  table of the mis-hearings that actually occur;
* everything after the name is the question, with politeness trimmed.

The matcher is pure text, so the whole table is in the test suite.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

#: Words that make the phrase a *call* rather than a mention.
GREETINGS: frozenset[str] = frozenset({
    "hey", "heyy", "heya", "hay", "he", "hhey", "hii", "hi", "hiii", "hello", "helo", "yo",
    "ok", "okay", "oye", "oy", "oi", "are", "arey", "arrey", "arre", "aye",
    "ae", "aye", "hallo", "halo",
    # Devanagari
    "अरे", "अर्रे", "हे", "हाय", "हैलो", "ओए", "ओ", "हेलो",
})

#: The bot's name, including the ways it comes back from an STT engine.
NAMES: frozenset[str] = frozenset({
    "paradise", "paradisebot", "paradis", "paradize", "paradaise", "paradis e",
    "pair of dice", "pair a dice", "pair-o-dice", "parade dice", "para dice",
    "paradiso", "paradisee", "paradice",
    # Devanagari
    "पैराडाइज़", "पैराडाइस", "पैराडाइज", "पैराडाइज़बॉट", "पेराडाइस", "पैराडाइसबॉट",
})

#: Politeness that is never part of a question.
_LEADING_FILLER = re.compile(
    r"^(?:please|pls|plz|zara|yaar|bhai|bro|umm?|uh+|"
    r"can\s+you|could\s+you|would\s+you|will\s+you|"
    r"मुझे|ज़रा|जरा)\s+",
    re.IGNORECASE,
)
#: "Paradise is a nice word" must never trigger: a name followed by a copula
#: is a sentence *about* the bot, not a call to it.
_NOT_A_CALL = frozenset({
    "is", "are", "was", "were", "means", "sounds", "rocks", "sucks",
    "and", "or", "but", "the", "a", "an", "wasnt", "isnt",
})
_TRAILING_PUNCT = re.compile(r"[\s\-–—,;:.!?\"'`]+$")
_EDGE_PUNCT = re.compile(r"^[\s\-–—,;:.!?\"'`]+")
_SPACES = re.compile(r"\s+")
_NON_WORD = re.compile(r"[^\w\u0900-\u097F\s'-]+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class WakeMatch:
    """The result of looking for the wake phrase in a transcript."""

    matched: bool
    question: str = ""
    phrase: str = ""
    bare: bool = False  # the phrase was said with nothing after it

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.matched


def normalise(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace (keeps Devanagari)."""
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = value.replace("’", "'").replace("‘", "'")
    value = _NON_WORD.sub(" ", value)
    value = _SPACES.sub(" ", value).strip()
    return value.lower()


def _levenshtein(left: str, right: str) -> int:
    """Tiny edit distance (rapidfuzz is used when it is installed)."""
    if left == right:
        return 0
    if not left or not right:
        return max(len(left), len(right))
    try:
        from rapidfuzz.distance import Levenshtein

        return int(Levenshtein.distance(left, right))
    except Exception:  # pragma: no cover - optional accelerator
        pass
    previous = list(range(len(right) + 1))
    for i, char_left in enumerate(left, start=1):
        current = [i]
        for j, char_right in enumerate(right, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (char_left != char_right),
                )
            )
        previous = current
    return previous[-1]


def _name_at(tokens: list[str], index: int) -> tuple[bool, int]:
    """Does the bot's name start at ``index``? Returns (matched, tokens used)."""
    if index >= len(tokens):
        return False, 0
    # Longest span first: "pair of dice" beats "pair".
    for span in (3, 2, 1):
        if index + span > len(tokens):
            continue
        candidate = " ".join(tokens[index : index + span])
        if candidate in NAMES:
            return True, span
    # Fuzzy: one edit is allowed on long spans only, which tolerates an STT
    # engine writing "paradise" as "paradaise" without matching random words.
    for span in (3, 2, 1):
        if index + span > len(tokens):
            continue
        candidate = " ".join(tokens[index : index + span])
        if len(candidate) < 6:
            continue
        for name in NAMES:
            if len(name) >= 6 and _levenshtein(candidate, name) <= 1:
                return True, span
    return False, 0


def _clean_question(text: str) -> str:
    value = _EDGE_PUNCT.sub("", _SPACES.sub(" ", text).strip())
    for _ in range(3):
        stripped = _LEADING_FILLER.sub("", value, count=1)
        if stripped == value:
            break
        value = stripped
    return _TRAILING_PUNCT.sub("", value).strip()


def find_wake(text: str, *, scan_limit: int = 10) -> WakeMatch:
    """Locate the wake phrase in a transcript and split off the question."""
    tokens = normalise(text).split()
    if not tokens:
        return WakeMatch(False)

    def build(start: int, used: int) -> WakeMatch:
        phrase = " ".join(tokens[start : start + used])
        rest = " ".join(tokens[start + used :])
        question = _clean_question(rest)
        return WakeMatch(
            matched=True,
            question=question,
            phrase=phrase,
            bare=not question,
        )

    # A name at the very start ("Paradise, what time is it?") is a call too —
    # unless the next word makes it a statement about the bot.
    matched, used = _name_at(tokens, 0)
    if matched and not (len(tokens) > used and tokens[used] in _NOT_A_CALL):
        return build(0, used)

    limit = min(len(tokens) - 1, scan_limit)
    for index in range(limit):
        if tokens[index] not in GREETINGS:
            continue
        matched, used = _name_at(tokens, index + 1)
        if matched:
            return build(index + 1, used)

    return WakeMatch(False)


__all__ = ["GREETINGS", "NAMES", "WakeMatch", "find_wake", "normalise"]
