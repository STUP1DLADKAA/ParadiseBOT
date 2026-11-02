"""The living mood: variety, self-repeat guards, slang, fresh roast angles.

Covers functions/vibe.py (the VibeTracker engine), its wiring into
functions/ai.py (answers carry the vibe, roasts never repeat an angle,
butt-in lines when discussed) and the personality engine's "they're
talking about me" lane.
"""

from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from config import Config, GeminiConfig, PersonalityConfig
from functions.ai import AIService
from functions.personality import (
    Personality,
    INTERJECTION_LINES,
    is_about_paradise,
)
from functions.vibe import (
    Lexicon,
    MOODS,
    MoodRing,
    RoastLedger,
    SelfEcho,
    VibeTracker,
)


# ---------------------------------------------------------------------------
# Fake plumbing (same shapes the other suites use)
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
    def __init__(self, post_responses=None, get_responses=None):
        self.post_responses = list(post_responses or [])
        self.get_responses = list(get_responses or [])
        self.requests = []
        self.closed = False

    def post(self, url, headers=None, json=None, **kwargs):
        self.requests.append({"url": url, "headers": headers or {}, "json": json})
        if not self.post_responses:
            raise RuntimeError("no queued response")
        return self.post_responses.pop(0)

    def get(self, url, headers=None, **kwargs):
        self.requests.append({"url": url, "headers": headers or {}})
        if not self.get_responses:
            raise RuntimeError("no queued response")
        return self.get_responses.pop(0)


def make_ai(lord_user_ids=(922359954025885737, 1049231767585964032)):
    config = Config(
        gemini=GeminiConfig(api_key="supersecretkey-123"),
        ai_base_url="http://local.test/v1",
        ai_model="llama",
        lord_user_ids=list(lord_user_ids),
    )
    ai = AIService(SimpleNamespace(user=SimpleNamespace(id=999)), config)
    ai.gemini._session = FakeSession()
    ai._session = FakeSession(
        post_responses=[
            FakeResponse(200, {"choices": [{"message": {"content": "primary answer"}}]})
        ] * 3
    )
    return ai


def make_msg(content, *, author_id=42, display_name="Ash", channel_id=10):
    return SimpleNamespace(
        content=content,
        author=SimpleNamespace(id=author_id, bot=False, display_name=display_name),
        webhook_id=None,
        mentions=[],
        channel=SimpleNamespace(id=channel_id),
        guild=SimpleNamespace(id=1, stickers=[]),
        type=discord.MessageType.default,
        add_reaction=AsyncMock(),
        reply=AsyncMock(),
    )


# ---------------------------------------------------------------------------
# The mood ring
# ---------------------------------------------------------------------------


class TestMoodRing(unittest.TestCase):
    def test_never_the_same_energy_twice_in_a_row(self):
        ring = MoodRing()
        picks = [ring.pick(1)[0] for _ in range(40)]
        for before, after in zip(picks, picks[1:]):
            self.assertNotEqual(before, after)

    def test_the_avoid_window_is_respected(self):
        ring = MoodRing()
        picks = [ring.pick(1)[0] for _ in range(40)]
        for index, label in enumerate(picks):
            window = picks[max(0, index - 4):index]
            self.assertNotIn(label, window)

    def test_channels_do_not_share_a_mood_ring(self):
        # Each channel's ring evolves on its own: lots of variety in one,
        # zero influence on the other (its history starts empty).
        ring = MoodRing()
        one = {ring.pick(100)[0] for _ in range(60)}
        self.assertGreaterEqual(len(one), 10)  # real variety, not one note
        self.assertEqual(len(ring._recent[200]), 0)

    def test_every_mood_comes_with_an_instruction(self):
        for label, instruction in MOODS:
            self.assertTrue(label)
            self.assertGreater(len(instruction), 10)
        # The banned-by-test words never sneak into the mood list.
        for label, instruction in MOODS:
            combined = f"{label} {instruction}".lower()
            self.assertNotIn("unhinged", combined)
            self.assertNotIn("ragebait", combined)

    def test_a_hinglish_mood_exists(self):
        labels = [label for label, _ in MOODS]
        self.assertIn("hinglish", labels)


# ---------------------------------------------------------------------------
# What Paradise just said
# ---------------------------------------------------------------------------


