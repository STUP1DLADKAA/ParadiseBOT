"""Unit tests for the Gemini client, config plumbing, and AI provider chain.

All offline: HTTP is faked at the session boundary.
"""

from __future__ import annotations

import os
import sys
import unittest
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config, GeminiConfig
from functions.ai import AIService, SYSTEM_PROMPT, _USER_COOLDOWN, _CHANNEL_COOLDOWN
from functions.gemini import GeminiService
from functions.helpers import ParadiseError
from functions.memory import MemoryStore


# ---------------------------------------------------------------------------
# Fake transport
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status=200, payload=None, text=""):
        self.status = status
        self._payload = payload if payload is not None else {}
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def json(self, content_type=None):
        return self._payload

    async def text(self):
        return self._text


class FakeSession:
    """Records requests; replays queued responses for post/get."""

    def __init__(self, post_responses=None, get_responses=None, *, closed=False):
        self.post_responses = list(post_responses or [])
        self.get_responses = list(get_responses or [])
        self.requests = []
        self.closed = closed

    def post(self, url, headers=None, json=None, **kwargs):
        self.requests.append({"method": "POST", "url": url, "headers": headers or {}, "json": json})
        if not self.post_responses:
            raise RuntimeError("no queued response")
        return self.post_responses.pop(0)

    def get(self, url, headers=None, **kwargs):
        self.requests.append({"method": "GET", "url": url, "headers": headers or {}})
        if not self.get_responses:
            raise RuntimeError("no queued response")
        return self.get_responses.pop(0)


def gemini_payload(text="a grounded answer"):
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def make_config(**gemini_overrides):
    gemini = GeminiConfig(**{"api_key": "supersecretkey-123", **gemini_overrides})
    return Config(gemini=gemini)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestGeminiConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = GeminiConfig.from_env({})
        self.assertFalse(cfg.configured)
        self.assertEqual(cfg.model, "gemini-2.5-flash")  # documented default, env-overridable
        self.assertTrue(cfg.enabled)

    def test_from_env(self):
        cfg = GeminiConfig.from_env({
            "GEMINI_API_KEY": "  key-abc ",
            "GEMINI_MODEL": "gemini-2.0-flash",
            "GEMINI_ENABLED": "true",
        })
        self.assertTrue(cfg.configured)
        self.assertEqual(cfg.api_key, "key-abc")
        self.assertEqual(cfg.model, "gemini-2.0-flash")

    def test_disabled_means_unconfigured(self):
        cfg = GeminiConfig.from_env({"GEMINI_API_KEY": "key-abc", "GEMINI_ENABLED": "false"})
        self.assertFalse(cfg.configured)

    def test_masked_key_never_contains_full_key(self):
        cfg = GeminiConfig(api_key="supersecretkey-123")
        masked = cfg.masked_key
        self.assertNotIn("supersecretkey", masked)
        self.assertIn("…", masked)
        short = GeminiConfig(api_key="abc")
        self.assertEqual(short.masked_key, "…")

    def test_config_ai_configured_chain(self):
        # Gemini key configured → AI available
        self.assertTrue(make_config().ai_configured)
        # No Gemini, but OpenAI-compatible → still available
        cfg = Config(ai_base_url="http://local/v1", ai_model="llama")
        self.assertTrue(cfg.ai_configured)
        # Nothing → unavailable but bot works
        self.assertFalse(Config().ai_configured)


# ---------------------------------------------------------------------------
# GeminiService
# ---------------------------------------------------------------------------


