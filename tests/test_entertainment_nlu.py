"""Tests for the widened entertainment NLU (the mention-router additions)."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from functions.entertainment import nlu


def parse(text: str):
    return nlu.match_entertainment_request(text)


class TestNewPatterns(unittest.TestCase):
    def test_find_kind_like_subject(self):
        intent = parse("find anime like Naruto")
        self.assertIsNotNone(intent)
        self.assertEqual(intent.kind, nlu.RECOMMEND)
        self.assertEqual(intent.subject.casefold(), "naruto")
        self.assertEqual(intent.media_type, "anime")

    def test_what_should_i_watch(self):
        intent = parse("what should I watch tonight")
        self.assertIsNotNone(intent)
        self.assertEqual(intent.kind, nlu.RECOMMEND)
        self.assertTrue(intent.is_actionable)
        self.assertEqual(intent.era, "tonight")

        intent = parse("what movie should we watch")
        self.assertEqual(intent.media_type, "movies")

        # safety: similar-looking chat must not trigger
        self.assertIsNone(parse("what should i watch out for"))

    def test_bare_vibe_with_episode_cap(self):
        intent = parse("short anime under 24 episodes")
        self.assertIsNotNone(intent)
        self.assertEqual(intent.kind, nlu.RECOMMEND)
        self.assertEqual(intent.media_type, "anime")
        self.assertEqual(intent.max_episodes, 24)

    def test_movies_with_vibe(self):
        intent = parse("movies with mind-blowing endings")
        self.assertIsNotNone(intent)
        self.assertEqual(intent.kind, nlu.RECOMMEND)
        self.assertEqual(intent.media_type, "movies")

    def test_something_underrated(self):
        intent = parse("give me something underrated")
        self.assertIsNotNone(intent)
        self.assertEqual(intent.kind, nlu.RECOMMEND)
        self.assertEqual(intent.tone, "underrated")
        self.assertTrue(intent.is_actionable)

    def test_latest_platform_shows(self):
        intent = parse("latest Netflix shows")
        self.assertIsNotNone(intent)
        self.assertEqual(intent.kind, nlu.TRENDING)
        self.assertEqual(intent.media_type, "series")
        self.assertEqual(intent.platform, "netflix")

        intent = parse("newest anime")
        self.assertIsNotNone(intent)
        self.assertEqual(intent.kind, nlu.TRENDING)
        self.assertEqual(intent.media_type, "anime")

    def test_existing_patterns_still_work(self):
        intent = parse("recommend anime like JJK")
        self.assertEqual((intent.kind, intent.subject.casefold()), (nlu.RECOMMEND, "jjk"))
        intent = parse("upcoming anime")
        self.assertEqual(intent.kind, nlu.UPCOMING)
        intent = parse("where can I watch Oppenheimer in India")
        self.assertEqual(intent.kind, nlu.STREAMING)


class TestChatSafety(unittest.TestCase):
    """Plain chat must not be hijacked by the widened patterns."""

    def test_everyday_chat_is_safe(self):
        for text in (
            "short movies are boring honestly",
            "find my keys like always",
            "give me something to eat",
            "newest phone models are expensive",
            "the latest episode was crazy",
            "what time is the meeting",
            "I recommend you watch out",
            "movies are fun",
            "anime is cool",
            "should i watch the budget report",
        ):
            intent = nlu.match_entertainment_request(text)
            self.assertFalse(
                bool(intent and intent.is_actionable),
                f"chat hijacked as entertainment: {text!r} → {intent}",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