class TestSelfEcho(unittest.TestCase):
    def test_recent_lines_come_back(self):
        echo = SelfEcho()
        echo.note(5, "first line")
        echo.note(5, "second line")
        self.assertEqual(echo.recent(5), ["first line", "second line"])

    def test_other_channels_are_isolated(self):
        echo = SelfEcho()
        echo.note(5, "mine")
        self.assertEqual(echo.recent(6), [])

    def test_empty_and_blank_lines_are_ignored(self):
        echo = SelfEcho()
        echo.note(5, "")
        echo.note(5, "   \n\t ")
        self.assertEqual(echo.recent(5), [])

    def test_long_lines_are_snipped(self):
        echo = SelfEcho()
        echo.note(5, "x" * 5000)
        self.assertLessEqual(len(echo.recent(5)[0]), 140)

    def test_old_lines_expire(self):
        echo = SelfEcho()
        now = 1000.0
        echo.note(5, "ancient", now=now)
        self.assertEqual(echo.recent(5, now=now + 7 * 3600), [])

    def test_the_ring_is_bounded(self):
        echo = SelfEcho()
        for index in range(30):
            echo.note(5, f"line {index}")
        self.assertEqual(len(echo.recent(5, limit=50)), 10)


# ---------------------------------------------------------------------------
# The group's slang
# ---------------------------------------------------------------------------


class TestLexicon(unittest.TestCase):
    def test_a_word_needs_three_uses(self):
        lex = Lexicon()
        lex.observe("that was a proper banger")
        lex.observe("again a banger")
        self.assertEqual(lex.top(), [])
        lex.observe("third banger incoming")
        self.assertIn("banger", lex.top())

    def test_stopwords_never_become_slang(self):
        lex = Lexicon()
        for _ in range(10):
            lex.observe("the and you with this that guys lol bhai")
        self.assertEqual(lex.top(), [])

    def test_urls_and_numbers_do_not_count(self):
        lex = Lexicon()
        for _ in range(5):
            lex.observe("https://example.com/whatever 12345 2morrow")
        self.assertEqual(lex.top(), [])

    def test_a_repeated_word_surfaces_but_is_capped(self):
        # Three uses are enough to become slang; the per-word cap only
        # stops one message-spammer from dwarfing everyone else.
        lex = Lexicon()
        for _ in range(50):
            lex.observe("banger banger")
        self.assertIn("banger", lex.top())
        self.assertLessEqual(lex._counts["banger"], 6)

    def test_old_words_expire(self):
        lex = Lexicon()
        now = 1000.0
        for _ in range(4):
            lex.observe("vintage word", now=now)
        self.assertIn("vintage", lex.top(now=now))
        self.assertEqual(lex.top(now=now + 49 * 3600), [])

    def test_repeats_within_one_message_count_once(self):
        lex = Lexicon()
        lex.observe("banger banger banger banger")
        self.assertEqual(lex.top(), [])  # one use, not three


# ---------------------------------------------------------------------------
# The roast ledger
# ---------------------------------------------------------------------------


class TestRoastLedger(unittest.TestCase):
    def test_lines_come_back_for_the_same_target(self):
        ledger = RoastLedger()
        ledger.note("Taher", "still scared of horror maps")
        self.assertIn(
            "still scared of horror maps", ledger.recent("taher")
        )  # case-insensitive target

    def test_targets_are_isolated(self):
        ledger = RoastLedger()
        ledger.note("Taher", "about taher")
        self.assertEqual(ledger.recent("Ash"), [])

    def test_empty_targets_are_ignored(self):
        ledger = RoastLedger()
        ledger.note("", "line")
        ledger.note("   ", "line")
        self.assertEqual(ledger.recent(""), [])

    def test_old_jokes_expire(self):
        ledger = RoastLedger()
        now = 1000.0
        ledger.note("Ash", "old jab", now=now)
        self.assertEqual(ledger.recent("Ash", now=now + 25 * 3600), [])

    def test_the_ledger_is_bounded(self):
        ledger = RoastLedger()
        for index in range(20):
            ledger.note("Ash", f"jab {index}")
        self.assertEqual(len(ledger.recent("Ash", limit=50)), 5)


# ---------------------------------------------------------------------------
# The prompt blocks
# ---------------------------------------------------------------------------


