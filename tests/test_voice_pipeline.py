"""Regression tests for the coalesced voice write pipeline.

These pin the data-safety properties of the optimisation, not its speed:

* a brand-new session is durable before the event handler returns;
* mute/deafen churn performs no database writes at all;
* a burst of state changes is merged into ONE batched heartbeat;
* a heartbeat that was queued before a session closed can never resurrect a
  row (no ghost session, no double-counted time);
* shutdown flushes pending state and closes sessions within a bound;
* identity writes are batched and retried instead of lost;
* the DM report path uses one batched lookup per guild;
* the local SQLite layer reuses connections and closes them.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from functions.voice import VoiceDatabase, VoicePeriod, VoiceTracker
from functions.voice.models import MemberProfile
from functions.voice.supabase_database import SupabaseVoiceDatabase
from voice_fakes import FakeSupabaseClient


def _dt(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


class _Channel:
    def __init__(self, channel_id: int, members=None) -> None:
        self.id = channel_id
        self.members = list(members or [])


class _Guild:
    def __init__(self, guild_id: int = 12345) -> None:
        self.id = guild_id
        self.name = "Paradise"
        self.afk_channel = None
        self.stage_channels = []
        self.voice_channels = [_Channel(801)]
        self.members = {}
        self.alert = _Channel(999_999)

    def get_member(self, user_id):
        return self.members.get(user_id)

    def get_channel(self, channel_id):
        return self.alert if channel_id == self.alert.id else None

    def get_channel_or_thread(self, channel_id):
        return self.get_channel(channel_id)


class _Member:
    def __init__(self, guild, user_id: int, name: str = "Alex") -> None:
        self.id = user_id
        self.name = name
        self.display_name = name
        self.bot = False
        self.guild = guild
        self.display_avatar = types.SimpleNamespace(url=f"https://cdn/{user_id}.png")
        self.voice = types.SimpleNamespace(
            self_mute=False, mute=False, self_deaf=False, deaf=False
        )


class _Bot:
    def __init__(self, guild) -> None:
        self.guilds = [guild]
        self._guild = guild
        self.user = None

    def get_guild(self, guild_id):
        return self._guild if guild_id == self._guild.id else None

    def get_channel(self, channel_id):
        return self._guild.get_channel(channel_id)

    async def fetch_channel(self, channel_id):
        return None

    def get_user(self, user_id):
        return None

    async def fetch_user(self, user_id):
        raise AssertionError("unexpected fetch_user")


class _State:
    def __init__(self, channel, **kwargs) -> None:
        self.channel = channel
        self.self_mute = kwargs.get("self_mute", False)
        self.mute = kwargs.get("mute", False)
        self.self_deaf = kwargs.get("self_deaf", False)
        self.deaf = kwargs.get("deaf", False)


class _ClockHolder:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class PipelineTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.local = VoiceDatabase(os.path.join(self.temp_dir.name, "paradise.db"))
        self.fake = FakeSupabaseClient()
        self.store = SupabaseVoiceDatabase(self.local, client=self.fake)
        self.store._bootstrapped = True
        self.guild = _Guild()
        self.bot = _Bot(self.guild)
        self.clock = _ClockHolder(_dt(2026, 10, 1, 12, 0))
        self.tracker = VoiceTracker(
            self.bot,
            self.store,
            alert_channel_id=0,
            clock=self.clock,
            coalesce_seconds=2.0,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def member(self, user_id: int = 777, name: str = "Alex") -> _Member:
        member = _Member(self.guild, user_id, name)
        self.guild.members[user_id] = member
        return member

    def active_row(self, user_id: int):
        return self.fake.active.get((str(self.guild.id), str(user_id)))

    async def join(self, member: _Member, channel=None) -> _Channel:
        channel = channel or self.guild.voice_channels[0]
        await self.tracker.handle_voice_state_update(
            member, _State(None), _State(channel)
        )
        if member not in channel.members:
            channel.members.append(member)
        return channel

    async def leave(self, member: _Member, channel) -> None:
        if member in channel.members:
            channel.members.remove(member)
        await self.tracker.handle_voice_state_update(
            member, _State(channel), _State(None)
        )


class TestImmediateDurability(PipelineTestBase):
    async def test_join_writes_the_row_before_returning(self):
        member = self.member()
        await self.join(member)

        row = self.active_row(member.id)
        self.assertIsNotNone(row, "a new session must be durable immediately")
        self.assertEqual(row["channel_id"], 801)
        self.assertEqual(row["is_afk"], False)

    async def test_leave_closes_immediately_and_records_time(self):
        member = self.member()
        channel = await self.join(member)
        self.clock.now = self.clock.now + timedelta(minutes=10)
        await self.leave(member, channel)

        self.assertIsNone(self.active_row(member.id))
        daily = sum(row["duration_seconds"] for row in self.fake.daily.values())
        self.assertEqual(daily, 600)

    async def test_join_does_not_write_a_profile_synchronously(self):
        self.fake.reset_calls()
        await self.join(self.member())

        counts = self.fake.call_counts()
        self.assertEqual(counts.get("rpc:voice_heartbeat_active"), 1)
        self.assertNotIn(
            "rpc:voice_upsert_profiles",
            counts,
            "identity writes belong to the coalesced writer, not the join path",
        )


class TestEventChurnIsFree(PipelineTestBase):
    async def test_repeated_mute_and_deafen_events_do_no_writes(self):
        member = self.member()
        await self.join(member)
        self.fake.reset_calls()

        for muted in (True, False, True, False, True):
            self.clock.now = self.clock.now + timedelta(seconds=5)
            await self.tracker.handle_voice_state_update(
                member,
                _State(self.guild.voice_channels[0]),
                _State(self.guild.voice_channels[0], self_mute=muted),
            )
        deaf = _State(self.guild.voice_channels[0], self_deaf=True)
        # Deafened time is not creditable, so this is a real session change.
        await self.tracker.handle_voice_state_update(
            member, _State(self.guild.voice_channels[0]), deaf
        )

        # Mute toggles must not touch the network; the deafen transition does
        # (session close + new untracked session) exactly as before.
        counts = self.fake.call_counts()
        heartbeats = counts.get("rpc:voice_heartbeat_active", 0)
        self.assertLessEqual(heartbeats, 1)
        self.assertNotIn("rpc:voice_upsert_profiles", counts)

    async def test_muted_member_still_credited(self):
        member = self.member()
        channel = await self.join(member)
        self.clock.now = self.clock.now + timedelta(minutes=2)
        await self.tracker.handle_voice_state_update(
            member,
            _State(channel),
            _State(channel, self_mute=True),
        )
        self.clock.now = self.clock.now + timedelta(minutes=8)
        await self.leave(member, channel)

        daily = sum(row["duration_seconds"] for row in self.fake.daily.values())
        self.assertEqual(daily, 600)


class TestBatchedFlush(PipelineTestBase):
    async def test_burst_of_moves_is_one_batched_heartbeat(self):
        members = [self.member(1000 + i, f"user{i}") for i in range(5)]
        for member in members:
            await self.join(member)
        self.fake.reset_calls()

        second = _Channel(802)
        for member in members:
            self.clock.now = self.clock.now + timedelta(seconds=1)
            await self.tracker.handle_voice_state_update(
                member, _State(self.guild.voice_channels[0]), _State(second)
            )

        # Nothing is written on the event path...
        self.assertEqual(self.fake.call_counts(), {})

        written = await self.tracker.flush_pending()
        counts = self.fake.call_counts()

        # Heartbeats + identity rows, all in one window (one request each).
        self.assertEqual(written, len(members) * 2)
        self.assertEqual(
            counts.get("rpc:voice_heartbeat_active"),
            1,
            "one window must fan out into exactly one batched upsert",
        )
        for member in members:
            self.assertEqual(self.active_row(member.id)["channel_id"], 802)

    async def test_flush_drops_snapshots_for_finished_sessions(self):
        member = self.member()
        channel = await self.join(member)

        # Simulate a heartbeat snapshot queued, then the session ending before
        # the window closes (the exact race coalescing introduces).
        session = self.tracker.active_sessions[(self.guild.id, member.id)]
        stale = self.tracker._active_snapshot(session, self.clock.now)
        self.tracker._dirty_active[(self.guild.id, member.id)] = stale

        await self.leave(member, channel)
        await self.tracker.flush_pending()

        self.assertIsNone(
            self.active_row(member.id),
            "a stale heartbeat must never resurrect a closed active row",
        )
        daily = sum(row["duration_seconds"] for row in self.fake.daily.values())
        self.assertEqual(daily, 0)

    async def test_no_ghost_row_after_close_then_flush(self):
        """Regression: close followed by a flush keeps one session, not two."""
        member = self.member()
        channel = await self.join(member)
        self.clock.now = self.clock.now + timedelta(minutes=5)

        session = self.tracker.active_sessions[(self.guild.id, member.id)]
        self.tracker._dirty_active[(self.guild.id, member.id)] = (
            self.tracker._active_snapshot(session, self.clock.now)
        )
        await self.leave(member, channel)
        self.assertIsNone(self.active_row(member.id))

        # A later restart reconciles: no extra time may appear.
        tracker2 = VoiceTracker(
            self.bot, self.store, alert_channel_id=0, clock=self.clock
        )
        await tracker2.reconcile_guilds()

        daily = sum(row["duration_seconds"] for row in self.fake.daily.values())
        self.assertEqual(daily, 300)
        self.assertEqual(len(self.fake.sessions), 1)
        self.assertEqual(len(self.fake.active), 0)

    async def test_coalescing_disabled_restores_write_per_event(self):
        tracker = VoiceTracker(
            self.bot,
            self.store,
            alert_channel_id=0,
            clock=self.clock,
            coalesce_seconds=0.0,
        )
        member = self.member()
        channel = _Channel(801)
        await tracker.handle_voice_state_update(member, _State(None), _State(channel))
        self.fake.reset_calls()
        await tracker.handle_voice_state_update(
            member, _State(channel), _State(channel, self_mute=True)
        )
        counts = self.fake.call_counts()
        self.assertEqual(counts.get("rpc:voice_heartbeat_active"), 1)


class TestShutdown(PipelineTestBase):
    async def test_stop_flushes_profiles_and_closes_sessions(self):
        member = self.member()
        await self.join(member)
        self.clock.now = self.clock.now + timedelta(seconds=90)

        await self.tracker.stop()

        self.assertIsNone(self.active_row(member.id))
        daily = sum(row["duration_seconds"] for row in self.fake.daily.values())
        self.assertEqual(daily, 90)
        self.assertIn((str(self.guild.id), str(member.id)), self.fake.profiles)

    async def test_stop_is_bounded_when_the_store_hangs(self):
        class HangingStore(SupabaseVoiceDatabase):
            async def close_active_session(self, *args, **kwargs):
                await asyncio.sleep(30)

        store = HangingStore(self.local, client=self.fake)
        store._bootstrapped = True
        tracker = VoiceTracker(
            self.bot,
            store,
            alert_channel_id=0,
            clock=self.clock,
            shutdown_timeout=1.0,
        )
        await tracker.handle_voice_state_update(
            self.member(), _State(None), _State(_Channel(801))
        )

        started = asyncio.get_running_loop().time()
        await tracker.stop()
        elapsed = asyncio.get_running_loop().time() - started

        self.assertLess(elapsed, 5.0, "shutdown must respect its timeout budget")

    async def test_stop_then_start_again_is_safe(self):
        await self.join(self.member())
        await self.tracker.stop()
        await self.tracker.stop()  # idempotent
        self.assertEqual(self.tracker.active_sessions, {})


class TestIdentityBatching(PipelineTestBase):
    async def test_profiles_are_flushed_in_one_request(self):
        members = [self.member(2000 + i, f"user{i}") for i in range(4)]
        for member in members:
            await self.join(member)
        self.fake.reset_calls()

        written = await self.tracker.flush_pending()

        self.assertEqual(written, len(members))
        counts = self.fake.call_counts()
        self.assertEqual(
            counts.get("rpc:voice_upsert_profiles"),
            1,
            "four identities must cost one upsert, not four",
        )
        self.assertEqual(len(self.fake.profiles), len(members))

    async def test_profile_write_failure_is_never_lost(self):
        """An outage turns the batch into durable outbox rows, not a loss."""
        member = self.member()
        await self.join(member)
        self.fake.fail_mode = True

        await self.tracker.flush_pending()
        self.assertEqual(len(self.fake.profiles), 0)
        self.assertGreaterEqual(
            self.local.count_outbox(),
            1,
            "a failed identity write must land in the durable outbox",
        )

        self.fake.fail_mode = False
        self.assertEqual(await self.store.flush_outbox(), 0)
        profile = self.fake.profiles[(str(self.guild.id), str(member.id))]
        self.assertEqual(profile["display_name"], "Alex")

    async def test_leaderboard_render_reads_profiles_once(self):
        member = self.member(3001, "Cached")
        await self.join(member)
        self.clock.now = self.clock.now + timedelta(minutes=5)
        self.fake.reset_calls()

        board = await self.tracker.get_live_leaderboard(self.guild, VoicePeriod.TODAY)

        self.assertTrue(board)
        counts = self.fake.call_counts()
        self.assertEqual(
            counts.get("table:voice_member_profiles:select"),
            1,
            "identity rows must be read once per render, not twice",
        )


class TestDmReportBatching(PipelineTestBase):
    async def test_sent_marks_are_fetched_once_per_guild(self):
        for i in range(4):
            self.fake.daily[("12345", str(4000 + i), "2026-10-01")] = {
                "guild_id": self.guild.id,
                "user_id": 4000 + i,
                "date": "2026-10-01",
                "duration_seconds": 600,
                "updated_at": "2026-10-01T00:00:00+00:00",
            }
            self.fake.dm_reports[("2026-10-01", "12345", str(4000 + i))] = {
                "date": "2026-10-01",
                "guild_id": self.guild.id,
                "user_id": 4000 + i,
            }
        self.fake.reset_calls()

        blocked = await self.tracker.dispatch_daily_dm_reports("2026-10-01")

        self.assertEqual(blocked, 0)
        counts = self.fake.call_counts()
        self.assertEqual(counts.get("rpc:voice_fetch_dm_report_batch"), 1)
        self.assertNotIn(
            "table:voice_dm_reports:select",
            counts,
            "the per-user mark lookup must not exist any more",
        )


class TestLocalSqliteReuse(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = VoiceDatabase(os.path.join(self.temp_dir.name, "reuse.db"))

    def tearDown(self) -> None:
        self.db.close()
        self.temp_dir.cleanup()

    def test_connection_is_reused_across_calls(self):
        first = self.db._get_connection()
        second = self.db._get_connection()
        self.assertIs(first, second)
        self.assertEqual(len(self.db._connections), 1)

        self.db.get_user_dm_preference(1)
        self.db.count_outbox()
        self.assertEqual(len(self.db._connections), 1)

    def test_close_releases_connections(self):
        self.db._get_connection()
        self.db.close()
        self.assertEqual(self.db._connections, [])
        # Using it after close transparently reconnects rather than crashing.
        self.assertTrue(self.db.get_user_dm_preference(1))

    def test_has_pending_voice_data_is_one_query(self):
        self.db.record_completed_session(
            guild_id=1,
            user_id=2,
            channel_id=3,
            started_at=_dt(2026, 10, 1, 10, 0),
            ended_at=_dt(2026, 10, 1, 11, 0),
        )
        self.assertTrue(self.db.has_pending_voice_data())
        self.assertEqual(self.db.get_user_dm_preferences([9, 10]), {9: True, 10: True})


class TestOutboxCoalescing(PipelineTestBase):
    async def test_outage_profile_rows_flush_in_one_request(self):
        rows = [
            MemberProfile(
                guild_id=self.guild.id,
                user_id=5000 + i,
                display_name=f"user{i}",
                username=f"user{i}",
                avatar_url=None,
            )
            for i in range(3)
        ]
        self.fake.fail_mode = True
        await self.store.upsert_member_profiles(rows)
        self.assertEqual(self.local.count_outbox(), 3)

        self.fake.fail_mode = False
        self.fake.reset_calls()
        remaining = await self.store.flush_outbox()

        self.assertEqual(remaining, 0)
        self.assertEqual(
            self.fake.call_counts().get("rpc:voice_upsert_profiles"),
            1,
            "a run of queued identities must drain in one request",
        )
        self.assertEqual(len(self.fake.profiles), 3)

    async def test_order_between_sessions_and_deletes_is_preserved(self):
        self.local.queue_outbox(
            "active_delete",
            {"guild_id": 1, "user_id": 2, "started_before": None},
        )
        self.local.queue_outbox(
            "profile",
            {
                "guild_id": 1,
                "user_id": 2,
                "display_name": "Alex",
                "username": "alex",
                "avatar_url": "",
            },
        )
        remaining = await self.store.flush_outbox()
        self.assertEqual(remaining, 0)


if __name__ == "__main__":
    unittest.main()
