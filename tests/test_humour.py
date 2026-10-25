"""Unit tests for Paradise's humour update:
- English-only grammar & spelling nerd replies (excluding Hinglish/non-English)
- Random native Discord stickers & die-cut PNG sticker fallback
- Dynamic Pillow message-meme generation & replies
- Unpredictable smart/stupid chaos quips & rotating random statuses
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image
import discord

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config, PersonalityConfig
from functions.grammar_nerd import (
    detect_english_mistake,
    format_nerd_reply,
    is_english_sentence,
)
from functions.memes import (
    MEME_TEMPLATES,
    StickerPicker,
    clean_meme_text,
    render_meme_png,
    render_sticker_png,
)
from functions.personality import CHAOS_QUIPS, Personality
from functions.presence import PresenceManager


def _make_msg(
    content: str,
    *,
    author_id: int = 42,
    display_name: str = "Ash",
    channel_id: int = 10,
    guild_stickers: list | None = None,
):
    return SimpleNamespace(
        content=content,
        author=SimpleNamespace(id=author_id, bot=False, display_name=display_name),
        webhook_id=None,
        mentions=[],
        channel=SimpleNamespace(id=channel_id),
        guild=SimpleNamespace(id=1, stickers=guild_stickers or []),
        type=discord.MessageType.default,
        add_reaction=AsyncMock(),
        reply=AsyncMock(),
    )


class TestEnglishGrammarNerd(unittest.TestCase):
    def test_catches_common_english_grammar_mistakes(self):
        cases = (
            ("err its broken right now bro", "it's"),
            ("bro your kidding me right now", "you're"),
            ("he should of told us about this", "should've"),
            ("i care alot about this server", "a lot"),
            ("their is no way that worked", "there is"),
            ("there going to win the tournament", "they're going"),
            ("this update is way better then before", "better than"),
            ("that plan is more smarter honestly", "smarter"),
            ("bro i didnt knew about the meeting", "didn't know"),
            ("he ate a apple on stream today", "an apple"),
            ("we are gonna loose this match", "gonna lose"),
        )
        for sentence, expected_right in cases:
            mistake = detect_english_mistake(sentence)
            self.assertIsNotNone(mistake, f"Expected mistake in {sentence!r}")
            self.assertEqual(mistake.right, expected_right)
            reply = format_nerd_reply(mistake)
            self.assertIn("🤓", reply)
            self.assertIn(expected_right, reply)

    def test_catches_common_english_spelling_mistakes(self):
        cases = (
            ("if someone makes a grammer mistake here", "grammar"),
            ("that speling mistake was wild bro", "spelling"),
            ("i definately saw him in voice chat", "definitely"),
            ("did you recieve the file earlier", "receive"),
            ("we should keep those two channels seperate", "separate"),
            ("this whole situation is super wierd man", "weird"),
            ("are we playing ranked again tommorow night", "tomorrow"),
        )
        for sentence, expected_right in cases:
            mistake = detect_english_mistake(sentence)
            self.assertIsNotNone(mistake, f"Expected spelling catch in {sentence!r}")
            self.assertEqual(mistake.right, expected_right)

    def test_strictly_ignores_hinglish_hindi_and_other_languages(self):
        non_english = (
            "bhai kal milte hain pakka",
            "acha theek hai yaar koi baat nahi",
            "abey kya bol raha hai bro its crazy",
            "mujhe pata nahi tha ki yeh hoga",
            "नमस्ते भाई आज क्या प्लान है",
            "no puedo creer esto amigo mio",
            "je suis dans le salon vocal",
            "definately",  # isolated single word, not a sentence
        )
        for text in non_english:
            self.assertIsNone(
                detect_english_mistake(text),
                f"Should NOT flag non-English/fragment {text!r}",
            )

    def test_ignores_clean_english_uk_spellings_and_serious_topics(self):
        clean_or_exempt = (
            "we should plan the trip for saturday",
            "that colour is my favourite sense of humour",
            "it's honestly working on its own right now",
            "you're welcome to bring your friends tonight",
            "his grandfather passed away at the hospital definately sad",
        )
        for text in clean_or_exempt:
            self.assertIsNone(
                detect_english_mistake(text),
                f"Unexpected false positive in {text!r}",
            )
        self.assertTrue(is_english_sentence("we should plan the trip for saturday"))


class TestMemesAndStickers(unittest.TestCase):
    def test_all_meme_templates_render_valid_png(self):
        for tmpl in MEME_TEMPLATES:
            png_bytes, chosen = render_meme_png(
                "bro i swear one more game and im sleeping 😭",
                "Ladkaa",
                template=tmpl,
            )
            self.assertEqual(chosen, tmpl)
            with Image.open(io.BytesIO(png_bytes)) as img:
                self.assertEqual(img.format, "PNG")
                self.assertEqual(img.size, (720, 420))

    def test_vinyl_sticker_fallback_renders_rgba_png(self):
        png_bytes = render_sticker_png()
        with Image.open(io.BytesIO(png_bytes)) as img:
            self.assertEqual(img.format, "PNG")
            self.assertEqual(img.mode, "RGBA")
            self.assertEqual(img.size, (320, 320))

    def test_clean_meme_text_strips_mentions_and_urls(self):
        cleaned = clean_meme_text("yo <@12345> check https://example.com/test 💀")
        self.assertIn("@someone", cleaned)
        self.assertIn("[link]", cleaned)
        self.assertNotIn("https://", cleaned)

    def test_sticker_picker_uses_guild_and_standard_stickers(self):
        s1 = SimpleNamespace(id=11, name="kekw", available=True)
        s2 = SimpleNamespace(id=12, name="unavailable_one", available=False)
        std = SimpleNamespace(id=99, name="wumpus_wave", available=True)
        bot = SimpleNamespace(
            fetch_premium_sticker_packs=AsyncMock(
                return_value=[SimpleNamespace(stickers=[std])]
            )
        )
        picker = StickerPicker(bot)
        guild = SimpleNamespace(stickers=[s1, s2])

        picked = [
            asyncio.run(picker.pick(guild))
            for _ in range(10)
        ]
        self.assertTrue(all(p in (s1, std) for p in picked))
        self.assertNotIn(s2, picked)


class TestHumourPersonalityIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_nerd_grammar_reply_fires_and_prevents_double_reply(self):
        cfg = PersonalityConfig(
            enabled=True,
            reaction_chance=1.0,
            reply_chance=1.0,
            sticker_chance=1.0,
            meme_chance=1.0,
            random_quip_chance=1.0,
            grammar_chance=1.0,
            global_cooldown=0,
            channel_cooldown=0,
            grammar_cooldown=60,
        )
        bot = SimpleNamespace(user=SimpleNamespace(id=999))
        personality = Personality(bot, cfg)

        msg = _make_msg("i finally fixed it and its working great")
        await personality.handle(msg)

        msg.reply.assert_awaited_once()
        sent_text = msg.reply.call_args.args[0]
        self.assertIn("🤓", sent_text)
        self.assertIn("it's", sent_text)
        msg.add_reaction.assert_not_awaited()

    async def test_meme_reply_attaches_rendered_png(self):
        cfg = PersonalityConfig(
            enabled=True,
            reaction_chance=0.0,
            reply_chance=0.0,
            sticker_chance=0.0,
            meme_chance=1.0,
            random_quip_chance=0.0,
            grammar_chance=0.0,
            global_cooldown=0,
            channel_cooldown=0,
            humour_cooldown=0,
        )
        bot = SimpleNamespace(user=SimpleNamespace(id=999))
        personality = Personality(bot, cfg)

        msg = _make_msg("honestly i think cereal is technically cold soup")
        await personality.handle(msg)

        msg.reply.assert_awaited_once()
        attached: discord.File = msg.reply.call_args.kwargs["file"]
        self.assertIsInstance(attached, discord.File)
        self.assertEqual(attached.filename, "paradise_meme.png")

    async def test_sticker_reply_uses_native_guild_sticker_or_png_fallback(self):
        cfg = PersonalityConfig(
            enabled=True,
            reaction_chance=0.0,
            reply_chance=0.0,
            sticker_chance=1.0,
            meme_chance=0.0,
            random_quip_chance=0.0,
            grammar_chance=0.0,
            global_cooldown=0,
            channel_cooldown=0,
            humour_cooldown=0,
        )
        sticker = SimpleNamespace(id=501, name="cat_vibing", available=True)
        bot = SimpleNamespace(
            user=SimpleNamespace(id=999),
            fetch_premium_sticker_packs=AsyncMock(return_value=[]),
        )
        personality = Personality(bot, cfg)

        # Case 1: Guild has a custom sticker -> sends native Discord sticker
        msg1 = _make_msg(
            "we are planning the weekend tournament right now",
            guild_stickers=[sticker],
        )
        await personality.handle(msg1)
        msg1.reply.assert_awaited_once()
        self.assertEqual(msg1.reply.call_args.kwargs["stickers"], [sticker])

        # Case 2: Guild has 0 stickers -> falls back cleanly to custom vinyl sticker PNG
        msg2 = _make_msg(
            "another normal sentence in a stickerless server",
            author_id=43,
            guild_stickers=[],
        )
        await personality.handle(msg2)
        msg2.reply.assert_awaited_once()
        fallback_file = msg2.reply.call_args.kwargs["file"]
        self.assertIsInstance(fallback_file, discord.File)
        self.assertEqual(fallback_file.filename, "paradise_sticker.png")

    async def test_chaos_quip_replies_on_casual_chat(self):
        cfg = PersonalityConfig(
            enabled=True,
            reaction_chance=0.0,
            reply_chance=0.0,
            sticker_chance=0.0,
            meme_chance=0.0,
            random_quip_chance=1.0,
            grammar_chance=0.0,
            global_cooldown=0,
            channel_cooldown=0,
            humour_cooldown=0,
        )
        bot = SimpleNamespace(user=SimpleNamespace(id=999))
        personality = Personality(bot, cfg)

        msg = _make_msg("just sitting here drinking tea on a wednesday")
        await personality.handle(msg)

        msg.reply.assert_awaited_once()
        self.assertIn(msg.reply.call_args.args[0], CHAOS_QUIPS)

    def test_presence_picks_diverse_random_activities(self):
        manager = PresenceManager(SimpleNamespace())
        seen_labels = set()
        seen_types = set()
        for _ in range(80):
            act = manager.pick_activity()
            self.assertIsInstance(act, discord.BaseActivity)
            seen_types.add(act.type)
            seen_labels.add(getattr(act, "name", "") or getattr(act, "state", ""))
        self.assertGreater(len(seen_labels), 25)
        self.assertIn(discord.ActivityType.custom, seen_types)

    def test_config_parses_new_humour_env_vars(self):
        cfg = Config.from_env(
            {
                "PERSONALITY_STICKER_CHANCE": "0.08",
                "PERSONALITY_MEME_CHANCE": "0.07",
                "PERSONALITY_QUIP_CHANCE": "0.05",
                "PERSONALITY_GRAMMAR_CHANCE": "0.85",
                "PERSONALITY_HUMOUR_COOLDOWN": "90",
                "PERSONALITY_GRAMMAR_COOLDOWN": "60",
            }
        )
        self.assertEqual(cfg.personality.sticker_chance, 0.08)
        self.assertEqual(cfg.personality.meme_chance, 0.07)
        self.assertEqual(cfg.personality.random_quip_chance, 0.05)
        self.assertEqual(cfg.personality.grammar_chance, 0.85)
        self.assertEqual(cfg.personality.humour_cooldown, 90)
        self.assertEqual(cfg.personality.grammar_cooldown, 60)


if __name__ == "__main__":
    unittest.main()