class TestVibeBlocks(unittest.TestCase):
    def test_the_block_carries_a_mood(self):
        vibe = VibeTracker()
        block = vibe.prompt_block(1)
        instructions = [instruction for _, instruction in MOODS]
        self.assertTrue(
            any(f"YOUR CURRENT MOOD: {note}" in block for note in instructions)
        )

    def test_the_block_lists_what_he_just_said(self):
        vibe = VibeTracker()
        vibe.note_reply(1, "i said this earlier")
        self.assertIn("i said this earlier", vibe.prompt_block(1))

    def test_the_block_lists_the_groups_slang(self):
        vibe = VibeTracker()
        for _ in range(4):
            vibe.observe("Ash", "absolute banger of a clip")
        block = vibe.prompt_block(1)
        self.assertIn("banger", block)

    def test_the_block_is_his_own_head_not_a_database(self):
        vibe = VibeTracker()
        vibe.note_reply(1, "something")
        for _ in range(4):
            vibe.observe("Ash", "banger banger of banger")
        block = vibe.prompt_block(1)
        # Never framed as memory/learning — it's just who he is right now.
        lowered = block.lower()
        for banned in ("memory", "learn", "taught", "stored", "database"):
            self.assertNotIn(banned, lowered)

    def test_moods_rotate_between_blocks(self):
        vibe = VibeTracker()
        seen = {vibe.prompt_block(1) for _ in range(20)}
        self.assertGreater(len(seen), 1)

    def test_the_roast_block_lists_old_angles(self):
        vibe = VibeTracker()
        self.assertEqual(vibe.roast_block("Ash"), "")
        vibe.note_roast("Ash", "chess loss number forty")
        block = vibe.roast_block("Ash")
        self.assertIn("chess loss number forty", block)
        self.assertIn("Ash", block)
        lowered = block.lower()
        for banned in ("memory", "learn", "taught"):
            self.assertNotIn(banned, lowered)

    def test_observing_never_raises_on_garbage(self):
        vibe = VibeTracker()
        vibe.observe(None, None)  # type: ignore[arg-type]
        vibe.note_reply(-1, "")
        vibe.note_roast(None, None)  # type: ignore[arg-type]
        self.assertIsInstance(vibe.prompt_block(1), str)

    def test_bot_lines_do_not_feed_the_lexicon(self):
        vibe = VibeTracker()
        for _ in range(5):
            vibe.observe("Paradise", "i love saying banger", from_bot=True)
        self.assertEqual(vibe.lexicon.top(), [])


# ---------------------------------------------------------------------------
# Wiring into the AI service
# ---------------------------------------------------------------------------


class TestAIWiring(unittest.IsolatedAsyncioTestCase):
    async def test_answers_carry_the_current_mood(self):
        ai = make_ai()
        await ai.answer_question(
            "what is a black hole", asker="Tester", channel_id=4
        )
        content = ai._session.requests[0]["json"]["messages"][1]["content"]
        self.assertIn("YOUR CURRENT MOOD", content)

    async def test_mentions_carry_the_current_mood(self):
        ai = make_ai()
        message = make_msg("paradise explain gravity", author_id=55, channel_id=7)
        await ai.respond(message)
        content = ai._session.requests[0]["json"]["messages"][1]["content"]
        self.assertIn("YOUR CURRENT MOOD", content)

    async def test_his_own_answers_feed_the_echo_guard(self):
        ai = make_ai()
        await ai.answer_question(
            "anything", asker="Tester", channel_id=12
        )
        self.assertEqual(ai.vibe.echo.recent(12), ["primary answer"])

    async def test_human_messages_feed_the_lexicon(self):
        ai = make_ai()
        for _ in range(4):
            ai.record(make_msg("that was a banger shot", channel_id=3))
        self.assertIn("banger", ai.vibe.lexicon.top())

    async def test_roasts_do_not_repeat_an_angle(self):
        ai = make_ai()
        first = await ai.tease("Rohan", "i lost again", target_id=555)
        self.assertEqual(first, "primary answer")
        # The jab was remembered for this target...
        self.assertIn("primary answer", ai.vibe.roasts.recent("Rohan"))
        # ...and the NEXT roast about them sees it in the prompt.
        second = await ai.tease("Rohan", "i lost differently", target_id=555)
        self.assertEqual(second, "primary answer")
        prompts = [
            request["json"]["messages"][1]["content"]
            for request in ai._session.requests
        ]
        self.assertIn("JOKES YOU ALREADY MADE ABOUT Rohan", prompts[1])
        self.assertIn("primary answer", prompts[1])

    async def test_the_first_roast_has_no_history_block(self):
        ai = make_ai()
        await ai.tease("Rohan", "i lost again", target_id=555)
        content = ai._session.requests[0]["json"]["messages"][1]["content"]
        self.assertNotIn("JOKES YOU ALREADY MADE", content)

    async def test_butt_in_returns_the_line(self):
        ai = make_ai()
        line = await ai.butt_in("Ash", "paradise has been sleeping all day", 5)
        self.assertEqual(line, "primary answer")
        ask = ai._session.requests[0]["json"]["messages"][1]["content"]
        self.assertIn("talking ABOUT you", ask)
        self.assertIn("paradise has been sleeping all day", ask)
        self.assertIn("YOUR CURRENT MOOD", ask)
        # and the line feeds the echo guard like any other reply
        self.assertEqual(ai.vibe.echo.recent(5), ["primary answer"])

    async def test_butt_in_is_silent_when_the_provider_dies(self):
        ai = make_ai()
        ai._session = FakeSession()  # no queued responses -> post() raises
        line = await ai.butt_in("Ash", "paradise is lazy", 5)
        self.assertEqual(line, "")

    async def test_butt_in_needs_content(self):
        ai = make_ai()
        self.assertEqual(await ai.butt_in("Ash", "", 5), "")

    async def test_butt_in_needs_a_configured_ai(self):
        ai = AIService(SimpleNamespace(user=SimpleNamespace(id=999)), Config())
        self.assertEqual(await ai.butt_in("Ash", "paradise who", 5), "")


