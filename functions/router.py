"""Paradise's deterministic intent router — no AI involved.

This is the brain of the ping-to-use system. A plain sentence addressed
to the bot ("@Paradise convert 100 USD to INR") is classified into the
most specialised tool that can answer it:

    Native tools → deterministic processing → web search → Gemini AI fallback

Routing priority (first confident match wins):

    1. command/help   "what does /search do", "help"
    2. calculator     "25% of 800", "234 × 56", "what's 2+2"
    3. define         "define photosynthesis", "what does X mean"
    4. convert        "convert 100 USD to INR", "10 km to miles"
    5. image          "show me a picture of a red panda", "what does X look like"
    6. weather        "weather in Delhi", "how hot is it in Tokyo"
    7. time / date    "what time is it in Tokyo", "what day is it"
    8. translate      "translate hello into Japanese"
    9. web search    explicit search requests and freshness-sensitive facts
                      the native search feature can answer directly
    10. AI (Gemini)   reasoning/comparison questions and anything genuinely
                      ambiguous — always the last resort; it is never preceded
                      by an automatic search-and-rewrite loop

:func:`route_many` additionally handles requests that ask for two things
at once ("search the latest Minecraft update and convert 20 USD to INR")
by splitting conservatively and routing each half on its own.

The router never makes network calls: classification is pure pattern
matching, so it answers in microseconds. When it genuinely cannot tell
(low confidence), the assistant may ask Gemini for a tiny JSON
classification — see :mod:`functions.intent`.
"""

from __future__ import annotations

import re

from functions.calculator import extract_expression, looks_like_math
from functions.conversion import parse_conversion_query
from functions.conversion.currency import resolve_currency
from functions.conversion.units import TEMP_UNITS, resolve_unit
from functions.helpers import ParadiseError
from functions.keywords import keyword_route
from functions.normalize import normalize_request
from functions.routes import (
    AI, CALCULATE, CHOOSE, COINFLIP, COMMAND_HELP, CONVERT, DATE, DEFINE,
    DETERMINISTIC_KINDS, HELP, HIGH_CONFIDENCE, IMAGE, MEDIUM_CONFIDENCE,
    PING, Route, SEARCH, TIME, TRANSLATE, UNKNOWN, WEATHER,
)

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

_HELP = re.compile(
    r"^(?:help|commands|what can you do|what do you do|how do (?:i|you) use (?:you|this)|usage)\s*[?!.]*$",
    re.IGNORECASE,
)
_COMMAND_HELP = re.compile(
    r"^(?:what does|how does)\s+/(?P<command>[a-z][a-z0-9_-]*)\s+(?:do|work)\s*[?!.]*$",
    re.IGNORECASE,
)

_PING = re.compile(
    r"^(?:ping|are you (?:alive|online)|check your latency)\s*[?!.]*$",
    re.IGNORECASE,
)
_COINFLIP = re.compile(
    r"^(?:(?:flip|toss)\s+(?:a\s+)?coin|coin ?flip|heads or tails)\s*[?!.]*$",
    re.IGNORECASE,
)
_CHOOSE = re.compile(
    r"^(?:choose|pick|decide between|choose between)\s+(?P<options>.+?)\s*[?!.]*$",
    re.IGNORECASE,
)
_CALC_VERB = re.compile(
    r"^(?:calculate|compute|evaluate|solve|do the math on|math)\s+",
    re.IGNORECASE,
)

_DEFINE = re.compile(
    r"^(?:define|definition of|meaning of|the meaning of|what does|what's the meaning of|"
    r"what is the meaning of|explain the word|spell)\s+"
    r"(?P<term>[\w'\"“”\- ]{1,60}?)\s*(?:mean)?\s*[?!.]*$",
    re.IGNORECASE,
)
_DEFINE_WHAT_IS = re.compile(
    r"^what(?:'s| is|s)?\s+(?:a|an|the)\s+(?P<term>[\w'\-]{2,40})\s*[?!.]*$",
    re.IGNORECASE,
)
_DEFINE_WHAT_IS_ONE_WORD = re.compile(
    r"^what(?:'s| is|s)?\s+(?P<term>[a-z][a-z'\-]{2,39})\s*[?!.]*$",
    re.IGNORECASE,
)
_WHAT_DOES_MEAN = re.compile(
    r"^what does (?P<term>[\w'\"“”\- ]{1,40}?) mean\s*[?!.]*$", re.IGNORECASE
)

