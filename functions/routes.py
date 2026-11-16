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
SUMMARIZE = "summarize"
RECIPE = "recipe"      # offline recipe matching from ingredients
MEME = "meme"
GIF = "gif"
UTILITY = "utility"    # deterministic toolkit tools (payload2 = tool key)
REMIND = "remind"      # "remind me in 20 minutes to …"
POLL = "poll"          # reaction polls
INFO = "info"          # Discord-native info: whois / avatar / server
RESEARCH = "research"  # web-backed answers, news briefs, link summaries
WRITE = "write"        # rewriting, drafting, tone work (payload2 = style)
DEV = "dev"            # code/error/regex/git/sql/minecraft help
BUILD = "build"        # AI Build Mode (payload = prompt, payload2 = edit|new)
AI = "ai"
UNKNOWN = "unknown"

#: Every kind the assistant knows how to dispatch.
KINDS = frozenset(
    {
        HELP, COMMAND_HELP, PING, COINFLIP, CHOOSE, ROAST, CALCULATE, DEFINE,
        CONVERT, IMAGE, WEATHER, TIME, DATE, TRANSLATE, SEARCH, SUMMARIZE,
        RECIPE, MEME, GIF, UTILITY, REMIND, POLL, INFO, RESEARCH, WRITE, DEV,
        BUILD, AI, UNKNOWN,
    }
)

#: Deterministic tools: answered locally or by a dedicated provider, never
#: by asking a language model to invent the answer.
DETERMINISTIC_KINDS = frozenset(
    {CALCULATE, CONVERT, WEATHER, TIME, DATE, DEFINE, TRANSLATE, IMAGE,
     RECIPE, MEME, GIF, UTILITY, REMIND, POLL, INFO}
)

#: Route kinds where newlines carry structural meaning (code blocks, JSON,
#: stack traces, line-based list tools).
_MULTILINE_KINDS = frozenset({DEV, WRITE})
_MULTILINE_UTILITY_TOOLS = frozenset({"json", "sort", "dedupe"})

# Confidence bands used across the routing stack.
HIGH_CONFIDENCE = 0.75   # execute immediately
MEDIUM_CONFIDENCE = 0.45  # worth one tiny AI classification
MAX_PAYLOAD = 400
MAX_RICH_PAYLOAD = 1600


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
        self.payload2 = " ".join(str(self.payload2 or "").split())[:80]
        raw_payload = str(self.payload or "")
        preserve_lines = (
            self.kind in _MULTILINE_KINDS
            or (self.kind == UTILITY and self.payload2 in _MULTILINE_UTILITY_TOOLS)
        )
        if preserve_lines and "\n" in raw_payload:
            lines = [line.rstrip() for line in raw_payload.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
            self.payload = "\n".join(lines).strip()[:MAX_RICH_PAYLOAD]
        else:
            cap = MAX_RICH_PAYLOAD if preserve_lines else MAX_PAYLOAD
            self.payload = " ".join(raw_payload.split())[:cap]
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
    "AI", "BUILD", "CALCULATE", "CHOOSE", "COINFLIP", "COMMAND_HELP", "CONVERT", "DATE",
    "DEFINE", "DETERMINISTIC_KINDS", "GIF", "HELP", "HIGH_CONFIDENCE", "IMAGE",
    "DEV", "INFO", "KINDS", "MEDIUM_CONFIDENCE", "MEME", "PING", "POLL",
    "RECIPE", "REMIND", "RESEARCH", "Route", "SEARCH", "SUMMARIZE", "TIME",
    "TRANSLATE", "UNKNOWN", "UTILITY", "WEATHER", "WRITE", "clamp_confidence",
]
