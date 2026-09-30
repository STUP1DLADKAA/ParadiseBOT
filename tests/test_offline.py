"""Offline validation for Paradise.

Runs without a Discord token or network: it checks the exact registered
command set, that the removed music/TTS commands stay gone, the pure
logic behind ping and coinflip, config parsing, AI message building,
the presence rotation rules and the passive personality system.

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import EXTENSIONS, ParadiseBot  # noqa: E402
from commands.fun import PING_QUIPS, build_ping_message, flip_coin  # noqa: E402
from commands.system import HELP_BLURBS, HELP_GROUPS  # noqa: E402
from commands.utility import resolve_language  # noqa: E402
from config import Config, PersonalityConfig  # noqa: E402
from functions import personality as personality_module  # noqa: E402
from functions import presence as presence_module  # noqa: E402
from functions.ai import AIService  # noqa: E402
from functions.helpers import parse_options, shorten  # noqa: E402
from functions.personality import PATTERNS, Personality  # noqa: E402
from functions.presence import NOTES, ROTATE_SECONDS, WATCHING, PresenceManager  # noqa: E402

EXPECTED_COMMANDS = {
    "ping", "coinflip", "choose",
    "remind", "poll", "afk", "translate", "dictionary", "define", "recipe",
    "summarize", "help",
}

# Everything that must never come back: the whole music/TTS stack plus
# commands removed in earlier passes.
REMOVED_COMMANDS = {
    # music
    "play", "pause", "resume", "skip", "previous", "stop", "queue",
    "nowplaying", "remove", "move", "clearqueue", "shuffle", "seek",
    "forward", "backward", "loop", "autoplay", "filter", "volume",
    "join", "leave", "lyrics", "status",
    # tts / voice
    "say",
    # misc removals
    "8ball", "settings", "truth", "dare", "serverinfo", "userinfo", "vibe", "roll",
}


# ---------------------------------------------------------------------------
# Command registration
# ---------------------------------------------------------------------------


class TestCommandSet(unittest.TestCase):
    def test_exact_command_set(self):
        async def run():
            bot = ParadiseBot(Config())
            for extension in EXTENSIONS:
                await bot.load_extension(extension)
            return sorted(command.name for command in bot.tree.get_commands())

        names = asyncio.run(run())
        self.assertEqual(len(names), len(set(names)), "duplicate commands registered")
        self.assertEqual(set(names), EXPECTED_COMMANDS, f"got {names}")
        self.assertEqual(names.count("ping"), 1, "exactly one /ping")

    def test_removed_commands_are_gone(self):
        async def run():
            bot = ParadiseBot(Config())
            for extension in EXTENSIONS:
                await bot.load_extension(extension)
            return {command.name for command in bot.tree.get_commands()}

        registered = asyncio.run(run())
        leaked = registered & REMOVED_COMMANDS
        self.assertFalse(leaked, f"removed commands still registered: {leaked}")

    def test_no_removed_commands_in_source(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for folder in ("commands", "functions"):
            for filename in os.listdir(os.path.join(root, folder)):
                if not filename.endswith(".py"):
                    continue
                with open(os.path.join(root, folder, filename), encoding="utf-8") as handle:
                    source = handle.read()
                for name in REMOVED_COMMANDS:
                    self.assertNotIn(
                        f'"{name}"',
                        source,
                        f"{folder}/{filename} still references removed command {name}",
                    )

    def test_no_music_or_tts_modules_left(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for path in (
            "commands/music.py", "commands/voice.py", "functions/music",
            "functions/tts.py", "functions/lyrics.py", "functions/settings.py",
            "lavalink",
        ):
            self.assertFalse(
                os.path.exists(os.path.join(root, path)),
                f"{path} should have been removed",
            )

    def test_no_music_or_tts_references_in_source(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        files = ["bot.py", "config.py"]
        for folder in ("commands", "functions"):
            files.extend(
                os.path.join(folder, name)
                for name in os.listdir(os.path.join(root, folder))
                if name.endswith(".py")
            )
        for relative in files:
            with open(os.path.join(root, relative), encoding="utf-8") as handle:
                source = handle.read().lower()
            for needle in ("lavalink", "edge_tts", "edge-tts", "ttsservice", "musicservice"):
                self.assertNotIn(needle, source, f"{relative} still mentions {needle}")

    def test_help_covers_every_registered_command(self):
        # Every registered command (except /help itself) must appear in /help.
        listed = {name for names in HELP_GROUPS.values() for name in names}
        self.assertEqual(listed, EXPECTED_COMMANDS - {"help"})
        for name in listed:
            self.assertIn(name, HELP_BLURBS, f"/{name} has no help blurb")


# ---------------------------------------------------------------------------
# Fun commands
# ---------------------------------------------------------------------------


class TestPing(unittest.TestCase):
    def test_includes_latency_and_quip(self):
        for quip in PING_QUIPS:
            message = build_ping_message(0.042, quip)
            self.assertIn(quip, message)
            self.assertIn("42ms", message)

    def test_latency_is_measured_not_faked(self):
        self.assertIn("123ms", build_ping_message(0.1234))

    def test_unknown_latency_is_honest(self):
        message = build_ping_message(float("nan"))
        self.assertIn("measuring", message)
        self.assertNotIn("ms\n", message)


class TestCoinflip(unittest.TestCase):
    def test_only_heads_or_tails(self):
        for _ in range(50):
            self.assertIn(flip_coin(), ("Heads", "Tails"))

    def test_genuinely_random(self):
        counts = Counter(flip_coin() for _ in range(2000))
        self.assertGreater(counts["Heads"], 800)
        self.assertGreater(counts["Tails"], 800)


class TestParseOptions(unittest.TestCase):
    def test_pipe_and_comma_both_work(self):
        self.assertEqual(parse_options("a|b,c"), ["a", "b", "c"])

    def test_trims_and_ignores_empty(self):
        self.assertEqual(parse_options(" a | | b ,, "), ["a", "b"])

    def test_limit(self):
        raw = "|".join(str(i) for i in range(40))
        self.assertEqual(len(parse_options(raw)), 20)


class TestHelpers(unittest.TestCase):
    def test_shorten(self):
        self.assertEqual(shorten("short", 10), "short")
        self.assertEqual(len(shorten("x" * 50, 10)), 10)
        self.assertTrue(shorten("x" * 50, 10).endswith("…"))

    def test_resolve_language(self):
        self.assertEqual(resolve_language("hindi"), "hi")
        self.assertEqual(resolve_language("HINDI "), "hi")
        self.assertEqual(resolve_language("fr"), "fr")
        self.assertIsNone(resolve_language("klingon language"))
        self.assertIsNone(resolve_language(""))


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestConfig(unittest.TestCase):
    def test_from_env(self):
        config = Config.from_env(
            {
                "DISCORD_TOKEN": "tok",
                "AI_BASE_URL": "https://api.groq.com/openai/v1/",
                "AI_MODEL": "llama-3.3-70b-versatile",
                "PERSONALITY_ENABLED": "false",
                "PERSONALITY_REACTION_CHANCE": "0.1",
                "PERSONALITY_REPLY_CHANCE": "0.02",
                "PERSONALITY_GLOBAL_COOLDOWN": "60",
                "PERSONALITY_CHANNEL_COOLDOWN": "200",
                "PERSONALITY_REPLY_CHANNEL_COOLDOWN": "900",
            }
        )
        self.assertEqual(config.token, "tok")
        self.assertEqual(config.ai_base_url, "https://api.groq.com/openai/v1")
        self.assertTrue(config.ai_configured)
        self.assertFalse(config.personality.enabled)
        self.assertEqual(config.personality.reaction_chance, 0.1)
        self.assertEqual(config.personality.reply_chance, 0.02)
        self.assertEqual(config.personality.global_cooldown, 60)
        self.assertEqual(config.personality.channel_cooldown, 200)
        self.assertEqual(config.personality.reply_channel_cooldown, 900)

    def test_empty_env_is_safe(self):
        config = Config.from_env({})
        self.assertFalse(config.ai_configured)
        self.assertTrue(config.personality.enabled)
        self.assertLessEqual(config.personality.reaction_chance, 0.1)
        self.assertLessEqual(config.personality.reply_chance, 0.05)

    def test_bad_chances_snap_to_defaults(self):
        config = Config.from_env(
            {"PERSONALITY_REACTION_CHANCE": "5", "PERSONALITY_REPLY_CHANCE": "banana"}
        )
        self.assertEqual(config.personality.reaction_chance, 0.045)
        self.assertEqual(config.personality.reply_chance, 0.015)

    def test_template_has_no_secrets_or_music(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env.example"
        )
        with open(path, encoding="utf-8") as handle:
            content = handle.read().lower()
        for needle in ("lavalink", "tts_", "music_", "lyrics", "genius"):
            self.assertNotIn(needle, content, f".env.example still mentions {needle}")


# ---------------------------------------------------------------------------
# AI
# ---------------------------------------------------------------------------


class TestAI(unittest.TestCase):
    def _service(self):
        config = Config(
            ai_base_url="https://api.example.com/v1", ai_model="test-model"
        )
        fake_bot = SimpleNamespace(user=SimpleNamespace(id=999))
        return AIService(fake_bot, config)

    def test_availability(self):
        self.assertTrue(self._service().available)
        self.assertFalse(AIService(SimpleNamespace(user=None), Config()).available)

    def test_extract_question_strips_mention(self):
        ai = self._service()
        message = SimpleNamespace(
            content="<@999> what's a good minecraft build idea?",
            author=SimpleNamespace(display_name="Ash"),
            mentions=[],
        )
        self.assertEqual(
            ai.extract_question(message), "what's a good minecraft build idea?"
        )

    def test_context_is_bounded(self):
        ai = self._service()
        for i in range(30):
            ai._history[5].append((f"user{i}", f"message number {i}"))
        block = ai._context_block(5)
        self.assertLessEqual(len(block), 2400)
        self.assertIn("message number 29", block)
        self.assertNotIn("message number 0", block)

    def test_cooldown(self):
        ai = self._service()
        message = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            channel=SimpleNamespace(id=2),
            author=SimpleNamespace(id=3),
        )
        self.assertEqual(ai.cooldown_remaining(message), 0.0)
        ai._touch(message)
        self.assertGreater(ai.cooldown_remaining(message), 0)

    def test_reply_to_bot_message_triggers(self):
        ai = self._service()
        message = SimpleNamespace(
            author=SimpleNamespace(bot=False),
            mentions=[],
            reference=SimpleNamespace(
                resolved=SimpleNamespace(author=SimpleNamespace(id=999)),
                message_id=1,
            ),
        )
        self.assertTrue(ai.should_reply_to(message))


# ---------------------------------------------------------------------------
# Presence
# ---------------------------------------------------------------------------


class TestPresence(unittest.TestCase):
    def test_note_pool_is_large_and_clean(self):
        self.assertGreaterEqual(len(NOTES), 60, "note pool should not feel repetitive")
        self.assertEqual(len(set(NOTES)), len(NOTES), "duplicate notes in the pool")
        for note in NOTES:
            self.assertTrue(note.strip(), "empty note")
            self.assertLessEqual(len(note), 120, f"note too long: {note!r}")

    def test_required_example_notes_present(self):
        for note in (
            "Probably doing something.",
            "Paradise is watching 👀",
            "Everything is fine.",
            "Another day in Paradise.",
            "I am merely observing.",
        ):
            self.assertIn(note, NOTES)

    def test_rotation_interval_is_twenty_minutes(self):
        self.assertEqual(ROTATE_SECONDS, 20 * 60)

    def test_activity_is_always_watching_paradise(self):
        self.assertEqual(WATCHING, "Paradise")
        activity = PresenceManager.build_activity("Still here.")
        self.assertEqual(activity.type, discord.ActivityType.watching)
        self.assertEqual(activity.name, "Paradise")
        self.assertEqual(activity.state, "Still here.")

    def test_no_back_to_back_repeats(self):
        manager = PresenceManager(SimpleNamespace())
        previous = None
        for _ in range(500):
            note = manager.pick_note()
            self.assertIn(note, NOTES)
            self.assertNotEqual(note, previous, "same note picked twice in a row")
            previous = note

    def test_start_never_duplicates_the_timer(self):
        async def run():
            bot = SimpleNamespace(
                wait_until_ready=AsyncMock(side_effect=asyncio.Event().wait),
                is_closed=lambda: False,
                change_presence=AsyncMock(),
            )
            manager = PresenceManager(bot)
            manager.start()
            first_task = manager._task
            manager.start()
            self.assertIs(manager._task, first_task, "start() created a second timer")
            await manager.stop()
            self.assertIsNone(manager._task)

        asyncio.run(run())

    def test_apply_current_reasserts_without_advancing(self):
        async def run():
            bot = SimpleNamespace(change_presence=AsyncMock())
            manager = PresenceManager(bot)
            await manager.apply_current()  # nothing yet — must be a no-op
            bot.change_presence.assert_not_called()
            manager._current_note = "Still here."
            await manager.apply_current()
            bot.change_presence.assert_called_once()
            activity = bot.change_presence.call_args.kwargs["activity"]
            self.assertEqual(activity.name, "Paradise")
            self.assertEqual(activity.state, "Still here.")

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Personality
# ---------------------------------------------------------------------------


def make_message(
    content="bro I'm so tired",
    author_id=42,
    author_bot=False,
    webhook_id=None,
    mentions=(),
    channel_id=7,
    guild_id=1,
    message_type=discord.MessageType.default,
):
    return SimpleNamespace(
        content=content,
        author=SimpleNamespace(id=author_id, bot=author_bot),
        webhook_id=webhook_id,
        mentions=list(mentions),
        channel=SimpleNamespace(id=channel_id),
        guild=SimpleNamespace(id=guild_id),
        type=message_type,
        add_reaction=AsyncMock(),
        reply=AsyncMock(),
    )


def make_personality(**overrides):
    defaults = dict(
        enabled=True,
        reaction_chance=1.0,
        reply_chance=0.0,
        global_cooldown=0,
        channel_cooldown=0,
        reply_channel_cooldown=0,
    )
    defaults.update(overrides)
    config = PersonalityConfig(**defaults)
    bot = SimpleNamespace(user=SimpleNamespace(id=999))
    return Personality(bot, config)


class TestPersonalityMatching(unittest.TestCase):
    def test_contextual_matches(self):
        cases = {
            "bro I'm so tired": "tired",
            "I finally fixed it": "achievement",
            "who broke this": "who_broke",
            "this is so cursed": "cursed",
            "NO WAY 💀": "no_way",
            "wtf just happened": "wtf",
            "LMAOOO": "funny",
            "I'm hungry": "hungry",
            "nobody is online": "dead_server",
            "this server is dead": "dead_server",
            "bro look at this": "look",
            "can someone help me with this": "help",
            "found diamonds in minecraft": "minecraft",
            "one more game": "gaming",
            "good morning everyone": "greeting",
            "why is this happening": "why",
            "i'm so confused": "confusion",
            "ughhh so annoying": "frustration",
            "LETS GOOO": "excitement",
        }
        for content, expected in cases.items():
            pattern = Personality.match(content)
            self.assertIsNotNone(pattern, f"no pattern matched {content!r}")
            self.assertEqual(pattern.name, expected, f"{content!r} matched {pattern.name}")

    def test_plain_chat_matches_nothing(self):
        for content in (
            "we should plan the trip for saturday",
            "sent you the file",
            "it costs about twenty dollars",
        ):
            self.assertIsNone(Personality.match(content), f"{content!r} matched")

    def test_every_pattern_offers_something(self):
        for pattern in PATTERNS:
            self.assertTrue(
                pattern.reactions or pattern.replies,
                f"pattern {pattern.name} has neither reactions nor replies",
            )
            for reply in pattern.replies:
                self.assertLess(len(reply), 120, f"reply too long: {reply!r}")


class TestPersonalityGuards(unittest.TestCase):
    def _run(self, personality, message):
        asyncio.run(personality.handle(message))

    def assert_silent(self, message, **overrides):
        personality = make_personality(reaction_chance=1.0, reply_chance=1.0, **overrides)
        self._run(personality, message)
        message.add_reaction.assert_not_called()
        message.reply.assert_not_called()

    def test_reacts_when_chance_hits(self):
        personality = make_personality(reaction_chance=1.0)
        message = make_message("this is so cursed")
        self._run(personality, message)
        message.add_reaction.assert_called_once()
        emoji = message.add_reaction.call_args.args[0]
        self.assertIn(emoji, Personality.match("this is so cursed").reactions)
        message.reply.assert_not_called()

    def test_replies_when_chance_hits_and_never_reacts_too(self):
        personality = make_personality(reaction_chance=1.0, reply_chance=1.0)
        message = make_message("bro I'm so tired")
        self._run(personality, message)
        message.reply.assert_called_once()
        reply = message.reply.call_args.args[0]
        self.assertIn(reply, Personality.match("bro I'm so tired").replies)
        message.add_reaction.assert_not_called()  # never both

    def test_disabled_config_means_silence(self):
        self.assert_silent(make_message(), enabled=False)

    def test_ignores_bots_webhooks_and_system_messages(self):
        self.assert_silent(make_message(author_bot=True))
        self.assert_silent(make_message(webhook_id=123))
        self.assert_silent(make_message(message_type=discord.MessageType.new_member))

    def test_ignores_itself_and_dms(self):
        message = make_message(author_id=999)  # Paradise's own id
        self.assert_silent(message)
        dm = make_message()
        dm.guild = None
        self.assert_silent(dm)

    def test_ignores_command_messages(self):
        for content in ("!play something", "/help", ".status", "?who broke this"):
            self.assert_silent(make_message(content))

    def test_ignores_messages_addressed_to_paradise(self):
        message = make_message(
            "hey I'm so tired", mentions=[SimpleNamespace(id=999)]
        )
        self.assert_silent(message)

    def test_ignores_empty_and_walls_of_text(self):
        self.assert_silent(make_message(""))
        self.assert_silent(make_message("so tired " + "x" * 400))

    def test_ignores_repeated_content_from_same_author(self):
        personality = make_personality(reaction_chance=1.0)
        first = make_message("I'm so tired")
        second = make_message("I'm so tired")
        asyncio.run(personality.handle(first))
        # global/channel cooldowns are 0 here, so only the repeat guard blocks
        asyncio.run(personality.handle(second))
        first.add_reaction.assert_called_once()
        second.add_reaction.assert_not_called()

    def test_stays_out_of_fast_channels(self):
        personality = make_personality(reaction_chance=1.0)
        # Five rapid messages: the fifth would qualify but the channel is hot.
        for index in range(4):
            asyncio.run(personality.handle(make_message(f"filler {index}")))
        message = make_message("this is so cursed")
        asyncio.run(personality.handle(message))
        message.add_reaction.assert_not_called()

    def test_global_cooldown_blocks_second_action(self):
        personality = make_personality(reaction_chance=1.0, global_cooldown=3600)
        first = make_message("this is so cursed", channel_id=1)
        second = make_message("I finally fixed it", channel_id=2, author_id=43)
        asyncio.run(personality.handle(first))
        asyncio.run(personality.handle(second))
        first.add_reaction.assert_called_once()
        second.add_reaction.assert_not_called()

    def test_channel_cooldown_blocks_same_channel_only(self):
        personality = make_personality(reaction_chance=1.0, channel_cooldown=3600)
        first = make_message("this is so cursed", channel_id=1)
        blocked = make_message("I finally fixed it", channel_id=1, author_id=43)
        other = make_message("I finally fixed it", channel_id=2, author_id=44)
        asyncio.run(personality.handle(first))
        asyncio.run(personality.handle(blocked))
        asyncio.run(personality.handle(other))
        first.add_reaction.assert_called_once()
        blocked.add_reaction.assert_not_called()
        other.add_reaction.assert_called_once()

    def test_reply_channel_cooldown(self):
        personality = make_personality(
            reaction_chance=0.0, reply_chance=1.0, reply_channel_cooldown=3600
        )
        first = make_message("I'm so tired")
        second = make_message("who broke this", author_id=43)
        asyncio.run(personality.handle(first))
        asyncio.run(personality.handle(second))
        first.reply.assert_called_once()
        second.reply.assert_not_called()

    def test_replies_do_not_repeat_immediately(self):
        personality = make_personality(reaction_chance=0.0, reply_chance=1.0)
        seen = []
        for index in range(3):
            message = make_message("bro I'm so tired", author_id=50 + index)
            message.content = f"bro I'm so tired {'!' * index}"  # dodge repeat guard
            asyncio.run(personality.handle(message))
            seen.append(message.reply.call_args.args[0])
        self.assertEqual(len(seen), len(set(seen)), f"reply repeated: {seen}")

    def test_replies_never_ping(self):
        personality = make_personality(reaction_chance=0.0, reply_chance=1.0)
        message = make_message("who broke this")
        asyncio.run(personality.handle(message))
        kwargs = message.reply.call_args.kwargs
        self.assertFalse(kwargs.get("mention_author", True))


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


class TestWiring(unittest.TestCase):
    def test_bot_exposes_everything_the_commands_expect(self):
        async def run():
            bot = ParadiseBot(Config())
            for extension in EXTENSIONS:
                await bot.load_extension(extension)
            for attribute in (
                "config", "personality", "presence_manager", "ai",
                "afk_states", "started_at", "_afk_notice_last", "_ai_fallback_last",
            ):
                self.assertTrue(hasattr(bot, attribute), f"bot.{attribute} missing")
            for attribute in (
                "respond", "summarize", "record", "should_reply_to",
                "cooldown_remaining", "close", "available",
            ):
                self.assertTrue(hasattr(bot.ai, attribute), f"ai.{attribute} missing")
            for attribute in ("start", "stop", "apply_current", "pick_note"):
                self.assertTrue(
                    hasattr(bot.presence_manager, attribute),
                    f"presence.{attribute} missing",
                )
            self.assertTrue(hasattr(bot.personality, "handle"))
            self.assertIs(bot.personality.config, bot.config.personality)

        asyncio.run(run())

    def test_no_music_or_tts_extensions(self):
        self.assertNotIn("commands.music", EXTENSIONS)
        self.assertNotIn("commands.voice", EXTENSIONS)

    def test_dictionary_extension_is_loaded(self):
        self.assertIn("commands.dictionary", EXTENSIONS)

    def test_modules_have_no_dead_music_imports(self):
        # Importing every remaining module must not pull removed ones in.
        for name in (
            "bot", "config", "commands.fun", "commands.utility", "commands.ai",
            "commands.system", "commands.dictionary", "functions.ai",
            "functions.helpers", "functions.personality", "functions.presence",
        ):
            __import__(name)
        for gone in (
            "functions.music", "functions.tts", "functions.lyrics",
            "functions.settings", "commands.music", "commands.voice",
        ):
            self.assertNotIn(gone, sys.modules, f"{gone} was imported somewhere")

    def test_pattern_and_note_modules_expose_config(self):
        self.assertTrue(hasattr(personality_module, "PATTERNS"))
        self.assertTrue(hasattr(presence_module, "NOTES"))


# ---------------------------------------------------------------------------
# Recipe assistant — normalization, matching, ranking (all offline)
# ---------------------------------------------------------------------------


from functions.recipes import (  # noqa: E402
    RecipeFilters,
    get_default_source,
    get_ingredient_data,
    near_misses,
    normalize_input,
    rank_recipes,
    suggest_substitution,
)


class TestRecipeData(unittest.TestCase):
    def test_dataset_loads_and_is_valid(self):
        recipes = get_default_source().all_recipes()
        self.assertGreaterEqual(len(recipes), 20, "expected a real recipe book")
        ids = [r.id for r in recipes]
        self.assertEqual(len(ids), len(set(ids)), "duplicate recipe ids")
        for recipe in recipes:
            self.assertTrue(recipe.name)
            self.assertTrue(recipe.ingredients, f"{recipe.id} has no ingredients")
            self.assertTrue(recipe.instructions, f"{recipe.id} has no instructions")
            self.assertGreater(recipe.time_minutes, 0, f"{recipe.id} missing time")

    def test_pantry_and_aliases_present(self):
        data = get_ingredient_data()
        self.assertIn("salt", data.pantry)
        self.assertIn("water", data.pantry)
        self.assertTrue(data.aliases, "alias table should not be empty")


class TestIngredientNormalization(unittest.TestCase):
    def test_basic_list(self):
        self.assertEqual(
            normalize_input("chicken, rice, onion"), ["chicken", "rice", "onion"]
        )

    def test_quantities_and_units_are_stripped(self):
        self.assertEqual(
            normalize_input("2 eggs, 4 slices of bread, one tomato"),
            ["egg", "bread", "tomato"],
        )

    def test_hinglish_names(self):
        self.assertEqual(
            normalize_input("aloo, pyaaz, tamatar"), ["potato", "onion", "tomato"]
        )
        self.assertEqual(normalize_input("hari mirchi"), ["green chilli"])
        self.assertEqual(normalize_input("anda, dahi, paneer"), ["egg", "yogurt", "paneer"])
        self.assertEqual(normalize_input("lahsun aur adrak"), ["garlic", "ginger"])

    def test_plurals_and_spelling(self):
        self.assertEqual(normalize_input("potatoes"), ["potato"])
        self.assertEqual(normalize_input("tomatos"), ["tomato"])
        self.assertEqual(normalize_input("aaloo"), ["potato"])

    def test_deduplicates(self):
        self.assertEqual(normalize_input("egg, eggs, anda"), ["egg"])

    def test_empty_and_junk(self):
        self.assertEqual(normalize_input(""), [])
        self.assertEqual(normalize_input("   ,  , "), [])


class TestRecipeMatching(unittest.TestCase):
    def test_finds_matches_for_common_input(self):
        results = rank_recipes(normalize_input("chicken, rice, tomato, onion, garlic"))
        self.assertTrue(results)
        names = [r.recipe.name for r in results]
        self.assertIn("Chicken Tomato Rice", names)

    def test_pantry_items_never_counted_missing(self):
        results = rank_recipes(normalize_input("egg, bread, onion"))
        for result in results:
            self.assertNotIn("salt", result.missing)
            self.assertNotIn("oil", result.missing)
            self.assertNotIn("water", result.missing)

    def test_missing_ingredients_reported(self):
        # We have all of Chicken Fried Rice except egg + soy sauce.
        results = rank_recipes(normalize_input("chicken, rice, onion, garlic"))
        fried = next((r for r in results if r.recipe.id == "chicken-fried-rice"), None)
        self.assertIsNotNone(fried, "chicken fried rice should be a candidate")
        self.assertIn("egg", fried.missing)
        self.assertIn("soy sauce", fried.missing)
        self.assertNotIn("chicken", fried.missing)

    def test_everything_available_has_no_missing(self):
        results = rank_recipes(normalize_input("potato, onion, tomato"))
        top = results[0]
        self.assertEqual(top.recipe.id, "aloo-pyaaz-sabzi")
        self.assertEqual(top.missing, [])

    def test_ranking_prefers_more_matches(self):
        results = rank_recipes(normalize_input("chicken, rice, onion, garlic, tomato"))
        # The top result should use most of what the user has.
        self.assertGreaterEqual(results[0].have_count, 4)

    def test_no_strong_match_returns_empty(self):
        self.assertEqual(rank_recipes(normalize_input("saffron, caviar, truffle")), [])

    def test_diet_filter(self):
        results = rank_recipes(
            normalize_input("chicken, rice, onion, garlic, tomato"),
            filters=RecipeFilters(diet="vegetarian"),
        )
        for result in results:
            self.assertIn("vegetarian", result.recipe.diet)

    def test_max_time_filter(self):
        results = rank_recipes(
            normalize_input("egg, bread, onion"),
            filters=RecipeFilters(max_time=12),
        )
        for result in results:
            self.assertLessEqual(result.recipe.time_minutes, 12)

    def test_match_label_shape(self):
        results = rank_recipes(normalize_input("egg, bread, onion"))
        self.assertRegex(results[0].match_label, r"^\d+/\d+ ingredients$")


class TestRecipeFallbacks(unittest.TestCase):
    def test_near_misses_when_partial(self):
        # Only one recognised ingredient that appears in recipes.
        misses = near_misses(normalize_input("paneer"))
        self.assertTrue(misses)
        self.assertTrue(all(m.have_count >= 1 for m in misses))

    def test_substitutions_are_conservative(self):
        self.assertIsNotNone(suggest_substitution("soy sauce"))
        self.assertIsNone(suggest_substitution("chicken"))


if __name__ == "__main__":
    unittest.main()