_CONVERT_VERB = re.compile(
    r"^(?:convert|change|turn|how (?:much|many) is|how (?:much|many) (?:are|is))\s+",
    re.IGNORECASE,
)
_CONVERT_QUESTION = re.compile(
    r"^(?:what(?:'s| is|s)?|how much is|how many)\s+", re.IGNORECASE
)

# -- image requests ---------------------------------------------------------
# "show me a picture of X", "find 3 images of X", "what does X look like".
# Deliberately requires an explicit picture word (or "look like") so nothing
# else in the router can be stolen by these patterns.
_IMAGE_NOUN = r"(?:pictures?|pics?|images?|photos?|photographs?|screenshots?|wallpapers?)"
_IMAGE_COUNT = r"(?:an?|one|a\s+few|some|couple\s+of|a\s+couple\s+of|\d{1,2})"
_IMAGE_VERB = re.compile(
    rf"^(?:(?:can\s+you\s+|could\s+you\s+|please\s+|pls\s+)?"
    rf"(?:show|send|give|find|get|fetch|post|share|grab|display|search\s+for|look\s+up)"
    rf"\s+(?:me\s+|us\s+)?)?"
    rf"(?:{_IMAGE_COUNT}\s+)?"
    rf"{_IMAGE_NOUN}"
    rf"\s+(?:of|for|showing|with)\s+(?P<subject>.{{2,120}}?)\s*[?!.]*$",
    re.IGNORECASE,
)
_IMAGE_LOOK_LIKE = re.compile(
    r"^(?:what(?:'s| is|s)?|how)\s+(?:does|do|did)\s+(?:an?\s+|the\s+)?"
    r"(?P<subject>.{2,120}?)\s+look\s+like\s*[?!.]*$",
    re.IGNORECASE,
)
# "picture of the Eiffel Tower" with no verb at all.
_IMAGE_BARE = re.compile(
    rf"^(?:{_IMAGE_COUNT}\s+)?{_IMAGE_NOUN}\s+(?:of|for)\s+(?P<subject>.{{2,120}}?)\s*[?!.]*$",
    re.IGNORECASE,
)
_IMAGE_COUNT_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4,
    "a few": 3, "some": 3, "couple of": 2, "a couple of": 2,
}
_IMAGE_COUNT_PREFIX = re.compile(
    rf"\b(?P<count>{_IMAGE_COUNT})\s+{_IMAGE_NOUN}\b", re.IGNORECASE
)
_IMAGE_SUBJECT_TRIM = re.compile(
    r"^(?:a|an|the|some|any)\s+|\s+(?:please|pls|thanks|thx)$", re.IGNORECASE
)

_WEATHER = re.compile(
    r"^(?:(?:what(?:'s| is|s)?|how(?:'s| is|s)?)\s+(?:the\s+)?)?"
    r"(?:weather|temperature|forecast)\s*"
    r"(?:(?:in|at|for|of|like in)\s+(?P<city>[\w' .\-]{2,50}))?\s*[?!.]*$",
    re.IGNORECASE,
)
_WEATHER_CITY_FIRST = re.compile(
    r"^(?P<city>[a-z][\w' .\-]{1,40}?)\s+weather\s*[?!.]*$", re.IGNORECASE
)
_WEATHER_HOW = re.compile(
    r"^how (?:hot|cold|warm|sunny|rainy|windy) is it(?:\s+in)?\s+(?P<city>[\w' .\-]{2,50})\s*[?!.]*$",
    re.IGNORECASE,
)
_WEATHER_BARE = re.compile(r"^(?:weather|forecast|temperature)\s*[?!.]*$", re.IGNORECASE)

