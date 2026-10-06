"""Tests for the shared bounded TTL cache primitives (functions/cache.py)."""

from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from functions.cache import SingleFlight, TTLCache  # noqa: E402


class TestTTLCache(unittest.TestCase):
    def test_stores_and_reads_back(self):
        cache: TTLCache[str] = TTLCache(ttl=60, max_entries=4)
        cache.set("a", "one")
        self.assertEqual(cache.get("a"), "one")
        self.assertIn("a", cache)

    def test_missing_key_is_none(self):
        cache: TTLCache[str] = TTLCache(ttl=60)
        self.assertIsNone(cache.get("nope"))

    def test_entries_expire(self):
        cache: TTLCache[str] = TTLCache(ttl=10, max_entries=4)
        with patch.object(time, "monotonic", return_value=1000.0):
            cache.set("a", "one")
        with patch.object(time, "monotonic", return_value=1005.0):
            self.assertEqual(cache.get("a"), "one")
        with patch.object(time, "monotonic", return_value=1011.0):
            self.assertIsNone(cache.get("a"))
        self.assertEqual(len(cache), 0)  # expired entries are dropped, not kept

    def test_max_entries_is_enforced(self):
        cache: TTLCache[int] = TTLCache(ttl=600, max_entries=3)
        for index in range(50):
            cache.set(f"k{index}", index)
        self.assertLessEqual(len(cache), 3)
        self.assertIsNone(cache.get("k0"))
        self.assertEqual(cache.get("k49"), 49)

    def test_reads_refresh_recency(self):
        cache: TTLCache[int] = TTLCache(ttl=600, max_entries=2)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.get("a")           # 'a' becomes the most recently used
        cache.set("c", 3)        # evicts the least recently used ('b')
        self.assertEqual(cache.get("a"), 1)
        self.assertIsNone(cache.get("b"))

    def test_pop_and_clear(self):
        cache: TTLCache[int] = TTLCache(ttl=600)
        cache.set("a", 1)
        self.assertEqual(cache.pop("a"), 1)
        self.assertIsNone(cache.pop("a"))
        cache.set("b", 2)
        cache.clear()
        self.assertEqual(len(cache), 0)

    def test_invalid_configuration_is_rejected(self):
        with self.assertRaises(ValueError):
            TTLCache(ttl=0)
        with self.assertRaises(ValueError):
            TTLCache(ttl=10, max_entries=0)


class TestSingleFlight(unittest.IsolatedAsyncioTestCase):
    async def test_identical_concurrent_calls_run_once(self):
        flight = SingleFlight()
        calls = 0

        async def work():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            return "done"

        results = await asyncio.gather(*(flight.run("k", work) for _ in range(6)))
        self.assertEqual(results, ["done"] * 6)
        self.assertEqual(calls, 1)

    async def test_different_keys_run_separately(self):
        flight = SingleFlight()
        calls = []

        async def work(name):
            calls.append(name)
            return name

        await asyncio.gather(
            flight.run("a", lambda: work("a")), flight.run("b", lambda: work("b"))
        )
        self.assertEqual(sorted(calls), ["a", "b"])

    async def test_entries_are_released_after_completion(self):
        flight = SingleFlight()

        async def work():
            return 1

        await flight.run("k", work)
        await asyncio.sleep(0)
        self.assertEqual(len(flight), 0)

    async def test_failures_propagate_and_do_not_leak(self):
        flight = SingleFlight()

        async def boom():
            raise RuntimeError("nope")

        with self.assertRaises(RuntimeError):
            await flight.run("k", boom)
        await asyncio.sleep(0)
        self.assertEqual(len(flight), 0)

    async def test_one_cancelled_waiter_does_not_cancel_the_others(self):
        flight = SingleFlight()

        async def slow():
            await asyncio.sleep(0.05)
            return "finished"

        first = asyncio.ensure_future(flight.run("k", slow))
        second = asyncio.ensure_future(flight.run("k", slow))
        await asyncio.sleep(0)
        first.cancel()
        self.assertEqual(await second, "finished")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
