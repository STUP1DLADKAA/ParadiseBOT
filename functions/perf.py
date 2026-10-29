"""Hardware-aware performance plumbing shared by every feature.

Paradise is a small bot that should still feel instant on a machine with
spare capacity. Three things used to cost real milliseconds on every
request and are fixed here, in one place:

``shared_session``
    One pooled :class:`aiohttp.ClientSession` per purpose instead of a fresh
    TLS handshake per lookup. Sessions are keyed to the running event loop,
    so a rebuilt loop (tests, restarts) never inherits a dead connector.

``image_executor``
    A dedicated thread pool for Pillow/SVG-style rendering, so drawing a
    leaderboard card can never queue behind audio decoding, file I/O or a
    blocking database call on the default executor.

``prewarm``
    Font loading and the large gradient backdrops are the slowest part of a
    first render. They are deterministic, so they are built once in the
    background at start-up instead of on the first person who asks.

Every knob is optional and every failure is non-fatal: without any of this
the bot still works, just less quickly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

# Ceilings keep a 128-core CI box from spawning absurd pools.
_MAX_WORKERS = 32
_MIN_WORKERS = 8
_MAX_RENDER_THREADS = 16
_DEFAULT_HTTP_LIMIT = 100


def cpu_count() -> int:
    """The machine's usable CPU count (never less than 1)."""
    try:
        return max(1, len(os.sched_getaffinity(0)))  # type: ignore[attr-defined]
    except AttributeError:
        return max(1, os.cpu_count() or 1)
    except OSError:  # pragma: no cover - exotic platforms
        return max(1, os.cpu_count() or 1)


def auto_workers() -> int:
    """Default-executor size: roomy enough for audio + rendering + file I/O."""
    return max(_MIN_WORKERS, min(_MAX_WORKERS, cpu_count() * 4))


def auto_render_threads() -> int:
    """Dedicated render threads — cards are CPU-bound and embarrassingly parallel."""
    return max(2, min(_MAX_RENDER_THREADS, cpu_count()))


def auto_http_limit() -> int:
    """Connections to keep open across every outbound service."""
    return max(32, min(256, cpu_count() * 16))


# ---------------------------------------------------------------------------
# Shared HTTP sessions
# ---------------------------------------------------------------------------


class _LoopSessions:
    """Sessions bound to one event loop. Dropped wholesale when the loop changes."""

    __slots__ = ("loop", "sessions")

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.sessions: dict[str, aiohttp.ClientSession] = {}


_state: _LoopSessions | None = None


def _current() -> _LoopSessions:
    global _state
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - called outside asyncio
        loop = asyncio.new_event_loop()
    if _state is None or _state.loop is not loop:
        # A new loop means every connector below is unusable; start clean.
        _state = _LoopSessions(loop)
    return _state


def shared_session(
    name: str,
    *,
    timeout: float = 10.0,
    headers: dict[str, str] | None = None,
    limit: int | None = None,
) -> aiohttp.ClientSession:
    """Return a long-lived pooled session for *name*.

    The session is created lazily inside the running loop, reuses its
    connector (so DNS/TLS work is paid once), and is shared by every caller
    with the same name.
    """
    state = _current()
    session = state.sessions.get(name)
    if session is not None and not getattr(session, "closed", False):
        return session
    connector = aiohttp.TCPConnector(
        limit=int(limit or _DEFAULT_HTTP_LIMIT),
        limit_per_host=max(8, int((limit or _DEFAULT_HTTP_LIMIT) // 4)),
        ttl_dns_cache=600,
        enable_cleanup_closed=True,
    )
    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=timeout, connect=min(5.0, timeout)),
        connector=connector,
        headers=dict(headers or {}),
    )
    state.sessions[name] = session
    return session


async def close_shared_sessions() -> None:
    """Close everything this loop owns. Called on shutdown, never fatal."""
    state = _current()
    sessions = list(state.sessions.values())
    state.sessions.clear()
    for session in sessions:
        try:
            await session.close()
        except Exception:  # noqa: BLE001 - shutdown is best effort
            log.debug("Could not close a shared HTTP session", exc_info=True)


