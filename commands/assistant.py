"""The mention router: "@Paradise <anything>" — no commands to learn.

When someone addresses Paradise (mention or reply), this cog classifies
the request with the deterministic router (functions/router.py) and sends
it to the most specialised tool:

    calculator · define · convert · image search · weather · time/date
    · translate · meme · gif · recap · the free toolkit (dice, qr codes,
    passwords, ciphers, countdowns, text tools) · reminders · polls
    · whois/avatar/server info → the AI (openai-compatible first, with
    per-role model routing, gemini fallback)

Search-shaped requests are no longer answered by retrieving the web: the
request is forwarded straight to the AI and the AI replies, in character
(``AIService.answer_question``). No search provider is contacted.

Pipeline:

    mention → strip mention → empty? → deterministic route(s)
            → ambiguous? one tiny AI label (functions/intent.py)
            → run tool(s), concurrently when the request asks for several
            → Discord-safe reply

Everything here reuses the existing services owned by the other cogs, so
there is exactly one implementation of each capability. Failures degrade
gracefully: an unavailable provider falls back to a friendly message, and
a missing AI key never breaks the deterministic tool answers.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord.ext import commands

from commands.ai import MAX_SUMMARY_CHARS
from commands.banter import _GIF_EMPTY, _MEME_EMPTY, meme_embed, render_topic_meme
from commands.fun import build_ping_message, choose_option, flip_coin
from commands.system import HELP_BLURBS
from functions.startup import RETIRED_COMMANDS
from functions.calculator import calculate, extract_expression
from functions.conversion import parse_conversion_query
from functions.conversion.currency import resolve_currency
from functions.conversion.units import TEMP_UNITS, resolve_unit
from functions.helpers import (
    Colour, ParadiseError, parse_options, shorten, split_discord_text,
)
from functions.images import (
    NO_IMAGE_LINE, ImageSearchService, build_image_embeds,
)
from functions.intent import classify_request, should_classify, validate_refined_route
from functions.recipes import (
    RecipeFilters, near_misses, normalize_input, prettify, rank_recipes,
    suggest_substitution,
)
from functions.roasts import (
    build_context_roast,
    build_roast,
    clean_name,
    sanitize_intensity,
)
from functions.build import BuildOrchestrator
from functions.devhelp import DEV_MODES, run_devhelp
from functions.research import ResearchService, is_fetchable_url
from functions.router import (
    AI, BUILD, CALCULATE, CHOOSE, COINFLIP, COMMAND_HELP, CONVERT, DATE, DEFINE,
    DEV, GIF, HELP, IMAGE, INFO, MEME, PING, POLL, RECIPE, REMIND, RESEARCH,
    ROAST, SEARCH, SUMMARIZE, TIME, TRANSLATE, UNKNOWN, UTILITY, WEATHER, WRITE,
    Route, route_many,
)
from functions.toolkit import parse_duration, run_tool
from functions.time import build_time_embed, get_current_time_for_zone, resolve_timezone
from functions.translate import resolve_language, translate_text
from functions.writing import WRITE_MODES, run_writing, strip_deixis
from functions.weather import build_weather_embed, render_weather_card

log = logging.getLogger(__name__)

_USER_COOLDOWN = 5.0
#: Same member, same channel: keeps one person from machine-gunning the bot.
_CHANNEL_COOLDOWN = 2.0
#: A *different* member asking while another answer is still in flight waits
#: at most this long (usually for an AI slot) instead of being dropped —
#: parallel requests queue, they never disappear.
_AI_WAIT_TOLERANCE = 2.5
_HINT_COOLDOWN = 120.0
_FALLBACK_NOTE_COOLDOWN = 15 * 60

# How long the AI may think before the placeholder is replaced with an
# apology (the model itself gets its own, longer timeout).
_AI_TIMEOUT = 45.0

ASK_HINT = (
    "hey! just ask me things like:\n"
    "• `@Paradise define photosynthesis`\n"
    "• `@Paradise 25% of 800`  ·  `@Paradise 100 USD to INR`\n"
    "• `@Paradise weather in Delhi`  ·  `@Paradise time in Tokyo`\n"
    "• `@Paradise recipe with eggs, onion, bread`\n"
    "• `@Paradise show me a picture of a red panda`\n"
    "• `@Paradise latest Minecraft news`\n"
    "• `@Paradise translate hello into Japanese`\n"
    "• `@Paradise flip a coin`  ·  `@Paradise choose pizza or burgers`\n"
    "• `@Paradise roast me`  ·  `@Paradise meme`  ·  `@Paradise bruh gif`\n"
    "• `@Paradise summarize`  (recap of the recent chat)\n"
    "and the free toolkit: `@Paradise roll 2d20+3` · `password` · `qr code for <link>`\n"
    "· `rot13 <text>` · `days until christmas` · `remind me in 20 minutes to …`\n"
    "· `poll pizza or burgers` · `who is @user` · `avatar`\n"
    "no commands to remember — just say it normally."
)

# What to say instead, now that these commands are gone.
_RETIRED_EXAMPLES = {
    "search": "@Paradise search the latest Minecraft update",
    "weather": "@Paradise weather in Delhi",
    "convert": "@Paradise 100 USD to INR",
    "time": "@Paradise what time is it in Tokyo",
    "define": "@Paradise define ephemeral",
    "dictionary": "@Paradise define ephemeral",
    "recommend": "@Paradise recommend a movie like Interstellar",
}

SEARCH_FAIL_LINE = (
    "⚠️ the AI isn't answering right now. give it a minute and try again."
)

# "convert 50" — a value with no units at all.
_BARE_NUMBER = re.compile(r"^[-+]?\d+(?:[.,]\d+)?$")

# Follow-ups: "what about london", "in EUR", "and 250?", "+ 50", "reverse",
# "time there", etc. — tracked per (channel, user) with channel fallback.
_FOLLOWUP_KINDS = frozenset(
    {TIME, WEATHER, CONVERT, TRANSLATE, CALCULATE, DEFINE, IMAGE, RECIPE, RESEARCH}
)
_FOLLOWUP_WINDOW = 300.0  # five minutes of relevance
_TOOL_TIMEOUT = 20.0
_FOLLOWUP_SHAPE = re.compile(
    r"^(?:what|how)\s+about\s+(?P<rest>.+?)\s*[?!.]*$"
    r"|^(?:and|also|now)\s+(?:in\s+|to\s+|into\s+|for\s+|with\s+)?(?P<rest2>.+?)\s*[?!.]*$"
    r"|^(?:in|to|into)\s+(?P<rest3>[a-z$€£¥₹₩][a-z\s$€£¥₹₩°/]{0,40})\s*[?!.]*$",
    re.IGNORECASE,
)
_REVERSE_FOLLOWUP = re.compile(
    r"^(?:reverse(?:\s+(?:it|that))?|the\s+other\s+way(?:\s+around)?|"
    r"flip\s+(?:it|that)|vice\s+versa|swap(?:\s+units|\s+them)?)\s*[?!.]*$",
    re.IGNORECASE,
)
_MATH_CHAIN_OP = re.compile(
    r"^(?P<op>[+\-*/%^]|plus|minus|times|divided\s+by|multiplied\s+by)\s+"
    r"(?P<rhs>[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*[?!.]*$",
    re.IGNORECASE,
)
_MATH_ANS_TOKEN = re.compile(r"\b(?:ans|that|last\s+answer)\b", re.IGNORECASE)
_PLACE_DEIXIS = frozenset({"there", "over there", "that place", "that city", "same place"})
_SELF_TIME_DEIXIS = frozenset({"me", "myself", "my zone", "my timezone", "here"})
_REPLY_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_PRONOUN_TARGET = frozenset(
    {"this", "that", "it", "him", "her", "them", "this guy", "this message", "above"}
)


@dataclass(slots=True)
class _TurnContext:
    when: float
    route: Route
    user_id: int = 0
    location: str = ""
    numeric: float | None = None
    conversion: tuple[float, str, str] | None = None


@dataclass(slots=True)
class _ReplyContext:
    text: str
    author_name: str
    author_id: int
    author: object | None
    urls: tuple[str, ...]


def _format_number_token(value: float) -> str:
    if value == int(value) and abs(value) < 10**15:
        return str(int(value))
    return f"{value:.8g}"


def _city_from_iana(tz_name: str | None) -> str:
    """Derive a weather-queryable city from an IANA zone ('Asia/Kolkata' -> 'Kolkata')."""
    if not tz_name or "/" not in tz_name:
        return ""
    head, _, tail = tz_name.rpartition("/")
    if head.lower().startswith("etc") or not tail:
        return ""
    city = tail.replace("_", " ").strip()
    if not city or city.upper() in ("UTC", "GMT", "LOCAL"):
        return ""
    return city


class Assistant(commands.Cog):
    """Routes mentions/replies to the right tool; everything language-shaped
    (chat, questions, search asks) goes to the AI."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._user_last: dict[int, float] = {}
        # channel -> (when, who asked last): a different member is never
        # blocked by the previous one (parallel, not spam).
        self._channel_last: dict[int, tuple[float, int]] = {}
        self._hint_last: dict[int, float] = {}
        self._fallback_last: dict[int, float] = {}
        # The only image-search implementation in the bot. Key-less by
        # default; it caches short URLs, never image bytes.
        self.images = ImageSearchService(
            getattr(getattr(bot, "config", None), "image_search", None)
        )
        # Web research (DuckDuckGo-lite + Wikipedia, keyless). Off unless the
        # config says otherwise, so a bare construction never hits the
        # network. Every research failure falls back to a plain AI answer.
        self.research = ResearchService(
            getattr(getattr(bot, "config", None), "web_search", None)
        )
        # channel -> (when, last answered route): powers "what about <x>"
        # follow-ups; complemented by per-(channel, user) turn state so two
        # members talking at once don't overwrite each other's context.
        self._last_route: dict[int, tuple[float, Route]] = {}
        self._user_turns: dict[tuple[int, int], _TurnContext] = {}
        self._channel_turns: dict[int, _TurnContext] = {}
        self._staged_numeric: dict[tuple[int, int], float] = {}
        self._staged_conversion: dict[tuple[int, int], tuple[float, str, str]] = {}
        self._staged_location: dict[tuple[int, int], str] = {}
        log.info("Web research: %s", self.research.describe())

        # AI Build Mode orchestrator
        build_cfg = getattr(getattr(bot, "config", None), "build", None)
        self.build_orchestrator = BuildOrchestrator(bot, bot.ai, build_cfg) if build_cfg else None

    # ------------------------------------------------------------------
    # Entry point & conversational context (called from bot.on_message)
    # ------------------------------------------------------------------

    def _note_turn_detail(
        self,
        message: discord.Message,
        *,
        numeric: float | None = None,
        conversion: tuple[float, str, str] | None = None,
        location: str = "",
    ) -> None:
        key = (
            int(getattr(getattr(message, "channel", None), "id", 0) or 0),
            int(getattr(getattr(message, "author", None), "id", 0) or 0),
        )
        if numeric is not None:
            self._staged_numeric[key] = numeric
        if conversion is not None:
            self._staged_conversion[key] = conversion
        if location:
            self._staged_location[key] = location

    def _remember_route(
        self, channel_id: int, route: Route, *, user_id: int = 0
    ) -> None:
        """Keep recent follow-up state per (channel, user) and per channel."""
        now = time.monotonic()
        key = (int(channel_id or 0), int(user_id or 0))
        numeric = self._staged_numeric.pop(key, None)
        conversion = self._staged_conversion.pop(key, None)
        location = self._staged_location.pop(key, "")
        if not location and route.kind in (WEATHER, TIME) and route.payload:
            if route.payload.strip().casefold() not in (_PLACE_DEIXIS | _SELF_TIME_DEIXIS):
                location = route.payload.strip()
        if location == "":
            prev = self._user_turns.get(key) or self._channel_turns.get(channel_id)
            if prev is not None and now - prev.when <= _FOLLOWUP_WINDOW:
                location = prev.location

        turn = _TurnContext(
            when=now,
            route=route,
            user_id=user_id,
            location=location,
            numeric=numeric,
            conversion=conversion,
        )
        self._last_route[channel_id] = (now, route)
        self._channel_turns[channel_id] = turn
        if user_id:
            self._user_turns[key] = turn

        if len(self._last_route) > 128 or len(self._user_turns) > 256:
            cutoff = now - _FOLLOWUP_WINDOW
            self._last_route = {
                k: v for k, v in self._last_route.items() if v[0] > cutoff
            }
            self._channel_turns = {
                k: v for k, v in self._channel_turns.items() if v.when > cutoff
            }
            self._user_turns = {
                k: v for k, v in self._user_turns.items() if v.when > cutoff
            }

    def _lookup_turn(self, message: discord.Message) -> _TurnContext | None:
        """Prefer this author's recent turn in the channel, else channel last."""
        now = time.monotonic()
        channel_id = int(getattr(getattr(message, "channel", None), "id", 0) or 0)
        user_id = int(getattr(getattr(message, "author", None), "id", 0) or 0)

        # Honour direct _last_route inspection/overrides (e.g. expiry checks).
        legacy = self._last_route.get(channel_id)
        if legacy is not None:
            leg_when, leg_route = legacy
            ch_turn = self._channel_turns.get(channel_id)
            if ch_turn is None or ch_turn.when != leg_when or ch_turn.route is not leg_route:
                if now - leg_when > _FOLLOWUP_WINDOW:
                    return None
                return _TurnContext(when=leg_when, route=leg_route)

        if user_id:
            own = self._user_turns.get((channel_id, user_id))
            if own is not None and now - own.when <= _FOLLOWUP_WINDOW:
                return own
        shared = self._channel_turns.get(channel_id)
        if shared is not None and now - shared.when <= _FOLLOWUP_WINDOW:
            return shared
        return None

    @staticmethod
    def _extract_reply_context(message: discord.Message) -> _ReplyContext | None:
        """Inspect a Discord reply reference if the caller replied to a message."""
        ref = getattr(message, "reference", None)
        if ref is None:
            return None
        target = getattr(ref, "resolved", None) or getattr(ref, "cached_message", None)
        if target is None or isinstance(target, discord.DeletedReferencedMessage):
            return None
        raw_content = getattr(target, "content", None)
        if not isinstance(raw_content, str):
            raw_content = ""
        text = raw_content.strip()
        # Fall back to embed description/title when replying to a bot/link embed.
        if not text:
            embeds = getattr(target, "embeds", None)
            if isinstance(embeds, (list, tuple)) and embeds:
                first = embeds[0]
                desc = getattr(first, "description", None)
                title = getattr(first, "title", None)
                parts = [p.strip() for p in (title, desc) if isinstance(p, str) and p.strip()]
                text = "\n".join(parts)
        author = getattr(target, "author", None)
        author_name = ""
        author_id = 0
        if author is not None:
            raw_name = getattr(author, "display_name", None) or getattr(author, "name", None)
            if isinstance(raw_name, str):
                author_name = raw_name.strip()
            raw_id = getattr(author, "id", 0)
            if isinstance(raw_id, int):
                author_id = raw_id
        urls = tuple(
            cleaned
            for m in _REPLY_URL_RE.findall(text)
            if (cleaned := m.rstrip(".,?!;:>'\")")) and is_fetchable_url(cleaned)
        )
        if not text and not author_id and not urls:
            return None
        return _ReplyContext(
            text=text,
            author_name=author_name,
            author_id=author_id,
            author=author,
            urls=urls,
        )

    def _hydrate_from_reply(
        self, route: Route, raw_text: str, reply: _ReplyContext | None
    ) -> Route:
        """Fill missing/deictic tool inputs from the replied-to message."""
        if reply is None:
            return route
        kind = route.kind
        payload = (route.payload or "").strip()
        lowered = payload.casefold()

        if kind == TRANSLATE and (not payload or lowered in _PRONOUN_TARGET) and reply.text:
            return Route(
                kind=TRANSLATE,
                payload=reply.text[:380],
                payload2=route.payload2,
                confidence=route.confidence,
            )

        if kind == RESEARCH and route.payload2 == "link" and not payload and reply.urls:
            return Route(
                kind=RESEARCH,
                payload=reply.urls[0],
                payload2="link",
                confidence=route.confidence,
            )

        if kind == SUMMARIZE and reply.urls and re.search(
            r"\b(?:this|that|it|link|article|page)\b", raw_text, re.IGNORECASE
        ):
            return Route(
                kind=RESEARCH,
                payload=reply.urls[0],
                payload2="link",
                confidence=0.9,
            )
        if kind == SUMMARIZE and len(reply.text) >= 60 and re.match(
            r"^(?:please\s+)?(?:summar(?:ize|ise)|tl;?dr)(?:\s+(?:this|that|it))?\s*[?!.]*$",
            raw_text.strip(),
            re.IGNORECASE,
        ):
            return Route(
                kind=WRITE,
                payload=reply.text[:1400],
                payload2="shorten",
                confidence=0.9,
            )

        if kind == DEV and reply.text:
            if not payload or lowered in _PRONOUN_TARGET:
                return Route(
                    kind=DEV,
                    payload=reply.text[:1400],
                    payload2=route.payload2,
                    confidence=route.confidence,
                )

        if kind == WRITE and reply.text:
            stripped = strip_deixis(payload)
            if not stripped or stripped.casefold() in _PRONOUN_TARGET:
                return Route(
                    kind=WRITE,
                    payload=reply.text[:1400],
                    payload2=route.payload2,
                    confidence=route.confidence,
                )
            # "make this more polite" / "rewrite this to be formal" on a reply:
            # combine the style instruction with the replied-to text.
            if route.payload2 == "rewrite" and len(stripped.split()) <= 5 and ":" not in payload:
                return Route(
                    kind=WRITE,
                    payload=f"({stripped}): {reply.text[:1300]}",
                    payload2="rewrite",
                    confidence=route.confidence,
                )

        if kind == UTILITY and reply.text:
            json_mode_only = route.payload2 == "json" and lowered in (
                "format", "validate", "check", "verify", "prettify",
                "pretty print", "pretty-print", "beautify", "fix",
            )
            if json_mode_only:
                return Route(
                    kind=UTILITY,
                    payload=f"{lowered} {reply.text[:1200]}",
                    payload2="json",
                    confidence=route.confidence,
                )
            if (not payload or lowered in _PRONOUN_TARGET) and route.payload2 in (
                "json", "sort", "dedupe", "count", "base64", "url", "hash",
                "rot13", "reverse", "case", "mock", "aesthetic", "qr",
            ):
                return Route(
                    kind=UTILITY,
                    payload=reply.text[:1200],
                    payload2=route.payload2,
                    confidence=route.confidence,
                )

        if kind == DEFINE and lowered in _PRONOUN_TARGET and reply.text:
            word = reply.text.strip().strip("\"'.,?!")
            if 1 <= len(word.split()) <= 3 and len(word) <= 40:
                return Route(kind=DEFINE, payload=word, confidence=route.confidence)

        if kind == CALCULATE and not extract_expression(payload) and reply.text:
            expr = extract_expression(reply.text)
            if expr:
                return Route(kind=CALCULATE, payload=expr, confidence=route.confidence)

        return route

    def _build_convert_followup(
        self, turn: _TurnContext, rest: str
    ) -> Route | None:
        """Resolve 'in EUR', 'what about 250', or '50 EUR to JPY' after CONVERT."""
        # Full standalone conversion always wins directly.
        try:
            parse_conversion_query(rest)
            return Route(kind=CONVERT, payload=rest, confidence=0.85)
        except (ParadiseError, ValueError):
            pass

        conv = turn.conversion
        if conv is None:
            try:
                conv = parse_conversion_query(turn.route.payload)
            except (ParadiseError, ValueError):
                return None
        prev_val, prev_from, prev_to = conv
        token = rest.strip(" ?!.")

        # New amount with same units ("what about 250" / "and 1,500?").
        num_candidate = token.replace(",", "")
        if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", num_candidate):
            return Route(
                kind=CONVERT,
                payload=f"{num_candidate} {prev_from} to {prev_to}",
                confidence=0.85,
            )

        # New target unit/currency ("what about EUR" / "in miles" / "to fahrenheit").
        unit_clean = re.sub(r"^(?:in|to|into|as)\s+", "", token, flags=re.IGNORECASE).strip()
        if (
            resolve_unit(unit_clean)
            or resolve_currency(unit_clean)
            or unit_clean.lower().strip("°º∘") in TEMP_UNITS
        ):
            return Route(
                kind=CONVERT,
                payload=f"{_format_number_token(prev_val)} {prev_from} to {unit_clean}",
                confidence=0.85,
            )
        return None

    def _apply_followup(
        self, message: discord.Message, routes: list[Route], text: str
    ) -> list[Route]:
        """Resolve conversational follow-ups, pronoun locations & math chains."""
        cleaned = " ".join(str(text or "").split()).strip()
        turn = self._lookup_turn(message)
        if turn is None:
            return routes

        # 1. Cross-tool location deixis: "what time is it there?" / "weather there"
        if len(routes) == 1 and routes[0].kind in (WEATHER, TIME) and turn.location:
            if routes[0].payload.strip().casefold() in _PLACE_DEIXIS:
                return [
                    Route(
                        kind=routes[0].kind,
                        payload=turn.location,
                        confidence=0.9,
                    )
                ]

        # 2. Numeric chaining after CALCULATE or CONVERT: "+ 50", "* 1.18", "ans / 2"
        if turn.numeric is not None and len(routes) == 1:
            num_str = _format_number_token(turn.numeric)
            chain = _MATH_CHAIN_OP.match(cleaned)
            if chain is not None:
                op_map = {
                    "plus": "+", "minus": "-", "times": "*",
                    "multiplied by": "*", "divided by": "/",
                }
                raw_op = " ".join(chain.group("op").lower().split())
                op = op_map.get(raw_op, raw_op)
                rhs = chain.group("rhs").replace(",", "")
                return [Route(kind=CALCULATE, payload=f"{num_str} {op} {rhs}", confidence=0.9)]
            if _MATH_ANS_TOKEN.search(cleaned) and any(
                sym in cleaned for sym in ("+", "-", "*", "/", "%", "^")
            ):
                substituted = _MATH_ANS_TOKEN.sub(num_str, cleaned)
                expr = extract_expression(substituted)
                if expr:
                    return [Route(kind=CALCULATE, payload=expr, confidence=0.9)]

        # 3. "reverse" / "the other way" after a conversion.
        if (
            turn.route.kind == CONVERT
            and _REVERSE_FOLLOWUP.match(cleaned)
            and routes
            and routes[0].kind in (AI, UNKNOWN, SEARCH)
        ):
            conv = turn.conversion
            if conv is None:
                try:
                    conv = parse_conversion_query(turn.route.payload)
                except (ParadiseError, ValueError):
                    conv = None
            if conv is not None:
                val, from_u, to_u = conv
                return [
                    Route(
                        kind=CONVERT,
                        payload=f"{_format_number_token(val)} {to_u} to {from_u}",
                        confidence=0.88,
                    )
                ]

        # 4. Concrete tool asks are never overwritten by generic follow-up glue;
        # however "what about Tokyo?" gets a weak 0.6 factual-question route due
        # to the trailing "?", which IS a follow-up when a recent turn exists.
        is_what_about = cleaned.casefold().startswith(("what about ", "how about "))
        if routes and not (
            routes[0].kind in (AI, UNKNOWN)
            or (is_what_about and routes[0].kind in (RESEARCH, SEARCH) and routes[0].confidence <= 0.65)
        ):
            return routes

        match = _FOLLOWUP_SHAPE.match(cleaned)
        if match is None:
            return routes
        rest = (
            match.group("rest") or match.group("rest2") or match.group("rest3") or ""
        ).strip(" ?!.")
        if not rest:
            return routes

        previous = turn.route
        if previous.kind == CONVERT:
            built = self._build_convert_followup(turn, rest)
            if built is not None:
                log.info("Assistant convert follow-up: %s", built.payload)
                return [built]
            return routes

        if previous.kind == TRANSLATE:
            # "in French" / "what about Spanish" swaps the TARGET language while
            # keeping the original phrase; a non-language phrase translates into
            # the same target language.
            if resolve_language(rest) and previous.payload:
                followup = Route(
                    kind=TRANSLATE,
                    payload=previous.payload,
                    payload2=rest,
                    confidence=0.85,
                )
            else:
                followup = Route(
                    kind=TRANSLATE,
                    payload=rest,
                    payload2=previous.payload2,
                    confidence=0.8,
                )
            log.info("Assistant translate follow-up → %s", followup.payload2)
            return [followup]

        if previous.kind == RECIPE:
            merged = f"{previous.payload}, {rest}" if previous.payload else rest
            return [
                Route(
                    kind=RECIPE,
                    payload=merged,
                    payload2=previous.payload2,
                    confidence=0.82,
                )
            ]

        if previous.kind == CALCULATE:
            expr = extract_expression(rest)
            if not expr:
                return routes
            return [Route(kind=CALCULATE, payload=expr, confidence=0.82)]

        followup = Route(
            kind=previous.kind,
            payload=rest,
            payload2=previous.payload2 if previous.kind != RESEARCH else "answer",
            confidence=0.75,
            fresh=previous.fresh,
        )
        log.info("Assistant follow-up: %s → %s", previous.kind, rest)
        return [followup]

    async def handle_message(self, message: discord.Message) -> bool:
        """True when this message was addressed to Paradise and handled."""
        if message.author.bot or message.guild is None:
            return False
        if not self.bot.ai.should_reply_to(message):
            return False

        channel = message.channel
        permissions = channel.permissions_for(message.guild.me)
        if not permissions.send_messages:
            return False

        text = self.bot.ai.extract_question(message)
        if not text:
            # An empty mention is conversation, not a request: no external
            # API is ever called for it.
            return await self._maybe_hint(message)

        # Stage 1: deterministic routing, which also splits genuine
        # multi-tool requests ("weather in Delhi and time in Tokyo").
        routes = route_many(text)
        if not self._cooled(message):
            return True  # addressed correctly but rate-limited: stay quiet

        # A bare "what about <x>", "+ 50", "reverse", or "weather there"
        # continues the asker's (or channel's) recent tool turn.
        routes = self._apply_followup(message, routes, text)

        reply_ctx = self._extract_reply_context(message)

        try:
            if len(routes) > 1:
                hydrated = [self._hydrate_from_reply(r, text, reply_ctx) for r in routes]
                handled = await self._dispatch_many(message, hydrated)
                if handled:
                    for item in hydrated:
                        if item.kind in _FOLLOWUP_KINDS:
                            self._remember_route(
                                message.channel.id,
                                item,
                                user_id=int(getattr(message.author, "id", 0) or 0),
                            )
                return handled
            route = self._hydrate_from_reply(routes[0], text, reply_ctx)
            route = await self._refine(route, text, reply=reply_ctx)
            route = self._hydrate_from_reply(route, text, reply_ctx)
            # Observability: the tool chosen and how long it took — never
            # the message content, the user, or any credential.
            log.info("Assistant → %s (confidence %.2f)", route.kind, route.confidence)
            started = time.monotonic()
            handled = await self._dispatch(message, route, text, reply=reply_ctx)
            turn_key = (
                int(getattr(message.channel, "id", 0) or 0),
                int(getattr(message.author, "id", 0) or 0),
            )
            if handled and route.kind in _FOLLOWUP_KINDS:
                self._remember_route(
                    message.channel.id,
                    route,
                    user_id=turn_key[1],
                )
            else:
                self._staged_numeric.pop(turn_key, None)
                self._staged_conversion.pop(turn_key, None)
                self._staged_location.pop(turn_key, None)
            log.info(
                "Assistant %s %s in %.2fs",
                route.kind,
                "answered" if handled else "declined",
                time.monotonic() - started,
            )
            return handled
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the assistant never raises in chat
            log.exception("Assistant dispatch failed (%s)", routes[0].kind)
            await self._quiet_reply(
                message,
                "that broke on my end — it's been logged. try again in a moment.",
            )
            return True

    # ------------------------------------------------------------------
    # Stage 2: one tiny AI label, only when stage 1 was unsure
    # ------------------------------------------------------------------

    async def _refine(
        self, route: Route, text: str, *, reply: _ReplyContext | None = None
    ) -> Route:
        """Upgrade a low-confidence route with a compact validated AI label."""
        if not should_classify(route):
            return route
        context_hint = reply.text[:140] if (reply and reply.text) else ""
        guess = (
            await classify_request(
                text, getattr(self.bot, "ai", None), context=context_hint
            )
            if context_hint
            else await classify_request(text, getattr(self.bot, "ai", None))
        )
        if guess is None:
            return route
        refined = validate_refined_route(guess.to_route(route), route)
        if refined.kind == route.kind and refined.payload == route.payload:
            return route
        log.info("Intent refined by classifier: %s → %s", route.kind, refined.kind)
        return refined

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def _dispatch(
        self,
        message: discord.Message,
        route: Route,
        text: str,
        *,
        reply: _ReplyContext | None = None,
    ) -> bool:
        kind = route.kind

        if kind == HELP:
            await self._quiet_reply(message, ASK_HINT)
            return True
        if kind == COMMAND_HELP:
            return await self._command_help(message, route)
        if kind == PING:
            await self._quiet_reply(
                message,
                build_ping_message(getattr(self.bot, "latency", None)),
            )
            return True
        if kind == COINFLIP:
            await self._quiet_reply(message, f"🪙 **{flip_coin()}**")
            return True
        if kind == CHOOSE:
            return await self._choose(message, route)
        if kind == ROAST:
            return await self._roast(message, route, reply=reply)
        if kind == CALCULATE:
            return await self._calculate(message, route)
        if kind == DEFINE:
            return await self._define(message, route)
        if kind == CONVERT:
            return await self._convert(message, route)
        if kind == IMAGE:
            return await self._image(message, route)
        if kind == WEATHER:
            return await self._weather(message, route)
        if kind == TIME:
            return await self._time(message, route)
        if kind == DATE:
            return await self._date(message)
        if kind == TRANSLATE:
            return await self._translate(message, route)
        if kind == RECIPE:
            return await self._recipe(message, route)
        if kind == SUMMARIZE:
            return await self._summarize(message, route)
        if kind == MEME:
            return await self._meme(message, route)
        if kind == GIF:
            return await self._gif(message, route)
        if kind == UTILITY:
            return await self._utility(message, route)
        if kind == REMIND:
            return await self._remind(message, route)
        if kind == POLL:
            return await self._poll(message, route)
        if kind == INFO:
            return await self._info(message, route, reply=reply)
        if kind == RESEARCH:
            return await self._research(message, route)
        if kind == WRITE:
            return await self._writing(message, route)
        if kind == DEV:
            return await self._dev(message, route)
        if kind == BUILD:
            return await self._build(message, route, text)
        if kind == SEARCH:
            return await self._search(message, route)
        if kind in (AI, UNKNOWN):
            return await self._ai(message, text)
        return await self._ai(message, text)

    async def _roast(
        self,
        message: discord.Message,
        route: Route,
        *,
        reply: _ReplyContext | None = None,
    ) -> bool:
        """``@Paradise roast <someone>`` — a jab aimed at the right person,
        about what they actually just said when Paradise knows it."""
        target = self._roast_target(message, route.payload or "", reply=reply)
        engine = getattr(self.bot, "personality", None)
        intensity = sanitize_intensity(getattr(engine, "roast_intensity", "spicy"))
        recent = getattr(engine, "_recent_roasts", None)
        context = getattr(target, "reply_quote", "") or self._target_last_words(message, target)
        line = await self._tease_with_ai(message, target, context)
        if line is None:
            if context:
                line = build_context_roast(target.name, context, recent=recent)
            line = line or build_roast(
                target.name, intensity=intensity, recent=recent
            )
        if target.self_roast:
            line = f"roasting myself, since you asked: {line}"
        await self._quiet_reply(message, line)
        return True

    def _target_last_words(self, message: discord.Message, target) -> str:
        """What the roast target last said — the roast should be about that.

        Never the triggering request itself (that's "roast me", not content),
        and never a hard dependency: any missing piece means "no context",
        which falls back to the classic roast book.
        """
        if getattr(target, "self_roast", False):
            return ""
        words = getattr(getattr(self.bot, "personality", None), "_last_words", None)
        target_id = int(getattr(target, "id", 0) or 0)
        if isinstance(words, dict) and target_id:
            candidate = words.get(target_id)
            if candidate:
                return str(candidate)
        ai = getattr(self.bot, "ai", None)
        last_words = getattr(ai, "last_words", None)
        if callable(last_words):
            try:
                return str(last_words(message.channel.id, target.name) or "")
            except Exception:  # noqa: BLE001 - context is a bonus, never a need
                log.debug("Looking up last words failed", exc_info=True)
        return ""

    async def _tease_with_ai(self, message: discord.Message, target, context: str) -> str | None:
        """A context-specific AI tease of the target, or ``None``.

        The AI tier of the roast pipeline: what the target actually said,
        made fun of in Paradise's own voice. Any failure falls straight
        through to the roast book — a roast should never 500.
        """
        ai = getattr(self.bot, "ai", None)
        if ai is None or not getattr(ai, "available", False):
            return None
        tease = getattr(ai, "tease", None)
        if not callable(tease):
            return None
        try:
            text = await asyncio.wait_for(
                tease(
                    target.name,
                    context or "",
                    target_id=int(getattr(target, "id", 0) or 0),
                ),
                timeout=_AI_TIMEOUT,
            )
        except Exception:  # noqa: BLE001 - the roast book is always ready
            log.debug("AI tease failed; using the roast book", exc_info=True)
            return None
        text = str(text or "").strip().strip('"')
        return text or None

    @staticmethod
    def _roast_target(
        message: discord.Message,
        payload: str,
        *,
        reply: _ReplyContext | None = None,
    ):
        """Who the roast is for: a mentioned member, reply author, asker, or plain name."""
        text = (payload or "").strip()
        mention = re.search(r"<@!?(\d+)>", text)
        if mention is not None and message.guild is not None:
            member = message.guild.get_member(int(mention.group(1)))
            if member is not None:
                name = (
                    getattr(member, "display_name", None)
                    or getattr(member, "name", None)
                    or "them"
                )
                return SimpleNamespace(
                    name=clean_name(name, fallback="them"),
                    self_roast=False,
                    id=int(getattr(member, "id", 0) or 0),
                    reply_quote="",
                )
        lowered = text.lower()
        if reply is not None and lowered in _PRONOUN_TARGET and reply.author_name:
            return SimpleNamespace(
                name=clean_name(reply.author_name, fallback="them"),
                self_roast=(reply.author_id == int(getattr(message.author, "id", 0) or 0)),
                id=reply.author_id,
                reply_quote=reply.text[:220],
            )
        if not text or lowered == "me" or lowered.startswith("me ") or lowered == "myself":
            name = (
                getattr(message.author, "display_name", None)
                or getattr(message.author, "name", None)
                or "you"
            )
            return SimpleNamespace(
                name=clean_name(name, fallback="you"),
                self_roast=True,
                id=int(getattr(message.author, "id", 0) or 0),
                reply_quote="",
            )
        return SimpleNamespace(
            name=clean_name(text, fallback="them"),
            self_roast=False,
            id=0,
            reply_quote="",
        )

    # ------------------------------------------------------------------
    # Multi-tool requests ("weather in Delhi and time in Tokyo")
    # ------------------------------------------------------------------

    async def _dispatch_many(self, message: discord.Message, routes: list[Route]) -> bool:
        """Run each part concurrently and answer with one combined message."""
        log.info(
            "Assistant combined request: %s", ", ".join(route.kind for route in routes)
        )
        started = time.monotonic()
        # ``routes`` is capped at three by the router, so this is a bounded
        # fan-out, not an unbounded promise explosion. Kinds that naturally
        # send their own message (a reminder, a poll, an info card) are
        # dispatched in full; everything else is compacted into one reply.
        asker = getattr(message.author, "display_name", None) or "someone"
        asker_id = int(getattr(message.author, "id", 0) or 0)
        own_kinds = (REMIND, POLL, INFO)
        compact_routes = [route for route in routes if route.kind not in own_kinds]
        own_routes = [route for route in routes if route.kind in own_kinds]
        outcomes = await asyncio.gather(
            *(
                self._compact(route, asker=asker, asker_id=asker_id, guild=message.guild)
                for route in compact_routes
            ),
            *(self._dispatch(message, route, "") for route in own_routes),
            return_exceptions=True,
        )

        lines: list[str] = []
        embeds: list[discord.Embed] = []
        handled_own = 0
        for route, outcome in zip(compact_routes + own_routes, outcomes, strict=False):
            if isinstance(outcome, BaseException):
                if isinstance(outcome, asyncio.CancelledError):
                    raise outcome
                log.warning("Combined %s part failed", route.kind, exc_info=outcome)
                lines.append(f"• couldn't finish the *{route.kind}* part of that.")
                continue
            if route.kind in own_kinds:
                if outcome:
                    handled_own += 1
                continue
            text, part_embeds = outcome
            if text:
                lines.append(text)
            embeds.extend(part_embeds)

        log.info(
            "Assistant combined request done in %.2fs (%d parts)",
            time.monotonic() - started,
            len(routes),
        )
        if not lines and not embeds:
            if handled_own:
                return True  # the separate messages already answered it
            await self._quiet_reply(message, SEARCH_FAIL_LINE)
            return True
        extras: dict = {}
        if embeds:
            extras["embeds"] = embeds[:4]
        await self._quiet_reply(message, "\n".join(lines), **extras)
        return True

    def _user_timezone(self, user_id: int) -> str | None:
        utility = self.bot.get_cog("Utility")
        if utility is None or not user_id:
            return None
        db = getattr(utility, "db", None)
        getter = getattr(db, "get_user_timezone", None)
        if not callable(getter):
            return None
        try:
            tz_name = getter(int(user_id))
        except Exception:  # noqa: BLE001
            return None
        return str(tz_name).strip() if isinstance(tz_name, str) and tz_name.strip() else None

    async def _compact(
        self,
        route: Route,
        *,
        asker: str = "someone",
        asker_id: int = 0,
        guild: discord.Guild | None = None,
    ) -> tuple[str, list[discord.Embed]]:
        """One short, combinable answer for a single route. Never raises."""
        kind = route.kind
        try:
            if kind == CALCULATE:
                return f"🧮 {calculate(route.payload).display}", []
            if kind == CONVERT:
                return await self._compact_convert(route)
            if kind == IMAGE:
                return await self._compact_image(route)
            if kind == WEATHER:
                return await self._compact_weather(route)
            if kind == TIME:
                return self._compact_time(route, asker=asker, asker_id=asker_id, guild=guild), []
            if kind == DATE:
                tz_name = self._user_timezone(asker_id)
                try:
                    zone = ZoneInfo(tz_name) if tz_name else ZoneInfo("UTC")
                except (ZoneInfoNotFoundError, KeyError, ValueError):
                    tz_name, zone = None, ZoneInfo("UTC")
                now = datetime.now(zone)
                return (
                    f"📅 today is **{now.strftime('%A, %d %B %Y')}** ({tz_name or 'UTC'})",
                    [],
                )
            if kind == TRANSLATE:
                return await self._compact_translate(route)
            if kind == RECIPE:
                return self._compact_recipe(route), []
            if kind in (MEME, GIF):
                return await self._compact_media(route)
            if kind == UTILITY:
                result = run_tool(route.payload2, route.payload)
                return result.text, []
            if kind in (REMIND, POLL, INFO):
                return (
                    f"• the *{kind}* part needs its own message — ask it on its own.",
                    [],
                )
            if kind == SUMMARIZE:
                return (
                    "📋 the recap needs its own message — just say "
                    "**@Paradise summarize** on its own.",
                    [],
                )
            if kind == RESEARCH:
                return await self._compact_research(route, asker=asker)
            if kind == DEFINE:
                return await self._compact_search(
                    Route(kind=SEARCH, payload=f"what is {route.payload}"),
                    asker=asker,
                )
            return await self._compact_search(route, asker=asker)
        except ParadiseError as error:
            return f"• {error}", []
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad part never kills the reply
            log.warning("Compact %s failed", kind, exc_info=True)
            return f"• couldn't finish the *{kind}* part of that.", []

    async def _compact_research(
        self, route: Route, *, asker: str = "someone"
    ) -> tuple[str, list[discord.Embed]]:
        """A combined reply's web part: top hits, no model call."""
        service = self.research
        subject = (route.payload or "").strip()
        if not service.enabled:
            # Web off (or a bare test bot): the model answers this part,
            # exactly like a standalone research ask would.
            return await self._compact_search(
                Route(kind=SEARCH, payload=subject, confidence=route.confidence),
                asker=asker,
            )
        if not subject:
            return ("🔍 search what, exactly?", [])
        try:
            results = await asyncio.wait_for(
                service.search(subject), timeout=service.timeout + 5
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad part never kills the reply
            log.warning("Compact research failed", exc_info=True)
            results = []
        if not results:
            return ("🔍 the web side came up empty — ask it on its own and I'll think instead.", [])
        lines = [f"🔍 **{subject[:60]}** — top hits:"]
        for index, result in enumerate(results[:3], 1):
            title = (result.title or result.url)[:60]
            lines.append(f"{index}. [{title}]({result.url})")
        return ("\n".join(lines), [])

    async def _compact_convert(self, route: Route) -> tuple[str, list[discord.Embed]]:
        utility = self.bot.get_cog("Utility")
        if utility is None:
            return "", []
        outcome = await asyncio.wait_for(
            utility.conversion_engine.convert_query(route.payload), timeout=20
        )
        return f"🔄 {outcome.display_text}", []

    async def _compact_weather(self, route: Route) -> tuple[str, list[discord.Embed]]:
        utility = self.bot.get_cog("Utility")
        if utility is None or not route.payload:
            return "", []
        try:
            data = await asyncio.wait_for(
                utility.weather_service.get_weather(route.payload), timeout=20
            )
        except (ParadiseError, asyncio.TimeoutError):
            # Live data could not be retrieved — say so; never guess it.
            return "🌦️ I couldn't retrieve live weather right now.", []
        details = []
        if data.humidity is not None:
            details.append(f"humidity {data.humidity}%")
        if data.wind_speed_kmh is not None:
            wind = f"wind {data.wind_speed_kmh:.0f} km/h"
            if data.wind_direction:
                wind += f" {data.wind_direction}"
            details.append(wind)
        temperature = f"{data.temp_c:.0f}°C" if data.temp_c is not None else "temperature unavailable"
        suffix = " · " + " · ".join(details) if details else ""
        icon = f"{data.condition_emoji} " if data.condition_emoji else ""
        return (
            f"{icon}**{data.location.name}** — "
            f"{temperature}, {data.condition.lower()}{suffix}",
            [],
        )

    def _compact_time(
        self,
        route: Route,
        *,
        asker: str = "someone",
        asker_id: int = 0,
        guild: discord.Guild | None = None,
    ) -> str:
        raw = (route.payload or "").strip()
        if not raw or raw.casefold() in _SELF_TIME_DEIXIS:
            saved = self._user_timezone(asker_id)
            tz_name = saved or "UTC"
            label = asker if saved else "UTC"
        else:
            mention_match = re.fullmatch(r"<@!?(\d+)>", raw)
            if mention_match is not None:
                target_id = int(mention_match.group(1))
                saved = self._user_timezone(target_id)
                member = guild.get_member(target_id) if guild is not None else None
                who = getattr(member, "display_name", None) or "that member"
                if not saved:
                    return f"🕒 **{who}** hasn't set a timezone yet (`/settimezone`)."
                tz_name, label = saved, who
            else:
                tz_name = resolve_timezone(raw)
                label = raw.title()
        if tz_name is None:
            return f"🕒 I don't recognize the place **{shorten(raw, 40)}**."
        info = get_current_time_for_zone(tz_name, label=label or "UTC")
        return (
            f"🕒 **{info.location_or_target}** — {info.formatted_12h} "
            f"({info.abbreviation})"
        )

    def _compact_recipe(self, route: Route) -> str:
        items = normalize_input(route.payload)
        if not items:
            return "🍳 tell me what ingredients you have."
        filters = self._parse_recipe_filters(route.payload2)
        matches = rank_recipes(items, filters=filters, limit=3)
        if not matches:
            matches = near_misses(items, filters=filters, limit=2)
        if not matches:
            return f"🍳 no recipe match for **{shorten(route.payload, 50)}**."
        picks = ", ".join(
            f"**{m.recipe.name}** ({m.recipe.time_minutes}m)"
            if m.recipe.time_minutes
            else f"**{m.recipe.name}**"
            for m in matches[:3]
        )
        return f"🍳 {picks}"

    async def _compact_translate(self, route: Route) -> tuple[str, list[discord.Embed]]:
        target = resolve_language(route.payload2)
        if target is None:
            return f"• I don't know the language **{shorten(route.payload2, 30)}**.", []
        result = await asyncio.wait_for(translate_text(route.payload, target), timeout=20)
        if result is None:
            return "• the translation service isn't responding right now.", []
        return f"🗣️ {shorten(result.text, 300)}", []

    async def _compact_media(self, route: Route) -> tuple[str, list[discord.Embed]]:
        """One meme or GIF for a combined answer. Best effort, never raises."""
        engine = getattr(self.bot, "personality", None)
        if route.kind == GIF:
            search = getattr(engine, "gif_search", None)
            if search is not None and getattr(search, "available", False):
                try:
                    gif = await asyncio.wait_for(
                        search.search(route.payload or "reaction"), timeout=8.0
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    gif = None
                if gif is not None:
                    return "", [meme_embed(gif)]
        feed = getattr(engine, "meme_feed", None)
        if feed is not None:
            try:
                meme = await asyncio.wait_for(feed.pick(), timeout=8.0)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                meme = None
            if meme is not None:
                return "", [meme_embed(meme)]
        return f"• couldn't find a {route.kind} for that part right now.", []

    async def _compact_image(self, route: Route) -> tuple[str, list[discord.Embed]]:
        response = await self._find_images(route.payload, route.count)
        if response is None or not response.has_results:
            return NO_IMAGE_LINE, []
        return "", build_image_embeds(response, route.count)

    async def _compact_search(
        self, route: Route, *, asker: str = "someone"
    ) -> tuple[str, list[discord.Embed]]:
        """One short, combinable AI answer for a search part. Never raises.

        No web retrieval: the request is forwarded straight to the model
        (``AIService.answer_question``) and the reply is trimmed to fit the
        combined message.
        """
        subject = route.payload.strip()
        if not subject:
            return "", []
        ai = getattr(self.bot, "ai", None)
        if ai is None or not getattr(ai, "available", False):
            return "🔎 the AI isn't configured here, so that part went nowhere.", []
        try:
            answer = await asyncio.wait_for(
                ai.answer_question(subject, asker=asker), timeout=45
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - CancelledError is a BaseException
            log.debug("Combined AI part failed", exc_info=True)
            return "🔎 I couldn't answer that part right now.", []
        if not answer:
            return f"🔎 nothing solid for **{shorten(subject, 60)}** right now.", []
        return f"🔎 **{shorten(subject, 60)}** — {shorten(answer, 400)}", []

    # -- command metadata --------------------------------------------------

    async def _command_help(self, message: discord.Message, route: Route) -> bool:
        name = route.payload.casefold().lstrip("/")
        if name in RETIRED_COMMANDS:
            example = _RETIRED_EXAMPLES.get(name)
            if example:
                await self._quiet_reply(
                    message,
                    f"`/{name}` isn't a command any more — just ask me directly: "
                    f"*{example}*",
                )
            else:
                await self._quiet_reply(
                    message,
                    f"`/{name}` has been removed. try `/help` to see what's available.",
                )
            return True
        blurb = HELP_BLURBS.get(name)
        if blurb is None:
            await self._quiet_reply(
                message,
                f"I don't know `/{name}`. try `/help` for the commands that are actually registered.",
            )
            return True
        await self._quiet_reply(message, f"`/{name}` — {blurb}.")
        return True

    # -- native fun commands ----------------------------------------------

    async def _choose(self, message: discord.Message, route: Route) -> bool:
        raw = route.payload.strip()
        choices = parse_options(raw, limit=20)
        if len(choices) < 2:
            # The slash command documents `|` and `,`; natural speech often
            # uses "or", so accept that shape without involving AI.
            choices = [
                part.strip()
                for part in re.split(r"\s+or\s+", raw, flags=re.IGNORECASE)
                if part.strip()
            ]
        if not 2 <= len(choices) <= 20 or any(len(item) > 100 for item in choices):
            await self._quiet_reply(
                message,
                "give me 2–20 choices, separated by `|`, `,`, or `or`.",
            )
            return True
        pick = choose_option(choices)
        await self._quiet_reply(
            message,
            f"**{discord.utils.escape_markdown(pick)}**.\n\nthe council has spoken.",
        )
        return True

    # -- calculator --------------------------------------------------------

    async def _calculate(self, message: discord.Message, route: Route) -> bool:
        try:
            result = calculate(route.payload)
        except ParadiseError as error:
            await self._quiet_reply(message, f"🧮 {error}")
            return True
        num_val = getattr(result, "result", None)
        if isinstance(num_val, (int, float)):
            self._note_turn_detail(message, numeric=float(num_val))
        await self._quiet_reply(message, f"🧮 {result.display}")
        return True

    # -- dictionary ---------------------------------------------------------

    async def _define(self, message: discord.Message, route: Route) -> bool:
        cog = self.bot.get_cog("Dictionary")
        if cog is None:
            return await self._search(message, route)
        try:
            term = cog.feature.service.validate(route.payload)
            result = await asyncio.wait_for(
                cog.feature.lookup(term), timeout=_TOOL_TIMEOUT
            )
        except ParadiseError:
            result = None
        except Exception:  # noqa: BLE001 - fall through to AI search
            log.debug("Dictionary pre-lookup failed", exc_info=True)
            result = None

        if result is not None and result.found and result.entry is not None:
            from commands.dictionary import Editor, LoadingView  # local: heavy UI deps

            try:
                placeholder = await message.reply(
                    view=LoadingView(term), mention_author=False
                )
            except discord.HTTPException:
                return False
            await cog.deliver(
                term, Editor(message=placeholder), invoker_id=message.author.id
            )
            return True

        # Not a dictionary word (or not found): the AI answers better.
        return await self._search(message, route, query=f"what is {route.payload}")

    # -- conversion ----------------------------------------------------------

    async def _convert(self, message: discord.Message, route: Route) -> bool:
        utility = self.bot.get_cog("Utility")
        if utility is None:
            return False
        if _BARE_NUMBER.match(route.payload):
            # "convert 50" — the number is clear, the units are not. Ask for
            # exactly the missing piece, nothing else.
            await self._quiet_reply(
                message,
                f"{route.payload} what — USD, km, kg, °C? tell me the units "
                f"(e.g. *{route.payload} USD to INR*).",
            )
            return True
        parsed_tuple: tuple[float, str, str] | None = None
        try:
            parsed_tuple = parse_conversion_query(route.payload)
        except (ParadiseError, ValueError):
            parsed_tuple = None
        try:
            outcome = await asyncio.wait_for(
                utility.conversion_engine.convert_query(route.payload),
                timeout=_TOOL_TIMEOUT,
            )
        except ParadiseError as error:
            await self._quiet_reply(message, str(error))
            return True
        except asyncio.TimeoutError:
            await self._quiet_reply(
                message,
                "the conversion service timed out — try again in a moment.",
            )
            return True
        except Exception:  # noqa: BLE001
            log.warning("Mention conversion failed", exc_info=True)
            await self._quiet_reply(message, "couldn't perform that conversion.")
            return True
        from functions.conversion import build_conversion_embed  # local import fine

        to_val = getattr(outcome, "to_value", None)
        self._note_turn_detail(
            message,
            numeric=float(to_val) if isinstance(to_val, (int, float)) else None,
            conversion=parsed_tuple,
        )
        await self._quiet_reply(message, embed=build_conversion_embed(outcome))
        return True

    # -- image search ----------------------------------------------------------

    async def _image(self, message: discord.Message, route: Route) -> bool:
        """Find a real photo on the web. Never generates an image."""
        subject = route.payload.strip()
        if not subject:
            await self._quiet_reply(message, "a picture of what?")
            return True

        response = await self._find_images(subject, route.count)
        if response is None or not response.has_results:
            ai = getattr(self.bot, "ai", None)
            if ai is None or not getattr(ai, "available", False):
                await self._quiet_reply(message, NO_IMAGE_LINE)
                return True
            await self._quiet_reply(
                message,
                f"{NO_IMAGE_LINE} asking the AI about **{shorten(subject, 60)}** instead…",
            )
            return await self._search(
                message, Route(kind=SEARCH, payload=subject, confidence=route.confidence)
            )

        embeds = build_image_embeds(response, route.count)
        await self._quiet_reply(message, embeds=embeds)
        return True

    async def _find_images(self, subject: str, count: int):
        """Shared image lookup used by single and combined replies."""
        try:
            return await asyncio.wait_for(
                self.images.search(subject, limit=count), timeout=25
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - providers never break a reply
            log.warning("Mention image search failed", exc_info=True)
            return None

    # -- weather ---------------------------------------------------------------

    async def _weather(self, message: discord.Message, route: Route) -> bool:
        utility = self.bot.get_cog("Utility")
        if utility is None:
            return False
        city = (route.payload or "").strip()
        if not city:
            city = _city_from_iana(self._user_timezone(message.author.id))
        if not city:
            await self._quiet_reply(
                message, "which city? e.g. *weather in Delhi* or *weather Tokyo*."
            )
            return True
        try:
            data = await asyncio.wait_for(
                utility.weather_service.get_weather(city),
                timeout=_TOOL_TIMEOUT,
            )
        except ParadiseError as error:
            await self._quiet_reply(message, str(error))
            return True
        except Exception:  # noqa: BLE001
            log.warning("Mention weather failed", exc_info=True)
            await self._quiet_reply(
                message, "the weather service is unavailable right now. try again in a bit."
            )
            return True
        resolved_name = getattr(getattr(data, "location", None), "name", "") or city
        self._note_turn_detail(message, location=str(resolved_name))
        try:
            image = await asyncio.to_thread(render_weather_card, data)
            await message.reply(
                content=f"Weather for {data.location.name}",
                file=discord.File(io.BytesIO(image), filename="weather.png"),
                mention_author=False,
            )
        except Exception:  # noqa: BLE001 - the data still has a text fallback
            log.warning("Weather card rendering or delivery failed; using textual weather fallback", exc_info=True)
            await self._quiet_reply(message, embed=build_weather_embed(data))
        return True

    # -- time & date ---------------------------------------------------------------

    async def _time(self, message: discord.Message, route: Route) -> bool:
        tz_name: str | None = None
        label = ""
        raw = (route.payload or "").strip()
        if not raw or raw.casefold() in _SELF_TIME_DEIXIS:
            tz_name = self._user_timezone(message.author.id)
            label = getattr(message.author, "display_name", None) or "Local"
            if tz_name is None:
                info = get_current_time_for_zone("UTC", label="UTC")
                await self._quiet_reply(message, embed=build_time_embed(info))
                return True
        else:
            mention_match = re.search(r"<@!?(\d+)>", raw)
            if mention_match is not None:
                target_id = int(mention_match.group(1))
                member = (
                    message.guild.get_member(target_id)
                    if message.guild is not None
                    else None
                )
                who = (
                    getattr(member, "display_name", None)
                    or getattr(member, "name", None)
                    or "that member"
                )
                tz_name = self._user_timezone(target_id)
                if tz_name is None:
                    await self._quiet_reply(
                        message,
                        f"**{discord.utils.escape_markdown(who)}** hasn't saved a timezone yet — "
                        "they can set one with `/settimezone`.",
                    )
                    return True
                label = who
                city_hint = _city_from_iana(tz_name)
                if city_hint:
                    self._note_turn_detail(message, location=city_hint)
            else:
                tz_name = resolve_timezone(raw)
                if tz_name is None:
                    await self._quiet_reply(
                        message,
                        f"I don't recognize the place **{shorten(raw, 40)}** for time — "
                        "try a major city like `Tokyo`, `London`, `New York`, or an IANA zone.",
                    )
                    return True
                label = raw.title()
                self._note_turn_detail(message, location=raw)
        try:
            info = get_current_time_for_zone(tz_name, label=label)
        except ParadiseError as error:
            await self._quiet_reply(message, str(error))
            return True
        await self._quiet_reply(message, embed=build_time_embed(info))
        return True

    async def _date(self, message: discord.Message) -> bool:
        tz_name = self._user_timezone(message.author.id)
        try:
            zone = ZoneInfo(tz_name) if tz_name else ZoneInfo("UTC")
        except (ZoneInfoNotFoundError, KeyError, ValueError):
            tz_name, zone = None, ZoneInfo("UTC")
        now = datetime.now(zone)
        await self._quiet_reply(
            message,
            f"📅 today is **{now.strftime('%A, %d %B %Y')}**"
            + (f" ({tz_name})" if tz_name else " (UTC — set yours with /settimezone)"),
        )
        return True

    # -- translate & recipe --------------------------------------------------------

    async def _translate(self, message: discord.Message, route: Route) -> bool:
        text = (route.payload or "").strip()
        if not text:
            await self._quiet_reply(
                message,
                "what should I translate? reply to a message with "
                "`@Paradise translate to Hindi` or say `@Paradise translate hello into Japanese`.",
            )
            return True
        target = resolve_language(route.payload2)
        if target is None:
            await self._quiet_reply(
                message,
                f"I don't know the language **{shorten(route.payload2, 30)}** — "
                "try `japanese`, `hindi`, `spanish`… or a code like `ja` / `hi`.",
            )
            return True
        try:
            result = await asyncio.wait_for(
                translate_text(text, target), timeout=15.0
            )
        except Exception:  # noqa: BLE001 - translation is a native best-effort tool
            log.warning("Mention translation failed", exc_info=True)
            result = None
        if result is None:
            ai = getattr(self.bot, "ai", None)
            if ai is not None and getattr(ai, "available", False):
                return await self._answer_with_ai(
                    message,
                    f"Translate into {route.payload2} (reply with only the translation): {text}",
                )
            await self._quiet_reply(
                message, "the translation service isn't responding right now."
            )
            return True
        await self._quiet_reply(
            message,
            f"> {shorten(result.text, 900)}\n\n-# {result.source} → {target} · Google Translate",
        )
        return True

    @staticmethod
    def _parse_recipe_filters(meta: str) -> RecipeFilters:
        diet_part, _, mins_part = str(meta or "").partition("|")
        diet = diet_part.strip() or None
        max_time = int(mins_part) if mins_part.strip().isdigit() else None
        return RecipeFilters(diet=diet, max_time=max_time)

    async def _recipe(self, message: discord.Message, route: Route) -> bool:
        """Offline recipe matching from the bundled recipe book (zero API keys)."""
        ingredients = normalize_input(route.payload)
        if not ingredients:
            await self._quiet_reply(
                message,
                "tell me what's in your kitchen — e.g. `@Paradise recipe with eggs, onion, bread`.",
            )
            return True
        filters = self._parse_recipe_filters(route.payload2)
        matches = rank_recipes(ingredients, filters=filters, limit=3)
        approx = False
        if not matches:
            matches = near_misses(ingredients, filters=filters, limit=3)
            approx = True
        if not matches and (filters.diet or filters.max_time):
            matches = rank_recipes(ingredients, filters=RecipeFilters(), limit=3)
            approx = True
        if not matches:
            await self._quiet_reply(
                message,
                f"couldn't match a recipe with **{shorten(', '.join(prettify(i) for i in ingredients), 80)}** — "
                "try adding a base like `rice`, `bread`, `eggs`, `pasta`, or `paneer`.",
            )
            return True

        top = matches[0]
        recipe = top.recipe
        subtitle = "Closest match from what you have:" if approx else "Best match from your kitchen:"
        meta_bits = []
        if recipe.time_minutes:
            meta_bits.append(f"⏱ {recipe.time_minutes} min")
        if recipe.difficulty:
            meta_bits.append(f"📊 {recipe.difficulty}")
        if recipe.cuisine:
            meta_bits.append(recipe.cuisine.title())

        desc_lines = [subtitle]
        if meta_bits:
            desc_lines.append(" · ".join(meta_bits))
        if top.have:
            desc_lines.append(
                "**Have:** " + ", ".join(prettify(i) for i in top.have[:8])
            )
        if top.missing:
            missing_bits = []
            for item in top.missing[:5]:
                sub = suggest_substitution(item)
                missing_bits.append(
                    f"{prettify(item)} *(or {prettify(sub)})*" if sub else prettify(item)
                )
            desc_lines.append("**Need:** " + ", ".join(missing_bits))
        if recipe.instructions:
            steps_preview = "\n".join(
                f"{idx}. {shorten(step, 140)}"
                for idx, step in enumerate(recipe.instructions[:4], 1)
            )
            desc_lines.append(f"\n**Steps**\n{steps_preview}")
        if len(matches) > 1:
            alts = ", ".join(m.recipe.name for m in matches[1:3])
            desc_lines.append(f"\n-# Also possible: {alts} · use `/recipe` for interactive steps")

        embed = discord.Embed(
            title=f"🍳 {recipe.name}",
            description="\n".join(desc_lines)[:4000],
            colour=Colour.ACCENT,
        )
        await self._quiet_reply(message, embed=embed)
        return True

    # -- search (forwarded straight to the AI) --------------------------------------------

    # ------------------------------------------------------------------
    # The free toolkit lane (dice, qr, passwords, …) + reminders, polls
    # and Discord-native info — all offline, all instant.
    # ------------------------------------------------------------------

    async def _utility(self, message: discord.Message, route: Route) -> bool:
        """Run one deterministic toolkit tool and attach its image if any."""
        result = run_tool(
            route.payload2, route.payload,
            author=getattr(message.author, "display_name", "") or "",
        )
        if result.image:
            await self._quiet_reply(
                message,
                result.text,
                file=discord.File(io.BytesIO(result.image), filename=result.filename or "tool.png"),
            )
            return True
        await self._quiet_reply(message, result.text)
        return True

    async def _remind(self, message: discord.Message, route: Route) -> bool:
        """'remind me in 20 minutes to …' — the /remind engine by mention."""
        utility = self.bot.get_cog("Utility")
        if utility is None or message.channel.id is None:
            await self._quiet_reply(message, "the reminder engine isn't loaded right now.")
            return True
        when, what = self._split_reminder(route.payload)
        if when is None or not what:
            await self._quiet_reply(
                message,
                "when? try `remind me in 20 minutes to check the oven` "
                "(minutes, hours or days).",
            )
            return True
        minutes = max(1, min(int(when // 60) or 1, 10_080))
        okay, note = await utility.create_reminder(
            message.author.id, message.channel.id, minutes, what
        )
        await self._quiet_reply(message, note if okay else f"• {note}")
        return True

    @staticmethod
    def _split_reminder(rest: str) -> tuple[float | None, str]:
        """Split 'me in 20 minutes to X' into (seconds, text)."""
        import re as _re

        value = " ".join(str(rest or "").split()).strip()
        if value.casefold().startswith(("me ", "us ")):
            value = value.split(" ", 1)[1] if " " in value else ""
        if not value:
            return None, ""
        match = _re.search(
            r"\bin\s+(?P<dur>[^,]+?)\s+(?:to|about|that|for)\s+(?P<what>.+)$",
            value, _re.IGNORECASE,
        )
        if match is None:
            match = _re.search(
                r"^(?P<what>.+?)\s+in\s+(?P<dur>[^,]+)$", value, _re.IGNORECASE
            )
        if match is None:
            # "remind me to X" with no time at all.
            head = _re.sub(r"^(?:to|about)\s+", "", value, flags=_re.IGNORECASE)
            return None, head
        seconds = parse_duration(f"in {match.group('dur')}")
        what = " ".join(match.group("what").split())
        if what.casefold().startswith("to "):
            what = what[3:]
        return seconds, what

    async def _poll(self, message: discord.Message, route: Route) -> bool:
        """'poll pizza or burgers' — the /poll engine by mention."""
        utility = self.bot.get_cog("Utility")
        channel = getattr(message, "channel", None)
        if utility is None or channel is None:
            await self._quiet_reply(message, "the poll engine isn't loaded right now.")
            return True
        payload = route.payload
        question, _, options_text = payload.partition(":")
        if not options_text.strip():
            question, options_text = "", payload
        # Natural separators: "pizza or burgers", "manga vs anime", "a | b".
        import re as _re

        options_text = _re.sub(
            r"\s+(?:or|vs|versus)\s+", "|", options_text, flags=_re.IGNORECASE
        )
        choices = [c.strip() for c in options_text.split("|") if c.strip()]
        question = question.strip() or f"{getattr(message.author, 'display_name', 'someone')} asks"
        if not 2 <= len(choices) <= 10:
            await self._quiet_reply(
                message, "give me 2–10 options — `poll: pizza or burgers or pasta`."
            )
            return True
        okay = await utility.deploy_poll(
            channel, question, choices,
            getattr(message.author, "display_name", "someone"),
        )
        if not okay:
            await self._quiet_reply(message, "the poll fell over. permissions, probably.")
        return True

    async def _info(
        self,
        message: discord.Message,
        route: Route,
        *,
        reply: _ReplyContext | None = None,
    ) -> bool:
        """Discord-native info: whois, avatar, server card. No API calls."""
        tool = route.payload2 or "whois"
        if tool == "find":
            return await self._find_message(message, route.payload or "")
        guild = message.guild
        if tool == "server":
            if guild is None:
                await self._quiet_reply(message, "that only works inside a server.")
                return True
            embed = discord.Embed(
                title=f"🏡 {guild.name}",
                colour=discord.Colour.blurple(),
            )
            if guild.icon:
                embed.set_thumbnail(url=guild.icon.url)
            created = getattr(guild, "created_at", None)
            embed.add_field(name="members", value=str(getattr(guild, "member_count", "?")))
            embed.add_field(
                name="created",
                value=created.strftime("%d %b %Y") if created else "?",
            )
            embed.add_field(name="boosts", value=str(getattr(guild, "premium_subscription_count", 0) or 0))
            owner = getattr(guild, "owner", None)
            if owner is not None:
                embed.add_field(name="owner", value=owner.display_name)
            await self._quiet_reply(message, embed=embed)
            return True

        member = await self._resolve_member(message, route, reply=reply)
        if member is None:
            await self._quiet_reply(message, "who? mention them — `who is @someone`.")
            return True
        if tool == "avatar":
            embed = discord.Embed(
                title=f"🖼️ {member.display_name}'s avatar",
                colour=discord.Colour.blurple(),
            )
            avatar = getattr(member, "display_avatar", None)
            if avatar is not None:
                embed.set_image(url=avatar.url)
            await self._quiet_reply(message, embed=embed)
            return True
        embed = discord.Embed(
            title=f"👤 {member.display_name}",
            colour=discord.Colour.blurple(),
        )
        avatar = getattr(member, "display_avatar", None)
        if avatar is not None:
            embed.set_thumbnail(url=avatar.url)
        created = getattr(member, "created_at", None)
        joined = getattr(member, "joined_at", None)
        if joined is not None:
            embed.add_field(name="joined", value=joined.strftime("%d %b %Y"))
        if created is not None:
            embed.add_field(name="account made", value=created.strftime("%d %b %Y"))
        roles = getattr(member, "roles", None) or []
        if roles:
            names = [role.name for role in roles if role.name != "@everyone"]
            if names:
                embed.add_field(
                    name=f"roles ({len(names)})",
                    value=shorten(", ".join(names), 100),
                    inline=False,
                )
        await self._quiet_reply(message, embed=embed)
        return True

    async def _resolve_member(
        self,
        message: discord.Message,
        route: Route,
        *,
        reply: _ReplyContext | None = None,
    ):
        """The member an info route points at (mention, reply author, or asker)."""
        target = None
        for mention in getattr(message, "mentions", []) or []:
            if getattr(mention, "bot", False):
                continue
            target = mention
            break
        if target is None:
            match = re.search(r"<@!?(\d+)>", route.payload or "")
            if match and message.guild is not None:
                try:
                    target = message.guild.get_member(int(match.group(1))) or (
                        await message.guild.fetch_member(int(match.group(1)))
                    )
                except (discord.DiscordException, ValueError):
                    target = None
        if (
            target is None
            and reply is not None
            and reply.author is not None
            and not getattr(reply.author, "bot", False)
            and (route.payload or "").strip().casefold() not in ("me", "mine", "myself")
        ):
            target = reply.author
        return target or getattr(message, "author", None)

    async def _research(self, message: discord.Message, route: Route) -> bool:
        """Web research: real sources, real links, honest failure.

        Reuses its own thinking placeholder on model fallback so a failed
        web lookup never leaves a stale progress bubble in the channel.
        """
        subject = (route.payload or "").strip()
        if not subject:
            await self._quiet_reply(message, "search what, exactly?")
            return True
        mode = route.payload2 or "answer"
        service = self.research
        if not service.enabled:
            return await self._answer_with_ai(message, subject)
        ai = self.bot.ai
        asker = getattr(message.author, "display_name", None) or "someone"
        budget = service.timeout * (2 + max(0, service.fetch_pages)) + _AI_TIMEOUT
        placeholder = None
        try:
            if mode == "link":
                placeholder = await self._thinking_placeholder(
                    message, "🔗 reading that page…"
                )
                page_kwargs: dict = {"asker": asker}
                if isinstance(service, ResearchService):
                    page_kwargs["asker_id"] = int(getattr(message.author, "id", 0) or 0)
                summary = await asyncio.wait_for(
                    service.summarize_page(subject, ai, **page_kwargs),
                    timeout=budget,
                )
                if summary:
                    await self._deliver_ai_answer(message, placeholder, summary)
                    return True
                await self._deliver_ai_answer(
                    message, placeholder,
                    "couldn't read that page — the site refused, or it isn't "
                    "text I can parse (videos, raw files). want me to search "
                    "the topic instead?",
                )
                return True

            placeholder = await self._thinking_placeholder(
                message, "🔍 checking the real world…"
            )
            answer = await asyncio.wait_for(
                service.answer(
                    subject, ai, asker=asker, asker_id=message.author.id,
                    news=(mode == "news" or bool(route.fresh)),
                ),
                timeout=budget,
            )
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001 - fall back, never stall
            log.warning("Web research failed for %r", subject, exc_info=True)
            return await self._answer_with_ai(
                message, subject, placeholder=placeholder
            )

        if answer.answer:
            await self._deliver_ai_answer(
                message, placeholder, self._format_research(answer)
            )
            return True
        # If the AI synthesis failed/was off, surface the real sources we did find.
        surviving = getattr(answer, "sources", None) or []
        if surviving:
            lines = [f"🔍 top sources for **{shorten(subject, 80)}**:"]
            for idx, src in enumerate(surviving[:5], 1):
                title = (src.title or src.url).strip()
                snippet = f" — {shorten(src.snippet, 110)}" if getattr(src, "snippet", "") else ""
                lines.append(f"{idx}. [{title}]({src.url}){snippet}")
            await self._deliver_ai_answer(message, placeholder, "\n".join(lines))
            return True
        # Nothing usable came back — reuse the placeholder for the model's shot.
        return await self._answer_with_ai(message, subject, placeholder=placeholder)

    def _format_research(self, answer) -> str:
        """Answer text plus a clickable source footer (real links only)."""
        text = str(answer.answer or "").strip()
        if not getattr(self.research, "show_sources", False):
            return text
        sources = getattr(answer, "sources", None) or []
        if not sources:
            return text + "\n\n*(couldn't verify this one against a source — grain of salt)*"
        # Prefer the numbering the answer itself used ([n] markers), so the
        # footer and the citations line up for the reader.
        cited = list(getattr(answer, "cited", None) or [])
        offset = int(getattr(answer, "pages_read", 0) or 0)
        pairs = (
            list(zip(cited, sources))
            if cited and len(cited) == len(sources)
            else [
                (index, source)
                for index, source in enumerate(sources[:5], start=offset + 1)
            ]
        )
        lines = []
        for index, source in pairs[:5]:
            label = source.title.strip() or source.url
            lines.append(f"[{index}] [{label}]({source.url})")
        return f"{text}\n\n**sources**\n" + "\n".join(lines)

    async def _thinking_placeholder(self, message: discord.Message, line: str):
        """The 'working on it' message a long task edits in place."""
        try:
            return await message.reply(line, mention_author=False)
        except discord.DiscordException:
            return None

    async def _writing(self, message: discord.Message, route: Route) -> bool:
        """Writing help: rewrites, drafts, action items — copy-ready."""
        mode = route.payload2 if route.payload2 in WRITE_MODES else "rewrite"
        payload = (route.payload or "").strip()
        if mode in ("rewrite", "actions", "shorten", "expand"):
            # "rewrite this to be formal:" with nothing after the colon is
            # a usage question, not a model call.
            payload = strip_deixis(payload)
        if not payload:
            await self._quiet_reply(
                message, "give me the text to work with — e.g. 'rewrite this: <text>'."
            )
            return True
        ai = self.bot.ai
        if not getattr(ai, "available", False):
            return await self._ai_unavailable_note(message)
        placeholder = await self._thinking_placeholder(message, "✍️ drafting…")
        try:
            result = await asyncio.wait_for(
                run_writing(
                    mode, payload, ai,
                    asker=getattr(message.author, "display_name", None) or "someone",
                    asker_id=message.author.id,
                ),
                timeout=_AI_TIMEOUT,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a writing failure is a bad answer, not a crash
            log.warning("Writing help failed (%s)", mode, exc_info=True)
            result = ""
        if result:
            await self._deliver_ai_answer(message, placeholder, result)
        else:
            await self._deliver_ai_answer(
                message, placeholder, SEARCH_FAIL_LINE
            )
        return True

    async def _dev(self, message: discord.Message, route: Route) -> bool:
        """Dev/Minecraft help: explains, reviews, advises — never executes."""
        payload = (route.payload or "").strip()
        if not payload:
            await self._quiet_reply(
                message,
                "paste it after — e.g. 'explain this error: <the error>'.",
            )
            return True
        mode = route.payload2 if route.payload2 in DEV_MODES else "error"
        ai = self.bot.ai
        if not getattr(ai, "available", False):
            return await self._ai_unavailable_note(message)
        placeholder = await self._thinking_placeholder(message, "🛠️ digging in…")
        try:
            result = await asyncio.wait_for(
                run_devhelp(
                    mode, payload, ai,
                    asker=getattr(message.author, "display_name", None) or "someone",
                    asker_id=message.author.id,
                ),
                timeout=_AI_TIMEOUT,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.warning("Dev help failed (%s)", mode, exc_info=True)
            result = ""
        if result:
            await self._deliver_ai_answer(message, placeholder, result)
        else:
            await self._deliver_ai_answer(message, placeholder, SEARCH_FAIL_LINE)
        return True

    async def _build(
        self, message: discord.Message, route: Route, text: str = ""
    ) -> bool:
        """Handle AI Build Mode: create or edit web applications with live previews."""
        payload = (route.payload or "").strip()
        is_edit = route.payload2 == "edit"

        if is_edit:
            if self.build_orchestrator:
                active_project = self.build_orchestrator.store.get_active_user_project(
                    message.author.id, message.channel.id
                )
                if active_project:
                    return await self.build_orchestrator.handle_edit_build(
                        message, active_project, payload
                    )
            # Without an active project in this channel, a generic "add a …" or
            # "improve the …" sentence is a normal AI request — not a command to
            # spin up an unasked-for web app (or fail with "Build Mode off").
            return await self._ai(message, text or payload)

        if not self.build_orchestrator:
            await self._quiet_reply(
                message, "AI Build Mode is not configured on this instance."
            )
            return True

        # Start new build
        return await self.build_orchestrator.handle_new_build(message, payload)

    async def _find_message(self, message: discord.Message, query: str) -> bool:
        """Search this channel's recent history for a message."""
        clean = " ".join(str(query or "").split()).strip()
        if not clean:
            await self._quiet_reply(
                message, "find which message? try 'find the message where taher said hi'."
            )
            return True
        # Optional "<name> said <text>" / "from <name>: <text>" shape.
        author_hint = ""
        text_hint = clean
        match = re.match(
            r"^(?P<who>[\w .-]{2,32}?)\s+(?:said|wrote|posted|typed)\s+(?P<what>.+)$",
            clean, re.IGNORECASE,
        )
        if match:
            author_hint = match.group("who").strip().casefold()
            text_hint = match.group("what").strip().casefold()
        else:
            match = re.match(
                r"^from\s+(?P<who>[\w .-]{2,32}?)\s*[:,]\s*(?P<what>.+)$",
                clean, re.IGNORECASE,
            )
            if match:
                author_hint = match.group("who").strip().casefold()
                text_hint = match.group("what").strip().casefold()
        needle = text_hint.casefold()
        found = []
        history = getattr(message.channel, "history", None)
        if history is None:
            await self._quiet_reply(message, "I can't read history in this channel.")
            return True
        try:
            async for old in history(limit=300, before=message):
                if old.author.bot or old.id == message.id:
                    continue
                if author_hint:
                    author = f"{old.author.name} {getattr(old.author, 'display_name', '')}".casefold()
                    if author_hint not in author:
                        continue
                if needle and needle not in str(old.content or "").casefold():
                    continue
                found.append(old)
                if len(found) >= 3:
                    break
        except discord.DiscordException:
            found = []
        if not found:
            await self._quiet_reply(
                message, "nothing matching that in the last 300 messages here."
            )
            return True
        guild_id = getattr(message.guild, "id", None) or 0
        lines = ["🔎 found:"]
        for old in found:
            link = f"https://discord.com/channels/{guild_id}/{message.channel.id}/{old.id}"
            author = getattr(old.author, "display_name", None) or "someone"
            snippet = " ".join(str(old.content or "").split())[:80]
            lines.append(f"• [{author}]({link}): {snippet}")
        await self._quiet_reply(message, "\n".join(lines))
        return True

    async def _search(
        self, message: discord.Message, route: Route, *, query: str | None = None
    ) -> bool:
        """Forward a search-shaped request to the AI (web off or gray zone).

        Explicitly search-shaped asks (high confidence) go to the research
        pipeline when the web side is on; everything else gets the model's
        own answer, in character, with no moralising non-answers.
        """
        subject = (query or route.payload or "").strip()
        if not subject:
            await self._quiet_reply(message, "search what, exactly?")
            return True
        if route.confidence >= 0.8 and self.research.enabled:
            return await self._research(
                message,
                Route(
                    kind=RESEARCH, payload=subject, payload2="answer",
                    confidence=route.confidence, fresh=route.fresh,
                ),
            )
        return await self._answer_with_ai(message, subject)

    # -- recap, memes and GIFs by mention ---------------------------------

    async def _summarize(
        self, message: discord.Message, route: Route | None = None
    ) -> bool:
        """``@Paradise summarize`` — the /summarize logic, no slash command.

        Reads the recent human conversation in this channel (bounded, oldest
        first), honouring an explicit count like "summarize last 20 messages".
        """
        ai = getattr(self.bot, "ai", None)
        if ai is None or not getattr(ai, "available", False):
            await self._quiet_reply(
                message, "the AI isn't configured right now, so no recaps either."
            )
            return True
        channel = message.channel
        history = getattr(channel, "history", None)
        if history is None:
            await self._quiet_reply(message, "i need a normal text channel for that.")
            return True

        raw_wanted = str(getattr(route, "payload", "") or "").strip()
        wanted = max(5, min(int(raw_wanted), 100)) if raw_wanted.isdigit() else 40
        current_id = getattr(message, "id", None)
        picked = []
        try:
            async for past in channel.history(limit=wanted * 3):
                if past.author.bot or not str(past.content).strip():
                    continue
                past_id = getattr(past, "id", None)
                if current_id is not None and past_id is not None and past_id == current_id:
                    continue
                picked.append(past)
                if len(picked) >= wanted:
                    break
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - history is best-effort
            log.debug("Could not read channel history", exc_info=True)
            picked = []

        if len(picked) < 3:
            await self._quiet_reply(
                message, "there isn't enough recent conversation here to recap."
            )
            return True

        picked.reverse()  # oldest first
        lines = []
        total = 0
        for past in picked:
            line = f"{getattr(past.author, 'display_name', 'someone')}: {str(past.content)[:220]}"
            if total + len(line) > MAX_SUMMARY_CHARS:
                lines.append("(older messages trimmed)")
                break
            lines.append(line)
            total += len(line) + 1

        try:
            summary = await asyncio.wait_for(
                ai.summarize(lines, getattr(channel, "name", "this channel")),
                timeout=_AI_TIMEOUT,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Mention summarize failed")
            await self._quiet_reply(
                message, "that broke on my end — it's been logged. try again in a moment."
            )
            return True

        header = (
            f"📋 **recap of the last {len(picked)} messages in "
            f"#{getattr(channel, 'name', 'here')}:**"
        )
        parts = split_discord_text(summary, limit=1800)
        await self._quiet_reply(message, f"{header}\n\n{parts[0]}")
        for part in parts[1:]:
            await self._quiet_reply(message, part)
        return True

    async def _meme(self, message: discord.Message, route: Route) -> bool:
        """``@Paradise meme`` / ``@Paradise meme about exams`` — one meme.

        Same pipeline as /meme: a real internet meme when the feed has one,
        a locally drawn card about the topic otherwise. Never fails loudly.
        """
        topic = (route.payload or "").strip()
        if await self._send_online_meme(message):
            return True
        author = clean_name(
            getattr(message.author, "display_name", None), fallback="someone"
        )
        try:
            png = render_topic_meme(topic or "the meme mines are closed today", author=author)
        except Exception:  # noqa: BLE001 - rendering must never crash chat
            log.debug("Topic meme render failed", exc_info=True)
            await self._quiet_reply(message, _MEME_EMPTY)
            return True
        await self._quiet_reply(
            message,
            file=discord.File(io.BytesIO(png), filename="paradise_meme.png"),
        )
        return True

    async def _gif(self, message: discord.Message, route: Route) -> bool:
        """``@Paradise gif`` / ``@Paradise bruh gif`` — one reaction GIF.

        Keyless by default (same tiers as /gif); when no GIF turns up the
        meme pipeline answers instead, so the mention never dead-ends.
        """
        query = (route.payload or "").strip() or "reaction"
        engine = getattr(self.bot, "personality", None)
        search = getattr(engine, "gif_search", None)
        if search is not None and getattr(search, "available", False):
            try:
                gif = await asyncio.wait_for(search.search(query), timeout=8.0)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.debug("GIF search failed", exc_info=True)
                gif = None
            if gif is not None:
                await self._quiet_reply(message, embed=meme_embed(gif))
                return True
            await self._quiet_reply(message, _GIF_EMPTY)
            return True
        # No GIF tier available: a meme is the standing substitute.
        return await self._meme(message, route)

    async def _send_online_meme(self, message: discord.Message) -> bool:
        """Post one internet meme from the personality's feed, if it has one."""
        engine = getattr(self.bot, "personality", None)
        feed = getattr(engine, "meme_feed", None)
        if feed is None:
            return False
        try:
            meme = await asyncio.wait_for(feed.pick(), timeout=8.0)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.debug("Meme fetch failed", exc_info=True)
            return False
        if meme is None:
            return False
        await self._quiet_reply(message, embed=meme_embed(meme))
        return True

    async def _answer_with_ai(
        self,
        message: discord.Message,
        subject: str,
        *,
        placeholder=None,
    ) -> bool:
        """Ask the AI *subject* and deliver its answer, reusing any placeholder."""
        ai = self.bot.ai
        if not getattr(ai, "available", False):
            if placeholder is not None:
                await self._deliver_ai_answer(
                    message,
                    placeholder,
                    "couldn't get a live answer right now, and my AI brain isn't configured here.",
                )
                return True
            return await self._ai_unavailable_note(message)

        if placeholder is None:
            try:
                placeholder = await message.reply(
                    "🧠 thinking with my entire brain…", mention_author=False
                )
            except discord.DiscordException:
                placeholder = None

        try:
            answer = await asyncio.wait_for(
                ai.answer_question(
                    subject,
                    asker=getattr(message.author, "display_name", None) or "someone",
                    channel_id=message.channel.id,
                ),
                timeout=_AI_TIMEOUT,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            log.warning("AI answer timed out")
            await self._deliver_ai_answer(message, placeholder, SEARCH_FAIL_LINE)
            return True
        except ParadiseError as error:
            await self._deliver_ai_answer(message, placeholder, str(error))
            return True
        except Exception:  # noqa: BLE001
            log.exception("AI answer failed")
            await self._deliver_ai_answer(message, placeholder, SEARCH_FAIL_LINE)
            return True

        await self._deliver_ai_answer(
            message, placeholder, answer or "I couldn't get a useful answer. try again."
        )
        return True

    async def _deliver_ai_answer(
        self, message: discord.Message, placeholder, text: str
    ) -> None:
        """Replace the placeholder with the full answer; reply if edit fails."""
        chunks = split_discord_text(text)
        delivered_first = False
        if placeholder is not None:
            edited = await self._quiet_edit(placeholder, chunks[0])
            delivered_first = edited is not False
        if not delivered_first:
            await self._quiet_reply(message, chunks[0])
        for chunk in chunks[1:]:
            await self._quiet_reply(message, chunk)

    # -- AI conversation ---------------------------------------------------------------

    async def _ai_unavailable_note(self, message: discord.Message) -> bool:
        """What we say when no AI provider is configured (rate-limited)."""
        now = time.monotonic()
        last = self._fallback_last.get(message.channel.id)
        if last is not None and now - last < _FALLBACK_NOTE_COOLDOWN:
            return True
        self._fallback_last[message.channel.id] = now
        await self._quiet_reply(
            message,
            "my AI brain isn't configured here, but the tools work! try:\n"
            "*define <word>* · *100 USD to INR* ·\n"
            "*weather in <city>* · *time in <city>* · *picture of <thing>*.",
        )
        return True

    async def _ai(self, message: discord.Message, text: str) -> bool:
        ai = self.bot.ai
        if not ai.available:
            return await self._ai_unavailable_note(message)
        wait = max(0.0, ai.cooldown_remaining(message))
        if wait > 0:
            if wait > _AI_WAIT_TOLERANCE:
                # The same member machine-gunning mentions: quiet, as ever.
                return True
            # A parallel request is finishing / an AI slot frees up in a
            # moment: queue this one briefly instead of dropping it.
            await asyncio.sleep(wait)
        try:
            answer = await asyncio.wait_for(ai.respond(message), timeout=45)
        except asyncio.TimeoutError:
            log.warning("Assistant AI answer timed out")
            await self._quiet_reply(message, "AI's having a moment. try again.")
            return True
        except ParadiseError as error:
            await self._quiet_reply(message, str(error))
            return True
        except Exception:  # noqa: BLE001
            log.exception("Assistant AI answer failed")
            await self._quiet_reply(message, "I couldn't finish that answer. try again.")
            return True
        if not answer:
            await self._quiet_reply(message, "I couldn't get a useful answer. try again.")
            return True
        await self._quiet_reply(message, answer)
        return True

    # -- small utilities ----------------------------------------------------------------

    async def _maybe_hint(self, message: discord.Message) -> bool:
        now = time.monotonic()
        # None = never shown (0.0 would silently block hints for the first
        # minutes after the host booted, since monotonic time starts at 0).
        last = self._hint_last.get(message.channel.id)
        if last is not None and now - last < _HINT_COOLDOWN:
            return True
        self._hint_last[message.channel.id] = now
        await self._quiet_reply(message, ASK_HINT)
        return True

    def _cooled(self, message: discord.Message) -> bool:
        """Anti-spam, never anti-social.

        Discord dispatches every message as its own task, so several members
        can be mid-answer at once. The only thing paced here is *one member*
        repeating themselves: the channel window applies solely to the person
        who asked last. A different member asking right after them goes
        straight through — parallel requests are the point, not a violation.
        """
        now = time.monotonic()
        user_last = self._user_last.get(message.author.id)
        if user_last is not None and now - user_last < _USER_COOLDOWN:
            return False
        channel_last = self._channel_last.get(message.channel.id)
        if (
            channel_last is not None
            and channel_last[1] == message.author.id
            and now - channel_last[0] < _CHANNEL_COOLDOWN
        ):
            return False
        self._user_last[message.author.id] = now
        self._channel_last[message.channel.id] = (now, message.author.id)
        if len(self._user_last) > 512:
            cutoff = now - _USER_COOLDOWN
            self._user_last = {k: v for k, v in self._user_last.items() if v > cutoff}
            self._channel_last = {
                k: v for k, v in self._channel_last.items() if v[0] > cutoff
            }
        return True

    async def cog_load(self) -> None:
        """Start background services."""
        if self.build_orchestrator:
            try:
                await self.build_orchestrator.start()
            except Exception:
                log.exception("Failed to start BuildOrchestrator preview server")

    async def cog_unload(self) -> None:
        """Stop background services."""
        if self.build_orchestrator:
            try:
                await self.build_orchestrator.stop()
            except Exception:
                log.exception("Failed to stop BuildOrchestrator")

    async def _quiet_reply(self, message: discord.Message, content: str = "", **kwargs) -> None:
        """Reply safely without truncating a complete answer.

        Discord allows 2,000 characters per message. Normal responses use
        one message; only unexpectedly long output is split at natural
        boundaries. Attachments/embeds stay on the first message.
        """
        try:
            reply_kwargs = dict(kwargs)
            if content:
                reply_kwargs.setdefault("content", content)
            raw_content = reply_kwargs.pop("content", None)
            chunks = split_discord_text(raw_content) if raw_content else [None]
            for index, chunk in enumerate(chunks):
                current = dict(reply_kwargs)
                if chunk is not None:
                    current["content"] = chunk
                if index:
                    # Views and files are single-use; the first message owns
                    # them and continuation messages are plain text.
                    for key in ("embed", "embeds", "file", "files", "view"):
                        current.pop(key, None)
                await asyncio.wait_for(
                    message.reply(
                        allowed_mentions=discord.AllowedMentions.none(),
                        mention_author=False,
                        **current,
                    ),
                    timeout=15,
                )
        except (discord.DiscordException, asyncio.TimeoutError, AttributeError):
            log.debug("Assistant reply failed", exc_info=True)

    async def _quiet_edit(
        self, message: discord.Message, content: str = "", **kwargs
    ) -> bool:
        try:
            if content:
                kwargs.setdefault("content", split_discord_text(content)[0])
            await asyncio.wait_for(message.edit(**kwargs), timeout=15)
            return True
        except (discord.DiscordException, asyncio.TimeoutError, AttributeError):
            log.debug("Assistant edit failed", exc_info=True)
            return False


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Assistant(bot))
