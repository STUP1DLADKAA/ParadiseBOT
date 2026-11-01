"""The mention router: "@Paradise <anything>" — no commands to learn.

When someone addresses Paradise (mention or reply), this cog classifies
the request with the deterministic router (functions/router.py) and sends
it to the most specialised tool:

    calculator · define · convert · image search · weather · time/date
    · translate → the AI (openai-compatible first, gemini fallback)

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
    HELP, IMAGE, PING, ROAST, SEARCH, TIME, TRANSLATE, UNKNOWN, WEATHER,
    Route, route_many,
)
from functions.time import build_time_embed, get_current_time_for_zone, resolve_timezone
from functions.translate import resolve_language, translate_text
from functions.weather import build_weather_embed, render_weather_card

log = logging.getLogger(__name__)

_USER_COOLDOWN = 5.0
_CHANNEL_COOLDOWN = 2.0
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
    "• `@Paradise roast me`  ·  `@Paradise meme`\n"
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
        self._channel_last: dict[int, float] = {}
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
        # fan-out, not an unbounded promise explosion.
        asker = getattr(message.author, "display_name", None) or "someone"
        outcomes = await asyncio.gather(
            *(self._compact(route, asker=asker) for route in routes),
            return_exceptions=True,
        )

        lines: list[str] = []
        embeds: list[discord.Embed] = []
        for route, outcome in zip(routes, outcomes, strict=False):
            if isinstance(outcome, BaseException):
                if isinstance(outcome, asyncio.CancelledError):
                    raise outcome
                log.warning("Combined %s part failed", route.kind, exc_info=outcome)
                lines.append(f"• couldn't finish the *{route.kind}* part of that.")
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
        if ai.cooldown_remaining(message) > 0:
            return True
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
        now = time.monotonic()
        user_last = self._user_last.get(message.author.id)
        if user_last is not None and now - user_last < _USER_COOLDOWN:
            return False
        channel_last = self._channel_last.get(message.channel.id)
        if channel_last is not None and now - channel_last < _CHANNEL_COOLDOWN:
            return False
        self._user_last[message.author.id] = now
        self._channel_last[message.channel.id] = now
        if len(self._user_last) > 512:
            cutoff = now - _USER_COOLDOWN
            self._user_last = {k: v for k, v in self._user_last.items() if v > cutoff}
            self._channel_last = {k: v for k, v in self._channel_last.items() if v > cutoff}
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
