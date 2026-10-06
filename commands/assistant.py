"""The mention router: "@Paradise <anything>" — no commands to learn.

When someone addresses Paradise (mention or reply), this cog classifies
the request with the deterministic router (functions/router.py) and sends
it to the most specialised tool:

    calculator · define · convert · image search · weather · time/date
    · translate → explicit web search (answer + sources)
    → Gemini/OpenAI-compatible AI — strictly the last resort

Pipeline:

    mention → strip mention → empty? → deterministic route(s)
            → ambiguous? one tiny Gemini label (functions/intent.py)
            → run tool(s), concurrently when the request asks for several
            → optional grounded Gemini synthesis over retrieved sources
            → Discord-safe reply

Everything here reuses the existing services owned by the other cogs, so
there is exactly one implementation of each capability. Failures degrade
gracefully: an unavailable source falls back to a friendly message, and
no AI key ever breaks tool answers. Live data is never invented — when a
provider is down Paradise says so rather than asking the AI to guess.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands

from commands.fun import build_ping_message, choose_option, flip_coin
from commands.system import HELP_BLURBS
from functions.calculator import calculate
from functions.helpers import (
    ParadiseError, parse_options, sanitize_untrusted, shorten, split_discord_text,
)
from functions.images import (
    NO_IMAGE_LINE, ImageSearchService, build_image_embeds,
)
from functions.intent import classify_request, should_classify
from functions.router import (
    AI, CALCULATE, CHOOSE, COINFLIP, COMMAND_HELP, CONVERT, DATE, DEFINE,
    HELP, IMAGE, PING, SEARCH, TIME, TRANSLATE, UNKNOWN, WEATHER,
    Route, route_many,
)
from functions.search import SearchAnswerView, SearchResponse, build_answer_embed
from functions.time import build_time_embed, get_current_time_for_zone, resolve_timezone
from functions.translate import resolve_language, translate_text
from functions.weather import build_weather_embed, render_weather_card

log = logging.getLogger(__name__)

_USER_COOLDOWN = 5.0
_CHANNEL_COOLDOWN = 2.0
_HINT_COOLDOWN = 120.0
_FALLBACK_NOTE_COOLDOWN = 15 * 60

# Grounded synthesis budget: compact evidence in, a short answer out.
_SYNTH_SOURCES = 4
_SYNTH_SNIPPET = 220
_SYNTH_TIMEOUT = 20.0
_WEAK_SUMMARY_CHARS = 140

ASK_HINT = (
    "hey! just ask me things like:\n"
    "• `@Paradise define photosynthesis`\n"
    "• `@Paradise 25% of 800`  ·  `@Paradise convert 100 USD to INR`\n"
    "• `@Paradise flip a coin`  ·  `@Paradise choose pizza or burgers`\n"
    "• `@Paradise weather in Delhi`  ·  `@Paradise time in Tokyo`\n"
    "• `@Paradise show me a picture of a red panda`\n"
    "• `@Paradise latest Minecraft news`\n"
    "• `@Paradise recommend a movie like Interstellar`\n"
    "or use `/help` for the full list."
)

SEARCH_FAIL_LINE = "⚠️ I couldn't retrieve reliable information right now. Try again in a moment."


class Assistant(commands.Cog):
    """Routes mentions/replies to the right tool, AI last."""

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
            # AI is a language/reasoning fallback, not an agent loop. In
            # particular, never search first and then ask AI to rewrite the
            # result; explicit search requests are handled by _search above.
            return await self._ai(message, text)
        return await self._ai(message, text)

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
        outcomes = await asyncio.gather(
            *(self._compact(route) for route in routes), return_exceptions=True
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

    async def _compact(self, route: Route) -> tuple[str, list[discord.Embed]]:
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
                    Route(kind=SEARCH, payload=f"what is {route.payload}")
                )
            return await self._compact_search(route)
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
        return (
            f"{data.condition_emoji} **{data.location.name}** — "
            f"{data.temp_c:.0f}°C, {data.condition.lower()} · "
            f"humidity {data.humidity}% · wind {data.wind_speed_kmh:.0f} km/h",
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

    async def _compact_search(self, route: Route) -> tuple[str, list[discord.Embed]]:
        utility = self.bot.get_cog("Utility")
        subject = route.payload.strip()
        if utility is None or not subject:
            return "", []
        try:
            answer = await asyncio.wait_for(
                utility.answer_engine.answer(
                    subject, fresh=route.fresh or None, reason_complex=route.reason_complex
                ),
                timeout=30,
            )
        except Exception:  # noqa: BLE001 - CancelledError is a BaseException
            log.debug("Combined search part failed", exc_info=True)
            return "🔎 I couldn't reach the web search right now.", []

        answer = await self._maybe_synthesize(answer, subject, route)
        text = answer.direct_answer or answer.summary
        if not text and answer.key_points:
            text = answer.key_points[0]
        if not text:
            return f"🔎 nothing solid for **{shorten(subject, 60)}** right now.", []

        line = f"🔎 **{shorten(subject, 60)}** — {shorten(text, 400)}"
        names: list[str] = []
        for item in answer.sources[:3]:
            if item.source and item.source not in names:
                names.append(item.source)
        if names:
            line += "\n-# sources: " + " · ".join(names)
        return line, []

    # -- command metadata --------------------------------------------------

    async def _command_help(self, message: discord.Message, route: Route) -> bool:
        name = route.payload.casefold().lstrip("/")
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

        # Not a dictionary word (or not found): the web answers better.
        return await self._search(message, route, query=f"what is {route.payload}")

    # -- conversion ----------------------------------------------------------

    async def _convert(self, message: discord.Message, route: Route) -> bool:
        utility = self.bot.get_cog("Utility")
        if utility is None:
            return False
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
            await self._quiet_reply(message, NO_IMAGE_LINE)
            return True

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
        except Exception:  # noqa: BLE001 - card art is optional
            log.debug("Weather card render failed, using embed", exc_info=True)
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

    # -- web search ---------------------------------------------------------------------

    async def _search(
        self, message: discord.Message, route: Route, *, query: str | None = None
    ) -> bool:
        utility = self.bot.get_cog("Utility")
        if utility is None:
            return False
        subject = (query or route.payload or "").strip()
        if not subject:
            await self._quiet_reply(message, "search what, exactly?")
            return True

        try:
            placeholder = await message.reply(
                "🔎 searching the web…", mention_author=False
            )
        except discord.DiscordException:
            return False

        try:
            answer = await asyncio.wait_for(
                utility.answer_engine.answer(
                    subject, fresh=route.fresh or None, reason_complex=route.reason_complex
                ),
                timeout=30,
            )
        except Exception:  # noqa: BLE001
            log.debug("Mention search failed", exc_info=True)
            await self._quiet_edit(placeholder, SEARCH_FAIL_LINE)
            return True

        # Optional grounded synthesis: the AI may *explain* what was
        # retrieved, never add to it.
        answer = await self._maybe_synthesize(answer, subject, route)

        # AnswerEngine already performed the native retrieval and exposes the
        # evidence needed by both the card and its browse view. Never repeat
        # the same network search here.
        response = SearchResponse(
            query=answer.query,
            results=list(answer.sources),
            engine=answer.engine,
            cached=answer.cached,
        )
        embed = build_answer_embed(answer)
        view = SearchAnswerView(answer, response, author_id=message.author.id)
        try:
            await placeholder.edit(content=None, embed=embed, view=view)
            view.message = placeholder
        except (discord.DiscordException, AttributeError):
            log.debug("Could not deliver search answer", exc_info=True)
        return True

    async def _maybe_synthesize(self, answer, question: str, route: Route):
        """Let the AI write the answer *from the retrieved sources only*.

        Used when the deterministic extractor produced something thin, or
        when the question needs reasoning over the facts. Rules:

        - no sources → no AI call at all (never fabricate live data);
        - sources are untrusted web text, so they are sanitized and the
          model is told to treat them strictly as data (see
          ``functions/gemini.py``);
        - any failure silently keeps the deterministic answer.
        """
        if not getattr(answer, "has_sources", False):
            return answer
        weak = not answer.direct_answer and len(answer.summary or "") < _WEAK_SUMMARY_CHARS
        if not (route.reason_complex or weak):
            return answer
        ai = getattr(self.bot, "ai", None)
        if ai is None or not getattr(ai, "available", False):
            return answer

        facts = [
            f"{sanitize_untrusted(item.title, 120)} "
            f"[{sanitize_untrusted(item.source, 60)}] "
            f"{sanitize_untrusted(item.snippet, _SYNTH_SNIPPET)}".strip()
            for item in answer.sources[:_SYNTH_SOURCES]
            if (item.title or item.snippet)
        ]
        if not facts:
            return answer

        try:
            written = await asyncio.wait_for(
                ai.reason(question, facts, max_tokens=260), timeout=_SYNTH_TIMEOUT
            )
        except asyncio.CancelledError:
            raise
        except (ParadiseError, asyncio.TimeoutError):
            log.debug("Grounded synthesis unavailable; keeping native answer")
            return answer
        except Exception:  # noqa: BLE001 - synthesis is an upgrade, not a need
            log.warning("Grounded synthesis failed", exc_info=True)
            return answer

        written = (written or "").strip()
        if written:
            answer.summary = written
            answer.generated_by = "gemini"
        return answer

    # -- AI (the last resort) ---------------------------------------------------------------

    async def _ai(self, message: discord.Message, text: str) -> bool:
        ai = self.bot.ai
        if not ai.available:
            now = time.monotonic()
            last = self._fallback_last.get(message.channel.id)
            if last is not None and now - last < _FALLBACK_NOTE_COOLDOWN:
                return True
            self._fallback_last[message.channel.id] = now
            await self._quiet_reply(
                message,
                "my AI brain isn't configured here, but the tools work! try:\n"
                "*search <topic>* · *define <word>* · *convert 100 USD to INR* ·\n"
                "*weather in <city>* · *recommend anime like <title>* — or `/help`.",
            )
            return True
        if ai.cooldown_remaining(message) > 0:
            return True
        try:
            answer = await asyncio.wait_for(ai.respond(message), timeout=60)
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
