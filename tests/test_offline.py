"""Offline validation for Paradise.

Runs without a Discord token, network, or Lavalink node: it checks the
exact registered command set, the removed commands, and the pure logic
behind ping, coinflip, TTS limits, panels, queue pagination, settings,
lyrics, AI message building, node plugin diagnostics and the search
source fallback.

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
from unittest.mock import AsyncMock, MagicMock, patch

import discord

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
    MusicPanelView,
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
from lavalink.server import LoadType  # noqa: E402

EXPECTED_COMMANDS = {
    "ping", "8ball", "coinflip", "choose",
    "join", "play", "pause", "resume", "skip", "previous", "stop", "queue",
    "nowplaying", "volume", "clearqueue", "shuffle", "leave", "lyrics",
    "remove", "move", "seek", "forward", "backward", "loop", "autoplay", "filter",
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
        is_seekable=True,
        requester=42,
        source_name="youtube",
        identifier=f"id-{title}",
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

    def test_parse_duration(self):
        from functions.helpers import parse_duration

        self.assertEqual(parse_duration("1:32"), 92_000)
        self.assertEqual(parse_duration("  2:05 "), 125_000)
        self.assertEqual(parse_duration("1:02:03"), 3_723_000)
        self.assertEqual(parse_duration("15"), 15_000)
        self.assertEqual(parse_duration("0:00"), 0)
        self.assertIsNone(parse_duration("-5"))
        self.assertIsNone(parse_duration(""))
        self.assertIsNone(parse_duration("abc"))
        self.assertIsNone(parse_duration("1:99"))
        self.assertIsNone(parse_duration("100000"))  # > 24h is not a song position

    def test_normalize_track_key(self):
        from functions.helpers import normalize_track_key

        base = normalize_track_key("Song", "Artist")
        self.assertEqual(normalize_track_key("Artist - Song (Official Video)"), base)
        self.assertEqual(normalize_track_key("Artist - Song [Official Audio]"), base)
        self.assertEqual(normalize_track_key("Artist - Song"), base)
        self.assertEqual(normalize_track_key("Song (feat. Someone)", "Artist"), base)
        self.assertNotEqual(normalize_track_key("Song (Live at Wembley)", "Artist"), base)
        self.assertNotEqual(normalize_track_key("Other Song", "Artist"), base)

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

    def test_v2_panel_handles_long_titles_and_missing_artwork(self):
        from functions.music.panel import MusicPanelView

        track = TrackInfo.from_track(make_track("x" * 500, artwork_url=None, source_name="http", identifier=None))
        state = playing_state(track=track, upcoming=[])
        service = SimpleNamespace(max_volume=150, panel_state=lambda gid: state)
        view = MusicPanelView(service, 1, button_states(state), state)
        payload = view.to_components()
        self.assertEqual(payload[0]["type"], 17)
        rendered = str(payload)
        self.assertIn("Queue is empty", rendered)
        self.assertNotIn("'type': 11", rendered)  # no broken thumbnail component
        self.assertLess(len(rendered), 7000)

    def test_button_states(self):
        disconnected = button_states(PanelState(guild_id=1, status="disconnected", voice_channel=None))
        self.assertTrue(disconnected["play"])
        self.assertTrue(disconnected["join"])
        self.assertFalse(disconnected["stop"])
        self.assertFalse(disconnected["pause"])
        self.assertFalse(disconnected["previous"])
        self.assertFalse(disconnected["loop"])
        self.assertFalse(disconnected["autoplay"])
        playing = button_states(playing_state())
        self.assertTrue(playing["pause"])
        self.assertFalse(playing["resume"])
        self.assertTrue(playing["loop"])
        self.assertTrue(playing["autoplay"])
        self.assertTrue(playing["effects"])
        self.assertFalse(playing["previous"], "no history yet")
        playing_with_history = button_states(playing_state(has_previous=True))
        self.assertTrue(playing_with_history["previous"])
        paused = button_states(playing_state(status="paused"))
        self.assertFalse(paused["pause"])
        self.assertTrue(paused["resume"])
        speaking = button_states(playing_state(status="speaking", track=None))
        self.assertTrue(speaking["skip"])
        self.assertFalse(speaking["pause"])
        no_effects = button_states(playing_state(effects_available=False))
        self.assertFalse(no_effects["effects"])

    def test_panel_state_chips_include_loop_and_autoplay(self):
        from functions.music.panel import MusicPanelView

        state = playing_state(loop_mode="queue", autoplay=True, filters=["Bass Boost"])
        view = MusicPanelView(
            SimpleNamespace(max_volume=150, panel_state=lambda gid: state),
            1, button_states(state), state,
        )
        payload = str(view.to_components())
        self.assertIn("Loop: Queue", payload)
        self.assertIn("Autoplay", payload)
        self.assertIn("Bass Boost", payload)

    def test_panel_has_three_control_rows(self):
        from functions.music.panel import MusicPanelView

        state = playing_state(has_previous=True)
        view = MusicPanelView(
            SimpleNamespace(max_volume=150, panel_state=lambda gid: state),
            1, button_states(state), state,
        )
        labels = set()

        def collect(items):
            for item in items:
                if "label" in item:
                    labels.add(item["label"])
                collect(item.get("components", []))

        collect(view.to_components())
        for label in (
            "Previous", "Pause", "Next", "Shuffle", "Queue", "Volume", "Lyrics",
            "Stop", "Loop: Off", "Autoplay", "Effects", "Add Music",
        ):
            self.assertIn(label, labels, f"missing panel button {label}")


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

    def test_transition_keeps_previous_track_until_queue_end(self):
        guild = FakeGuild(id=1, voice_client=object())
        player = FakePlayer(current=make_track())
        service = self._service(guild)
        service.client = SimpleNamespace(
            player_manager=SimpleNamespace(get=lambda gid: player),
            node_manager=SimpleNamespace(available_nodes=[]),
        )
        playing = service.panel_state(1)
        player.current = None
        transitioning = service.panel_state(1)
        self.assertEqual(transitioning.status, "loading")
        self.assertEqual(transitioning.track.title, playing.track.title)
        self.assertEqual(transitioning.position_ms, playing.position_ms)

    def test_speaking_preserves_loaded_track_position_and_music_queue(self):
        guild = FakeGuild(id=1, voice_client=object())
        service = self._service(guild)
        tts_track = make_track("voice clip", duration=2_000)
        service.client = SimpleNamespace(
            player_manager=SimpleNamespace(get=lambda gid: FakePlayer(current=tts_track, queue=[])),
            node_manager=SimpleNamespace(available_nodes=[]),
        )
        session = service.session(1)
        session.tts_active = True
        session.tts_text = "announcement"
        session.tts_stash = (make_track("saved music"), 61_000, [make_track("saved next")], True)
        state = service.panel_state(1)
        self.assertEqual(state.active_track.title, "saved music")
        self.assertEqual(state.position_ms, 61_000)
        self.assertEqual([track.title for track in state.upcoming], ["saved next"])
        self.assertTrue(button_states(state)["lyrics"])

    def test_refresh_panel_serializes_concurrent_edits(self):
        async def run():
            guild = FakeGuild(id=1)
            service = self._service(guild)

            class Message:
                def __init__(self):
                    self.calls = 0
                    self.active = 0
                    self.max_active = 0

                async def edit(self, **kwargs):
                    self.calls += 1
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                    self.assert_v2 = kwargs["view"].to_components()[0]["type"] == 17
                    await asyncio.sleep(0)
                    self.active -= 1

            message = Message()
            service._panel_messages[1] = message
            await asyncio.gather(*(service.refresh_panel(1) for _ in range(8)))
            self.assertEqual(message.calls, 8)
            self.assertEqual(message.max_active, 1)
            self.assertTrue(message.assert_v2)

        asyncio.run(run())

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

    def test_expected_plugins_parsing(self):
        config = Config.from_env(
            {"LAVALINK_EXPECTED_PLUGINS": " youtube-plugin:f45bb , chanom-plugin:1.0.6 , , :x , bare ,foo:"}
        )
        self.assertEqual(
            config.expected_plugins,
            (
                ("youtube-plugin", "f45bb"),
                ("chanom-plugin", "1.0.6"),
                ("bare", None),
                ("foo", None),
            ),
        )

    def test_expected_plugins_empty(self):
        self.assertEqual(Config().expected_plugins, ())
        self.assertEqual(Config(lavalink_expected_plugins="   ").expected_plugins, ())


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

    def test_clean_query_strips_noise_but_keeps_meaning(self):
        title, _ = lyrics.clean_query("Song [Official Audio] (Lyric Video)", "Artist")
        self.assertEqual(title, "Song")
        title, _ = lyrics.clean_query("Song (Live at Wembley)", "Artist")
        self.assertEqual(title, "Song (Live at Wembley)")
        title, _ = lyrics.clean_query("Song (feat. Someone)", "Artist")
        self.assertEqual(title, "Song")

    def test_paginate(self):
        text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
        pages = lyrics.paginate(text)
        self.assertGreater(len(pages), 1)
        self.assertLessEqual(len(pages), 12)
        for page in pages:
            self.assertLessEqual(len(page), 1900)

    def test_short_lyrics_one_page(self):
        self.assertEqual(len(lyrics.paginate("la la la")), 1)

    def test_cache_reuses_success_and_missing_results(self):
        async def run():
            cache = lyrics.LyricsCache(ttl=60, missing_ttl=60)
            calls = 0

            async def provider(artist, title, duration_ms=None):
                nonlocal calls
                calls += 1
                if title == "song":
                    return lyrics.LyricsResult("first line\nsecond line", "LRCLIB")
                return None

            result = await cache.get("hit", "artist", "song", fetcher=provider)
            self.assertEqual(result.text, "first line\nsecond line")
            self.assertEqual(result.source, "LRCLIB")
            again = await cache.get("hit", "artist", "song", fetcher=provider)
            self.assertEqual(again.text, "first line\nsecond line")
            self.assertIsNone(await cache.get("miss", "artist", "missing", fetcher=provider))
            self.assertIsNone(await cache.get("miss", "artist", "missing", fetcher=provider))
            self.assertEqual(calls, 2)

        asyncio.run(run())

    def test_cache_deduplicates_inflight_and_handles_provider_failure(self):
        async def run():
            cache = lyrics.LyricsCache(ttl=60, missing_ttl=60)
            calls = 0

            async def provider(artist, title, duration_ms=None):
                nonlocal calls
                calls += 1
                await asyncio.sleep(0)
                if title == "broken":
                    raise asyncio.TimeoutError
                return lyrics.LyricsResult("lyrics", "lyrics.ovh")

            values = await asyncio.gather(*[
                cache.get("same", "artist", "song", fetcher=provider) for _ in range(4)
            ])
            self.assertEqual([value.text for value in values], ["lyrics"] * 4)
            self.assertEqual(calls, 1)
            self.assertIsNone(await cache.get("broken", "artist", "broken", fetcher=provider))

        asyncio.run(run())

    def test_fetch_lyrics_tries_providers_in_order(self):
        async def run():
            from unittest.mock import patch

            async def lrclib(artist, title, duration_ms=None):
                return lyrics.LyricsResult("from lrclib", "LRCLIB")

            with patch.object(lyrics, "_fetch_lrclib", lrclib), \
                 patch.object(lyrics, "_fetch_lyrics_ovh", AsyncMock()) as ovh:
                result = await lyrics.fetch_lyrics("artist", "song")
                self.assertEqual(result.source, "LRCLIB")
                ovh.assert_not_called()

            calls = []

            async def failing(artist, title, duration_ms=None):
                calls.append(title)
                return None

            async def working(artist, title, duration_ms=None):
                calls.append(title)
                return lyrics.LyricsResult("from ovh", "lyrics.ovh")

            with patch.object(lyrics, "_fetch_lrclib", failing), \
                 patch.object(lyrics, "_fetch_lyrics_ovh", working), \
                 patch.object(lyrics, "_fetch_genius", failing):
                result = await lyrics.fetch_lyrics("artist", "song")
                self.assertEqual(result.source, "lyrics.ovh")
                self.assertEqual(len(calls), 2)

        asyncio.run(run())

    def test_genius_skipped_without_key(self):
        async def run():
            # The real provider returns immediately when no key is configured
            # (no network call, no exception) — that early return is the guard.
            with patch.object(lyrics, "GENIUS_API_KEY", None):
                result = await lyrics._fetch_genius("artist", "song")
            self.assertIsNone(result)

        asyncio.run(run())

    def test_track_key_prevents_stale_lyrics_preview(self):
        old = TrackInfo.from_track(make_track("old song", identifier="old"))
        current_raw = make_track("new song", identifier="new")
        current = TrackInfo.from_track(current_raw)
        lyrics.CACHE._entries[old.lyrics_key] = lyrics.LyricsEntry("old words", "LRCLIB", time.monotonic() + 60)
        guild = FakeGuild(id=1, voice_client=object())
        store = SettingsStore(os.path.join(tempfile.mkdtemp(), "settings.json"))
        service = MusicService(FakeBot([guild]), Config(), store)
        service.client = SimpleNamespace(
            player_manager=SimpleNamespace(get=lambda gid: FakePlayer(current=current_raw)),
            node_manager=SimpleNamespace(available_nodes=[]),
        )
        state = service.panel_state(1)
        self.assertFalse(state.lyrics_loaded)
        self.assertIsNone(state.lyrics_preview)
        lyrics.CACHE._entries[current.lyrics_key] = lyrics.LyricsEntry("new words", "LRCLIB", time.monotonic() + 60)
        state = service.panel_state(1)
        self.assertTrue(state.lyrics_loaded)
        self.assertEqual(state.lyrics_preview, "new words")


# ---------------------------------------------------------------------------
# Playback history + Previous (A → B → C → D, then back)
# ---------------------------------------------------------------------------


class TestPlaybackHistory(unittest.TestCase):
    def _service(self, player, responses=None, **config_kwargs):
        guild = MusicFlowGuild(player=player)
        guild.player = player
        guild.voice_client = FakeVoiceProtocol(guild, guild.channels[99])
        config = Config(lavalink_host="n", lavalink_password="p", **config_kwargs)
        store = SettingsStore(os.path.join(tempfile.mkdtemp(), "settings.json"))
        service = MusicService(MusicFlowBot(guild), config, store)
        service.client = FakeSearchClient(player, responses or {})
        return service, guild

    def _member(self, guild):
        return MusicFlowMember(guild=guild)

    async def _play(self, service, player, track):
        await player.play(track)
        service._record_history(player, track)

    def test_previous_walks_back_through_history(self):
        async def run():
            player = FakeRealPlayer()
            service, guild = self._service(player)
            member = self._member(guild)
            for name in "ABCD":
                await self._play(service, player, make_track(f"song {name}"))
            session = service.session(guild.id)
            self.assertEqual(session.history_index, 3)

            result = await service.previous(guild, member)
            self.assertEqual(result.track.title, "song C")
            self.assertEqual(session.history_index, 2)

            result = await service.previous(guild, member)
            self.assertEqual(result.track.title, "song B")
            self.assertEqual(session.history_index, 1)

            result = await service.previous(guild, member)
            self.assertEqual(result.track.title, "song A")
            self.assertEqual(session.history_index, 0)

            with self.assertRaisesRegex(MusicError, "isn't a previous track"):
                await service.previous(guild, member)

        asyncio.run(run())

    def test_normal_playback_truncates_abandoned_branch(self):
        async def run():
            player = FakeRealPlayer()
            service, guild = self._service(player)
            member = self._member(guild)
            for name in "ABC":
                await self._play(service, player, make_track(f"song {name}"))
            await service.previous(guild, member)   # C -> B
            await service.previous(guild, member)   # B -> A
            session = service.session(guild.id)
            self.assertEqual(session.history_index, 0)
            # Normal playback from here keeps A and drops the dead branch.
            await self._play(service, player, make_track("song E"))
            titles = [entry.track.title for entry in session.playback_history]
            self.assertEqual(titles, ["song A", "song E"])
            self.assertEqual(session.history_index, 1)

        asyncio.run(run())

    def test_consecutive_repeats_do_not_duplicate_history(self):
        async def run():
            player = FakeRealPlayer()
            service, guild = self._service(player)
            track = make_track("on repeat")
            await self._play(service, player, track)
            await self._play(service, player, make_track("on repeat"))  # loop replay
            session = service.session(guild.id)
            self.assertEqual(len(session.playback_history), 1)
            self.assertEqual(session.history_index, 0)

        asyncio.run(run())

    def test_tts_clips_never_enter_history(self):
        async def run():
            player = FakeRealPlayer()
            service, guild = self._service(player)
            session = service.session(guild.id)
            session.tts_active = True
            service._record_history(player, make_track("voice clip"))
            self.assertEqual(session.playback_history, [])
            session.tts_active = False
            service._record_history(player, make_track("real song"))
            self.assertEqual(len(session.playback_history), 1)

        asyncio.run(run())

    def test_history_is_bounded(self):
        async def run():
            player = FakeRealPlayer()
            service, guild = self._service(player, music_history_limit=5)
            for index in range(10):
                await self._play(service, player, make_track(f"song {index}"))
            session = service.session(guild.id)
            self.assertEqual(len(session.playback_history), 5)
            self.assertEqual(session.history_index, 4)
            self.assertEqual(session.playback_history[0].track.title, "song 5")

        asyncio.run(run())

    def test_previous_preserves_resume_position(self):
        async def run():
            player = FakeRealPlayer()
            service, guild = self._service(player)
            member = self._member(guild)
            track_a = make_track("song A")
            track_b = make_track("song B")
            await self._play(service, player, track_a)
            await self._play(service, player, track_b)
            player.position = 90_000
            await service.previous(guild, member)  # back to A; B remembers 1:30
            session = service.session(guild.id)
            self.assertEqual(session.playback_history[1].resume_ms, 90_000)
            # The original track object is preserved — no re-search happened.
            self.assertIs(session.playback_history[1].track, track_b)
            self.assertEqual(player.current.title, "song A")
            self.assertNotIn(("play", track_b), [("p", t) for p, t, _ in player.calls])

        asyncio.run(run())

    def test_previous_uses_saved_offset_when_returning(self):
        async def run():
            player = FakeRealPlayer()
            service, guild = self._service(player)
            member = self._member(guild)
            track_a = make_track("song A")
            await self._play(service, player, track_a)
            session = service.session(guild.id)
            session.playback_history[0].resume_ms = 50_000
            await self._play(service, player, make_track("song B"))
            await service.previous(guild, member)
            kind, track, kwargs = player.calls[-1]
            self.assertEqual(kind, "play")
            self.assertIs(track, track_a)
            self.assertEqual(kwargs.get("start_time"), 50_000)

        asyncio.run(run())

    def test_previous_blocked_while_speaking(self):
        async def run():
            player = FakeRealPlayer()
            service, guild = self._service(player)
            member = self._member(guild)
            await self._play(service, player, make_track("song A"))
            await self._play(service, player, make_track("song B"))
            service.session(guild.id).tts_active = True
            with self.assertRaisesRegex(MusicError, "I'm speaking"):
                await service.previous(guild, member)

        asyncio.run(run())

    def test_stop_clears_history(self):
        async def run():
            player = FakeRealPlayer()
            service, guild = self._service(player)
            member = self._member(guild)
            await self._play(service, player, make_track("song"))
            await service.stop(guild, member)
            session = service.session(guild.id)
            self.assertEqual(session.playback_history, [])
            self.assertEqual(session.history_index, -1)
            self.assertEqual(session.loop_mode, "off")
            self.assertFalse(session.autoplay_enabled)

        asyncio.run(run())


def _history_entry(track, resume_ms=None):
    from functions.music.service import HistoryEntry

    return HistoryEntry(track, resume_ms)


# ---------------------------------------------------------------------------
# Panel buttons must call the same MusicService methods as the commands
# ---------------------------------------------------------------------------


class FakePanelMember:
    """Stands in for discord.Member inside panel callback tests."""

    def __init__(self, member_id=7, channel_id=99, move_members=False):
        self.id = member_id
        channel = SimpleNamespace(id=channel_id) if channel_id is not None else None
        self.voice = SimpleNamespace(channel=channel)
        self.guild_permissions = SimpleNamespace(
            move_members=move_members, administrator=False
        )


class FakePanelInteraction:
    """Just enough Interaction for MusicPanelView callbacks."""

    def __init__(self, guild, member):
        self.guild = guild
        self.user = member
        self.channel_id = None
        self.responses = []
        self.modals = []
        self.response = SimpleNamespace(
            is_done=lambda: False,
            send_message=self._send,
            defer=self._defer,
            send_modal=self._modal,
            edit_message=AsyncMock(),
        )
        self.followup = SimpleNamespace(send=self._send)

    async def _send(self, text=None, *, ephemeral=None, view=None, **kwargs):
        self.responses.append(text or view)

    async def _defer(self, **kwargs):
        pass

    async def _modal(self, modal):
        self.modals.append(modal)


class TestPanelButtonsUseService(unittest.TestCase):
    def _view_and_service(self):
        player = FakeRealPlayer()
        player.current = make_track()
        player.queue = [make_track("next one"), make_track("after that")]
        guild = MusicFlowGuild(player=player)
        guild.player = player
        guild.voice_client = FakeVoiceProtocol(guild, guild.channels[99])
        config = Config(lavalink_host="n", lavalink_password="p")
        store = SettingsStore(os.path.join(tempfile.mkdtemp(), "settings.json"))
        service = MusicService(MusicFlowBot(guild), config, store)
        service.client = FakeSearchClient(player, {})
        session = service.session(guild.id)
        session.playback_history = [_history_entry(make_track("earlier"))]
        session.history_index = 0
        service._record_history(player, player.current)
        service._panel_messages.pop(guild.id, None)
        service.refresh_panel = AsyncMock()  # no Discord I/O in this test
        state = service.panel_state(guild.id)
        view = MusicPanelView(service, guild.id, button_states(state), state)
        return view, service, guild

    def _member(self, guild, **kwargs):
        return FakePanelMember(**kwargs)

    def test_previous_button_calls_service_previous(self):
        async def run():
            view, service, guild = self._view_and_service()
            with patch.object(service, "previous", AsyncMock(return_value=SimpleNamespace(
                track=TrackInfo.from_track(make_track("earlier")),
            ))) as previous:
                with patch.object(discord, "Member", FakePanelMember):
                    await view._previous(FakePanelInteraction(guild, self._member(guild)))
                previous.assert_awaited_once()
                called_guild, called_member = previous.await_args.args
                self.assertIs(called_guild, guild)
                self.assertEqual(called_member.id, 7)

        asyncio.run(run())

    def test_loop_and_autoplay_buttons_call_service(self):
        async def run():
            view, service, guild = self._view_and_service()
            interaction = FakePanelInteraction(guild, self._member(guild))
            member_patch = patch.object(discord, "Member", FakePanelMember)
            member_patch.start()
            self.addCleanup(member_patch.stop)
            with patch.object(service, "set_loop", AsyncMock(return_value="track")) as set_loop:
                await view._loop(interaction)
                set_loop.assert_awaited_once()
                self.assertIsNone(set_loop.await_args.args[2], "no mode = cycle")
            with patch.object(service, "set_autoplay", MagicMock(return_value=True)) as set_autoplay:
                await view._autoplay(interaction)
                set_autoplay.assert_called_once()
                self.assertIsNone(set_autoplay.call_args.args[2], "no state = toggle")

        asyncio.run(run())

    def test_queue_button_requires_same_channel(self):
        async def run():
            view, service, guild = self._view_and_service()
            outsider = FakePanelMember(channel_id=100)
            with patch.object(discord, "Member", FakePanelMember):
                interaction = FakePanelInteraction(guild, outsider)
                await view._queue(interaction)
            self.assertTrue(interaction.responses, "the permission error was surfaced")
            self.assertIn("join", str(interaction.responses[0]))

        asyncio.run(run())

    def test_effects_button_requires_same_channel(self):
        async def run():
            view, service, guild = self._view_and_service()
            outsider = FakePanelMember(channel_id=100)
            with patch.object(discord, "Member", FakePanelMember):
                interaction = FakePanelInteraction(guild, outsider)
                await view._effects(interaction)
            self.assertTrue(interaction.responses)
            self.assertIn("join", str(interaction.responses[0]))

        asyncio.run(run())


# ---------------------------------------------------------------------------
# TTS interruption must not corrupt history, loop, autoplay or the queue
# ---------------------------------------------------------------------------


class TestTTSCompatibility(unittest.TestCase):
    def _service(self, player, responses=None):
        guild = MusicFlowGuild(player=player)
        guild.player = player
        guild.voice_client = FakeVoiceProtocol(guild, guild.channels[99])
        config = Config(lavalink_host="n", lavalink_password="p")
        store = SettingsStore(os.path.join(tempfile.mkdtemp(), "settings.json"))
        service = MusicService(MusicFlowBot(guild), config, store)
        service.client = FakeSearchClient(player, responses or {})
        return service, guild

    def _member(self, guild):
        return MusicFlowMember(guild=guild)

    def test_tts_round_trip_preserves_music_state(self):
        async def run():
            player = FakeRealPlayer()
            tts_url = "https://cdn.example/clip.mp3"
            service, guild = self._service(
                player,
                {tts_url: FakeLoadResult(LoadType.TRACK, [make_track("voice clip", duration=2000)])},
            )
            member = self._member(guild)

            # Music playing: A current, B and C queued, queue loop on.
            track_a = make_track("song A")
            track_b = make_track("song B")
            track_c = make_track("song C")
            await player.play(track_a)
            service._record_history(player, track_a)
            player.queue = [track_b, track_c]
            await service.set_loop(guild, member, "queue")
            session = service.session(guild.id)
            session.autoplay_enabled = True

            # TTS interrupts: music stashed, queue handed over.
            await service.play_tts(guild.id, tts_url, "hello everyone")
            self.assertTrue(session.tts_active)
            self.assertEqual(session.tts_stash[0].title, "song A")
            self.assertEqual(player.queue, [])
            # The TTS clip must never enter playback history.
            service._record_history(player, player.current)
            self.assertEqual([e.track.title for e in session.playback_history], ["song A"])

            # TTS finishes: music resumes exactly where it was.
            await service.finish_tts(guild.id)
            self.assertFalse(session.tts_active)
            self.assertEqual(player.current.title, "song A")
            self.assertEqual([t.title for t in player.queue], ["song B", "song C"])
            self.assertEqual(session.loop_mode, "queue")
            self.assertEqual(player.loop, 2, "loop mode survived the interruption")
            self.assertTrue(session.autoplay_enabled)
            # The restored track start does not duplicate history.
            service._record_history(player, player.current)
            self.assertEqual([e.track.title for e in session.playback_history], ["song A"])
            self.assertEqual(session.history_index, 0)

        asyncio.run(run())

    def test_queue_end_during_tts_leaves_transition_to_tts_worker(self):
        async def run():
            player = FakeRealPlayer()
            tts_url = "https://cdn.example/clip.mp3"
            service, guild = self._service(
                player,
                {tts_url: FakeLoadResult(LoadType.TRACK, [make_track("voice clip", duration=2000)])},
            )
            last = make_track("song", "artist", identifier="abc")
            service._record_history(player, last)
            session = service.session(guild.id)
            session.autoplay_enabled = True
            await service.play_tts(guild.id, tts_url, "announcement")
            # The clip ends and the (empty) queue fires QueueEnd — the TTS
            # worker owns the next transition, autoplay must not hijack it.
            await service._on_queue_end(SimpleNamespace(player=player))
            self.assertEqual(player.queue, [])
            self.assertEqual(len(service.client.queries), 1, "no autoplay search ran")

        asyncio.run(run())

    def test_autoplay_disabled_while_speaking_via_maintenance(self):
        async def run():
            player = FakeRealPlayer()
            service, guild = self._service(player)
            session = service.session(guild.id)
            session.tts_active = True
            session.sticky = False
            session.alone_since = time.monotonic() - 10**6
            vc = guild.voice_client
            await service._maintenance_tick()
            self.assertEqual(vc.disconnected, 0, "never auto-disconnect mid-speech")

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Loop + autoplay
# ---------------------------------------------------------------------------


class TestLoopAndAutoplay(unittest.TestCase):
    def _service(self, player, responses=None, node_sources="unset"):
        guild = MusicFlowGuild(player=player)
        guild.player = player
        guild.voice_client = FakeVoiceProtocol(guild, guild.channels[99])
        config = Config(lavalink_host="n", lavalink_password="p")
        store = SettingsStore(os.path.join(tempfile.mkdtemp(), "settings.json"))
        service = MusicService(MusicFlowBot(guild), config, store)
        service.client = FakeSearchClient(player, responses or {})
        if node_sources != "unset":
            service.node_sources = node_sources
        return service, guild

    def _member(self, guild):
        return MusicFlowMember(guild=guild)

    def test_set_loop_modes_and_cycling(self):
        async def run():
            player = FakeRealPlayer()
            service, guild = self._service(player)
            member = self._member(guild)

            self.assertEqual(await service.set_loop(guild, member, "track"), "track")
            self.assertEqual(player.loop, 1)
            self.assertEqual(await service.set_loop(guild, member, "queue"), "queue")
            self.assertEqual(player.loop, 2)
            self.assertEqual(await service.set_loop(guild, member, "off"), "off")
            self.assertEqual(player.loop, 0)

            # Cycling: off → track → queue → off.
            await service.set_loop(guild, member, None)
            self.assertEqual(player.loop, 1)
            await service.set_loop(guild, member, None)
            self.assertEqual(player.loop, 2)
            await service.set_loop(guild, member, None)
            self.assertEqual(player.loop, 0)

            with self.assertRaisesRegex(MusicError, "off.*track.*queue"):
                await service.set_loop(guild, member, "sideways")

        asyncio.run(run())

    def test_skip_advances_even_under_track_loop(self):
        async def run():
            player = FakeRealPlayer()
            service, guild = self._service(player)
            member = self._member(guild)
            player.current = make_track("current")
            player.queue = [make_track("next")]
            await service.set_loop(guild, member, "track")
            await service.skip(guild, member)
            self.assertEqual(player.current.title, "next")
            self.assertEqual(player.loop, 1, "loop mode survives the skip")
            # The outgoing track must not have been re-inserted into the queue.
            self.assertEqual(len(player.queue), 0)

        asyncio.run(run())

    def test_autoplay_fills_queue_with_related_tracks(self):
        async def run():
            player = FakeRealPlayer()
            mix_url = "https://www.youtube.com/watch?v=abc&list=RDabc"
            related = [
                make_track("Related One", "Rick Astley", identifier="r1"),
                make_track("Related Two", "Rick Astley", identifier="r2"),
                make_track("Related Three", "Rick Astley", identifier="r3"),
                make_track("Related Four", "Rick Astley", identifier="r4"),
            ]
            service, guild = self._service(
                player,
                {mix_url: FakeLoadResult(LoadType.PLAYLIST, related)},
                node_sources=frozenset({"youtube", "http"}),
            )
            last = make_track("Never Gonna Give You Up", "Rick Astley", identifier="abc")
            session = service.session(guild.id)
            service._record_history(player, last)
            session.autoplay_enabled = True

            self.assertTrue(await service._maybe_autoplay(player, session))
            self.assertEqual(player.current.title, "Related One")
            self.assertEqual(
                len(player.queue), 2, "a small batch, not the whole mix (one already playing)"
            )
            self.assertTrue(all(call[0] != "stop" for call in player.calls))

        asyncio.run(run())

    def test_autoplay_filters_duplicates_and_recent_tracks(self):
        async def run():
            player = FakeRealPlayer()
            mix_url = "https://www.youtube.com/watch?v=abc&list=RDabc"
            candidates = [
                make_track("Never Gonna Give You Up", "Rick Astley", identifier="abc"),
                make_track("Never Gonna Give You Up (Official Video)", "Rick Astley", identifier="abc2"),
                make_track("Brand New Track", "Someone Else", identifier="fresh"),
            ]
            service, guild = self._service(
                player,
                {mix_url: FakeLoadResult(LoadType.PLAYLIST, candidates)},
                node_sources=frozenset({"youtube", "http"}),
            )
            last = make_track("Never Gonna Give You Up", "Rick Astley", identifier="abc")
            session = service.session(guild.id)
            service._record_history(player, last)
            session.autoplay_enabled = True

            self.assertTrue(await service._maybe_autoplay(player, session))
            self.assertEqual(player.current.title, "Brand New Track")
            self.assertEqual(player.queue, [], "the duplicates were filtered out")
            # What autoplay added is remembered so it is never re-added.
            self.assertTrue(
                any("brand new track" in key for key in session.autoplay_history)
            )

        asyncio.run(run())

    def test_autoplay_respects_recently_added_memory(self):
        async def run():
            player = FakeRealPlayer()
            service, guild = self._service(
                player,
                {
                    "ytsearch:Rick Astley": FakeLoadResult(
                        LoadType.SEARCH,
                        [make_track("Old Pick", "Rick Astley", identifier="old")],
                    )
                },
            )
            last = make_track("Never Gonna Give You Up", "Rick Astley", identifier="abc")
            session = service.session(guild.id)
            service._record_history(player, last)
            session.autoplay_enabled = True
            session.autoplay_history.append("rick astley old pick")
            session.autoplay_history.append("old")

            self.assertFalse(await service._maybe_autoplay(player, session))
            self.assertEqual(player.queue, [])

        asyncio.run(run())

    def test_autoplay_disabled_leaves_queue_end_idle(self):
        async def run():
            player = FakeRealPlayer()
            service, guild = self._service(player)
            last = make_track("song", "artist")
            session = service.session(guild.id)
            service._record_history(player, last)
            session.autoplay_enabled = False

            self.assertFalse(await service._maybe_autoplay(player, session))
            self.assertEqual(player.queue, [])

        asyncio.run(run())

    def test_queue_end_triggers_autoplay(self):
        async def run():
            player = FakeRealPlayer()
            mix_url = "https://www.youtube.com/watch?v=abc&list=RDabc"
            service, guild = self._service(
                player,
                {mix_url: FakeLoadResult(LoadType.PLAYLIST, [make_track("Related", "Artist", identifier="r")])},
                node_sources=frozenset({"youtube", "http"}),
            )
            last = make_track("song", "Artist", identifier="abc")
            session = service.session(guild.id)
            service._record_history(player, last)
            session.autoplay_enabled = True
            session.transition_track = TrackInfo.from_track(last)

            event = SimpleNamespace(player=player)
            await service._on_queue_end(event)
            self.assertEqual(player.current.title, "Related")
            self.assertIsNotNone(session.last_action)

        asyncio.run(run())

    def test_autoplay_gives_up_after_repeated_failures(self):
        async def run():
            player = FakeRealPlayer()
            service, guild = self._service(player, {})  # every search is empty
            last = make_track("song", "artist", identifier="abc")
            session = service.session(guild.id)
            service._record_history(player, last)
            session.autoplay_enabled = True

            for _ in range(3):
                session.autoplay_retry_at = 0.0  # skip the backoff in-test
                self.assertFalse(await service._maybe_autoplay(player, session))
            self.assertEqual(session.autoplay_failures, 3)
            # After three failures autoplay stays quiet until a human plays.
            self.assertFalse(await service._maybe_autoplay(player, session))
            self.assertEqual(player.queue, [])

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Queue editing: remove / move
# ---------------------------------------------------------------------------


class TestQueueEditing(unittest.TestCase):
    def _service(self, player):
        guild = MusicFlowGuild(player=player)
        guild.player = player
        guild.voice_client = FakeVoiceProtocol(guild, guild.channels[99])
        config = Config(lavalink_host="n", lavalink_password="p")
        store = SettingsStore(os.path.join(tempfile.mkdtemp(), "settings.json"))
        service = MusicService(MusicFlowBot(guild), config, store)
        service.client = FakeSearchClient(player, {})
        return service, guild

    def _member(self, guild):
        return MusicFlowMember(guild=guild)

    def test_remove_track(self):
        async def run():
            player = FakeRealPlayer()
            player.current = make_track("current")
            player.queue = [make_track(f"q{i}") for i in range(3)]
            service, guild = self._service(player)
            member = self._member(guild)

            removed = service.remove_track(guild, member, 2)
            self.assertEqual(removed.title, "q1")
            self.assertEqual([t.title for t in player.queue], ["q0", "q2"])

            with self.assertRaisesRegex(MusicError, "between 1 and 2"):
                service.remove_track(guild, member, 3)
            with self.assertRaisesRegex(MusicError, "whole number"):
                service.remove_track(guild, member, True)

        asyncio.run(run())

    def test_remove_from_empty_queue_is_friendly(self):
        async def run():
            player = FakeRealPlayer()
            player.current = make_track("current")
            service, guild = self._service(player)
            with self.assertRaisesRegex(MusicError, "queue is empty"):
                service.remove_track(guild, self._member(guild), 1)

        asyncio.run(run())

    def test_move_track(self):
        async def run():
            player = FakeRealPlayer()
            player.current = make_track("current")
            player.queue = [make_track(f"q{i}") for i in range(4)]
            service, guild = self._service(player)
            member = self._member(guild)

            moved = service.move_track(guild, member, 3, 1)
            self.assertEqual(moved.title, "q2")
            self.assertEqual([t.title for t in player.queue], ["q2", "q0", "q1", "q3"])

            moved = service.move_track(guild, member, 1, 4)
            self.assertEqual([t.title for t in player.queue], ["q0", "q1", "q3", "q2"])

            with self.assertRaisesRegex(MusicError, "between 1 and 4"):
                service.move_track(guild, member, 5, 1)
            with self.assertRaisesRegex(MusicError, "whole numbers"):
                service.move_track(guild, member, "one", 2)

        asyncio.run(run())

    def test_move_requires_queue(self):
        async def run():
            player = FakeRealPlayer()
            service, guild = self._service(player)
            with self.assertRaisesRegex(MusicError, "queue is empty"):
                service.move_track(guild, self._member(guild), 1, 1)

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Seeking
# ---------------------------------------------------------------------------


class TestSeeking(unittest.TestCase):
    def _service(self, player):
        guild = MusicFlowGuild(player=player)
        guild.player = player
        guild.voice_client = FakeVoiceProtocol(guild, guild.channels[99])
        config = Config(lavalink_host="n", lavalink_password="p")
        store = SettingsStore(os.path.join(tempfile.mkdtemp(), "settings.json"))
        service = MusicService(MusicFlowBot(guild), config, store)
        service.client = FakeSearchClient(player, {})
        return service, guild

    def _member(self, guild):
        return MusicFlowMember(guild=guild)

    def test_seek_absolute(self):
        async def run():
            player = FakeRealPlayer()
            player.current = make_track()
            service, guild = self._service(player)
            result = await service.seek(guild, self._member(guild), 92_000)
            self.assertEqual(result, 92_000)
            self.assertEqual(player.position, 92_000)

        asyncio.run(run())

    def test_seek_validation(self):
        async def run():
            player = FakeRealPlayer()
            player.current = make_track()  # 212s long
            service, guild = self._service(player)
            member = self._member(guild)
            with self.assertRaisesRegex(MusicError, "past the end"):
                await service.seek(guild, member, 300_000)
            with self.assertRaisesRegex(MusicError, "negative"):
                await service.seek(guild, member, -1)
            player.current = make_track(is_stream=True, is_seekable=False, duration=0)
            with self.assertRaisesRegex(MusicError, "can't be seeked"):
                await service.seek(guild, member, 1_000)
            player.current = None
            with self.assertRaisesRegex(MusicError, "nothing is playing"):
                await service.seek(guild, member, 1_000)

        asyncio.run(run())

    def test_forward_and_backward_clamp(self):
        async def run():
            player = FakeRealPlayer()
            player.current = make_track()  # 212s
            player.position = 72_000
            service, guild = self._service(player)
            member = self._member(guild)

            self.assertEqual(await service.nudge(guild, member, 15_000), 87_000)
            self.assertEqual(await service.nudge(guild, member, -30_000), 57_000)
            # Clamped at both ends.
            self.assertEqual(await service.nudge(guild, member, 500_000), 211_500)
            self.assertEqual(await service.nudge(guild, member, -500_000), 0)

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Audio effects (filters) — gated on what the node reports
# ---------------------------------------------------------------------------


class TestFilters(unittest.TestCase):
    def _service(self, player, node_filters="unset"):
        guild = MusicFlowGuild(player=player)
        guild.player = player
        guild.voice_client = FakeVoiceProtocol(guild, guild.channels[99])
        config = Config(lavalink_host="n", lavalink_password="p")
        store = SettingsStore(os.path.join(tempfile.mkdtemp(), "settings.json"))
        service = MusicService(MusicFlowBot(guild), config, store)
        service.client = FakeSearchClient(player, {})
        if node_filters != "unset":
            service.node_filters = node_filters
        return service, guild

    def _member(self, guild):
        return MusicFlowMember(guild=guild)

    def test_every_preset_maps_to_a_real_lavalink_filter(self):
        async def run():
            player = FakeRealPlayer()
            service, _ = self._service(player)
            known = {"equalizer", "timescale", "karaoke", "tremolo", "vibrato", "rotation"}
            for preset in service.filter_presets():
                self.assertIn(preset.node_filter, known, preset.name)
                self.assertTrue(preset.label)
            self.assertEqual(len(service.filter_presets()), 7)

        asyncio.run(run())

    def test_set_filter_toggles(self):
        async def run():
            player = FakeRealPlayer()
            service, guild = self._service(player)
            member = self._member(guild)

            self.assertEqual(await service.set_filter(guild, member, "bassboost"), "applied")
            self.assertIn("equalizer", player.filters)
            self.assertEqual(
                [p.label for p in service.active_filter_presets(guild.id)], ["Bass Boost"]
            )

            self.assertEqual(await service.set_filter(guild, member, "bassboost"), "removed")
            self.assertEqual(player.filters, {})
            self.assertEqual(service.active_filter_presets(guild.id), [])

        asyncio.run(run())

    def test_shared_node_filters_replace_each_other(self):
        async def run():
            player = FakeRealPlayer()
            service, guild = self._service(player)
            member = self._member(guild)
            await service.set_filter(guild, member, "nightcore")
            await service.set_filter(guild, member, "daycore")
            names = {preset.name for preset in service.active_filter_presets(guild.id)}
            self.assertEqual(names, {"daycore"})
            self.assertEqual(list(player.filters), ["timescale"])

        asyncio.run(run())

    def test_filters_gated_on_node_report(self):
        async def run():
            player = FakeRealPlayer()
            service, guild = self._service(player, node_filters=frozenset({"volume"}))
            member = self._member(guild)
            with self.assertRaisesRegex(MusicError, "doesn't support"):
                await service.set_filter(guild, member, "bassboost")
            # Unknown effect names never reach the node.
            with self.assertRaisesRegex(MusicError, "don't know that effect"):
                await service.set_filter(guild, member, "fuzzwah")
            # No report at all (older node): allow and let the node decide.
            service.node_filters = None
            self.assertEqual(await service.set_filter(guild, member, "rotation"), "applied")

        asyncio.run(run())

    def test_clear_filters(self):
        async def run():
            player = FakeRealPlayer()
            service, guild = self._service(player)
            member = self._member(guild)
            await service.set_filter(guild, member, "karaoke")
            await service.set_filter(guild, member, "tremolo")
            self.assertEqual(await service.clear_filters(guild, member), 2)
            self.assertEqual(player.filters, {})
            self.assertEqual(await service.clear_filters(guild, member), 0)

        asyncio.run(run())

    def test_panel_state_surfaces_filters(self):
        async def run():
            player = FakeRealPlayer()
            service, guild = self._service(player)
            member = self._member(guild)
            await service.set_filter(guild, member, "nightcore")
            guild.voice_client = FakeVoiceProtocol(guild, guild.channels[99])
            state = service.panel_state(guild.id)
            self.assertEqual(state.filters, ["Nightcore"])

        asyncio.run(run())

    def test_node_info_parses_filter_report(self):
        async def run():
            player = FakeRealPlayer()
            service, _ = self._service(player)
            info = dict(REAL_INFO)
            info["filters"] = ["volume", "equalizer", "timescale", "rotation"]
            await service._refresh_node_info(FakeInfoNode(info))
            self.assertEqual(
                service.node_filters,
                frozenset({"volume", "equalizer", "timescale", "rotation"}),
            )
            self.assertEqual(service.unsupported_filter_names(), [
                "Karaoke", "Tremolo", "Vibrato",
            ])
            # Dict-shaped report (defensive) is accepted too.
            await service._refresh_node_info(
                FakeInfoNode({"filters": {"equalizer": True, "karaoke": False}})
            )
            self.assertEqual(service.node_filters, frozenset({"equalizer"}))

        asyncio.run(run())


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
                "join", "play", "pause", "resume", "skip", "previous", "stop",
                "set_volume", "clear_queue", "shuffle_queue", "remove_track",
                "move_track", "seek", "nudge", "set_loop", "set_autoplay",
                "set_filter", "clear_filters", "filter_presets", "active_filter_presets",
                "lyrics_for_guild", "panel_state", "refresh_panel",
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
        payload = view.to_components()
        labels = set()

        def collect(items):
            for item in items:
                if "label" in item:
                    labels.add(item["label"])
                collect(item.get("components", []))

        collect(payload)
        for label in ("Previous", "Pause", "Next", "Stop", "Queue", "Volume", "Lyrics"):
            self.assertIn(label, labels)
        self.assertEqual(payload[0]["type"], 17, "panel must use a Components V2 container")
        self.assertTrue(view.is_persistent())
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

    def __init__(self, channel_id=99, guild_id=1):
        self.channel_id = channel_id
        self.guild_id = guild_id
        self.current = None
        self.queue = []
        self.paused = False
        self.volume = 100
        self.position = 0
        self.loop = 0
        self.filters = {}
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
        self.calls.append(("play", track, kwargs))
        if self.loop == 1 and self.current is not None:
            if track is not None:
                self.queue.insert(0, self.current)
            else:
                track = self.current
        elif self.loop == 2 and self.current is not None:
            self.queue.append(self.current)
        self.current = track or (self.queue.pop(0) if self.queue else None)
        self.paused = bool(kwargs.get("pause", False))

    async def stop(self):
        self.calls.append(("stop",))
        self.current = None

    async def skip(self):
        self.calls.append(("skip",))
        await self.play()

    def set_loop(self, loop):
        if not 0 <= loop <= 2:
            raise ValueError("Loop must be 0, 1 or 2.")
        self.loop = loop

    async def seek(self, position):
        self.calls.append(("seek", position))
        self.position = position

    async def set_pause(self, paused):
        self.paused = paused

    async def set_volume(self, volume):
        self.volume = volume

    async def set_filter(self, filter_object, *, replace=False):
        if replace:
            self.filters.clear()
        self.filters[type(filter_object).__name__.lower()] = filter_object

    async def set_filters(self, *filters, replace=False):
        if replace:
            self.filters.clear()
        for filter_object in filters:
            self.filters[type(filter_object).__name__.lower()] = filter_object

    async def clear_filters(self):
        self.filters.clear()


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


# ---------------------------------------------------------------------------
# Node info / plugin diagnostics (what /v4/info reports drives /play)
# ---------------------------------------------------------------------------

REAL_INFO = {
    "version": {"semver": "4.2.1", "major": 4, "minor": 2, "patch": 1},
    "sourceManagers": ["soundcloud", "bandcamp", "twitch", "vimeo", "youtube", "http"],
    "plugins": [{"name": "youtube-plugin", "version": "1.18.1"}],
}


class FakeInfoNode:
    def __init__(self, info=None, error=None):
        self._info = info
        self._error = error

    async def get_info(self):
        if self._error is not None:
            raise self._error
        return self._info


class TestNodeInfo(unittest.TestCase):
    def _service(self, expected_plugins=""):
        config = Config(
            lavalink_host="n", lavalink_password="p",
            lavalink_expected_plugins=expected_plugins,
        )
        store = SettingsStore(os.path.join(tempfile.mkdtemp(), "settings.json"))
        return MusicService(FakeBot(), config, store)

    def test_parses_real_node_info(self):
        async def run():
            service = self._service()
            await service._refresh_node_info(FakeInfoNode(REAL_INFO))
            self.assertEqual(service.node_plugins, {"youtube-plugin": "1.18.1"})
            self.assertEqual(
                service.node_sources,
                frozenset({"soundcloud", "bandcamp", "twitch", "vimeo", "youtube", "http"}),
            )
            self.assertEqual(service.node_version, "4.2.1")

        asyncio.run(run())

    def test_accepts_string_version_and_missing_sections(self):
        async def run():
            service = self._service()
            await service._refresh_node_info(
                FakeInfoNode({"version": "4.0.0", "sourceManagers": ["youtube"]})
            )
            self.assertEqual(service.node_version, "4.0.0")
            self.assertEqual(service.node_plugins, {})
            self.assertEqual(service.node_sources, frozenset({"youtube"}))
            await service._refresh_node_info(FakeInfoNode({}))
            self.assertIsNone(service.node_sources)  # empty list -> unknown, use defaults

        asyncio.run(run())

    def test_node_info_failure_keeps_defaults(self):
        async def run():
            service = self._service()
            await service._refresh_node_info(FakeInfoNode(error=RuntimeError("boom")))
            self.assertEqual(service.node_plugins, {})
            self.assertIsNone(service.node_sources)

        asyncio.run(run())

    def test_missing_plugins(self):
        async def run():
            service = self._service(
                "youtube-plugin:1.18.1,chanom-plugin:1.0.6"
            )
            await service._refresh_node_info(FakeInfoNode(REAL_INFO))
            # Loaded (any version) is not missing; only absent ones are.
            self.assertEqual(service.missing_plugins(), ["chanom-plugin 1.0.6"])
            service.node_plugins = {}
            self.assertEqual(
                service.missing_plugins(),
                ["youtube-plugin 1.18.1", "chanom-plugin 1.0.6"],
            )

        asyncio.run(run())

    def test_missing_plugins_without_versions(self):
        async def run():
            service = self._service("chanom-plugin")
            await service._refresh_node_info(FakeInfoNode(REAL_INFO))
            self.assertEqual(service.missing_plugins(), ["chanom-plugin"])

        asyncio.run(run())

    def test_plugins_label_is_compact_and_safe(self):
        async def run():
            service = self._service()
            await service._refresh_node_info(FakeInfoNode(REAL_INFO))
            self.assertEqual(service.plugins_label(), "youtube-plugin@1.18.1")
            # Snapshot hashes are truncated, never dumped whole.
            service.node_plugins = {
                "youtube-plugin": "f45bbb7aebfcbc1c553769e04af6cd43afa8b7c3"
            }
            label = service.plugins_label()
            self.assertEqual(label, "youtube-plugin@f45bbb7aebfc…")
            self.assertNotIn("f45bbb7aebfcbc1c553769e04af6cd43afa8b7c3", label)

        asyncio.run(run())

    def test_plugins_label_none_when_empty(self):
        self.assertIsNone(self._service().plugins_label())

    def test_search_prefixes_gated_by_node_sources(self):
        service = self._service()
        self.assertEqual(service._search_prefixes(), ("ytsearch", "scsearch"))  # unknown node
        service.node_sources = frozenset({"youtube", "soundcloud", "http"})
        self.assertEqual(service._search_prefixes(), ("ytsearch", "ytmsearch", "scsearch"))
        service.node_sources = frozenset({"soundcloud"})
        self.assertEqual(service._search_prefixes(), ("scsearch",))
        service.node_sources = frozenset({"http"})
        self.assertEqual(service._search_prefixes(), ())


# ---------------------------------------------------------------------------
# Search source fallback (ytsearch -> ytmsearch -> scsearch, one pass)
# ---------------------------------------------------------------------------


class FakeSearchClient:
    """get_tracks scripted per identifier, recording every query."""

    def __init__(self, player, responses):
        self.player = player
        self.responses = responses
        self.queries = []
        self.player_manager = SimpleNamespace(
            get=lambda gid: player,
            create=lambda guild_id=None, **kw: player,
        )
        self.node_manager = SimpleNamespace(available_nodes=[object()])

    async def get_tracks(self, identifier):
        self.queries.append(identifier)
        response = self.responses.get(identifier, FakeLoadResult(LoadType.EMPTY, []))
        if isinstance(response, Exception):
            raise response
        return response


class TestSearchFallback(unittest.TestCase):
    def _service(self, player, responses, node_sources="unset"):
        guild = MusicFlowGuild(player=player)
        guild.player = player
        config = Config(lavalink_host="n", lavalink_password="p")
        store = SettingsStore(os.path.join(tempfile.mkdtemp(), "settings.json"))
        service = MusicService(MusicFlowBot(guild), config, store)
        service.client = FakeSearchClient(player, responses)
        if node_sources != "unset":
            service.node_sources = node_sources
        return service, guild

    def _member(self, guild, **kwargs):
        return MusicFlowMember(guild=guild, **kwargs)

    def test_search_falls_back_to_soundcloud(self):
        async def run():
            player = FakeRealPlayer(channel_id=None)
            service, guild = self._service(
                player,
                {
                    "ytsearch:hello": FakeLoadResult(LoadType.EMPTY, []),
                    "scsearch:hello": FakeLoadResult(LoadType.SEARCH, [make_track()]),
                },
            )
            result = await service.play(guild, self._member(guild), "hello")
            self.assertTrue(result.started)
            self.assertEqual(
                service.client.queries, ["ytsearch:hello", "scsearch:hello"]
            )

        asyncio.run(run())

    def test_search_tries_ytmsearch_when_node_reports_youtube(self):
        async def run():
            player = FakeRealPlayer(channel_id=None)
            service, guild = self._service(
                player,
                {
                    "ytsearch:hello": FakeLoadResult(LoadType.EMPTY, []),
                    "ytmsearch:hello": FakeLoadResult(LoadType.EMPTY, []),
                    "scsearch:hello": FakeLoadResult(LoadType.SEARCH, [make_track()]),
                },
                node_sources=frozenset({"youtube", "soundcloud", "http"}),
            )
            await service.play(guild, self._member(guild), "hello")
            self.assertEqual(
                service.client.queries,
                ["ytsearch:hello", "ytmsearch:hello", "scsearch:hello"],
            )

        asyncio.run(run())

    def test_load_error_falls_through_to_next_source(self):
        async def run():
            player = FakeRealPlayer(channel_id=None)
            service, guild = self._service(
                player,
                {
                    "ytsearch:hello": FakeLoadResult(LoadType.ERROR, []),
                    "scsearch:hello": FakeLoadResult(LoadType.SEARCH, [make_track()]),
                },
            )
            result = await service.play(guild, self._member(guild), "hello")
            self.assertTrue(result.started)
            self.assertEqual(len(service.client.queries), 2)

        asyncio.run(run())

    def test_source_exception_falls_through_to_next_source(self):
        async def run():
            player = FakeRealPlayer(channel_id=None)
            service, guild = self._service(
                player,
                {
                    "ytsearch:hello": RuntimeError("node hiccup"),
                    "scsearch:hello": FakeLoadResult(LoadType.SEARCH, [make_track()]),
                },
            )
            result = await service.play(guild, self._member(guild), "hello")
            self.assertTrue(result.started)
            self.assertEqual(len(service.client.queries), 2)

        asyncio.run(run())

    def test_url_passes_through_without_source_fallback(self):
        async def run():
            player = FakeRealPlayer(channel_id=None)
            url = "https://youtu.be/dQw4w9WgXcQ"
            service, guild = self._service(
                player, {url: FakeLoadResult(LoadType.TRACK, [make_track()])},
                node_sources=frozenset(),  # even a source-less node gets URLs
            )
            result = await service.play(guild, self._member(guild), url)
            self.assertTrue(result.started)
            self.assertEqual(service.client.queries, [url])

        asyncio.run(run())

    def test_all_sources_empty_is_a_friendly_error(self):
        async def run():
            player = FakeRealPlayer(channel_id=None)
            service, guild = self._service(player, {})
            with self.assertRaisesRegex(MusicError, "couldn't find anything"):
                await service.play(guild, self._member(guild), "hello")
            self.assertEqual(
                service.client.queries, ["ytsearch:hello", "scsearch:hello"]
            )

        asyncio.run(run())

    def test_node_unreachable_is_a_friendly_error(self):
        async def run():
            player = FakeRealPlayer(channel_id=None)
            service, guild = self._service(
                player,
                {
                    "ytsearch:hello": RuntimeError("refused"),
                    "scsearch:hello": RuntimeError("refused"),
                },
            )
            with self.assertRaisesRegex(MusicError, "couldn't reach the music node"):
                await service.play(guild, self._member(guild), "hello")

        asyncio.run(run())

    def test_url_load_error_is_a_friendly_error(self):
        async def run():
            player = FakeRealPlayer(channel_id=None)
            url = "https://example.com/broken"
            service, guild = self._service(
                player, {url: FakeLoadResult(LoadType.ERROR, [])}
            )
            with self.assertRaisesRegex(MusicError, "didn't load"):
                await service.play(guild, self._member(guild), url)
            self.assertEqual(service.client.queries, [url])  # no fallback retries for URLs

        asyncio.run(run())

    def test_no_searchable_sources_is_a_friendly_error(self):
        async def run():
            player = FakeRealPlayer(channel_id=None)
            service, guild = self._service(
                player, {}, node_sources=frozenset({"http"})
            )
            with self.assertRaisesRegex(MusicError, "no searchable sources"):
                await service.play(guild, self._member(guild), "hello")
            self.assertEqual(service.client.queries, [])  # nothing was queried

        asyncio.run(run())

    def test_node_down_is_a_friendly_error(self):
        async def run():
            player = FakeRealPlayer(channel_id=None)
            service, guild = self._service(player, {})
            service.client.node_manager = SimpleNamespace(available_nodes=[])
            with self.assertRaisesRegex(MusicError, "music is unavailable"):
                await service.play(guild, self._member(guild), "hello")
            with self.assertRaisesRegex(MusicError, "music is unavailable"):
                await service.join(guild, self._member(guild))

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
