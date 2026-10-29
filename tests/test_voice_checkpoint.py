"""Regression tests for the voice accounting incident.

Every test here corresponds to a failure mode that was observed or hypothesised
in the incident. They run the REAL ``VoiceTracker`` against the REAL
``SupabaseVoiceDatabase``, talking to the in-memory fake client that emulates
the PostgreSQL functions from ``supabase/migrations/``.

The invariant under test throughout:

    credited time for a user who stays connected, never deafens and never
    leaves must never decrease, must never be credited twice, and must survive
    restarts, reconnects, database outages and administrative edits.

The SQL semantics themselves (atomicity, idempotency, monotonicity) are proven
separately against real PostgreSQL by ``supabase/validate_locally.mjs``.
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from functions.voice import VoiceDatabase, VoicePeriod, VoiceTracker
from functions.voice.supabase_database import SupabaseVoiceDatabase, _parse_dt
from functions.voice.time_windows import IST
from voice_fakes import FakeSupabaseClient

GUILD_ID = 12345
USER_ID = 10001
CHANNEL_A = 801
CHANNEL_B = 802


def ist(*args) -> datetime:
    """An instant expressed in the leaderboard's authoritative timezone."""
    return datetime(*args, tzinfo=IST)


class _Channel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.members: list = []
        self.guild = None


class _Guild:
    def __init__(self, guild_id: int = GUILD_ID, afk_channel_id: int | None = None) -> None:
        self.id = guild_id
        self.name = f"Paradise {guild_id}"
        self.stage_channels: list = []
        self.voice_channels = [_Channel(CHANNEL_A), _Channel(CHANNEL_B)]
        for channel in self.voice_channels:
            channel.guild = self
        self.afk_channel = None
        if afk_channel_id is not None:
            self.afk_channel = _Channel(afk_channel_id)
            self.afk_channel.guild = self
            self.voice_channels.append(self.afk_channel)
        self.members: dict[int, object] = {}
        self.alert = _Channel(999_999)

    def get_member(self, user_id):
        return self.members.get(user_id)

    def get_channel(self, channel_id):
        return self.alert if channel_id == self.alert.id else None

    def get_channel_or_thread(self, channel_id):
        return self.get_channel(channel_id)


class _Member:
    def __init__(self, guild, user_id: int, name: str = "Alex", bot: bool = False) -> None:
        self.id = user_id
        self.name = name
        self.display_name = name
        self.bot = bot
        self.guild = guild
        self.display_avatar = types.SimpleNamespace(url=f"https://cdn/{user_id}.png")
        self.voice = types.SimpleNamespace(
            self_mute=False, mute=False, self_deaf=False, deaf=False
        )


class _Bot:
    def __init__(self, guilds) -> None:
        self.guilds = list(guilds)
        self._by_id = {g.id: g for g in self.guilds}

    def get_guild(self, guild_id):
        return self._by_id.get(guild_id)

    def get_channel(self, channel_id):
        for guild in self.guilds:
            found = guild.get_channel(channel_id)
            if found is not None:
                return found
        return None

    def get_user(self, user_id):
        return None

    async def fetch_user(self, user_id):
        raise RuntimeError("no network in tests")


