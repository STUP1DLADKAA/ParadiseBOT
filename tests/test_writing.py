"""Tests for the writing helper (functions/writing.py).

The writing modes are pure prompt construction around the model: what must
hold is the mode vocabulary, the tone detection, the copy-ready framing and
the fact that the payload reaches the model framed as the user's text.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from functions.writing import (
    WRITE_MODES, build_prompt, detect_tone, run_writing, strip_deixis,
    writing_system_prompt,
)


class FakeChatAI:
    def __init__(self, reply="a polished line"):
        self.reply = reply
        self.calls = []

    async def chat(self, messages, *, max_tokens=0, role="deep", caller_id=None):
        self.calls.append(
            {"messages": messages, "max_tokens": max_tokens, "role": role,
             "caller_id": caller_id}
        )
        return self.reply


class TestModes(unittest.TestCase):
    def test_mode_vocabulary(self):
        self.assertEqual(
            WRITE_MODES,
            {"rewrite", "announcement", "reply", "actions", "tone", "shorten", "expand"},
        )

    def test_every_mode_has_a_prompt(self):
        for mode in sorted(WRITE_MODES):
            overlay = writing_system_prompt(mode)
            self.assertTrue(overlay.strip(), mode)

    def test_text_producing_modes_insist_on_copy_ready_output(self):
        for mode in ("rewrite", "announcement", "reply", "actions", "shorten", "expand"):
            overlay = writing_system_prompt(mode)
            self.assertIn("copy", overlay.casefold(), mode)

    def test_unknown_mode_falls_back_to_rewrite(self):
        self.assertIn("rewrite", writing_system_prompt("not-a-mode").casefold())


class TestDetectTone(unittest.TestCase):
    def test_known_tones(self):
        self.assertEqual(detect_tone("make it formal please"), "formal")
        self.assertEqual(detect_tone("a sarcastic reply"), "sarcastic")
        self.assertEqual(detect_tone("gen z energy"), "gen z")

    def test_unknown_tone_is_casual(self):
        self.assertEqual(detect_tone("no tone word here"), "casual")
        self.assertEqual(detect_tone(""), "casual")
        self.assertEqual(detect_tone("whimsical"), "casual")


class TestStripDeixis(unittest.TestCase):
    def test_tone_prefix_is_stripped(self):
        self.assertEqual(
            strip_deixis("this to be formal: hey can u send it"),
            "hey can u send it",
        )
        self.assertEqual(strip_deixis("that in a casual tone: regards"), "regards")

    def test_plain_this_colon_is_stripped(self):
        self.assertEqual(strip_deixis("this: do the thing"), "do the thing")

    def test_text_without_deixis_is_untouched(self):
        self.assertEqual(
            strip_deixis("that sounds great, another meeting"),
            "that sounds great, another meeting",
        )


class TestBuildPrompt(unittest.TestCase):
    def test_rewrite_names_the_tone_and_keeps_the_text(self):
        prompt = build_prompt("rewrite", "this to be formal: yo send the file")
        self.assertIn("formal", prompt)
        self.assertIn("yo send the file", prompt)
        self.assertNotIn("this to be formal", prompt.replace("formal tone", ""))

    def test_announcement_mentions_the_event(self):
        prompt = build_prompt("announcement", "movie night friday 8pm", asker="Stupid")
        self.assertIn("movie night friday 8pm", prompt)
        self.assertIn("Stupid", prompt)

    def test_actions_and_tone_and_shorten_frame_the_text(self):
        for mode in ("actions", "tone", "shorten", "expand"):
            prompt = build_prompt(mode, "taher will book the server")
            self.assertIn("taher will book the server", prompt, mode)

    def test_reply_mode_includes_the_message_to_answer(self):
        prompt = build_prompt("reply", "sorry i forgot our plan")
        self.assertIn("sorry i forgot our plan", prompt)


class TestRunWriting(unittest.IsolatedAsyncioTestCase):
    async def test_happy_path_uses_deep_role_and_copy_ready_reply(self):
        ai = FakeChatAI(reply="  Here is your line.  ")
        result = await run_writing(
            "rewrite", "make this formal: hi team", ai, asker="Stupid", asker_id=42
        )
        self.assertEqual(result, "Here is your line.")
        self.assertEqual(len(ai.calls), 1)
        call = ai.calls[0]
        self.assertEqual(call["role"], "deep")
        self.assertEqual(call["caller_id"], 42)
        self.assertGreater(call["max_tokens"], 0)
        # the system prompt carries the writing overlay
        self.assertIn("rewrite", call["messages"][0]["content"].casefold())
        # the user prompt carries the payload
        self.assertIn("hi team", call["messages"][1]["content"])

    async def test_empty_model_reply_becomes_empty_string(self):
        ai = FakeChatAI(reply=None)
        self.assertEqual(await run_writing("tone", "some text", ai), "")

    async def test_provider_failure_propagates_to_the_caller(self):
        class FailingAI:
            async def chat(self, *args, **kwargs):
                raise RuntimeError("provider down")

        with self.assertRaises(RuntimeError):
            await run_writing("tone", "some text", FailingAI())


if __name__ == "__main__":
    unittest.main()
