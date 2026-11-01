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
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands

from commands.ai import MAX_SUMMARY_CHARS
from commands.banter import _GIF_EMPTY, _MEME_EMPTY, meme_embed, render_topic_meme
from commands.fun import build_ping_message, choose_option, flip_coin
from commands.system import HELP_BLURBS
from functions.startup import RETIRED_COMMANDS
from functions.calculator import calculate
from functions.helpers import (
    ParadiseError, parse_options, shorten, split_discord_text,
)
from functions.images import (
    NO_IMAGE_LINE, ImageSearchService, build_image_embeds,
)
from functions.intent import classify_request, should_classify
from functions.roasts import (
    build_context_roast,
    build_roast,
    clean_name,
    sanitize_intensity,
)
from functions.router import (
    AI, CALCULATE, CHOOSE, COINFLIP, COMMAND_HELP, CONVERT, DATE, DEFINE,
    GIF, HELP, IMAGE, INFO, MEME, PING, POLL, REMIND, ROAST, SEARCH, SUMMARIZE,
    TIME, TRANSLATE, UNKNOWN, UTILITY, WEATHER,
    Route, route_many,
)
from functions.toolkit import parse_duration, run_tool
from functions.time import build_time_embed, get_current_time_for_zone, resolve_timezone
from functions.translate import resolve_language, translate_text
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

    # ------------------------------------------------------------------
    # Entry point (called from bot.on_message)
    # ------------------------------------------------------------------

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

        try:
            if len(routes) > 1:
                return await self._dispatch_many(message, routes)
            route = await self._refine(routes[0], text)
            # Observability: the tool chosen and how long it took — never
            # the message content, the user, or any credential.
            log.info("Assistant → %s (confidence %.2f)", route.kind, route.confidence)
            started = time.monotonic()
            handled = await self._dispatch(message, route, text)
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

    async def _refine(self, route: Route, text: str) -> Route:
        """Upgrade a low-confidence route with a compact AI classification."""
        if not should_classify(route):
            return route
        guess = await classify_request(text, getattr(self.bot, "ai", None))
        if guess is None:
            return route
        refined = guess.to_route(route)
        if refined.kind == route.kind:
            return route
        log.info("Intent refined by classifier: %s → %s", route.kind, refined.kind)
        return refined

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, message: discord.Message, route: Route, text: str) -> bool:
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
            return await self._roast(message, route)
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
        if kind == SUMMARIZE:
            return await self._summarize(message)
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
            return await self._info(message, route)
        if kind == SEARCH:
            return await self._search(message, route)
        if kind in (AI, UNKNOWN):
            # AI is the language/reasoning layer, not an agent loop. Search
            # requests land in _search above, which forwards them to the
            # same provider chain — there is no web retrieval either way.
            return await self._ai(message, text)
        return await self._ai(message, text)

    async def _roast(self, message: discord.Message, route: Route) -> bool:
        """``@Paradise roast <someone>`` — a jab aimed at the right person,
        about what they actually just said when Paradise knows it."""
        target = self._roast_target(message, route.payload or "")
        engine = getattr(self.bot, "personality", None)
        intensity = sanitize_intensity(getattr(engine, "roast_intensity", "spicy"))
        recent = getattr(engine, "_recent_roasts", None)
        context = self._target_last_words(message, target)
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
    def _roast_target(message: discord.Message, payload: str):
        """Who the roast is for: a mentioned member, the asker, or a plain name."""
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
                )
        lowered = text.lower()
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
            )
        return SimpleNamespace(
            name=clean_name(text, fallback="them"), self_roast=False, id=0
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
        own_kinds = (REMIND, POLL, INFO)
        compact_routes = [route for route in routes if route.kind not in own_kinds]
        own_routes = [route for route in routes if route.kind in own_kinds]
        outcomes = await asyncio.gather(
            *(self._compact(route, asker=asker) for route in compact_routes),
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

    async def _compact(
        self, route: Route, *, asker: str = "someone"
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
                return self._compact_time(route), []
            if kind == DATE:
                now = datetime.now(ZoneInfo("UTC"))
                return f"📅 today is **{now.strftime('%A, %d %B %Y')}** (UTC)", []
            if kind == TRANSLATE:
                return await self._compact_translate(route)
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

    def _compact_time(self, route: Route) -> str:
        tz_name = resolve_timezone(route.payload) if route.payload else "UTC"
        if tz_name is None:
            return f"🕒 I don't recognize the place **{shorten(route.payload, 40)}**."
        info = get_current_time_for_zone(tz_name, label=route.payload.title() or "UTC")
        return (
            f"🕒 **{info.location_or_target}** — {info.formatted_12h} "
            f"({info.abbreviation})"
        )

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
            # The capability still exists — only the command is gone.
            example = _RETIRED_EXAMPLES.get(name, f"@Paradise {name} …")
            await self._quiet_reply(
                message,
                f"`/{name}` isn't a command any more — just ask me directly: "
                f"*{example}*",
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
        await self._quiet_reply(message, f"🧮 {result.display}")
        return True

    # -- dictionary ---------------------------------------------------------

    async def _define(self, message: discord.Message, route: Route) -> bool:
        cog = self.bot.get_cog("Dictionary")
        if cog is None:
            return await self._search(message, route)
        try:
            term = cog.feature.service.validate(route.payload)
            result = await cog.feature.lookup(term)
        except ParadiseError:
            result = None
        except Exception:  # noqa: BLE001 - fall through to web search
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
        try:
            outcome = await utility.conversion_engine.convert_query(route.payload)
        except ParadiseError as error:
            await self._quiet_reply(message, str(error))
            return True
        except Exception:  # noqa: BLE001
            log.warning("Mention conversion failed", exc_info=True)
            await self._quiet_reply(message, "couldn't perform that conversion.")
            return True
        from functions.conversion import build_conversion_embed  # local import fine

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
            # No usable picture: say so once, then answer the question the
            # only other way we can — by asking the AI about the subject.
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
        if not route.payload:
            await self._quiet_reply(
                message, "which city? e.g. *weather in Delhi* or *weather Tokyo*."
            )
            return True
        try:
            data = await utility.weather_service.get_weather(route.payload)
        except ParadiseError as error:
            await self._quiet_reply(message, str(error))
            return True
        except Exception:  # noqa: BLE001
            log.warning("Mention weather failed", exc_info=True)
            await self._quiet_reply(
                message, "the weather service is unavailable right now. try again in a bit."
            )
            return True
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
        utility = self.bot.get_cog("Utility")

        tz_name: str | None = None
        label = ""
        if not route.payload:
            # No place named → the caller's configured timezone, if any.
            if utility is not None:
                tz_name = utility.db.get_user_timezone(message.author.id)
                label = message.author.display_name
            if tz_name is None:
                info = get_current_time_for_zone("UTC", label="UTC")
                await self._quiet_reply(
                    message, embed=build_time_embed(info),
                )
                return True
        else:
            tz_name = resolve_timezone(route.payload)
            if tz_name is None:
                await self._quiet_reply(
                    message,
                    f"I don't recognize the place **{shorten(route.payload, 40)}** for time — "
                    "try a major city like `Tokyo`, `London`, `New York`, or an IANA zone.",
                )
                return True
            label = route.payload.title()
        try:
            info = get_current_time_for_zone(tz_name, label=label)
        except ParadiseError as error:
            await self._quiet_reply(message, str(error))
            return True
        await self._quiet_reply(message, embed=build_time_embed(info))
        return True

    async def _date(self, message: discord.Message) -> bool:
        utility = self.bot.get_cog("Utility")
        tz_name = (
            utility.db.get_user_timezone(message.author.id) if utility is not None else None
        )
        zone = ZoneInfo(tz_name) if tz_name else ZoneInfo("UTC")
        now = datetime.now(zone)
        await self._quiet_reply(
            message,
            f"📅 today is **{now.strftime('%A, %d %B %Y')}**"
            + (f" ({tz_name})" if tz_name else " (UTC — set yours with /settimezone)"),
        )
        return True

    # -- translate ---------------------------------------------------------------

    async def _translate(self, message: discord.Message, route: Route) -> bool:
        target = resolve_language(route.payload2)
        if target is None:
            await self._quiet_reply(
                message,
                f"I don't know the language **{shorten(route.payload2, 30)}** — "
                "try `japanese`, `hindi`, `spanish`… or a code like `ja` / `hi`.",
            )
            return True
        try:
            result = await translate_text(route.payload, target)
        except Exception:  # noqa: BLE001 - translation is a native best-effort tool
            log.warning("Mention translation failed", exc_info=True)
            result = None
        if result is None:
            await self._quiet_reply(message, "the translation service isn't responding right now.")
            return True
        await self._quiet_reply(
            message,
            f"> {shorten(result.text, 900)}\n\n-# {result.source} → {target} · Google Translate",
        )
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

    async def _info(self, message: discord.Message, route: Route) -> bool:
        """Discord-native info: whois, avatar, server card. No API calls."""
        tool = route.payload2 or "whois"
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

        member = await self._resolve_member(message, route)
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

    async def _resolve_member(self, message: discord.Message, route: Route):
        """The member an info route points at (mention), else the asker."""
        import re as _re

        target = None
        for mention in getattr(message, "mentions", []) or []:
            if getattr(mention, "bot", False):
                continue
            target = mention
            break
        if target is None:
            match = _re.search(r"<@!?(\d+)>", route.payload or "")
            if match and message.guild is not None:
                try:
                    target = message.guild.get_member(int(match.group(1))) or (
                        await message.guild.fetch_member(int(match.group(1)))
                    )
                except (discord.DiscordException, ValueError):
                    target = None
        return target or getattr(message, "author", None)

    async def _search(
        self, message: discord.Message, route: Route, *, query: str | None = None
    ) -> bool:
        """Forward a search-shaped request straight to the AI.

        There is no web retrieval on this path any more: the request goes to
        the model (openai-compatible provider first, gemini fallback) and the
        model replies in character. Genuine questions are never refused —
        the system prompt forbids moralising non-answers.
        """
        subject = (query or route.payload or "").strip()
        if not subject:
            await self._quiet_reply(message, "search what, exactly?")
            return True
        return await self._answer_with_ai(message, subject)

    # -- recap, memes and GIFs by mention ---------------------------------

    async def _summarize(self, message: discord.Message) -> bool:
        """``@Paradise summarize`` — the /summarize logic, no slash command.

        Reads the recent human conversation in this channel (bounded, oldest
        first) and asks the AI for the same recap /summarize produces.
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

        wanted = 40
        picked = []
        try:
            async for past in channel.history(limit=wanted * 3):
                if past.author.bot or not str(past.content).strip():
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

    async def _answer_with_ai(self, message: discord.Message, subject: str) -> bool:
        """Ask the AI *subject* and deliver its answer, placeholder first."""
        ai = self.bot.ai
        if not getattr(ai, "available", False):
            return await self._ai_unavailable_note(message)

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
        """Replace the placeholder with the full answer, split if needed."""
        chunks = split_discord_text(text)
        if placeholder is not None:
            await self._quiet_edit(placeholder, chunks[0])
        else:
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

    async def _quiet_edit(self, message: discord.Message, content: str = "", **kwargs) -> None:
        try:
            if content:
                kwargs.setdefault("content", split_discord_text(content)[0])
            await asyncio.wait_for(message.edit(**kwargs), timeout=15)
        except (discord.DiscordException, asyncio.TimeoutError, AttributeError):
            log.debug("Assistant edit failed", exc_info=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Assistant(bot))