class _Clock:
    """Deterministic, injectable clock."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _state(channel, *, deaf: bool = False, mute: bool = False):
    return types.SimpleNamespace(
        channel=channel, self_mute=mute, mute=False, self_deaf=deaf, deaf=False
    )


class CheckpointTestBase(unittest.IsolatedAsyncioTestCase):
    """A wired tracker + real Supabase store + fake database + fake clock."""

    start = ist(2026, 3, 10, 7, 0)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.client = FakeSupabaseClient()
        self.clock = _Clock(self.start)
        self.guild = _Guild()
        self.guilds = [self.guild]
        self.bot = _Bot(self.guilds)
        self.local = VoiceDatabase(os.path.join(self._tmp.name, "local.db"))
        self.store = SupabaseVoiceDatabase(self.local, client=self.client)
        # Stand in for the maintenance loop's schema check + outbox drain.
        self.store._schema_version = 3
        self.store._schema_ok = True
        self.store._bootstrapped = True
        self.tracker = VoiceTracker(
            self.bot, self.store, reconcile_interval=20.0, dm_reports_enabled=False,
            alert_channel_id=0, clock=self.clock, coalesce_seconds=2.0,
            checkpoint_seconds=300.0,
        )

    # -- helpers ---------------------------------------------------------

    def member(self, user_id: int = USER_ID, guild=None, name: str = "Alex", bot=False):
        guild = guild or self.guild
        member = _Member(guild, user_id, name, bot=bot)
        guild.members[user_id] = member
        return member

    async def join(self, member, channel=None):
        channel = channel or self.guild.voice_channels[0]
        channel.members.append(member)
        await self.tracker.handle_voice_state_update(
            member, _state(None), _state(channel)
        )
        return channel

    async def leave(self, member, channel=None):
        channel = channel or self.guild.voice_channels[0]
        if member in channel.members:
            channel.members.remove(member)
        await self.tracker.handle_voice_state_update(member, _state(channel), _state(None))

    async def advance(self, seconds: float, *, reconcile_every: float = 20.0) -> None:
        """Advance the clock, running the reconcile pass at the real cadence."""
        steps = max(1, int(seconds / reconcile_every))
        for _ in range(steps):
            self.clock.advance(reconcile_every)
            await self.tracker.reconcile_guilds()
            await self.tracker.rollover_midnight_sessions(self.clock.now)

    async def shown(self, user_id: int = USER_ID, guild=None) -> int:
        """Exactly what /vc leaderboard displays for one user."""
        board = await self.tracker.get_live_leaderboard(
            guild or self.guild, hydrate=False
        )
        return next((m.total_seconds for m in board if m.user_id == user_id), 0)

    def live(self, user_id: int = USER_ID, guild_id: int = GUILD_ID) -> int:
        """Seconds of the live session that have not been checkpointed yet."""
        session = self.tracker.active_sessions.get((guild_id, user_id))
        if session is None or session.is_afk:
            return 0
        return int((self.clock.now - session.credit_from).total_seconds())

    def committed(self, user_id: int = USER_ID, guild_id: int = GUILD_ID, day=None) -> int:
        day = day or self.clock.now.astimezone(IST).date().isoformat()
        return self.client.daily.get(
            (str(guild_id), str(user_id), day), {}
        ).get("duration_seconds", 0)

    def active_row(self, user_id: int = USER_ID, guild_id: int = GUILD_ID):
        return self.client.active.get((str(guild_id), str(user_id)))

    def respawn(self, *, bootstrapped: bool = True) -> VoiceTracker:
        """A fresh process over the same durable stores."""
        tracker = VoiceTracker(
            self.bot, self.store, reconcile_interval=20.0, dm_reports_enabled=False,
            alert_channel_id=0, clock=self.clock, coalesce_seconds=2.0,
            checkpoint_seconds=300.0,
        )
        self.store._bootstrapped = bootstrapped
        self.tracker = tracker
        return tracker


# ===========================================================================
# Test 1 — Continuous session
# ===========================================================================


class TestContinuousSession(CheckpointTestBase):
    async def test_four_hour_session_credits_four_hours(self):
        member = self.member()
        await self.join(member)
        await self.advance(4 * 3600)
        self.assertEqual(await self.shown(), 4 * 3600)

    async def test_time_is_durable_before_the_session_ends(self):
        """The incident's core defect: nothing was committed while connected."""
        member = self.member()
        await self.join(member)
        await self.advance(2 * 3600)
        self.assertGreaterEqual(
            self.committed(), 2 * 3600 - 300,
            "an in-progress session must checkpoint into voice_daily_stats",
        )

    async def test_committed_plus_live_equals_true_duration(self):
        member = self.member()
        await self.join(member)
        await self.advance(3 * 3600 + 700)
        session = self.tracker.active_sessions[(GUILD_ID, USER_ID)]
        live = int((self.clock.now - session.credit_from).total_seconds())
        self.assertEqual(self.committed() + live, 3 * 3600 + 700)


# ===========================================================================
# Test 2 — Previously accumulated time never disappears
# ===========================================================================


class TestStaleWriteCannotEraseCredit(CheckpointTestBase):
    async def test_stale_heartbeat_cannot_rewind_the_durable_start(self):
        member = self.member()
        await self.join(member)
        await self.advance(3600)
        before = await self.shown()
        durable_start = self.active_row()["started_at"]

        # A stale worker replays a heartbeat carrying a much later start time.
        from functions.voice.models import PersistedActiveSession

        await self.store.heartbeat_active_sessions(
            [
                PersistedActiveSession(
                    guild_id=GUILD_ID, user_id=USER_ID, channel_id=CHANNEL_A,
                    started_at=self.clock.now, last_seen_at=self.clock.now,
                    is_afk=False,
                )
            ]
        )
        self.assertEqual(
            self.active_row()["started_at"], durable_start,
            "a heartbeat must never rewrite the durable session start",
        )
        await self.advance(600)
        self.assertGreaterEqual(await self.shown(), before + 600)

    async def test_stale_snapshot_cannot_lower_the_checkpoint_cursor(self):
        member = self.member()
        await self.join(member)
        await self.advance(3600)
        cursor = self.active_row()["credited_until"]
        committed_before = self.committed()

        # Replaying an older checkpoint must credit nothing and move nothing.
        stale = self.clock.now - timedelta(hours=1)
        result = await self.store.checkpoint_active_sessions(
            [
                __import__(
                    "functions.voice.models", fromlist=["PersistedActiveSession"]
                ).PersistedActiveSession(
                    guild_id=GUILD_ID, user_id=USER_ID, channel_id=CHANNEL_A,
                    started_at=self.clock.now - timedelta(hours=2),
                    last_seen_at=stale, is_afk=False,
                )
            ],
            stale,
        )
        self.assertEqual(result[(GUILD_ID, USER_ID)][0].isoformat(), cursor)
        self.assertEqual(self.committed(), committed_before)


