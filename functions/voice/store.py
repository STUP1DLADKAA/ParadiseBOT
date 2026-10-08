"""The storage interface used by the voice tracker.

``VoiceTracker`` talks to a ``VoiceStore`` and never to SQL of any flavour.
Production wires :class:`functions.voice.supabase_database.SupabaseVoiceDatabase`
(Supabase PostgreSQL is the source of truth) into it; a small SQLite-backed
implementation exists for tests and as a temporary fallback
(:class:`functions.voice.sqlite_store.LocalVoiceStore`).

All methods are ``async`` so a network-backed implementation can exist
without the tracker caring. Methods that hit the authoritative database
raise :class:`VoiceStoreUnavailable` when it cannot be reached — the tracker
and commands translate that into friendly behaviour instead of crashes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime
from typing import Sequence

from .models import (
    DailyUserStat,
    LeaderboardEntry,
    MemberProfile,
    PersistedActiveSession,
)


class VoiceStoreError(Exception):
    """Base class for voice storage problems."""


class VoiceStoreUnavailable(VoiceStoreError):
    """The authoritative voice database (Supabase) is temporarily unreachable."""


class VoiceSchemaError(VoiceStoreError):
    """The remote schema is missing or older than the bot expects."""


class VoiceStore(ABC):
    """Interface between the tracker and wherever voice data lives."""

    # ------------------------------------------------------------------
    # User settings (timezone + DM report preference). These are local
    # concerns shared with other commands and intentionally stay in SQLite.
    # ------------------------------------------------------------------

    @abstractmethod
    async def get_user_timezone(self, user_id: int) -> str | None: ...

    @abstractmethod
    async def set_user_timezone(self, user_id: int, tz_name: str) -> None: ...

    @abstractmethod
    async def get_user_dm_preference(self, user_id: int) -> bool: ...

    @abstractmethod
    async def set_user_dm_preference(self, user_id: int, enabled: bool) -> None: ...

    # ------------------------------------------------------------------
    # Completed sessions (history + daily aggregates)
    # ------------------------------------------------------------------

    @abstractmethod
    async def record_completed_session(
        self,
        guild_id: int,
        user_id: int,
        channel_id: int,
        started_at: datetime,
        ended_at: datetime,
    ) -> int:
        """Record a completed session (with midnight splitting).

        Returns the number of seconds actually recorded; 0 when the session
        was a duplicate/already known. Implementations must be idempotent
        and must never lose the data on a transient outage (bounded local
        fallback), even though they may raise nothing on failure.
        """

    @abstractmethod
    async def close_active_session(
        self,
        guild_id: int,
        user_id: int,
        ended_at: datetime,
        *,
        fallback_channel_id: int | None = None,
        fallback_started_at: datetime | None = None,
    ) -> int | None:
        """Atomically close a persisted active session.

        Uses the persisted row's own started_at/channel, records the session
        and removes the row in one transaction. Returns the recorded seconds
        (0 for AFK or duplicates) or ``None`` when no active row exists.
        ``fallback_*`` values (from the in-memory session) are used only if
        the row cannot be found or the store is unreachable.
        """

    # ------------------------------------------------------------------
    # Active sessions (restart/crash resilience)
    # ------------------------------------------------------------------

    @abstractmethod
    async def upsert_active_session(
        self,
        guild_id: int,
        user_id: int,
        channel_id: int,
        started_at: datetime,
        last_seen_at: datetime,
        is_afk: bool = False,
    ) -> None:
        """Persist (or heartbeat) one in-progress session (one compact row)."""

    @abstractmethod
    async def heartbeat_active_sessions(
        self, sessions: Sequence[PersistedActiveSession]
    ) -> None:
        """Batched heartbeat: bump last_seen_at for many sessions at once."""

    @abstractmethod
    async def delete_active_session(
        self, guild_id: int, user_id: int, *, started_before: datetime | None = None
    ) -> None:
        """Drop a persisted active row (guarded to never delete a newer session)."""

    @abstractmethod
    async def get_active_sessions(self) -> list[PersistedActiveSession]:
        """Every in-progress session across all guilds (best effort)."""

    # ------------------------------------------------------------------
    # Member identity persistence
    # ------------------------------------------------------------------

    @abstractmethod
    async def upsert_member_profile(
        self,
        guild_id: int,
        user_id: int,
        display_name: str,
        username: str,
        avatar_url: str | None,
    ) -> None: ...

    @abstractmethod
    async def get_member_profiles(
        self,
        guild_id: int,
        user_ids: Sequence[int] | None = None,
    ) -> dict[int, MemberProfile]: ...

    # ------------------------------------------------------------------
    # Leaderboard reads (Supabase is the source of truth)
    # ------------------------------------------------------------------

    @abstractmethod
    async def get_leaderboard_data(
        self,
        guild_id: int,
        period: str = "today",
        target_date: date | None = None,
    ) -> list[LeaderboardEntry]:
        """Ranked entries for 'today'|'yesterday'|'week'|'month'|'all_time'."""

    @abstractmethod
    async def get_user_breakdown(
        self,
        guild_id: int,
        user_id: int,
        target_date: date | None = None,
    ) -> dict[str, int]: ...

    @abstractmethod
    async def get_daily_server_total(self, guild_id: int, date_str: str) -> int: ...

    @abstractmethod
    async def get_daily_guild_user_records(self, date_str: str) -> list[DailyUserStat]: ...

    # ------------------------------------------------------------------
    # Daily DM report state
    # ------------------------------------------------------------------

    @abstractmethod
    async def has_dm_report_sent(self, date_str: str, guild_id: int, user_id: int) -> bool:
        """Raises VoiceStoreUnavailable when the store cannot answer."""

    @abstractmethod
    async def mark_dm_report_sent(self, date_str: str, guild_id: int, user_id: int) -> None: ...
