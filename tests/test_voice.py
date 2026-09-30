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
