"""Tolerant keyword routing — stage 1b of the ping pipeline.

:mod:`functions.router` matches well-formed sentences with a small set of
patterns. Real chat is rarely well formed, so when those patterns do not
fire confidently this module looks at the *normalised tokens* instead and
scores the evidence for each tool:

    "wthr delhi"        → weather(Delhi)
    "tokyo time rn"     → time(Tokyo)
    "5 kg pound"        → convert(5 kg → pound)
    "pic red panda"     → image("red panda")
    "meaning ambivalent"→ define("ambivalent")
    "latest mcpe"       → search("latest mcpe", fresh)

It is deliberately *not* a giant regex chain: each tool contributes a
small keyword/entity test, the strongest evidence wins, and every result
carries a confidence so the assistant can decide whether to execute it,
double-check it with one tiny AI label, or just chat.
"""

from __future__ import annotations

import re

from functions.calculator import extract_expression, looks_like_math
from functions.conversion import parse_conversion_query
from functions.conversion.currency import resolve_currency
from functions.conversion.units import TEMP_UNITS, resolve_unit
from functions.helpers import ParadiseError
from functions.normalize import NormalizedRequest, normalize_request
from functions.routes import (
    CALCULATE, CONVERT, DATE, DEFINE, IMAGE, SEARCH, TIME, TRANSLATE, WEATHER,
    Route,
)
from functions.translate import resolve_language

# ---------------------------------------------------------------------------
# Keyword tables (normalised, lowercase)
# ---------------------------------------------------------------------------

WEATHER_WORDS = frozenset(
    {"weather", "temperature", "forecast", "humidity", "raining", "rain",
     "rains", "snow", "snowing", "sunny", "cloudy", "windy", "hot", "cold",
     "warm", "chilly", "humid", "climate"}
)
# Words that only mean weather with another weather word present.
WEATHER_WEAK = frozenset({"hot", "cold", "warm", "chilly", "rain", "rains", "snow"})

TIME_WORDS = frozenset({"time", "timezone", "clock", "hour"})
DATE_WORDS = frozenset({"date", "day"})
DEFINE_WORDS = frozenset(
    {"define", "definition", "dictionary", "meaning", "means", "mean",
     "synonym", "synonyms", "antonym", "spelling", "spell"}
)
IMAGE_WORDS = frozenset(
    {"picture", "pictures", "image", "images", "photo", "photos",
     "photograph", "photographs", "wallpaper", "wallpapers", "screenshot",
     "screenshots", "pfp"}
)
TRANSLATE_WORDS = frozenset({"translate", "translation"})
SEARCH_WORDS = frozenset(
    {"search", "google", "lookup", "news", "headlines", "article", "articles"}
)
FRESH_WORDS = frozenset(
    {"latest", "today", "tonight", "now", "currently", "current", "recent",
     "recently", "news", "update", "updates", "updated", "new", "live",
     "breaking", "release", "released", "price", "score", "announced"}
)
MATH_WORDS = frozenset(
    {"calculate", "calculator", "sqrt", "squared", "cubed", "percent",
     "percentage", "sum", "product", "root", "divided", "divide",
     "multiplied", "multiply", "plus", "minus", "times"}
)
CONVERT_WORDS = frozenset({"convert", "conversion", "exchange", "rate"})

# Tokens that are scaffolding rather than content in any extracted subject.
_STOPWORDS = frozenset(
    {"the", "a", "an", "of", "in", "at", "for", "to", "into", "is", "it",
     "its", "what", "whats", "how", "when", "where", "which", "who", "do",
     "does", "did", "will", "would", "can", "could", "should", "i", "my",
     "me", "give", "show", "send", "find", "get", "fetch", "post", "share",
     "tell", "about", "like", "look", "looks", "looking", "right", "some",
     "any", "and", "or", "going", "gonna", "be", "s", "that", "this",
     "there", "here", "much", "many", "lot"}
)
_TIME_HINTS = frozenset({"now", "today", "tomorrow", "tonight", "yesterday", "currently"})
_NUMBER = re.compile(r"^[-+]?\d+(?:[.,]\d+)?$")
_COUNT_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "a": 1, "an": 1,
                "some": 3, "few": 3, "couple": 2}
