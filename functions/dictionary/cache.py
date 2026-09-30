"""Small bounded caches and request coalescing.

Nothing in here knows about Discord, HTTP or dictionaries — it is the
plumbing that keeps the feature fast without ever growing without bound:

- :class:`TTLCache`  — LRU + time-to-live, optionally capped by total weight
  (used for audio bytes and rendered PNGs, where item *count* is the wrong
  unit to bound on).
- :class:`SingleFlight` — if ten people ask for the same word at the same
  moment, exactly one lookup happens and everyone shares the answer.
- :class:`RateLimiter` — a tiny token bucket so Paradise never hammers a
  free public API.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


@dataclass(slots=True)
class CacheStats:
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    expirations: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


class TTLCache(Generic[K, V]):
    """Least-recently-used cache with expiry and an optional weight cap.

    ``weigher`` turns a value into a cost (bytes, usually). When the total
    weight exceeds ``max_weight`` the oldest entries are dropped, so a few
    huge items can never pin megabytes in memory.
    """

    __slots__ = (
        "_data",
        "_max_items",
        "_max_weight",
        "_ttl",
        "_timer",
        "_weigher",
        "_weight",
        "stats",
    )

    def __init__(
        self,
        max_items: int = 256,
        ttl: float = 3600.0,
        *,
        max_weight: int | None = None,
        weigher: Callable[[V], int] | None = None,
        timer: Callable[[], float] = time.monotonic,
    ) -> None:
        self._data: OrderedDict[K, tuple[float, int, V]] = OrderedDict()
        self._max_items = max(1, int(max_items))
        self._max_weight = max_weight
        self._ttl = float(ttl)
        self._timer = timer
        self._weigher = weigher
        self._weight = 0
        self.stats = CacheStats()

    # -- reads -------------------------------------------------------------

    def get(self, key: K) -> V | None:
        entry = self._data.get(key)
        if entry is None:
            self.stats.misses += 1
            return None
        expires_at, _weight, value = entry
        if expires_at <= self._timer():
            self._drop(key)
            self.stats.expirations += 1
            self.stats.misses += 1
            return None
        self._data.move_to_end(key)
        self.stats.hits += 1
        return value

    def __contains__(self, key: object) -> bool:
        try:
            return self.get(key) is not None  # type: ignore[arg-type]
        except TypeError:
            return False

    def __len__(self) -> int:
        return len(self._data)

    @property
    def weight(self) -> int:
        return self._weight

    # -- writes ------------------------------------------------------------

    def put(self, key: K, value: V, *, ttl: float | None = None) -> None:
        self._drop(key)
        cost = self._weigher(value) if self._weigher else 0
        # A single oversized item is not worth evicting the whole cache for.
        if self._max_weight is not None and cost > self._max_weight:
            return
        self._data[key] = (self._timer() + (self._ttl if ttl is None else ttl), cost, value)
        self._weight += cost
        self._evict()

    def pop(self, key: K) -> V | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        self._drop(key)
        return entry[2]

    def clear(self) -> None:
        self._data.clear()
        self._weight = 0

    # -- housekeeping ------------------------------------------------------

    def prune(self) -> int:
        """Drop everything that has expired. Returns how many went."""
        now = self._timer()
        stale = [key for key, (expires_at, _, _) in self._data.items() if expires_at <= now]
        for key in stale:
            self._drop(key)
        self.stats.expirations += len(stale)
        return len(stale)

    def _drop(self, key: K) -> None:
        entry = self._data.pop(key, None)
        if entry is not None:
            self._weight -= entry[1]

    def _evict(self) -> None:
        while len(self._data) > self._max_items:
            key, entry = self._data.popitem(last=False)
            self._weight -= entry[1]
            self.stats.evictions += 1
        if self._max_weight is None:
            return
        while self._weight > self._max_weight and self._data:
            key, entry = self._data.popitem(last=False)
            self._weight -= entry[1]
            self.stats.evictions += 1


class SingleFlight:
    """Collapse concurrent identical async calls into one.

    The second caller for a key awaits the first caller's task instead of
    starting its own. Failures propagate to every waiter and are never
    cached — only the in-flight task is shared.
    """

    __slots__ = ("_calls",)

    def __init__(self) -> None:
        self._calls: dict[Hashable, asyncio.Future[Any]] = {}

    @property
    def in_flight(self) -> int:
        return len(self._calls)

    async def run(self, key: Hashable, factory: Callable[[], Awaitable[V]]) -> V:
        existing = self._calls.get(key)
        if existing is not None:
            # shield: one waiter cancelling must not cancel it for everyone.
            return await asyncio.shield(existing)

        loop = asyncio.get_running_loop()
        future: asyncio.Future[V] = loop.create_future()
        self._calls[key] = future
        try:
            result = await factory()
        except BaseException as error:  # noqa: BLE001 - re-raised below
            if not future.done():
                future.set_exception(error)
            # Nobody may be awaiting it; keep the loop quiet about that.
            future.exception()
            raise
        else:
            if not future.done():
                future.set_result(result)
            return result
        finally:
            self._calls.pop(key, None)


class RateLimiter:
    """Token bucket: at most ``rate`` calls per ``per`` seconds, with burst."""

    __slots__ = ("_per", "_rate", "_timer", "_tokens", "_updated")

    def __init__(
        self,
        rate: int = 8,
        per: float = 1.0,
        *,
        timer: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rate = max(1, int(rate))
        self._per = max(0.001, float(per))
        self._timer = timer
        self._tokens = float(self._rate)
        self._updated = timer()

    def _refill(self) -> None:
        now = self._timer()
        elapsed = now - self._updated
        if elapsed <= 0:
            return
        self._updated = now
        self._tokens = min(float(self._rate), self._tokens + elapsed * self._rate / self._per)

    def try_acquire(self) -> bool:
        self._refill()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    def retry_after(self) -> float:
        self._refill()
        if self._tokens >= 1.0:
            return 0.0
        return (1.0 - self._tokens) * self._per / self._rate

    async def acquire(self, timeout: float = 5.0) -> bool:
        """Wait (briefly) for a token. False means "give up, we're throttled"."""
        deadline = self._timer() + timeout
        while True:
            if self.try_acquire():
                return True
            wait = self.retry_after()
            if self._timer() + wait > deadline:
                return False
            await asyncio.sleep(min(wait, 0.25))
