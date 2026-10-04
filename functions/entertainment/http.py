"""One careful HTTP client shared by every entertainment source.

Same philosophy as the dictionary's client, tuned for this feature:

- one pooled :class:`aiohttp.ClientSession`, created lazily, closed once;
- a total timeout on every request (no request can hang the bot);
- one retry for transient failures (network blips, 5xx), never for 404;
- ``Retry-After`` respected, plus a local cool-down for a host that just
  rate-limited or fell over, so Paradise stops poking it;
- a hard cap on response size;
- logs that name the host and status, never a full URL with credentials.

Every provider call goes through :meth:`get_json` / :meth:`get_text`, and
every failure surfaces as :class:`HttpError` — callers treat sources as
optional and fall back to the next one.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any
from urllib.parse import urlsplit

import aiohttp

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
IDENTITY = "ParadiseBot/1.0 (+https://github.com/STUP1DLADKAA/ParadiseBOT) entertainment"

DEFAULT_TIMEOUT = 8.0
DEFAULT_RETRIES = 1
FAILURE_COOLDOWN = 45.0
RATE_LIMIT_COOLDOWN = 60.0
MAX_BYTES = 4 * 1024 * 1024


class HttpError(Exception):
    """Any transport-level problem. Callers fall back to another source."""


class NotFoundError(HttpError):
    """The upstream definitively said this thing does not exist."""


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
        rate: int = 10,
        per: float = 1.0,
        concurrency: int = 8,
    ) -> None:
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()
        self._min_interval = per / max(1, rate)
        self._last_request = 0.0
        self._request_lock = asyncio.Lock()
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
                    headers={
                        "User-Agent": USER_AGENT,
                        "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
                        "Accept-Language": "en-US,en;q=0.8",
                    },
                    connector=aiohttp.TCPConnector(limit=16, ttl_dns_cache=300),
                )
        return self._session

    async def close(self) -> None:
        self._closed = True
        session, self._session = self._session, None
        if session is not None and not session.closed:
            try:
                await session.close()
            except Exception:  # noqa: BLE001 - shutdown must never raise
                log.debug("Closing entertainment HTTP session failed", exc_info=True)

    # -- back-off bookkeeping ---------------------------------------------

    def cooling_down(self, url: str) -> float:
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

    # -- politeness: spread requests a little, globally ---------------------

    async def _polite_pause(self) -> None:
        """A tiny global spacing so bursts never look like an attack."""
        async with self._request_lock:
            wait = self._last_request + self._min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(min(wait, 0.5))
            self._last_request = time.monotonic()

    # -- requests ----------------------------------------------------------

    async def request_bytes(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        max_bytes: int = MAX_BYTES,
        accept_status: tuple[int, ...] = (200,),
    ) -> tuple[bytes, str]:
        remaining = self.cooling_down(url)
        if remaining > 0:
            raise ThrottledError(f"{host_of(url)} is cooling down for {remaining:.0f}s")

        last_error: Exception = HttpError("request never ran")
        total = float(self.timeout if timeout is None else timeout)
        for attempt in range(self.retries + 1):
            try:
                await self._polite_pause()
                async with self._gate:
                    session = await self._ensure_session()
                    async with session.request(
                        method,
                        url,
                        params=params,
                        json=json,
                        data=data,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=total),
                        allow_redirects=True,
                    ) as response:
                        if response.status in accept_status:
                            body = await _read_capped(response, max_bytes)
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
                await asyncio.sleep(0.25 * (attempt + 1) + random.uniform(0, 0.15))

        self._cool_down(url, FAILURE_COOLDOWN)
        log.debug("Entertainment request to %s failed: %s", host_of(url), last_error)
        raise HttpError(str(last_error)) from last_error

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
        max_bytes: int = 2 * 1024 * 1024,
    ) -> Any:
        body, _content_type = await self.request_bytes(
            "GET", url, params=params, timeout=timeout, max_bytes=max_bytes
        )
        import json as _json

        try:
            return _json.loads(body.decode("utf-8", errors="replace"))
        except ValueError as error:
            raise HttpError(f"{host_of(url)} sent malformed JSON") from error

    async def get_text(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
        max_bytes: int = 2 * 1024 * 1024,
    ) -> str:
        body, _content_type = await self.request_bytes(
            "GET", url, params=params, timeout=timeout, max_bytes=max_bytes
        )
        return body.decode("utf-8", errors="replace")

    async def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        timeout: float | None = None,
        max_bytes: int = 2 * 1024 * 1024,
    ) -> Any:
        body, _content_type = await self.request_bytes(
            "POST", url, json=payload, timeout=timeout, max_bytes=max_bytes
        )
        import json as _json

        try:
            return _json.loads(body.decode("utf-8", errors="replace"))
        except ValueError as error:
            raise HttpError(f"{host_of(url)} sent malformed JSON") from error


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