# ===========================================================================
# Test 3 — Frozen session recovery
# ===========================================================================


class TestFrozenSessionRecovery(CheckpointTestBase):
    async def test_a_stalled_cursor_resumes_without_duplicating(self):
        member = self.member()
        await self.join(member)
        await self.advance(3600)
        stalled = self.active_row()["credited_until"]
        committed_before = self.committed()

        # Simulate the cursor having stopped advancing: the reconcile loop must
        # pick it back up on the next interval.
        self.tracker._last_checkpoint_at = None
        await self.advance(1800)
        self.assertNotEqual(self.active_row()["credited_until"], stalled)
        self.assertGreater(self.committed(), committed_before)
        self.assertEqual(await self.shown(), 5400)

    async def test_health_snapshot_reports_a_stale_session(self):
        member = self.member()
        await self.join(member)
        await self.advance(3600)
        # Make the session look stalled: no checkpoint for two hours.
        stalled = self.clock.now - timedelta(hours=2)
        session = self.tracker.active_sessions[(GUILD_ID, USER_ID)]
        session.started_at = stalled
        session.credited_until = stalled
        snapshot = self.tracker.health_snapshot(self.clock.now)
        self.assertEqual(snapshot["creditable_sessions"], 1)
        self.assertGreaterEqual(snapshot["max_uncredited_seconds"], 7200)
        self.assertEqual(snapshot["stale_sessions"], 1)


# ===========================================================================
# Tests 4 & 5 — Repeated and concurrent checkpoints
# ===========================================================================


class TestCheckpointIdempotency(CheckpointTestBase):
    async def test_repeated_checkpoint_is_credited_once(self):
        from functions.voice.models import PersistedActiveSession

        member = self.member()
        await self.join(member)
        await self.advance(1800)
        row = PersistedActiveSession(
            guild_id=GUILD_ID, user_id=USER_ID, channel_id=CHANNEL_A,
            started_at=self.tracker.active_sessions[(GUILD_ID, USER_ID)].started_at,
            last_seen_at=self.clock.now, is_afk=False,
        )
        target = self.clock.now
        first = await self.store.checkpoint_active_sessions([row], target)
        second = await self.store.checkpoint_active_sessions([row], target)
        self.assertGreater(first[(GUILD_ID, USER_ID)][0], self.start)
        self.assertEqual(second[(GUILD_ID, USER_ID)][0], first[(GUILD_ID, USER_ID)][0])
        self.assertEqual(self.committed(), 1800)

    async def test_concurrent_checkpoints_never_lose_or_duplicate_time(self):
        member = self.member()
        await self.join(member)
        await self.advance(3600)

        # Two workers race to checkpoint overlapping intervals.
        await self.tracker.checkpoint_sessions(self.clock.now)
        first = self.committed()
        await self.tracker.checkpoint_sessions(self.clock.now)
        self.assertEqual(self.committed(), first, "the second worker must credit nothing")

        await self.advance(1800)
        self.assertEqual(self.committed(), 5400)

    async def test_a_failed_checkpoint_does_not_advance_the_cursor(self):
        member = self.member()
        await self.join(member)
        await self.advance(1800)
        before = self.active_row()["credited_until"]
        self.client.fail_mode = True
        await self.tracker.checkpoint_sessions(self.clock.now)
        self.client.fail_mode = False
        self.assertEqual(self.active_row()["credited_until"], before)
        self.assertEqual(self.tracker._checkpoint_failures, 1)


# ===========================================================================
# Test 6 — Disconnect and reconnect
# ===========================================================================


class TestDisconnectReconnect(CheckpointTestBase):
    async def test_completed_interval_survives_a_reconnect(self):
        member = self.member()
        channel = await self.join(member)
        await self.advance(1800)
        first = await self.shown()
        await self.leave(member, channel)
        after_leave = await self.shown()
        self.assertEqual(after_leave, first, "the closed slice must stay credited")

        self.clock.advance(300)  # away from voice; this time is not counted
        await self.join(member, channel)
        await self.advance(1800)
        self.assertEqual(await self.shown(), first + 1800)


# ===========================================================================
# Test 7 — Channel move
# ===========================================================================


class TestChannelMove(CheckpointTestBase):
    async def test_moving_channels_keeps_time_and_session(self):
        member = self.member()
        channel_a, channel_b = self.guild.voice_channels[0], self.guild.voice_channels[1]
        await self.join(member, channel_a)
        await self.advance(1800)
        started = self.tracker.active_sessions[(GUILD_ID, USER_ID)].started_at

        channel_a.members.remove(member)
        channel_b.members.append(member)
        await self.tracker.handle_voice_state_update(
            member, _state(channel_a), _state(channel_b)
        )
        self.assertEqual(
            self.tracker.active_sessions[(GUILD_ID, USER_ID)].started_at, started,
            "a channel move must not reset the session",
        )
        await self.advance(1800)
        self.assertEqual(await self.shown(), 3600)


