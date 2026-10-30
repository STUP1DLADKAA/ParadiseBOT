"""Route kinds and the :class:`Route` value object.

Kept in its own module so the deterministic router
(:mod:`functions.router`), the tolerant keyword classifier
(:mod:`functions.keywords`) and the AI classifier
(:mod:`functions.intent`) can all speak the same language without
importing each other in a cycle.

A :class:`Route` is *data*, never an instruction: it carries the chosen
tool, a validated payload and a confidence score. Nothing in it is ever
executed, formatted into code, or trusted beyond being an argument to one
of Paradise's own functions.
"""

from __future__ import annotations

from dataclasses import dataclass

# -- route kinds ------------------------------------------------------------

HELP = "help"
COMMAND_HELP = "command_help"
PING = "ping"
COINFLIP = "coinflip"
CHOOSE = "choose"
ROAST = "roast"
CALCULATE = "calculate"
DEFINE = "define"
CONVERT = "convert"
IMAGE = "image"
WEATHER = "weather"
TIME = "time"
DATE = "date"
TRANSLATE = "translate"
SEARCH = "search"
AI = "ai"
UNKNOWN = "unknown"

#: Every kind the assistant knows how to dispatch.
KINDS = frozenset(
    {
        HELP, COMMAND_HELP, PING, COINFLIP, CHOOSE, ROAST, CALCULATE, DEFINE,
        CONVERT, IMAGE, WEATHER, TIME, DATE, TRANSLATE, SEARCH, AI, UNKNOWN,
    }
)

#: Deterministic tools: answered locally or by a dedicated provider, never
#: by asking a language model to invent the answer.
DETERMINISTIC_KINDS = frozenset(
    {CALCULATE, CONVERT, WEATHER, TIME, DATE, DEFINE, TRANSLATE, IMAGE}
)

# Confidence bands used across the routing stack.
HIGH_CONFIDENCE = 0.75   # execute immediately
MEDIUM_CONFIDENCE = 0.45  # worth one tiny AI classification
MAX_PAYLOAD = 400


def clamp_confidence(value) -> float:
    """Coerce anything into a sane 0.0–1.0 confidence."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:  # NaN
        return 0.0
    return max(0.0, min(1.0, number))


@dataclass(slots=True)
class Route:
    """Where a query should be answered, plus the cleaned argument."""

    kind: str
    payload: str = ""        # primary argument (word, query, city, text…)
    payload2: str = ""       # secondary argument (target language)
    confidence: float = 0.0
    fresh: bool = False      # the answer likely depends on recent information
    reason_complex: bool = False  # retrieved facts probably need AI reasoning
    count: int = 1           # how many results were asked for (images)

    def __post_init__(self) -> None:
        # Defensive normalisation: a Route may be built from AI output, so
        # every field is validated before anything can act on it.
        self.kind = str(self.kind or UNKNOWN)
        if self.kind not in KINDS:
            self.kind = UNKNOWN
        self.payload = " ".join(str(self.payload or "").split())[:MAX_PAYLOAD]
        self.payload2 = " ".join(str(self.payload2 or "").split())[:80]
        self.confidence = clamp_confidence(self.confidence)
        self.fresh = bool(self.fresh)
        self.reason_complex = bool(self.reason_complex)
        try:
            self.count = max(1, min(int(self.count), 4))
        except (TypeError, ValueError):
            self.count = 1

    @property
    def routed(self) -> bool:
        return self.kind not in (UNKNOWN,)

    @property
    def deterministic(self) -> bool:
        return self.kind in DETERMINISTIC_KINDS

    @property
    def confident(self) -> bool:
        return self.confidence >= HIGH_CONFIDENCE


__all__ = [
    "AI", "CALCULATE", "CHOOSE", "COINFLIP", "COMMAND_HELP", "CONVERT", "DATE",
    "DEFINE", "DETERMINISTIC_KINDS", "HELP", "HIGH_CONFIDENCE", "IMAGE",
    "KINDS", "MEDIUM_CONFIDENCE", "PING", "Route", "SEARCH", "TIME",
    "TRANSLATE", "UNKNOWN", "WEATHER", "clamp_confidence",
]
