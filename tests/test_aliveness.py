"""Unit tests for the aliveness engine (functions/aliveness.py).

Everything here runs on fakes: a configurable clock, a fixed IST "now" and
SimpleNamespace guilds. No bot, no gateway, no network.
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from commands.aliveness import Alive
from config import AlivenessConfig
from functions.aliveness import (
    AlivenessEngine,
    AlivenessSignals,
    DEAD_REASON,
    EVERYONE_IN_VC_REASON,
    format_duration,
)

IST = ZoneInfo("Asia/Kolkata")
LORD_ID = 922359954025885737
CHANNEL_ID = 1181574439893217280

#: A weekday afternoon — outside the default 01:00-07:00 quiet hours.
AWAKE_NOW = datetime(2026, 10, 30, 16, 30, tzinfo=IST)
#: 03:00 IST — inside them.
QUIET_NOW = datetime(2026, 10, 30, 3, 15, tzinfo=IST)


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_engine(
    *,
    clock: FakeClock | None = None,
    now: datetime = AWAKE_NOW,
    presences_enabled: bool = False,
    **config_overrides,
) -> AlivenessEngine:
    config = AlivenessConfig(**config_overrides)
    return AlivenessEngine(
        config,
        clock=clock or FakeClock(),
        now_fn=lambda: now,
        presences_enabled=presences_enabled,
    )


class FakeMember:
    """Hashable stand-in for discord.Member (Guild.voice_states keys)."""

    def __init__(self, member_id: int, name: str, *, bot: bool = False, status: str = "online"):
        self.id = member_id
        self.name = name
        self.display_name = name
        self.bot = bot
        self.status = status

    def __hash__(self):
        return hash(self.id)

    def __eq__(self, other):
        return isinstance(other, FakeMember) and other.id == self.id


def member(member_id: int, name: str, *, bot: bool = False, status: str = "online"):
    return FakeMember(member_id, name, bot=bot, status=status)


def voice_state(channel, *, joined_minutes_ago: float = 0.0, joined_at=None):
    from datetime import datetime as dt, timedelta, timezone as tz

    joined = joined_at
    if joined is None and joined_minutes_ago:
        joined = dt.now(tz.utc) - timedelta(minutes=joined_minutes_ago)
    return SimpleNamespace(channel=channel, joined_at=joined)


def make_guild(
    *,
    members=(),
    voice_states=None,
    member_count: int | None = None,
):
    return SimpleNamespace(
        id=1,
        members=list(members),
        voice_states=voice_states or {},
        member_count=member_count if member_count is not None else len(members),
        afk_channel_id=None,
    )


def note_chat(engine: AlivenessEngine, clock: FakeClock, *, authors=(1,), channels=(10,)):
    """Simulate humans talking: one message per author, per channel."""
    for channel_id in channels:
        for author_id in authors:
            author = member(author_id, f"User{author_id}")
            engine.note_message(
                SimpleNamespace(
                    author=author,
                    guild=SimpleNamespace(id=1),
                    channel=SimpleNamespace(id=channel_id),
                    content="hello there friends",
                )
            )
    clock.advance(1)


class TestNoteMessage(unittest.TestCase):
    def test_human_guild_messages_are_tracked(self):
        clock = FakeClock()
        engine = make_engine(clock=clock)
        note_chat(engine, clock, authors=(1, 2))
        signals = engine.snapshot(make_guild())
        self.assertEqual(signals.chatters_active, 2)
        self.assertLess(signals.chat_silent_seconds, 10)

    def test_bots_dms_and_empty_messages_are_ignored(self):
        clock = FakeClock()
        engine = make_engine(clock=clock)
        engine.note_message(
            SimpleNamespace(
                author=member(5, "Paradise", bot=True),
                guild=SimpleNamespace(id=1),
                channel=SimpleNamespace(id=10),
                content="beep",
            )
        )
        engine.note_message(
            SimpleNamespace(
                author=member(6, "DMer"),
                guild=None,
                channel=SimpleNamespace(id=10),
                content="hello",
            )
        )
        engine.note_message(
            SimpleNamespace(
                author=member(7, "Spacer"),
                guild=SimpleNamespace(id=1),
                channel=SimpleNamespace(id=10),
                content="   ",
            )
        )
        clock.advance(3600)
        signals = engine.snapshot(make_guild())
        self.assertEqual(signals.chatters_active, 0)


class TestSnapshot(unittest.TestCase):
    def test_voice_members_names_and_durations_are_collected(self):
        clock = FakeClock()
        engine = make_engine(clock=clock)
        guild = make_guild(
            members=[member(1, "Rohit"), member(2, "Aryan"), member(99, "Bot", bot=True)],
            voice_states={
                member(1, "Rohit"): voice_state(
                    SimpleNamespace(id=55, name="General"), joined_minutes_ago=43
                ),
                member(2, "Aryan"): voice_state(
                    SimpleNamespace(id=55, name="General"), joined_minutes_ago=5
                ),
                member(99, "Bot", bot=True): voice_state(
                    SimpleNamespace(id=55, name="General"), joined_minutes_ago=90
                ),
            },
            member_count=3,
        )
        signals = engine.snapshot(guild)
        self.assertEqual(signals.vc_humans, 2)  # the bot is not counted
        self.assertEqual(len(signals.vc_detail), 2)
        self.assertTrue(any("Rohit" in line and "43m" in line for line in signals.vc_detail))
        self.assertTrue(any("Aryan" in line for line in signals.vc_detail))
        self.assertEqual(signals.member_count, 3)

    def test_afk_channel_members_are_not_counted(self):
        engine = make_engine()
        guild = make_guild(
            members=[member(1, "Sleeper")],
            voice_states={
                member(1, "Sleeper"): voice_state(
                    SimpleNamespace(id=77, name="AFK"), joined_minutes_ago=60
                )
            },
        )
        guild.afk_channel_id = 77
        self.assertEqual(engine.snapshot(guild).vc_humans, 0)

    def test_everyone_in_vc_detection(self):
        clock = FakeClock()
        engine = make_engine(clock=clock)
        note_chat(engine, clock, authors=(1, 2))
        voice = {
            member(1, "User1"): voice_state(SimpleNamespace(id=55, name="General")),
            member(2, "User2"): voice_state(SimpleNamespace(id=55, name="General")),
        }
        guild = make_guild(members=[member(1, "User1"), member(2, "User2")], voice_states=voice)
        self.assertTrue(engine.snapshot(guild).everyone_in_vc)

        # Someone who chatted is NOT in the call -> not "everyone".
        voice.pop(member(2, "User2"))
        self.assertFalse(engine.snapshot(guild).everyone_in_vc)

    def test_online_count_requires_the_presence_intent(self):
        engine = make_engine(presences_enabled=False)
        guild = make_guild(members=[member(1, "A", status="online")])
        self.assertIsNone(engine.snapshot(guild).online_count)

        engine = make_engine(presences_enabled=True)
        guild = make_guild(
            members=[
                member(1, "A", status="online"),
                member(2, "B", status="idle"),
                member(3, "C", status="offline"),
                member(4, "Bot", bot=True, status="online"),
            ]
        )
        self.assertEqual(engine.snapshot(guild).online_count, 2)

    def test_chatters_outside_the_window_expire(self):
        clock = FakeClock()
        engine = make_engine(clock=clock, recent_window=30)
        note_chat(engine, clock, authors=(1,))
        clock.advance(31 * 60)
        signals = engine.snapshot(make_guild())
        self.assertEqual(signals.chatters_active, 0)


class TestDecision(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.engine = make_engine(clock=self.clock, dead_after=25, vc_quiet=10)

    def _signals(self, **overrides) -> AlivenessSignals:
        base = dict(
            ist_now=AWAKE_NOW,
            chat_silent_seconds=0.0,
            chatters_active=0,
            chatter_names=(),
            vc_humans=0,
            vc_detail=(),
            everyone_in_vc=False,
            member_count=20,
            online_count=None,
        )
        base.update(overrides)
        return AlivenessSignals(**base)

    def test_a_lively_server_stays_quiet(self):
        self.assertIsNone(self.engine.decision(self._signals(chat_silent_seconds=60)))

    def test_a_dead_server_is_called_out(self):
        self.assertEqual(
            self.engine.decision(self._signals(chat_silent_seconds=30 * 60)),
            DEAD_REASON,
        )

    def test_dead_chat_with_a_live_vc_still_gets_a_line(self):
        # A silent chat while people are in a call is worth commenting on too
        # (the prompt carries the VC data so the AI can use it).
        reason = self.engine.decision(
            self._signals(chat_silent_seconds=40 * 60, vc_humans=3)
        )
        self.assertEqual(reason, DEAD_REASON)

    def test_everyone_in_vc_after_a_lull(self):
        reason = self.engine.decision(
            self._signals(
                chat_silent_seconds=15 * 60,
                chatters_active=3,
                vc_humans=3,
                everyone_in_vc=True,
            )
        )
        self.assertEqual(reason, EVERYONE_IN_VC_REASON)

    def test_everyone_in_vc_needs_the_quiet_lull(self):
        self.assertIsNone(
            self.engine.decision(
                self._signals(
                    chat_silent_seconds=30,  # they're still typing too
                    chatters_active=3,
                    vc_humans=3,
                    everyone_in_vc=True,
                )
            )
        )

    def test_the_post_cooldown_is_respected(self):
        first = self.engine.decision(self._signals(chat_silent_seconds=30 * 60))
        self.assertEqual(first, DEAD_REASON)
        # Even 40 minutes later, the 45-minute cooldown holds.
        self.clock.advance(40 * 60)
        self.assertIsNone(self.engine.decision(self._signals(chat_silent_seconds=70 * 60)))
        self.clock.advance(6 * 60)
        self.assertEqual(
            self.engine.decision(self._signals(chat_silent_seconds=76 * 60)), DEAD_REASON
        )

    def test_quiet_hours_silence_everything(self):
        engine = make_engine(clock=self.clock)
        engine._now_fn = lambda: QUIET_NOW
        self.assertIsNone(
            engine.decision(
                AlivenessSignals(
                    ist_now=QUIET_NOW,
                    chat_silent_seconds=10 * 3600,
                    chatters_active=0,
                    chatter_names=(),
                    vc_humans=0,
                    vc_detail=(),
                    everyone_in_vc=False,
                    member_count=20,
                    online_count=None,
                )
            )
        )

    def test_quiet_hours_wrap_midnight_and_can_be_disabled(self):
        self.assertTrue(make_engine(quiet_hours=(23, 6)).in_quiet_hours(QUIET_NOW))
        self.assertFalse(make_engine(quiet_hours=(23, 6)).in_quiet_hours(AWAKE_NOW))
        self.assertFalse(make_engine(quiet_hours=(-1, -1)).in_quiet_hours(QUIET_NOW))

    def test_a_fresh_boot_is_not_dead_until_dead_after_elapses(self):
        # The engine just started; it cannot know how quiet it was before.
        signals = self.engine.snapshot(make_guild())
        self.assertLessEqual(signals.chat_silent_seconds, 1.0)
        self.assertIsNone(self.engine.decision(signals))
        self.clock.advance(26 * 60)
        signals = self.engine.snapshot(make_guild())
        self.assertEqual(self.engine.decision(signals), DEAD_REASON)

    def test_priming_from_history_prevents_a_false_dead_call(self):
        clock = FakeClock()
        engine = make_engine(clock=clock)
        clock.advance(30 * 60)  # uptime beyond dead_after…
        # …but the target channel's last message was 10 minutes ago.
        engine.prime(1, clock.now - 10 * 60)
        signals = engine.snapshot(make_guild())
        self.assertLess(signals.chat_silent_seconds, 25 * 60)
        self.assertIsNone(engine.decision(signals))


class TestPromptAndFallback(unittest.TestCase):
    def _signals(self, **overrides) -> AlivenessSignals:
        base = dict(
            ist_now=AWAKE_NOW,
            chat_silent_seconds=42 * 60,
            chatters_active=0,
            chatter_names=(),
            vc_humans=0,
            vc_detail=(),
            everyone_in_vc=False,
            member_count=20,
            online_count=None,
        )
        base.update(overrides)
        return AlivenessSignals(**base)

    def test_the_prompt_carries_the_real_numbers(self):
        engine = make_engine()
        prompt = engine.build_prompt(self._signals(member_count=20, online_count=7), DEAD_REASON)
        self.assertIn("42", prompt)  # minutes of silence
        self.assertIn("20", prompt)  # member count
        self.assertIn("7", prompt)  # online count
        self.assertIn("16:30", prompt)  # IST time
        self.assertIn("ONE short message", prompt)

    def test_the_prompt_names_the_voice_call(self):
        engine = make_engine()
        prompt = engine.build_prompt(
            self._signals(
                chatters_active=2,
                vc_humans=2,
                vc_detail=("Rohit — 43m in General", "Aryan — 12m in General"),
                everyone_in_vc=True,
            ),
            EVERYONE_IN_VC_REASON,
        )
        self.assertIn("Rohit", prompt)
        self.assertIn("Aryan", prompt)
        self.assertIn("voice call", prompt)

    def test_the_prompt_admits_it_cannot_see_statuses(self):
        engine = make_engine()
        prompt = engine.build_prompt(self._signals(), DEAD_REASON)
        self.assertIn("unknown", prompt)

    def test_fallback_dead_lines_are_filled_in(self):
        engine = make_engine()
        for _ in range(12):  # exercise every canned line
            line = engine.fallback_line(
                self._signals(chat_silent_seconds=42 * 60), DEAD_REASON
            )
            self.assertTrue(line.strip())
            self.assertNotIn("{minutes}", line)  # the placeholder was filled
        vc_line = engine.fallback_line(
            self._signals(everyone_in_vc=True), EVERYONE_IN_VC_REASON
        )
        self.assertTrue(vc_line)
        self.assertNotIn("{", vc_line)
        # Dead chat while people sit in a call: the canned line is about VC.
        dead_vc = engine.fallback_line(
            self._signals(vc_humans=3, vc_detail=("Rohit — 43m in General",)),
            reason=DEAD_REASON,
        )
        self.assertTrue(dead_vc)


class TestFormatDuration(unittest.TestCase):
    def test_durations_read_like_humans(self):
        self.assertEqual(format_duration(30), "just joined")
        self.assertEqual(format_duration(5 * 60), "5m")
        self.assertEqual(format_duration(43 * 60), "43m")
        self.assertEqual(format_duration(3600), "1h")
        self.assertEqual(format_duration(3600 + 25 * 60), "1h 25m")


class FakeAI:
    def __init__(self, *, available=True, line="custom ai line", error=None):
        self.available = available
        self.line = line
        self.error = error
        self.improvised = []

    async def improvise(self, prompt, *, max_tokens=200):
        self.improvised.append(prompt)
        if self.error is not None:
            raise self.error
        return self.line


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))


class TestAliveCog(unittest.IsolatedAsyncioTestCase):
    """commands/aliveness.py: the loop body, without a gateway."""

    def _bot(self, *, ai=None, guild=None, channel=None, **config_overrides):
        from commands.aliveness import Alive

        config = SimpleNamespace(aliveness=AlivenessConfig(**config_overrides))
        channel = channel or FakeChannel()
        bot = SimpleNamespace(
            config=config,
            ai=ai,
            guilds=[guild or make_guild()],
            get_channel=lambda channel_id: channel if channel_id == CHANNEL_ID else None,
            intents=SimpleNamespace(presences=False),
        )
        cog = Alive(bot)
        return cog, bot, channel

    async def test_a_dead_server_gets_an_ai_line(self):
        ai = FakeAI()
        cog, bot, channel = self._bot(ai=ai)
        cog.engine._started = cog.engine._clock() - 40 * 60  # long uptime
        await cog._check_guild(bot.guilds[0])
        self.assertEqual(len(channel.sent), 1)
        text, kwargs = channel.sent[0]
        self.assertEqual(text, "custom ai line")
        self.assertFalse(kwargs.get("allowed_mentions").everyone)
        self.assertEqual(len(ai.improvised), 1)
        self.assertIn("dead", ai.improvised[0].lower())

    async def test_the_fallback_line_when_the_ai_is_down(self):
        cog, bot, channel = self._bot(ai=FakeAI(available=False))
        cog.engine._started = cog.engine._clock() - 40 * 60
        await cog._check_guild(bot.guilds[0])
        self.assertEqual(len(channel.sent), 1)
        self.assertTrue(channel.sent[0][0].strip())

    async def test_an_ai_line_that_tries_to_mention_someone_is_replaced(self):
        cog, bot, channel = self._bot(ai=FakeAI(line="hey <@922359954025885737> wake up"))
        cog.engine._started = cog.engine._clock() - 40 * 60
        await cog._check_guild(bot.guilds[0])
        self.assertEqual(len(channel.sent), 1)
        self.assertNotIn("<@", channel.sent[0][0])

    async def test_a_lively_server_gets_nothing(self):
        ai = FakeAI()
        cog, bot, channel = self._bot(ai=ai)
        # Someone chatted a minute ago.
        clock = cog.engine._clock
        engine = cog.engine
        author = member(1, "User1")
        engine.note_message(
            SimpleNamespace(
                author=author,
                guild=SimpleNamespace(id=1),
                channel=SimpleNamespace(id=10),
                content="hello friends",
            )
        )
        await cog._check_guild(bot.guilds[0])
        self.assertEqual(channel.sent, [])
        self.assertEqual(ai.improvised, [])

    async def test_a_missing_target_channel_posts_nothing(self):
        ai = FakeAI()
        cog, bot, channel = self._bot(ai=ai)
        cog.engine._started = cog.engine._clock() - 40 * 60
        bot.get_channel = lambda channel_id: None
        await cog._check_guild(bot.guilds[0])
        self.assertEqual(channel.sent, [])

    async def test_on_message_feeds_the_engine(self):
        cog, bot, _ = self._bot()
        before = len(cog.engine._guilds)
        await cog._track_human_chat(
            SimpleNamespace(
                author=member(9, "Human"),
                guild=SimpleNamespace(id=1),
                channel=SimpleNamespace(id=10),
                content="hello",
            )
        )
        self.assertEqual(len(cog.engine._guilds), before + 1)


class TestConfig(unittest.TestCase):
    def test_defaults_match_the_server(self):
        config = AlivenessConfig.from_env({})
        self.assertTrue(config.enabled)
        self.assertEqual(config.channel_id, CHANNEL_ID)
        self.assertEqual(config.dead_after, 25)
        self.assertEqual(config.quiet_hours, (1, 7))

    def test_env_overrides_parse(self):
        config = AlivenessConfig.from_env(
            {
                "ALIVENESS_ENABLED": "false",
                "ALIVENESS_CHANNEL_ID": "555",
                "ALIVENESS_DEAD_AFTER_MINUTES": "40",
                "ALIVENESS_QUIET_HOURS": "23-6",
                "ALIVENESS_QUIET_HOURS_OFF": "1",
            }
        )
        self.assertFalse(config.enabled)
        self.assertEqual(config.channel_id, 555)
        self.assertEqual(config.dead_after, 40)
        self.assertEqual(config.quiet_hours, (23, 6))

    def test_lord_id_comes_from_the_main_config(self):
        from config import Config

        config = Config.from_env({})
        self.assertEqual(config.lord_user_id, LORD_ID)
        self.assertEqual(
            Config.from_env({"PARADISE_LORD_USER_ID": "42"}).lord_user_id, 42
        )


if __name__ == "__main__":
    unittest.main()