# ===========================================================================
# Test 8 — Multiple guilds
# ===========================================================================


class TestMultipleGuilds(CheckpointTestBase):
    async def test_users_in_every_accessible_guild_are_tracked(self):
        second = _Guild(GUILD_ID + 1)
        self.guilds.append(second)
        self.bot = _Bot(self.guilds)
        self.tracker.bot = self.bot

        first_member = self.member(USER_ID, self.guild)
        second_member = self.member(USER_ID, second)  # same user, other guild
        other_member = self.member(20002, second, "Bea")

        await self.join(first_member, self.guild.voice_channels[0])
        second.voice_channels[0].members.append(second_member)
        second.voice_channels[1].members.append(other_member)

        await self.advance(1800)
        # The first member joined through a voice-state event at t=0; the other
        # two were already connected, so the scan picked them up on its first
        # pass, one reconcile interval later.
        self.assertEqual(await self.shown(USER_ID, self.guild), 1800)
        self.assertEqual(await self.shown(USER_ID, second), 1780)
        self.assertEqual(await self.shown(20002, second), 1780)
        # Distinct (guild, user) rows: the same user in two guilds is two
        # sessions, and neither is double counted inside its own guild.
        self.assertEqual(len(self.client.active), 3)

    async def test_a_bot_is_never_tracked(self):
        member = self.member(30001, self.guild, "MusicBot", bot=True)
        await self.join(member)
        await self.advance(1800)
        self.assertEqual(await self.shown(30001), 0)
        self.assertEqual(len(self.client.active), 0)


# ===========================================================================
# Test 9 — Bot restart
# ===========================================================================


class TestRestart(CheckpointTestBase):
    async def test_restart_while_bootstrapping_keeps_the_session(self):
        """The exact defect that lost 5h59m: recovery raced the store bootstrap."""
        member = self.member()
        await self.join(member)
        await self.advance(6 * 3600)
        before = await self.shown()
        durable_start = self.active_row()["started_at"]

        tracker = self.respawn(bootstrapped=False)
        await tracker.reconcile_guilds()
        self.assertEqual(
            self.tracker.active_sessions[(GUILD_ID, USER_ID)].started_at.isoformat(),
            durable_start,
            "recovery must restore the durable start, not restart the clock",
        )
        self.assertGreaterEqual(await self.shown(), before)
        self.assertEqual(self.active_row()["started_at"], durable_start)

        # The deferred remote recovery then runs once the store is online.
        self.store._bootstrapped = True
        await self.advance(600)
        self.assertTrue(self.tracker._recovered_remote)
        self.assertGreaterEqual(await self.shown(), before + 600)

    async def test_recovery_is_retried_after_a_failure(self):
        member = self.member()
        await self.join(member)
        await self.advance(1800)
        tracker = self.respawn()
        tracker._recovered = False
        tracker._recovered_remote = False

        async def boom(_now):
            raise RuntimeError("database exploded")

        tracker._recover_persisted_sessions = boom  # type: ignore[assignment]
        await tracker.reconcile_guilds()
        self.assertFalse(tracker._recovered, "a failed recovery must not be marked done")

        del tracker._recover_persisted_sessions  # type: ignore[attr-defined]
        await tracker.reconcile_guilds()
        self.assertTrue(tracker._recovered)

    async def test_no_reconcile_before_the_guild_cache_exists(self):
        member = self.member()
        await self.join(member)
        await self.advance(1800)
        before = await self.shown()

        tracker = self.respawn()
        self.bot.guilds = []  # gateway has not delivered the guilds yet
        await tracker.reconcile_guilds()
        self.assertEqual(len(self.client.active), 1, "sessions must not be closed blind")
        self.bot.guilds = self.guilds
        await tracker.reconcile_guilds()
        self.assertGreaterEqual(await self.shown(), before)

    async def test_restart_preserves_committed_history(self):
        member = self.member()
        await self.join(member)
        await self.advance(3600)
        await self.leave(member)
        history = self.committed()
        self.assertEqual(history, 3600)

        self.respawn()
        await self.advance(60)
        self.assertEqual(self.committed(), history)


# ===========================================================================
# Test 10 — Supabase outage
# ===========================================================================


