"""Caches with per-fact freshness rules, plus the autocomplete memory.

The entertainment system distinguishes two classes of information:

- **stable** facts (synopsis, cast, director, genres, franchise links)
  change rarely → long TTL (default 24h);
- **volatile** facts (ratings, streaming availability, airing status,
  trending, upcoming lists) change constantly → short TTL (default 45min).

:class:`MediaCache` stores :class:`.MediaTitle` objects per query and
knows how to rebuild only what went stale, :class:`TitleMemory` is the
lightweight local layer that makes autocomplete instant for anything the
server has ever talked about.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any, Generic, Hashable, TypeVar

from .models import Identity, MediaTitle

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")

STABLE_TTL = 24 * 3600.0
VOLATILE_TTL = 45 * 60.0
SEARCH_TTL = 15 * 60.0
NEGATIVE_TTL = 10 * 60.0
MEMORY_TTL = 14 * 24 * 3600.0


class TTLCache(Generic[K, V]):
    """Least-recently-used cache with expiry and an item cap."""

    __slots__ = ("_data", "_max_items", "_ttl", "_timer")

    def __init__(self, max_items: int = 256, ttl: float = 3600.0) -> None:
        self._data: OrderedDict[K, tuple[float, V]] = OrderedDict()
        self._max_items = max(1, int(max_items))
        self._ttl = float(ttl)
        self._timer = time.monotonic

    def get(self, key: K) -> V | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= self._timer():
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return value

    def put(self, key: K, value: V, ttl: float | None = None) -> None:
        self._data.pop(key, None)
        self._data[key] = (self._timer() + (self._ttl if ttl is None else ttl), value)
        while len(self._data) > self._max_items:
            self._data.popitem(last=False)

    def pop(self, key: K) -> V | None:
        entry = self._data.pop(key, None)
        return entry[1] if entry else None

    def __len__(self) -> int:
        return len(self._data)

    def clear(self) -> None:
        self._data.clear()


class MediaCache:
    """Two-tier result cache: one stable copy, one volatile overlay.

    ``lookup`` results are stored whole with a stable TTL; volatile parts
    (ratings, streaming, status) expire much sooner. When volatile data
    goes stale we still return the stable body — the pipeline refreshes
    only the volatile bits on the next lookup of the same query.
    """

    def __init__(
        self,
        max_items: int = 256,
        stable_ttl: float = STABLE_TTL,
        volatile_ttl: float = VOLATILE_TTL,
    ) -> None:
        self._stable = TTLCache[str, tuple[MediaTitle, float]](max_items, stable_ttl)
        self._volatile_keys = TTLCache[str, float](max_items, volatile_ttl)

    def get_stable(self, key: str) -> MediaTitle | None:
        entry = self._stable.get(self._norm(key))
        return entry[0] if entry else None

    def put_stable(self, key: str, title: MediaTitle) -> None:
        self._stable.put(self._norm(key), (title, time.monotonic()))

    def volatile_fresh(self, key: str) -> bool:
        return self._volatile_keys.get(self._norm(key)) is not None

    def mark_volatile(self, key: str) -> None:
        self._volatile_keys.put(self._norm(key), time.monotonic())

    @staticmethod
    def _norm(key: str) -> str:
        return key.strip().casefold()


class TitleMemory:
    """Every title this server has resolved, kept for instant autocomplete.

    This is the 'lightweight local cache layer' for the suggestion box:
    prefix matches come back with zero network traffic, while the live
    IMDb/Kitsu suggestions still run in the background and get merged in.
    """

    def __init__(self, max_items: int = 1500, ttl: float = MEMORY_TTL) -> None:
        self._cache = TTLCache[str, list[Identity]](max_items, ttl)

    def remember_identities(self, identities: list[Identity]) -> None:
        for identity in identities:
            if not identity.is_media or not identity.title:
                continue
            key = identity.title.strip().casefold()
            if not key:
                continue
            existing = self._cache.get(key) or []
            # Replace a same-titled entry; otherwise keep the newer list.
            merged = [i for i in existing if i.title != identity.title]
            merged.append(identity)
            self._cache.put(key, merged[:5])

    def remember_title(self, title: MediaTitle) -> None:
        identity = title.identity
        if identity is not None:
            self.remember_identities([identity])

    def search(self, query: str, limit: int = 25) -> list[Identity]:
        query = query.strip().casefold()
        if not query:
            return []
        hits: list[Identity] = []
        seen: set[str] = set()
        for key in list(self._cache._data.keys()):
            if query in key:
                for identity in self._cache.get(key) or []:
                    sig = f"{identity.title.casefold()}|{identity.year}"
                    if sig not in seen:
                        seen.add(sig)
                        hits.append(identity)
        return hits[:limit]


class RateLimiter:
    """Token bucket: at most ``rate`` calls per ``per`` seconds."""

    __slots__ = ("_per", "_rate", "_tokens", "_updated")

    def __init__(self, rate: int = 10, per: float = 1.0) -> None:
        self._rate = max(1, int(rate))
        self._per = max(0.001, float(per))
        self._tokens = float(self._rate)
        self._updated = time.monotonic()

    def try_acquire(self) -> bool:
        now = time.monotonic()
        elapsed = now - self._updated
        if elapsed > 0:
            self._updated = now
            self._tokens = min(float(self._rate), self._tokens + elapsed * self._rate / self._per)
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False


class SingleFlight(Generic[K, V]):
    """Collapse concurrent identical async calls into one."""

    __slots__ = ("_calls",)

    def __init__(self) -> None:
        self._calls: dict[Hashable, Any] = {}

    async def run(self, key: Hashable, factory) -> V:
        existing = self._calls.get(key)
        if existing is not None:
            return await _shield(existing)

        import asyncio

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._calls[key] = future
        try:
            result = await factory()
        except BaseException as error:  # noqa: BLE001 - re-raised below
            if not future.done():
                future.set_exception(error)
            future.exception()
            raise
        else:
            if not future.done():
                future.set_result(result)
            return result
        finally:
            self._calls.pop(key, None)


async def _shield(future):
    import asyncio

    return await asyncio.shield(future)