_TIME_IN = re.compile(
    r"^(?:what(?:'s| is|s)?\s+)?(?:the\s+)?(?:(?:current|local)\s+)?"
    r"time(?:\s+is\s+it|\s+now)?\s+(?:in|at|for)\s+(?P<city>[\w' .\-]{2,50})\s*[?!.]*$",
    re.IGNORECASE,
)
_TIME_CITY_FIRST = re.compile(
    r"^(?P<city>[a-z][\w' .\-]{1,40}?)\s+time(?:\s+now)?\s*[?!.]*$", re.IGNORECASE
)
_TIME_BARE = re.compile(
    r"^(?:(?:what(?:'s| is|s)?\s+the\s+)?(?:current|local)\s+)?time(?:\s+now|(?:\s+is\s+it))?\s*[?!.]*$",
    re.IGNORECASE,
)
_DATE = re.compile(
    r"^(?:(?:what(?:'s| is|s)?|whats)\s+(?:the\s+)?(?:date|day)(?:\s+(?:is\s+it|today))?|"
    r"what day is it|today'?s date|date)\s*[?!.]*$",
    re.IGNORECASE,
)

_TRANSLATE = re.compile(
    r"^translate\s+(?P<text>.+?)\s+(?:to|into|in)\s+(?P<lang>[a-z]{2,20})\s*[?!.]*$",
    re.IGNORECASE,
)
_TRANSLATE_SAY = re.compile(
    r"^(?:how (?:do you|to) say|say)\s+(?P<text>.+?)\s+in\s+(?P<lang>[a-z]{2,20})\s*[?!.]*$",
    re.IGNORECASE,
)

_SEARCH_VERB = re.compile(
    r"^(?:(?:please\s+)?search(?:\s+the\s+(?:web|internet))?(?:\s+for)?|google|look\s?up|"
    r"find(?:\s+out)?(?:\s+about)?|tell\s+me\s+about|who(?:'s| is)\s+)\s+(?P<q>.+?)\s*[?!.]*$",
    re.IGNORECASE,
)

_FRESH_WORDS = re.compile(
    r"\b(?:latest|news|today|tonight|recent(?:ly)?|update[sd]?|now|current(?:ly)?|"
    r"this (?:week|month|year|season)|breaking|announced|release[sd]?|patch|"
    r"score|match|won|winner|live|20(?:2[5-9]|3\d))\b",
    re.IGNORECASE,
)

_FACTUAL_QUESTION = re.compile(
    r"^(?:who|what|when|where|which|how)\b.*", re.IGNORECASE | re.DOTALL
)

_REASONING_MARKERS = re.compile(
    r"\b(?:compare|comparison|versus|vs\.?|difference between|which (?:one |of )?(?:is|would) better|"
    r"pros and cons|explain why|why (?:is|are|did|does|do)\b.+\b(?:popular|successful|better|important|"
    r"influential|special|famous)|should i|would you recommend|your (?:opinion|take|thoughts)|"
    r"what do you think)\b",
    re.IGNORECASE,
)

_GREETING = re.compile(
    r"^(?:hey+|hi|hello|yo|hiya|howdy|sup|what's up|whats up|good (?:morning|evening|afternoon)|"
    r"thanks|thank you|thx|lol+|lmao|ok(?:ay)?|nice|cool|bye|good night|gn)\b[\s!.?]*$",
    re.IGNORECASE,
)


def _squash(text: str) -> str:
    return " ".join(str(text).split()).strip()


def _looks_fresh(text: str) -> bool:
    return bool(_FRESH_WORDS.search(text))


def _conversion_resolvable(value: float, from_raw: str, to_raw: str) -> bool:
    from_u, to_u = resolve_unit(from_raw), resolve_unit(to_raw)
    if from_u and to_u:
        return True
    if resolve_currency(from_raw) and resolve_currency(to_raw):
        return True
    from_c = from_raw.strip().lower().strip("°º∘")
    to_c = to_raw.strip().lower().strip("°º∘")
    return from_c in TEMP_UNITS and to_c in TEMP_UNITS


def _try_conversion(text: str) -> Route | None:
    """CONVERT when the text has a resolvable "<n> <unit> to <unit>" shape."""
    try:
        value, from_raw, to_raw = parse_conversion_query(text)
    except (ParadiseError, ValueError):
        return None
    if not _conversion_resolvable(value, from_raw, to_raw):
        return None
    return Route(kind=CONVERT, payload=text, confidence=0.95)


