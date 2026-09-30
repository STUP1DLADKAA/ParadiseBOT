"""One small, careful HTTP client shared by the dictionary providers.

Everything the feature does over the network goes through here, which is
the only place that has to get timeouts, retries, size caps, rate limits
and back-off right:

- one pooled :class:`aiohttp.ClientSession`, created lazily, closed once;
- a total timeout on every request (no request can hang the bot);
- one retry for transient failures (network blips, 5xx), never for a 404;
- ``Retry-After`` respected, plus a short local cool-down for a host that
  just rate-limited or fell over, so Paradise stops poking it;
- a hard cap on how many bytes a response may deliver;
- logs that name the host and status, never a URL with credentials in it.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from .cache import RateLimiter

log = logging.getLogger(__name__)

USER_AGENT = "ParadiseBot/1.0 (+https://github.com/STUP1DLADKAA/ParadiseBOT) dictionary"

# Free services deserve a light touch.
DEFAULT_TIMEOUT = 8.0
DEFAULT_RETRIES = 1
FAILURE_COOLDOWN = 45.0
RATE_LIMIT_COOLDOWN = 60.0
MAX_BYTES = 4 * 1024 * 1024


class HttpError(Exception):
    """Any transport-level problem. Always safe to show a generic message for."""


class NotFoundError(HttpError):
    """The upstream said, definitively, that this thing does not exist."""


class ThrottledError(HttpError):
    """We are being rate limited, or are backing off from a host."""


def host_of(url: str) -> str:
    try:
        return urlsplit(url).hostname or "?"
    except ValueError:
        return "?"


class HttpClient:
    """A shared session with sane limits. One per service, closed on shutdown."""

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        rate: int = 8,
        per: float = 1.0,
        user_agent: str = USER_AGENT,
        concurrency: int = 6,
    ) -> None:
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))
        self.user_agent = user_agent
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()
        self._limiter = RateLimiter(rate=rate, per=per)
        self._gate = asyncio.Semaphore(max(1, concurrency))
        self._cooldowns: dict[str, float] = {}
        self._closed = False

    # -- lifecycle ---------------------------------------------------------

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is not None and not self._session.closed:
            return self._session
        async with self._lock:
            if self._session is None or self._session.closed:
                if self._closed:
                    raise HttpError("client is closed")
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                    headers={"User-Agent": self.user_agent},
                    connector=aiohttp.TCPConnector(limit=10, ttl_dns_cache=300),
                )
        return self._session

    async def close(self) -> None:
        self._closed = True
        session, self._session = self._session, None
        if session is not None and not session.closed:
            try:
                await session.close()
            except Exception:  # noqa: BLE001 - shutdown must never raise
                log.debug("Closing dictionary HTTP session failed", exc_info=True)

    # -- back-off bookkeeping ---------------------------------------------

    def cooling_down(self, url: str) -> float:
        """Seconds left before this host should be touched again."""
        until = self._cooldowns.get(host_of(url), 0.0)
        return max(0.0, until - time.monotonic())

    def _cool_down(self, url: str, seconds: float) -> None:
        host = host_of(url)
        self._cooldowns[host] = max(
            self._cooldowns.get(host, 0.0), time.monotonic() + seconds
        )
        if len(self._cooldowns) > 64:  # pragma: no cover - paranoia
            now = time.monotonic()
            self._cooldowns = {k: v for k, v in self._cooldowns.items() if v > now}

    def clear_cooldowns(self) -> None:
        self._cooldowns.clear()

    # -- requests ----------------------------------------------------------

    async def request_bytes(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        max_bytes: int = MAX_BYTES,
        accept_status: tuple[int, ...] = (200,),
    ) -> tuple[bytes, str]:
        """Perform a request and return ``(body, content_type)``."""
        remaining = self.cooling_down(url)
        if remaining > 0:
            raise ThrottledError(f"{host_of(url)} is cooling down for {remaining:.0f}s")
        if not await self._limiter.acquire(timeout=2.0):
            raise ThrottledError("local rate limit reached")

        last_error: Exception = HttpError("request never ran")
        total = float(self.timeout if timeout is None else timeout)
        for attempt in range(self.retries + 1):
            try:
                async with self._gate:
                    session = await self._ensure_session()
                    async with session.request(
                        method,
                        url,
                        params=params,
                        json=json,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=total),
                        allow_redirects=True,
                    ) as response:
                        if response.status in accept_status:
                            body = await self._read_capped(response, max_bytes)
                            content_type = (response.headers.get("Content-Type") or "").split(";")[0]
                            return body, content_type.strip().lower()

                        if response.status == 404:
                            raise NotFoundError(f"{host_of(url)} has no entry")
                        if response.status == 429:
                            retry_after = _retry_after(response.headers.get("Retry-After"))
                            self._cool_down(url, retry_after or RATE_LIMIT_COOLDOWN)
                            raise ThrottledError(f"{host_of(url)} rate limited")
                        if 400 <= response.status < 500:
                            raise HttpError(f"{host_of(url)} refused ({response.status})")
                        raise HttpError(f"{host_of(url)} returned {response.status}")

            except (NotFoundError, ThrottledError):
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, HttpError) as error:
                last_error = error
                if attempt >= self.retries:
                    break
                # Jittered back-off keeps a flapping service from being hammered.
                await asyncio.sleep(0.25 * (attempt + 1) + random.uniform(0, 0.15))

        self._cool_down(url, FAILURE_COOLDOWN)
        log.debug("Request to %s failed: %s", host_of(url), last_error)
        raise HttpError(str(last_error)) from last_error

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
        max_bytes: int = 1024 * 1024,
    ) -> Any:
        body, _content_type = await self.request_bytes(
            "GET", url, params=params, timeout=timeout, max_bytes=max_bytes
        )
        import json as _json

        try:
            return _json.loads(body.decode("utf-8", errors="replace"))
        except ValueError as error:
            raise HttpError(f"{host_of(url)} sent malformed JSON") from error

    async def get_bytes(
        self,
        url: str,
        *,
        timeout: float | None = None,
        max_bytes: int = MAX_BYTES,
        headers: dict[str, str] | None = None,
    ) -> tuple[bytes, str]:
        return await self.request_bytes(
            "GET", url, timeout=timeout, max_bytes=max_bytes, headers=headers
        )

    @staticmethod
    async def _read_capped(response: aiohttp.ClientResponse, max_bytes: int) -> bytes:
        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise HttpError("response too large")
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.content.iter_chunked(64 * 1024):
            size += len(chunk)
            if size > max_bytes:
                raise HttpError("response too large")
            chunks.append(chunk)
        return b"".join(chunks)


def _retry_after(raw: str | None) -> float:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return 0.0
    return min(max(value, 0.0), 300.0)
