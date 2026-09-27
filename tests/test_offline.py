"""Offline validation for Paradise.

Runs without a Discord token, network, or Lavalink node: it checks the
exact registered command set, the removed commands, and the pure logic
behind ping, coinflip, TTS limits, panels, queue pagination, settings,
lyrics and AI message building.

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
import tempfile
import time
import unittest
from collections import Counter
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot as bot_module  # noqa: E402
from bot import EXTENSIONS, ParadiseBot  # noqa: E402
from commands.fun import BALL_ANSWERS, PING_QUIPS, build_ping_message, flip_coin  # noqa: E402
from commands.system import HELP_BLURBS, HELP_GROUPS  # noqa: E402
from commands.utility import resolve_language  # noqa: E402
from config import Config  # noqa: E402
from functions import lyrics  # noqa: E402
from functions.ai import AIService  # noqa: E402
from functions.helpers import (  # noqa: E402
    MusicError,
    TTSError,
    clean_for_speech,
    format_duration,
    is_hindi,
    parse_options,
    progress_bar,
)
from functions.music.panel import (  # noqa: E402
    PanelState,
    TrackInfo,
    button_states,
    queue_page_count,
    render_now_playing,
    render_panel,
    render_queue_page,
)
from functions.music.service import MusicService  # noqa: E402
from functions.settings import GuildSettings, SettingsStore, TTS_COOLDOWN_CHOICES  # noqa: E402
from functions.tts import TTSService  # noqa: E402

EXPECTED_COMMANDS = {
    "ping", "8ball", "coinflip", "choose",
    "join", "play", "pause", "resume", "skip", "stop", "queue", "nowplaying",
    "volume", "clearqueue", "shuffle", "leave", "lyrics",
    "remind", "poll", "afk", "translate", "define", "status", "settings",
    "say", "summarize", "help",
}
REMOVED_COMMANDS = {"truth", "dare", "serverinfo", "userinfo", "vibe", "roll"}


def make_track(title="Never Gonna Give You Up", author="Rick Astley", **overrides):
    data = dict(
        title=title,
        author=author,
        uri="https://example.com",
        duration=212_000,
        is_stream=False,
        requester=42,
        source_name="youtube",
    )
    data.update(overrides)
    return SimpleNamespace(**data)


class FakeGuild:
    def __init__(self, id=1, name="Paradise", voice_client=None, channel_name="lounge"):
        self.id = id
        self.name = name
        self.voice_client = voice_client
        self._channel_name = channel_name
        self.voice_states = {}

    def get_channel(self, channel_id):
        if channel_id is None:
            return None
        return SimpleNamespace(id=channel_id, name=self._channel_name)

    def get_member(self, member_id):
        return SimpleNamespace(id=member_id, display_name=f"member{member_id}")


class FakePlayer:
    def __init__(self, channel_id=99, current=None, queue=None, paused=False):
        self.channel_id = channel_id
        self.current = current
        self.queue = queue or []
        self.paused = paused
        self.position = 72_000
        self.volume = 75

    @property
    def is_connected(self):
        return self.channel_id is not None

    @property
    def is_playing(self):
        return self.is_connected and self.current is not None


class FakeBot:
    """Just enough bot for MusicService state rendering."""

    def __init__(self, guilds=None):
        self.guilds = guilds or []

    def get_guild(self, guild_id):
        return next((g for g in self.guilds if g.id == guild_id), None)


class FakeMusicForTTS:
    def __init__(self, player):
        self._player = player
        self.configured = True
        self.node_ready = True

    def player(self, guild_id):
        return self._player

    def abort_tts(self, guild_id):
        pass


# ---------------------------------------------------------------------------
# Command registration
# ---------------------------------------------------------------------------


class TestCommandSet(unittest.TestCase):
    def test_exact_command_set(self):
        async def run():
            bot = ParadiseBot(Config())
            for extension in EXTENSIONS:
                await bot.load_extension(extension)
            bot.prune_disabled_commands()
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

    def test_lyrics_removed_when_disabled(self):
        async def run():
            config = Config(lyrics_enabled=False)
            bot = ParadiseBot(config)
            for extension in EXTENSIONS:
                await bot.load_extension(extension)
            bot.prune_disabled_commands()
            return {command.name for command in bot.tree.get_commands()}

        registered = asyncio.run(run())
        self.assertNotIn("lyrics", registered)
        self.assertEqual(registered, EXPECTED_COMMANDS - {"lyrics"})

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
            self.assertIn("Latency: 42ms", message)
            self.assertIn(quip, message)

    def test_latency_is_measured_not_faked(self):
        message = build_ping_message(0.1378)
        self.assertIn("Latency: 138ms", message)

    def test_unknown_latency_is_honest(self):
        message = build_ping_message(float("nan"))
        self.assertIn("still measuring", message)
        self.assertIn("Latency", message)

    def test_no_duplicate_ping(self):
        self.assertEqual(len(PING_QUIPS), len(set(PING_QUIPS)))


class TestCoinflip(unittest.TestCase):
    def test_only_heads_or_tails(self):
        for _ in range(200):
            self.assertIn(flip_coin(), ("Heads", "Tails"))

    def test_genuinely_random(self):
        random.seed(1337)
        flips = Counter(flip_coin() for _ in range(20_000))
        heads, tails = flips["Heads"], flips["Tails"]
        self.assertGreater(heads, 8_000)
        self.assertGreater(tails, 8_000)
        self.assertLess(abs(heads - tails), 1_200, f"unbalanced: {flips}")

    def test_8ball_pool_is_sane(self):
        self.assertGreaterEqual(len(BALL_ANSWERS), 10)


class TestParseOptions(unittest.TestCase):
    def test_pipe_and_comma_both_work(self):
        self.assertEqual(parse_options("pizza | burger, sleep"), ["pizza", "burger", "sleep"])

    def test_trims_and_ignores_empty(self):
        self.assertEqual(parse_options("  a  |  | , b ,,"), ["a", "b"])

    def test_limit(self):
        self.assertEqual(len(parse_options("|".join(str(i) for i in range(40)))), 20)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestHelpers(unittest.TestCase):
    def test_format_duration(self):
        self.assertEqual(format_duration(0), "0:00")
        self.assertEqual(format_duration(72_000), "1:12")
        self.assertEqual(format_duration(3_723_000), "1:02:03")
        self.assertEqual(format_duration(-5_000), "0:00")

    def test_progress_bar(self):
        bar = progress_bar(50_000, 100_000, width=10)
        self.assertIn("●", bar)
        self.assertEqual(len(bar), 10)
        self.assertTrue(bar.startswith("▬▬▬▬▬"))
        empty = progress_bar(0, 100_000, width=10)
        self.assertEqual(empty, "●▬▬▬▬▬▬▬▬▬")
        full = progress_bar(100_000, 100_000, width=10)
        self.assertEqual(full, "▬▬▬▬▬▬▬▬▬●")
        self.assertEqual(progress_bar(10, 0, width=5), "▬▬▬▬▬")

    def test_clean_for_speech(self):
        raw = "<@123> check https://example.com 😂 <a:dance:456> bhai kya haal hai"
        cleaned = clean_for_speech(raw, 220)
        self.assertNotIn("<@", cleaned)
        self.assertNotIn("http", cleaned)
        self.assertNotIn("😂", cleaned)
        self.assertIn("bhai kya haal hai", cleaned)

    def test_clean_for_speech_length(self):
        cleaned = clean_for_speech("word " * 100, 50)
        self.assertLessEqual(len(cleaned), 50)

    def test_is_hindi(self):
        self.assertTrue(is_hindi("नमस्ते दोस्तों, सब ठीक है?"))
        self.assertTrue(is_hindi("hello भाई"))
        self.assertFalse(is_hindi("hello everyone, kya haal hai"))
        self.assertFalse(is_hindi(""))

    def test_resolve_language(self):
        self.assertEqual(resolve_language("Hindi"), "hi")
        self.assertEqual(resolve_language("english"), "en")
        self.assertEqual(resolve_language("es"), "es")
        self.assertIsNone(resolve_language("klingon"))
        self.assertIsNone(resolve_language(""))


# ---------------------------------------------------------------------------
# Panel rendering
# ---------------------------------------------------------------------------


def playing_state(**overrides):
    state = PanelState(
        guild_id=1,
        status="playing",
        voice_channel="Paradise Lounge",
        track=TrackInfo.from_track(make_track()),
        position_ms=72_000,
        upcoming=[TrackInfo.from_track(make_track(f"song {i}", duration=180_000)) for i in range(3)],
        volume=75,
        last_action="queued · Never Gonna Give You Up",
    )
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


class TestPanelRendering(unittest.TestCase):
    def test_playing_panel(self):
        embed = render_panel(playing_state())
        self.assertEqual(embed.title, "🎵 Paradise Music")
        values = {field.name: field.value for field in embed.fields}
        self.assertEqual(values["Status"], "▶ Playing")
        self.assertIn("1:12 / 3:32", values["Progress"])
        self.assertEqual(values["Queue"], "3 upcoming · 9:00")
        self.assertEqual(values["Volume"], "75%")
        self.assertEqual(values["Voice"], "Paradise Lounge")
        self.assertIn("Never Gonna Give You Up", embed.description)
        self.assertIn("<@42>", embed.description)

    def test_paused_panel(self):
        embed = render_panel(playing_state(status="paused"))
        values = {field.name: field.value for field in embed.fields}
        self.assertEqual(values["Status"], "⏸ Paused")

    def test_idle_panel(self):
        embed = render_panel(PanelState(guild_id=1, status="idle", voice_channel="lounge", volume=100))
        self.assertIn("nothing is currently playing", embed.description)
        self.assertIn("/play", embed.description)

    def test_disconnected_panel(self):
        embed = render_panel(PanelState(guild_id=1, status="disconnected", voice_channel=None))
        self.assertIn("not in voice", embed.description)
        self.assertIn("/join", embed.description)
        self.assertEqual(embed.fields, [])

    def test_speaking_panel(self):
        embed = render_panel(
            playing_state(status="speaking", spoken_text="hello everyone", track=None)
        )
        self.assertIn("hello everyone", embed.description)

    def test_live_stream_progress(self):
        track = make_track(is_stream=True, duration=0)
        state = playing_state(track=TrackInfo.from_track(track))
        embed = render_panel(state)
        values = {field.name: field.value for field in embed.fields}
        self.assertEqual(values["Progress"], "🔴 live")

    def test_now_playing_idle_and_playing(self):
        idle = render_now_playing(PanelState(guild_id=1, status="idle", voice_channel="x"))
        self.assertIn("nothing is playing", idle.description)
        playing = render_now_playing(playing_state())
        self.assertIn("Never Gonna Give You Up", playing.description)
        self.assertIn("youtube", [field.value for field in playing.fields])

    def test_queue_pages(self):
        state = playing_state()
        self.assertEqual(queue_page_count(state), 1)
        big = playing_state(
            upcoming=[TrackInfo.from_track(make_track(f"s{i}")) for i in range(19)]
        )
        self.assertEqual(queue_page_count(big), 3)
        page2 = render_queue_page(big, 1)
        self.assertIn("page 2/3", page2.footer.text)

    def test_empty_queue_state(self):
        state = PanelState(guild_id=1, status="idle", voice_channel="x")
        embed = render_queue_page(state, 0)
        self.assertIn("queue is empty", embed.fields[0].value)

    def test_button_states(self):
        disconnected = button_states(PanelState(guild_id=1, status="disconnected", voice_channel=None))
        self.assertTrue(disconnected["play"])
        self.assertTrue(disconnected["join"])
        self.assertFalse(disconnected["stop"])
        self.assertFalse(disconnected["pause"])
        playing = button_states(playing_state())
        self.assertTrue(playing["pause"])
        self.assertFalse(playing["resume"])
        paused = button_states(playing_state(status="paused"))
        self.assertFalse(paused["pause"])
        self.assertTrue(paused["resume"])
        speaking = button_states(playing_state(status="speaking", track=None))
        self.assertTrue(speaking["skip"])
        self.assertFalse(speaking["pause"])


class TestMusicServiceState(unittest.TestCase):
    def _service(self, guild):
        config = Config()
        store = SettingsStore(os.path.join(tempfile.mkdtemp(), "settings.json"))
        return MusicService(FakeBot([guild]), config, store)

    def test_panel_state_disconnected(self):
        service = self._service(FakeGuild(id=1))
        state = service.panel_state(1)
        self.assertEqual(state.status, "disconnected")

    def test_panel_state_playing(self):
        guild = FakeGuild(id=1, voice_client=object())
        service = self._service(guild)
        service.client = SimpleNamespace(
            player_manager=SimpleNamespace(get=lambda gid: FakePlayer(current=make_track(), queue=[make_track("next")])),
            node_manager=SimpleNamespace(available_nodes=[]),
        )
        state = service.panel_state(1)
        self.assertEqual(state.status, "playing")
        self.assertEqual(state.voice_channel, "lounge")
        self.assertEqual(len(state.upcoming), 1)
        self.assertEqual(state.track.title, "Never Gonna Give You Up")

    def test_panel_state_paused_and_speaking(self):
        guild = FakeGuild(id=1, voice_client=object())
        service = self._service(guild)
        service.client = SimpleNamespace(
            player_manager=SimpleNamespace(get=lambda gid: FakePlayer(current=make_track(), paused=True)),
            node_manager=SimpleNamespace(available_nodes=[]),
        )
        self.assertEqual(service.panel_state(1).status, "paused")
        service.session(1).tts_active = True
        service.session(1).tts_text = "hello everyone"
        state = service.panel_state(1)
        self.assertEqual(state.status, "speaking")
        self.assertEqual(state.spoken_text, "hello everyone")

    def test_lavalink_status(self):
        guild = FakeGuild(id=1)
        service = self._service(guild)
        self.assertEqual(service.lavalink_status(), "off")
        service.client = SimpleNamespace(node_manager=SimpleNamespace(available_nodes=[]))
        self.assertEqual(service.lavalink_status(), "down")
        service.client = SimpleNamespace(node_manager=SimpleNamespace(available_nodes=[object()]))
        self.assertEqual(service.lavalink_status(), "connected")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class TestSettings(unittest.TestCase):
    def test_round_trip(self):
        async def run():
            path = os.path.join(tempfile.mkdtemp(), "settings.json")
            store = SettingsStore(path)
            await store.update(123, tts_enabled=False, tts_cooldown=45, auto_disconnect=False)
            store2 = SettingsStore(path)
            loaded = store2.get(123)
            self.assertFalse(loaded.tts_enabled)
            self.assertEqual(loaded.tts_cooldown, 45)
            self.assertFalse(loaded.auto_disconnect)
            self.assertIn(123, store2.all_items())

        asyncio.run(run())

    def test_unknown_setting_rejected(self):
        async def run():
            store = SettingsStore(os.path.join(tempfile.mkdtemp(), "s.json"))
            with self.assertRaises(KeyError):
                await store.update(1, nonsense=True)

        asyncio.run(run())

    def test_validation_snaps_cooldown(self):
        settings = GuildSettings(tts_cooldown=33)
        settings.validate()
        self.assertIn(settings.tts_cooldown, TTS_COOLDOWN_CHOICES)


class TestConfig(unittest.TestCase):
    def test_from_env(self):
        config = Config.from_env(
            {
                "DISCORD_TOKEN": "tok",
                "LAVALINK_HOST": "node.example.com",
                "LAVALINK_PORT": "4000",
                "LAVALINK_PASSWORD": "secret",
                "LAVALINK_SECURE": "true",
                "AI_BASE_URL": "https://api.groq.com/openai/v1/",
                "AI_MODEL": "llama-3.3-70b-versatile",
                "TTS_MAX_LENGTH": "150",
                "LYRICS_ENABLED": "false",
            }
        )
        self.assertEqual(config.token, "tok")
        self.assertTrue(config.music_configured)
        self.assertEqual(config.lavalink_port, 4000)
        self.assertTrue(config.lavalink_secure)
        self.assertEqual(config.ai_base_url, "https://api.groq.com/openai/v1")
        self.assertTrue(config.ai_configured)
        self.assertEqual(config.tts_max_length, 150)
        self.assertFalse(config.lyrics_enabled)

    def test_empty_env_is_safe(self):
        config = Config.from_env({})
        self.assertFalse(config.music_configured)
        self.assertFalse(config.ai_configured)
        self.assertFalse(config.tts_configured)
        self.assertTrue(config.lyrics_enabled)

    def test_template_has_no_secrets(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env.example"
        )
        with open(path, encoding="utf-8") as handle:
            content = handle.read()
        self.assertNotIn("heavencloud.in", content.lower())


# ---------------------------------------------------------------------------
# TTS limits
# ---------------------------------------------------------------------------


class TestTTSLimits(unittest.TestCase):
    def _service(self, player):
        config = Config(
            lavalink_host="node.example.com",
            lavalink_password="pw",
            tts_max_length=220,
            tts_user_cooldown=30,
            tts_global_cooldown=8,
        )
        store = SettingsStore(os.path.join(tempfile.mkdtemp(), "settings.json"))
        music = FakeMusicForTTS(player)
        guild = FakeGuild(id=1, voice_client=object())
        member = SimpleNamespace(
            id=7,
            voice=SimpleNamespace(channel=SimpleNamespace(id=99)),
            guild_permissions=SimpleNamespace(move_members=False, administrator=False),
        )
        service = TTSService(FakeBot([guild]), config, music, store)
        return service, guild, member

    def test_passes_when_in_same_channel(self):
        service, guild, member = self._service(FakePlayer(channel_id=99))
        text = service.check(guild, member, "hello everyone")
        self.assertEqual(text, "hello everyone")

    def test_rejects_other_channel(self):
        service, guild, member = self._service(FakePlayer(channel_id=100))
        with self.assertRaises(TTSError):
            service.check(guild, member, "hello")

    def test_rejects_length(self):
        service, guild, member = self._service(FakePlayer(channel_id=99))
        with self.assertRaises(TTSError):
            service.check(guild, member, "word " * 100)

    def test_rejects_emoji_only(self):
        service, guild, member = self._service(FakePlayer(channel_id=99))
        with self.assertRaises(TTSError):
            service.check(guild, member, "😂😂😂")

    def test_per_user_cooldown(self):
        async def run():
            service, guild, member = self._service(FakePlayer(channel_id=99))
            await service.say(guild, member, "first")
            with self.assertRaises(TTSError) as caught:
                await service.say(guild, member, "second")
            self.assertIn("before I speak for you again", str(caught.exception))

        asyncio.run(run())

    def test_global_cooldown_blocks_second_user(self):
        async def run():
            service, guild, member = self._service(FakePlayer(channel_id=99))
            other = SimpleNamespace(
                id=8,
                voice=SimpleNamespace(channel=SimpleNamespace(id=99)),
                guild_permissions=SimpleNamespace(move_members=False, administrator=False),
            )
            await service.say(guild, member, "first")
            with self.assertRaises(TTSError) as caught:
                await service.say(guild, other, "second")
            self.assertIn("already talking", str(caught.exception))

        asyncio.run(run())

    def test_queue_cap(self):
        async def run():
            service, guild, member = self._service(FakePlayer(channel_id=99))
            service._ensure_worker = lambda guild_id: None  # keep items queued
            accepted = 0
            for index in range(8):
                # Fake cooldowns to keep adding items from the same user.
                service._user_last[(guild.id, member.id)] = (
                    time.monotonic() - service.config.tts_user_cooldown - index
                )
                service._guild_last[guild.id] = (
                    time.monotonic() - service.config.tts_global_cooldown - index
                )
                try:
                    await service.say(guild, member, f"line {index}")
                    accepted += 1
                except TTSError as error:
                    self.assertIn("queue is full", str(error))
            self.assertEqual(accepted, 5)
            self.assertEqual(len(service._queues[guild.id]), 5)

        asyncio.run(run())

    def test_bypass_allows_remote_control(self):
        service, guild, member = self._service(FakePlayer(channel_id=100))
        member.guild_permissions = SimpleNamespace(move_members=True, administrator=False)
        text = service.check(guild, member, "hello from afar")
        self.assertEqual(text, "hello from afar")

    def test_say_rejects_when_tts_disabled(self):
        async def run():
            service, guild, member = self._service(FakePlayer(channel_id=99))
            service.config = Config(tts_enabled=False)
            with self.assertRaises(TTSError):
                await service.say(guild, member, "hello")

        asyncio.run(run())


# ---------------------------------------------------------------------------
# TTS trigger parsing
# ---------------------------------------------------------------------------


class TestTTSTriggers(unittest.TestCase):
    def test_mention_form(self):
        text = "<@123456789> say hello everyone"
        stripped = text.replace("<@123456789>", " ")
        match = bot_module._MENTION_TTS_RE.match(stripped)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1).strip(), "hello everyone")

    def test_mention_with_punctuation(self):
        text = "<@123> , say hi there"
        match = bot_module._MENTION_TTS_RE.match(text.replace("<@123>", " "))
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1).strip(), "hi there")

    def test_mention_without_say_is_not_tts(self):
        text = "<@123> what do you think about pizza"
        match = bot_module._MENTION_TTS_RE.match(text.replace("<@123>", " "))
        self.assertIsNone(match)

    def test_plain_form(self):
        cases = (
            "paradise say hello everyone",
            "Paradise, say hello everyone",
            "paradise bolo namaste",
            "PARADISE: speak the truth",
            "paradise बोलो नमस्ते दोस्तों",
        )
        for case in cases:
            match = bot_module._PLAIN_TTS_RE.match(case)
            self.assertIsNotNone(match, case)

    def test_plain_form_requires_the_word_say(self):
        self.assertIsNone(bot_module._PLAIN_TTS_RE.match("paradise play some music"))
        self.assertIsNone(bot_module._PLAIN_TTS_RE.match("paradise is a nice server"))


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


# ---------------------------------------------------------------------------
# Lyrics
# ---------------------------------------------------------------------------


class TestLyrics(unittest.TestCase):
    def test_clean_query(self):
        title, artist = lyrics.clean_query(
            "Never Gonna Give You Up (Official Music Video)", "Rick Astley - Topic"
        )
        self.assertEqual(title, "Never Gonna Give You Up")
        self.assertEqual(artist, "Rick Astley")

    def test_paginate(self):
        text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
        pages = lyrics.paginate(text)
        self.assertLessEqual(len(pages), 5)
        for page in pages:
            self.assertLessEqual(len(page), 1900)

    def test_short_lyrics_one_page(self):
        self.assertEqual(len(lyrics.paginate("la la la")), 1)


# ---------------------------------------------------------------------------
# Wiring: every attribute and method the commands/buttons actually call
# ---------------------------------------------------------------------------


class TestWiring(unittest.TestCase):
    def test_bot_exposes_everything_the_commands_expect(self):
        async def run():
            bot = ParadiseBot(Config())
            for extension in EXTENSIONS:
                await bot.load_extension(extension)
            for attribute in (
                "config", "settings_store", "personality", "presence_manager",
                "ai", "music", "tts", "afk_states", "started_at",
                "_tts_note_last", "_afk_notice_last", "_ai_fallback_last",
            ):
                self.assertTrue(hasattr(bot, attribute), f"bot.{attribute} missing")
            for attribute in (
                "join", "play", "pause", "resume", "skip", "stop", "set_volume",
                "clear_queue", "shuffle_queue", "panel_state", "refresh_panel",
                "move_panel", "require_connected_control", "handle_forced_disconnect",
                "lavalink_status", "node_ready", "configured", "max_volume", "player",
            ):
                self.assertTrue(hasattr(bot.music, attribute), f"music.{attribute} missing")
            for attribute in ("say", "check", "cancel", "shutdown", "status"):
                self.assertTrue(hasattr(bot.tts, attribute), f"tts.{attribute} missing")
            for attribute in (
                "respond", "summarize", "record", "should_reply_to",
                "cooldown_remaining", "close", "available",
            ):
                self.assertTrue(hasattr(bot.ai, attribute), f"ai.{attribute} missing")

        asyncio.run(run())

    def test_panel_components_construct_with_fake_service(self):
        from functions.music.panel import (
            MusicPanelView,
            PlayModal,
            QueuePaginator,
            VolumeModal,
        )

        service = SimpleNamespace(max_volume=150, panel_state=lambda gid: playing_state())
        view = MusicPanelView(service, 1, button_states(playing_state()))
        children = {child.label for child in view.children if hasattr(child, "label")}
        for label in ("Play", "Pause", "Resume", "Skip", "Stop", "Queue",
                      "Now Playing", "Volume", "Join"):
            self.assertIn(label, children)
        self.assertIsNone(view.timeout, "panel view must be persistent (timeout=None)")
        modal = PlayModal(service, 1)
        self.assertEqual(len(modal.children), 1)
        volume_modal = VolumeModal(service, 1, 150)
        self.assertEqual(len(volume_modal.children), 1)
        paginator = QueuePaginator(service, 1)
        self.assertIsNotNone(paginator.previous_button)
        self.assertIsNotNone(paginator.next_button)

    def test_settings_view_constructs(self):
        import commands.system as system

        async def run():
            bot = ParadiseBot(Config())
            for extension in EXTENSIONS:
                await bot.load_extension(extension)
            view = system.SettingsView(bot, 1)
            self.assertEqual(len(view.children), 5)

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Music service flow (fake Lavalink client)
# ---------------------------------------------------------------------------


class FakeLoadResult:
    def __init__(self, load_type, tracks=None):
        self.load_type = load_type
        self.tracks = tracks or []


class FakeRealPlayer:
    """Mimics lavalink.DefaultPlayer closely enough for the flows."""

    def __init__(self, channel_id=99):
        self.channel_id = channel_id
        self.current = None
        self.queue = []
        self.paused = False
        self.volume = 100
        self.position = 0
        self.calls = []

    @property
    def is_connected(self):
        return self.channel_id is not None

    @property
    def is_playing(self):
        return self.is_connected and self.current is not None

    def add(self, track, requester=0, index=None):
        track.requester = requester
        self.queue.append(track)

    async def play(self, track=None, **kwargs):
        self.calls.append(("play", track))
        self.current = track or (self.queue.pop(0) if self.queue else None)
        self.paused = bool(kwargs.get("pause", False))

    async def stop(self):
        self.calls.append(("stop",))
        self.current = None

    async def skip(self):
        self.calls.append(("skip",))
        self.current = self.queue.pop(0) if self.queue else None

    async def set_pause(self, paused):
        self.paused = paused

    async def set_volume(self, volume):
        self.volume = volume


class FakeVoiceChannel:
    def __init__(self, id=99, guild=None):
        self.id = id
        self.guild = guild
        self.user_limit = 0
        self.name = "Paradise Lounge"
        self.members = []
        self.connected = []

    async def connect(self, *, cls, self_deaf=False, timeout=None, reconnect=False):
        self.connected.append(cls)
        self.guild.voice_client = FakeVoiceProtocol(self.guild, self)
        # The real bridge sets the player's channel via VOICE_STATE_UPDATE.
        if self.guild.player is not None:
            self.guild.player.channel_id = self.id

    def permissions_for(self, member):
        return SimpleNamespace(connect=True, speak=True, move_members=True)


class FakeVoiceProtocol:
    def __init__(self, guild, channel):
        self.guild = guild
        self.channel = channel
        self.moves = []
        self.disconnected = 0

    async def move_to(self, channel):
        self.moves.append(channel)
        if self.guild.player is not None:
            self.guild.player.channel_id = channel.id

    async def disconnect(self, *, force=False):
        self.disconnected += 1
        self.guild.voice_client = None
        if self.guild.player is not None:
            self.guild.player.channel_id = None


class MusicFlowGuild:
    def __init__(self, id=1, player=None):
        self.id = id
        self.name = "Paradise"
        self.voice_client = None
        self.me = SimpleNamespace(id=2)
        self.player = player
        self.channels = {99: FakeVoiceChannel(id=99, guild=self),
                         100: FakeVoiceChannel(id=100, guild=self)}
        self.voice_states = {}

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    async def change_voice_state(self, *, channel=None, self_mute=False, self_deaf=False):
        pass


class MusicFlowBot(FakeBot):
    def __init__(self, guild):
        super().__init__([guild])


class MusicFlowMember:
    def __init__(self, id=7, channel_id=99, move_members=False, guild=None):
        self.id = id
        self.display_name = "Ash"
        if channel_id is None:
            self.voice = None
        else:
            channel = guild.channels[channel_id] if guild else SimpleNamespace(id=channel_id)
            self.voice = SimpleNamespace(channel=channel)
        self.guild_permissions = SimpleNamespace(
            move_members=move_members, administrator=False
        )


class FakeLavalinkClient:
    def __init__(self, player, result):
        self.player_manager = SimpleNamespace(
            get=lambda gid: player,
            create=lambda guild_id=None, **kw: player,
        )
        self.node_manager = SimpleNamespace(available_nodes=[object()])
        self._result = result
        self.queries = []

    async def get_tracks(self, identifier):
        self.queries.append(identifier)
        return self._result


class TestMusicFlows(unittest.TestCase):
    def _service(self, player, result=None, guild=None):
        guild = guild or MusicFlowGuild(player=player)
        guild.player = player
        config = Config(lavalink_host="n", lavalink_password="p")
        store = SettingsStore(os.path.join(tempfile.mkdtemp(), "settings.json"))
        service = MusicService(MusicFlowBot(guild), config, store)
        service.client = FakeLavalinkClient(player, result or FakeLoadResult("track", [make_track()]))
        return service, guild

    def _member(self, guild, **kwargs):
        return MusicFlowMember(guild=guild, **kwargs)

    def test_play_joins_and_starts(self):
        async def run():
            player = FakeRealPlayer(channel_id=None)
            service, guild = self._service(player)
            result = await service.play(guild, self._member(guild), "never gonna give you up")
            self.assertTrue(result.started)
            self.assertEqual(result.position, 0)
            self.assertEqual(player.current.title, "Never Gonna Give You Up")
            self.assertEqual(service.client.queries, ["ytsearch:never gonna give you up"])
            self.assertFalse(service.session(guild.id).sticky, "/play sessions are not sticky")

        asyncio.run(run())

    def test_play_url_passes_through(self):
        async def run():
            player = FakeRealPlayer(channel_id=None)
            service, guild = self._service(player)
            await service.play(guild, self._member(guild), "https://youtu.be/dQw4w9WgXcQ")
            self.assertEqual(
                service.client.queries, ["https://youtu.be/dQw4w9WgXcQ"]
            )

        asyncio.run(run())

    def test_play_rejects_other_channel(self):
        async def run():
            player = FakeRealPlayer(channel_id=99)
            player.current = make_track()
            service, guild = self._service(player)
            guild.voice_client = FakeVoiceProtocol(guild, guild.channels[99])
            member = self._member(guild, channel_id=100)
            with self.assertRaisesRegex(MusicError, "join"):
                await service.play(guild, member, "something")

        asyncio.run(run())

    def test_play_no_results(self):
        async def run():
            player = FakeRealPlayer(channel_id=None)
            service, guild = self._service(player, FakeLoadResult("empty", []))
            with self.assertRaisesRegex(MusicError, "couldn't find anything"):
                await service.play(guild, self._member(guild), "silence")

        asyncio.run(run())

    def test_play_queues_when_already_playing(self):
        async def run():
            player = FakeRealPlayer(channel_id=99)
            player.current = make_track("first")
            player.queue = [make_track("second")]
            service, guild = self._service(player)
            guild.voice_client = FakeVoiceProtocol(guild, guild.channels[99])
            result = await service.play(guild, self._member(guild), "third")
            self.assertFalse(result.started)
            self.assertEqual(result.position, 2)
            self.assertEqual(len(player.queue), 2)

        asyncio.run(run())

    def test_pause_resume_skip_volume(self):
        async def run():
            player = FakeRealPlayer(channel_id=99)
            player.current = make_track()
            player.queue = [make_track("next")]
            service, guild = self._service(player)
            guild.voice_client = FakeVoiceProtocol(guild, guild.channels[99])
            member = self._member(guild)

            await service.pause(guild, member)
            self.assertTrue(player.paused)
            with self.assertRaisesRegex(MusicError, "already paused"):
                await service.pause(guild, member)

            await service.resume(guild, member)
            self.assertFalse(player.paused)

            result = await service.skip(guild, member)
            self.assertEqual(result.skipped.title, "Never Gonna Give You Up")
            self.assertEqual(result.up_next.title, "next")

            value = await service.set_volume(guild, member, 130)
            self.assertEqual(value, 130)
            self.assertEqual(service.session(guild.id).volume, 130)
            with self.assertRaisesRegex(MusicError, "between 0 and 150"):
                await service.set_volume(guild, member, 500)

        asyncio.run(run())

    def test_clearqueue_keeps_current_and_shuffle_keeps_first_slot(self):
        async def run():
            player = FakeRealPlayer(channel_id=99)
            player.current = make_track("current")
            player.queue = [make_track(f"q{i}") for i in range(6)]
            service, guild = self._service(player)
            guild.voice_client = FakeVoiceProtocol(guild, guild.channels[99])
            member = self._member(guild)

            removed = service.clear_queue(guild, member)
            self.assertEqual(removed, 6)
            self.assertEqual(player.queue, [])
            self.assertEqual(player.current.title, "current")

            player.queue = [make_track(f"q{i}") for i in range(6)]
            shuffled = service.shuffle_queue(guild, member)
            self.assertEqual(shuffled, 6)
            self.assertEqual(player.current.title, "current")
            titles = {t.title for t in player.queue}
            self.assertEqual(titles, {f"q{i}" for i in range(6)})

            player.queue = [make_track("only")]
            with self.assertRaisesRegex(MusicError, "nothing to shuffle"):
                service.shuffle_queue(guild, member)

        asyncio.run(run())

    def test_stop_clears_everything_and_disconnects(self):
        async def run():
            player = FakeRealPlayer(channel_id=99)
            player.current = make_track()
            player.queue = [make_track("next")]
            service, guild = self._service(player)
            vc = FakeVoiceProtocol(guild, guild.channels[99])
            guild.voice_client = vc
            member = self._member(guild)

            await service.stop(guild, member)
            self.assertEqual(player.calls, [("stop",)])
            self.assertEqual(player.queue, [])
            self.assertEqual(vc.disconnected, 1)
            session = service.session(guild.id)
            self.assertFalse(session.sticky)
            self.assertFalse(session.tts_active)

        asyncio.run(run())

    def test_stop_when_not_connected_is_a_clean_error(self):
        async def run():
            player = FakeRealPlayer(channel_id=None)
            service, guild = self._service(player)
            with self.assertRaisesRegex(MusicError, "not connected"):
                await service.stop(guild, self._member(guild))

        asyncio.run(run())

    def test_volume_persists_across_reconnect(self):
        async def run():
            player = FakeRealPlayer(channel_id=None)
            service, guild = self._service(player)
            guild.voice_client = None
            await service.play(guild, self._member(guild), "song")  # connects
            connected = FakeRealPlayer(channel_id=99)
            service.client.player_manager = SimpleNamespace(
                get=lambda gid: connected,
                create=lambda guild_id=None, **kw: connected,
            )
            await service.set_volume(guild, self._member(guild), 80)
            # Simulate a disconnect + rejoin: fresh player, session keeps volume.
            guild.voice_client = None
            service._reset_session(guild.id)
            self.assertEqual(service.session(guild.id).volume, 80)

        asyncio.run(run())

    def test_auto_disconnect_only_for_non_sticky_sessions(self):
        async def run():
            player = FakeRealPlayer(channel_id=99)
            player.current = make_track()
            service, guild = self._service(player)
            vc = FakeVoiceProtocol(guild, guild.channels[99])
            guild.voice_client = vc
            # Nobody else in the channel.
            session = service.session(guild.id)

            # Sticky (/join): never auto-disconnect.
            session.sticky = True
            session.alone_since = time.monotonic() - 10**6
            await service._maintenance_tick()
            self.assertEqual(vc.disconnected, 0)

            # Non-sticky music session past the threshold: disconnect.
            session.sticky = False
            session.alone_since = time.monotonic() - 10**6
            await service._maintenance_tick()
            self.assertEqual(vc.disconnected, 1)

            # Auto-disconnect disabled via guild settings: stay.
            player2 = FakeRealPlayer(channel_id=99)
            player2.current = make_track()
            service2, guild2 = self._service(player2, guild=MusicFlowGuild(id=2, player=player2))
            vc2 = FakeVoiceProtocol(guild2, guild2.channels[99])
            guild2.voice_client = vc2
            await service2.settings.update(2, auto_disconnect=False)
            session2 = service2.session(2)
            session2.sticky = False
            session2.alone_since = time.monotonic() - 10**6
            await service2._maintenance_tick()
            self.assertEqual(vc2.disconnected, 0)

        asyncio.run(run())

    def test_join_moves_only_when_idle_or_allowed(self):
        async def run():
            player = FakeRealPlayer(channel_id=99)
            service, guild = self._service(player)
            vc = FakeVoiceProtocol(guild, guild.channels[99])
            guild.voice_client = vc

            # Idle: moves freely.
            result = await service.join(guild, self._member(guild, channel_id=100))
            self.assertEqual(result.action, "moved")
            self.assertEqual(len(vc.moves), 1)

            # Playing and requester is elsewhere: denied without permission.
            player.channel_id = 99
            player.current = make_track()
            with self.assertRaisesRegex(MusicError, "playing for people"):
                await service.join(guild, self._member(guild, channel_id=100))

            # With Move Members: allowed.
            player.channel_id = 99
            result = await service.join(
                guild, self._member(guild, channel_id=100, move_members=True)
            )
            self.assertEqual(result.action, "moved")

            player.channel_id = 99

            # Already there: sticky and acknowledged.
            result = await service.join(guild, self._member(guild, channel_id=99))
            self.assertEqual(result.action, "already")
            self.assertTrue(service.session(guild.id).sticky)

        asyncio.run(run())

    def test_join_requires_voice(self):
        async def run():
            player = FakeRealPlayer(channel_id=None)
            service, guild = self._service(player)
            with self.assertRaisesRegex(MusicError, "you're not in a voice channel"):
                await service.join(guild, _voiceless_member())

        asyncio.run(run())


def _voiceless_member():
    return SimpleNamespace(
        id=9,
        display_name="Ghost",
        voice=None,
        guild_permissions=SimpleNamespace(move_members=False, administrator=False),
    )


if __name__ == "__main__":
    unittest.main()