def _image_count(text: str) -> int:
    """How many pictures were asked for (1–4), from "3 pictures of…" etc."""
    match = _IMAGE_COUNT_PREFIX.search(text)
    if match is None:
        return 1
    raw = " ".join(match.group("count").split()).casefold()
    if raw.isdigit():
        return max(1, min(int(raw), 4))
    return _IMAGE_COUNT_WORDS.get(raw, 1)


def _clean_image_subject(subject: str) -> str:
    cleaned = _squash(subject).strip(" \"'“”")
    # Run twice so "the some X" style doubles collapse too.
    cleaned = _IMAGE_SUBJECT_TRIM.sub("", cleaned).strip()
    cleaned = _IMAGE_SUBJECT_TRIM.sub("", cleaned).strip()
    return cleaned


def _try_image(text: str) -> Route | None:
    """IMAGE when the text explicitly asks to see a picture of something."""
    for pattern in (_IMAGE_VERB, _IMAGE_BARE, _IMAGE_LOOK_LIKE):
        match = pattern.match(text)
        if match is None:
            continue
        subject = _clean_image_subject(match.group("subject"))
        if not subject or len(subject) > 120:
            continue
        return Route(
            kind=IMAGE,
            payload=subject,
            confidence=0.95,
            count=_image_count(text),
        )
    return None


