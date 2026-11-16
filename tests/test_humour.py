"""Unit tests for Paradise's humour update:
- English-only grammar & spelling nerd replies (excluding Hinglish/non-English)
- Random native Discord stickers & die-cut PNG sticker fallback
- Dynamic Pillow message-meme generation & replies
- Unpredictable smart/stupid chaos quips & rotating random statuses
- Conversation interjections ("hello!" when two people are talking)
- The lord's acknowledgement (crown him when he speaks)
- Context roasts (quoting someone's own words back at them)
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
from functions.personality import (
    AMBIENT_REACTIONS,
    LORD_REACTIONS,
    PATTERNS,
    Personality,
)
from functions.presence import PresenceManager
from functions.roasts import (
    CONTEXT_ROAST_FRAMES,
    LORD_CONTEXT_FRAMES,
    build_context_roast,
    clean_snippet,
)


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


class TestSupremeLoyalty(unittest.IsolatedAsyncioTestCase):
    """Mentioning a lord's name gets at most a loyalty reaction — never a text reply."""

    def _personality(self):
        cfg = PersonalityConfig(
            enabled=True,
            reaction_chance=1.0,
            ambient_reaction_chance=0.0,
            lord_reaction_chance=0.0,
            global_cooldown=0,
            channel_cooldown=0,
            user_cooldown=0,
            lord_cooldown=0,
            ambient_hourly_cap=0,
        )
        return Personality(SimpleNamespace(user=SimpleNamespace(id=999)), cfg)

    async def test_mentioning_stupid_gets_loyalty_reaction_only(self):
        personality = self._personality()
        msg = _make_msg("stupid is so bad at this game lol")
        await personality.handle(msg)
        msg.reply.assert_not_awaited()
        msg.add_reaction.assert_awaited_once()
        loyalty = next(p for p in PATTERNS if p.name == "loyalty")
        self.assertIn(msg.add_reaction.call_args.args[0], loyalty.reactions)

    async def test_the_owner_phrase_gets_loyalty_reaction_only(self):
        personality = self._personality()
        msg = _make_msg("i am stupid")
        await personality.handle(msg)
        msg.reply.assert_not_awaited()
        msg.add_reaction.assert_awaited_once()
        loyalty = next(p for p in PATTERNS if p.name == "loyalty")
        self.assertIn(msg.add_reaction.call_args.args[0], loyalty.reactions)

    async def test_mentioning_taher_gets_loyalty_reaction_only(self):
        personality = self._personality()
        msg = _make_msg("taher is so bad at this game lol")
        await personality.handle(msg)
        msg.reply.assert_not_awaited()
        msg.add_reaction.assert_awaited_once()
        loyalty_taher = next(p for p in PATTERNS if p.name == "loyalty-taher")
        self.assertIn(msg.add_reaction.call_args.args[0], loyalty_taher.reactions)

    async def test_plain_message_gets_neither_reply_nor_loyalty_reaction(self):
        personality = self._personality()
        msg = _make_msg("honestly i think cereal is technically cold soup")
        await personality.handle(msg)
        msg.reply.assert_not_awaited()
        msg.add_reaction.assert_not_awaited()


class TestHumourPersonalityIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_grammar_slips_never_trigger_automatic_text_reply(self):
        cfg = PersonalityConfig(
            enabled=True,
            reaction_chance=1.0,
            ambient_reaction_chance=1.0,
            lord_reaction_chance=1.0,
            global_cooldown=0,
            channel_cooldown=0,
            user_cooldown=0,
            ambient_hourly_cap=0,
        )
        bot = SimpleNamespace(user=SimpleNamespace(id=999))
        personality = Personality(bot, cfg)

        msg = _make_msg("i finally fixed it and its working great")
        await personality.handle(msg)

        msg.reply.assert_not_awaited()
        msg.add_reaction.assert_awaited_once()

    async def test_casual_chat_never_sends_automatic_meme_gif_sticker_or_quip(self):
        sticker = SimpleNamespace(id=501, name="cat_vibing", available=True)
        feed = SimpleNamespace(next_meme=AsyncMock())
        gifs = SimpleNamespace(available=True, search=AsyncMock())
        cfg = PersonalityConfig(
            enabled=True,
            reaction_chance=1.0,
            ambient_reaction_chance=1.0,
            lord_reaction_chance=1.0,
            global_cooldown=0,
            channel_cooldown=0,
            user_cooldown=0,
            ambient_hourly_cap=0,
        )
        bot = SimpleNamespace(user=SimpleNamespace(id=999))
        personality = Personality(bot, cfg, meme_feed=feed, gif_search=gifs)

        for idx, line in enumerate(
            (
                "honestly i think cereal is technically cold soup",
                "we are planning the weekend tournament right now",
                "just sitting here drinking tea on a wednesday",
            )
        ):
            msg = _make_msg(
                line,
                author_id=40 + idx,
                channel_id=10 + idx,
                guild_stickers=[sticker],
            )
            await personality.handle(msg)
            msg.reply.assert_not_awaited()
            msg.add_reaction.assert_awaited_once()
            self.assertIn(msg.add_reaction.call_args.args[0], AMBIENT_REACTIONS)

        feed.next_meme.assert_not_awaited()
        gifs.search.assert_not_awaited()

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


