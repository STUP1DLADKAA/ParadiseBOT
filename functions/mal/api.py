"""MyAnimeList API v2 client with rate limiting, caching, and error handling.

Uses ``X-MAL-CLIENT-ID`` for public endpoints (search, details) and
``Authorization: Bearer <token>`` for authenticated endpoints (list,
profile, add, update, remove). All network access goes through a shared
``aiohttp.ClientSession`` from :mod:`functions.perf`.

Rate limit: MAL does not publish exact limits, so we use a conservative
minimum interval between requests (1.1s) plus retry-on-429 backoff.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp

from functions.cache import SingleFlight
from functions.perf import shared_session

log = logging.getLogger("paradise.mal.api")

MAL_API_BASE = "https://api.myanimelist.net/v2"

# Conservative minimum interval between requests to MAL.
MAL_MIN_INTERVAL = 1.1  # seconds
# Max retries on 429 (rate limited).
MAL_MAX_RETRIES = 3

# Fields we request for anime search / details — keeps payloads small.
ANIME_FIELDS = (
    "id,title,main_picture,alternative_titles,start_date,end_date,synopsis,"
    "mean,rank,popularity,num_list_users,num_scoring_users,nsfw,media_type,"
    "status,genres,num_episodes,start_season,broadcast,source,"
    "average_episode_duration,rating,pictures,background,my_list_status"
)

# Fields for user anime list entries.
LIST_FIELDS = (
    "list_status{status,num_watched_episodes,num_times_rewatched,"
    "rewatch_value,is_rewatching,score,priority,tags,comments,updated_at},"
    "node{id,title,main_picture,media_type,status,num_episodes,start_date,"
    "end_date,synopsis,mean,rank,popularity,genres,pictures}"
)


class MalApiError(Exception):
    """Raised when the MAL API returns an error."""


class MalUnauthorized(MalApiError):
    """Access token is invalid or expired."""


class MalRateLimited(MalApiError):
    """MAL API rate limit exceeded."""


class MalNotFound(MalApiError):
    """Requested resource does not exist (404)."""


class MalClient:
    """Thin client over MAL API v2 with rate limiting and error handling."""

    def __init__(
        self,
        client_id: str,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.client_id = client_id
        self._session = session
        self._last_request = 0.0
        self._lock = asyncio.Lock()
        self._singleflight = SingleFlight()

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = shared_session("mal")
        return self._session

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    async def _pace(self) -> None:
        """Ensure a minimum gap between consecutive MAL requests."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request
            if elapsed < MAL_MIN_INTERVAL:
                await asyncio.sleep(MAL_MIN_INTERVAL - elapsed)
            self._last_request = time.monotonic()

    # ------------------------------------------------------------------
    # Raw request
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        access_token: str | None = None,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Make a rate-limited request to MAL API v2.

        ``access_token`` is used for authenticated endpoints; otherwise the
        ``X-MAL-CLIENT-ID`` header is used (public, read-only endpoints).
        """
        url = f"{MAL_API_BASE}{path}"
        headers: dict[str, str] = {}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        else:
            headers["X-MAL-CLIENT-ID"] = self.client_id

        await self._pace()

        last_error: Exception | None = None
        for attempt in range(MAL_MAX_RETRIES + 1):
            try:
                async with self.session.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    data=data,
                ) as resp:
                    body = await resp.text()
                    if resp.status == 401:
                        raise MalUnauthorized("token invalid or expired")
                    if resp.status == 403:
                        raise MalApiError("forbidden — possible DoS detection")
                    if resp.status == 404:
                        raise MalNotFound("resource not found")
                    if resp.status == 429:
                        retry_after = float(
                            resp.headers.get("Retry-After", MAL_MIN_INTERVAL * 2)
                        )
                        last_error = MalRateLimited(
                            f"rate limited, retry after {retry_after}s"
                        )
                        if attempt < MAL_MAX_RETRIES:
                            log.warning(
                                "MAL rate limited, retrying in %ss", retry_after
                            )
                            await asyncio.sleep(retry_after)
                            continue
                        raise last_error
                    if resp.status >= 500:
                        if attempt < MAL_MAX_RETRIES:
                            await asyncio.sleep(MAL_MIN_INTERVAL * (attempt + 1))
                            continue
                        raise MalApiError(f"MAL API error: HTTP {resp.status}")
                    if resp.status >= 400:
                        raise MalApiError(f"MAL API error: HTTP {resp.status}")
                    if resp.status == 204:
                        return None
                    try:
                        return await resp.json()
                    except Exception:
                        return {"raw": body}
            except (MalUnauthorized, MalNotFound, MalApiError):
                raise
            except MalRateLimited:
                raise
            except Exception:  # noqa: BLE001 - network issues
                last_error = MalApiError(f"request failed: {last_error!r}")
                if attempt < MAL_MAX_RETRIES:
                    await asyncio.sleep(MAL_MIN_INTERVAL * (attempt + 1))
                    continue
                raise last_error
        raise last_error or MalApiError("request failed")

    # ------------------------------------------------------------------
    # Public endpoints (client-auth, no access token needed)
    # ------------------------------------------------------------------

    async def search_anime(
        self, query: str, limit: int = 10, offset: int = 0
    ) -> dict[str, Any] | None:
        """Search anime by title. Returns the parsed ``data`` + ``paging``."""
        return await self._request(
            "GET",
            "/anime",
            params={
                "q": query,
                "limit": min(max(limit, 1), 100),
                "offset": offset,
                "fields": ANIME_FIELDS,
            },
        )

    async def get_anime(
        self, anime_id: int
    ) -> dict[str, Any] | None:
        """Get details for a single anime by ID."""
        return await self._request(
            "GET",
            f"/anime/{anime_id}",
            params={"fields": ANIME_FIELDS},
        )

    async def get_user(self, access_token: str) -> dict[str, Any] | None:
        """Get the authenticated user's MAL profile."""
        return await self._request(
            "GET",
            "/users/@me",
            access_token=access_token,
            params={"fields": "id,name,anime_statistics"},
        )

    # ------------------------------------------------------------------
    # Authenticated list endpoints
    # ------------------------------------------------------------------

    async def get_animelist(
        self,
        access_token: str,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any] | None:
        """Get the authenticated user's anime list."""
        params: dict[str, Any] = {
            "limit": min(max(limit, 1), 1000),
            "offset": offset,
            "fields": LIST_FIELDS,
        }
        if status:
            params["status"] = status
        return await self._request(
            "GET",
            "/users/@me/animelist",
            access_token=access_token,
            params=params,
        )

    async def update_animelist(
        self,
        access_token: str,
        anime_id: int,
        *,
        status: str | None = None,
        num_watched_episodes: int | None = None,
        score: int | None = None,
        is_rewatching: bool | None = None,
        priority: int | None = None,
        num_times_rewatched: int | None = None,
        rewatch_value: int | None = None,
        tags: str | None = None,
        comments: str | None = None,
    ) -> dict[str, Any] | None:
        """Add or update an anime entry. Only provided fields are sent."""
        data: dict[str, Any] = {}
        if status is not None:
            data["status"] = status
        if num_watched_episodes is not None:
            data["num_watched_episodes"] = num_watched_episodes
        if score is not None:
            data["score"] = score
        if is_rewatching is not None:
            data["is_rewatching"] = "true" if is_rewatching else "false"
        if priority is not None:
            data["priority"] = priority
        if num_times_rewatched is not None:
            data["num_times_rewatched"] = num_times_rewatched
        if rewatch_value is not None:
            data["rewatch_value"] = rewatch_value
        if tags is not None:
            data["tags"] = tags
        if comments is not None:
            data["comments"] = comments
        return await self._request(
            "PUT",
            f"/anime/{anime_id}/my_list_status",
            access_token=access_token,
            data=data,
        )

    async def delete_animelist(
        self, access_token: str, anime_id: int
    ) -> bool:
        """Remove an anime from the user's list. Returns True on success."""
        try:
            await self._request(
                "DELETE",
                f"/anime/{anime_id}/my_list_status",
                access_token=access_token,
            )
            return True
        except MalNotFound:
            return False
