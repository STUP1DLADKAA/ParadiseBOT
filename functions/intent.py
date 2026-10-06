"""Stage 2 of intent detection: a tiny AI classifier for ambiguous asks.

Stage 1 is :mod:`functions.router` — pure pattern matching, zero cost,
and it already handles the overwhelming majority of requests. This module
is the narrow fallback for the rest: when the router is genuinely unsure
("anything on the new pixel?"), Gemini is asked for a *label*, not an
answer.

Design constraints that keep this cheap and safe:

- One request, ~60 output tokens, temperature 0, strict JSON schema.
- Never asks for reasoning, and never forwards model prose to Discord.
- A bounded TTL cache means a repeated ambiguous phrase is classified once.
- Any failure (no key, timeout, bad JSON, unknown label) returns ``None``
  and the deterministic route stands. The classifier can only ever
  *improve* routing, never block it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

from functions.cache import TTLCache
from functions.helpers import ParadiseError
from functions.routes import (
    AI, CALCULATE, CONVERT, DATE, DEFINE, IMAGE, MEDIUM_CONFIDENCE, SEARCH,
    TIME, TRANSLATE, WEATHER, Route, clamp_confidence,
)

log = logging.getLogger(__name__)

# Only a route the assistant can actually execute is accepted back.
_INTENT_TO_KIND: dict[str, str] = {
    "calculate": CALCULATE,
    "convert": CONVERT,
    "weather": WEATHER,
    "time": TIME,
    "date": DATE,
    "image": IMAGE,
    "search": SEARCH,
    "define": DEFINE,
    "translate": TRANSLATE,
    "dictionary": DEFINE,
    "recipe": SEARCH,
    "chat": AI,
    "unknown": AI,
}

# Model output is data. Anything that looks like code, a command, a URL
# with credentials or an attempt to address the bot's internals is dropped
# rather than passed to a tool.
_UNSAFE_PAYLOAD = re.compile(
    r"(?:```|\$\(|`|<\?|</|\bimport\s|\bexec\s*\(|\beval\s*\(|\bos\.|"
    r"\bsubprocess\b|\bapi[_-]?key\b|\btoken\b|\benv(?:iron)?\b|"
    r"\bsystem\s*\(|\brm\s+-rf\b|\bsudo\b|\\x[0-9a-f]{2})",
    re.IGNORECASE,
)

# Below this, the deterministic router is admitting it guessed.
AMBIGUOUS_CONFIDENCE = MEDIUM_CONFIDENCE
_CLASSIFY_TIMEOUT = 10.0
_MAX_QUERY = 200

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)
_FENCE = re.compile(r"^\s*```(?:json)?|```\s*$", re.IGNORECASE)

_cache: TTLCache[str] = TTLCache(ttl=600.0, max_entries=128)


@dataclass(slots=True)
class IntentGuess:
    """The classifier's compact verdict."""

    intent: str
    query: str
    needs_web: bool = False
    needs_image: bool = False
    confidence: float = 0.55

    @property
    def kind(self) -> str:
        return _INTENT_TO_KIND.get(self.intent, AI)

    def to_route(self, fallback: Route) -> Route:
        """Turn the label into a Route the assistant can dispatch."""
        kind = self.kind
        if self.needs_image:
            kind = IMAGE
        elif kind == AI and self.needs_web:
            kind = SEARCH
        payload = self.query or fallback.payload
        return Route(
            kind=kind,
            payload=payload,
            confidence=self.confidence,
            fresh=self.needs_web or fallback.fresh,
            reason_complex=fallback.reason_complex,
            count=fallback.count,
        )


def should_classify(route: Route) -> bool:
    """True when the router was unsure enough to be worth one AI label.

    Deterministic tool routes are never sent to the model — that is the
    whole point of the hierarchy. Only an unplaced request (``ai`` /
    ``unknown``) at or below the medium-confidence band qualifies, so an
    obvious calculation or weather lookup can never cost an API call.
    """
    return route.kind in (AI, "unknown") and route.confidence <= AMBIGUOUS_CONFIDENCE


def parse_intent(raw: str) -> IntentGuess | None:
    """Parse the classifier's JSON. Tolerant of fences, strict about values."""
    text = _FENCE.sub("", str(raw or "")).strip()
    if not text:
        return None
    match = _JSON_OBJECT.search(text)
    if match is None:
        return None
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    intent = str(data.get("intent", "")).strip().casefold()
    if intent not in _INTENT_TO_KIND:
        return None
    query = " ".join(str(data.get("query", "") or "").split())[:_MAX_QUERY]
    if query and _UNSAFE_PAYLOAD.search(query):
        # Never hand an executable-looking string to a tool, whatever the
        # model claims it is.
        log.debug("Rejected a classifier payload that looked executable")
        return None
    # Confidence is advisory: clamp it, and never let the model claim more
    # certainty than a deterministic match would get.
    confidence = data.get("confidence")
    if confidence is None:
        score = 0.55
    else:
        score = min(0.7, max(0.3, clamp_confidence(confidence)))
    return IntentGuess(
        intent=intent,
        query=query,
        needs_web=_as_bool(data.get("needs_web")),
        needs_image=_as_bool(data.get("needs_image")),
        confidence=score,
    )


async def classify_request(text: str, ai) -> IntentGuess | None:
    """Ask the AI for a tiny label for *text*. Never raises, may return None."""
    query = " ".join(str(text or "").split()).strip()
    if not query or len(query) > 400 or ai is None:
        return None
    gemini = getattr(ai, "gemini", None)
    if gemini is None or not getattr(gemini, "available", False):
        return None

    key = query.casefold()
    cached = _cache.get(key)
    if cached is not None:
        return parse_intent(cached)

    try:
        raw = await asyncio.wait_for(gemini.classify(query), timeout=_CLASSIFY_TIMEOUT)
    except asyncio.CancelledError:
        raise
    except (asyncio.TimeoutError, ParadiseError):
        log.debug("Intent classification unavailable")
        return None
    except Exception:  # noqa: BLE001 - classification is strictly best-effort
        log.debug("Intent classification failed", exc_info=True)
        return None

    guess = parse_intent(raw)
    if guess is not None:
        # Only cache parseable labels; a malformed reply is worth retrying.
        _cache.set(key, raw[:300])
    return guess


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().casefold() in ("true", "yes", "1")
    return bool(value)


__all__ = [
    "AMBIGUOUS_CONFIDENCE",
    "IntentGuess",
    "classify_request",
    "parse_intent",
    "should_classify",
]
