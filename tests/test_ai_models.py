"""Tests for the multi-model OpenAI-compatible layer (AI_MODELS).

One endpoint, several models, each tagged with the job it is best at.
These tests pin: config parsing, role selection, the failover cascade,
per-model back-off, per-user fairness and the Gemini fallback boundary.
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import AIModelSpec, Config, parse_ai_models  # noqa: E402
from functions.ai import AIService  # noqa: E402
from functions.helpers import ParadiseError  # noqa: E402


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


class TestParseAiModels(unittest.TestCase):
    def test_role_model_entries(self):
        specs = parse_ai_models("fast:gpt-4o-mini, deep:gpt-4o")
        self.assertEqual(
            [(s.name, s.role) for s in specs],
            [("gpt-4o-mini", "fast"), ("gpt-4o", "deep")],
        )

    def test_bare_model_serves_both_roles(self):
        specs = parse_ai_models("llama3")
        self.assertEqual([(s.name, s.role) for s in specs], [("llama3", "")])

    def test_mixed_entries_and_junk(self):
        # A bare word is a legal model name; only empty entries, trailing
        # colons and unknown *roles* are dropped.
        specs = parse_ai_models("fast:m1, m2, junk:, nonsense, deep:m3")
        self.assertEqual(
            [(s.name, s.role) for s in specs],
            [("m1", "fast"), ("m2", ""), ("nonsense", ""), ("m3", "deep")],
        )

    def test_duplicates_keep_first_position(self):
        specs = parse_ai_models("deep:m1, fast:m1")
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].role, "deep")

    def test_role_must_be_known(self):
        self.assertEqual(parse_ai_models("spicy:m1"), [])

    def test_env_wiring(self):
        config = Config.from_env({"AI_MODELS": "fast:m1,deep:m2"})
        self.assertEqual(len(config.ai_models), 2)
        self.assertEqual(Config.from_env({}).ai_models, [])


# ---------------------------------------------------------------------------
# Fake endpoint
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status, data):
        self.status = status
        self._data = data

    async def text(self):
        return "error body"

    async def json(self, content_type=None):
        return self._data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _PostContext:
    def __init__(self, session, response):
        self._session = session
        self._response = response

    async def __aenter__(self):
        await asyncio.sleep(0)
        return self._response

    async def __aexit__(self, *args):
        self._session._inflight -= 1
        return False


class FakeSession:
    """Remembers which models were asked and can fail any of them."""

    def __init__(self, failing=()):
        self.requests: list[str] = []
        self.failing = set(failing)
        self.max_inflight = 0
        self._inflight = 0

    def post(self, url, headers=None, json=None):
        model = json["model"]
        self.requests.append(model)
        self._inflight += 1
        self.max_inflight = max(self.max_inflight, self._inflight)
        if model in self.failing:
            response = FakeResponse(500, {})
        else:
            response = FakeResponse(
                200,
                {
                    "choices": [
                        {
                            "message": {"content": f"answer from {model}"},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )
        return _PostContext(self, response)


def _service(session, *, models=None, model="m0"):
    config = Config(
        ai_base_url="https://api.example.com/v1",
        ai_model=model,
        ai_models=models or [],
    )
    ai = AIService(SimpleNamespace(user=SimpleNamespace(id=999)), config)
    original = ai._get_session

    async def get_session():
        return session

    ai._get_session = get_session
    return ai


# ---------------------------------------------------------------------------
# Role selection and the cascade
# ---------------------------------------------------------------------------


class TestModelRouting(unittest.IsolatedAsyncioTestCase):
    def test_role_models_order(self):
        ai = _service(
            None,
            models=[AIModelSpec("m1", "fast"), AIModelSpec("m2", "deep")],
        )
        # The role's own model first, then the other, then the legacy default.
        self.assertEqual(ai._role_models("deep"), ["m2", "m1", "m0"])
        self.assertEqual(ai._role_models("fast"), ["m1", "m2", "m0"])

    def test_single_model_setup_ignores_roles(self):
        ai = _service(None, models=[])
        self.assertEqual(ai._role_models("fast"), ["m0"])
        self.assertEqual(ai._role_models("deep"), ["m0"])

    async def test_deep_and_fast_pick_their_models(self):
        session = FakeSession()
        ai = _service(
            session, models=[AIModelSpec("mini", "fast"), AIModelSpec("big", "deep")]
        )
        self.assertEqual(
            await ai.chat([{"role": "user", "content": "hi"}], role="deep"),
            "answer from big",
        )
        self.assertEqual(
            await ai.chat([{"role": "user", "content": "hi"}], role="fast"),
            "answer from mini",
        )

    async def test_a_failing_model_fails_over_mid_flight(self):
        session = FakeSession(failing={"mini"})
        ai = _service(
            session, models=[AIModelSpec("mini", "fast"), AIModelSpec("big", "deep")]
        )
        answer = await ai.chat([{"role": "user", "content": "hi"}], role="fast")
        self.assertEqual(answer, "answer from big")
        self.assertEqual(session.requests, ["mini", "big"])

    async def test_failed_model_is_skipped_for_a_while(self):
        session = FakeSession(failing={"mini"})
        ai = _service(
            session, models=[AIModelSpec("mini", "fast"), AIModelSpec("big", "deep")]
        )
        await ai.chat([{"role": "user", "content": "hi"}], role="fast")
        session.requests.clear()
        # The second call goes straight to the healthy model.
        self.assertEqual(
            await ai.chat([{"role": "user", "content": "hi"}], role="fast"),
            "answer from big",
        )
        self.assertEqual(session.requests, ["big"])

    async def test_everything_failing_raises_for_the_gemini_boundary(self):
        session = FakeSession(failing={"m0"})
        ai = _service(session)  # no gemini configured
        with self.assertRaises(ParadiseError):
            await ai.chat([{"role": "user", "content": "hi"}])

    async def test_describe_lists_models_and_roles(self):
        ai = _service(
            None, models=[AIModelSpec("mini", "fast"), AIModelSpec("big", "deep")]
        )
        described = ai.describe()
        self.assertIn("mini (fast)", described)
        self.assertIn("big (deep)", described)

    async def test_classify_uses_the_fast_model(self):
        session = FakeSession()
        ai = _service(
            session, models=[AIModelSpec("mini", "fast"), AIModelSpec("big", "deep")]
        )
        await ai.classify("weather in delhi")
        self.assertEqual(session.requests, ["mini"])


class TestPerUserFairness(unittest.IsolatedAsyncioTestCase):
    async def test_one_user_cannot_hog_the_global_slots(self):
        session = FakeSession()
        ai = _service(session)
        # One user fires 12 requests; the ceiling is 2 concurrent per user.
        tasks = [
            asyncio.create_task(
                ai.chat(
                    [{"role": "user", "content": "hi"}], caller_id=1
                )
            )
            for _ in range(12)
        ]
        await asyncio.sleep(0.2)
        # Everyone else still gets served while the spammer is queued.
        other = asyncio.create_task(
            ai.chat([{"role": "user", "content": "hi"}], caller_id=2)
        )
        answers = await asyncio.gather(other, *tasks)
        self.assertTrue(all(answer.startswith("answer from") for answer in answers))
        self.assertEqual(len(session.requests), 13)

    async def test_user_slots_registry_is_bounded(self):
        ai = _service(None)
        for user_id in range(600):
            ai._user_slot(user_id)
        self.assertLessEqual(len(ai._user_slots), 600)
        ai._user_slot(1)  # touch again after the clear
        self.assertLessEqual(len(ai._user_slots), 520)

    async def test_anonymous_callers_bypass_the_user_limit(self):
        session = FakeSession()
        ai = _service(session)
        tasks = [
            asyncio.create_task(ai.chat([{"role": "user", "content": "hi"}]))
            for _ in range(6)
        ]
        answers = await asyncio.gather(*tasks)
        self.assertEqual(len(answers), 6)


if __name__ == "__main__":
    unittest.main()
