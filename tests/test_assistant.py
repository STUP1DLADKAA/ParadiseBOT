"""Dispatch tests for the mention assistant (commands/assistant.py).

Everything is faked at the Discord and service boundaries — these tests
prove that a mention lands on the right tool, that tool failures degrade
gracefully, and that AI stays the last resort.
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from commands.assistant import Assistant  # noqa: E402
from functions.conversion.models import ConversionCategory, ConversionResult  # noqa: E402
from functions.helpers import ParadiseError  # noqa: E402
from functions.images import ImageResponse, ImageResult  # noqa: E402
from functions.intent import IntentGuess  # noqa: E402
from functions.router import AI, CALCULATE, IMAGE, SEARCH, Route  # noqa: E402
from functions.weather.models import LocationInfo, WeatherData  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakePermissions:
    send_messages = True


class FakeChannel:
    id = 55

    def permissions_for(self, _member):
        return FakePermissions()


class FakeMessage:
    next_id = 0

    def __init__(self, content="", *, addressed=True):
        FakeMessage.next_id += 1
        self.id = FakeMessage.next_id
        self.content = content
        self.author = SimpleNamespace(id=7, bot=False, display_name="Tester")
        self.guild = SimpleNamespace(id=1, me=SimpleNamespace())
        self.channel = FakeChannel()
        self.mentions = []
        self.replies = []
        self.edits = []
        self._addressed = addressed

    async def reply(self, content=None, mention_author=False, **kwargs):
        self.replies.append({"content": content, **kwargs})
        placeholder = FakeMessage(addressed=False)
        return placeholder


class FakeAI:
    def __init__(self, *, available=False, answer="ai answer", tease_error=None):
        self.available = available
        self._answer = answer
        self.tease_error = tease_error
        self.responded_to = []
        self.questions = []
        self.classified = []
        self.teased = []

    def should_reply_to(self, message):
        return message._addressed

    def extract_question(self, message):
        return message.content

    def cooldown_remaining(self, message):
        return 0

    async def respond(self, message):
        self.responded_to.append(message.content)
        return self._answer

    async def answer_question(self, question, *, asker="someone", channel_id=0,
                              max_tokens=None):
        self.questions.append(question)
        return self._answer

    async def classify(self, request, max_tokens=120):
        self.classified.append(request)
        return ""

    async def summarize(self, lines, channel_name):
        self.summarized = (list(lines), channel_name)
        return self._answer

    async def tease(self, name, text="", *, target_id=0, max_tokens=140):
        self.teased.append((name, text, target_id))
        if self.tease_error is not None:
            raise self.tease_error
        return self._answer

    def reset(self):
        self.classified = []


class FakeConversionEngine:
    def __init__(self, error=None):
        self.error = error
        self.queries = []

    async def convert_query(self, query):
        self.queries.append(query)
        if self.error:
            raise self.error
        return ConversionResult(
            from_value=100.0, from_unit="USD", from_symbol="$",
            to_value=8320.4, to_unit="INR", to_symbol="₹",
            category=ConversionCategory.CURRENCY, rate_info="1 USD ≈ 83.20 INR",
        )


class FakeWeatherService:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    async def get_weather(self, city):
        self.calls.append(city)
        if self.error is not None:
            raise self.error
        return WeatherData(
            location=LocationInfo(name=city, country="India", timezone="Asia/Kolkata"),
            temp_c=29.0,
            feels_like_c=31.0,
            condition="Partly cloudy",
            condition_emoji="⛅",
            humidity=61,
            wind_speed_kmh=12.0,
            wind_direction="NE",
            forecast_text="Partly cloudy through the evening.",
        )


class FakeUtilityCog:
    def __init__(self):
        self.conversion_engine = FakeConversionEngine()
        self.weather_service = FakeWeatherService()
        self.db = SimpleNamespace(get_user_timezone=lambda _id: None)
        self.reminders = []
        self.polls = []

    async def create_reminder(self, user_id, channel_id, minutes, text):
        self.reminders.append((user_id, channel_id, minutes, text))
        return True, f"got it. I'll remind you in {minutes} minutes."

    async def deploy_poll(self, channel, question, choices, starter):
        self.polls.append((question, tuple(choices), starter))
        return True


class FakeBot:
    def __init__(self, *, ai):
        self.ai = ai
        self.user = SimpleNamespace(id=42)
        self._cogs = {}

    def add(self, name, cog):
        self._cogs[name] = cog

    def get_cog(self, name):
        return self._cogs.get(name)


def make_assistant(*, ai=None, utility=True) -> tuple[Assistant, FakeBot]:
    bot = FakeBot(ai=ai or FakeAI())
    if utility:
        bot.add("Utility", FakeUtilityCog())
    return Assistant(bot), bot


def mention(text: str) -> FakeMessage:
    return FakeMessage(text, addressed=True)


class TestAssistantDispatch(unittest.IsolatedAsyncioTestCase):
    async def test_roast_me_roasts_the_asker(self):
        assistant, _ = make_assistant()
        message = mention("roast me")
        handled = await assistant.handle_message(message)
        self.assertTrue(handled)
        text = message.replies[0]["content"]
        self.assertIn("roasting myself", text)
        self.assertIn("Tester", text)

    async def test_roast_a_mentioned_member(self):
        assistant, _ = make_assistant()
        message = mention("roast <@555>")
        message.guild = SimpleNamespace(
            id=1,
            me=SimpleNamespace(),
            get_member=lambda member_id: SimpleNamespace(
                id=member_id, display_name="Rohan", name="Rohan"
            )
            if member_id == 555
            else None,
        )
        handled = await assistant.handle_message(message)
        self.assertTrue(handled)
        self.assertIn("Rohan", message.replies[0]["content"])

    async def test_roast_a_named_target(self):
        assistant, _ = make_assistant()
        message = mention("roast the guy who lost")
        handled = await assistant.handle_message(message)
        self.assertTrue(handled)
        self.assertIn("guy who lost", message.replies[0]["content"])

    async def test_roast_uses_the_ai_tease_when_it_is_awake(self):
        ai = FakeAI(available=True)
        assistant, _ = make_assistant(ai=ai)
        message = mention("roast the guy who lost")
        handled = await assistant.handle_message(message)
        self.assertTrue(handled)
        # The AI tier answered, in the target's direction.
        self.assertEqual(message.replies[0]["content"], "ai answer")
        self.assertEqual(len(ai.teased), 1)
        self.assertIn("guy who lost", ai.teased[0][0])

    async def test_roast_falls_back_to_the_book_when_the_ai_tease_fails(self):
        ai = FakeAI(available=True, tease_error=RuntimeError("brain offline"))
        assistant, _ = make_assistant(ai=ai)
        message = mention("roast the guy who lost")
        handled = await assistant.handle_message(message)
        self.assertTrue(handled)
        sent = message.replies[0]["content"]
        self.assertIn("guy who lost", sent)  # the roast book answered instead

    async def test_unaddressed_messages_ignored(self):
        assistant, _ = make_assistant()
        handled = await assistant.handle_message(FakeMessage("25% of 800", addressed=False))
        self.assertFalse(handled)

    async def test_calculate(self):
        assistant, _ = make_assistant()
        message = mention("25% of 800")
        handled = await assistant.handle_message(message)
        self.assertTrue(handled)
        self.assertTrue(message.replies)
        self.assertIn("200", message.replies[0]["content"])
        self.assertIn("25", message.replies[0]["content"])

    async def test_calculate_error_is_friendly(self):
        assistant, _ = make_assistant()
        message = mention("10/0")
        handled = await assistant.handle_message(message)
        self.assertTrue(handled)
        self.assertIn("zero", message.replies[0]["content"])

    async def test_convert_uses_conversion_engine(self):
        assistant, bot = make_assistant()
        message = mention("convert 100 USD to INR")
        handled = await assistant.handle_message(message)
        self.assertTrue(handled)
        engine = bot.get_cog("Utility").conversion_engine
        self.assertEqual(engine.queries, ["100 USD to INR"])
        self.assertIn("embed", message.replies[0])

    async def test_no_ai_call_for_tool_answers(self):
        ai = FakeAI(available=True)
        assistant, _ = make_assistant(ai=ai)
        for text in ("25% of 800", "convert 10 km to miles", "weather", "help"):
            await assistant.handle_message(mention(text))
        self.assertEqual(ai.responded_to, [])  # tools never touch AI

    async def test_search_flow_is_forwarded_to_the_ai(self):
        ai = FakeAI(available=True, answer="Minecraft 1.22? absolutely real, trust me.")
        assistant, bot = make_assistant(ai=ai)
        message = mention("search latest Minecraft news")
        handled = await assistant.handle_message(message)
        self.assertTrue(handled)
        # thinking placeholder first, then the AI's answer replaces it…
        self.assertIn("thinking", message.replies[0]["content"].casefold())
        self.assertEqual(ai.questions, ["latest Minecraft news"])
        self.assertEqual(ai.responded_to, [])  # search path, not the chat path

    async def test_search_without_ai_gets_the_fallback_note(self):
        assistant, _ = make_assistant(ai=FakeAI(available=False))
        message = mention("search latest Minecraft news")
        handled = await assistant.handle_message(message)
        self.assertTrue(handled)
        self.assertIn("isn't configured", message.replies[0]["content"])

    async def test_weather_asks_for_city(self):
        assistant, _ = make_assistant()
        message = mention("weather")
        await assistant.handle_message(message)
        self.assertIn("which city", message.replies[0]["content"])

    async def test_time_unknown_place_friendly(self):
        assistant, _ = make_assistant()
        message = mention("time in the upside down")
        await assistant.handle_message(message)
        self.assertIn("don't recognize", message.replies[0]["content"])

    async def test_help_shows_quick_uses(self):
        assistant, _ = make_assistant()
        message = mention("help")
        await assistant.handle_message(message)
        self.assertIn("define", message.replies[0]["content"])

    async def test_empty_mention_shows_hint(self):
        assistant, ai = make_assistant()
        message = FakeMessage("", addressed=True)
        await assistant.handle_message(message)
        self.assertIn("hey!", message.replies[0]["content"])

    async def test_conversational_uses_ai_when_available(self):
        ai = FakeAI(available=True, answer="hey, what's up?")
        assistant, _ = make_assistant(ai=ai)
        message = mention("hey")
        await assistant.handle_message(message)
        self.assertEqual(ai.responded_to, ["hey"])
        self.assertIn("what's up", message.replies[0]["content"])

    async def test_conversational_without_ai_gets_tool_hint(self):
        ai = FakeAI(available=False)
        assistant, _ = make_assistant(ai=ai)
        message = mention("hey friend")
        await assistant.handle_message(message)
        self.assertIn("tools work", message.replies[0]["content"])

    async def test_ai_error_is_shown_friendly(self):
        class FlakyAI(FakeAI):
            async def respond(self, message):
                raise ParadiseError("gemini is cooling down")

        assistant, _ = make_assistant(ai=FlakyAI(available=True))
        message = mention("hey")
        await assistant.handle_message(message)
        self.assertIn("cooling down", message.replies[0]["content"])

    async def test_rate_limit_stays_quiet(self):
        assistant, _ = make_assistant()
        first = mention("25% of 800")
        second = mention("25% of 800")
        await assistant.handle_message(first)
        await assistant.handle_message(second)  # same user, immediately
        self.assertEqual(len(second.replies), 0)

    async def test_translate_unknown_language(self):
        assistant, _ = make_assistant()
        message = mention("translate hello into klingon")
        await assistant.handle_message(message)
        self.assertIn("don't know the language", message.replies[0]["content"])

    def test_never_used_is_never_cooled(self):
        """Regression: monotonic time starts at 0 at boot — "never seen" must
        not read as "seen 0 seconds ago" and silence the very first mention."""
        import commands.assistant as assistant_module

        assistant, _ = make_assistant()
        message = mention("anything")
        original = assistant_module.time.monotonic
        assistant_module.time.monotonic = lambda: 1.0  # one second after boot
        try:
            self.assertTrue(assistant._cooled(message))
        finally:
            assistant_module.time.monotonic = original


class FakeImageService:
    """Stands in for the real web image search."""

    def __init__(self, *, results=1, error=None):
        self.error = error
        self._results = results
        self.calls: list[tuple[str, int]] = []

    async def search(self, query, limit=1):
        self.calls.append((query, limit))
        if self.error is not None:
            raise self.error
        return ImageResponse(
            query=query,
            provider="Wikimedia Commons",
            results=[
                ImageResult(
                    title=f"{query} {index}",
                    image_url=f"https://upload.wikimedia.org/{index}.jpg",
                    source_url="https://commons.wikimedia.org/wiki/File:x.jpg",
                    provider="Wikimedia Commons",
                )
                for index in range(min(self._results, limit))
            ],
        )


class TestImageDispatch(unittest.IsolatedAsyncioTestCase):
    """Picture requests search the web — they never generate an image."""

    async def test_picture_request_embeds_a_real_web_image(self):
        assistant, _ = make_assistant()
        assistant.images = FakeImageService()
        message = mention("show me a picture of a red panda")
        self.assertTrue(await assistant.handle_message(message))

        self.assertEqual(assistant.images.calls, [("red panda", 1)])
        embeds = message.replies[0]["embeds"]
        self.assertEqual(len(embeds), 1)
        self.assertEqual(embeds[0].image.url, "https://upload.wikimedia.org/0.jpg")
        self.assertIn("red panda", embeds[0].title)

    async def test_multiple_images_are_requested_and_bounded(self):
        assistant, _ = make_assistant()
        assistant.images = FakeImageService(results=5)
        message = mention("show me 3 pictures of Minecraft villages")
        await assistant.handle_message(message)
        self.assertEqual(assistant.images.calls, [("Minecraft villages", 3)])
        self.assertEqual(len(message.replies[0]["embeds"]), 3)

    async def test_image_request_never_calls_the_ai(self):
        ai = FakeAI(available=True)
        assistant, _ = make_assistant(ai=ai)
        assistant.images = FakeImageService()
        await assistant.handle_message(mention("find me a picture of the Eiffel Tower"))
        self.assertEqual(ai.responded_to, [])

    async def test_no_results_is_a_friendly_line(self):
        assistant, _ = make_assistant()
        assistant.images = FakeImageService(results=0)
        message = mention("picture of a flurbleglorp")
        await assistant.handle_message(message)
        self.assertIn("couldn't find a usable image", message.replies[0]["content"])

    async def test_provider_failure_is_contained(self):
        assistant, _ = make_assistant()
        assistant.images = FakeImageService(error=RuntimeError("provider down"))
        message = mention("picture of a wolf")
        self.assertTrue(await assistant.handle_message(message))
        self.assertIn("couldn't find a usable image", message.replies[0]["content"])

    async def test_provider_timeout_is_contained(self):
        assistant, _ = make_assistant()
        assistant.images = FakeImageService(error=asyncio.TimeoutError())
        message = mention("picture of a wolf")
        self.assertTrue(await assistant.handle_message(message))
        self.assertIn("couldn't find a usable image", message.replies[0]["content"])

    async def test_no_subject_asks_for_one(self):
        assistant, _ = make_assistant()
        assistant.images = FakeImageService()
        message = mention("x")
        await assistant._image(message, Route(kind=IMAGE, payload="  "))
        self.assertIn("a picture of what", message.replies[0]["content"])
        self.assertEqual(assistant.images.calls, [])


class TestCombinedRequests(unittest.IsolatedAsyncioTestCase):
    """Multi-tool asks run concurrently and answer in one message."""

    async def test_search_plus_conversion(self):
        ai = FakeAI(available=True, answer="The Minecraft update is huge.")
        assistant, bot = make_assistant(ai=ai)
        message = mention(
            "search the latest Minecraft update and convert 100 USD to INR"
        )
        self.assertTrue(await assistant.handle_message(message))

        content = message.replies[0]["content"]
        self.assertIn("Minecraft update", content)
        self.assertIn("INR", content)
        utility = bot.get_cog("Utility")
        self.assertEqual(utility.conversion_engine.queries, ["100 USD to INR"])
        self.assertEqual(len(ai.questions), 1)

    async def test_calculation_plus_image(self):
        assistant, _ = make_assistant()
        assistant.images = FakeImageService()
        message = mention("show me a picture of a wolf and what's 15% of 847")
        await assistant.handle_message(message)

        reply = message.replies[0]
        self.assertIn("127", reply["content"])  # 15% of 847 = 127.05
        self.assertEqual(len(reply["embeds"]), 1)

    async def test_one_failing_part_does_not_sink_the_other(self):
        assistant, bot = make_assistant()
        bot.get_cog("Utility").conversion_engine.error = RuntimeError("rates down")
        message = mention("what is 2+2 and convert 100 USD to INR")
        self.assertTrue(await assistant.handle_message(message))
        content = message.replies[0]["content"]
        self.assertIn("4", content)
        self.assertIn("convert", content.casefold())

    async def test_parts_run_concurrently(self):
        assistant, bot = make_assistant()
        running = 0
        peak = 0

        async def slow_convert(query):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.02)
            running -= 1
            return ConversionResult(
                from_value=1.0, from_unit="USD", from_symbol="$",
                to_value=83.2, to_unit="INR", to_symbol="₹",
                category=ConversionCategory.CURRENCY,
            )

        bot.get_cog("Utility").conversion_engine.convert_query = slow_convert
        await assistant.handle_message(
            mention("convert 1 USD to INR and convert 2 USD to INR")
        )
        self.assertEqual(peak, 2)

    async def test_combined_reply_is_a_single_message(self):
        assistant, _ = make_assistant()
        message = mention("weather in Delhi and what is 2+2")
        await assistant.handle_message(message)
        self.assertEqual(len(message.replies), 1)


class TestSearchForwardsToAI(unittest.IsolatedAsyncioTestCase):
    """A search request goes straight to the model — no web retrieval."""

    async def test_question_reaches_answer_question(self):
        ai = FakeAI(available=True, answer="here's the answer")
        assistant, _ = make_assistant(ai=ai)
        message = mention("who is the president of France")
        self.assertTrue(await assistant.handle_message(message))
        self.assertEqual(len(ai.questions), 1)
        self.assertIn("president of france", ai.questions[0].casefold())

    async def test_placeholder_is_replaced_by_the_answer(self):
        ai = FakeAI(available=True, answer="the answer, delivered")
        assistant, _ = make_assistant(ai=ai)
        message = mention("search latest Minecraft news")
        edits = []
        original = assistant._quiet_edit

        async def record(target, content="", **kwargs):
            edits.append(content)
            await original(target, content, **kwargs)

        assistant._quiet_edit = record
        await assistant.handle_message(message)
        self.assertTrue(edits)
        self.assertIn("the answer, delivered", edits[-1])

    async def test_ai_failure_is_reported_not_guessed(self):
        class BrokenAI(FakeAI):
            async def answer_question(self, question, *, asker="someone",
                                      channel_id=0, max_tokens=None):
                self.questions.append(question)
                raise ParadiseError("the AI isn't responding right now.")

        ai = BrokenAI(available=True)
        assistant, _ = make_assistant(ai=ai)
        message = mention("search latest Minecraft news")
        edits = []
        assistant._quiet_edit = lambda target, content="", **kwargs: edits.append(content)
        self.assertTrue(await assistant.handle_message(message))
        self.assertTrue(edits)
        self.assertIn("isn't responding", edits[0])

    async def test_unexpected_failure_gets_the_short_line(self):
        class ExplodingAI(FakeAI):
            async def answer_question(self, question, *, asker="someone",
                                      channel_id=0, max_tokens=None):
                raise RuntimeError("provider exploded")

        ai = ExplodingAI(available=True)
        assistant, _ = make_assistant(ai=ai)
        message = mention("search latest Minecraft news")
        edits = []
        assistant._quiet_edit = lambda target, content="", **kwargs: edits.append(content)
        self.assertTrue(await assistant.handle_message(message))
        self.assertTrue(edits)
        self.assertIn("isn't answering", edits[0])

    async def test_oversized_answer_is_split_not_truncated(self):
        ai = FakeAI(available=True, answer="word " * 900)
        assistant, _ = make_assistant(ai=ai)
        message = mention("search the entire history of rome")
        await assistant.handle_message(message)
        # one placeholder + at least one continuation
        self.assertGreater(len(message.replies), 1)

    async def test_no_subject_asks_for_one(self):
        ai = FakeAI(available=True)
        assistant, _ = make_assistant(ai=ai)
        message = mention("x")
        await assistant._search(message, Route(kind=SEARCH, payload="  "))
        self.assertIn("search what, exactly", message.replies[0]["content"])
        self.assertEqual(ai.questions, [])

    async def test_define_fallback_forwards_the_phrase(self):
        ai = FakeAI(available=True, answer="a word that means fleeting")
        assistant, _ = make_assistant(ai=ai)
        message = mention("define flurbleglorp")
        self.assertTrue(await assistant.handle_message(message))
        self.assertEqual(len(ai.questions), 1)
        self.assertIn("flurbleglorp", ai.questions[0])


class TestAssistantSafety(unittest.IsolatedAsyncioTestCase):
    """Self-loop protection, rate limiting and oversized output."""

    async def test_bot_messages_are_ignored(self):
        assistant, _ = make_assistant(ai=FakeAI(available=True))
        message = mention("show me a picture of a wolf")
        message.author = SimpleNamespace(id=42, bot=True, display_name="Paradise")
        self.assertFalse(await assistant.handle_message(message))
        self.assertEqual(message.replies, [])

    async def test_bot_cannot_answer_its_own_message(self):
        assistant, bot = make_assistant(ai=FakeAI(available=True))
        own = mention("25% of 800")
        own.author = SimpleNamespace(id=bot.user.id, bot=True, display_name="Paradise")
        self.assertFalse(await assistant.handle_message(own))

    async def test_dm_without_guild_is_ignored(self):
        assistant, _ = make_assistant()
        message = mention("25% of 800")
        message.guild = None
        self.assertFalse(await assistant.handle_message(message))

    async def test_rate_limited_user_gets_no_external_calls(self):
        assistant, _ = make_assistant()
        assistant.images = FakeImageService()
        first = mention("picture of a wolf")
        second = mention("picture of a cat")
        await assistant.handle_message(first)
        await assistant.handle_message(second)
        self.assertEqual(len(assistant.images.calls), 1)  # second was throttled
        self.assertEqual(second.replies, [])

    async def test_cooldown_maps_stay_bounded(self):
        assistant, _ = make_assistant()
        for index in range(900):
            assistant._user_last[index] = 0.0
            assistant._channel_last[index] = (0.0, index)
        assistant._cooled(mention("hi"))
        self.assertLessEqual(len(assistant._user_last), 600)
        self.assertLessEqual(len(assistant._channel_last), 600)

    async def test_oversized_answer_is_split_not_dropped(self):
        ai = FakeAI(available=True, answer="word " * 1200)
        assistant, _ = make_assistant(ai=ai)
        message = mention("tell me a very long story please")
        await assistant.handle_message(message)
        self.assertGreater(len(message.replies), 1)
        for reply in message.replies:
            self.assertLessEqual(len(reply["content"]), 2000)
        joined = "".join(reply["content"] for reply in message.replies)
        self.assertIn("word", joined)

    async def test_empty_query_calls_no_external_service(self):
        ai = FakeAI(available=True)
        assistant, _ = make_assistant(ai=ai)
        assistant.images = FakeImageService()
        message = mention("")
        await assistant.handle_message(message)
        self.assertEqual(assistant.images.calls, [])
        self.assertEqual(ai.questions, [])
        self.assertEqual(ai.responded_to, [])


class TestToolkitByMention(unittest.IsolatedAsyncioTestCase):
    """@Paradise + the free toolkit: dice, qr, remind, poll, whois, avatar."""

    async def test_dice_by_mention(self):
        assistant, _ = make_assistant()
        message = mention("roll 2d20+3")
        self.assertTrue(await assistant.handle_message(message))
        text = message.replies[0]["content"]
        self.assertIn("🎲", text)
        self.assertIn("**", text)

    async def test_reverse_by_mention(self):
        assistant, _ = make_assistant()
        message = mention("reverse hello world")
        self.assertTrue(await assistant.handle_message(message))
        self.assertIn("dlrow olleh", message.replies[0]["content"])

    async def test_password_by_mention_mentions_nothing(self):
        assistant, _ = make_assistant()
        message = mention("generate a strong 24 character password")
        self.assertTrue(await assistant.handle_message(message))
        self.assertIn("🔒", message.replies[0]["content"])

    async def test_qr_by_mention_attaches_a_png(self):
        assistant, _ = make_assistant()
        message = mention("qr code for https://example.com")
        self.assertTrue(await assistant.handle_message(message))
        reply = message.replies[0]
        self.assertIn("QR", reply["content"])
        file = reply.get("file")
        self.assertIsNotNone(file)
        self.assertEqual(file.filename, "qr.png")

    async def test_countdown_by_mention(self):
        assistant, _ = make_assistant()
        message = mention("days until christmas")
        self.assertTrue(await assistant.handle_message(message))
        self.assertIn("Christmas", message.replies[0]["content"])

    async def test_remind_by_mention_uses_the_reminder_engine(self):
        assistant, bot = make_assistant()
        utility = bot.get_cog("Utility")
        message = mention("remind me in 20 minutes to check the oven")
        self.assertTrue(await assistant.handle_message(message))
        self.assertEqual(len(utility.reminders), 1)
        user_id, channel_id, minutes, text = utility.reminders[0]
        self.assertEqual(minutes, 20)
        self.assertEqual(text, "check the oven")
        self.assertIn("remind you", message.replies[0]["content"])

    async def test_remind_with_no_time_asks_for_one(self):
        assistant, bot = make_assistant()
        utility = bot.get_cog("Utility")
        message = mention("remind me to stretch")
        self.assertTrue(await assistant.handle_message(message))
        self.assertEqual(utility.reminders, [])
        self.assertIn("when?", message.replies[0]["content"])

    async def test_poll_by_mention_deploys_the_poll(self):
        assistant, bot = make_assistant()
        utility = bot.get_cog("Utility")
        message = mention("poll: pizza or burgers")
        self.assertTrue(await assistant.handle_message(message))
        self.assertEqual(len(utility.polls), 1)
        question, choices, starter = utility.polls[0]
        self.assertEqual(choices, ("pizza", "burgers"))
        self.assertEqual(starter, "Tester")

    async def test_whois_by_mention_cards_the_mentioned_member(self):
        assistant, _ = make_assistant()
        message = mention("who is <@77>")
        target = SimpleNamespace(
            id=77, bot=False, display_name="Taher",
            created_at=__import__("datetime").datetime(2020, 1, 1),
            joined_at=__import__("datetime").datetime(2024, 6, 1),
            roles=[SimpleNamespace(name="@everyone"), SimpleNamespace(name="lords")],
            display_avatar=SimpleNamespace(url="https://avatar"),
        )
        message.mentions = [target]
        self.assertTrue(await assistant.handle_message(message))
        embed = message.replies[0].get("embed")
        self.assertIsNotNone(embed)
        self.assertIn("Taher", embed.title)

    async def test_avatar_by_mention_shows_the_asker_without_a_mention(self):
        assistant, _ = make_assistant()
        message = mention("avatar")
        self.assertTrue(await assistant.handle_message(message))
        embed = message.replies[0].get("embed")
        self.assertIsNotNone(embed)

    async def test_server_info_by_mention(self):
        assistant, _ = make_assistant()
        message = mention("server info")
        message.guild = SimpleNamespace(
            id=1,
            me=SimpleNamespace(),
            name="Paradise",
            member_count=42,
            created_at=__import__("datetime").datetime(2021, 5, 1),
            premium_subscription_count=2,
            owner=SimpleNamespace(display_name="Stupid"),
            icon=None,
        )
        self.assertTrue(await assistant.handle_message(message))
        embed = message.replies[0].get("embed")
        self.assertIsNotNone(embed)
        self.assertIn("Paradise", embed.title)

    async def test_compound_dice_and_countdown(self):
        assistant, _ = make_assistant()
        message = mention("roll a d20 and days until christmas")
        self.assertTrue(await assistant.handle_message(message))
        text = message.replies[0]["content"]
        self.assertIn("🎲", text)
        self.assertIn("Christmas", text)

    async def test_compound_weather_and_reminder(self):
        # The reminder part sends its own message; the weather part is
        # compacted — and the city no longer eats the reminder text.
        assistant, bot = make_assistant()
        utility = bot.get_cog("Utility")
        message = mention("weather in delhi and remind me in 20 minutes to eat")
        self.assertTrue(await assistant.handle_message(message))
        self.assertEqual(len(utility.reminders), 1)
        self.assertEqual(utility.reminders[0][2], 20)
        self.assertEqual(utility.reminders[0][3], "eat")
        # The combined reply still carries the weather part.
        text = "\n".join(str(reply.get("content")) for reply in message.replies)
        self.assertIn("delhi", text.lower())

    async def test_compound_reminder_and_dice(self):
        assistant, bot = make_assistant()
        utility = bot.get_cog("Utility")
        message = mention("remind me in 30 minutes to stretch and roll a d20")
        self.assertTrue(await assistant.handle_message(message))
        self.assertEqual(len(utility.reminders), 1)
        self.assertEqual(utility.reminders[0][3], "stretch")
        text = "\n".join(str(reply.get("content")) for reply in message.replies)
        self.assertIn("🎲", text)


class TestMediaAndRecapByMention(unittest.IsolatedAsyncioTestCase):
    """@Paradise meme / gif / summarize — no slash command needed."""

    @staticmethod
    def _history_channel(messages, *, name="general"):
        class _HistoryChannel:
            def __init__(self):
                self.id = 55
                self.name = name

            def permissions_for(self, _member):
                return FakePermissions()

            def history(self, limit=None):
                async def _gen():
                    for message in messages:
                        yield message

                return _gen()

        return _HistoryChannel()

    @staticmethod
    def _past(author, content):
        return SimpleNamespace(
            author=SimpleNamespace(bot=False, display_name=author),
            content=content,
        )

    async def test_summarize_recaps_the_channel(self):
        ai = FakeAI(available=True, answer="they argued about pizza for an hour")
        assistant, _ = make_assistant(ai=ai)
        message = mention("summarize")
        message.channel = self._history_channel(
            [self._past(f"User{index}", f"message {index}") for index in range(6)]
        )
        handled = await assistant.handle_message(message)
        self.assertTrue(handled)
        text = message.replies[0]["content"]
        self.assertIn("recap of the last 6 messages", text)
        self.assertIn("pizza", text)

    async def test_summarize_without_enough_chat_says_so(self):
        ai = FakeAI(available=True)
        assistant, _ = make_assistant(ai=ai)
        message = mention("what did i miss")
        message.channel = self._history_channel(
            [self._past("Ash", "hi"), self._past("Rohan", "hello")]
        )
        handled = await assistant.handle_message(message)
        self.assertTrue(handled)
        self.assertIn("enough recent conversation", message.replies[0]["content"])

    async def test_summarize_without_ai_says_so(self):
        assistant, _ = make_assistant(ai=FakeAI(available=False))
        message = mention("tl;dr")
        handled = await assistant.handle_message(message)
        self.assertTrue(handled)
        self.assertIn("isn't configured", message.replies[0]["content"])

    async def test_a_meme_request_posts_the_feed_meme(self):
        class FakeMeme:
            pass

        class FakeFeed:
            async def pick(self):
                return FakeMeme()

        assistant, _ = make_assistant()
        assistant.bot.personality = SimpleNamespace(meme_feed=FakeFeed())
        with mock.patch(
            "commands.assistant.meme_embed", return_value="MEME EMBED"
        ) as embed:
            message = mention("send me a meme")
            handled = await assistant.handle_message(message)
        self.assertTrue(handled)
        embed.assert_called_once()
        self.assertEqual(message.replies[0].get("embed"), "MEME EMBED")

    async def test_a_meme_request_without_the_feed_draws_one(self):
        assistant, _ = make_assistant()
        assistant.bot.personality = SimpleNamespace(
            meme_feed=SimpleNamespace(pick=None)  # no usable feed
        )
        png = b"fake png bytes"
        with mock.patch(
            "commands.assistant.render_topic_meme", return_value=png
        ) as render:
            message = mention("give me a meme about exams")
            handled = await assistant.handle_message(message)
        self.assertTrue(handled)
        render.assert_called_once()
        topic_arg = render.call_args.args[0]
        self.assertEqual(topic_arg, "exams")
        self.assertIsNotNone(message.replies[0].get("file"))

    async def test_a_gif_request_searches_and_posts(self):
        class FakeSearch:
            available = True

            def __init__(self):
                self.queries = []

            async def search(self, query):
                self.queries.append(query)
                return "A GIF"

        search = FakeSearch()
        assistant, _ = make_assistant()
        assistant.bot.personality = SimpleNamespace(gif_search=search)
        with mock.patch(
            "commands.assistant.meme_embed", return_value="GIF EMBED"
        ):
            message = mention("show me a gif of a cat failing a jump")
            handled = await assistant.handle_message(message)
        self.assertTrue(handled)
        self.assertEqual(search.queries, ["cat failing a jump"])
        self.assertEqual(message.replies[0].get("embed"), "GIF EMBED")

    async def test_a_gif_request_with_no_result_says_so(self):
        class FakeSearch:
            available = True

            async def search(self, _query):
                return None

        assistant, _ = make_assistant()
        assistant.bot.personality = SimpleNamespace(gif_search=FakeSearch())
        message = mention("bruh gif")
        handled = await assistant.handle_message(message)
        self.assertTrue(handled)
        self.assertIn("came back empty", message.replies[0]["content"])

    async def test_a_gif_request_without_a_key_falls_back_to_a_meme(self):
        class FakeFeed:
            async def pick(self):
                return "ONLINE MEME"

        assistant, _ = make_assistant()
        assistant.bot.personality = SimpleNamespace(
            gif_search=SimpleNamespace(available=False),
            meme_feed=FakeFeed(),
        )
        with mock.patch(
            "commands.assistant.meme_embed", return_value="MEME EMBED"
        ):
            message = mention("gif")
            handled = await assistant.handle_message(message)
        self.assertTrue(handled)
        self.assertEqual(message.replies[0].get("embed"), "MEME EMBED")


class TestParallelRequests(unittest.IsolatedAsyncioTestCase):
    """Several members talking to the bot at once: everyone gets an answer.

    Discord dispatches every message as its own task; the bot's own gates
    must never turn that back into a queue where only the first person is
    answered and everyone else is silently ignored.
    """

    @staticmethod
    def _from(assistant_message, member_id, name):
        assistant_message.author = SimpleNamespace(id=member_id, bot=False, display_name=name)
        return assistant_message

    async def test_a_second_member_is_never_channel_blocked(self):
        assistant, _ = make_assistant()
        first = self._from(mention("hello there friend"), 1, "Ash")
        second = self._from(mention("hello there friend"), 2, "Rohan")
        self.assertTrue(assistant._cooled(first))
        # Different member, same channel, immediately after: straight through.
        self.assertTrue(assistant._cooled(second))

    async def test_the_same_member_is_still_paced(self):
        assistant, _ = make_assistant()
        first = self._from(mention("hello there friend"), 1, "Ash")
        self.assertTrue(assistant._cooled(first))
        self.assertFalse(assistant._cooled(self._from(mention("again"), 1, "Ash")))

    async def test_two_members_get_real_answers_back_to_back(self):
        assistant, _ = make_assistant()
        first = self._from(mention("25% of 800"), 1, "Ash")
        second = self._from(mention("15% of 200"), 2, "Rohan")
        handled_one = await assistant.handle_message(first)
        handled_two = await assistant.handle_message(second)
        self.assertTrue(handled_one)
        self.assertTrue(handled_two)
        self.assertIn("200", first.replies[0]["content"])
        self.assertIn("30", second.replies[0]["content"])

    async def test_a_short_ai_cooldown_is_waited_out_not_dropped(self):
        class SlowCooldownAI(FakeAI):
            def __init__(self):
                super().__init__(available=True)
                self.waited = []

            def cooldown_remaining(self, message):
                remaining = self._remaining.get(message.author.id, 0.0)
                self.waited.append(remaining)
                return remaining

        ai = SlowCooldownAI()
        ai._remaining = {2: 0.05}  # a parallel request finishing any moment
        assistant, _ = make_assistant(ai=ai)
        message = self._from(mention("what is a black hole"), 2, "Rohan")
        handled = await assistant.handle_message(message)
        self.assertTrue(handled)
        self.assertTrue(message.replies)  # waited, then answered — not dropped

        ai._remaining = {3: 30.0}  # the same member machine-gunning: quiet
        spam = self._from(mention("what is a black hole"), 3, "Spammer")
        handled = await assistant.handle_message(spam)
        self.assertTrue(handled)
        self.assertFalse(spam.replies)


class TestLiveDataHonesty(unittest.IsolatedAsyncioTestCase):
    """A failed live lookup is reported — never handed to the AI to guess."""

    async def test_weather_outage_says_so_and_never_asks_the_ai(self):
        ai = FakeAI(available=True)
        assistant, bot = make_assistant(ai=ai)
        bot.get_cog("Utility").weather_service.error = RuntimeError("provider down")
        message = mention("weather in Delhi")
        self.assertTrue(await assistant.handle_message(message))
        self.assertIn("weather service is unavailable", message.replies[0]["content"])
        self.assertEqual(ai.responded_to, [])

    async def test_weather_outage_in_a_combined_reply(self):
        ai = FakeAI(available=True)
        assistant, bot = make_assistant(ai=ai)
        bot.get_cog("Utility").weather_service.error = ParadiseError("no data")
        message = mention("weather in Delhi and what is 2+2")
        await assistant.handle_message(message)
        content = message.replies[0]["content"]
        self.assertIn("couldn't retrieve live weather", content)
        self.assertIn("4", content)      # the healthy part still answers
        self.assertEqual(ai.responded_to, [])

    async def test_search_outage_says_so(self):
        class CoolingAI(FakeAI):
            async def answer_question(self, question, *, asker="someone",
                                      channel_id=0, max_tokens=None):
                self.questions.append(question)
                raise ParadiseError("the AI is being moody. give it a minute.")

        ai = CoolingAI(available=True)
        assistant, _ = make_assistant(ai=ai)
        message = mention("search the latest Minecraft news")
        self.assertTrue(await assistant.handle_message(message))
        self.assertEqual(ai.questions, ["the latest Minecraft news"])
        self.assertEqual(ai.responded_to, [])

    async def test_healthy_weather_still_renders(self):
        assistant, bot = make_assistant()
        message = mention("weather in Delhi")
        await assistant.handle_message(message)
        self.assertEqual(bot.get_cog("Utility").weather_service.calls, ["Delhi"])
        self.assertTrue(message.replies)
        self.assertIn("file", message.replies[0])

    async def test_weather_card_failure_uses_textual_embed_fallback(self):
        assistant, _bot = make_assistant()
        message = mention("weather in Delhi")
        with patch("commands.assistant.render_weather_card", side_effect=RuntimeError("font error")):
            await assistant.handle_message(message)
        self.assertTrue(message.replies)
        self.assertIn("embed", message.replies[0])
        self.assertIn("Delhi", message.replies[0]["embed"].title)


class TestIntentRefinement(unittest.IsolatedAsyncioTestCase):
    """Stage 2 only runs when stage 1 was unsure, and can only help."""

    async def test_confident_route_is_not_refined(self):
        assistant, _ = make_assistant()
        route = Route(kind=CALCULATE, payload="2+2", confidence=0.95)
        refined = await assistant._refine(route, "2+2")
        self.assertIs(refined, route)

    async def test_ambiguous_route_upgraded_by_the_classifier(self):
        assistant, _ = make_assistant()
        route = Route(kind=AI, payload="any red panda content", confidence=0.4)
        with patch(
            "commands.assistant.classify_request",
            new=AsyncMock(
                return_value=IntentGuess(
                    intent="image", query="red panda", needs_image=True
                )
            ),
        ):
            refined = await assistant._refine(route, "any red panda content")
        self.assertEqual(refined.kind, IMAGE)
        self.assertEqual(refined.payload, "red panda")

    async def test_classifier_failure_keeps_the_deterministic_route(self):
        assistant, _ = make_assistant()
        route = Route(kind=AI, payload="hmm", confidence=0.4)
        with patch(
            "commands.assistant.classify_request", new=AsyncMock(return_value=None)
        ):
            refined = await assistant._refine(route, "hmm")
        self.assertIs(refined, route)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class TestPingAlwaysAnswers(unittest.IsolatedAsyncioTestCase):
    """Every explicit ping produces exactly one useful response."""

    async def test_messy_requests_reach_the_right_tool(self):
        cases = {
            "wthr delhi pls": "weather",
            "wha time tokyo rn": "time",
            "calc 23% 829": "calculate",
            "100 doller in rupees": "convert",
            "dictonry meaning of ephemeral": "define",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                from functions.router import route_query

                self.assertEqual(route_query(text).kind, expected)

    async def test_terrible_input_still_gets_a_reply(self):
        ai = FakeAI(available=True, answer="here you go")
        for text in ("wthr delhi pls", "calc 23% 829", "100 doller in rupees",
                     "asdkjh", "hmmm idk"):
            assistant, _ = make_assistant(ai=ai)
            message = mention(text)
            handled = await assistant.handle_message(message)
            self.assertTrue(handled, text)
            self.assertTrue(message.replies, f"silent on {text!r}")

    async def test_deterministic_requests_never_reach_the_ai_classifier(self):
        ai = FakeAI(available=True)
        for text in ("what is 15% of 920", "wthr delhi", "100 usd inr",
                     "tokyo time rn", "define ephemeral"):
            assistant, _ = make_assistant(ai=ai)
            assistant.images = FakeImageService()
            await assistant.handle_message(mention(text))
        self.assertEqual(ai.classified, [])
        self.assertEqual(ai.responded_to, [])

    async def test_bare_value_conversion_asks_only_for_units(self):
        assistant, _ = make_assistant()
        message = mention("x")
        await assistant._convert(message, Route(kind="convert", payload="50"))
        content = message.replies[0]["content"]
        self.assertIn("50", content)
        self.assertIn("USD", content)


class TestSearchReliability(unittest.IsolatedAsyncioTestCase):
    """Search answers, or says it couldn't — never nothing."""

    async def test_provider_failure_gets_a_clear_short_message(self):
        class DownAI(FakeAI):
            async def answer_question(self, question, *, asker="someone",
                                      channel_id=0, max_tokens=None):
                raise RuntimeError("provider exploded")

        assistant, _ = make_assistant(ai=DownAI(available=True))
        message = mention("search latest Minecraft update")
        self.assertTrue(await assistant.handle_message(message))
        placeholder = message.replies[0]
        self.assertIn("thinking", placeholder["content"].casefold())

    async def test_placeholder_failure_is_replaced_with_an_apology(self):
        class DownAI(FakeAI):
            async def answer_question(self, question, *, asker="someone",
                                      channel_id=0, max_tokens=None):
                raise RuntimeError("provider exploded")

        assistant, _ = make_assistant(ai=DownAI(available=True))
        message = mention("search latest Minecraft update")
        edits = []
        assistant._quiet_edit = lambda target, content="", **kwargs: edits.append(content)
        await assistant.handle_message(message)
        self.assertTrue(edits)
        self.assertIn("isn't answering", edits[0])


