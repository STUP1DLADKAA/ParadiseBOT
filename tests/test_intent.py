"""Tests for the stage-2 AI intent classifier (functions/intent.py).

The classifier is a *fallback*: it only runs when the deterministic
router is unsure, and any failure must leave the deterministic route
untouched.
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from functions.helpers import ParadiseError  # noqa: E402
from functions.intent import (  # noqa: E402
    IntentGuess,
    classify_request,
    parse_intent,
    should_classify,
)
from functions.intent import _cache as intent_cache  # noqa: E402
from functions.router import (  # noqa: E402
    AI, CALCULATE, CONVERT, IMAGE, SEARCH, TIME, WEATHER, Route, route_query,
)


class FakeGemini:
    def __init__(self, reply="", error=None, delay=0.0):
        self.available = True
        self.reply = reply
        self.error = error
        self.delay = delay
        self.calls: list[str] = []

    async def classify(self, request, max_tokens=120):
        self.calls.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.reply


def fake_ai(gemini=None):
    return SimpleNamespace(gemini=gemini, available=gemini is not None)


class TestShouldClassify(unittest.TestCase):
    def test_confident_tool_routes_are_never_classified(self):
        for text in (
            "25% of 800",
            "convert 100 USD to INR",
            "weather in Delhi",
            "what time is it in Tokyo",
            "show me a picture of a red panda",
            "search the latest Minecraft news",
        ):
            self.assertFalse(should_classify(route_query(text)), text)

    def test_vague_requests_are_classified(self):
        for text in ("anything on that thing we discussed", "hmm maybe the other one"):
            route = route_query(text)
            self.assertEqual(route.kind, AI, text)
            self.assertTrue(should_classify(route), text)


class TestParseIntent(unittest.TestCase):
    def test_parses_minified_json(self):
        guess = parse_intent(
            '{"intent":"search","query":"latest Minecraft Bedrock update",'
            '"needs_web":true,"needs_image":false}'
        )
        self.assertIsNotNone(guess)
        self.assertEqual(guess.intent, "search")
        self.assertEqual(guess.query, "latest Minecraft Bedrock update")
        self.assertTrue(guess.needs_web)
        self.assertFalse(guess.needs_image)

    def test_tolerates_code_fences_and_prose(self):
        guess = parse_intent('```json\n{"intent":"image","query":"a wolf"}\n```')
        self.assertIsNotNone(guess)
        self.assertEqual(guess.intent, "image")

    def test_rejects_garbage(self):
        for raw in ("", "   ", "not json", "{", "[]", '{"intent":"hack_the_bot"}',
                    '{"query":"no intent"}', None):
            self.assertIsNone(parse_intent(raw), repr(raw))

    def test_string_booleans_are_accepted(self):
        guess = parse_intent('{"intent":"search","query":"x","needs_web":"true"}')
        self.assertTrue(guess.needs_web)

    def test_oversized_query_is_truncated(self):
        guess = parse_intent('{"intent":"search","query":"%s"}' % ("x" * 900))
        self.assertLessEqual(len(guess.query), 200)


class TestToRoute(unittest.TestCase):
    def setUp(self):
        self.fallback = Route(kind=AI, payload="original", confidence=0.4)

    def test_maps_intents_to_route_kinds(self):
        for intent, expected in (
            ("calculate", CALCULATE),
            ("convert", CONVERT),
            ("weather", WEATHER),
            ("time", TIME),
            ("image", IMAGE),
            ("search", SEARCH),
            ("chat", AI),
        ):
            route = IntentGuess(intent=intent, query="q").to_route(self.fallback)
            self.assertEqual(route.kind, expected, intent)

    def test_needs_image_wins(self):
        route = IntentGuess(intent="search", query="a wolf", needs_image=True).to_route(
            self.fallback
        )
        self.assertEqual(route.kind, IMAGE)

    def test_chat_plus_needs_web_becomes_search(self):
        route = IntentGuess(intent="chat", query="pixel news", needs_web=True).to_route(
            self.fallback
        )
        self.assertEqual(route.kind, SEARCH)
        self.assertTrue(route.fresh)

    def test_empty_query_falls_back_to_the_original(self):
        route = IntentGuess(intent="search", query="").to_route(self.fallback)
        self.assertEqual(route.payload, "original")


class TestClassifyRequest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        intent_cache.clear()

    async def test_happy_path(self):
        gemini = FakeGemini('{"intent":"image","query":"red panda","needs_image":true}')
        guess = await classify_request("got any red panda content", fake_ai(gemini))
        self.assertIsNotNone(guess)
        self.assertEqual(guess.intent, "image")

    async def test_no_ai_configured_returns_none(self):
        self.assertIsNone(await classify_request("whatever", fake_ai(None)))
        self.assertIsNone(await classify_request("whatever", None))

    async def test_unavailable_gemini_returns_none(self):
        gemini = FakeGemini('{"intent":"search","query":"x"}')
        gemini.available = False
        self.assertIsNone(await classify_request("whatever", fake_ai(gemini)))
        self.assertEqual(gemini.calls, [])

    async def test_api_error_returns_none(self):
        gemini = FakeGemini(error=ParadiseError("Gemini is cooling down"))
        self.assertIsNone(await classify_request("whatever", fake_ai(gemini)))

    async def test_unexpected_error_returns_none(self):
        gemini = FakeGemini(error=RuntimeError("kaboom"))
        self.assertIsNone(await classify_request("whatever", fake_ai(gemini)))

    async def test_timeout_returns_none(self):
        import functions.intent as intent_module

        gemini = FakeGemini('{"intent":"search","query":"x"}', delay=0.2)
        original = intent_module._CLASSIFY_TIMEOUT
        intent_module._CLASSIFY_TIMEOUT = 0.01
        try:
            self.assertIsNone(await classify_request("whatever", fake_ai(gemini)))
        finally:
            intent_module._CLASSIFY_TIMEOUT = original

    async def test_malformed_reply_returns_none_and_is_not_cached(self):
        gemini = FakeGemini("I think the user wants a picture!")
        self.assertIsNone(await classify_request("hmm", fake_ai(gemini)))
        self.assertIsNone(await classify_request("hmm", fake_ai(gemini)))
        self.assertEqual(len(gemini.calls), 2)  # worth retrying

    async def test_repeated_question_is_classified_once(self):
        gemini = FakeGemini('{"intent":"search","query":"pixel"}')
        await classify_request("anything on the pixel", fake_ai(gemini))
        await classify_request("Anything  on the Pixel", fake_ai(gemini))
        self.assertEqual(len(gemini.calls), 1)

    async def test_empty_and_oversized_input_is_skipped(self):
        gemini = FakeGemini('{"intent":"search","query":"x"}')
        self.assertIsNone(await classify_request("", fake_ai(gemini)))
        self.assertIsNone(await classify_request("x" * 500, fake_ai(gemini)))
        self.assertEqual(gemini.calls, [])

    async def test_classifier_never_returns_prose_to_the_caller(self):
        gemini = FakeGemini(
            'Reasoning: the user seems to want a photo.\n'
            '{"intent":"image","query":"a wolf","needs_image":true}'
        )
        guess = await classify_request("wolf stuff", fake_ai(gemini))
        self.assertIsNotNone(guess)
        self.assertEqual(guess.query, "a wolf")
        self.assertNotIn("Reasoning", guess.query)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