# ---------------------------------------------------------------------------
# The about-him lane
# ---------------------------------------------------------------------------


class TestAboutDetection(unittest.TestCase):
    def test_obvious_cases(self):
        for line in (
            "paradise is actually funny",
            "PARADISE please",
            "paradisebot said something wild",
            "paradise's jokes are mid",
            "the bot is cooking today",
            "this bot never misses",
            "ask the bot later",
            "paradise bot opinion?",
            "im telling paradise everything",
            "paradise you listening?",
        ):
            self.assertTrue(is_about_paradise(line), line)

    def test_not_every_sentence_with_his_name(self):
        for line in (
            "paradiselost was a great level",
            "someparadise thing",
            "@paradise hello",  # a mention render is the assistant's business
            "the bottle is empty",
            "this botnet is huge",
            "the bots are spamming",
            # Places, plural places and famous collocations are not him.
            "my paradise hotel booking confirmed",
            "he said paradises was a place, honestly",
            "paradise city is a banger",
            "paradise island trip booked",
            "we honeymooned in paradises",
        ):
            self.assertFalse(is_about_paradise(line), line)


class TestAboutLane(unittest.IsolatedAsyncioTestCase):
    LORD_ID = 922359954025885737

    def _personality(self, **overrides):
        defaults = dict(
            enabled=True,
            reaction_chance=0.0,
            reply_chance=0.0,
            sticker_chance=0.0,
            meme_chance=0.0,
            gif_chance=0.0,
            random_quip_chance=0.0,
            roast_chance=0.0,
            ambient_reaction_chance=0.0,
            grammar_chance=0.0,
            global_cooldown=0,
            channel_cooldown=0,
            reply_channel_cooldown=0,
            humour_cooldown=0,
            roast_user_cooldown=0,
            interject_cooldown=0,
            interject_chance=0.0,
            lord_reply_chance=0.0,
        )
        defaults.update(overrides)
        cfg = PersonalityConfig(**defaults)
        bot = SimpleNamespace(
            user=SimpleNamespace(id=999),
            config=SimpleNamespace(
                lord_user_ids=(self.LORD_ID, 1049231767585964032)
            ),
        )
        return Personality(bot, cfg), bot

    def _with_ai(self, personality, bot, line="excuse me i am RIGHT here"):
        bot.ai = SimpleNamespace(
            available=True,
            butt_in=AsyncMock(return_value=line),
        )
        return bot.ai

    async def test_being_discussed_gets_a_fresh_line(self):
        personality, bot = self._personality(about_chance=1.0, about_cooldown=0)
        ai = self._with_ai(personality, bot)
        with patch("functions.personality.random.random", return_value=0.0):
            msg = make_msg("paradise has been yapping all day", channel_id=10)
            await personality.handle(msg)
        ai.butt_in.assert_awaited_once()
        msg.reply.assert_awaited_once()
        self.assertEqual(msg.reply.call_args.args[0], "excuse me i am RIGHT here")

    async def test_mentions_still_belong_to_the_assistant(self):
        # A real ping goes to the assistant and personality stays out —
        # even though the text contains his name.
        personality, bot = self._personality(about_chance=1.0, about_cooldown=0)
        ai = self._with_ai(personality, bot)
        with patch("functions.personality.random.random", return_value=0.0):
            msg = make_msg("<@999> wake up", channel_id=10)
            msg.mentions = [SimpleNamespace(id=999)]
            await personality.handle(msg)
        ai.butt_in.assert_not_awaited()
        msg.reply.assert_not_awaited()

    async def test_the_chance_gate(self):
        personality, bot = self._personality(about_chance=0.35, about_cooldown=0)
        ai = self._with_ai(personality, bot)
        with patch("functions.personality.random.random", return_value=0.9):
            msg = make_msg("paradise is asleep", channel_id=10)
            await personality.handle(msg)
        ai.butt_in.assert_not_awaited()
        msg.reply.assert_not_awaited()

    async def test_chance_zero_disables_the_lane(self):
        personality, bot = self._personality(about_chance=0.0)
        ai = self._with_ai(personality, bot)
        with patch("functions.personality.random.random", return_value=0.0):
            msg = make_msg("paradise who", channel_id=10)
            await personality.handle(msg)
        ai.butt_in.assert_not_awaited()

    async def test_the_cooldown_holds(self):
        personality, bot = self._personality(
            about_chance=1.0, about_cooldown=240
        )
        ai = self._with_ai(personality, bot)
        with patch("functions.personality.random.random", return_value=0.0):
            first = make_msg("paradise hello", channel_id=10)
            await personality.handle(first)
            first.reply.assert_awaited_once()

            second = make_msg("paradise still listening?", author_id=7)
            await personality.handle(second)
        second.reply.assert_not_awaited()  # the lane is cooling down
        self.assertEqual(ai.butt_in.await_count, 1)

    async def test_an_ai_failure_is_silence(self):
        personality, bot = self._personality(about_chance=1.0, about_cooldown=0)
        bot.ai = SimpleNamespace(
            available=True,
            butt_in=AsyncMock(side_effect=RuntimeError("provider down")),
        )
        with patch("functions.personality.random.random", return_value=0.0):
            msg = make_msg("paradise broke again", channel_id=10)
            await personality.handle(msg)  # must not raise
        msg.reply.assert_not_awaited()

    async def test_an_empty_line_is_silence(self):
        personality, bot = self._personality(about_chance=1.0, about_cooldown=0)
        self._with_ai(personality, bot, line="")
        with patch("functions.personality.random.random", return_value=0.0):
            msg = make_msg("paradise is quiet", channel_id=10)
            await personality.handle(msg)
        msg.reply.assert_not_awaited()

    async def test_without_an_ai_the_lane_sleeps(self):
        # No bot.ai at all (the standard personality test setup).
        personality, _ = self._personality(about_chance=1.0, about_cooldown=0)
        with patch("functions.personality.random.random", return_value=0.0):
            msg = make_msg("paradise you there", channel_id=10)
            await personality.handle(msg)
        msg.reply.assert_not_awaited()

    async def test_url_only_slander_is_ignored(self):
        personality, bot = self._personality(about_chance=1.0, about_cooldown=0)
        ai = self._with_ai(personality, bot)
        with patch("functions.personality.random.random", return_value=0.0):
            msg = make_msg("https://paradise.example.com", channel_id=10)
            await personality.handle(msg)
        ai.butt_in.assert_not_awaited()

    async def test_being_discussed_counts_against_the_ambient_budget(self):
        personality, bot = self._personality(about_chance=1.0, about_cooldown=0)
        self._with_ai(personality, bot)
        with patch("functions.personality.random.random", return_value=0.0):
            await personality.handle(make_msg("paradise hi", channel_id=10))
        self.assertEqual(len(personality._ambient_log[10]), 1)

    async def test_ambient_budget_caps_the_butting_in(self):
        # Six unprompted actions in an hour is the ceiling; a seventh
        # about-reply must wait even with every other gate wide open.
        personality, bot = self._personality(about_chance=1.0, about_cooldown=0)
        ai = self._with_ai(personality, bot)
        now = time.monotonic()
        personality._ambient_log[10] = __import__("collections").deque(
            [now - 60] * 6, maxlen=12
        )
        with patch("functions.personality.random.random", return_value=0.0):
            msg = make_msg("paradise, buddy", channel_id=10)
            await personality.handle(msg)
        ai.butt_in.assert_not_awaited()
        msg.reply.assert_not_awaited()


# ---------------------------------------------------------------------------
# Persona and pools
# ---------------------------------------------------------------------------


class TestPersona(unittest.TestCase):
    def test_interjection_pool_grew_some_variety(self):
        self.assertGreaterEqual(len(INTERJECTION_LINES), 18)

    def test_personality_config_knows_the_about_lane(self):
        cfg = PersonalityConfig.from_env(
            {
                "PERSONALITY_ABOUT_CHANCE": "0.5",
                "PERSONALITY_ABOUT_COOLDOWN": "90",
            }
        )
        self.assertEqual(cfg.about_chance, 0.5)
        self.assertEqual(cfg.about_cooldown, 90)

    def test_defaults_stay_documented(self):
        cfg = PersonalityConfig()
        self.assertEqual(cfg.about_chance, 0.35)
        self.assertEqual(cfg.about_cooldown, 180)


if __name__ == "__main__":
    unittest.main()