class TestImageFallback(unittest.IsolatedAsyncioTestCase):
    async def test_no_images_falls_back_to_the_ai(self):
        ai = FakeAI(available=True, answer="a flurbleglorp is nothing, actually.")
        assistant, _ = make_assistant(ai=ai)
        assistant.images = FakeImageService(results=0)
        message = mention("show me pictures of a flurbleglorp")
        self.assertTrue(await assistant.handle_message(message))
        self.assertIn("couldn't find a usable image", message.replies[0]["content"])
        # …and the AI answered for the same subject instead of nothing.
        self.assertTrue(ai.questions)
        self.assertIn("flurbleglorp", ai.questions[0])


class TestSingleResponsePerPing(unittest.IsolatedAsyncioTestCase):
    """bot.on_message: AFK → assistant → stop. Never two handlers."""

    async def test_mention_skips_dictionary_and_personality(self):
        from config import Config

        import bot as bot_module

        paradise = bot_module.ParadiseBot.__new__(bot_module.ParadiseBot)
        paradise.config = Config()
        paradise._handled = __import__("collections").deque()
        paradise._handled_ids = set()
        paradise.afk_states = {}
        paradise._afk_notice_last = {}
        calls = []

        class Recorder:
            def __init__(self, name, result):
                self.name = name
                self.result = result

            async def __call__(self, message):
                calls.append(self.name)
                return self.result

        paradise.ai = SimpleNamespace(
            record=lambda message: None,
            should_reply_to=lambda message: True,
        )
        paradise.personality = SimpleNamespace(handle=Recorder("personality", None))
        paradise._handle_afk = Recorder("afk", None)
        paradise._maybe_dictionary = Recorder("dictionary", True)
        paradise._maybe_assistant = Recorder("assistant", True)

        message = mention("define ephemeral")
        await bot_module.ParadiseBot.on_message(paradise, message)

        self.assertEqual(calls, ["afk", "assistant"])

    async def test_same_message_is_only_handled_once(self):
        from config import Config

        import bot as bot_module

        paradise = bot_module.ParadiseBot.__new__(bot_module.ParadiseBot)
        paradise.config = Config()
        paradise._handled = __import__("collections").deque()
        paradise._handled_ids = set()
        paradise.afk_states = {}
        paradise._afk_notice_last = {}
        handled = []
        paradise.ai = SimpleNamespace(
            record=lambda message: None, should_reply_to=lambda message: True
        )
        paradise.personality = SimpleNamespace(handle=AsyncMock())

        async def noop(message):
            return None

        async def assistant(message):
            handled.append(message.id)
            return True

        paradise._handle_afk = noop
        paradise._maybe_dictionary = noop
        paradise._maybe_assistant = assistant

        message = mention("weather in Delhi")
        await bot_module.ParadiseBot.on_message(paradise, message)
        await bot_module.ParadiseBot.on_message(paradise, message)

        self.assertEqual(handled, [message.id])

    async def test_non_mentions_still_use_dictionary_and_personality(self):
        from config import Config

        import bot as bot_module

        paradise = bot_module.ParadiseBot.__new__(bot_module.ParadiseBot)
        paradise.config = Config()
        paradise._handled = __import__("collections").deque()
        paradise._handled_ids = set()
        paradise.afk_states = {}
        paradise._afk_notice_last = {}
        calls = []
        paradise.ai = SimpleNamespace(
            record=lambda message: None, should_reply_to=lambda message: False
        )

        async def noop(message):
            return None

        async def dictionary(message):
            calls.append("dictionary")
            return False

        async def personality(message):
            calls.append("personality")

        paradise._handle_afk = noop
        paradise._maybe_dictionary = dictionary
        paradise._maybe_assistant = noop
        paradise.personality = SimpleNamespace(handle=personality)

        await bot_module.ParadiseBot.on_message(paradise, FakeMessage("define x", addressed=False))
        self.assertEqual(calls, ["dictionary", "personality"])


class TestRetiredCommandQuestions(unittest.IsolatedAsyncioTestCase):
    async def test_asking_about_a_retired_command_points_at_the_sentence(self):
        assistant, _ = make_assistant()
        message = mention("what does /weather do")
        self.assertTrue(await assistant.handle_message(message))
        content = message.replies[0]["content"]
        self.assertIn("isn't a command any more", content)
        self.assertIn("weather in Delhi", content)
