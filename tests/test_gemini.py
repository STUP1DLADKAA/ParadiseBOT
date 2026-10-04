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
from functions.ai import AIService
from functions.gemini import GeminiService
from functions.helpers import ParadiseError


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

    async def test_chat_falls_back_to_openai_compatible(self):
        config = Config(
            gemini=GeminiConfig(api_key="supersecretkey-123"),
            ai_base_url="http://local.test/v1",
            ai_api_key="",
            ai_model="llama",
        )
        ai = self.make_ai(config)
        # Gemini rejects (403) → provider should fall back transparently.
        ai.gemini._session = FakeSession(post_responses=[FakeResponse(403, text="bad")])
        import aiohttp  # real session used for the openai-compat leg; patch its post.

        class OpenAIFake(FakeSession):
            pass

        ai._session = OpenAIFake(post_responses=[
            FakeResponse(200, {"choices": [{"message": {"content": "fallback hi"}}]})
        ])
        out = await ai.chat([{"role": "user", "content": "hello"}])
        self.assertEqual(out, "fallback hi")
        request = ai._session.requests[0]
        self.assertEqual(request["url"], "http://local.test/v1/chat/completions")
        self.assertEqual(request["json"]["model"], "llama")
        self.assertFalse(ai.gemini.available)

    async def test_reason_uses_gemini_with_facts(self):
        ai = self.make_ai(make_config())
        ai.gemini._session = FakeSession(post_responses=[FakeResponse(200, gemini_payload("grounded"))])
        out = await ai.reason("what year?", ["it happened in 2024"])
        self.assertEqual(out, "grounded")

    async def test_validate_startup_skips_without_key(self):
        ai = self.make_ai(Config())
        self.assertFalse(await ai.validate_startup())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
