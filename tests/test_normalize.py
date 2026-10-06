"""Tests for request normalisation (functions/normalize.py).

Normalisation is the first stage of the ping pipeline: it must make messy
human text routable without changing what the user asked for.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from functions.normalize import normalize_request, repair_token  # noqa: E402


def text_of(raw: str) -> str:
    return normalize_request(raw).text


class TestCleanup(unittest.TestCase):
    def test_collapses_spaces_and_punctuation(self):
        self.assertEqual(text_of("  weather   in   delhi ??? "), "weather in delhi")

    def test_strips_emoji_and_decorations(self):
        self.assertEqual(text_of("weather in delhi 🌧️🔥"), "weather in delhi")

    def test_strips_mentions(self):
        self.assertEqual(text_of("<@123456789> weather delhi"), "weather delhi")

    def test_collapses_elongated_words(self):
        self.assertEqual(text_of("weatherrrr delhiii"), "weather delhi")

    def test_lowercases(self):
        self.assertEqual(text_of("WEATHER In DELHI"), "weather in delhi")

    def test_empty_input_is_safe(self):
        request = normalize_request("")
        self.assertEqual(request.text, "")
        self.assertEqual(request.tokens, [])


class TestSlangAndTypos(unittest.TestCase):
    def test_chat_shorthand(self):
        self.assertEqual(text_of("delhi weather rn"), "delhi weather")
        self.assertEqual(text_of("whats the time tmr"), "what is the time tomorrow")

    def test_tool_word_typos(self):
        self.assertIn("weather", text_of("wthr delhi"))
        self.assertIn("weather", text_of("wheather delhi"))
        self.assertIn("dictionary", text_of("dictonary ephemeral"))
        self.assertIn("calculate", text_of("calc 2+2"))
        self.assertIn("picture", text_of("pic of a red panda"))

    def test_filler_and_profanity_removed(self):
        self.assertEqual(text_of("bro whats the weather in delhi pls"),
                         "what is the weather in delhi")
        self.assertEqual(text_of("how many bloody rupees is 50 dollars"),
                         "how many rupees is 50 dollars")

    def test_light_hinglish(self):
        self.assertIn("weather delhi", text_of("delhi ka weather batao"))

    def test_spoken_maths_rewrites(self):
        self.assertEqual(text_of("divide 829 by 17"), "829 / 17")
        self.assertEqual(text_of("23% 482"), "23% of 482")
        self.assertEqual(text_of("15 percent of 500"), "15% of 500")

    def test_real_words_are_not_corrected_into_tool_words(self):
        # "times"/"divided" are legitimate English, not typos of time/divide.
        self.assertEqual(repair_token("times"), "times")
        self.assertEqual(repair_token("divided"), "divided")
        self.assertEqual(text_of("25 times 32"), "25 times 32")

    def test_subject_words_survive(self):
        self.assertIn("red panda", text_of("show me pictures of a red panda"))
        self.assertIn("capybara", text_of("what does a capybara look like"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
