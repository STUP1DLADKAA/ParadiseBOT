"""Unit tests for voice activity tracking, database, midnight splitting, and DM reporting."""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord

from functions.voice import (
    ActiveVoiceSession,
    LeaderboardMember,
    LocalVoiceStore,
    VoiceDatabase,
    VoicePeriod,
    VoiceTracker,
    build_leaderboard_text_embed,
    build_user_stats_embed,
    format_duration,
    format_duration_diff,
    split_session_by_midnight,
)


class TestVoiceDatabaseAndMidnightSplitting(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_paradise.db")
        self.db = VoiceDatabase(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_midnight_splitting_single_day(self):
        t_start = datetime(2026, 9, 30, 14, 0, 0, tzinfo=timezone.utc)
        t_end = datetime(2026, 9, 30, 15, 30, 0, tzinfo=timezone.utc)

        splits = split_session_by_midnight(t_start, t_end)
        self.assertEqual(len(splits), 1)
        self.assertEqual(splits["2026-09-30"], 5400)

    def test_midnight_splitting_spanning_midnight(self):
        # These instants straddle midnight in IST (not in UTC).
        t_start = datetime(2026, 10, 1, 18, 20, 0, tzinfo=timezone.utc)
        t_end = datetime(2026, 10, 1, 18, 50, 0, tzinfo=timezone.utc)

        splits = split_session_by_midnight(t_start, t_end)
        self.assertEqual(len(splits), 2)
        self.assertEqual(splits["2026-10-01"], 600)   # 10 minutes before IST midnight
        self.assertEqual(splits["2026-10-02"], 1200)  # 20 minutes after IST midnight

    def test_midnight_splitting_multiday(self):
        t_start = datetime(2026, 9, 29, 17, 0, 0, tzinfo=timezone.utc)  # 22:30 IST
        t_end = datetime(2026, 9, 30, 20, 0, 0, tzinfo=timezone.utc)   # Oct 1, 01:30 IST

        splits = split_session_by_midnight(t_start, t_end)
        self.assertEqual(len(splits), 3)
        self.assertEqual(splits["2026-09-29"], 5400)   # 1.5 hours
        self.assertEqual(splits["2026-09-30"], 86400)  # 24 hours
        self.assertEqual(splits["2026-10-01"], 5400)   # 1.5 hours

    def test_user_settings_timezone_and_dm(self):
        user_id = 111222333
        self.assertTrue(self.db.get_user_dm_preference(user_id))

        self.db.set_user_dm_preference(user_id, False)
        self.assertFalse(self.db.get_user_dm_preference(user_id))

        self.assertIsNone(self.db.get_user_timezone(user_id))
        self.db.set_user_timezone(user_id, "Asia/Kolkata")
        self.assertEqual(self.db.get_user_timezone(user_id), "Asia/Kolkata")

    def test_record_completed_session_and_aggregates(self):
        guild_id = 999
        user_1 = 101
        user_2 = 102

        # User 1: 23:50–00:20 IST across Oct 1/Oct 2.
        self.db.record_completed_session(
            guild_id=guild_id,
            user_id=user_1,
            channel_id=555,
            started_at=datetime(2026, 10, 1, 18, 20, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 10, 1, 18, 50, 0, tzinfo=timezone.utc),
        )

        # User 2: 14:00 to 16:00 on 2026-09-30 (7200s)
        self.db.record_completed_session(
            guild_id=guild_id,
            user_id=user_2,
            channel_id=555,
            started_at=datetime(2026, 9, 30, 14, 0, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 9, 30, 16, 0, 0, tzinfo=timezone.utc),
        )

        # Check the untouched Sep 30 record and both sides of the IST split.
        sep_30 = date(2026, 9, 30)
        board_30 = self.db.get_leaderboard_data(guild_id, "today", target_date=sep_30)
        self.assertEqual([(entry.user_id, entry.total_seconds) for entry in board_30], [(user_2, 7200)])

        oct_01 = date(2026, 10, 1)
        board_01 = self.db.get_leaderboard_data(guild_id, "today", target_date=oct_01)
        self.assertEqual([(entry.user_id, entry.total_seconds) for entry in board_01], [(user_1, 600)])
        oct_02 = date(2026, 10, 2)
        board_02 = self.db.get_leaderboard_data(guild_id, "today", target_date=oct_02)
        self.assertEqual([(entry.user_id, entry.total_seconds) for entry in board_02], [(user_1, 1200)])

        # User breakdown
        breakdown_u1 = self.db.get_user_breakdown(guild_id, user_1, target_date=oct_02)
        self.assertEqual(breakdown_u1["today"], 1200)
        self.assertEqual(breakdown_u1["yesterday"], 600)
        self.assertEqual(breakdown_u1["all_time"], 1800)


class TestMemberProfilePersistence(unittest.TestCase):
    """Names/avatars must be retained across restarts (no more 'User <id>')."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_paradise.db")
        self.db = VoiceDatabase(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_profiles_survive_database_reopen(self):
        self.db.upsert_member_profile(
            guild_id=1, user_id=28494379237,
            display_name="Alex", username="alex_games",
            avatar_url="https://cdn.discordapp.com/avatars/2/x.png",
        )
        # Simulate a restart: a brand new connection to the same file
        db2 = VoiceDatabase(self.db_path)
        profiles = db2.get_member_profiles(1, [28494379237])
        self.assertIn(28494379237, profiles)
        self.assertEqual(profiles[28494379237].display_name, "Alex")
        self.assertEqual(profiles[28494379237].username, "alex_games")
        self.assertEqual(
            profiles[28494379237].avatar_url,
            "https://cdn.discordapp.com/avatars/2/x.png",
        )

    def test_profile_update_keeps_avatar_when_missing(self):
        self.db.upsert_member_profile(1, 2, "Alex", "alex", "http://img/a.png")
        self.db.upsert_member_profile(1, 2, "Alex Renamed", "alex", None)
        profile = self.db.get_member_profiles(1)[2]
        self.assertEqual(profile.display_name, "Alex Renamed")
        self.assertEqual(profile.avatar_url, "http://img/a.png")


class TestVoiceFormatting(unittest.TestCase):
    def test_format_duration(self):
        self.assertEqual(format_duration(0), "0m")
        self.assertEqual(format_duration(45), "45s")
        self.assertEqual(format_duration(2520), "42m")
        self.assertEqual(format_duration(16320), "4h 32m")
        self.assertEqual(format_duration(86400), "24h")

    def test_format_duration_diff(self):
        self.assertEqual(format_duration_diff(2520), "+42m")
        self.assertEqual(format_duration_diff(-900), "-15m")
        self.assertEqual(format_duration_diff(10), "same as yesterday")
        self.assertEqual(format_duration_diff(-20), "same as yesterday")


class TestVoiceTracker(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_paradise.db")
        self.db = VoiceDatabase(self.db_path)
        self.store = LocalVoiceStore(self.db)
        self.mock_bot = MagicMock()
        self.tracker = VoiceTracker(self.mock_bot, self.store)

    def tearDown(self):
        self.temp_dir.cleanup()

    async def test_handle_voice_join_leave(self):
        mock_guild = MagicMock()
        mock_guild.id = 12345
        mock_guild.afk_channel = None

        mock_member = MagicMock()
        mock_member.id = 777
        mock_member.guild = mock_guild
        mock_member.bot = False

        channel_1 = MagicMock()
        channel_1.id = 888

        # Join event
        before_state = MagicMock(channel=None)
        after_state = MagicMock(channel=channel_1, self_mute=False, mute=False, self_deaf=False, deaf=False)
        await self.tracker.handle_voice_state_update(mock_member, before_state, after_state)

        key = (12345, 777)
        self.assertIn(key, self.tracker.active_sessions)
        self.assertEqual(self.tracker.active_sessions[key].channel_id, 888)

        # Leave event
        before_state_leave = MagicMock(channel=channel_1)
        after_state_leave = MagicMock(channel=None)
        await self.tracker.handle_voice_state_update(mock_member, before_state_leave, after_state_leave)

        self.assertNotIn(key, self.tracker.active_sessions)

    def _make_mock_member(self, guild, user_id, name="Alex"):
        member = MagicMock()
        member.id = user_id
        member.name = name
        member.display_name = name
        member.bot = False
        member.guild = guild
        member.display_avatar.url = f"https://cdn.discordapp.com/avatars/{user_id}/x.png"
        member.voice = MagicMock(self_mute=False, mute=False, self_deaf=False, deaf=False)
        return member

    def _voice_state(self, channel=None):
        return MagicMock(
            channel=channel, self_mute=False, mute=False, self_deaf=False, deaf=False
        )

    async def test_join_persists_and_leave_clears_active_session_row(self):
        mock_guild = MagicMock()
        mock_guild.id = 12345
        mock_guild.afk_channel = None
        member = self._make_mock_member(mock_guild, 777)
        channel = MagicMock()
        channel.id = 888

        t0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
        t1 = t0 + timedelta(minutes=5)

        with patch("functions.voice.tracker.datetime") as mock_dt:
            # Join -> persisted in-progress row exists (crash safety)
            mock_dt.now.return_value = t0
            await self.tracker.handle_voice_state_update(
                member, self._voice_state(None), self._voice_state(channel)
            )
            rows = self.db.get_active_sessions()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].guild_id, 12345)
            self.assertEqual(rows[0].user_id, 777)
            self.assertEqual(rows[0].channel_id, 888)

            # Leave -> row cleared and the completed session recorded
            mock_dt.now.return_value = t1
            await self.tracker.handle_voice_state_update(
                member, self._voice_state(channel), self._voice_state(None)
            )

        self.assertEqual(self.db.get_active_sessions(), [])
        board = self.db.get_leaderboard_data(12345, "today", date(2026, 10, 1))
        self.assertEqual(len(board), 1)
        self.assertEqual(board[0].user_id, 777)
        self.assertEqual(board[0].total_seconds, 300)

    async def test_restart_retains_ongoing_session_time(self):
        """A user still in VC across a restart keeps their original start time."""
        guild_id, user_id, channel_id = 555, 999, 888

        mock_guild = MagicMock()
        mock_guild.id = guild_id
        mock_guild.afk_channel = None
        member = self._make_mock_member(mock_guild, user_id)
        channel = MagicMock()
        channel.id = channel_id
        channel.members = [member]
        mock_guild.voice_channels = [channel]
        mock_guild.stage_channels = []
        mock_guild.get_member.return_value = member
        self.mock_bot.guilds = [mock_guild]
        self.mock_bot.get_guild = MagicMock(return_value=mock_guild)

        t0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
        t1 = t0 + timedelta(hours=2)

        with patch("functions.voice.tracker.datetime") as mock_dt:
            mock_dt.now.return_value = t0
            await self.tracker.handle_voice_state_update(
                member, self._voice_state(None), self._voice_state(channel)
            )

            # "Restart" — brand new tracker over the same database file
            tracker2 = VoiceTracker(self.mock_bot, LocalVoiceStore(self.db))

            # The user stays connected through the restart; 2 hours pass
            mock_dt.now.return_value = t1
            await tracker2.reconcile_guilds()

            session = tracker2.active_sessions[(guild_id, user_id)]
            self.assertEqual(
                session.started_at, t0,
                "Restart must resume the original start time, not restart the clock",
            )

            # Leaderboard counts the full 2 hours, including pre-restart time
            board = await tracker2.get_live_leaderboard(mock_guild, VoicePeriod.TODAY)
            entry = next(m for m in board if m.user_id == user_id)
            self.assertGreaterEqual(entry.total_seconds, 7200)
            self.assertEqual(entry.display_name, "Alex")

    async def test_crash_closes_session_at_last_heartbeat(self):
        """If the bot dies while a user is in VC, time up to the last heartbeat is kept."""
        guild_id, user_id, channel_id = 555, 999, 888

        mock_guild = MagicMock()
        mock_guild.id = guild_id
        mock_guild.afk_channel = None
        member = self._make_mock_member(mock_guild, user_id)
        channel = MagicMock()
        channel.id = channel_id
        channel.members = [member]
        mock_guild.voice_channels = [channel]
        mock_guild.stage_channels = []
        mock_guild.get_member.return_value = member
        self.mock_bot.guilds = [mock_guild]
        self.mock_bot.get_guild = MagicMock(return_value=mock_guild)

        t0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
        t_heartbeat = t0 + timedelta(minutes=10)
        t_restart = t0 + timedelta(minutes=30)

        with patch("functions.voice.tracker.datetime") as mock_dt:
            # Join at t0, heartbeat at t0+10m via a reconcile cycle
            mock_dt.now.return_value = t0
            await self.tracker.handle_voice_state_update(
                member, self._voice_state(None), self._voice_state(channel)
            )
            mock_dt.now.return_value = t_heartbeat
            await self.tracker.reconcile_guilds()

            # --- crash --- user leaves while the bot is down ---
            channel.members = []
            tracker2 = VoiceTracker(self.mock_bot, LocalVoiceStore(self.db))
            mock_dt.now.return_value = t_restart
            await tracker2.reconcile_guilds()

        # Session closed at the last heartbeat: exactly 10 minutes recorded
        board = self.db.get_leaderboard_data(guild_id, "today", date(2026, 10, 1))
        self.assertEqual(len(board), 1)
        self.assertEqual(board[0].total_seconds, 600)

        # Stale persisted row cleaned up; nothing left in memory
        self.assertEqual(self.db.get_active_sessions(), [])
        self.assertNotIn((guild_id, user_id), tracker2.active_sessions)

    async def test_leaderboard_uses_persisted_profile_for_uncached_member(self):
        """Cold member cache after restart -> name comes from the persisted profile."""
        guild_id, user_id = 777, 28494379237

        self.db.record_completed_session(
            guild_id=guild_id,
            user_id=user_id,
            channel_id=111,
            started_at=datetime(2026, 10, 1, 10, 0, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc),
        )
        self.db.upsert_member_profile(
            guild_id=guild_id, user_id=user_id,
            display_name="Alex", username="alex_games",
            avatar_url="https://cdn.discordapp.com/avatars/x/y.png",
        )

        mock_guild = MagicMock()
        mock_guild.id = guild_id
        mock_guild.get_member.return_value = None  # cold member cache
        mock_guild.fetch_member = AsyncMock()

        board = await self.tracker.get_live_leaderboard(
            mock_guild, VoicePeriod.ALL_TIME, date(2026, 10, 1)
        )
        self.assertEqual(len(board), 1)
        self.assertEqual(board[0].display_name, "Alex")
        self.assertEqual(board[0].username, "alex_games")
        self.assertEqual(board[0].avatar_url, "https://cdn.discordapp.com/avatars/x/y.png")
        self.assertNotEqual(board[0].display_name, f"User {user_id}")
        # Profile already persisted -> no HTTP fetch needed at all
        mock_guild.fetch_member.assert_not_awaited()

    async def test_leaderboard_hydrates_uncached_member_and_remembers(self):
        """Cold cache + no profile -> fetch once, persist, never show 'User <id>'."""
        guild_id, user_id = 777, 28494379237

        self.db.record_completed_session(
            guild_id=guild_id,
            user_id=user_id,
            channel_id=111,
            started_at=datetime(2026, 10, 1, 10, 0, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 10, 1, 11, 0, 0, tzinfo=timezone.utc),
        )

        mock_guild = MagicMock()
        mock_guild.id = guild_id
        mock_guild.get_member.return_value = None  # cold member cache
        fetched_member = self._make_mock_member(mock_guild, user_id, name="Alex")
        mock_guild.fetch_member = AsyncMock(return_value=fetched_member)

        board = await self.tracker.get_live_leaderboard(
            mock_guild, VoicePeriod.ALL_TIME, date(2026, 10, 1)
        )
        self.assertEqual(len(board), 1)
        self.assertEqual(board[0].display_name, "Alex")
        self.assertNotEqual(board[0].display_name, f"User {user_id}")
        mock_guild.fetch_member.assert_awaited_once_with(user_id)

        # Second render (e.g. after another restart): served from the
        # persisted profile, no further HTTP fetch needed
        board2 = await self.tracker.get_live_leaderboard(
            mock_guild, VoicePeriod.ALL_TIME, date(2026, 10, 1)
        )
        self.assertEqual(board2[0].display_name, "Alex")
        mock_guild.fetch_member.assert_awaited_once_with(user_id)  # still once

    async def test_dispatch_daily_dm_reports_includes_live_time_on_ist_day_boundary(self):
        guild_id, user_id = 555, 999
        now = datetime(2026, 10, 1, 18, 40, tzinfo=timezone.utc)  # Oct 2, 00:10 IST
        self.tracker._clock = lambda: now
        self.tracker.active_sessions[(guild_id, user_id)] = ActiveVoiceSession(
            guild_id=guild_id,
            user_id=user_id,
            channel_id=111,
            started_at=datetime(2026, 10, 1, 18, 20, tzinfo=timezone.utc),  # Oct 1, 23:50 IST
        )
        mock_guild = MagicMock()
        mock_guild.id = guild_id
        mock_guild.name = "Paradise Server"
        self.mock_bot.get_guild.return_value = mock_guild
        mock_user = MagicMock()
        mock_user.id = user_id
        mock_user.name = "Alex"
        mock_user.send = AsyncMock()
        self.mock_bot.get_user.return_value = mock_user

        await self.tracker.dispatch_daily_dm_reports("2026-10-01")

        mock_user.send.assert_awaited_once()
        embed = mock_user.send.call_args.kwargs["embed"]
        self.assertIn("10m", embed.description)
        self.assertIn("You spent **10m** in voice on", embed.description)
        self.assertTrue(self.db.has_dm_report_sent("2026-10-01", guild_id, user_id))

    async def test_dispatch_daily_dm_reports(self):
        guild_id = 555
        user_id = 999
        date_str = "2026-09-30"

        # Record voice activity for user
        self.db.record_completed_session(
            guild_id=guild_id,
            user_id=user_id,
            channel_id=111,
            started_at=datetime(2026, 9, 30, 10, 0, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 9, 30, 13, 42, 0, tzinfo=timezone.utc),
        )

        mock_guild = MagicMock()
        mock_guild.id = guild_id
        mock_guild.name = "Paradise Server"
        self.mock_bot.get_guild.return_value = mock_guild

        mock_user = MagicMock()
        mock_user.id = user_id
        mock_user.name = "Alex"
        mock_user.send = AsyncMock()
        self.mock_bot.get_user.return_value = mock_user

        await self.tracker.dispatch_daily_dm_reports(date_str)

        # Verify the DM reads like a report a person would send.
        mock_user.send.assert_called_once()
        embed = mock_user.send.call_args.kwargs["embed"]
        self.assertEqual(embed.title, "Wednesday, 30 September")
        self.assertIn("You spent **3h 42m** in voice on Wednesday.", embed.description)
        self.assertEqual(embed.footer.text, "Paradise Server")

        # Verify marked as sent so no duplicate
        self.assertTrue(self.db.has_dm_report_sent(date_str, guild_id, user_id))




# ---------------------------------------------------------------------------
# Root-cause regression tests: cold member cache & concurrent VCs
# ---------------------------------------------------------------------------


class _VCTestMember:
    """Minimal member stub for cold-cache reconciliation tests."""

    def __init__(self, guild, user_id, name="Alex", bot=False):
        self.id = user_id
        self.name = name
        self.display_name = name
        self.bot = bot
        self.guild = guild
        self.voice = types.SimpleNamespace(
            self_mute=False, mute=False, self_deaf=False, deaf=False
        )
        self.display_avatar = types.SimpleNamespace(
            url=f"https://cdn/{user_id}/x.png"
        )


class _VCTestVoiceState:
    """Minimal voice-state stub for guild.voice_states fallback tests."""

    def __init__(self, channel, self_mute=False, self_deaf=False, deaf=False, mute=False):
        self.channel = channel
        self.self_mute = self_mute
        self.mute = mute
        self.self_deaf = self_deaf
        self.deaf = deaf


class TestColdMemberCache(unittest.IsolatedAsyncioTestCase):
    """Reconciliation must find users in voice even when channel.members is
    empty (cold member cache after restart, large guild, etc.)."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_paradise.db")
        self.db = VoiceDatabase(self.db_path)
        self.store = LocalVoiceStore(self.db)
        self.mock_bot = MagicMock()
        self.tracker = VoiceTracker(self.mock_bot, self.store)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _make_guild(self, guild_id=12345):
        guild = MagicMock()
        guild.id = guild_id
        guild.name = "Cold Cache Server"
        guild.afk_channel = None
        channel = MagicMock()
        channel.id = 888
        channel.members = []  # cold cache: no members cached
        guild.voice_channels = [channel]
        guild.stage_channels = []
        guild.get_member.return_value = None  # member not cached
        self.mock_bot.guilds = [guild]
        self.mock_bot.get_guild = MagicMock(return_value=guild)
        return guild, channel

    async def test_reconcile_finds_user_via_voice_states_when_channel_members_empty(self):
        """A user in VC whose member isn't cached must still be tracked via guild.voice_states."""
        guild, channel = self._make_guild()
        user_id = 777

        # voice_states knows the user is in the channel even though channel.members is empty
        vs = _VCTestVoiceState(channel)
        guild.voice_states = {user_id: vs}

        t0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
        with patch("functions.voice.tracker.datetime") as mock_dt:
            mock_dt.now.return_value = t0
            await self.tracker.reconcile_guilds()

        key = (guild.id, user_id)
        self.assertIn(key, self.tracker.active_sessions)
        self.assertEqual(self.tracker.active_sessions[key].channel_id, 888)
        self.assertEqual(self.tracker.active_sessions[key].started_at, t0)

    async def test_reconcile_does_not_double_count_user_in_both_sources(self):
        """A user found via both channel.members and voice_states gets one session."""
        guild, channel = self._make_guild()
        user_id = 777

        member = _VCTestMember(guild, user_id)
        channel.members = [member]
        guild.get_member.return_value = member

        vs = _VCTestVoiceState(channel)
        guild.voice_states = {user_id: vs}

        t0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
        t1 = t0 + timedelta(minutes=5)
        with patch("functions.voice.tracker.datetime") as mock_dt:
            mock_dt.now.return_value = t0
            await self.tracker.reconcile_guilds()
            mock_dt.now.return_value = t1
            await self.tracker.reconcile_guilds()

        key = (guild.id, user_id)
        self.assertIn(key, self.tracker.active_sessions)
        self.assertEqual(self.tracker.active_sessions[key].channel_id, 888)
        self.assertEqual(self.tracker.active_sessions[key].started_at, t0)

    async def test_reconcile_handles_multiple_concurrent_vcs_independently(self):
        """Multiple users in separate voice channels are tracked independently."""
        guild = MagicMock()
        guild.id = 12345
        guild.name = "Multi VC Server"
        guild.afk_channel = None
        self.mock_bot.guilds = [guild]
        self.mock_bot.get_guild = MagicMock(return_value=guild)

        channels = []
        members = []
        for i in range(3):
            ch = MagicMock()
            ch.id = 1000 + i
            ch.members = []  # cold cache
            channels.append(ch)

        guild.voice_channels = channels
        guild.stage_channels = []

        t0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
        with patch("functions.voice.tracker.datetime") as mock_dt:
            mock_dt.now.return_value = t0
            for i, ch in enumerate(channels):
                user_id = 7000 + i
                vs = _VCTestVoiceState(ch)
                guild.voice_states = {**guild.voice_states, user_id: vs} if guild.voice_states else {user_id: vs}
            await self.tracker.reconcile_guilds()

        self.assertEqual(len(self.tracker.active_sessions), 3)
        for i, ch in enumerate(channels):
            key = (12345, 7000 + i)
            self.assertIn(key, self.tracker.active_sessions)
            self.assertEqual(self.tracker.active_sessions[key].channel_id, ch.id)

    async def test_reconcile_tracks_user_before_recovery_completes(self):
        """Users in voice are tracked even before _recovered is True."""
        guild, channel = self._make_guild()
        user_id = 777

        vs = _VCTestVoiceState(channel)
        guild.voice_states = {user_id: vs}

        # _recovered is False (recovery hasn't run)
        self.assertFalse(self.tracker._recovered)

        t0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
        with patch("functions.voice.tracker.datetime") as mock_dt:
            mock_dt.now.return_value = t0
            await self.tracker.reconcile_guilds()

        key = (guild.id, user_id)
        self.assertIn(key, self.tracker.active_sessions)
        self.assertEqual(self.tracker.active_sessions[key].started_at, t0)

    async def test_reconcile_channel_switch_updates_existing_session(self):
        """A user moving channels during reconcile keeps their session."""
        guild, channel_a = self._make_guild()
        channel_b = MagicMock()
        channel_b.id = 999
        channel_b.members = []
        guild.voice_channels = [channel_a, channel_b]
        user_id = 777

        vs_a = _VCTestVoiceState(channel_a)
        guild.voice_states = {user_id: vs_a}

        t0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
        t1 = t0 + timedelta(minutes=10)
        with patch("functions.voice.tracker.datetime") as mock_dt:
            mock_dt.now.return_value = t0
            await self.tracker.reconcile_guilds()
            key = (guild.id, user_id)
            self.assertEqual(self.tracker.active_sessions[key].channel_id, 888)

            # User switches channels between reconcile cycles
            vs_b = _VCTestVoiceState(channel_b)
            guild.voice_states = {user_id: vs_b}
            mock_dt.now.return_value = t1
            await self.tracker.reconcile_guilds()

        self.assertEqual(self.tracker.active_sessions[key].channel_id, 999)
        self.assertEqual(self.tracker.active_sessions[key].started_at, t0)


class TestMissingUserRootCause(unittest.IsolatedAsyncioTestCase):
    """The specific incident: users in VC whose members aren't cached were
    never found by reconciliation, so their persisted sessions were closed
    at last heartbeat and all subsequent time was lost."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_paradise.db")
        self.db = VoiceDatabase(self.db_path)
        self.store = LocalVoiceStore(self.db)
        self.mock_bot = MagicMock()
        self.tracker = VoiceTracker(self.mock_bot, self.store)

    def tearDown(self):
        self.temp_dir.cleanup()

    async def test_persisted_session_resumed_via_voice_states_after_restart(self):
        """A user whose member isn't cached but whose persisted row exists must
        have their session resumed from the original start time via voice_states."""
        guild = MagicMock()
        guild.id = 12345
        guild.name = "Restart Server"
        guild.afk_channel = None
        channel = MagicMock()
        channel.id = 888
        channel.members = []  # cold cache after restart
        guild.voice_channels = [channel]
        guild.stage_channels = []
        guild.get_member.return_value = None
        self.mock_bot.guilds = [guild]
        self.mock_bot.get_guild = MagicMock(return_value=guild)

        guild_id, user_id, channel_id = 12345, 777, 888
        t0 = datetime(2026, 10, 1, 10, 0, 0, tzinfo=timezone.utc)
        t_now = t0 + timedelta(hours=3)

        # Manually seed a persisted active session (as if from before a restart)
        self.db.upsert_active_session(
            guild_id, user_id, channel_id, t0, t0 + timedelta(minutes=30)
        )

        vs = _VCTestVoiceState(channel)
        guild.voice_states = {user_id: vs}

        with patch("functions.voice.tracker.datetime") as mock_dt:
            mock_dt.now.return_value = t_now
            await self.tracker.reconcile_guilds()

        key = (guild_id, user_id)
        self.assertIn(key, self.tracker.active_sessions)
        self.assertEqual(
            self.tracker.active_sessions[key].started_at,
            t0,
            "Recovery must restore the original start time, not restart the clock",
        )
        # The session should NOT be closed — the user is still in voice
        active_rows = self.db.get_active_sessions()
        self.assertEqual(len(active_rows), 1)
        self.assertEqual(active_rows[0].user_id, user_id)


if __name__ == "__main__":
    unittest.main()
