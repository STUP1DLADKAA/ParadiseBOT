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
    AI, CALCULATE, CONVERT, DATE, DEFINE, ENTERTAINMENT, HELP,
    SEARCH, TIME, TRANSLATE, WEATHER, route_query,
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

    def test_entertainment(self):
        for text in (
            "upcoming anime",
            "recommend me a thriller movie",
            "recommend anime like JJK",
            "what's trending in anime",
            "find anime like Naruto",
            "what should I watch tonight",
            "what movies are trending",
            "best sci-fi movies",
            "latest Netflix shows",
            "give me something underrated",
            "where can I watch Oppenheimer in India",
            "short anime under 24 episodes",
            "movies with mind-blowing endings",
            "best psychological anime",
        ):
            route = self.route(text)
            self.assertEqual(route.kind, ENTERTAINMENT, text)

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
            ("upcoming anime", ENTERTAINMENT),
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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