class TestSupabaseOutage(CheckpointTestBase):
    async def test_outage_is_survivable_and_loses_nothing(self):
        member = self.member()
        await self.join(member)
        await self.advance(1800)
        before = await self.shown()

        self.client.fail_mode = True
        await self.advance(1200)  # 20 minutes with no database
        self.client.fail_mode = False
        await self.store.flush_outbox()
        await self.advance(1800)
        self.assertEqual(await self.shown(), before + 3000)

    async def test_checkpoint_failure_is_reported_not_swallowed(self):
        member = self.member()
        await self.join(member)
        await self.advance(600)
        self.client.fail_mode = True
        self.tracker._last_checkpoint_at = None
        await self.tracker.checkpoint_sessions(self.clock.now)
        self.assertEqual(self.tracker._checkpoint_failures, 1)
        snapshot = self.tracker.health_snapshot(self.clock.now)
        self.assertEqual(snapshot["consecutive_checkpoint_failures"], 1)


# ===========================================================================
# Test 11 — Midnight rollover
# ===========================================================================


class TestMidnightRollover(CheckpointTestBase):
    start = ist(2026, 3, 10, 22, 0)

    async def test_session_crossing_midnight_splits_by_ist_day(self):
        member = self.member()
        await self.join(member)
        await self.advance(6 * 3600)  # 22:00 -> 04:00 IST
        day_one = self.committed(day="2026-03-10")
        day_two = self.committed(day="2026-03-11")
        live = await self.shown()
        self.assertEqual(day_one, 7200, "22:00-00:00 belongs to the first IST day")
        self.assertEqual(day_one + live, 6 * 3600)
        self.assertLessEqual(day_two, 6 * 3600)

    async def test_rollover_does_not_reset_credited_time(self):
        member = self.member()
        await self.join(member)
        await self.advance(3 * 3600)  # 22:00 -> 01:00 IST, crosses midnight
        before = self.committed(day="2026-03-10")
        self.assertEqual(before, 7200)
        await self.advance(3600)
        self.assertEqual(
            self.committed(day="2026-03-10"), before,
            "a rollover must never lower the finished day's total",
        )


# ===========================================================================
# Tests 12 & 13 — Manual Supabase edits
# ===========================================================================


class TestManualEdits(CheckpointTestBase):
    async def test_start_time_edit_persists_and_is_adopted(self):
        member = self.member()
        await self.join(member)
        await self.advance(120)  # shorter than the checkpoint interval
        self.assertIsNone(self.active_row()["credited_until"], "nothing credited yet")
        corrected = self.clock.now - timedelta(hours=1)
        self.client.active[(str(GUILD_ID), str(USER_ID))]["started_at"] = (
            corrected.isoformat()
        )

        await self.advance(120)
        self.assertEqual(
            self.active_row()["started_at"], corrected.isoformat(),
            "the tracking loop must not revert a Dashboard edit",
        )
        self.assertEqual(
            self.tracker.active_sessions[(GUILD_ID, USER_ID)].started_at, corrected,
            "the bot must reconcile its cache with the corrected row",
        )
        # Nothing had been credited yet, so the corrected start is honoured in
        # full: the whole [corrected, now) interval is live.
        elapsed = int((self.clock.now - corrected).total_seconds())
        self.assertEqual(await self.shown(), elapsed)

    async def test_start_time_edit_does_not_recredit_a_checkpointed_session(self):
        member = self.member()
        await self.join(member)
        await self.advance(3600)  # checkpoints have committed an hour
        committed_before = self.committed()
        shown_before = await self.shown()
        corrected = (self.clock.now - timedelta(hours=3)).isoformat()
        self.client.active[(str(GUILD_ID), str(USER_ID))]["started_at"] = corrected

        await self.advance(300)
        self.assertEqual(self.active_row()["started_at"], corrected)
        # The already-credited part lives in voice_daily_stats, not in this
        # row, so it is not counted a second time: only the genuinely elapsed
        # 300s is added, not the corrected 3h.
        self.assertEqual(self.committed(), committed_before + 300)
        self.assertLess(await self.shown(), 2 * 3600)

    async def test_cursor_edit_grants_the_corrected_interval(self):
        member = self.member()
        await self.join(member)
        await self.advance(3600)
        committed_before = self.committed()
        corrected = (self.clock.now - timedelta(hours=3)).isoformat()
        row = self.client.active[(str(GUILD_ID), str(USER_ID))]
        row["started_at"] = corrected
        row["credited_until"] = corrected

        await self.advance(300)
        # The corrected interval became uncredited again and was honoured in
        # full, on top of what had already been credited for that session.
        # (An admin moving the cursor back into already-credited time is an
        # explicit decision to grant that time again.)
        corrected_span = int((self.clock.now - _parse_dt(corrected)).total_seconds())
        self.assertEqual(await self.shown(), committed_before + corrected_span)

    async def test_edit_during_an_in_flight_checkpoint_is_not_reverted(self):
        member = self.member()
        await self.join(member)
        await self.advance(3600)
        admin_started_at = self.active_row()["started_at"]
        committed_before = self.committed()
        corrected = (self.clock.now - timedelta(minutes=30)).isoformat()
        self.client.active[(str(GUILD_ID), str(USER_ID))]["credited_until"] = corrected

        # The checkpoint that was already in flight must not rewind the edit.
        await self.tracker.checkpoint_sessions(self.clock.now)
        cursor = _parse_dt(self.active_row()["credited_until"])
        self.assertGreaterEqual(cursor, _parse_dt(corrected))
        # The cursor only ever moves forward, and started_at is never written.
        self.assertEqual(self.active_row()["started_at"], admin_started_at)
        # The corrected 30 minutes was credited on top of what was committed.
        self.assertEqual(self.committed(), committed_before + 1800)


