"""Tests for the shared performance plumbing (``functions/perf.py``).

These pin the behaviours the rest of the bot relies on: one pooled HTTP
session per purpose, a dedicated render pool, hardware-sized worker counts and
a prewarm pass that never raises.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from functions import perf


class TestSizing(unittest.TestCase):
    def test_worker_counts_are_bounded_and_sane(self):
        self.assertGreaterEqual(perf.cpu_count(), 1)
        self.assertGreaterEqual(perf.auto_workers(), perf._MIN_WORKERS)
        self.assertLessEqual(perf.auto_workers(), perf._MAX_WORKERS)
        self.assertGreaterEqual(perf.auto_render_threads(), 2)
        self.assertLessEqual(perf.auto_render_threads(), perf._MAX_RENDER_THREADS)
        self.assertGreaterEqual(perf.auto_http_limit(), 32)

    def test_a_huge_machine_does_not_produce_absurd_pools(self):
        original = perf.cpu_count
        perf.cpu_count = lambda: 512
        try:
            self.assertEqual(perf.auto_workers(), perf._MAX_WORKERS)
            self.assertEqual(perf.auto_render_threads(), perf._MAX_RENDER_THREADS)
            self.assertLessEqual(perf.auto_http_limit(), 256)
        finally:
            perf.cpu_count = original


class TestSharedSessions(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await perf.close_shared_sessions()

    async def test_the_same_name_reuses_one_session(self):
        first = perf.shared_session("unit-a", timeout=5.0)
        second = perf.shared_session("unit-a", timeout=5.0)
        self.assertIs(first, second)
        self.assertFalse(first.closed)

    async def test_different_purposes_get_different_sessions(self):
        a = perf.shared_session("unit-b", timeout=5.0)
        b = perf.shared_session("unit-c", timeout=5.0)
        self.assertIsNot(a, b)
        self.assertEqual(sorted(perf.pool_snapshot()), ["unit-b", "unit-c"])

    async def test_closing_releases_and_allows_reuse(self):
        session = perf.shared_session("unit-d", timeout=5.0)
        await perf.close_shared_sessions()
        self.assertTrue(session.closed)
        self.assertEqual(perf.pool_snapshot(), {})
        replacement = perf.shared_session("unit-d", timeout=5.0)
        self.assertIsNot(replacement, session)
        self.assertFalse(replacement.closed)

    async def test_a_new_event_loop_gets_fresh_sessions(self):
        sessions: list[object] = []

        def in_loop() -> None:
            sessions.append(asyncio.run(_open("unit-e")))

        await asyncio.to_thread(in_loop)
        mine = perf.shared_session("unit-e", timeout=5.0)
        # The session created on the other loop is a different object: a
        # connector can never be shared across loops.
        self.assertIsNot(mine, sessions[0])


async def _open(name: str):
    session = perf.shared_session(name, timeout=5.0)
    return session


class TestRenderPool(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await perf.shutdown_render_pool()

    async def test_render_work_runs_off_the_event_loop(self):
        main_thread = threading.get_ident()
        worker = await perf.run_render(threading.get_ident)
        self.assertNotEqual(worker, main_thread)

    async def test_the_executor_is_created_once(self):
        first = perf.image_executor()
        self.assertIs(first, perf.image_executor())

    async def test_shutdown_is_idempotent(self):
        perf.image_executor()
        await perf.shutdown_render_pool()
        await perf.shutdown_render_pool()
        # A later render simply builds a fresh pool.
        self.assertIsNotNone(perf.image_executor())


class TestPrewarm(unittest.IsolatedAsyncioTestCase):
    def test_prewarm_blocking_returns_timings_without_raising(self):
        timings = perf.prewarm_blocking()
        self.assertIsInstance(timings, dict)
        self.assertIn("fonts_ms", timings)
        self.assertGreaterEqual(timings["fonts_ms"], 0.0)

    async def test_prewarm_can_be_disabled(self):
        await perf.prewarm(enabled=False)
        self.assertEqual(perf._prewarm_done_at, 0.0)

    async def test_prewarm_survives_a_broken_asset(self):
        original = perf.prewarm_blocking

        def boom():
            raise RuntimeError("assets are missing")

        perf.prewarm_blocking = boom
        try:
            await perf.prewarm()  # must not raise
        finally:
            perf.prewarm_blocking = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
