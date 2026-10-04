"""Unit tests for the search answer pipeline (summary-first search).

Offline: the retrieval layer is replaced with canned responses; the
synthesis, ranking, warning, caching and embed logic is what gets tested.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from functions.search import (
    AnswerEngine,
    SearchAnswer,
    SearchResponse,
    SearchResult,
    build_answer_embed,
)
from functions.helpers import ParadiseError


def result(title, url, snippet, source="", authoritative=False) -> SearchResult:
    return SearchResult(
        title=title, url=url, snippet=snippet, source=source, authoritative=authoritative
    )


class FakeSearchEngine:
    def __init__(self, response=None, error=None):
        self.response = response or SearchResponse(query="q", results=[])
        self.error = error
        self.calls = []

    async def search(self, query, limit=8):
        self.calls.append((query, limit))
        if self.error is not None:
            raise self.error
        return self.response


def patch_legs(engine: AnswerEngine, *, instant=None, wiki=None):
    async def fake_instant(query):
        return instant

    async def fake_wiki(query):
        return wiki

    engine._instant_answer = fake_instant
    engine._wikipedia_summary = fake_wiki
    return engine


WIKI = {
    "title": "Photosynthesis",
    "extract": (
        "Photosynthesis is a system of biological processes by which "
        "photosynthetic organisms convert light energy into chemical energy. "
        "It is used by plants, algae and some bacteria."
    ),
    "url": "https://en.wikipedia.org/wiki/Photosynthesis",
}


class TestAnswerEngine(unittest.IsolatedAsyncioTestCase):
    async def test_wikipedia_direct_answer(self):
        engine = AnswerEngine(FakeSearchEngine(SearchResponse(
            query="photosynthesis",
            results=[result("Photosynthesis - Britannica", "https://britannica.com/p",
                            "Photosynthesis is how plants make food.", "britannica.com")],
        )))
        patch_legs(engine, wiki=WIKI)

        answer = await engine.answer("what is photosynthesis")
        self.assertIn("light energy", answer.direct_answer)
        self.assertEqual(answer.answer_source, "Wikipedia")
        self.assertTrue(answer.sources)
        self.assertEqual(answer.sources[0].source, "wikipedia.org")  # wiki merged first
        self.assertEqual(answer.generated_by, "local")
        self.assertFalse(answer.cached)

    async def test_instant_answer_fallback(self):
        engine = AnswerEngine(FakeSearchEngine(SearchResponse(
            query="x",
            results=[result("Some site", "https://a.example", "Unrelated snippet here.", "a.example")],
        )))
        patch_legs(engine, instant={
            "abstract": "The speed of light is 299,792,458 metres per second. It is the universe's speed limit.",
            "url": "https://en.wikipedia.org/wiki/Speed_of_light",
            "source": "Wikipedia",
            "heading": "Speed of light",
        })

        answer = await engine.answer("speed of light")
        self.assertIn("299,792,458", answer.direct_answer)
        # The instant-answer source is merged into the evidence list.
        self.assertTrue(any("wikipedia" in s.source.casefold() for s in answer.sources))

    async def test_snippet_only_synthesis(self):
        engine = AnswerEngine(FakeSearchEngine(SearchResponse(
            query="minecraft 1.22",
            results=[
                result("Minecraft 1.22 announced", "https://news.example/1",
                       "Minecraft 1.22 was announced today at Minecraft Live. The update adds new blocks.", "news.example"),
                result("Official Minecraft", "https://minecraft.net/2",
                       "The 1.22 update introduces a new mob and several gameplay changes.", "minecraft.net",
                       authoritative=True),
            ],
        )))
        patch_legs(engine)

        answer = await engine.answer("minecraft 1.22")
        self.assertTrue(answer.has_answer)
        self.assertTrue(answer.key_points)
        source_list = answer.sources
        self.assertGreaterEqual(len(source_list), 2)

    async def test_conflicting_year_claims_warn(self):
        engine = AnswerEngine(FakeSearchEngine(SearchResponse(
            query="game x release",
            results=[
                result("Site A", "https://a.example", "Game X is expected to release in 2025.", "a.example"),
                result("Site B", "https://b.example", "Game X is coming in 2026, says the studio.", "b.example"),
            ],
        )), synthesize=False)
        patch_legs(engine)

        answer = await engine.answer("when does game x release", fresh=True)
        self.assertTrue(any("disagree" in warning for warning in answer.warnings))
        self.assertTrue(any("2025" in warning and "2026" in warning for warning in answer.warnings))

    async def test_stale_sources_warn_on_fresh_queries(self):
        engine = AnswerEngine(FakeSearchEngine(SearchResponse(
            query="latest y news",
            results=[
                result("Old article", "https://old.example", "Back in 2019 the service launched this feature.", "old.example"),
                result("Older article", "https://older.example", "The 2020 update changed everything.", "older.example"),
            ],
        )), synthesize=False)
        patch_legs(engine)

        answer = await engine.answer("latest y news", fresh=True)
        self.assertTrue(any("freshest" in warning for warning in answer.warnings))

    async def test_caching(self):
        fake_engine = FakeSearchEngine(SearchResponse(
            query="q", results=[result("A", "https://a.example", "Something useful about the topic here.", "a.example")]
        ))
        engine = AnswerEngine(fake_engine, cache_ttl=600)
        patch_legs(engine)

        first = await engine.answer("q topic")
        second = await engine.answer("q topic")
        self.assertFalse(first.cached)
        self.assertTrue(second.cached)
        self.assertEqual(fake_engine.calls, [("q topic", 8)])  # underlying search ran once

    async def test_total_failure_never_raises(self):
        engine = AnswerEngine(FakeSearchEngine(error=RuntimeError("boom")))
        answer = await engine.answer("anything at all")
        self.assertIsInstance(answer, SearchAnswer)
        self.assertFalse(answer.has_answer)
        self.assertFalse(answer.has_sources)

    async def test_gemini_synthesis_grounded(self):
        class FakeAI:
            available = True

            def __init__(self):
                self.calls = []

            async def reason(self, question, facts, max_tokens=0):
                self.calls.append((question, facts))
                return "Minecraft 1.22 adds new blocks and a new mob, per official sources."

        ai = FakeAI()
        engine = AnswerEngine(FakeSearchEngine(SearchResponse(
            query="minecraft",
            results=[
                result("News A", "https://a.example",
                       "Minecraft 1.22 was announced today. It adds new blocks and a new mob.", "a.example"),
                result("News B", "https://b.example",
                       "The update also brings gameplay changes and performance fixes.", "b.example"),
                result("News C", "https://c.example",
                       "Players can expect the update soon, developers said.", "c.example"),
            ],
        )), ai=ai)
        patch_legs(engine)

        answer = await engine.answer("what is new in minecraft", fresh=True)
        self.assertTrue(ai.calls, "question-form + multi-source should invite synthesis")
        question, facts = ai.calls[0]
        self.assertIn("minecraft", question.casefold())
        self.assertTrue(facts)  # grounded: facts went in
        self.assertEqual(answer.generated_by, "gemini")
        self.assertIn("1.22", answer.summary)

    async def test_gemini_failure_falls_back_to_local(self):
        class BrokenAI:
            available = True

            async def reason(self, question, facts, max_tokens=0):
                raise ParadiseError("nope")

        engine = AnswerEngine(FakeSearchEngine(SearchResponse(
            query="minecraft",
            results=[
                result("News A", "https://a.example", "Minecraft 1.22 announced today with new blocks.", "a.example"),
                result("News B", "https://b.example", "The update adds a new mob too.", "b.example"),
                result("News C", "https://c.example", "Performance improvements are included.", "c.example"),
            ],
        )), ai=BrokenAI())
        patch_legs(engine)

        answer = await engine.answer("what is new in minecraft", fresh=True)
        self.assertEqual(answer.generated_by, "local")
        self.assertTrue(answer.has_answer)

    async def test_no_ai_never_used_for_synthesis(self):
        engine = AnswerEngine(FakeSearchEngine(SearchResponse(
            query="x", results=[result("A", "https://a.example", "Plain old snippet content here.", "a.example")]
        )), ai=None)
        patch_legs(engine)
        answer = await engine.answer("x topic")
        self.assertEqual(answer.generated_by, "local")


class TestAnswerEmbed(unittest.TestCase):
    def sample(self) -> SearchAnswer:
        return SearchAnswer(
            query="minecraft 1.22 features",
            direct_answer="Minecraft 1.22 introduces new blocks and a new mob.",
            summary="The update is expected to add gameplay changes, per official sources.",
            key_points=["New mob revealed at Minecraft Live", "Performance fixes included"],
            sources=[
                result("Official Minecraft", "https://minecraft.net/", "…", "minecraft.net", True),
                result("Coverage", "https://news.example/", "…", "news.example"),
            ],
            warnings=["Sources currently disagree on the timing — some say 2025, others say 2026."],
            answer_source="Wikipedia",
        )

    def test_full_card_layout(self):
        embed = build_answer_embed(self.sample())
        self.assertIn("🔎", embed.title)
        self.assertIn("Quick Answer", embed.description)
        self.assertIn("new blocks", embed.description)

        field_names = [field.name for field in embed.fields]
        self.assertIn("Key points", field_names)
        self.assertIn("⚠️ Heads-up", field_names)
        self.assertIn("Sources", field_names)

        sources_field = next(f for f in embed.fields if f.name == "Sources")
        self.assertIn("](https://minecraft.net/)", sources_field.value)
        self.assertIn("⭐", sources_field.value)
        self.assertIn("disagree", next(f for f in embed.fields if "Heads-up" in f.name).value)

    def test_empty_answer_is_friendly(self):
        embed = build_answer_embed(SearchAnswer(query="zxcvbnm nothing"))
        self.assertIn("couldn't retrieve", embed.description)

    def test_sources_only_answer(self):
        embed = build_answer_embed(SearchAnswer(
            query="obscure thing",
            sources=[result("Only link", "https://only.example", "", "only.example")],
        ))
        self.assertIn("sources below", embed.description.lower())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
