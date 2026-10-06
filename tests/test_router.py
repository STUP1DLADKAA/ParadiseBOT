"""Unit tests for the deterministic mention router.

The router must map everyday sentences to the most specialised tool —
never to AI when a tool or the web can answer. These tables mirror the
product spec's example phrases.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from functions.router import (
    AI, CALCULATE, CONVERT, DATE, DEFINE, HELP, IMAGE,
    SEARCH, TIME, TRANSLATE, WEATHER, route_many, route_query,
)


class TestRouter(unittest.TestCase):
    def route(self, text: str):
        return route_query(text)

    # -- spec examples ---------------------------------------------------

    def test_definitions(self):
        for text in (
            "define photosynthesis",
            "define gravity",
            "what does serendipity mean",
            "meaning of entropy",
            "what is a VPN",
        ):
            route = self.route(text)
            self.assertEqual(route.kind, DEFINE, text)
            self.assertTrue(route.payload)

    def test_search(self):
        route = self.route("search latest Minecraft update")
        self.assertEqual(route.kind, SEARCH)
        self.assertIn("minecraft", route.payload.casefold())

        route = self.route("search latest iPhone")
        self.assertEqual(route.kind, SEARCH)
        self.assertTrue(route.fresh)

    def test_fresh_questions(self):
        for text in ("who won the match", "what happened today", "latest Minecraft news"):
            route = self.route(text)
            self.assertEqual(route.kind, SEARCH, text)
            self.assertTrue(route.fresh, text)

    def test_currency_conversion(self):
        for text in ("convert 20 USD to INR", "100 USD to INR", "convert 100 USD to INR"):
            route = self.route(text)
            self.assertEqual(route.kind, CONVERT, text)
            self.assertIn("usd", route.payload.casefold())

    def test_unit_conversion(self):
        for text in ("10 km to miles", "20 km in miles", "what's 15 miles in kilometers?"):
            route = self.route(text)
            self.assertEqual(route.kind, CONVERT, text)

        # temperature
        self.assertEqual(self.route("25 celsius to fahrenheit").kind, CONVERT)

    def test_calculator(self):
        for text in ("what's 15% of 500", "25% of 800", "234 × 56", "what is 25% of 800"):
            route = self.route(text)
            self.assertEqual(route.kind, CALCULATE, text)

    def test_weather(self):
        route = self.route("weather in Delhi")
        self.assertEqual(route.kind, WEATHER)
        self.assertEqual(route.payload.lower(), "delhi")

        route = self.route("what's the weather in Delhi?")
        self.assertEqual(route.kind, WEATHER)
        self.assertEqual(route.payload.lower(), "delhi")

        route = self.route("weather")
        self.assertEqual(route.kind, WEATHER)
        self.assertEqual(route.payload, "")

    def test_time_and_date(self):
        route = self.route("what time is it in Tokyo")
        self.assertEqual(route.kind, TIME)
        self.assertEqual(route.payload.lower(), "tokyo")

        route = self.route("time in Tokyo")
        self.assertEqual(route.kind, TIME)
        self.assertEqual(route.payload.lower(), "tokyo")

        self.assertEqual(self.route("what day is it").kind, DATE)
        self.assertEqual(self.route("what's the date").kind, DATE)

    def test_translate(self):
        route = self.route("translate hello into Japanese")
        self.assertEqual(route.kind, TRANSLATE)
        self.assertEqual(route.payload, "hello")
        self.assertEqual(route.payload2.lower(), "japanese")

        route = self.route("how do you say thank you in hindi")
        self.assertEqual(route.kind, TRANSLATE)
        self.assertEqual(route.payload2.lower(), "hindi")

    def test_reasoning_goes_to_ai_or_grounded_search(self):
        route = self.route("explain why One Piece is so popular")
        self.assertEqual(route.kind, AI, "pure reasoning question should reach AI")
        self.assertTrue(route.reason_complex)

        route = self.route(
            "Compare the themes of these three anime and tell me which is darker"
        )
        self.assertTrue(route.reason_complex)

    def test_help_and_empty(self):
        self.assertEqual(self.route("").kind, HELP)
        self.assertEqual(self.route("help").kind, HELP)

    def test_greeting_is_conversation(self):
        for text in ("hey", "thanks", "lol"):
            route = self.route(text)
            self.assertEqual(route.kind, AI, text)

    # -- priority guarantees ----------------------------------------------

    def test_tools_never_fall_to_ai(self):
        """The AI-last guarantee: every tool-shaped query stays off AI."""
        for text, kind in (
            ("define photosynthesis", DEFINE),
            ("100 USD to INR", CONVERT),
            ("10 km to miles", CONVERT),
            ("25% of 800", CALCULATE),
            ("weather in Delhi", WEATHER),
            ("time in Tokyo", TIME),
            ("translate hello into Japanese", TRANSLATE),
            ("search latest Minecraft update", SEARCH),
            ("latest Minecraft news", SEARCH),
        ):
            route = route_query(text)
            self.assertEqual(route.kind, kind, f"{text} must route to {kind}, got {route.kind}")
            self.assertNotEqual(route.kind, "ai")

    def test_payloads_are_clean(self):
        route = self.route("what's 15% of 500?")
        self.assertEqual(route.payload, "15% of 500")
        route = self.route("define    'serendipity'  ")
        self.assertEqual(route.payload, "serendipity")


class TestImageRouting(unittest.TestCase):
    """"Show me a picture of X" must mean: search the web for a photo."""

    def test_natural_image_phrasings(self):
        for text, subject in (
            ("show me a picture of a red panda", "red panda"),
            ("find me a picture of the Eiffel Tower", "Eiffel Tower"),
            ("give me an image of a red panda", "red panda"),
            ("send me a photo of mount fuji", "mount fuji"),
            ("find an image of a wolf", "wolf"),
            ("image of the eiffel tower", "eiffel tower"),
            ("picture of a wolf", "wolf"),
            ("find pictures of golden retrievers", "golden retrievers"),
            ("give me images of nebulae", "nebulae"),
            ("what does a red panda look like", "red panda"),
            ("can you show me a photo of saturn", "saturn"),
            ("post a screenshot of the minecraft nether", "minecraft nether"),
        ):
            route = route_query(text)
            self.assertEqual(route.kind, IMAGE, text)
            self.assertEqual(route.payload, subject, text)

    def test_requested_count_is_captured_and_bounded(self):
        self.assertEqual(route_query("show me a picture of a cat").count, 1)
        self.assertEqual(route_query("show me 3 pictures of Minecraft villages").count, 3)
        self.assertEqual(route_query("show me some pics of tokyo").count, 3)
        self.assertEqual(route_query("show me 2 photos of jupiter").count, 2)
        # Never spam a channel, however many were asked for.
        self.assertLessEqual(route_query("show me 40 pictures of cats").count, 4)

    def test_image_beats_the_generic_search_verb(self):
        # "find …" is also a web-search verb; the picture words must win.
        self.assertEqual(route_query("find me a picture of a wolf").kind, IMAGE)
        self.assertEqual(route_query("look up an image of a wolf").kind, IMAGE)

    def test_non_image_requests_are_untouched(self):
        for text, kind in (
            ("what's 7283 * 492", CALCULATE),
            ("convert 72 km/h to mph", CONVERT),
            ("what's the weather in Delhi", WEATHER),
            ("what time is it in Tokyo", TIME),
            ("define photosynthesis", DEFINE),
            ("what does serendipity mean", DEFINE),
            ("search the latest Minecraft Bedrock update", SEARCH),
        ):
            self.assertEqual(route_query(text).kind, kind, text)


class TestTemperatureConversionRouting(unittest.TestCase):
    """Degree signs must not break conversion routing."""

    def test_degree_symbol_forms(self):
        for text in (
            "what's 25°C in Fahrenheit",
            "25°C to F",
            "convert 100°F to celsius",
            "32 fahrenheit to celsius",
            "25 celsius to fahrenheit",
        ):
            self.assertEqual(route_query(text).kind, CONVERT, text)


class TestCompoundRouting(unittest.TestCase):
    """Requests that genuinely ask for two things get split; others don't."""

    def test_multi_tool_requests_are_split(self):
        for text, kinds in (
            ("search the latest Minecraft update and convert 500 USD to INR",
             [SEARCH, CONVERT]),
            ("weather in Delhi and time in Tokyo", [WEATHER, TIME]),
            ("15 km in miles and 20 kg in lbs", [CONVERT, CONVERT]),
            ("what is 2+2 and 3+3", [CALCULATE, CALCULATE]),
            ("show me a picture of a wolf and what's 15% of 847", [IMAGE, CALCULATE]),
        ):
            routes = route_many(text)
            self.assertEqual([route.kind for route in routes], kinds, text)

    def test_ordinary_requests_stay_single(self):
        for text in (
            "convert 100 USD to INR",
            "explain quantum tunnelling",
            "compare RTX 5070 and RX 9070",
            "weather in Trinidad and Tobago",
            "show me a picture of dungeons and dragons",
            "picture of a cat and a dog",
            "define rock and roll",
            "convert 100 USD to INR and EUR",
            "what is the difference between a virus and a bacteria",
            "",
        ):
            self.assertEqual(len(route_many(text)), 1, text)

    def test_split_is_bounded(self):
        routes = route_many(
            "what is 2+2 and 3+3 and 4+4 and 5+5 and 6+6 and 7+7"
        )
        self.assertLessEqual(len(routes), 3)

    def test_single_route_matches_route_query(self):
        for text in ("25% of 800", "weather in Delhi", "hey"):
            self.assertEqual(route_many(text)[0].kind, route_query(text).kind, text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class TestTolerantRouting(unittest.TestCase):
    """Real chat: typos, slang, Hinglish, missing punctuation, profanity.

    Every line here is a request a human actually types. None of them may
    fall through to the AI when a deterministic tool can answer.
    """

    def assertRoute(self, text, kind, *, payload_contains=None, count=None):
        route = route_query(text)
        self.assertEqual(route.kind, kind, f"{text!r} → {route.kind}")
        self.assertGreaterEqual(route.confidence, 0.6, text)
        if payload_contains is not None:
            self.assertIn(payload_contains, route.payload.casefold(), text)
        if count is not None:
            self.assertEqual(route.count, count, text)
        return route

    # -- calculator --------------------------------------------------------

    def test_calculator(self):
        for text in (
            "calculate 20% of 500",
            "25 * 32",
            "what's 99 divided by 3",
            "calc 8238/29",
            "8238 divided by 29",
            "calc 23% 829",
            "23% 482",
            "bro calculate 29% of 734",
            "how much is 15 percent of 4900",
            "sqrt 144",
            "what's 18 squared",
            "divide 829 by 17",
        ):
            self.assertRoute(text, CALCULATE)

    def test_calculator_payload_is_evaluable(self):
        from functions.calculator import calculate

        for text in ("bro calculate 29% of 734", "calc 23% 829", "what is 25 * 32"):
            route = route_query(text)
            self.assertEqual(route.kind, CALCULATE, text)
            calculate(route.payload)  # must not raise

    # -- conversion --------------------------------------------------------

    def test_conversion(self):
        for text, needle in (
            ("100 USD to INR", "usd"),
            ("5 km to miles", "km"),
            ("72F in C", "72"),
            ("100 usd inr", "usd"),
            ("5 kg pound", "kg"),
            ("100 doller in rupees", "100"),
            ("how many bloody rupees is 50 dollars", "50"),
            ("convert 50 dollars into rupees", "50"),
            ("how many inches is 6 feet", "6"),
        ):
            self.assertRoute(text, CONVERT, payload_contains=needle)

    # -- weather -----------------------------------------------------------

    def test_weather(self):
        for text in (
            "weather Delhi",
            "Delhi weather",
            "will it rain in Delhi tomorrow",
            "wthr delhi",
            "wats weather delhi",
            "delhi weather rn??",
            "yo bro how hot is delhi rn",
            "wthr delhi pls",
            "temperature in Tokyo",
            "forecast for Mumbai",
            "delhi ka weather batao",
        ):
            route = self.assertRoute(text, WEATHER)
            self.assertTrue(route.payload, text)

    # -- time / date -------------------------------------------------------

    def test_time(self):
        for text in ("time Tokyo", "what time is it in Tokyo", "tokyo time rn",
                     "wha time tokyo", "tokyo time"):
            self.assertRoute(text, TIME, payload_contains="tokyo")

    def test_date(self):
        for text in ("what date is it", "what day is today", "today's date"):
            self.assertEqual(route_query(text).kind, DATE, text)

    # -- dictionary --------------------------------------------------------

    def test_dictionary(self):
        for text in (
            "define ephemeral",
            "what does ephemeral mean",
            "dictonary ephemeral",
            "dictonry meaning of ephemeral",
            "meaning ambivalent",
            "meaning of ephemeral",
        ):
            route = self.assertRoute(text, DEFINE)
            self.assertLessEqual(len(route.payload.split()), 3, text)

    # -- images ------------------------------------------------------------

    def test_images(self):
        self.assertRoute("show me pictures of cats", IMAGE, payload_contains="cat")
        self.assertRoute("pic red panda", IMAGE, payload_contains="red panda")
        self.assertRoute("what does a capybara look like", IMAGE,
                         payload_contains="capybara")
        self.assertRoute("show pic of minecraft", IMAGE, payload_contains="minecraft")
        self.assertRoute("send me 3 pictures of a red panda", IMAGE, count=3)

    # -- search ------------------------------------------------------------

    def test_search(self):
        for text in (
            "search latest Minecraft update",
            "latest Minecraft news",
            "latest mcpe",
            "who invented Minecraft",
            "google rtx 4060 price",
        ):
            route = route_query(text)
            self.assertEqual(route.kind, SEARCH, text)
            self.assertTrue(route.payload, text)

    def test_comparison_requests_retrieve_before_reasoning(self):
        route = route_query("which is better RTX 4060 or 3060")
        self.assertEqual(route.kind, SEARCH)
        self.assertTrue(route.reason_complex)

    # -- AI stays the last resort -----------------------------------------

    def test_ai_fallback_only_for_open_questions(self):
        for text in (
            "explain why the sky is blue like I'm stupid",
            "why do cats purr",
            "explain recursion simply",
            "give me an idea for a Minecraft event",
        ):
            self.assertEqual(route_query(text).kind, AI, text)

    def test_translation(self):
        route = route_query('translate "hello bro" into Hindi')
        self.assertEqual(route.kind, TRANSLATE)
        self.assertEqual(route.payload2.casefold(), "hindi")
        self.assertEqual(route_query("hello in spanish").kind, TRANSLATE)
        self.assertEqual(route_query("how do you say hello in japanese").kind, TRANSLATE)

    # -- mixed requests ----------------------------------------------------

    def test_mixed_request_splits(self):
        routes = route_many("what's the weather in Delhi and convert 100 USD to INR")
        kinds = [route.kind for route in routes]
        self.assertIn(WEATHER, kinds)
        self.assertIn(CONVERT, kinds)

    def test_single_requests_are_not_split(self):
        for text in ("difference between rock and roll", "weather in Delhi"):
            self.assertEqual(len(route_many(text)), 1, text)

    # -- everything returns something -------------------------------------

    def test_router_never_returns_unknown_for_real_text(self):
        for text in ("asdkjh", "???", "idk man", "hmm", "ok"):
            route = route_query(text)
            self.assertTrue(route.routed, text)