# ===========================================================================
# Test 14 — Existing history is preserved
# ===========================================================================


class TestExistingHistoryPreserved(CheckpointTestBase):
    async def test_preexisting_nonzero_stats_are_kept(self):
        # Seed history exactly as production has it: committed daily rows with
        # no active session.
        for day, seconds in (("2026-03-02", 5400), ("2026-03-09", 7200)):
            self.client.daily[(str(GUILD_ID), str(USER_ID), day)] = {
                "guild_id": GUILD_ID, "user_id": USER_ID, "date": day,
                "duration_seconds": seconds, "updated_at": "",
            }
        member = self.member()
        await self.join(member)
        await self.advance(3600)

        # 2026-03-10 is a Tuesday, so its ISO week starts Monday 2026-03-09:
        # 03-02 falls outside that window and 03-09 inside it.
        self.assertEqual(self.committed(day="2026-03-02"), 5400)
        self.assertEqual(self.committed(day="2026-03-09"), 7200)
        self.assertEqual(self.committed(day="2026-03-10") + self.live(), 3600)

        week = await self.tracker.get_live_leaderboard(
            self.guild, VoicePeriod.WEEK, hydrate=False
        )
        week_total = next(m.total_seconds for m in week if m.user_id == USER_ID)
        self.assertEqual(week_total, 7200 + 3600)

        all_time = await self.tracker.get_live_leaderboard(
            self.guild, VoicePeriod.ALL_TIME, hydrate=False
        )
        all_total = next(m.total_seconds for m in all_time if m.user_id == USER_ID)
        self.assertEqual(all_total, 5400 + 7200 + 3600)

    async def test_all_time_leaderboard_includes_history_plus_live(self):
        self.client.daily[(str(GUILD_ID), str(USER_ID), "2026-01-01")] = {
            "guild_id": GUILD_ID, "user_id": USER_ID, "date": "2026-01-01",
            "duration_seconds": 36000, "updated_at": "",
        }
        member = self.member()
        await self.join(member)
        await self.advance(1800)
        board = await self.tracker.get_live_leaderboard(
            self.guild, VoicePeriod.ALL_TIME, hydrate=False
        )
        total = next(m.total_seconds for m in board if m.user_id == USER_ID)
        self.assertEqual(total, 36000 + 1800)


# ===========================================================================
# Test 15 — Invalid or stale persisted session
# ===========================================================================


class TestStalePersistedSession(CheckpointTestBase):
    async def test_a_row_for_a_departed_user_is_closed_conservatively(self):
        member = self.member()
        await self.join(member)
        await self.advance(1800)
        # The user is gone from Discord but the row survived.
        self.guild.voice_channels[0].members.clear()
        self.tracker.active_sessions.clear()
        self.clock.advance(600)

        tracker = self.respawn()
        await tracker.reconcile_guilds()
        self.assertNotIn((GUILD_ID, USER_ID), tracker.active_sessions)
        self.assertEqual(self.committed(), 1800, "closes at the last heartbeat")
        self.assertIsNone(self.active_row())

    async def test_an_afk_row_is_never_credited(self):
        member = self.member()
        channel = await self.join(member)
        await self.advance(1800)
        member.voice.self_deaf = True
        await self.tracker.handle_voice_state_update(member, _state(channel), _state(channel, deaf=True))
        await self.advance(1800)
        self.assertEqual(await self.shown(), 1800, "deafened time is not credited")


# ===========================================================================
# Test 16 — Eligibility and exclusions
# ===========================================================================


class TestEligibility(CheckpointTestBase):
    async def test_afk_channel_time_is_not_credited(self):
        guild = _Guild(GUILD_ID, afk_channel_id=803)
        self.guild = guild
        self.guilds = [guild]
        self.bot = _Bot(self.guilds)
        self.tracker.bot = self.bot
        member = self.member(USER_ID, guild)
        channel = await self.join(member, guild.afk_channel)
        await self.advance(1800)
        self.assertEqual(await self.shown(USER_ID, guild), 0)
        self.assertTrue(self.active_row()["is_afk"])

    async def test_muted_members_are_still_credited(self):
        member = self.member()
        channel = await self.join(member)
        member.voice.self_mute = True
        await self.tracker.handle_voice_state_update(
            member, _state(channel), _state(channel, mute=True)
        )
        await self.advance(1800)
        self.assertEqual(await self.shown(), 1800)

    async def test_undeafening_resumes_credit_from_that_moment(self):
        member = self.member()
        channel = await self.join(member)
        await self.advance(1800)
        member.voice.self_deaf = True
        await self.tracker.handle_voice_state_update(
            member, _state(channel), _state(channel, deaf=True)
        )
        await self.advance(1800)  # deafened: not credited
        member.voice.self_deaf = False
        await self.tracker.handle_voice_state_update(
            member, _state(channel, deaf=True), _state(channel)
        )
        await self.advance(1800)
        self.assertEqual(await self.shown(), 3600)