_MAX_SUBJECT = 120
#: Conversion separators are never the unit themselves ("5 km in miles").
_SEPARATORS = frozenset({"to", "in", "into", "as", "=", "->", "=>", "is", "equals"})
_COMPARISON = re.compile(
    r"\b(?:vs\.?|versus|better|compare|comparison|difference between|worth it)\b"
)
_QUESTION_STARTERS = frozenset({"who", "when", "where", "which"})
# Lead-ins stripped from a translation request before the text to translate.
_TRANSLATE_LEAD = re.compile(
    r"^(?:translate|translation|how\s+do\s+(?:you|i)\s+s\w+|s\w{1,2}|write)\b",
)


def _subject(tokens: list[str], drop: frozenset[str]) -> str:
    """Whatever is left after removing tool words and scaffolding."""
    kept = [
        token
        for token in tokens
        if token not in drop and token not in _STOPWORDS and token not in _TIME_HINTS
    ]
    return " ".join(kept).strip(" ,.-")[:_MAX_SUBJECT]


def _is_unit_like(token: str) -> bool:
    bare = token.strip("°º∘.,")
    if not bare:
        return False
    return bool(
        resolve_unit(bare) or resolve_currency(bare) or bare.lower() in TEMP_UNITS
    )


# ---------------------------------------------------------------------------
# Individual detectors — each returns a Route or None
# ---------------------------------------------------------------------------


def _detect_math(request: NormalizedRequest) -> Route | None:
    text = request.text
    tokens = request.tokens
    if not any(char.isdigit() for char in text):
        return None
    has_word = bool(MATH_WORDS & request.token_set)
    expression = " ".join(
        token for token in tokens if token not in {"calculate", "calculator", "please"}
    ).strip(" ?!.")
    if not expression:
        return None
    # A conversion also has digits; never steal "100 usd to inr".
    if _detect_conversion(request) is not None:
        return None
    evaluable = extract_expression(expression)
    if evaluable and (looks_like_math(expression) or has_word):
        return Route(kind=CALCULATE, payload=evaluable, confidence=0.95 if looks_like_math(expression) else 0.85)
    if looks_like_math(expression):
        return Route(kind=CALCULATE, payload=expression, confidence=0.9)
    if has_word and evaluable:
        return Route(kind=CALCULATE, payload=evaluable, confidence=0.8)
    return None


def _detect_conversion(request: NormalizedRequest) -> Route | None:
    tokens = [token.strip(".,?!") for token in request.tokens if token.strip(".,?!")]
    if not tokens:
        return None
    # Find "<number> <unit-ish> … <unit-ish>" anywhere in the sentence.
    for index, token in enumerate(tokens):
        value_token = token
        attached_unit = ""
        if not _NUMBER.match(value_token):
            match = re.match(r"^([-+]?\d+(?:[.,]\d+)?)([a-z°$€£¥₹/]+)$", token)
            if match is None:
                continue
            value_token, attached_unit = match.group(1), match.group(2)
        tail = tokens[index + 1 :]
        head = tokens[:index]
        units: list[str] = []
        if attached_unit and _is_unit_like(attached_unit):
            units.append(attached_unit)
        for candidate in tail:
            if candidate in _SEPARATORS:
                continue
            if _is_unit_like(candidate):
                units.append(candidate)
            if len(units) >= 2:
                break
        if len(units) == 1:
            # "how many rupees is 50 dollars" — the target was named first.
            for candidate in reversed(head):
                if candidate in _SEPARATORS:
                    continue
                if _is_unit_like(candidate):
                    units.append(candidate)
                    break
        if len(units) < 2:
            continue
        probe = f"{value_token} {units[0]} {units[1]}"
        try:
            parse_conversion_query(probe)
        except (ParadiseError, ValueError):
            continue
        confidence = 0.9 if (CONVERT_WORDS & request.token_set) else 0.85
        return Route(kind=CONVERT, payload=probe, confidence=confidence)
    return None


def _detect_weather(request: NormalizedRequest) -> Route | None:
    present = WEATHER_WORDS & request.token_set
    if not present:
        return None
    strong = present - WEATHER_WEAK
    if not strong and not ({"is", "in"} & request.token_set):
        return None
    city = _subject(request.tokens, WEATHER_WORDS)
    confidence = 0.9 if strong else 0.7
    if not city:
        confidence = min(confidence, 0.75)
    return Route(kind=WEATHER, payload=city, confidence=confidence)


def _detect_time(request: NormalizedRequest) -> Route | None:
    tokens = request.token_set
    if DATE_WORDS & tokens and not (TIME_WORDS & tokens):
        return Route(kind=DATE, confidence=0.85)
    if not (TIME_WORDS & tokens):
        return None
    city = _subject(request.tokens, TIME_WORDS | DATE_WORDS)
    return Route(kind=TIME, payload=city, confidence=0.9 if city else 0.75)


