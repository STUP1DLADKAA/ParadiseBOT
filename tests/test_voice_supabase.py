"""Tests for the Supabase voice data layer (offline, against the fake client).

Covers: IST session recording + midnight splitting, retry idempotency,
calendar leaderboard reads for every period, manual
Dashboard edits being reflected immediately, heartbeats (frequency shape,
LWW, no history rows), atomic closes, the outbox fallback during outages,
outbox flush order, schema verification, DM report marks and restart
recovery through the real VoiceTracker.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from functions.voice import (
    VOICE_ALERT_CHANNEL_ID,
    VoiceDatabase,
    VoicePeriod,
    VoiceTracker,
)
from functions.voice.time_windows import IST
from functions.voice.models import PersistedActiveSession
from functions.voice.store import VoiceSchemaError, VoiceStoreUnavailable
from functions.voice.supabase_database import SupabaseVoiceDatabase
from voice_fakes import FakeNetworkError, FakeSupabaseClient


def _dt(*args):
    return datetime(*args, tzinfo=timezone.utc)


class SupabaseStoreTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.local = VoiceDatabase(os.path.join(self.temp_dir.name, "paradise.db"))
        self.fake = FakeSupabaseClient()
        self.store = SupabaseVoiceDatabase(self.local, client=self.fake)

    def tearDown(self):
        self.temp_dir.cleanup()

    async def flush(self):
        return await self.store.flush_outbox()


class TestSessionRecording(SupabaseStoreTestBase):
    async def test_record_session_splits_across_midnight(self):
        recorded = await self.store.record_completed_session(
            999, 101, 555,
            _dt(2026, 10, 1, 18, 20), _dt(2026, 10, 1, 18, 50),
        )
        self.assertEqual(recorded, 1800)
        daily = {
            row["date"]: row["duration_seconds"] for row in self.fake.daily.values()
        }
        self.assertEqual(daily.get("2026-10-01"), 600)
        self.assertEqual(daily.get("2026-10-02"), 1200)
        self.assertEqual(len(self.fake.sessions), 1)
        self.assertEqual(self.fake.sessions[list(self.fake.sessions)[0]]["duration_seconds"], 1800)

    async def test_duplicate_retry_never_double_counts(self):
        args = (999, 101, 555, _dt(2026, 10, 1, 18, 20), _dt(2026, 10, 1, 18, 50))
        first = await self.store.record_completed_session(*args)
        second = await self.store.record_completed_session(*args)
        self.assertEqual(first, 1800)
        self.assertEqual(second, 0)
        self.assertEqual(len(self.fake.sessions), 1)
        totals = sum(r["duration_seconds"] for r in self.fake.daily.values())
        self.assertEqual(totals, 1800)

    async def test_subsecond_session_ignored(self):
        recorded = await self.store.record_completed_session(
            1, 2, 3, _dt(2026, 10, 2, 10, 0, 0), _dt(2026, 10, 2, 10, 0, 0, 500000)
        )
        self.assertEqual(recorded, 0)
        self.assertEqual(len(self.fake.sessions), 0)

    async def test_manual_dashboard_edit_reflected_immediately(self):
        await self.store.record_completed_session(
            1, 7, 3, _dt(2026, 10, 8, 10, 0), _dt(2026, 10, 8, 11, 0)
        )
        # The owner edits voice_daily_stats.duration_seconds in the Dashboard:
        key = ("1", "7", "2026-10-08")
        self.fake.daily[key]["duration_seconds"] = 12345
        board = await self.store.get_leaderboard_data(1, "today", date(2026, 10, 8))
        self.assertEqual(board[0].total_seconds, 12345)
        self.assertEqual(board[0].user_id, 7)


class TestLeaderboardPeriods(SupabaseStoreTestBase):
    """Calendar week/month windows are anchored to Monday and month day one."""

    async def seed(self):
        seeds = {
            "2026-09-07": 11,  # outside every window
            "2026-09-30": 22,  # prior month
            "2026-10-01": 33,  # first of month, outside this week's Monday start
            "2026-10-02": 44,  # still in month, outside week
            "2026-10-05": 55,  # ISO Monday start — in
            "2026-10-08": 66,  # yesterday
            "2026-10-09": 77,  # today
            "2026-10-10": 88,  # future date — capped at target day
        }
        for day, seconds in seeds.items():
            self.fake.daily[("555", "501", day)] = {
                "guild_id": 555, "user_id": 501, "date": day,
                "duration_seconds": seconds, "updated_at": "2026-10-09T00:00:00+00:00",
            }
        # A second user with less time, and a zero-activity user.
        self.fake.daily[("555", "502", "2026-10-09")] = {
            "guild_id": 555, "user_id": 502, "date": "2026-10-09",
            "duration_seconds": 10, "updated_at": "2026-10-09T00:00:00+00:00",
        }
        self.fake.daily[("555", "503", "2026-10-09")] = {
            "guild_id": 555, "user_id": 503, "date": "2026-10-09",
            "duration_seconds": 0, "updated_at": "2026-10-09T00:00:00+00:00",
        }

    async def test_today(self):
        await self.seed()
        board = await self.store.get_leaderboard_data(555, "today", date(2026, 10, 9))
        self.assertEqual([(e.user_id, e.total_seconds) for e in board], [(501, 77), (502, 10)])
        self.assertEqual([e.rank for e in board], [1, 2])

    async def test_yesterday(self):
        await self.seed()
        board = await self.store.get_leaderboard_data(555, "yesterday", date(2026, 10, 9))
        self.assertEqual([(e.user_id, e.total_seconds) for e in board], [(501, 66)])

    async def test_week_starts_monday_and_excludes_future_days(self):
        await self.seed()
        board = await self.store.get_leaderboard_data(555, "week", date(2026, 10, 9))
        # Monday Oct 5 through Friday Oct 9; Oct 10 is neither current nor counted.
        self.assertEqual([(e.user_id, e.total_seconds) for e in board], [(501, 55 + 66 + 77), (502, 10)])

    async def test_month_starts_on_day_one_and_excludes_prior_month(self):
        await self.seed()
        board = await self.store.get_leaderboard_data(555, "month", date(2026, 10, 9))
        # Oct 1..Oct 9; Sep 30 is outside and Oct 10 is future.
        self.assertEqual(
            [(e.user_id, e.total_seconds) for e in board],
            [(501, 33 + 44 + 55 + 66 + 77), (502, 10)],
        )

    async def test_all_time(self):
        await self.seed()
        board = await self.store.get_leaderboard_data(555, "all_time", date(2026, 10, 9))
        self.assertEqual(board[0].total_seconds, 11 + 22 + 33 + 44 + 55 + 66 + 77 + 88)

    async def test_unknown_period_returns_empty(self):
        await self.seed()
        self.assertEqual(await self.store.get_leaderboard_data(555, "bogus", date(2026, 10, 9)), [])

    async def test_user_breakdown_matches_windows(self):
        await self.seed()
        breakdown = await self.store.get_user_breakdown(555, 501, date(2026, 10, 9))
        self.assertEqual(breakdown["today"], 77)
        self.assertEqual(breakdown["yesterday"], 66)
        self.assertEqual(breakdown["week"], 55 + 66 + 77)
        self.assertEqual(breakdown["month"], 33 + 44 + 55 + 66 + 77)
        self.assertEqual(breakdown["all_time"], 11 + 22 + 33 + 44 + 55 + 66 + 77 + 88)

    async def test_breakdown_unknown_user_zeroes(self):
        breakdown = await self.store.get_user_breakdown(555, 424242, date(2026, 10, 9))
        self.assertEqual(breakdown, {"today": 0, "yesterday": 0, "week": 0, "month": 0, "all_time": 0})

    async def test_daily_server_total_and_records(self):
        await self.seed()
        self.assertEqual(await self.store.get_daily_server_total(555, "2026-10-09"), 77 + 10)
        records = await self.store.get_daily_guild_user_records("2026-10-09")
        # zero-duration users are excluded
        self.assertEqual(sorted((r.user_id, r.duration_seconds) for r in records), [(501, 77), (502, 10)])


class TestActiveSessionsAndHeartbeats(SupabaseStoreTestBase):
    async def test_heartbeat_creates_single_row_no_history(self):
        await self.store.upsert_active_session(
            1, 10, 100, _dt(2026, 10, 5, 10, 0), _dt(2026, 10, 5, 10, 0, 30)
        )
        await self.store.heartbeat_active_sessions([
            PersistedActiveSession(
                guild_id=1, user_id=10, channel_id=100,
                started_at=_dt(2026, 10, 5, 10, 0), last_seen_at=_dt(2026, 10, 5, 10, 1), is_afk=False,
            )
        ])
        # Compact storage shape: ONE row per user, no heartbeat history, and
        # absolutely no completed voice_sessions rows from heartbeats.
        self.assertEqual(len(self.fake.active), 1)
        row = self.fake.active[("1", "10")]
        self.assertEqual(row["last_seen_at"], _dt(2026, 10, 5, 10, 1).isoformat())
        self.assertEqual(len(self.fake.sessions), 0)

    async def test_older_heartbeat_loses_lww(self):
        await self.store.upsert_active_session(
            1, 10, 100, _dt(2026, 10, 5, 10, 0), _dt(2026, 10, 5, 10, 20)
        )
        await self.store.upsert_active_session(
            1, 10, 100, _dt(2026, 10, 5, 10, 0), _dt(2026, 10, 5, 10, 5)
        )
        self.assertEqual(
            self.fake.active[("1", "10")]["last_seen_at"],
            _dt(2026, 10, 5, 10, 20).isoformat(),
        )

    async def test_close_is_atomic_and_uses_row_started_at(self):
        await self.store.upsert_active_session(
            1, 10, 100, _dt(2026, 10, 5, 10, 0), _dt(2026, 10, 5, 10, 30)
        )
        recorded = await self.store.close_active_session(1, 10, _dt(2026, 10, 5, 11, 0))
        self.assertEqual(recorded, 3600)
        self.assertEqual(len(self.fake.active), 0)  # row removed atomically
        self.assertEqual(len(self.fake.sessions), 1)
        session = self.fake.sessions[list(self.fake.sessions)[0]]
        self.assertEqual(session["started_at"], _dt(2026, 10, 5, 10, 0).isoformat())
        self.assertEqual(session["duration_seconds"], 3600)

    async def test_close_missing_row_returns_none(self):
        self.assertIsNone(await self.store.close_active_session(1, 10, _dt(2026, 10, 5, 11, 0)))

    async def test_close_afk_row_records_nothing(self):
        await self.store.upsert_active_session(
            1, 10, 100, _dt(2026, 10, 5, 10, 0), _dt(2026, 10, 5, 10, 30), is_afk=True
        )
        recorded = await self.store.close_active_session(1, 10, _dt(2026, 10, 5, 11, 0))
        self.assertEqual(recorded, 0)
        self.assertEqual(len(self.fake.active), 0)
        self.assertEqual(len(self.fake.sessions), 0)

    async def test_delete_guarded_by_started_before(self):
        # A newer session's row must survive an out-of-order delete retry.
        await self.store.upsert_active_session(
            1, 10, 100, _dt(2026, 10, 5, 12, 0), _dt(2026, 10, 5, 12, 10)
        )
        await self.store.delete_active_session(1, 10, started_before=_dt(2026, 10, 5, 11, 0))
        self.assertEqual(len(self.fake.active), 1)
        await self.store.delete_active_session(1, 10, started_before=_dt(2026, 10, 5, 12, 0))
        self.assertEqual(len(self.fake.active), 0)

    async def test_get_active_sessions_merges_remote_and_local(self):
        # Remote row fresher than the local mirror -> remote wins and vice versa.
        self.local.upsert_active_session(
            1, 11, 111, _dt(2026, 10, 5, 9, 0), _dt(2026, 10, 5, 9, 5)
        )
        await self.store.upsert_active_session(
            1, 10, 100, _dt(2026, 10, 5, 10, 0), _dt(2026, 10, 5, 10, 20)
        )
        self.store._bootstrapped = True
        sessions = await self.store.get_active_sessions()
        by_user = {s.user_id: s for s in sessions}
        self.assertEqual(by_user[10].last_seen_at, _dt(2026, 10, 5, 10, 20))
        self.assertEqual(by_user[11].last_seen_at, _dt(2026, 10, 5, 9, 5))

    async def test_get_active_sessions_local_only_before_bootstrap(self):
        self.local.upsert_active_session(
            1, 11, 111, _dt(2026, 10, 5, 9, 0), _dt(2026, 10, 5, 9, 5)
        )
        await self.store.upsert_active_session(
            1, 10, 100, _dt(2026, 10, 5, 10, 0), _dt(2026, 10, 5, 10, 20)
        )
        sessions = await self.store.get_active_sessions()
        self.assertEqual([s.user_id for s in sessions], [11])  # local mirror only


class TestOutageFallback(SupabaseStoreTestBase):
    async def test_failed_writes_queue_and_flush(self):
        self.fake.fail_mode = True
        await self.store.record_completed_session(
            1, 10, 100, _dt(2026, 10, 5, 10, 0), _dt(2026, 10, 5, 11, 0)
        )
        await self.store.upsert_member_profile(1, 10, "Alex", "alex", "http://img/a.png")
        await self.store.mark_dm_report_sent("2026-10-05", 1, 10)
        # Nothing reached Supabase; everything is durably queued.
        self.assertEqual(self.local.count_outbox(), 3)
        self.assertEqual(len(self.fake.sessions), 0)

        self.fake.fail_mode = False
        pending = await self.flush()
        self.assertEqual(pending, 0)
        self.assertEqual(self.local.count_outbox(), 0)
        self.assertEqual(len(self.fake.sessions), 1)
        self.assertEqual(self.fake.daily[("1", "10", "2026-10-05")]["duration_seconds"], 3600)
        self.assertEqual(self.fake.profiles[("1", "10")]["display_name"], "Alex")
        self.assertEqual(len(self.fake.dm_reports), 1)

    async def test_heartbeat_failure_uses_bounded_local_mirror(self):
        self.fake.fail_mode = True
        await self.store.upsert_active_session(
            1, 10, 100, _dt(2026, 10, 5, 10, 0), _dt(2026, 10, 5, 10, 5)
        )
        # One compact local row, NOT an outbox entry per heartbeat.
        self.assertEqual(self.local.count_outbox(), 0)
        local_rows = self.local.get_active_sessions()
        self.assertEqual(len(local_rows), 1)
        self.assertEqual(local_rows[0].started_at, _dt(2026, 10, 5, 10, 0))

        # Recovery works while Supabase is down (local mirrors only).
        sessions = await self.store.get_active_sessions()
        self.assertEqual(len(sessions), 1)

        # Once Supabase returns, a successful heartbeat uploads and drops
        # the redundant local mirror.
        self.fake.fail_mode = False
        await self.store.upsert_active_session(
            1, 10, 100, _dt(2026, 10, 5, 10, 0), _dt(2026, 10, 5, 10, 6)
        )
        self.assertEqual(self.local.get_active_sessions(), [])
        self.assertEqual(len(self.fake.active), 1)

    async def test_close_during_outage_queues_session_and_delete(self):
        await self.store.upsert_active_session(
            1, 10, 100, _dt(2026, 10, 5, 10, 0), _dt(2026, 10, 5, 10, 30)
        )
        self.fake.fail_mode = True
        result = await self.store.close_active_session(
            1, 10, _dt(2026, 10, 5, 11, 0),
            fallback_channel_id=100, fallback_started_at=_dt(2026, 10, 5, 10, 0),
        )
        self.assertIsNone(result)
        # Session + guarded delete queued in order; local mirror cleared.
        ops = self.local.fetch_outbox_batch(10)
        self.assertEqual([kind for _, kind, _ in ops], ["session", "active_delete"])
        self.assertEqual(self.local.get_active_sessions(), [])

        self.fake.fail_mode = False
        await self.flush()
        self.assertEqual(len(self.fake.sessions), 1)
        self.assertEqual(len(self.fake.active), 0)
        self.assertEqual(self.fake.daily[("1", "10", "2026-10-05")]["duration_seconds"], 3600)

    async def test_outbox_flush_preserves_order_and_is_idempotent(self):
        self.fake.fail_mode = True
        await self.store.record_completed_session(
            1, 10, 100, _dt(2026, 10, 5, 10, 0), _dt(2026, 10, 5, 11, 0)
        )
        await self.store.delete_active_session(1, 10, started_before=_dt(2026, 10, 5, 10, 0))
        self.fake.fail_mode = False
        # Simulate "commit succeeded but response was lost": apply once by
        # hand, then let the flush replay — replay must be harmless.
        await self.fake._run_rpc(
            "voice_record_session",
            {
                "p_guild_id": 1, "p_user_id": 10, "p_channel_id": 100,
                "p_started_at": _dt(2026, 10, 5, 10, 0).isoformat(),
                "p_ended_at": _dt(2026, 10, 5, 11, 0).isoformat(),
            },
        )
        await self.flush()
        self.assertEqual(len(self.fake.sessions), 1)  # not duplicated
        self.assertEqual(self.fake.daily[("1", "10", "2026-10-05")]["duration_seconds"], 3600)

    async def test_reads_raise_unavailable_during_outage(self):
        self.fake.fail_mode = True
        with self.assertRaises(VoiceStoreUnavailable):
            await self.store.get_leaderboard_data(1, "today")
        with self.assertRaises(VoiceStoreUnavailable):
            await self.store.has_dm_report_sent("2026-10-05", 1, 10)

    async def test_verify_schema_detects_missing_schema(self):
        self.fake.schema_installed = False
        with self.assertRaises(VoiceSchemaError) as ctx:
            await self.store.verify_schema()
        self.assertIn("002_voice_ist_calendar.sql", str(ctx.exception))

    async def test_verify_schema_detects_stale_version(self):
        self.fake.schema_version = 0
        with self.assertRaises(VoiceSchemaError):
            await self.store.verify_schema()

    async def test_verify_schema_ok(self):
        self.assertEqual(await self.store.verify_schema(), 2)


class TestProfilesAndDmReports(SupabaseStoreTestBase):
    async def test_profile_avatar_coalesce(self):
        await self.store.upsert_member_profile(1, 10, "Alex", "alex", "http://img/a.png")
        await self.store.upsert_member_profile(1, 10, "Alex R", "alex", None)
        profiles = await self.store.get_member_profiles(1, [10])
        self.assertEqual(profiles[10].display_name, "Alex R")
        self.assertEqual(profiles[10].avatar_url, "http://img/a.png")

    async def test_dm_report_mark_and_check(self):
        self.assertFalse(await self.store.has_dm_report_sent("2026-10-05", 1, 10))
        await self.store.mark_dm_report_sent("2026-10-05", 1, 10)
        await self.store.mark_dm_report_sent("2026-10-05", 1, 10)  # idempotent
        self.assertTrue(await self.store.has_dm_report_sent("2026-10-05", 1, 10))
        self.assertEqual(len(self.fake.dm_reports), 1)


def _make_mock_guild(guild_id=12345, afk_channel_id=None):
    guild = MagicMock()
    guild.id = guild_id
    guild.afk_channel = MagicMock() if afk_channel_id else None
    if guild.afk_channel:
        guild.afk_channel.id = afk_channel_id
    guild.voice_channels = []
    guild.stage_channels = []
    return guild


def _make_mock_member(guild, user_id, name="Alex"):
    member = MagicMock()
    member.id = user_id
    member.name = name
    member.display_name = name
    member.bot = False
    member.guild = guild
    member.display_avatar.url = f"https://cdn.discordapp.com/avatars/{user_id}/x.png"
    member.voice = MagicMock(self_mute=False, mute=False, self_deaf=False, deaf=False)
    return member


def _voice_state(channel=None):
    return MagicMock(channel=channel, self_mute=False, mute=False, self_deaf=False, deaf=False)


class TestTrackerOverSupabase(unittest.IsolatedAsyncioTestCase):
    """End-to-end tracker tests on the Supabase store (fake client)."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.local = VoiceDatabase(os.path.join(self.temp_dir.name, "paradise.db"))
        self.fake = FakeSupabaseClient()
        self.store = SupabaseVoiceDatabase(self.local, client=self.fake)
        self.store._bootstrapped = True
        self.mock_bot = MagicMock()
        self.tracker = VoiceTracker(self.mock_bot, self.store)

    def tearDown(self):
        self.temp_dir.cleanup()

    async def test_live_elapsed_added_to_stored_daily_time(self):
        guild = _make_mock_guild()
        guild.get_member.return_value = None
        guild.fetch_member = AsyncMock()

        # Stored committed time: 2h15m today.
        self.fake.daily[("12345", "777", "2026-10-01")] = {
            "guild_id": 12345, "user_id": 777, "date": "2026-10-01",
            "duration_seconds": 2 * 3600 + 15 * 60, "updated_at": "2026-10-01T00:00:00+00:00",
        }
        # Active session started 17m ago.
        t0 = _dt(2026, 10, 1, 12, 0)
        t1 = _dt(2026, 10, 1, 12, 17)
        member = _make_mock_member(guild, 777)
        channel = MagicMock()
        channel.id = 888
        with patch("functions.voice.tracker.datetime") as mock_dt:
            mock_dt.now.return_value = t0
            await self.tracker.handle_voice_state_update(
                member, _voice_state(None), _voice_state(channel)
            )
            mock_dt.now.return_value = t1
            board = await self.tracker.get_live_leaderboard(guild, VoicePeriod.TODAY)
            # Keep the same injected clock for yesterday's view; without an
            # explicit historical clock an active test session would extend
            # through the real current day.
            board_yesterday = await self.tracker.get_live_leaderboard(
                guild, VoicePeriod.YESTERDAY, target_date=date(2026, 10, 1)
            )
        entry = next(m for m in board if m.user_id == 777)
        self.assertEqual(entry.total_seconds, 2 * 3600 + 15 * 60 + 17 * 60)
        self.assertTrue(entry.is_active_now)
        self.assertEqual(board_yesterday, [])

    async def test_midnight_split_survives_full_tracker_flow(self):
        guild = _make_mock_guild()
        member = _make_mock_member(guild, 777)
        channel = MagicMock()
        channel.id = 888
        t_start = _dt(2026, 10, 1, 18, 20)
        t_end = _dt(2026, 10, 1, 18, 50)
        with patch("functions.voice.tracker.datetime") as mock_dt:
            mock_dt.now.return_value = t_start
            await self.tracker.handle_voice_state_update(
                member, _voice_state(None), _voice_state(channel)
            )
            mock_dt.now.return_value = t_end
            await self.tracker.handle_voice_state_update(
                member, _voice_state(channel), _voice_state(None)
            )
        daily = {row["date"]: row["duration_seconds"] for row in self.fake.daily.values()}
        self.assertEqual(daily.get("2026-10-01"), 600)
        self.assertEqual(daily.get("2026-10-02"), 1200)

    async def test_restart_recovery_resumes_original_start(self):
        guild = _make_mock_guild()
        member = _make_mock_member(guild, 777)
        channel = MagicMock()
        channel.id = 888
        channel.members = [member]
        guild.voice_channels = [channel]
        self.mock_bot.guilds = [guild]
        self.mock_bot.get_guild = MagicMock(return_value=guild)

        t0 = _dt(2026, 10, 1, 12, 0)
        t1 = _dt(2026, 10, 1, 14, 0)
        with patch("functions.voice.tracker.datetime") as mock_dt:
            mock_dt.now.return_value = t0
            await self.tracker.handle_voice_state_update(
                member, _voice_state(None), _voice_state(channel)
            )
            # "Restart": a fresh tracker over the same store.
            tracker2 = VoiceTracker(self.mock_bot, self.store)
            mock_dt.now.return_value = t1
            await tracker2.reconcile_guilds()
            session = tracker2.active_sessions[(12345, 777)]
            self.assertEqual(session.started_at, t0)
            board = await tracker2.get_live_leaderboard(guild, VoicePeriod.TODAY)
            entry = next(m for m in board if m.user_id == 777)
            self.assertGreaterEqual(entry.total_seconds, 7200)

    async def test_crash_recovery_closes_at_last_heartbeat(self):
        guild = _make_mock_guild()
        member = _make_mock_member(guild, 777)
        channel = MagicMock()
        channel.id = 888
        channel.members = [member]
        guild.voice_channels = [channel]
        self.mock_bot.guilds = [guild]
        self.mock_bot.get_guild = MagicMock(return_value=guild)

        t0 = _dt(2026, 10, 1, 12, 0)
        t_heartbeat = _dt(2026, 10, 1, 12, 10)
        t_restart = _dt(2026, 10, 1, 12, 30)
        with patch("functions.voice.tracker.datetime") as mock_dt:
            mock_dt.now.return_value = t0
            await self.tracker.handle_voice_state_update(
                member, _voice_state(None), _voice_state(channel)
            )
            mock_dt.now.return_value = t_heartbeat
            await self.tracker.reconcile_guilds()

            # Crash — the user leaves while the bot is down.
            channel.members = []
            tracker2 = VoiceTracker(self.mock_bot, self.store)
            mock_dt.now.return_value = t_restart
            await tracker2.reconcile_guilds()

        daily = {row["date"]: row["duration_seconds"] for row in self.fake.daily.values()}
        self.assertEqual(daily.get("2026-10-01"), 600)  # exactly one heartbeat interval
        self.assertEqual(len(self.fake.active), 0)
        self.assertEqual(len(self.fake.sessions), 1)

    async def test_deaf_time_excluded_except_exempt_user(self):
        guild = _make_mock_guild()
        alert_channel = MagicMock(id=VOICE_ALERT_CHANNEL_ID, guild=guild)
        alert_channel.send = AsyncMock()
        guild.get_channel.side_effect = (
            lambda cid: alert_channel if cid == VOICE_ALERT_CHANNEL_ID else None
        )
        guild.get_channel_or_thread.side_effect = guild.get_channel.side_effect
        self.mock_bot.get_guild.return_value = guild
        self.tracker.deaf_warning_seconds = 0.02

        channel = MagicMock(id=888)
        channel.send = AsyncMock()
        for user_id, expected in ((777, 1200), (922359954025885737, 1800)):
            member = _make_mock_member(guild, user_id)
            guild.get_member.side_effect = (
                lambda uid, m=member: m if uid == m.id else None
            )
            channel.send.reset_mock()
            alert_channel.send.reset_mock()
            with patch("functions.voice.tracker.datetime") as clock:
                # 00:00 join, 00:10 deafen -> wait >= threshold -> 00:20 undeafen -> 00:30 leave
                clock.now.return_value = _dt(2026, 10, 1, 12, 0)
                await self.tracker.handle_voice_state_update(
                    member, _voice_state(None), _voice_state(channel)
                )

                deaf_state = _voice_state(channel)
                deaf_state.self_deaf = True
                clock.now.return_value = _dt(2026, 10, 1, 12, 10)
                await self.tracker.handle_voice_state_update(
                    member, _voice_state(channel), deaf_state
                )
                # Never warns immediately in the VC chat itself
                channel.send.assert_not_awaited()
                await asyncio.sleep(0.05)

                clock.now.return_value = _dt(2026, 10, 1, 12, 20)
                await self.tracker.handle_voice_state_update(
                    member, deaf_state, _voice_state(channel)
                )

                clock.now.return_value = _dt(2026, 10, 1, 12, 30)
                await self.tracker.handle_voice_state_update(
                    member, _voice_state(channel), _voice_state(None)
                )

            total = sum(row["duration_seconds"] for row in self.fake.daily.values()
                        if row["user_id"] == user_id)
            self.assertEqual(total, expected)
            channel.send.assert_not_awaited()
            self.assertEqual(alert_channel.send.await_count, int(user_id == 777))

    async def test_short_deaf_under_30s_cancels_warning(self):
        guild = _make_mock_guild()
        alert_channel = MagicMock(id=VOICE_ALERT_CHANNEL_ID, guild=guild)
        alert_channel.send = AsyncMock()
        guild.get_channel_or_thread.side_effect = (
            lambda cid: alert_channel if cid == VOICE_ALERT_CHANNEL_ID else None
        )
        self.mock_bot.get_guild.return_value = guild
        self.tracker.deaf_warning_seconds = 0.08

        channel = MagicMock(id=888)
        member = _make_mock_member(guild, 777)
        guild.get_member.return_value = member

        deaf_state = _voice_state(channel)
        deaf_state.self_deaf = True
        await self.tracker.handle_voice_state_update(member, _voice_state(None), deaf_state)
        await asyncio.sleep(0.02)
        # Undeafen before 30s threshold -> cancels warning
        await self.tracker.handle_voice_state_update(member, deaf_state, _voice_state(channel))
        await asyncio.sleep(0.09)
        alert_channel.send.assert_not_awaited()

    async def test_muted_user_still_detected_and_counted_without_warning(self):
        guild = _make_mock_guild()
        alert_channel = MagicMock(id=VOICE_ALERT_CHANNEL_ID, guild=guild)
        alert_channel.send = AsyncMock()
        guild.get_channel_or_thread.side_effect = (
            lambda cid: alert_channel if cid == VOICE_ALERT_CHANNEL_ID else None
        )
        self.tracker.deaf_warning_seconds = 0.02

        channel = MagicMock(id=888)
        member = _make_mock_member(guild, 777)
        muted_state = _voice_state(channel)
        muted_state.self_mute = True

        with patch("functions.voice.tracker.datetime") as clock:
            clock.now.return_value = _dt(2026, 10, 1, 12, 0)
            await self.tracker.handle_voice_state_update(
                member, _voice_state(None), muted_state
            )
            session = self.tracker.active_sessions[(guild.id, 777)]
            self.assertTrue(session.is_muted)
            self.assertFalse(session.is_deaf)
            self.assertFalse(session.is_afk)
            await asyncio.sleep(0.05)

            clock.now.return_value = _dt(2026, 10, 1, 12, 30)
            await self.tracker.handle_voice_state_update(
                member, muted_state, _voice_state(None)
            )

        alert_channel.send.assert_not_awaited()
        total = sum(
            row["duration_seconds"]
            for row in self.fake.daily.values()
            if row["user_id"] == 777
        )
        self.assertEqual(total, 1800)

    async def test_join_8pm_leave_after_midnight_rollover_and_daily_channel_leaderboard(self):
        guild = _make_mock_guild()
        guild.name = "Paradise"
        alert_channel = MagicMock(id=VOICE_ALERT_CHANNEL_ID, guild=guild)
        alert_channel.send = AsyncMock()
        guild.get_channel_or_thread.side_effect = (
            lambda cid: alert_channel if cid == VOICE_ALERT_CHANNEL_ID else None
        )
        self.mock_bot.guilds = [guild]
        self.mock_bot.get_guild.return_value = guild

        channel = MagicMock(id=888)
        member = _make_mock_member(guild, 777, name="NightOwl")
        guild.get_member.side_effect = lambda uid: member if uid == 777 else None

        # User joins at 8:00 PM IST on Oct 25, 2026
        t_join = datetime(2026, 10, 25, 20, 0, 0, tzinfo=IST).astimezone(timezone.utc)
        # Clock hits 12:00:05 AM IST on Oct 26, 2026
        t_midnight = datetime(2026, 10, 26, 0, 0, 5, tzinfo=IST).astimezone(timezone.utc)
        # User leaves at 1:00 AM IST on Oct 26, 2026 (1 hour after 12:00 AM)
        t_leave = datetime(2026, 10, 26, 1, 0, 0, tzinfo=IST).astimezone(timezone.utc)

        current_now = t_join
        self.tracker._clock = lambda: current_now

        await self.tracker.handle_voice_state_update(
            member, _voice_state(None), _voice_state(channel)
        )

        # At 12:00 AM IST: rollover commits 8 PM–12 AM (4h) to Oct 25, and
        # publishes the daily leaderboard for Oct 25 in 1507796401122181352.
        current_now = t_midnight
        rolled = await self.tracker.rollover_midnight_sessions(t_midnight)
        self.assertEqual(rolled, 1)

        blocked = await self.tracker.dispatch_daily_channel_leaderboard("2026-10-25")
        self.assertEqual(blocked, 0)
        alert_channel.send.assert_awaited_once()
        sent_kwargs = alert_channel.send.call_args.kwargs
        self.assertIn("Daily Voice Leaderboard — October 25, 2026", sent_kwargs["embed"].title)
        self.assertIn("4h", sent_kwargs["embed"].footer.text)

        # Calling again for the same date is idempotent (no duplicate post).
        blocked_again = await self.tracker.dispatch_daily_channel_leaderboard("2026-10-25")
        self.assertEqual(blocked_again, 0)
        alert_channel.send.assert_awaited_once()

        # User leaves at 1:00 AM IST -> the 1 hour after 12:00 AM is counted in Oct 26!
        current_now = t_leave
        await self.tracker.handle_voice_state_update(
            member, _voice_state(channel), _voice_state(None)
        )

        daily_by_date = {
            row["date"]: row["duration_seconds"]
            for row in self.fake.daily.values()
            if row["user_id"] == 777
        }
        self.assertEqual(daily_by_date.get("2026-10-25"), 4 * 3600)  # 8 PM -> 12 AM = 4h
        self.assertEqual(daily_by_date.get("2026-10-26"), 1 * 3600)  # 12 AM -> 1 AM = 1h

        board_new_day = await self.tracker.get_live_leaderboard(
            guild, VoicePeriod.TODAY, target_date=date(2026, 10, 26)
        )
        self.assertEqual(len(board_new_day), 1)
        self.assertEqual(board_new_day[0].user_id, 777)
        self.assertEqual(board_new_day[0].total_seconds, 3600)

    async def test_join_8pm_leave_1pm_next_day_splits_post_midnight_into_new_day(self):
        guild = _make_mock_guild()
        channel = MagicMock(id=888)
        member = _make_mock_member(guild, 8881, name="Marathoner")

        t_join = datetime(2026, 10, 25, 20, 0, 0, tzinfo=IST).astimezone(timezone.utc)
        t_leave = datetime(2026, 10, 26, 13, 0, 0, tzinfo=IST).astimezone(timezone.utc)

        current_now = t_join
        self.tracker._clock = lambda: current_now
        await self.tracker.handle_voice_state_update(
            member, _voice_state(None), _voice_state(channel)
        )

        current_now = t_leave
        await self.tracker.handle_voice_state_update(
            member, _voice_state(channel), _voice_state(None)
        )

        daily_by_date = {
            row["date"]: row["duration_seconds"]
            for row in self.fake.daily.values()
            if row["user_id"] == 8881
        }
        self.assertEqual(daily_by_date.get("2026-10-25"), 4 * 3600)   # 8 PM -> 12 AM = 4h
        self.assertEqual(daily_by_date.get("2026-10-26"), 13 * 3600)  # 12 AM -> 1 PM = 13h

    async def test_afk_time_never_recorded(self):
        afk_channel = MagicMock()
        afk_channel.id = 999
        guild = _make_mock_guild(afk_channel_id=999)
        member = _make_mock_member(guild, 777)
        normal_channel = MagicMock()
        normal_channel.id = 888

        t0 = _dt(2026, 10, 1, 12, 0)
        t1 = _dt(2026, 10, 1, 12, 30)
        t2 = _dt(2026, 10, 1, 13, 0)
        with patch("functions.voice.tracker.datetime") as mock_dt:
            mock_dt.now.return_value = t0
            await self.tracker.handle_voice_state_update(
                member, _voice_state(None), _voice_state(normal_channel)
            )
            # Move into the AFK channel at 12:30 -> session closes.
            mock_dt.now.return_value = t1
            await self.tracker.handle_voice_state_update(
                member, _voice_state(normal_channel), _voice_state(afk_channel)
            )
            # Leave from AFK at 13:00 -> nothing more recorded.
            mock_dt.now.return_value = t2
            await self.tracker.handle_voice_state_update(
                member, _voice_state(afk_channel), _voice_state(None)
            )
        daily = {row["date"]: row["duration_seconds"] for row in self.fake.daily.values()}
        self.assertEqual(daily.get("2026-10-01"), 1800)
        self.assertEqual(len(self.fake.sessions), 1)

    async def test_stats_end_to_end(self):
        guild = _make_mock_guild()
        member = _make_mock_member(guild, 777)
        channel = MagicMock()
        channel.id = 888
        t0 = _dt(2026, 10, 1, 10, 0)
        t1 = _dt(2026, 10, 1, 11, 30)
        with patch("functions.voice.tracker.datetime") as mock_dt:
            mock_dt.now.return_value = t0
            await self.tracker.handle_voice_state_update(
                member, _voice_state(None), _voice_state(channel)
            )
            mock_dt.now.return_value = t1
            stats = await self.tracker.get_live_user_stats(guild, 777)
        self.assertEqual(stats["today_seconds"], 5400)
        self.assertEqual(stats["rank_today"], 1)
        self.assertTrue(stats["is_active_now"])
        self.assertEqual(stats["active_channel_id"], 888)
        self.assertEqual(stats["active_session_elapsed"], 5400)
        self.assertEqual(stats["yesterday_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
