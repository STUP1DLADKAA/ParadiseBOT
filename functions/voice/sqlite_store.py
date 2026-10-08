"""SQLite-backed :class:`VoiceStore` — tests and temporary fallback only.

This is NOT wired into the production bot: Supabase is the source of truth
for voice data. The class exists so the tracker can be exercised against a
real database without network access (unit tests), and as a reference
implementation of the interface. It is a thin async shell around
:class:`functions.voice.database.VoiceDatabase`.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Sequence

from .database import VoiceDatabase
from .models import (
    DailyUserStat,
    LeaderboardEntry,
    MemberProfile,
    PersistedActiveSession,
)
from .store import VoiceStore


class LocalVoiceStore(VoiceStore):
    """Async interface over the legacy SQLite voice database."""

    def __init__(self, db: VoiceDatabase) -> None:
        self.db = db

    # -- settings --------------------------------------------------------

    async def get_user_timezone(self, user_id: int) -> str | None:
        return self.db.get_user_timezone(user_id)

    async def set_user_timezone(self, user_id: int, tz_name: str) -> None:
        self.db.set_user_timezone(user_id, tz_name)

    async def get_user_dm_preference(self, user_id: int) -> bool:
        return self.db.get_user_dm_preference(user_id)

    async def set_user_dm_preference(self, user_id: int, enabled: bool) -> None:
        self.db.set_user_dm_preference(user_id, enabled)

    # -- sessions --------------------------------------------------------

    async def record_completed_session(
        self,
        guild_id: int,
        user_id: int,
        channel_id: int,
        started_at: datetime,
        ended_at: datetime,
    ) -> int:
        self.db.record_completed_session(
            guild_id=guild_id,
            user_id=user_id,
            channel_id=channel_id,
            started_at=started_at,
            ended_at=ended_at,
        )
        duration = int((ended_at - started_at).total_seconds())
        return max(0, duration)

    async def close_active_session(
        self,
        guild_id: int,
        user_id: int,
        ended_at: datetime,
        *,
        fallback_channel_id: int | None = None,
        fallback_started_at: datetime | None = None,
    ) -> int | None:
        row = self.db.get_active_session(guild_id, user_id)
        if row is None:
            return None
        self.db.delete_active_session(guild_id, user_id)
        if row.is_afk:
            return 0
        self.db.record_completed_session(
            guild_id=guild_id,
            user_id=user_id,
            channel_id=row.channel_id,
            started_at=row.started_at,
            ended_at=ended_at,
        )
        return max(0, int((ended_at - row.started_at).total_seconds()))

    # -- active sessions ---------------------------------------------------

    async def upsert_active_session(
        self,
        guild_id: int,
        user_id: int,
        channel_id: int,
        started_at: datetime,
        last_seen_at: datetime,
        is_afk: bool = False,
    ) -> None:
        self.db.upsert_active_session(
            guild_id, user_id, channel_id, started_at, last_seen_at, is_afk
        )

    async def heartbeat_active_sessions(
        self, sessions: Sequence[PersistedActiveSession]
    ) -> None:
        for s in sessions:
            self.db.upsert_active_session(
                s.guild_id,
                s.user_id,
                s.channel_id,
                s.started_at,
                s.last_seen_at,
                s.is_afk,
            )

    async def delete_active_session(
        self, guild_id: int, user_id: int, *, started_before: datetime | None = None
    ) -> None:
        row = self.db.get_active_session(guild_id, user_id)
        if row is None:
            return
        if started_before is not None and row.started_at > started_before:
            return  # a newer session owns this row now
        self.db.delete_active_session(guild_id, user_id)

    async def get_active_sessions(self) -> list[PersistedActiveSession]:
        return self.db.get_active_sessions()

    # -- profiles ----------------------------------------------------------

    async def upsert_member_profile(
        self,
        guild_id: int,
        user_id: int,
        display_name: str,
        username: str,
        avatar_url: str | None,
    ) -> None:
        self.db.upsert_member_profile(
            guild_id, user_id, display_name, username, avatar_url
        )

    async def get_member_profiles(
        self,
        guild_id: int,
        user_ids: Sequence[int] | None = None,
    ) -> dict[int, MemberProfile]:
        return self.db.get_member_profiles(guild_id, list(user_ids) if user_ids is not None else None)

    # -- leaderboards ------------------------------------------------------

    async def get_leaderboard_data(
        self,
        guild_id: int,
        period: str = "today",
        target_date: date | None = None,
    ) -> list[LeaderboardEntry]:
        return self.db.get_leaderboard_data(guild_id, period, target_date)

    async def get_user_breakdown(
        self,
        guild_id: int,
        user_id: int,
        target_date: date | None = None,
    ) -> dict[str, int]:
        return self.db.get_user_breakdown(guild_id, user_id, target_date)

    async def get_daily_server_total(self, guild_id: int, date_str: str) -> int:
        return self.db.get_daily_server_total(guild_id, date_str)

    async def get_daily_guild_user_records(self, date_str: str) -> list[DailyUserStat]:
        return self.db.get_daily_guild_user_records(date_str)

    # -- DM reports --------------------------------------------------------

    async def has_dm_report_sent(self, date_str: str, guild_id: int, user_id: int) -> bool:
        return self.db.has_dm_report_sent(date_str, guild_id, user_id)

    async def mark_dm_report_sent(self, date_str: str, guild_id: int, user_id: int) -> None:
        self.db.mark_dm_report_sent(date_str, guild_id, user_id)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