# ===========================================================================
# Test 17 — Process overlap
# ===========================================================================


class TestProcessOverlap(CheckpointTestBase):
    async def test_two_instances_do_not_double_credit(self):
        member = self.member()
        await self.join(member)
        await self.advance(1800)

        other = VoiceTracker(
            self.bot, self.store, reconcile_interval=20.0, dm_reports_enabled=False,
            alert_channel_id=0, clock=self.clock, coalesce_seconds=2.0,
            checkpoint_seconds=300.0,
        )
        await other.reconcile_guilds()

        await self.advance(3600)
        for _ in range(3):
            self.clock.advance(20)
            await other.reconcile_guilds()
        await self.advance(1800)

        session = self.tracker.active_sessions[(GUILD_ID, USER_ID)]
        live = int((self.clock.now - session.credit_from).total_seconds())
        elapsed = 1800 + 3600 + 60 + 1800  # includes the 3 extra 20s ticks
        self.assertEqual(
            self.committed() + live, elapsed,
            "the database must hold exactly the elapsed time, once",
        )
        self.assertEqual(await self.shown(), elapsed)

    async def test_a_foreign_cursor_advance_is_detected(self):
        member = self.member()
        await self.join(member)
        await self.advance(1800)
        with self.assertLogs("functions.voice.tracker", level="WARNING") as logs:
            # Another process checkpoints ahead of this one.
            from functions.voice.models import PersistedActiveSession

            await self.store.checkpoint_active_sessions(
                [
                    PersistedActiveSession(
                        guild_id=GUILD_ID, user_id=USER_ID, channel_id=CHANNEL_A,
                        started_at=self.tracker.active_sessions[
                            (GUILD_ID, USER_ID)
                        ].started_at,
                        last_seen_at=self.clock.now, is_afk=False,
                    )
                ],
                self.clock.now,
            )
            await self.store.heartbeat_active_sessions(
                [
                    PersistedActiveSession(
                        guild_id=GUILD_ID, user_id=USER_ID, channel_id=CHANNEL_A,
                        started_at=self.tracker.active_sessions[
                            (GUILD_ID, USER_ID)
                        ].started_at,
                        last_seen_at=self.clock.now + timedelta(seconds=1),
                        is_afk=False,
                    )
                ]
            )
            self.tracker._adopt_durable_state(
                {
                    (GUILD_ID, USER_ID): (
                        self.tracker.active_sessions[(GUILD_ID, USER_ID)].started_at,
                        self.clock.now,
                    )
                }
            )
        self.assertTrue(
            any("overlapping deployment" in line for line in logs.output),
            logs.output,
        )


# ===========================================================================
# Test 18 — Long-running stability
# ===========================================================================


class TestLongRunningStability(CheckpointTestBase):
    async def test_ten_hours_with_no_drift_and_no_decrease(self):
        member = self.member()
        await self.join(member)
        previous = 0
        elapsed = 0
        for _ in range(20):  # 20 x 30 minutes = 10 hours
            await self.advance(1800)
            elapsed += 1800
            shown = await self.shown()
            self.assertGreaterEqual(shown, previous, "the total must never decrease")
            self.assertEqual(shown, elapsed, "no drift over a long session")
            previous = shown

    async def test_twenty_four_hours_across_a_day_boundary(self):
        self.clock.now = ist(2026, 3, 10, 12, 0)
        member = self.member()
        await self.join(member)
        await self.advance(24 * 3600)
        day_one = self.committed(day="2026-03-10")
        day_two = self.committed(day="2026-03-11")
        shown = await self.shown()
        self.assertEqual(day_one, 12 * 3600)
        self.assertEqual(day_one + shown, 24 * 3600)


# ===========================================================================
# Compatibility: a schema-v2 database keeps the old, working behaviour
# ===========================================================================


class TestV2Compatibility(CheckpointTestBase):
    async def test_v2_database_tracks_without_checkpoints(self):
        self.store._schema_version = 2
        self.assertFalse(self.store.supports_checkpoints)
        member = self.member()
        await self.join(member)
        await self.advance(3600)
        self.assertEqual(self.committed(), 0, "v2 has no periodic checkpoint")
        self.assertEqual(await self.shown(), 3600)
        await self.leave(member)
        self.assertEqual(self.committed(), 3600)


if __name__ == "__main__":
    unittest.main()


# ===========================================================================
# Test — the fractional second a long session used to lose
# ===========================================================================