def _detect_define(request: NormalizedRequest) -> Route | None:
    if not (DEFINE_WORDS & request.token_set):
        return None
    term = _subject(request.tokens, DEFINE_WORDS)
    if not term or len(term) > 48:
        return None
    # Dictionary lookups are single words or very short phrases.
    if len(term.split()) > 3:
        return None
    return Route(kind=DEFINE, payload=term, confidence=0.85)


def _detect_translate(request: NormalizedRequest) -> Route | None:
    lowered = request.text
    if not (TRANSLATE_WORDS & request.token_set) and " in " not in f" {lowered} ":
        return None
    match = re.search(r"\b(?:to|into|in)\s+([a-z]{2,20})\s*$", lowered)
    if match is None:
        return None
    language = resolve_language(match.group(1))
    if language is None:
        return None
    body = _TRANSLATE_LEAD.sub("", lowered[: match.start()].strip()).strip(" \"'")
    if not body:
        return None
    confidence = 0.9 if (TRANSLATE_WORDS & request.token_set) else 0.7
    return Route(kind=TRANSLATE, payload=body, payload2=match.group(1), confidence=confidence)


def _detect_image(request: NormalizedRequest) -> Route | None:
    tokens = request.tokens
    if not (IMAGE_WORDS & request.token_set):
        if not re.search(r"\blook(?:s)? like\b", request.text):
            return None
        subject = _subject(tokens, frozenset({"look", "looks", "like"}))
        if not subject:
            return None
        return Route(kind=IMAGE, payload=subject, confidence=0.8, count=1)

    count = 1
    for index, token in enumerate(tokens):
        if token in IMAGE_WORDS and index:
            previous = tokens[index - 1]
            if previous.isdigit():
                count = max(1, min(int(previous), 4))
            elif previous in _COUNT_WORDS:
                count = _COUNT_WORDS[previous]
            break
    drop = IMAGE_WORDS | frozenset(str(n) for n in range(1, 11)) | frozenset(_COUNT_WORDS)
    subject = _subject(tokens, drop)
    if not subject:
        return None
    return Route(kind=IMAGE, payload=subject, confidence=0.9, count=count)


def _detect_search(request: NormalizedRequest) -> Route | None:
    tokens = request.token_set
    fresh = bool(FRESH_WORDS & tokens)
    explicit = bool(SEARCH_WORDS & tokens)
    if not (explicit or fresh):
        return None
    query = _subject(request.tokens, frozenset({"search", "google", "lookup"}))
    if not query:
        query = request.text
    if not query:
        return None
    confidence = 0.85 if explicit else 0.7
    return Route(
        kind=SEARCH,
        payload=query,
        confidence=confidence,
        fresh=fresh,
        reason_complex=bool(_COMPARISON.search(request.text)),
    )


def _detect_factual(request: NormalizedRequest) -> Route | None:
    """Short factual questions the web answers better than a language model."""
    tokens = request.tokens
    if len(tokens) < 3 or tokens[0] not in _QUESTION_STARTERS:
        return None
    return Route(
        kind=SEARCH,
        payload=request.text,
        confidence=0.6,
        fresh=bool(FRESH_WORDS & request.token_set),
        reason_complex=bool(_COMPARISON.search(request.text)),
    )


# Tool-first order, exactly as specified: deterministic tools get first
# refusal and search/AI only see what is left.
_DETECTORS = (
    _detect_math,
    _detect_conversion,
    _detect_weather,
    _detect_time,
    _detect_define,
    _detect_translate,
    _detect_image,
    _detect_search,
    _detect_factual,
)


def keyword_route(request: NormalizedRequest | str) -> Route | None:
    """Best-effort tolerant route for a messy request, or ``None``."""
    normalized = (
        request if isinstance(request, NormalizedRequest) else normalize_request(request)
    )
    if not normalized.tokens:
        return None
    for detector in _DETECTORS:
        try:
            route = detector(normalized)
        except Exception:  # noqa: BLE001 - a detector never breaks routing
            continue
        if route is None:
            continue
        if route.payload or route.kind in (DATE, WEATHER, TIME):
            return route
    return None


__all__ = [
    "CONVERT_WORDS",
    "DEFINE_WORDS",
    "FRESH_WORDS",
    "IMAGE_WORDS",
    "MATH_WORDS",
    "SEARCH_WORDS",
    "TIME_WORDS",
    "WEATHER_WORDS",
    "keyword_route",
]