def pool_snapshot() -> dict[str, int]:
    """Diagnostics: how many shared sessions each purpose currently holds."""
    return {name: 1 for name in _current().sessions}


# ---------------------------------------------------------------------------
# Render threads
# ---------------------------------------------------------------------------


class _RenderPool:
    __slots__ = ("executor", "threads")

    def __init__(self) -> None:
        self.executor: ThreadPoolExecutor | None = None
        self.threads = 0

    def get(self, threads: int | None = None) -> ThreadPoolExecutor:
        size = int(threads or 0) or auto_render_threads()
        if self.executor is None:
            self.executor = ThreadPoolExecutor(
                max_workers=size, thread_name_prefix="paradise-render"
            )
            self.threads = size
            log.info("Render pool ready — %d threads", size)
        return self.executor


_render_pool = _RenderPool()


def image_executor(threads: int | None = None) -> ThreadPoolExecutor:
    """The dedicated drawing pool (created on first use)."""
    return _render_pool.get(threads)


async def run_render(func, /, *args: Any, threads: int | None = None):
    """Run a blocking draw call on the render pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(image_executor(threads), func, *args)


async def shutdown_render_pool() -> None:
    """Stop the render pool without waiting on a stuck draw."""
    executor = _render_pool.executor
    _render_pool.executor = None
    if executor is None:
        return
    try:
        executor.shutdown(wait=False, cancel_futures=True)
    except Exception:  # noqa: BLE001
        log.debug("Render pool shutdown failed", exc_info=True)


# ---------------------------------------------------------------------------
# Start-up prewarm
# ---------------------------------------------------------------------------

_prewarm_done_at = 0.0


def prewarm_blocking() -> dict[str, float]:
    """Build the expensive, deterministic assets. Safe to call in a thread.

    Returns timing information for the start-up log; never raises.
    """
    timings: dict[str, float] = {}
    started = time.perf_counter()
    try:
        from functions.dictionary.fonts import FontLibrary

        library = FontLibrary()
        # The roles/sizes the card renderers actually ask for.
        for role in ("display", "semibold", "medium", "regular", "italic"):
            for size in (11, 13, 15, 16, 17, 20, 24, 26, 30, 34, 36, 38):
                library.get(role, size)
        timings["fonts_ms"] = (time.perf_counter() - started) * 1000
    except Exception:  # noqa: BLE001 - fonts are optional
        log.debug("Font prewarm failed", exc_info=True)

    started = time.perf_counter()
    try:
        from functions.voice.render import prewarm as render_prewarm

        render_prewarm()
        timings["cards_ms"] = (time.perf_counter() - started) * 1000
    except Exception:  # noqa: BLE001
        log.debug("Card render prewarm failed", exc_info=True)
    return timings


async def prewarm(*, enabled: bool = True) -> None:
    """Warm fonts and card assets off the hot path."""
    global _prewarm_done_at
    if not enabled:
        return
    started = time.perf_counter()
    try:
        timings = await asyncio.to_thread(prewarm_blocking)
    except Exception:  # noqa: BLE001 - prewarm is a bonus, never a requirement
        log.debug("Prewarm failed", exc_info=True)
        return
    _prewarm_done_at = time.monotonic()
    detail = " ".join(
        f"{name.removesuffix('_ms')}={value:.0f}ms" for name, value in timings.items()
    )
    log.info(
        "Prewarmed in %.0fms%s",
        (time.perf_counter() - started) * 1000,
        f" ({detail})" if detail else "",
    )


__all__ = [
    "auto_http_limit",
    "auto_render_threads",
    "auto_workers",
    "close_shared_sessions",
    "cpu_count",
    "image_executor",
    "pool_snapshot",
    "prewarm",
    "prewarm_blocking",
    "run_render",
    "shared_session",
    "shutdown_render_pool",
]
