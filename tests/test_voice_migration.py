"""Tests for the SQLite → Supabase voice data migration.

Covers: full migration of all five tables, idempotency (run twice), failure
safety (Supabase down mid-migration never deletes local rows), verification
mismatches keeping rows, batched progression with resume after partial
failure, the exactly-once daily ledger (with live activity landing during
the migration), source_id dedupe, and non-voice SQLite data staying intact.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from functions.voice import VoiceDatabase
from functions.voice.migration import VoiceMigrator
from functions.voice.models import DailyUserStat
from functions.voice.store import VoiceStoreUnavailable
from functions.voice.supabase_database import SupabaseVoiceDatabase
from voice_fakes import FakeSupabaseClient


def _dt(*args):
    return datetime(*args, tzinfo=timezone.utc)


class MigrationTestBase(unittest.IsolatedAsyncioTestCase):
    GUILD = 555
    USER = 501

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "paradise.db")
        self.db = VoiceDatabase(self.db_path)
        self.fake = FakeSupabaseClient()
        self.store = SupabaseVoiceDatabase(self.db, client=self.fake)
        self.migrator = VoiceMigrator(self.db, self.store, batch_size=500)

    def tearDown(self):
        self.temp_dir.cleanup()

    # -- seed helpers ----------------------------------------------------

    def _seed_daily_direct(self, date_str: str, seconds: int, user_id: int = None) -> None:
        """Write a pre-cutover SQLite daily total directly (no session row)."""
        with self.db._get_connection() as conn:
            conn.execute(
                "INSERT INTO voice_daily_stats (guild_id, user_id, date, duration_seconds)"
                " VALUES (?, ?, ?, ?)",
                (self.GUILD, user_id or self.USER, date_str, seconds),
            )

    def seed_sqlite(self, sessions=2, days=3, profiles=1, active=0, dm_reports=0):
        t0 = _dt(2026, 9, 28, 10, 0)
        for i in range(sessions):
            self.db.record_completed_session(
                self.GUILD,
                self.USER,
                999,
                t0 + timedelta(hours=2 * i),
                t0 + timedelta(hours=2 * i + 1),
            )
        for d in range(days):
            day = date(2026, 9, 20) + timedelta(days=d)
            # A pre-cutover SQLite daily total (not representable as one
            # clean session — e.g. legacy aggregates) written directly.
            self._seed_daily_direct(day.isoformat(), 3600)
        for i in range(profiles):
            self.db.upsert_member_profile(self.GUILD, 501 + i, f"Member {i}", f"member{i}", None)
        for i in range(active):
            self.db.upsert_active_session(
                self.GUILD, 601 + i, 998, _dt(2026, 10, 6, 9, 0), _dt(2026, 10, 6, 9, 5)
            )
        for d in range(dm_reports):
            self.db.mark_dm_report_sent(
                (date(2026, 10, 1) + timedelta(days=d)).isoformat(), self.GUILD, self.USER
            )

    def remaining(self):
        return {
            "sessions": self.db.count_migration_rows("voice_sessions"),
            "daily": self.db.count_migration_rows("voice_daily_stats"),
            "profiles": self.db.count_migration_rows("voice_member_profiles"),
            "active": self.db.count_migration_rows("voice_active_sessions"),
            "dm_reports": self.db.count_migration_rows("voice_dm_reports"),
        }


class TestFullMigration(MigrationTestBase):
    async def test_all_tables_migrate_and_local_voice_rows_are_deleted(self):
        self.seed_sqlite(sessions=3, days=4, profiles=2, active=2, dm_reports=3)
        before = self.remaining()
        reports = await self.migrator.run()

        self.assertEqual(self.remaining(), {
            "sessions": 0, "daily": 0, "profiles": 0, "active": 0, "dm_reports": 0,
        })
        self.assertEqual(reports["voice_sessions"].verified_deleted, before["sessions"])
        self.assertEqual(reports["voice_daily_stats"].verified_deleted, before["daily"])
        self.assertEqual(reports["voice_member_profiles"].verified_deleted, before["profiles"])
        self.assertEqual(reports["voice_active_sessions"].verified_deleted, before["active"])
        self.assertEqual(reports["voice_dm_reports"].verified_deleted, before["dm_reports"])

        # Data actually landed in "Supabase" with the right shape.
        self.assertEqual(len(self.fake.sessions), 3)
        for row in self.fake.sessions.values():
            self.assertIn("source_id", row)
            self.assertIsNotNone(row["source_id"])
        # 4 seeded daily rows + 1 from the sessions' own midnight splits.
        self.assertEqual(len(self.fake.daily), before["daily"])
        self.assertEqual(
            self.fake.daily[("555", "501", "2026-09-20")]["duration_seconds"], 3600
        )
        self.assertEqual(len(self.fake.profiles), 2)
        self.assertEqual(len(self.fake.active), 2)
        self.assertEqual(len(self.fake.dm_reports), 3)

    async def test_migration_is_idempotent_when_run_twice(self):
        self.seed_sqlite(sessions=3, days=4, profiles=2, active=1, dm_reports=2)
        await self.migrator.run()
        snapshot = {
            "sessions": len(self.fake.sessions),
            "daily": dict((k, v["duration_seconds"]) for k, v in self.fake.daily.items()),
            "profiles": len(self.fake.profiles),
            "active": len(self.fake.active),
            "dm_reports": len(self.fake.dm_reports),
        }
        await self.migrator.run()  # nothing left — no-ops everywhere
        self.assertEqual(self.remaining(), {
            "sessions": 0, "daily": 0, "profiles": 0, "active": 0, "dm_reports": 0,
        })
        self.assertEqual(len(self.fake.sessions), snapshot["sessions"])
        self.assertEqual(
            dict((k, v["duration_seconds"]) for k, v in self.fake.daily.items()),
            snapshot["daily"],
        )
        self.assertEqual(len(self.fake.profiles), snapshot["profiles"])
        self.assertEqual(len(self.fake.active), snapshot["active"])
        self.assertEqual(len(self.fake.dm_reports), snapshot["dm_reports"])

    async def test_non_voice_sqlite_data_survives(self):
        self.seed_sqlite()
        # Other Paradise data in the same file.
        self.db.set_user_timezone(self.USER, "Europe/Berlin")
        self.db.set_user_dm_preference(self.USER, True)
        self.db.set_user_timezone(4242, "Asia/Kolkata")

        await self.migrator.run()

        self.assertEqual(self.db.get_user_timezone(self.USER), "Europe/Berlin")
        self.assertEqual(self.db.get_user_timezone(4242), "Asia/Kolkata")
        self.assertTrue(self.db.get_user_dm_preference(self.USER))
        # And the file itself still exists.
        self.assertTrue(os.path.exists(self.db_path))

    async def test_daily_totals_audit_is_logged(self):
        self.seed_sqlite(days=3)
        daily_rows = self.remaining()["daily"]
        expected_seconds = sum(r.duration_seconds for r in self.db.fetch_migration_daily(1000))
        with self.assertLogs("functions.voice.migration", level="INFO") as logs:
            await self.migrator.run()
        joined = "\n".join(logs.output)
        self.assertIn(f"{expected_seconds:,} seconds", joined)
        self.assertIn(f"Migrated {daily_rows} daily records", joined)


class TestFailureSafety(MigrationTestBase):
    async def test_supabase_outage_keeps_all_local_rows(self):
        self.seed_sqlite(sessions=2, days=2, profiles=1, active=1, dm_reports=1)
        before = self.remaining()
        self.fake.fail_mode = True
        reports = await self.migrator.run()
        self.assertTrue(all(r.interrupted for r in reports.values()))
        self.assertEqual(self.remaining(), before)
        self.assertEqual(len(self.fake.sessions), 0)

        # Recovery once Supabase is back.
        self.fake.fail_mode = False
        await self.migrator.run()
        self.assertEqual(self.remaining(), {
            "sessions": 0, "daily": 0, "profiles": 0, "active": 0, "dm_reports": 0,
        })

    async def test_verification_failure_keeps_local_rows(self):
        self.seed_sqlite(sessions=2)
        # The backend lies: the uploaded rows can never be read back.
        with patch.object(
            self.store, "fetch_migration_sessions", return_value=[]
        ):
            reports = await self.migrator.run()
        self.assertEqual(reports["voice_sessions"].verification_failures, 2)
        self.assertEqual(self.remaining()["sessions"], 2)
        # Supabase actually holds them; the local rows simply weren't deleted.
        self.assertEqual(len(self.fake.sessions), 2)

    async def test_partial_migration_resumes_across_runs(self):
        self.seed_sqlite(sessions=5)
        self.migrator = VoiceMigrator(self.db, self.store, batch_size=2)

        # First batch of 2 succeeds, then the network dies.
        calls = {"n": 0}
        real_upload = self.store.upload_migration_sessions

        async def flaky_upload(rows):
            calls["n"] += 1
            if calls["n"] > 1:
                raise VoiceStoreUnavailable("upload sessions: down")
            return await real_upload(rows)

        with patch.object(self.store, "upload_migration_sessions", flaky_upload):
            await self.migrator.run()
        self.assertEqual(self.remaining()["sessions"], 3)
        self.assertEqual(len(self.fake.sessions), 2)

        # Second run finishes the job without duplicating anything.
        await self.migrator.run()
        self.assertEqual(self.remaining()["sessions"], 0)
        self.assertEqual(len(self.fake.sessions), 5)

    async def test_sessions_already_uploaded_are_not_duplicated(self):
        # Simulate a crash after upload but before local deletion.
        self.seed_sqlite(sessions=2)
        batch = self.db.fetch_migration_sessions(2)
        await self.store.upload_migration_sessions(batch)
        self.assertEqual(len(self.fake.sessions), 2)
        self.assertEqual(self.remaining()["sessions"], 2)

        await self.migrator.run()
        self.assertEqual(len(self.fake.sessions), 2)  # dedupe on source_id
        self.assertEqual(self.remaining()["sessions"], 0)


class TestDailyLedger(MigrationTestBase):
    async def test_replayed_merge_never_double_counts(self):
        rows = [DailyUserStat(self.GUILD, self.USER, "2026-09-25", 3600)]
        first = await self.store.merge_migration_daily(rows)
        self.assertEqual(first[0]["resulting_seconds"], 3600)
        # The bot records live activity on the same key between retries.
        await self.store.record_completed_session(
            self.GUILD, self.USER, 999, _dt(2026, 9, 25, 12, 0), _dt(2026, 9, 25, 13, 0)
        )
        self.assertEqual(
            self.fake.daily[("555", "501", "2026-09-25")]["duration_seconds"], 7200
        )
        # Retry the same migration batch (e.g. verification failed earlier).
        second = await self.store.merge_migration_daily(rows)
        self.assertEqual(second[0]["resulting_seconds"], 7200)  # not 10800
        fetched = await self.store.fetch_migration_daily(
            [(self.GUILD, self.USER, "2026-09-25")]
        )
        self.assertEqual(fetched[0]["duration_seconds"], 7200)

    async def test_migration_after_live_activity_merges_exactly_once(self):
        # Historical day already has live-recorded time in Supabase; the
        # SQLite total for that day is the PRE-CUTOVER total only.
        await self.store.record_completed_session(
            self.GUILD, self.USER, 999, _dt(2026, 9, 25, 9, 0), _dt(2026, 9, 25, 10, 0)
        )  # live: 3600
        self._seed_daily_direct("2026-09-25", 1000)

        await self.migrator.run()
        self.assertEqual(self.remaining()["daily"], 0)
        self.assertEqual(
            self.fake.daily[("555", "501", "2026-09-25")]["duration_seconds"], 4600
        )

    async def test_consecutive_batches_for_same_key_merge(self):
        # Re-merging the same value is a no-op; a grown value (e.g. the row
        # was kept after a failed verification and gained time) merges the
        # delta only.
        self._seed_daily_direct("2026-09-25", 1000)
        await self.migrator.run()
        self.assertEqual(
            self.fake.daily[("555", "501", "2026-09-25")]["duration_seconds"], 1000
        )
        # Re-merge the same value: nothing changes.
        same = [DailyUserStat(self.GUILD, self.USER, "2026-09-25", 1000)]
        result = await self.store.merge_migration_daily(same)
        self.assertEqual(result[0]["resulting_seconds"], 1000)
        # Re-merge a grown value: only the +500 delta is applied.
        grown = [DailyUserStat(self.GUILD, self.USER, "2026-09-25", 1500)]
        result = await self.store.merge_migration_daily(grown)
        self.assertEqual(result[0]["resulting_seconds"], 1500)


class TestActiveAndProfileMigration(MigrationTestBase):
    async def test_profiles_migrate_with_avatar_coalesce(self):
        self.db.upsert_member_profile(self.GUILD, self.USER, "Alex", "alex", "http://img/a.png")
        await self.migrator.run()
        self.assertEqual(
            self.fake.profiles[("555", "501")]["avatar_url"], "http://img/a.png"
        )

    async def test_active_sessions_migrate_lww(self):
        self.db.upsert_active_session(
            self.GUILD, self.USER, 998, _dt(2026, 10, 6, 9, 0), _dt(2026, 10, 6, 9, 5)
        )
        # A fresher live heartbeat already reached Supabase.
        await self.store.upsert_active_session(
            self.GUILD, self.USER, 998, _dt(2026, 10, 6, 9, 0), _dt(2026, 10, 6, 9, 7)
        )
        await self.migrator.run()
        self.assertEqual(self.remaining()["active"], 0)
        self.assertEqual(
            self.fake.active[("555", "501")]["last_seen_at"],
            _dt(2026, 10, 6, 9, 7).isoformat(),
        )

    async def test_dm_report_migration_prevents_duplicate_reports(self):
        self.db.mark_dm_report_sent("2026-10-01", self.GUILD, self.USER)
        await self.migrator.run()
        self.assertTrue(await self.store.has_dm_report_sent("2026-10-01", self.GUILD, self.USER))
        self.assertEqual(self.remaining()["dm_reports"], 0)
        # Re-marking (replay) is harmless.
        await self.store.mark_dm_report_sent("2026-10-01", self.GUILD, self.USER)
        self.assertEqual(len(self.fake.dm_reports), 1)


if __name__ == "__main__":
    unittest.main()
