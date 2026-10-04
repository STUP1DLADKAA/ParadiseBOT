"""Dispatch tests for the mention assistant (commands/assistant.py).

Everything is faked at the Discord and service boundaries — these tests
prove that a mention lands on the right tool, that tool failures degrade
gracefully, and that AI stays the last resort.
"""

from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord  # noqa: E402

from commands.assistant import Assistant  # noqa: E402
from functions.conversion.models import ConversionCategory, ConversionResult  # noqa: E402
from functions.helpers import ParadiseError  # noqa: E402
from functions.search import SearchAnswer, SearchResponse, SearchResult  # noqa: E402


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
    def __init__(self, *, available=False, answer="ai answer"):
        self.available = available
        self._answer = answer
        self.responded_to = []

    def should_reply_to(self, message):
        return message._addressed

    def extract_question(self, message):
        return message.content

    def cooldown_remaining(self, message):
        return 0

    async def respond(self, message):
        self.responded_to.append(message.content)
        return self._answer


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


class FakeAnswerEngine:
    def __init__(self):
        self.calls = []

    async def answer(self, query, *, fresh=None, reason_complex=False):
        self.calls.append((query, fresh, reason_complex))
        return SearchAnswer(
            query=query,
            direct_answer="Minecraft 1.22 was announced with new blocks.",
            sources=[SearchResult("Official", "https://minecraft.net/", "…")],
        )


class FakeSearchEngineWrapper:
    async def search(self, query, limit=8):
        return SearchResponse(query=query, results=[
            SearchResult("Official", "https://minecraft.net/", "…")
        ])


class FakeUtilityCog:
    def __init__(self):
        self.conversion_engine = FakeConversionEngine()
        self.answer_engine = FakeAnswerEngine()
        self.search_engine = FakeSearchEngineWrapper()
        self.db = SimpleNamespace(get_user_timezone=lambda _id: None)


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

    async def test_search_flow_with_loading_state(self):
        assistant, bot = make_assistant()
        message = mention("search latest Minecraft news")
        handled = await assistant.handle_message(message)
        self.assertTrue(handled)
        # loading placeholder first…
        self.assertIn("searching the web", message.replies[0]["content"].casefold())
        engine = bot.get_cog("Utility").answer_engine
        self.assertEqual(engine.calls[0][0], "latest Minecraft news")

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

    async def test_entertainment_falls_back_to_search_when_cog_gone(self):
        assistant, _ = make_assistant()
        message = mention("upcoming anime")
        await assistant.handle_message(message)
        # No Entertainment cog registered → search answer instead of silence.
        self.assertIn("searching the web", message.replies[0]["content"].casefold())


class TestEntertainmentCardButtons(unittest.TestCase):
    """The card buttons surface: trailer link is always safe & keyless."""

    def test_trailer_button_present_and_wellformed(self):
        from commands.entertainment import CardButtons
        from functions.entertainment.models import Identity, MediaKind

        identity = Identity(title="Naruto", kind=MediaKind.ANIME, year=2002)
        view = CardButtons(service=None, identity=identity)

        link_buttons = [
            item for item in view.children
            if getattr(item, "style", None) == discord.ButtonStyle.link
        ]
        self.assertTrue(link_buttons, "expected a trailer link button")
        urls = {item.url for item in link_buttons}
        self.assertTrue(any("youtube.com/results" in url for url in urls))
        self.assertTrue(any("Naruto" in url and "official%20trailer" in url for url in urls))

    def test_person_identity_gets_no_trailer_button(self):
        from commands.entertainment import CardButtons
        from functions.entertainment.models import Identity, MediaKind

        identity = Identity(title="Hayao Miyazaki", kind=MediaKind.PERSON)
        view = CardButtons(service=None, identity=identity)
        link_buttons = [
            item for item in view.children
            if getattr(item, "style", None) == discord.ButtonStyle.link
        ]
        self.assertEqual(link_buttons, [])


class TestSearchAnswerView(unittest.TestCase):
    def test_view_disables_browse_without_results(self):
        from functions.search import SearchAnswerView

        answer = SearchAnswer(query="q", direct_answer="answer text here")
        view = SearchAnswerView(answer, None, author_id=1)
        browse = [i for i in view.children if getattr(i, "label", "") == "📑 Browse results"]
        self.assertTrue(browse)
        self.assertTrue(browse[0].disabled)

        # link buttons only appear for real sources (max two)
        answer2 = SearchAnswer(
            query="q",
            sources=[SearchResult(f"S{i}", f"https://s{i}.example/", "…") for i in range(4)],
        )
        view2 = SearchAnswerView(answer2, None, author_id=1)
        links = [
            i for i in view2.children
            if getattr(i, "style", None) == discord.ButtonStyle.link
        ]
        self.assertEqual(len(links), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