def route_strict(text: str, *, allow_ai: bool = True) -> Route:
    """Pattern-match a *well formed* request. Pure and deterministic."""
    cleaned = _squash(text)
    if not cleaned:
        return Route(kind=HELP, confidence=1.0)
    if len(cleaned) > 400:
        cleaned = cleaned[:400]
    if _HELP.match(cleaned):
        return Route(kind=HELP, confidence=0.95)
    command_help = _COMMAND_HELP.match(cleaned)
    if command_help:
        return Route(
            kind=COMMAND_HELP,
            payload=command_help.group("command").casefold(),
            confidence=0.98,
        )
    # These are already native deterministic commands. Natural phrasing gets
    # routed to the same implementation instead of spending an AI call.
    if _PING.match(cleaned):
        return Route(kind=PING, confidence=0.98)
    if _COINFLIP.match(cleaned):
        return Route(kind=COINFLIP, confidence=0.98)
    choose_match = _CHOOSE.match(cleaned)
    if choose_match:
        return Route(kind=CHOOSE, payload=choose_match.group("options").strip(), confidence=0.95)

    # -- image requests go first: "find me a picture of X" must never be
    # grabbed by the generic "find …" web-search verb below.
    route = _try_image(cleaned)
    if route is not None:
        return route

    # -- 1/2/3. calculator, define, convert — the explicit-verb fast lane --
    calc_probe = _CALC_VERB.sub("", cleaned, count=1) if _CALC_VERB.match(cleaned) else ""
    if calc_probe:
        # Validate with the calculator itself so the payload is always
        # something it can actually evaluate.
        return Route(
            kind=CALCULATE,
            payload=extract_expression(calc_probe) or calc_probe,
            confidence=0.95,
        )
    if looks_like_math(cleaned):
        expression = extract_expression(cleaned)
        if expression:
            return Route(kind=CALCULATE, payload=expression, confidence=0.95)

    match = _WHAT_DOES_MEAN.match(cleaned) or _DEFINE.match(cleaned)
    if match:
        term = match.group("term").strip(" \"'“”")
        if term and len(term) <= 40:
            return Route(kind=DEFINE, payload=term, confidence=0.9)

    convert_text = _CONVERT_VERB.sub("", cleaned, count=1) if _CONVERT_VERB.match(cleaned) else ""
    if convert_text:
        route = _try_conversion(convert_text.rstrip("?!."))
        return route or Route(kind=CONVERT, payload=convert_text, confidence=0.6)
    route = _try_conversion(cleaned)
    if route is not None:
        return route
    # "what's 15 miles in kilometers?" — question-shaped conversion; only
    # counts when it actually resolves to a conversion (never blocks weather…).
    probe = _CONVERT_QUESTION.sub("", cleaned, count=1).rstrip("?!.").strip()
    if probe and probe != cleaned:
        route = _try_conversion(probe)
        if route is not None:
            route.payload = probe
            return route
    if cleaned.endswith(("?", "!", ".")):
        route = _try_conversion(cleaned.rstrip("?!."))
        if route is not None:
            return route

    # -- 4/5/6. weather, time/date, translate -------------------------------
    for pattern in (_WEATHER_HOW, _WEATHER_CITY_FIRST, _WEATHER):
        match = pattern.match(cleaned)
        if match:
            return Route(kind=WEATHER, payload=(match.group("city") or "").strip(), confidence=0.9)
    if _WEATHER_BARE.match(cleaned):
        return Route(kind=WEATHER, payload="", confidence=0.8)

    if _DATE.match(cleaned):
        return Route(kind=DATE, confidence=0.9)
    for pattern in (_TIME_IN, _TIME_CITY_FIRST):
        match = pattern.match(cleaned)
        if match:
            return Route(kind=TIME, payload=match.group("city").strip(), confidence=0.9)
    if _TIME_BARE.match(cleaned):
        return Route(kind=TIME, payload="", confidence=0.75)

    match = _TRANSLATE.match(cleaned) or _TRANSLATE_SAY.match(cleaned)
    if match:
        return Route(
            kind=TRANSLATE,
            payload=match.group("text").strip(),
            payload2=match.group("lang").strip(),
            confidence=0.9,
        )

    # -- implicit "what is X" definitions come after the tools that could
    # claim the same shape ("what's the date", "what's the weather in …").
    match = _DEFINE_WHAT_IS.match(cleaned) or _DEFINE_WHAT_IS_ONE_WORD.match(cleaned)
    if match:
        term = match.group("term").strip(" \"'“”")
        if term and len(term) <= 40:
            return Route(kind=DEFINE, payload=term, confidence=0.75)

    # -- 8. web search ---------------------------------------------------------
    reason_complex = bool(_REASONING_MARKERS.search(cleaned))
    match = _SEARCH_VERB.match(cleaned)
    if match:
        query = match.group("q").strip()
        if query:
            return Route(
                kind=SEARCH, payload=query, confidence=0.85,
                fresh=_looks_fresh(query), reason_complex=reason_complex,
            )
    if _looks_fresh(cleaned) and _FACTUAL_QUESTION.match(cleaned):
        return Route(
            kind=SEARCH, payload=cleaned, confidence=0.7,
            fresh=True, reason_complex=reason_complex,
        )
    if _looks_fresh(cleaned) and not _GREETING.match(cleaned) and len(cleaned.split()) >= 2:
        # Freshness-marked topics ("latest Minecraft news") are informational,
        # not conversation — the web answers them better than small-talk.
        return Route(
            kind=SEARCH, payload=cleaned, confidence=0.6,
            fresh=True, reason_complex=reason_complex,
        )
    if reason_complex and _FACTUAL_QUESTION.match(cleaned):
        # Reasoning over facts: retrieve first, then let the AI explain.
        return Route(kind=SEARCH, payload=cleaned, confidence=0.65, reason_complex=True)
    if _FACTUAL_QUESTION.match(cleaned) and "?" in cleaned:
        # Plain factual questions ("who won the election") are search-first.
        return Route(
            kind=SEARCH, payload=cleaned, confidence=0.6,
            fresh=_looks_fresh(cleaned), reason_complex=reason_complex,
        )

    # -- 9. AI fallback / conversational ---------------------------------------
    if _GREETING.match(cleaned):
        return Route(kind=AI, payload=cleaned, confidence=0.7)
    if allow_ai and reason_complex:
        return Route(kind=AI, payload=cleaned, confidence=0.6, reason_complex=True)
    if allow_ai and len(cleaned.split()) <= 24:
        # Short, clearly addressed to the bot but matched no tool: conversation.
        return Route(kind=AI, payload=cleaned, confidence=0.4)
    return Route(kind=SEARCH, payload=cleaned, confidence=0.3, fresh=_looks_fresh(cleaned))


# ---------------------------------------------------------------------------
# The hybrid router: strict patterns, then tolerant keywords
# ---------------------------------------------------------------------------

