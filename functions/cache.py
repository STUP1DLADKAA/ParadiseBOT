"""Tiny bounded TTL caching primitives shared by Paradise's tool layer.

Two pieces, both deliberately small:

``TTLCache``
    An insertion-ordered mapping with a maximum size *and* a per-entry
    expiry. Nothing here ever grows without bound: reads evict expired
    entries lazily and writes evict the oldest entry once ``max_entries``
    is reached. No background task, no persistence, no database.

``SingleFlight``
    Collapses identical concurrent requests into one in-flight task, so
    five people asking for the same picture in the same second cost one
    outbound API call. Cancelling one waiter never cancels the shared
    work for the others.

Both are intentionally generic so new capabilities reuse them instead of
inventing a fourth private cache implementation.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Awaitable, Callable, Generic, TypeVar

T = TypeVar("T")


class TTLCache(Generic[T]):
    """A bounded, expiring, in-memory cache. Not thread-safe (asyncio only)."""

    __slots__ = ("ttl", "max_entries", "_data")

    def __init__(self, *, ttl: float, max_entries: int = 128) -> None:
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self.ttl = float(ttl)
        self.max_entries = int(max_entries)
        self._data: OrderedDict[str, tuple[float, T]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None

    def get(self, key: str) -> T | None:
        """Return a live value, or ``None`` when missing/expired."""
        entry = self._data.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if time.monotonic() - stored_at >= self.ttl:
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return value

    def set(self, key: str, value: T) -> None:
        """Store a value, evicting expired entries and then the oldest."""
        self._data[key] = (time.monotonic(), value)
        self._data.move_to_end(key)
        self._purge_expired()
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)

    def pop(self, key: str) -> T | None:
        entry = self._data.pop(key, None)
        return None if entry is None else entry[1]

    def clear(self) -> None:
        self._data.clear()

    def _purge_expired(self) -> None:
        now = time.monotonic()
        # Entries are insertion-ordered, so the expired ones cluster at the
        # front; stop at the first live entry instead of scanning everything.
        while self._data:
            key, (stored_at, _) = next(iter(self._data.items()))
            if now - stored_at < self.ttl:
                break
            self._data.pop(key, None)


class SingleFlight:
    """Share one in-flight coroutine between identical concurrent callers."""

    __slots__ = ("_tasks",)

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}

    def __len__(self) -> int:
        return len(self._tasks)

    async def run(self, key: str, factory: Callable[[], Awaitable[T]]) -> T:
        """Run ``factory()`` once per *key* while a call is in flight."""
        task = self._tasks.get(key)
        if task is None:
            task = asyncio.ensure_future(factory())
            self._tasks[key] = task

            def _done(finished: asyncio.Task) -> None:
                if self._tasks.get(key) is finished:
                    self._tasks.pop(key, None)
                # Observe the exception so asyncio does not warn when the
                # only waiter was cancelled before the task completed.
                if not finished.cancelled():
                    finished.exception()

            task.add_done_callback(_done)
        # Shielding keeps one cancelled Discord interaction from cancelling
        # the shared outbound request for everyone else waiting on it.
        return await asyncio.shield(task)


__all__ = ["SingleFlight", "TTLCache"]