class TestConversationsAndTheLord(unittest.IsolatedAsyncioTestCase):
    """Conversations never get auto-replies; lords get only rare crown reactions."""

    LORD_ID = 922359954025885737

    def _personality(self, **overrides):
        defaults = dict(
            enabled=True,
            reaction_chance=0.0,
            ambient_reaction_chance=0.0,
            lord_reaction_chance=0.0,
            global_cooldown=0,
            channel_cooldown=0,
            user_cooldown=0,
            lord_cooldown=0,
            ambient_hourly_cap=0,
        )
        defaults.update(overrides)
        cfg = PersonalityConfig(**defaults)
        bot = SimpleNamespace(
            user=SimpleNamespace(id=999),
            config=SimpleNamespace(
                lord_user_ids=(self.LORD_ID, 1049231767585964032)
            ),
        )
        return Personality(bot, cfg)

    async def test_two_people_talking_never_get_interjected_with_text(self):
        personality = self._personality(reaction_chance=0.5, ambient_reaction_chance=0.5)
        first = _make_msg("good morning everyone, i slept great", author_id=1)
        second = _make_msg("nobody cares, i've been up since four", author_id=2)
        await personality.handle(first)
        await personality.handle(second)
        first.reply.assert_not_awaited()
        second.reply.assert_not_awaited()

    async def test_the_lord_gets_a_crown_reaction_never_a_text_reply(self):
        personality = self._personality(lord_reaction_chance=1.0, lord_cooldown=600)
        with patch("functions.personality.random.random", return_value=0.0):
            msg = _make_msg("i have returned", author_id=self.LORD_ID)
            await personality.handle(msg)
        msg.reply.assert_not_awaited()
        msg.add_reaction.assert_awaited_once()
        self.assertIn(msg.add_reaction.call_args.args[0], LORD_REACTIONS)

        # Lord cooldown holds on the next message in the same channel.
        follow = _make_msg("still here", author_id=self.LORD_ID)
        await personality.handle(follow)
        follow.reply.assert_not_awaited()
        follow.add_reaction.assert_not_awaited()

    async def test_taher_is_treated_as_a_lord_too(self):
        personality = self._personality(lord_reaction_chance=1.0)
        with patch("functions.personality.random.random", return_value=0.0):
            msg = _make_msg("taher has entered the chat", author_id=1049231767585964032)
            await personality.handle(msg)
        msg.reply.assert_not_awaited()
        msg.add_reaction.assert_awaited_once()
        self.assertIn(msg.add_reaction.call_args.args[0], LORD_REACTIONS)

    async def test_everyone_else_is_not_the_lord(self):
        personality = self._personality(lord_reaction_chance=1.0)
        msg = _make_msg("i am definitely not the lord", author_id=42)
        await personality.handle(msg)
        msg.reply.assert_not_awaited()
        msg.add_reaction.assert_not_awaited()

    def test_explicit_context_roast_builder_quotes_words_and_honours_lords(self):
        content = "pineapple belongs on pizza and i will die on this hill"
        snippet = clean_snippet(content)
        regular = build_context_roast("Ash", content)
        self.assertIn(
            regular,
            {f.format(name="Ash", snippet=snippet) for f in CONTEXT_ROAST_FRAMES},
        )
        loyal = build_context_roast("Ash", content, lord=True)
        self.assertIn(
            loyal,
            {f.format(name="Ash", snippet=snippet) for f in LORD_CONTEXT_FRAMES},
        )

    def test_config_ignores_legacy_auto_reply_env_vars_safely(self):
        cfg = Config.from_env(
            {
                "PERSONALITY_REPLY_CHANCE": "1.0",
                "PERSONALITY_STICKER_CHANCE": "1.0",
                "PERSONALITY_MEME_CHANCE": "1.0",
                "PERSONALITY_GIF_CHANCE": "1.0",
                "PERSONALITY_QUIP_CHANCE": "1.0",
                "PERSONALITY_GRAMMAR_CHANCE": "1.0",
                "PERSONALITY_INTERJECT_CHANCE": "1.0",
                "PERSONALITY_REACTION_CHANCE": "0.05",
                "PERSONALITY_AMBIENT_REACTION_CHANCE": "0.01",
                "PERSONALITY_LORD_REACTION_CHANCE": "0.04",
            }
        )
        self.assertEqual(cfg.personality.reaction_chance, 0.05)
        self.assertEqual(cfg.personality.ambient_reaction_chance, 0.01)
        self.assertEqual(cfg.personality.lord_reaction_chance, 0.04)
        for removed in (
            "reply_chance",
            "sticker_chance",
            "meme_chance",
            "gif_chance",
            "random_quip_chance",
            "roast_chance",
            "grammar_chance",
            "interject_chance",
            "about_chance",
            "memory_callback_chance",
        ):
            self.assertFalse(hasattr(cfg.personality, removed), removed)

    def test_config_parses_lords_and_quiet_reaction_defaults(self):
        cfg = Config.from_env(
            {
                "PERSONALITY_LORD_REACTION_CHANCE": "0.12",
                "PERSONALITY_LORD_COOLDOWN": "900",
                "PARADISE_LORD_USER_ID": "922359954025885737",
            }
        )
        self.assertEqual(cfg.personality.lord_reaction_chance, 0.12)
        self.assertEqual(cfg.personality.lord_cooldown, 900)
        self.assertEqual(cfg.lord_user_id, 922359954025885737)
        self.assertEqual(cfg.lord_user_ids, (922359954025885737,))
        self.assertFalse(cfg.is_lord(1049231767585964032))

        many = Config.from_env(
            {"PARADISE_LORD_USER_IDS": "111, 222", "PARADISE_LORD_USER_ID": "333"}
        )
        self.assertEqual(many.lord_user_ids, (111, 222, 333))

        defaults = Config.from_env({})
        self.assertEqual(defaults.personality.reaction_chance, 0.06)
        self.assertEqual(defaults.personality.ambient_reaction_chance, 0.015)
        self.assertEqual(defaults.personality.lord_reaction_chance, 0.06)
        self.assertEqual(defaults.personality.global_cooldown, 45)
        self.assertEqual(defaults.personality.channel_cooldown, 240)
        self.assertEqual(defaults.personality.user_cooldown, 300)
        self.assertEqual(defaults.personality.lord_cooldown, 600)
        self.assertEqual(defaults.personality.ambient_hourly_cap, 4)
        self.assertEqual(
            defaults.lord_user_ids, (922359954025885737, 1049231767585964032)
        )
        self.assertTrue(defaults.is_lord(1049231767585964032))
        self.assertTrue(defaults.is_lord(922359954025885737))


if __name__ == "__main__":
    unittest.main()
