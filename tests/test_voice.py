"""Unit tests for voice activity tracking, database, midnight splitting, and DM reporting."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord

from functions.voice import (
    ActiveVoiceSession,
    LeaderboardMember,
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
        t_start = datetime(2026, 9, 30, 23, 50, 0, tzinfo=timezone.utc)
        t_end = datetime(2026, 10, 1, 0, 20, 0, tzinfo=timezone.utc)

        splits = split_session_by_midnight(t_start, t_end)
        self.assertEqual(len(splits), 2)
        self.assertEqual(splits["2026-09-30"], 600)   # 10 minutes
        self.assertEqual(splits["2026-10-01"], 1200)  # 20 minutes

    def test_midnight_splitting_multiday(self):
        t_start = datetime(2026, 9, 29, 23, 0, 0, tzinfo=timezone.utc)
        t_end = datetime(2026, 10, 1, 2, 0, 0, tzinfo=timezone.utc)

        splits = split_session_by_midnight(t_start, t_end)
        self.assertEqual(len(splits), 3)
        self.assertEqual(splits["2026-09-29"], 3600)   # 1 hour
        self.assertEqual(splits["2026-09-30"], 86400)  # 24 hours
        self.assertEqual(splits["2026-10-01"], 7200)   # 2 hours

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

        # User 1: 23:50 on 2026-09-30 to 00:20 on 2026-10-01
        self.db.record_completed_session(
            guild_id=guild_id,
            user_id=user_1,
            channel_id=555,
            started_at=datetime(2026, 9, 30, 23, 50, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 10, 1, 0, 20, 0, tzinfo=timezone.utc),
        )

        # User 2: 14:00 to 16:00 on 2026-09-30 (7200s)
        self.db.record_completed_session(
            guild_id=guild_id,
            user_id=user_2,
            channel_id=555,
            started_at=datetime(2026, 9, 30, 14, 0, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 9, 30, 16, 0, 0, tzinfo=timezone.utc),
        )

        # Check leaderboard for 2026-09-30
        sep_30 = date(2026, 9, 30)
        board_30 = self.db.get_leaderboard_data(guild_id, "today", target_date=sep_30)
        self.assertEqual(len(board_30), 2)
        self.assertEqual(board_30[0].user_id, user_2)
        self.assertEqual(board_30[0].total_seconds, 7200)
        self.assertEqual(board_30[1].user_id, user_1)
        self.assertEqual(board_30[1].total_seconds, 600)

        # Check leaderboard for 2026-10-01
        oct_01 = date(2026, 10, 1)
        board_01 = self.db.get_leaderboard_data(guild_id, "today", target_date=oct_01)
        self.assertEqual(len(board_01), 1)
        self.assertEqual(board_01[0].user_id, user_1)
        self.assertEqual(board_01[0].total_seconds, 1200)

        # User breakdown
        breakdown_u1 = self.db.get_user_breakdown(guild_id, user_1, target_date=oct_01)
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
        self.mock_bot = MagicMock()
        self.tracker = VoiceTracker(self.mock_bot, self.db)

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
            tracker2 = VoiceTracker(self.mock_bot, self.db)

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
            tracker2 = VoiceTracker(self.mock_bot, self.db)
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

        # Verify DM was sent with expected report text
        mock_user.send.assert_called_once()
        sent_text = mock_user.send.call_args[0][0]
        self.assertIn("Your Paradise VC Report", sent_text)
        self.assertIn("September 30, 2026", sent_text)
        self.assertIn("3h 42m", sent_text)
        self.assertIn("Keep talking. Paradise was definitely not quiet today.", sent_text)

        # Verify marked as sent so no duplicate
        self.assertTrue(self.db.has_dm_report_sent(date_str, guild_id, user_id))


if __name__ == "__main__":
    unittest.main()