class _LegacyCheckpointModel:
    """A literal transcription of the v3 SQL, kept only to document the bug."""

    def __init__(self, start: datetime) -> None:
        self.cursor = start
        self.total = 0

    def checkpoint(self, up_to: datetime) -> None:
        span = (up_to - self.cursor).total_seconds()
        if span <= 0:
            return
        credited = int(span)  # voice_record_session() floors
        self.total += credited
        self.cursor = up_to  # v3: the un-floored instant, fraction discarded


class _CarriedCheckpointModel:
    """A literal transcription of migration 004."""

    def __init__(self, start: datetime) -> None:
        self.cursor = start
        self.total = 0

    def checkpoint(self, up_to: datetime) -> None:
        target = max(self.cursor, up_to)
        span = (target - self.cursor).total_seconds()
        if span <= 0:
            return
        credited = int(span)
        self.total += credited
        self.cursor = self.cursor + timedelta(seconds=credited)  # carry


def _jittery_steps(*, hours: float, seed: int = 7):
    """The intervals a real reconcile loop produces (20 s ± latency)."""
    import random

    rng = random.Random(seed)
    total = 0.0
    while total < hours * 3600:
        step = rng.uniform(19.5, 21.5)
        total += step
        yield step


def _jittery_outputs(start: datetime, *, hours: float, window: float, seed: int = 7):
    """Checkpoint boundaries a real reconcile loop would produce.

    The loop sleeps ~20 s *plus* the latency of whatever it just did, so the
    boundaries do not share a fractional part — which is exactly why the lost
    fractions never cancelled out and the loss grew with the length of the stay.
    """
    now = start
    last = start
    for step in _jittery_steps(hours=hours, seed=seed):
        now += timedelta(seconds=step)
        if (now - last).total_seconds() >= window:
            yield now
            last = now


class TestFractionalSecondCarry(unittest.TestCase):
    """A 24-hour stay was credited as ~23 h 51 m. It was never the heartbeat."""

    def test_the_old_arithmetic_loses_minutes_on_a_jittery_loop(self):
        start = ist(2026, 3, 10, 0, 0) + timedelta(milliseconds=700)
        model = _LegacyCheckpointModel(start)
        for boundary in _jittery_outputs(start, hours=24, window=120):
            model.checkpoint(boundary)
        lost = 24 * 3600 - model.total
        # Order of minutes, growing with the length of the stay — the report.
        self.assertGreater(lost, 120, f"expected a systematic loss, got {lost}s")
        self.assertLess(lost, 900)

    def test_carrying_the_fraction_loses_nothing(self):
        start = ist(2026, 3, 10, 0, 0) + timedelta(milliseconds=700)
        model = _CarriedCheckpointModel(start)
        for boundary in _jittery_outputs(start, hours=24, window=120):
            model.checkpoint(boundary)
        lost = 24 * 3600 - model.total
        self.assertLessEqual(lost, 1, f"lost {lost}s in a day")

    def test_a_replayed_checkpoint_still_credits_nothing(self):
        start = ist(2026, 3, 10, 0, 0)
        model = _CarriedCheckpointModel(start)
        boundary = start + timedelta(seconds=120)
        model.checkpoint(boundary)
        first = model.total
        model.checkpoint(boundary)  # same call again
        self.assertEqual(model.total, first)
        self.assertEqual(model.cursor, boundary)


class TestLongSessionAccounting(CheckpointTestBase):
    """End to end, through the tracker, with a deliberately jittery clock."""

    async def test_a_whole_day_of_voice_credits_a_whole_day(self):
        self.tracker.checkpoint_seconds = 120.0
        # Join mid-second: the very first interval is fractional too.
        self.clock.now = self.start + timedelta(milliseconds=700)
        member = self.member()
        await self.join(member)

        elapsed = 0.0
        for step in _jittery_steps(hours=24):
            self.clock.advance(step)
            elapsed += step
            await self.tracker.reconcile_guilds()
            await self.tracker.rollover_midnight_sessions(self.clock.now)

        credited = sum(
            row["duration_seconds"]
            for (guild, user, _day), row in self.client.daily.items()
            if guild == str(GUILD_ID) and user == str(USER_ID)
        ) + self.live()
        # The session really lasted `elapsed` seconds (the last tick overshoots
        # 24 h a little). Every one of them must be credited, ±1 s.
        self.assertGreaterEqual(credited, int(elapsed) - 1)
        self.assertLessEqual(credited, int(elapsed) + 1, f"credited {credited}s")

    def test_the_migration_that_fixes_it_is_present(self):
        import pathlib

        sql = pathlib.Path(
            "supabase/migrations/004_voice_credit_carry.sql"
        ).read_text()
        self.assertIn("make_interval(secs => v_secs)", sql)
        self.assertIn("select 4;", sql)
        self.assertIn("round(extract(epoch from (p_ended_at - v_from)))", sql)
