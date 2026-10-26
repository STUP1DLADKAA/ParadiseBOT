"""Supabase-backed voice store — the authoritative database for /vc.

Design summary
--------------
* Supabase PostgreSQL is the source of truth. Every read (`/vc leaderboard`,
  `/vc stats`, daily reports) goes straight to it; there is no long-lived
  leaderboard cache, so manual edits in the Supabase Dashboard show up on
  the very next render.
* Aggregation and sorting happen in PostgreSQL via the helper functions in
  ``supabase/migrations/002_voice_ist_calendar.sql``; Python only formats rows.
* Writes are idempotent (natural-key conflict handling) and atomic
  (``voice_close_active_session`` records + deletes in one transaction).
* Outage safety: when Supabase is unreachable, completed sessions, profiles
  and DM-report marks are queued in a bounded local SQLite *outbox* and
  flushed in order once the connection returns; in-progress sessions are
  mirrored one-row-per-user in the local ``voice_active_sessions`` table so
  restarts never lose live time. Neither mechanism is ever read as a
  leaderboard source — only as a forward queue.
* The bot never creates or alters Supabase tables. It verifies
  ``voice_schema_version()`` at startup and tells the operator exactly what
  to do when the schema is missing or stale.

Secrets: the service key is only ever placed in the client constructor. It
is never logged, never echoed in errors, and never exposed to Discord.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timezone
from typing import Any, Sequence

from .database import VoiceDatabase
from .models import (
    DailyUserStat,
    LeaderboardEntry,
    MemberProfile,
    PersistedActiveSession,
    VoiceSessionRecord,
)
from .store import VoiceSchemaError, VoiceStore, VoiceStoreUnavailable
from .time_windows import ist_date

log = logging.getLogger(__name__)

# Soft limits that keep the fallback queue bounded without ever dropping data.
DEFAULT_OUTBOX_BATCH = 100
DEFAULT_OUTBOX_WARN_ROWS = 25_000
DEFAULT_PROFILE_CHUNK = 50

SCHEMA_MISSING_MESSAGE = (
    "The Paradise Supabase project is missing the voice-activity schema. "
    "In the Dashboard SQL Editor, run migrations/001_voice_activity.sql "
    "followed by migrations/002_voice_ist_calendar.sql (or apply only 002 "
    "when upgrading schema version 1); see supabase/README.md. "
    "The bot retries automatically."
)

# Supabase / PostgREST error types are optional at import time so this module
# (and the offline test suite) stays importable without the client package.
try:  # pragma: no cover - trivial import guard
    import httpx as _httpx
except ImportError:  # pragma: no cover
    _httpx = None

try:  # pragma: no cover - trivial import guard
    from postgrest.exceptions import APIError as _PostgrestAPIError
except ImportError:  # pragma: no cover
    _PostgrestAPIError = None  # type: ignore[assignment]


def _ensure_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return _ensure_aware(dt).isoformat()


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return _ensure_aware(parsed)


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _chunks(items: Sequence, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


class SupabaseVoiceDatabase(VoiceStore):
    """VoiceStore implementation on Supabase PostgreSQL + SQLite fallback."""

    SCHEMA_VERSION = 3
    # v2 predates durable checkpoints. A v3 bot against a v2 database keeps
    # working exactly as before (no periodic checkpointing, close credits from
    # started_at) so deploying the bot before the owner applies 003 is safe.
    MIN_SCHEMA_VERSION = 2

    def __init__(
        self,
        local: VoiceDatabase,
        *,
        client: Any = None,
        url: str = "",
        key: str = "",
        request_timeout: float = 15.0,
        flush_interval: float = 30.0,
        migration_batch_size: int = 500,
        outbox_batch: int = DEFAULT_OUTBOX_BATCH,
        outbox_warn_rows: int = DEFAULT_OUTBOX_WARN_ROWS,
    ) -> None:
        self._local = local
        self._client = client  # injected in tests; created lazily otherwise
        self._url = url
        self._key = key
        self._request_timeout = request_timeout
        self._flush_interval = flush_interval
        self._migration_batch_size = max(1, int(migration_batch_size))
        self._outbox_batch = outbox_batch
        self._outbox_warn_rows = outbox_warn_rows
        self._active_lock = asyncio.Lock()
        self._maintenance_task: asyncio.Task | None = None
        self._stopped = False
        self._schema_ok = False
        self._schema_version = 0
        self._degraded_warned = False
        self._bootstrapped = False
        self._outbox_warned = False
        self._outbox_since_check = 0
        self._outbox_checked_at = 0.0
        # Lightweight request accounting (no payloads, no secrets): lets the
        # maintenance loop report real Supabase traffic and failures.
        self.metrics: dict[str, Any] = {
            "requests": 0,
            "failures": 0,
            "max_latency_ms": 0.0,
            "last_latency_ms": 0.0,
            "by_operation": {},
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def is_bootstrapped(self) -> bool:
        """True once the schema was verified and the outbox fully drained.

        Active-session recovery only consults Supabase after this point so a
        pending outbox entry can never race a recovery close.
        """
        return self._bootstrapped

    async def _ensure_client(self) -> Any:
        if self._client is None:
            if not self._url or not self._key:
                raise VoiceStoreUnavailable(
                    "Supabase credentials are not configured (SUPABASE_URL / SUPABASE_SECRET_KEY)"
                )
            from supabase import AsyncClientOptions, acreate_client

            timeout = self._request_timeout
            options = AsyncClientOptions(
                postgrest_client_timeout=timeout,
                auto_refresh_token=False,
                persist_session=False,
            )
            self._client = await acreate_client(self._url, self._key, options=options)
        return self._client

    def start(self) -> None:
        """Spawn the background bootstrap/flush/migration loop."""
        if self._maintenance_task is None or self._maintenance_task.done():
            self._stopped = False
            self._maintenance_task = asyncio.create_task(
                self._maintenance_loop(), name="voice-supabase-maintenance"
            )

    async def stop(self) -> None:
        self._stopped = True
        task, self._maintenance_task = self._maintenance_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # Best-effort final drain so a clean shutdown leaves nothing queued.
        try:
            await self.flush_outbox()
        except Exception:  # noqa: BLE001
            log.debug("Final outbox drain failed", exc_info=True)

    async def _maintenance_loop(self) -> None:
        # Phase 1: wait for a healthy, schema-correct database.
        while not self._stopped:
            try:
                await self.verify_schema()
                break
            except VoiceSchemaError as err:
                log.error("Voice store disabled: %s", err)
                await asyncio.sleep(60)  # the owner may apply the SQL meanwhile
            except VoiceStoreUnavailable as err:
                log.warning("Supabase unreachable, retrying in 30s: %s", err)
                await asyncio.sleep(30)
        if self._stopped:
            return

        # Phase 2: drain the outbox, then unlock remote recovery reads.
        while not self._stopped:
            try:
                remaining = await self.flush_outbox()
                if remaining == 0:
                    break
            except VoiceStoreUnavailable as err:
                log.warning("Outbox flush postponed (%s); local data is safe", err)
                await asyncio.sleep(30)
        if self._stopped:
            return
        self._bootstrapped = True
        log.info("Voice store online: Supabase is the authoritative voice database")

        # Phase 3: migrate pending SQLite history, then keep everything synced.
        from .migration import VoiceMigrator

        migrator = VoiceMigrator(self._local, self, batch_size=self._migration_batch_size)
        while not self._stopped:
            try:
                if self._local.has_pending_voice_data():
                    await migrator.run()
                remaining = await self.flush_outbox()
                log.debug(
                    "Voice store cycle: %s request(s), %s failure(s), %sms slowest, "
                    "%s outbox row(s) pending",
                    self.metrics["requests"],
                    self.metrics["failures"],
                    self.metrics["max_latency_ms"],
                    remaining,
                )
            except VoiceStoreUnavailable as err:
                log.debug("Voice maintenance cycle skipped: %s", err)
            except Exception:  # noqa: BLE001 - the loop must never die
                log.exception("Voice maintenance cycle failed")
            await asyncio.sleep(self._flush_interval)

    # ------------------------------------------------------------------
    # Low-level request helper (error translation; never logs secrets)
    # ------------------------------------------------------------------

    async def _execute(self, description: str, build):
        client = await self._ensure_client()
        started = time.perf_counter()
        try:
            result = await build(client).execute()
        except VoiceStoreUnavailable:
            self._record_request(description, started, failed=True)
            raise
        except Exception as err:
            # Network failures, 5xx, rate limits, missing schema objects —
            # all treated as "try again later". Messages never contain the key.
            self._record_request(description, started, failed=True)
            raise VoiceStoreUnavailable(f"{description}: {err.__class__.__name__}: {err}") from err
        self._record_request(description, started, failed=False)
        return result

    def _record_request(self, description: str, started: float, *, failed: bool) -> None:
        """Count one request (cheap; never records payloads or secrets)."""
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        stats = self.metrics
        stats["requests"] += 1
        stats["last_latency_ms"] = round(elapsed_ms, 3)
        if elapsed_ms > stats["max_latency_ms"]:
            stats["max_latency_ms"] = round(elapsed_ms, 3)
        counts = stats["by_operation"]
        entry = counts.get(description)
        if entry is None:
            counts[description] = {"requests": 1, "failures": 1 if failed else 0}
        else:
            entry["requests"] += 1
            if failed:
                entry["failures"] += 1
        if failed:
            stats["failures"] += 1

    # ------------------------------------------------------------------
    # Schema verification (never creates or alters anything)
    # ------------------------------------------------------------------

    async def verify_schema(self) -> int:
        try:
            res = await self._execute(
                "verify schema", lambda c: c.rpc("voice_schema_version")
            )
        except VoiceStoreUnavailable as err:
            if "could not find the function" in str(err).lower():
                raise VoiceSchemaError(SCHEMA_MISSING_MESSAGE) from err
            raise
        version = _coerce_int(res.data, default=-1)
        if version < self.MIN_SCHEMA_VERSION:
            raise VoiceSchemaError(
                f"Supabase voice schema version {version} is older than the bot's "
                f"minimum supported version {self.MIN_SCHEMA_VERSION}. Apply "
                "supabase/migrations/002_voice_ist_calendar.sql (and then "
                "003_voice_checkpoint.sql) in the SQL Editor."
            )
        if version > self.SCHEMA_VERSION:
            raise VoiceSchemaError(
                f"Supabase voice schema version {version} is newer than the bot's "
                f"expected version {self.SCHEMA_VERSION}. Update the bot to a "
                "version compatible with the database schema."
            )
        if version < self.SCHEMA_VERSION and not self._degraded_warned:
            self._degraded_warned = True
            log.warning(
                "Supabase voice schema is version %s; the bot needs %s for durable "
                "voice checkpoints. Tracking continues in compatibility mode, but "
                "in-progress time is only durable at session close until you apply "
                "supabase/migrations/003_voice_checkpoint.sql in the SQL Editor.",
                version,
                self.SCHEMA_VERSION,
            )
        self._schema_version = version
        self._schema_ok = True
        return version

    @property
    def schema_version(self) -> int:
        """The verified remote schema version (0 until verified)."""
        return self._schema_version

    @property
    def supports_checkpoints(self) -> bool:
        """True once a schema that can durably checkpoint a live session is verified."""
        return self._schema_ok and self._schema_version >= 3

    # ------------------------------------------------------------------
    # User settings (local SQLite; shared with /settimezone)
    # ------------------------------------------------------------------

    async def get_user_timezone(self, user_id: int) -> str | None:
        return self._local.get_user_timezone(user_id)

    async def set_user_timezone(self, user_id: int, tz_name: str) -> None:
        self._local.set_user_timezone(user_id, tz_name)

    async def get_user_dm_preference(self, user_id: int) -> bool:
        return self._local.get_user_dm_preference(user_id)

    async def set_user_dm_preference(self, user_id: int, enabled: bool) -> None:
        self._local.set_user_dm_preference(user_id, enabled)

    async def get_user_dm_preferences(self, user_ids: Sequence[int]) -> dict[int, bool]:
        # Local SQLite settings; one query instead of one per candidate.
        return self._local.get_user_dm_preferences(list(user_ids))

    # ------------------------------------------------------------------
    # Completed sessions
    # ------------------------------------------------------------------

    async def record_completed_session(
        self,
        guild_id: int,
        user_id: int,
        channel_id: int,
        started_at: datetime,
        ended_at: datetime,
    ) -> int:
        started_at = _ensure_aware(started_at)
        ended_at = _ensure_aware(ended_at)
        if ended_at <= started_at:
            return 0
        if int((ended_at - started_at).total_seconds()) <= 0:
            return 0
        payload = {
            "p_guild_id": int(guild_id),
            "p_user_id": int(user_id),
            "p_channel_id": int(channel_id),
            "p_started_at": started_at.isoformat(),
            "p_ended_at": ended_at.isoformat(),
        }
        try:
            res = await self._execute(
                "record session", lambda c: c.rpc("voice_record_session", payload)
            )
            return _coerce_int(res.data)
        except VoiceStoreUnavailable:
            # Durable local queue; retried idempotently (session identity is
            # the conflict key, so a replay can never double-count).
            self._queue(
                "session",
                {
                    "guild_id": int(guild_id),
                    "user_id": int(user_id),
                    "channel_id": int(channel_id),
                    "started_at": started_at.isoformat(),
                    "ended_at": ended_at.isoformat(),
                },
            )
            return 0

    async def close_active_session(
        self,
        guild_id: int,
        user_id: int,
        ended_at: datetime,
        *,
        fallback_channel_id: int | None = None,
        fallback_started_at: datetime | None = None,
        fallback_credited_until: datetime | None = None,
    ) -> int | None:
        ended_at = _ensure_aware(ended_at)

        def _credit_from(started: datetime, cursor: datetime | None) -> datetime:
            """Never re-credit an interval a checkpoint already committed."""
            if cursor is not None and _ensure_aware(cursor) > started:
                return _ensure_aware(cursor)
            return started

        def _fallback_values() -> tuple[datetime, int] | None:
            if fallback_started_at is not None and fallback_channel_id is not None:
                started = _credit_from(
                    _ensure_aware(fallback_started_at), fallback_credited_until
                )
                return started, int(fallback_channel_id)
            return None

        async with self._active_lock:
            params = {
                "p_guild_id": int(guild_id),
                "p_user_id": int(user_id),
                "p_ended_at": ended_at.isoformat(),
            }
            try:
                res = await self._execute(
                    "close active session",
                    lambda c: c.rpc("voice_close_active_session", params),
                )
            except VoiceStoreUnavailable:
                # Outage: queue the close from the freshest known state.
                local = self._local.get_active_session(guild_id, user_id)
                if local is not None:
                    started = _credit_from(local.started_at, local.credited_until)
                    channel = local.channel_id
                else:
                    fb = _fallback_values()
                    if fb is None:
                        self._local.delete_active_session(guild_id, user_id)
                        self._queue(
                            "active_delete",
                            {"guild_id": guild_id, "user_id": user_id, "started_before": None},
                        )
                        return None
                    started, channel = fb
                self._queue(
                    "session",
                    {
                        "guild_id": int(guild_id),
                        "user_id": int(user_id),
                        "channel_id": int(channel),
                        "started_at": _iso(started),
                        "ended_at": ended_at.isoformat(),
                    },
                )
                self._queue(
                    "active_delete",
                    {
                        "guild_id": int(guild_id),
                        "user_id": int(user_id),
                        "started_before": _iso(started),
                    },
                )
                self._local.delete_active_session(guild_id, user_id)
                return None

            data = res.data
            if data is not None:
                # Atomic close succeeded; the local mirror is redundant now.
                self._local.delete_active_session(guild_id, user_id)
                return _coerce_int(data)

            # No active row in Supabase. Either it was never uploaded (check
            # the local mirror) or it only ever existed in tracker memory.
            local = self._local.get_active_session(guild_id, user_id)
            if local is not None and not local.is_afk:
                recorded = await self.record_completed_session(
                    guild_id,
                    user_id,
                    local.channel_id,
                    _credit_from(local.started_at, local.credited_until),
                    ended_at,
                )
                await self.delete_active_session(
                    guild_id, user_id, started_before=local.started_at
                )
                return recorded
            if local is not None:  # AFK mirror: nothing to record
                await self.delete_active_session(
                    guild_id, user_id, started_before=local.started_at
                )
                return 0
            fb = _fallback_values()
            if fb is None:
                return None
            started, channel = fb
            recorded = await self.record_completed_session(
                guild_id, user_id, channel, started, ended_at
            )
            await self.delete_active_session(guild_id, user_id, started_before=started)
            return recorded

    # ------------------------------------------------------------------
    # Active sessions
    # ------------------------------------------------------------------

    async def upsert_active_session(
        self,
        guild_id: int,
        user_id: int,
        channel_id: int,
        started_at: datetime,
        last_seen_at: datetime,
        is_afk: bool = False,
    ) -> None:
        await self.heartbeat_active_sessions(
            [
                PersistedActiveSession(
                    guild_id=guild_id,
                    user_id=user_id,
                    channel_id=channel_id,
                    started_at=started_at,
                    last_seen_at=last_seen_at,
                    is_afk=is_afk,
                )
            ]
        )

    async def heartbeat_active_sessions(
        self, sessions: Sequence[PersistedActiveSession]
    ) -> dict[tuple[int, int], tuple[datetime, datetime | None]]:
        """Bump liveness for many sessions and report the durable values back.

        Uses ``voice_heartbeat_active_v2`` when the schema has it (v3+), which
        returns each row's authoritative ``started_at`` / ``credited_until`` so
        the tracker can reconcile its cache every cycle. On a v2 database it
        falls back to the original count-returning function and reports
        nothing.
        """
        if not sessions:
            return {}
        rows = [
            {
                "guild_id": int(s.guild_id),
                "user_id": int(s.user_id),
                "channel_id": int(s.channel_id),
                "started_at": _iso(s.started_at),
                "last_seen_at": _iso(s.last_seen_at),
                "is_afk": bool(s.is_afk),
            }
            for s in sessions
        ]
        use_v2 = self.supports_checkpoints
        fn = "voice_heartbeat_active_v2" if use_v2 else "voice_heartbeat_active"
        async with self._active_lock:
            try:
                res = await self._execute(
                    "heartbeat active sessions",
                    lambda c: c.rpc(fn, {"p_rows": rows}),
                )
            except VoiceStoreUnavailable:
                # Bounded local mirror (one row per user, last-write-wins).
                # Never queued to the outbox: heartbeats are high-volume and
                # the mirror already holds everything recovery needs.
                for s in sessions:
                    self._local.upsert_active_session(
                        s.guild_id,
                        s.user_id,
                        s.channel_id,
                        s.started_at,
                        s.last_seen_at,
                        s.is_afk,
                        credited_until=s.credited_until,
                    )
                return {}
            # Commit acknowledged: drop the now-redundant local mirrors.
            for s in sessions:
                self._local.delete_active_session(s.guild_id, s.user_id)

        known: dict[tuple[int, int], tuple[datetime, datetime | None]] = {}
        if not use_v2:
            return known
        for row in res.data or []:
            started = _parse_dt(row.get("o_started_at"))
            if started is None:
                continue
            known[
                (_coerce_int(row.get("o_guild_id")), _coerce_int(row.get("o_user_id")))
            ] = (started, _parse_dt(row.get("o_cursor")))
        return known

    async def checkpoint_active_sessions(
        self,
        sessions: Sequence[PersistedActiveSession],
        up_to: datetime,
    ) -> dict[tuple[int, int], tuple[datetime, datetime]]:
        """Atomically credit ``[cursor, up_to)`` and advance the cursor.

        One RPC for the whole batch. ``voice_checkpoint_active_sessions`` locks
        each row, credits only the part that has not been credited yet, and
        advances the cursor in the same transaction — so a replay credits
        nothing, an out-of-order call cannot rewind anything, and the cursor
        never advances for time that was not saved.

        Returns ``{(guild_id, user_id): (new_cursor, row_started_at)}``. The
        echoed ``started_at`` lets the tracker adopt an administrative
        Dashboard correction in the very same round trip.
        """
        if not sessions or not self.supports_checkpoints:
            return {}
        up_to = _ensure_aware(up_to)
        rows = [
            {
                "guild_id": int(s.guild_id),
                "user_id": int(s.user_id),
                "up_to": _iso(up_to),
            }
            for s in sessions
            if not s.is_afk  # AFK time is never credited (policy unchanged)
        ]
        if not rows:
            return {}
        async with self._active_lock:
            try:
                res = await self._execute(
                    "checkpoint active sessions",
                    lambda c: c.rpc("voice_checkpoint_active_sessions", {"p_rows": rows}),
                )
            except VoiceStoreUnavailable:
                # Re-raised rather than swallowed: nothing was credited and no
                # cursor moved, so the caller must be able to tell "failed"
                # from "nothing to do" and report it. The next checkpoint
                # simply covers a longer interval, so no time is lost and none
                # can be double credited.
                raise

        cursors: dict[tuple[int, int], tuple[datetime, datetime]] = {}
        credited_total = 0
        for row in res.data or []:
            key = (_coerce_int(row.get("o_guild_id")), _coerce_int(row.get("o_user_id")))
            cursor = _parse_dt(row.get("o_cursor"))
            if cursor is None:
                continue
            cursors[key] = (cursor, _parse_dt(row.get("o_started_at")) or cursor)
            credited_total += _coerce_int(row.get("o_credited_seconds"))
        if credited_total:
            log.debug(
                "Voice checkpoint credited %ss across %s live session(s)",
                credited_total,
                len(cursors),
            )
        # The local mirror only exists while Supabase is unreachable; once a
        # checkpoint lands remotely the mirror is redundant and could otherwise
        # resurrect a stale cursor during recovery.
        for key in cursors:
            self._local.delete_active_session(key[0], key[1])
        return cursors

    async def delete_active_session(
        self, guild_id: int, user_id: int, *, started_before: datetime | None = None
    ) -> None:
        threshold = _iso(started_before) if started_before is not None else None
        async with self._active_lock:
            local = self._local.get_active_session(guild_id, user_id)
            if local is not None and started_before is not None and local.started_at > started_before:
                # The local mirror already belongs to a newer session.
                pass
            else:
                self._local.delete_active_session(guild_id, user_id)
            params = {
                "p_guild_id": int(guild_id),
                "p_user_id": int(user_id),
                "p_started_before": threshold,
            }
            try:
                await self._execute(
                    "delete active session",
                    lambda c: c.rpc("voice_delete_active_if_older", params),
                )
            except VoiceStoreUnavailable:
                self._queue(
                    "active_delete",
                    {"guild_id": int(guild_id), "user_id": int(user_id), "started_before": threshold},
                )

    async def get_active_sessions(self) -> list[PersistedActiveSession]:
        async with self._active_lock:
            local_rows = self._local.get_active_sessions()
            if not self._bootstrapped:
                # Before the outbox is drained, remote rows could belong to
                # sessions whose close is still queued locally — reading them
                # now could double-count. Recovery uses local mirrors only.
                return local_rows
            # credited_until only exists from schema v3; asking a v2 database
            # for it would fail the whole recovery read, so select it only
            # when the verified schema actually has it.
            columns = "guild_id,user_id,channel_id,started_at,last_seen_at,is_afk"
            if self.supports_checkpoints:
                columns += ",credited_until"
            try:
                res = await self._execute(
                    "get active sessions",
                    lambda c: c.table("voice_active_sessions").select(columns)
                )
            except VoiceStoreUnavailable as err:
                log.warning("Active session recovery from Supabase skipped: %s", err)
                return local_rows

        merged: dict[tuple[int, int], PersistedActiveSession] = {}
        for row in local_rows:
            merged[(row.guild_id, row.user_id)] = row
        for row in res.data or []:
            started = _parse_dt(row.get("started_at"))
            last_seen = _parse_dt(row.get("last_seen_at"))
            if started is None or last_seen is None:
                continue
            key = (_coerce_int(row.get("guild_id")), _coerce_int(row.get("user_id")))
            candidate = PersistedActiveSession(
                guild_id=key[0],
                user_id=key[1],
                channel_id=_coerce_int(row.get("channel_id")),
                started_at=started,
                last_seen_at=last_seen,
                is_afk=bool(row.get("is_afk")),
                credited_until=_parse_dt(row.get("credited_until")),
            )
            existing = merged.get(key)
            if existing is None or candidate.last_seen_at >= existing.last_seen_at:
                merged[key] = candidate
        return list(merged.values())

    # ------------------------------------------------------------------
    # Member profiles
    # ------------------------------------------------------------------

    async def upsert_member_profile(
        self,
        guild_id: int,
        user_id: int,
        display_name: str,
        username: str,
        avatar_url: str | None,
    ) -> None:
        row = {
            "guild_id": int(guild_id),
            "user_id": int(user_id),
            "display_name": str(display_name),
            "username": str(username),
            "avatar_url": avatar_url or "",
        }
        try:
            await self._execute(
                "upsert profile",
                lambda c: c.rpc("voice_upsert_profiles", {"p_rows": [row]}),
            )
        except VoiceStoreUnavailable:
            self._queue("profile", row)

    async def upsert_member_profiles(self, rows: Sequence[MemberProfile]) -> None:
        """Persist many identities in ONE batched RPC (chunked, never lossy)."""
        payload = [
            {
                "guild_id": int(row.guild_id),
                "user_id": int(row.user_id),
                "display_name": str(row.display_name),
                "username": str(row.username),
                "avatar_url": row.avatar_url or "",
            }
            for row in rows
            if row is not None
        ]
        if not payload:
            return
        for chunk in _chunks(payload, DEFAULT_PROFILE_CHUNK):
            try:
                await self._execute(
                    "upsert profiles",
                    lambda c, ch=chunk: c.rpc("voice_upsert_profiles", {"p_rows": ch}),
                )
            except VoiceStoreUnavailable:
                # Durable per-row outbox entries; replay is idempotent.
                for row in chunk:
                    self._queue("profile", row)

    async def get_member_profiles(
        self,
        guild_id: int,
        user_ids: Sequence[int] | None = None,
    ) -> dict[int, MemberProfile]:
        profiles: dict[int, MemberProfile] = {}
        if user_ids is None:
            res = await self._execute(
                "get profiles",
                lambda c: c.table("voice_member_profiles")
                .select("guild_id,user_id,display_name,username,avatar_url")
                .eq("guild_id", int(guild_id))
            )
            self._collect_profiles(res.data or [], profiles)
            return profiles

        ids = [int(u) for u in user_ids]
        if not ids:
            return {}
        for chunk in _chunks(ids, 50):
            res = await self._execute(
                "get profiles",
                lambda c, ch=chunk: c.table("voice_member_profiles")
                .select("guild_id,user_id,display_name,username,avatar_url")
                .eq("guild_id", int(guild_id))
                .in_("user_id", ch)
            )
            self._collect_profiles(res.data or [], profiles)
        return profiles

    @staticmethod
    def _collect_profiles(rows: Sequence[dict], out: dict[int, MemberProfile]) -> None:
        for row in rows:
            try:
                out[int(row["user_id"])] = MemberProfile(
                    guild_id=int(row.get("guild_id", 0)),
                    user_id=int(row["user_id"]),
                    display_name=str(row.get("display_name") or ""),
                    username=str(row.get("username") or ""),
                    avatar_url=row.get("avatar_url") or None,
                )
            except (KeyError, TypeError, ValueError):
                continue

    # ------------------------------------------------------------------
    # Leaderboard reads (straight from Supabase — no cache)
    # ------------------------------------------------------------------

    async def get_leaderboard_data(
        self,
        guild_id: int,
        period: str = "today",
        target_date: date | None = None,
    ) -> list[LeaderboardEntry]:
        today = target_date or ist_date()
        params = {
            "p_guild_id": int(guild_id),
            "p_period": str(period),
            "p_target_date": today.isoformat(),
        }
        res = await self._execute(
            "leaderboard", lambda c: c.rpc("voice_leaderboard", params)
        )
        entries: list[LeaderboardEntry] = []
        for rank, row in enumerate(res.data or [], 1):
            try:
                entries.append(
                    LeaderboardEntry(
                        user_id=int(row["user_id"]),
                        total_seconds=_coerce_int(row.get("total_seconds")),
                        rank=rank,
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return entries

    async def get_user_breakdown(
        self,
        guild_id: int,
        user_id: int,
        target_date: date | None = None,
    ) -> dict[str, int]:
        today = target_date or ist_date()
        params = {
            "p_guild_id": int(guild_id),
            "p_user_id": int(user_id),
            "p_target_date": today.isoformat(),
        }
        res = await self._execute(
            "user breakdown", lambda c: c.rpc("voice_user_breakdown", params)
        )
        rows = res.data or []
        if not rows:
            return {
                "today": 0,
                "yesterday": 0,
                "week": 0,
                "month": 0,
                "all_time": 0,
            }
        row = rows[0]
        return {
            "today": _coerce_int(row.get("today_seconds")),
            "yesterday": _coerce_int(row.get("yesterday_seconds")),
            "week": _coerce_int(row.get("week_seconds")),
            "month": _coerce_int(row.get("month_seconds")),
            "all_time": _coerce_int(row.get("all_time_seconds")),
        }

    async def get_daily_server_total(self, guild_id: int, date_str: str) -> int:
        res = await self._execute(
            "daily server total",
            lambda c: c.rpc(
                "voice_daily_server_total",
                {"p_guild_id": int(guild_id), "p_day": date_str},
            ),
        )
        return _coerce_int(res.data)

    async def get_daily_guild_user_records(self, date_str: str) -> list[DailyUserStat]:
        res = await self._execute(
            "daily records", lambda c: c.rpc("voice_daily_records", {"p_day": date_str})
        )
        records: list[DailyUserStat] = []
        for row in res.data or []:
            try:
                records.append(
                    DailyUserStat(
                        guild_id=int(row["guild_id"]),
                        user_id=int(row["user_id"]),
                        date_str=str(row.get("date", ""))[:10],
                        duration_seconds=_coerce_int(row.get("duration_seconds")),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return records

    # ------------------------------------------------------------------
    # Daily DM report state
    # ------------------------------------------------------------------

    async def has_dm_report_sent(self, date_str: str, guild_id: int, user_id: int) -> bool:
        res = await self._execute(
            "check dm report",
            lambda c: c.table("voice_dm_reports")
            .select("user_id")
            .eq("date", date_str)
            .eq("guild_id", int(guild_id))
            .eq("user_id", int(user_id))
        )
        return bool(res.data)

    async def get_sent_dm_reports(
        self, date_str: str, guild_id: int, user_ids: Sequence[int]
    ) -> set[int]:
        """One batched lookup per guild/day instead of one query per user."""
        ids = [int(u) for u in user_ids]
        if not ids:
            return set()
        sent: set[int] = set()
        for chunk in _chunks(ids, DEFAULT_PROFILE_CHUNK * 4):
            payload = [
                {"date": date_str, "guild_id": int(guild_id), "user_id": user_id}
                for user_id in chunk
            ]
            res = await self._execute(
                "fetch dm report batch",
                lambda c, p=payload: c.rpc(
                    "voice_fetch_dm_report_batch", {"p_keys": p}
                ),
            )
            for row in res.data or []:
                try:
                    sent.add(int(row["user_id"]))
                except (KeyError, TypeError, ValueError):
                    continue
        return sent

    async def mark_dm_report_sent(self, date_str: str, guild_id: int, user_id: int) -> None:
        row = {
            "date": date_str,
            "guild_id": int(guild_id),
            "user_id": int(user_id),
        }
        try:
            await self._execute(
                "mark dm report",
                lambda c: c.table("voice_dm_reports")
                .upsert([row], on_conflict="date,guild_id,user_id", ignore_duplicates=True)
            )
        except VoiceStoreUnavailable:
            # Durable mark (the unique key makes replays harmless): without a
            # durable mark we never send, so duplicates are impossible.
            self._queue("dm_report", row)

    # ------------------------------------------------------------------
    # Local outbox
    # ------------------------------------------------------------------

    def _queue(self, kind: str, payload: dict) -> None:
        self._local.queue_outbox(kind, payload)
        # Counting the queue on every queued row is a full table scan during
        # exactly the outage window we care about. Check at most once per 25
        # rows or 30 seconds.
        self._outbox_since_check += 1
        now = time.monotonic()
        if self._outbox_since_check < 25 and now - self._outbox_checked_at < 30.0:
            return
        self._outbox_since_check = 0
        self._outbox_checked_at = now
        pending = self._local.count_outbox()
        if pending > self._outbox_warn_rows and not self._outbox_warned:
            log.error(
                "Voice outbox holds %s pending rows — Supabase has been unreachable "
                "for a while. Nothing is dropped; rows flush automatically on "
                "recovery. Prolonged outages should be investigated.",
                pending,
            )
            self._outbox_warned = True
        elif pending <= self._outbox_warn_rows // 2:
            self._outbox_warned = False

    async def flush_outbox(self) -> int:
        """Apply queued operations in order; returns rows still pending.

        Strict ordering is preserved because session and guarded-delete rows
        can depend on each other. Adjacent *idempotent* profile and DM-report
        rows are merged into one batched request each (their SQL functions are
        already set-based), so draining a large outage costs far fewer round
        trips without changing what is written.
        """
        while True:
            batch = self._local.fetch_outbox_batch(self._outbox_batch)
            if not batch:
                break
            done_ids: list[int] = []
            failed = False
            position = 0
            while position < len(batch):
                op_id, kind, payload = batch[position]
                try:
                    if kind in ("profile", "dm_report"):
                        group: list[tuple[int, dict]] = []
                        while position < len(batch) and batch[position][1] == kind:
                            group.append((batch[position][0], batch[position][2]))
                            position += 1
                        await self._apply_outbox_group(kind, [item[1] for item in group])
                        done_ids.extend(op_id for op_id, _ in group)
                    else:
                        await self._apply_outbox_op(kind, payload)
                        done_ids.append(op_id)
                        position += 1
                except VoiceStoreUnavailable:
                    failed = True  # keep strict order; retry next cycle
                    break
                except Exception:  # noqa: BLE001 - never lose the queue loop
                    log.exception("Outbox operation %s (%s) failed unexpectedly", op_id, kind)
                    failed = True
                    break
            if done_ids:
                self._local.delete_outbox_rows(done_ids)
                log.debug("Flushed %s voice outbox rows", len(done_ids))
            if failed or len(done_ids) < len(batch):
                break
        return self._local.count_outbox()

    async def _apply_outbox_group(self, kind: str, payloads: list[dict]) -> None:
        """Apply a run of same-kind idempotent outbox rows in one request."""
        if kind == "profile":
            rows = [
                {
                    "guild_id": int(item["guild_id"]),
                    "user_id": int(item["user_id"]),
                    "display_name": item["display_name"],
                    "username": item["username"],
                    "avatar_url": item.get("avatar_url") or "",
                }
                for item in payloads
            ]
            for chunk in _chunks(rows, DEFAULT_PROFILE_CHUNK):
                await self._execute(
                    "outbox profile",
                    lambda c, ch=chunk: c.rpc("voice_upsert_profiles", {"p_rows": ch}),
                )
            return
        rows = [
            {
                "date": item["date"],
                "guild_id": int(item["guild_id"]),
                "user_id": int(item["user_id"]),
            }
            for item in payloads
        ]
        for chunk in _chunks(rows, DEFAULT_OUTBOX_BATCH):
            await self._execute(
                "outbox dm report",
                lambda c, ch=chunk: c.table("voice_dm_reports")
                .upsert(ch, on_conflict="date,guild_id,user_id", ignore_duplicates=True),
            )

    async def _apply_outbox_op(self, kind: str, payload: dict) -> None:
        if kind == "session":
            await self._execute(
                "outbox session",
                lambda c: c.rpc(
                    "voice_record_session",
                    {
                        "p_guild_id": int(payload["guild_id"]),
                        "p_user_id": int(payload["user_id"]),
                        "p_channel_id": int(payload["channel_id"]),
                        "p_started_at": payload["started_at"],
                        "p_ended_at": payload["ended_at"],
                    },
                ),
            )
        elif kind == "profile":
            await self._execute(
                "outbox profile",
                lambda c: c.rpc("voice_upsert_profiles", {"p_rows": [payload]}),
            )
        elif kind == "dm_report":
            row = {
                "date": payload["date"],
                "guild_id": int(payload["guild_id"]),
                "user_id": int(payload["user_id"]),
            }
            await self._execute(
                "outbox dm report",
                lambda c: c.table("voice_dm_reports")
                .upsert([row], on_conflict="date,guild_id,user_id", ignore_duplicates=True)
            )
        elif kind == "active_delete":
            await self._execute(
                "outbox active delete",
                lambda c: c.rpc(
                    "voice_delete_active_if_older",
                    {
                        "p_guild_id": int(payload["guild_id"]),
                        "p_user_id": int(payload["user_id"]),
                        "p_started_before": payload.get("started_before"),
                    },
                ),
            )
        else:  # pragma: no cover - defensive
            log.error("Unknown outbox kind %r; dropping row", kind)

    # ------------------------------------------------------------------
    # Migration primitives (used by functions/voice/migration.py)
    # Every upload is idempotent; the migrator verifies before deleting.
    # ------------------------------------------------------------------

    async def upload_migration_sessions(self, rows: Sequence[VoiceSessionRecord]) -> None:
        payload = [
            {
                "guild_id": int(r.guild_id),
                "user_id": int(r.user_id),
                "channel_id": int(r.channel_id),
                "started_at": _iso(r.started_at),
                "ended_at": _iso(r.ended_at),
                "duration_seconds": int(r.duration_seconds),
                "source_id": int(r.id),
            }
            for r in rows
        ]
        if not payload:
            return
        await self._execute(
            "upload sessions",
            lambda c: c.table("voice_sessions")
            .upsert(payload, on_conflict="source_id", ignore_duplicates=True)
        )

    async def fetch_migration_sessions(self, source_ids: Sequence[int]) -> list[dict]:
        out: list[dict] = []
        for chunk in _chunks([int(i) for i in source_ids], 200):
            if not chunk:
                continue
            res = await self._execute(
                "verify sessions",
                lambda c, ch=chunk: c.rpc("voice_fetch_sessions_batch", {"p_source_ids": ch}),
            )
            out.extend(res.data or [])
        return out

    async def merge_migration_daily(self, rows: Sequence[DailyUserStat]) -> list[dict]:
        payload = [
            {
                "guild_id": int(r.guild_id),
                "user_id": int(r.user_id),
                "date": str(r.date_str)[:10],
                "duration_seconds": int(r.duration_seconds),
            }
            for r in rows
        ]
        if not payload:
            return []
        res = await self._execute(
            "merge daily batch",
            lambda c: c.rpc("voice_merge_daily_batch", {"p_rows": payload}),
        )
        return list(res.data or [])

    async def fetch_migration_daily(self, keys: Sequence[tuple[int, int, str]]) -> list[dict]:
        payload = [
            {"guild_id": int(g), "user_id": int(u), "date": str(d)[:10]}
            for g, u, d in keys
        ]
        if not payload:
            return []
        res = await self._execute(
            "verify daily batch",
            lambda c: c.rpc("voice_fetch_daily_batch", {"p_rows": payload}),
        )
        return list(res.data or [])

    async def upload_migration_profiles(
        self, rows: Sequence[tuple[MemberProfile, str]]
    ) -> None:
        payload = [
            {
                "guild_id": int(p.guild_id),
                "user_id": int(p.user_id),
                "display_name": p.display_name,
                "username": p.username,
                "avatar_url": p.avatar_url or "",
                "updated_at": updated_at,
            }
            for p, updated_at in rows
        ]
        if not payload:
            return
        await self._execute(
            "upload profiles",
            lambda c: c.rpc("voice_upsert_profiles", {"p_rows": payload}),
        )

    async def fetch_migration_profiles(
        self, keys: Sequence[tuple[int, int]]
    ) -> list[dict]:
        payload = [
            {"guild_id": int(g), "user_id": int(u)} for g, u in keys
        ]
        if not payload:
            return []
        res = await self._execute(
            "verify profiles",
            lambda c: c.rpc("voice_fetch_profiles_batch", {"p_keys": payload}),
        )
        return list(res.data or [])

    async def upload_migration_active(self, rows: Sequence[PersistedActiveSession]) -> None:
        payload = [
            {
                "guild_id": int(s.guild_id),
                "user_id": int(s.user_id),
                "channel_id": int(s.channel_id),
                "started_at": _iso(s.started_at),
                "last_seen_at": _iso(s.last_seen_at),
                "is_afk": bool(s.is_afk),
            }
            for s in rows
        ]
        if not payload:
            return
        async with self._active_lock:
            await self._execute(
                "upload active sessions",
                lambda c: c.rpc("voice_heartbeat_active", {"p_rows": payload}),
            )

    async def fetch_migration_active(
        self, keys: Sequence[tuple[int, int]]
    ) -> list[dict]:
        payload = [
            {"guild_id": int(g), "user_id": int(u)} for g, u in keys
        ]
        if not payload:
            return []
        res = await self._execute(
            "verify active sessions",
            lambda c: c.rpc("voice_fetch_active_batch", {"p_keys": payload}),
        )
        return list(res.data or [])

    async def upload_migration_dm_reports(
        self, rows: Sequence[tuple[str, int, int, str]]
    ) -> None:
        payload = [
            {"date": str(d)[:10], "guild_id": int(g), "user_id": int(u)}
            for d, g, u, _sent_at in rows
        ]
        if not payload:
            return
        await self._execute(
            "upload dm reports",
            lambda c: c.table("voice_dm_reports")
            .upsert(payload, on_conflict="date,guild_id,user_id", ignore_duplicates=True)
        )

    async def fetch_migration_dm_reports(
        self, keys: Sequence[tuple[str, int, int]]
    ) -> list[dict]:
        payload = [
            {"date": str(d)[:10], "guild_id": int(g), "user_id": int(u)}
            for d, g, u in keys
        ]
        if not payload:
            return []
        res = await self._execute(
            "verify dm reports",
            lambda c: c.rpc("voice_fetch_dm_report_batch", {"p_keys": payload}),
        )
        return list(res.data or [])