#: Above this, the strict patterns are trusted without a second opinion.
_TRUST_STRICT = 0.9


def route_query(text: str, *, allow_ai: bool = True) -> Route:
    """Classify a user query into the best tool.

    Three cheap, local stages — no network, no AI:

    1. strict patterns on the text as typed;
    2. strict patterns on the *normalised* text (slang, typos, Hinglish);
    3. tolerant keyword/entity scoring for everything still unclear.

    The most confident candidate wins, so a well-formed sentence costs one
    pass and a mangled one costs three cheap ones.
    """
    cleaned = _squash(text)
    best = route_strict(cleaned, allow_ai=allow_ai)
    if best.confidence >= _TRUST_STRICT:
        return best
    if not cleaned:
        return best

    normalized = normalize_request(cleaned)
    if normalized.text and normalized.text != cleaned.casefold():
        alternative = route_strict(normalized.text, allow_ai=allow_ai)
        if alternative.confidence > best.confidence:
            best = alternative
    if best.confidence >= _TRUST_STRICT:
        return best

    guess = keyword_route(normalized)
    if guess is not None and guess.confidence > best.confidence:
        best = guess
    return best


# ---------------------------------------------------------------------------
# Multi-tool requests ("search X and convert 20 USD to INR")
# ---------------------------------------------------------------------------

# Conservative split points only. Plain "and" is included but every split is
# re-validated below, so "rock and roll" or "difference between X and Y"
# stay single requests.
_COMPOUND_SPLIT = re.compile(
    r"(?:\s*;\s*|\s*,\s*(?:and|also|then)\s+|\s+and\s+(?:also\s+|then\s+)?|\s+&\s+)",
    re.IGNORECASE,
)
_MAX_COMPOUND_PARTS = 3

# Kinds that can take part in a combined answer at all.
_COMPOUND_KINDS = frozenset(
    {CALCULATE, CONVERT, WEATHER, TIME, DATE, IMAGE, TRANSLATE, SEARCH, DEFINE}
)
# At least one part must be an unambiguous deterministic tool, otherwise the
# "and" was almost certainly part of a single sentence.
_STRONG_KINDS = frozenset({CALCULATE, CONVERT, WEATHER, TIME, DATE, IMAGE, TRANSLATE})


def route_many(text: str) -> list[Route]:
    """Route *text*, splitting it only when it clearly asks for two things.

    Returns a single-element list for ordinary requests — the common case —
    and two or three routes for genuine multi-tool requests. Callers can
    execute the parts concurrently and combine the answers.
    """
    cleaned = _squash(text)
    primary = route_query(cleaned)
    if not cleaned or len(cleaned) > 400:
        return [primary]
    # Fast path: no join word at all, so there is nothing to split. Greedy
    # single-tool patterns ("weather in Delhi and time in Tokyo" matches the
    # weather regex whole) must still be re-checked, so confidence alone is
    # not enough to skip the split.
    if not _COMPOUND_SPLIT.search(cleaned):
        return [primary]

    segments = [part.strip(" ,;") for part in _COMPOUND_SPLIT.split(cleaned)]
    segments = [part for part in segments if len(part) >= 2]
    if not 2 <= len(segments) <= _MAX_COMPOUND_PARTS:
        return [primary]

    routes = [route_query(segment) for segment in segments]
    usable = [
        route
        for route in routes
        if route.kind in _COMPOUND_KINDS and route.confidence >= 0.6
    ]
    if len(usable) < 2:
        return [primary]
    if not any(route.kind in _STRONG_KINDS for route in usable):
        return [primary]
    return usable[:_MAX_COMPOUND_PARTS]


__all__ = [
    "AI", "CALCULATE", "CHOOSE", "COINFLIP", "COMMAND_HELP", "CONVERT", "DATE", "DEFINE",
    "DETERMINISTIC_KINDS", "HELP", "HIGH_CONFIDENCE", "IMAGE", "MEDIUM_CONFIDENCE",
    "PING", "Route", "SEARCH", "TIME", "TRANSLATE", "UNKNOWN", "WEATHER",
    "route_many", "route_query", "route_strict",
]
