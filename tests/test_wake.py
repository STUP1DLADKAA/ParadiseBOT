"""Tests for the “Hey Paradise …” voice-chat assistant (offline)."""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from commands.wake import REFUSAL_LINES, Wake, match_wake
from config import Config, WakeConfig
from functions.helpers import ParadiseError


class TestWakePhrase(unittest.TestCase):
    def test_common_greetings_are_recognised(self):
        for text in (
            "hey paradise what's the capital of Rajasthan?",
            "Hey Paradise, how are you",
            "hi paradise tell me a joke",
            "yo paradise 2+2",
            "okay paradise who won the match",
            "oye paradise kya haal hai",
            "arre paradise explain gravity",
            "HEY PARADISE  what is 10% of 90",
        ):
            self.assertIsNotNone(match_wake(text), text)

    def test_question_is_extracted_cleanly(self):
        self.assertEqual(
            match_wake("Hey Paradise,   what is the time in Tokyo?"),
            "what is the time in Tokyo?",
        )
        self.assertEqual(match_wake("hey paradise"), "")
        self.assertEqual(match_wake("Paradise, tell me a joke"), "tell me a joke")

    def test_ordinary_sentences_do_not_trigger(self):
        for text in (
            "paradise is a nice word",
            "I love this paradise server",
            "what does paradise mean",
            "hey what time is it",
        ):
            self.assertIsNone(match_wake(text), text)


def _make_channel(*, is_voice: bool = True, can_send: bool = True):
    if is_voice:
        channel = MagicMock(spec=discord.VoiceChannel)
    else:
        channel = MagicMock(spec=discord.TextChannel)
    channel.id = 4242
    channel.name = "voice-chat"
    channel.send = AsyncMock()
    channel.permissions_for = MagicMock(
        return_value=SimpleNamespace(
            send_messages=can_send, connect=True, speak=True, view_channel=True
        )
    )
    return channel


def _make_message(channel, *, content: str, author_id: int = 5):
    message = MagicMock()
    message.content = content
    message.channel = channel
    message.author = SimpleNamespace(
        id=author_id, bot=False, display_name="Tester", voice=None
    )
    message.guild = SimpleNamespace(
        id=99, name="Test Server", me=SimpleNamespace(id=1)
    )
    message.reply = AsyncMock()
    return message


class _FakeAI:
    def __init__(self, answer: str | None = None, error: Exception | None = None):
        self.answer = answer
        self.error = error
        self.calls: list[dict] = []

    async def answer_quick(self, prompt, *, models=(), system="", max_tokens=320):
        self.calls.append({"prompt": prompt, "models": models, "system": system})
        if self.error is not None:
            raise self.error
        return self.answer


class WakeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ai = _FakeAI(answer="Jaipur is the capital of Rajasthan.")
        self.config = Config.from_env({"WAKE_WORD_SPEAK": "false"})
        self.bot = SimpleNamespace(
            user=SimpleNamespace(id=1),
            ai=self.ai,
            guilds=[],
            config=self.config,
            get_cog=lambda name: None,
            get_guild=lambda gid: None,
        )
        self.cog = Wake(self.bot)
        await self.cog.cog_load()

    async def asyncTearDown(self):
        await self.cog.cog_unload()

    async def test_answers_in_the_voice_chat(self):
        channel = _make_channel()
        message = _make_message(channel, content="hey paradise capital of rajasthan?")
        handled = await self.cog.handle_message(message)
        self.assertTrue(handled)
        message.reply.assert_awaited_once()
        self.assertIn("Jaipur", message.reply.await_args.args[0])
        self.assertEqual(len(self.ai.calls), 1)
        # The fastest-first cascade from config is passed through.
        self.assertEqual(
            tuple(self.ai.calls[0]["models"]), self.config.wake.models
        )

    async def test_ignores_normal_text_channels(self):
        channel = _make_channel(is_voice=False)
        message = _make_message(channel, content="hey paradise hello")
        self.assertFalse(await self.cog.handle_message(message))
        message.reply.assert_not_awaited()

    async def test_ignores_bots_and_unrelated_messages(self):
        channel = _make_channel()
        message = _make_message(channel, content="hey paradise hi")
        message.author = SimpleNamespace(id=2, bot=True, display_name="Bot", voice=None)
        self.assertFalse(await self.cog.handle_message(message))

        message = _make_message(channel, content="just chatting")
        self.assertFalse(await self.cog.handle_message(message))

    async def test_empty_question_gets_a_short_nudge(self):
        channel = _make_channel()
        message = _make_message(channel, content="hey paradise")
        self.assertTrue(await self.cog.handle_message(message))
        self.assertIn("ask me something", message.reply.await_args.args[0])
        self.assertEqual(self.ai.calls, [])

    async def test_all_models_failing_uses_the_rude_refusal(self):
        self.ai.error = ParadiseError("every Gemini model is unavailable")
        channel = _make_channel()
        message = _make_message(channel, content="hey paradise explain time travel")
        self.assertTrue(await self.cog.handle_message(message))
        answer = message.reply.await_args.args[0]
        self.assertIn(answer, REFUSAL_LINES)

    async def test_unexpected_error_also_refuses_instead_of_crashing(self):
        self.ai.error = RuntimeError("boom")
        channel = _make_channel()
        message = _make_message(channel, content="hey paradise hi there")
        self.assertTrue(await self.cog.handle_message(message))
        self.assertIn(message.reply.await_args.args[0], REFUSAL_LINES)

    async def test_no_send_permission_means_no_reply(self):
        channel = _make_channel(can_send=False)
        message = _make_message(channel, content="hey paradise hi")
        self.assertFalse(await self.cog.handle_message(message))
        message.reply.assert_not_awaited()

    async def test_cooldown_swallows_a_burst(self):
        channel = _make_channel()
        first = _make_message(channel, content="hey paradise one")
        second = _make_message(channel, content="hey paradise two")
        self.assertTrue(await self.cog.handle_message(first))
        self.assertTrue(await self.cog.handle_message(second))
        self.assertEqual(first.reply.await_count, 1)
        second.reply.assert_not_awaited()

    async def test_answer_text_is_capped(self):
        self.ai.answer = "x" * 5000
        channel = _make_channel()
        message = _make_message(channel, content="hey paradise ramble")
        self.assertTrue(await self.cog.handle_message(message))
        posted = message.reply.await_args.args[0]
        self.assertLessEqual(len(posted), self.config.wake.max_answer_chars)

    async def test_disabled_cog_does_nothing(self):
        cog = Wake(self.bot, config=WakeConfig(enabled=False))
        channel = _make_channel()
        message = _make_message(channel, content="hey paradise hi")
        self.assertFalse(await cog.handle_message(message))
        await cog.speech.close()


if __name__ == "__main__":
    unittest.main()
