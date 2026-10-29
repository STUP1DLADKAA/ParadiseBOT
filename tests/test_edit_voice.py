"""Tests for /edit — the daily-stat write path that used to crash.

The production failure was::

    AttributeError: 'coroutine' object has no attribute 'execute'
    functions.voice.store.VoiceStoreUnavailable: edit daily stat: ...

So there are two things to prove here:

1. ``SupabaseVoiceDatabase._execute`` accepts both shapes of query builder
   (a builder, or a coroutine that resolves to one) — the crash itself.
2. ``adjust_daily_stat`` upserts a row that does not exist yet (the old code
   issued an UPDATE, matched nothing, then tried again with a broken builder)
   and ``/edit`` uses it instead of poking at the client from the cog.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from commands.edit import EditVoiceData
from functions.voice.database import VoiceDatabase
from functions.voice.models import VoicePeriod
from functions.voice.sqlite_store import LocalVoiceStore
from functions.voice.supabase_database import SupabaseVoiceDatabase
from functions.voice.store import VoiceStore, VoiceStoreUnavailable
from voice_fakes import FakeSupabaseClient


class TestExecuteAcceptsBothBuilderShapes(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = VoiceDatabase(os.path.join(self.tmp.name, "v.db"))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    async def test_builder_factory_that_is_sync(self):
        calls = []

        class Builder:
            def execute(self):
                async def go():
                    calls.append(1)
                    return SimpleNamespace(data=[{"ok": True}])

                return go()

        store = SupabaseVoiceDatabase(self.db, client=SimpleNamespace(table=lambda name: Builder()))
        result = await store._execute("test", lambda c: c.table("x"))
        self.assertEqual(result.data, [{"ok": True}])
        self.assertEqual(calls, [1])

    async def test_builder_factory_that_is_async(self):
        """The exact shape that produced the coroutine AttributeError."""

        class Builder:
            def execute(self):
                async def go():
                    return SimpleNamespace(data=[{"ok": True}])

                return go()

        class AsyncTableClient:
            async def table(self, name):  # <- coroutine, like the broken build
                return Builder()

        store = SupabaseVoiceDatabase(self.db, client=AsyncTableClient())
        result = await store._execute("test", lambda c: c.table("x"))
        self.assertEqual(result.data, [{"ok": True}])

    async def test_failures_still_translate_to_store_unavailable(self):
        class Exploding:
            def table(self, name):
                raise RuntimeError("network down")

        store = SupabaseVoiceDatabase(self.db, client=Exploding())
        with self.assertRaises(VoiceStoreUnavailable):
            await store._execute("test", lambda c: c.table("x"))


class TestAdjustDailyStatSupabase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = VoiceDatabase(os.path.join(self.tmp.name, "v.db"))
        self.client = FakeSupabaseClient()
        self.store = SupabaseVoiceDatabase(self.db, client=self.client)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    async def test_creates_a_row_that_did_not_exist(self):
        day = date(2026, 10, 29)
        total = await self.store.adjust_daily_stat(1, 2, day, 300)
        self.assertEqual(total, 300)
        self.assertEqual(self.client.daily[("1", "2", day.isoformat())]["duration_seconds"], 300)

    async def test_adds_to_an_existing_row(self):
        day = date(2026, 10, 29)
        self.client.daily[("1", "2", day.isoformat())] = {
            "guild_id": 1, "user_id": 2, "date": day.isoformat(), "duration_seconds": 100,
        }
        total = await self.store.adjust_daily_stat(1, 2, day, 60)
        self.assertEqual(total, 160)

    async def test_negative_adjustment_clamps_at_zero(self):
        day = date(2026, 10, 29)
        self.client.daily[("1", "2", day.isoformat())] = {
            "guild_id": 1, "user_id": 2, "date": day.isoformat(), "duration_seconds": 30,
        }
        total = await self.store.adjust_daily_stat(1, 2, day, -120)
        self.assertEqual(total, 0)

    async def test_outage_returns_none_instead_of_raising(self):
        self.client.fail_mode = True
        total = await self.store.adjust_daily_stat(1, 2, date(2026, 10, 29), 60)
        self.assertIsNone(total)


class TestAdjustDailyStatLocal(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = VoiceDatabase(os.path.join(self.tmp.name, "v.db"))
        self.store = LocalVoiceStore(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    async def test_local_store_supports_the_edit_path(self):
        day = date(2026, 10, 29)
        self.assertEqual(await self.store.adjust_daily_stat(1, 2, day, 120), 120)
        self.assertEqual(await self.store.adjust_daily_stat(1, 2, day, 30), 150)
        self.assertEqual(await self.store.adjust_daily_stat(1, 2, day, -500), 0)

    async def test_default_store_implementation_is_harmless(self):
        # A store that has no daily-aggregate concept must never crash /edit.
        class Minimal(LocalVoiceStore):
            adjust_daily_stat = VoiceStore.adjust_daily_stat

        store = Minimal(self.db)
        self.assertIsNone(
            await store.adjust_daily_stat(1, 2, date.today(), 10)
        )


class _RecordingStore:
    def __init__(self, *, fails: bool = False):
        self.fails = fails
        self.calls: list[tuple[int, int, date, int]] = []

    async def adjust_daily_stat(self, guild_id, user_id, day, delta):
        if self.fails:
            raise VoiceStoreUnavailable("database is down")
        self.calls.append((guild_id, user_id, day, delta))
        return max(0, delta)


class TestEditCommand(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = MagicMock()
        self.cog = EditVoiceData(self.bot)

    async def test_today_adjustment_goes_through_the_store(self):
        store = _RecordingStore()
        ok, message = await self.cog._apply_time_adjustment(
            store, 10, 20, VoicePeriod.TODAY, 300
        )
        self.assertTrue(ok, message)
        self.assertEqual(store.calls, [(10, 20, date.today(), 300)])
        self.assertIn("+5m", message)

    async def test_yesterday_adjustment(self):
        store = _RecordingStore()
        ok, _ = await self.cog._apply_time_adjustment(
            store, 10, 20, VoicePeriod.YESTERDAY, -120
        )
        self.assertTrue(ok)
        from datetime import timedelta

        self.assertEqual(store.calls[0][2], date.today() - timedelta(days=1))

    async def test_week_splits_across_seven_days_without_losing_seconds(self):
        store = _RecordingStore()
        ok, message = await self.cog._apply_time_adjustment(
            store, 10, 20, VoicePeriod.WEEK, 700
        )
        self.assertTrue(ok, message)
        self.assertEqual(len(store.calls), 7)
        self.assertEqual(sum(call[3] for call in store.calls), 700)

    async def test_month_splits_across_thirty_days(self):
        store = _RecordingStore()
        ok, _ = await self.cog._apply_time_adjustment(
            store, 10, 20, VoicePeriod.MONTH, 300
        )
        self.assertTrue(ok)
        self.assertEqual(sum(call[3] for call in store.calls), 300)

    async def test_all_time_books_on_today(self):
        store = _RecordingStore()
        ok, message = await self.cog._apply_time_adjustment(
            store, 10, 20, VoicePeriod.ALL_TIME, 60
        )
        self.assertTrue(ok)
        self.assertEqual(store.calls, [(10, 20, date.today(), 60)])
        self.assertIn("today", message)

    async def test_store_outage_reports_failure_without_raising(self):
        store = _RecordingStore(fails=True)
        ok, message = await self.cog._apply_time_adjustment(
            store, 10, 20, VoicePeriod.TODAY, 60
        )
        self.assertFalse(ok)
        self.assertIn("Failed", message)

    async def test_zero_adjustment_is_a_no_op_success(self):
        store = _RecordingStore()
        ok, message = await self.cog._apply_time_adjustment(
            store, 10, 20, VoicePeriod.WEEK, 0
        )
        self.assertTrue(ok)
        self.assertEqual(store.calls, [])


if __name__ == "__main__":
    unittest.main()