class TestGeminiService(unittest.IsolatedAsyncioTestCase):
    def service(self, session: FakeSession, **overrides) -> GeminiService:
        return GeminiService(GeminiConfig(api_key="supersecretkey-123", **overrides), session=session)

    async def test_generate_success(self):
        session = FakeSession(post_responses=[FakeResponse(200, gemini_payload("hello there"))])
        svc = self.service(session)
        answer = await svc.generate("say hi", system="be nice", max_tokens=100)
        self.assertEqual(answer, "hello there")

        request = session.requests[0]
        # THE KEY RULE: the key goes in the header, NEVER in the URL.
        self.assertEqual(request["headers"].get("x-goog-api-key"), "supersecretkey-123")
        self.assertNotIn("supersecretkey", request["url"])
        self.assertNotIn("key=", urlparse(request["url"]).query)
        # Model name is configurable, not hard-coded.
        self.assertIn("gemini-2.5-flash:generateContent", request["url"])
        # Contents + system instruction are sent.
        payload = request["json"]
        self.assertEqual(payload["contents"][0]["parts"][0]["text"], "say hi")
        self.assertIn("be nice", str(payload["systemInstruction"]))

    async def test_generate_custom_model_in_url(self):
        session = FakeSession(post_responses=[FakeResponse(200, gemini_payload())])
        svc = self.service(session, model="gemini-2.0-pro")
        await svc.generate("q")
        self.assertIn("models/gemini-2.0-pro:generateContent", session.requests[0]["url"])

    async def test_not_configured_raises(self):
        svc = GeminiService(GeminiConfig())
        with self.assertRaises(ParadiseError):
            await svc.generate("anything")

    async def test_rejected_key_disables_provider(self):
        session = FakeSession(post_responses=[FakeResponse(403, text="API key not valid")])
        svc = self.service(session)
        self.assertTrue(svc.available)
        with self.assertRaises(ParadiseError) as ctx:
            await svc.generate("q")
        self.assertIn("key", str(ctx.exception).casefold())
        self.assertFalse(svc.available)
        self.assertEqual(svc.disabled_reason, "API key rejected")

        # Later calls short-circuit without touching the network.
        with self.assertRaises(ParadiseError):
            await svc.generate("q2")
        self.assertEqual(len(session.requests), 1)

    async def test_quota_cooldown(self):
        session = FakeSession(post_responses=[FakeResponse(429, text="quota")])
        svc = self.service(session)
        with self.assertRaises(ParadiseError) as ctx:
            await svc.generate("q")
        self.assertIn("busy", str(ctx.exception))
        self.assertTrue(svc.available)  # transient, not disabled
        with self.assertRaises(ParadiseError) as ctx2:
            await svc.generate("q2")
        self.assertIn("cooling down", str(ctx2.exception))
        self.assertEqual(len(session.requests), 1)

    async def test_unknown_model(self):
        session = FakeSession(post_responses=[FakeResponse(404, text="model not found")])
        svc = self.service(session)
        with self.assertRaises(ParadiseError) as ctx:
            await svc.generate("q")
        self.assertIn("gemini-2.5-flash", str(ctx.exception))

    async def test_empty_payload(self):
        session = FakeSession(post_responses=[FakeResponse(200, {"candidates": []})])
        svc = self.service(session)
        with self.assertRaises(ParadiseError):
            await svc.generate("q")

    async def test_reason_prompt_carries_facts(self):
        session = FakeSession(post_responses=[FakeResponse(200, gemini_payload("from facts"))])
        svc = self.service(session)
        out = await svc.reason("when is the update?", ["Update released in June 2026 (official site)"])
        self.assertEqual(out, "from facts")
        prompt = session.requests[0]["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("June 2026", prompt)
        self.assertIn("when is the update?", prompt)
        self.assertIn("VERIFIED FACTS", prompt)

    async def test_validate_success(self):
        session = FakeSession(get_responses=[FakeResponse(200, {"name": "models/gemini-2.5-flash"})])
        svc = self.service(session)
        self.assertTrue(await svc.validate())
        self.assertTrue(svc.validated)
        # Key in header only, model name in URL path.
        self.assertNotIn("supersecretkey", session.requests[0]["url"])
        self.assertIn("x-goog-api-key", session.requests[0]["headers"])

    async def test_validate_rejected(self):
        session = FakeSession(get_responses=[FakeResponse(401, text="bad key")])
        svc = self.service(session)
        self.assertFalse(await svc.validate())
        self.assertFalse(svc.validated)
        self.assertFalse(svc.available)

    async def test_validate_offline_is_not_fatal(self):
        class BrokenSession(FakeSession):
            def get(self, *a, **k):
                raise OSError("no network")

        svc = self.service(BrokenSession())
        self.assertFalse(await svc.validate())
        self.assertTrue(svc.available)  # network blip ≠ bad key

    async def test_describe_masks_key(self):
        svc = self.service(FakeSession())
        text = svc.describe()
        self.assertIn("gemini-2.5-flash", text)
        self.assertNotIn("supersecretkey", text.replace(svc.masked_key, ""))


# ---------------------------------------------------------------------------
# AIService provider chain
# ---------------------------------------------------------------------------


class FakeBot:
    user = None


class TestAIProviderChain(unittest.IsolatedAsyncioTestCase):
    def make_ai(self, config) -> AIService:
        svc = AIService(FakeBot(), config)
        return svc

    async def test_provider_none(self):
        ai = self.make_ai(Config())
        self.assertFalse(ai.available)
        self.assertEqual(ai.provider_name, "none")
        with self.assertRaises(ParadiseError):
            await ai.chat([{"role": "user", "content": "hi"}])
        with self.assertRaises(ParadiseError):
            await ai.reason("q", ["f"])

    async def test_provider_gemini(self):
        ai = self.make_ai(make_config())
        ai.gemini._session = FakeSession(post_responses=[FakeResponse(200, gemini_payload("gemini says hi"))])
        self.assertTrue(ai.available)
        self.assertEqual(ai.provider_name, "gemini")
        out = await ai.chat([{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}])
        self.assertEqual(out, "gemini says hi")

    async def test_openai_compatible_is_the_primary_provider(self):
        config = Config(
            gemini=GeminiConfig(api_key="supersecretkey-123"),
            ai_base_url="http://local.test/v1",
            ai_api_key="",
            ai_model="llama",
        )
        ai = self.make_ai(config)
        self.assertEqual(ai.provider_name, "openai-compatible")
        # No queued Gemini response: if the fallback were contacted the
        # fake session would raise and fail this test.
        ai.gemini._session = FakeSession(post_responses=[])
        ai._session = FakeSession(post_responses=[
            FakeResponse(200, {"choices": [{"message": {"content": "primary hi"}}]})
        ])
        out = await ai.chat([{"role": "user", "content": "hello"}])
        self.assertEqual(out, "primary hi")
        request = ai._session.requests[0]
        self.assertEqual(request["url"], "http://local.test/v1/chat/completions")
        self.assertEqual(request["json"]["model"], "llama")
        self.assertEqual(ai.gemini._session.requests, [])

    async def test_chat_falls_back_to_gemini(self):
        config = Config(
            gemini=GeminiConfig(api_key="supersecretkey-123"),
            ai_base_url="http://local.test/v1",
            ai_api_key="",
            ai_model="llama",
        )
        ai = self.make_ai(config)
        # Primary provider returns 500 → Gemini answers transparently.
        ai._session = FakeSession(post_responses=[FakeResponse(500, text="boom")])
        ai.gemini._session = FakeSession(
            post_responses=[FakeResponse(200, gemini_payload("gemini fallback hi"))]
        )
        out = await ai.chat([{"role": "user", "content": "hello"}])
        self.assertEqual(out, "gemini fallback hi")
        self.assertEqual(len(ai._session.requests), 1)
        self.assertTrue(ai._session.requests[0]["url"].endswith("/chat/completions"))
        self.assertIn(":generateContent", ai.gemini._session.requests[0]["url"])

    async def test_answer_question_uses_the_primary_provider(self):
        config = Config(
            gemini=GeminiConfig(api_key="supersecretkey-123"),
            ai_base_url="http://local.test/v1",
            ai_model="llama",
        )
        ai = self.make_ai(config)
        ai.gemini._session = FakeSession(post_responses=[])
        ai._session = FakeSession(post_responses=[
            FakeResponse(200, {"choices": [{"message": {"content": "an unhinged answer"}}]})
        ])
        out = await ai.answer_question("what is a black hole", asker="Tester")
        self.assertEqual(out, "an unhinged answer")
        messages = ai._session.requests[0]["json"]["messages"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("ParadiseBOT", messages[0]["content"])
        self.assertIn("Tester asks you: what is a black hole", messages[1]["content"])
        self.assertEqual(ai.gemini._session.requests, [])

    async def test_answer_question_without_ai_raises(self):
        ai = self.make_ai(Config())
        with self.assertRaises(ParadiseError):
            await ai.answer_question("anything")

    async def test_reason_uses_gemini_with_facts(self):
        ai = self.make_ai(make_config())
        ai.gemini._session = FakeSession(post_responses=[FakeResponse(200, gemini_payload("grounded"))])
        out = await ai.reason("what year?", ["it happened in 2024"])
        self.assertEqual(out, "grounded")

    async def test_reason_prefers_the_primary_provider(self):
        config = Config(
            gemini=GeminiConfig(api_key="supersecretkey-123"),
            ai_base_url="http://local.test/v1",
            ai_model="llama",
        )
        ai = self.make_ai(config)
        ai.gemini._session = FakeSession(post_responses=[])
        ai._session = FakeSession(post_responses=[
            FakeResponse(200, {"choices": [{"message": {"content": "from facts"}}]})
        ])
        out = await ai.reason("what year?", ["it happened in 2024"])
        self.assertEqual(out, "from facts")
        prompt = ai._session.requests[0]["json"]["messages"][0]["content"]
        self.assertIn("VERIFIED FACTS", prompt)
        self.assertEqual(ai.gemini._session.requests, [])

    async def test_classify_prefers_the_primary_provider(self):
        config = Config(
            gemini=GeminiConfig(api_key="supersecretkey-123"),
            ai_base_url="http://local.test/v1",
            ai_model="llama",
        )
        ai = self.make_ai(config)
        ai.gemini._session = FakeSession(post_responses=[])
        ai._session = FakeSession(post_responses=[
            FakeResponse(
                200,
                {"choices": [{"message": {"content": '{"intent":"chat"}'}}]},
            )
        ])
        out = await ai.classify("got any red panda content")
        self.assertIn("chat", out)
        self.assertEqual(ai.gemini._session.requests, [])

    async def test_classify_falls_back_to_gemini(self):
        config = Config(
            gemini=GeminiConfig(api_key="supersecretkey-123"),
            ai_base_url="http://local.test/v1",
            ai_model="llama",
        )
        ai = self.make_ai(config)
        ai._session = FakeSession(post_responses=[FakeResponse(500, text="boom")])
        ai.gemini._session = FakeSession(
            post_responses=[FakeResponse(200, gemini_payload('{"intent":"chat"}'))]
        )
        out = await ai.classify("got any red panda content")
        self.assertIn("chat", out)
        self.assertIn(":generateContent", ai.gemini._session.requests[0]["url"])

    def _primary_ai(self, **config_overrides):
        config = Config(
            gemini=GeminiConfig(api_key="supersecretkey-123"),
            ai_base_url="http://local.test/v1",
            ai_model="llama",
            **config_overrides,
        )
        ai = self.make_ai(config)
        ai.gemini._session = FakeSession(post_responses=[])
        ai._session = FakeSession(post_responses=[
            FakeResponse(200, {"choices": [{"message": {"content": "primary answer"}}]})
        ])
        return ai

    async def test_the_system_prompt_is_short_and_light(self):
        # Short prompt = faster replies; the persona is funny, not brutal.
        self.assertLess(len(SYSTEM_PROMPT), 4000)
        lowered = SYSTEM_PROMPT.lower()
        self.assertIn("1-3 sentences", lowered)
        self.assertIn("never refuse a genuine question", lowered)
        self.assertIn("my lord", lowered)
        self.assertNotIn("ragebait", lowered)
        self.assertNotIn("unhinged", lowered)

    async def test_the_lord_gets_special_treatment(self):
        ai = self._primary_ai(lord_user_id=922359954025885737)
        out = await ai.answer_question(
            "what should we eat tonight",
            asker="Stupid",
            asker_id=922359954025885737,
        )
        self.assertEqual(out, "primary answer")
        content = ai._session.requests[0]["json"]["messages"][1]["content"]
        self.assertIn("lord and master", content)
        self.assertIn("Stupid asks you: what should we eat tonight", content)

    async def test_everyone_else_gets_no_lord_note(self):
        ai = self._primary_ai(lord_user_id=922359954025885737)
        await ai.answer_question("hello?", asker="Rohan", asker_id=555)
        content = ai._session.requests[0]["json"]["messages"][1]["content"]
        self.assertNotIn("lord and master", content)

    async def test_answers_fold_in_remembered_facts(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            ai = self._primary_ai()
            ai.bot.memory = MemoryStore(Path(tmp) / "memory.db")
            ai.bot.memory.add("Rohan only plays Valorant, badly")
            await ai.answer_question(
                "who plays valorant in this server", asker="Rohan"
            )
            content = ai._session.requests[0]["json"]["messages"][1]["content"]
            self.assertIn("Rohan only plays Valorant, badly", content)
            self.assertIn("remember", content)

    async def test_a_broken_memory_never_breaks_the_answer(self):
        class ExplodingStore:
            def relevant(self, question, limit=6):
                raise RuntimeError("database on fire")

        ai = self._primary_ai()
        ai.bot.memory = ExplodingStore()
        out = await ai.answer_question("anything at all", asker="Rohan")
        self.assertEqual(out, "primary answer")

    async def test_tease_describes_the_target_and_their_words(self):
        ai = self._primary_ai()
        out = await ai.tease("Rohan", "i lost to a bot at chess", target_id=555)
        self.assertEqual(out, "primary answer")
        messages = ai._session.requests[0]["json"]["messages"]
        self.assertEqual(messages[0]["content"], SYSTEM_PROMPT)
        self.assertIn("Rohan", messages[1]["content"])
        self.assertIn("i lost to a bot at chess", messages[1]["content"])
        self.assertIn("LIGHT-HEARTED", messages[1]["content"])
        self.assertNotIn("my lord", messages[1]["content"])

    async def test_teasing_the_lord_stays_loyal(self):
        ai = self._primary_ai(lord_user_id=922359954025885737)
        await ai.tease(
            "Stupid", "i deleted the database for fun", target_id=922359954025885737
        )
        content = ai._session.requests[0]["json"]["messages"][1]["content"]
        self.assertIn("my lord", content)
        self.assertIn("loyal", content)

    async def test_tease_without_ai_raises(self):
        ai = self.make_ai(Config())
        with self.assertRaises(ParadiseError):
            await ai.tease("Rohan", "anything")

    async def test_improvise_answers_one_line_in_character(self):
        ai = self._primary_ai()
        out = await ai.improvise("the server has been dead for 40 minutes")
        self.assertEqual(out, "primary answer")
        messages = ai._session.requests[0]["json"]["messages"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[0]["content"], SYSTEM_PROMPT)
        self.assertIn("dead for 40 minutes", messages[1]["content"])
        self.assertLessEqual(
            ai._session.requests[0]["json"]["max_tokens"], 200
        )

    async def test_improvise_without_a_prompt_raises(self):
        ai = self._primary_ai()
        with self.assertRaises(ParadiseError):
            await ai.improvise("   ")

    def test_response_budgets_stay_small(self):
        # Small budgets = short replies and faster turns.
        self.assertEqual(AIService._response_budget("hi"), 200)
        self.assertEqual(
            AIService._response_budget(
                "what is the best phone to buy for my little sister this year"
            ),
            300,
        )
        self.assertEqual(
            AIService._response_budget(
                "explain the difference between tcp and udp for a school essay"
            ),
            500,
        )
        self.assertLess(_USER_COOLDOWN, 15)
        self.assertLess(_CHANNEL_COOLDOWN, 8)

    async def test_validate_startup_skips_without_key(self):
        ai = self.make_ai(Config())
        self.assertFalse(await ai.validate_startup())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
