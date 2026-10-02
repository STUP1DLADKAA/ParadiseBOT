"""Database storage for voice tracking, daily aggregates, and user settings."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

log = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "paradise.db"


def _ensure_aware(dt: datetime) -> datetime:
    """Guarantee a timezone-aware datetime (assume UTC when naive)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass(slots=True)
class VoiceSessionRecord:
    id: int
    guild_id: int
    user_id: int
    channel_id: int
    started_at: datetime
    ended_at: datetime
    duration_seconds: int


@dataclass(slots=True)
class DailyUserStat:
    guild_id: int
    user_id: int
    date_str: str
    duration_seconds: int


@dataclass(slots=True)
class LeaderboardEntry:
    user_id: int
    total_seconds: int
    rank: int = 0


@dataclass(slots=True)
class MemberProfile:
    """Last-known Discord identity for a member, kept across restarts."""

    guild_id: int
    user_id: int
    display_name: str
    username: str
    avatar_url: str | None


@dataclass(slots=True)
class PersistedActiveSession:
    """A voice session that was still in progress when it was persisted."""

    guild_id: int
    user_id: int
    channel_id: int
    started_at: datetime
    last_seen_at: datetime
    is_afk: bool


class VoiceDatabase:
    """Thread-safe SQLite database manager for voice sessions and settings."""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=20.0,
            check_same_thread=False,
            isolation_level=None,  # Autocommit mode
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id INTEGER PRIMARY KEY,
                    timezone TEXT,
                    dm_reports_enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS voice_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT NOT NULL,
                    duration_seconds INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS voice_daily_stats (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    date TEXT NOT NULL,
                    duration_seconds INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id, date)
                );

                CREATE TABLE IF NOT EXISTS voice_dm_reports (
                    date TEXT NOT NULL,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    sent_at TEXT NOT NULL,
                    PRIMARY KEY (date, guild_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS voice_member_profiles (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    display_name TEXT NOT NULL,
                    username TEXT NOT NULL,
                    avatar_url TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (guild_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS voice_active_sessions (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    is_afk INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id)
                );

                CREATE INDEX IF NOT EXISTS idx_voice_sessions_guild_user
                    ON voice_sessions(guild_id, user_id);
                CREATE INDEX IF NOT EXISTS idx_voice_sessions_started
                    ON voice_sessions(started_at);
                CREATE INDEX IF NOT EXISTS idx_voice_daily_guild_date
                    ON voice_daily_stats(guild_id, date);
                CREATE INDEX IF NOT EXISTS idx_voice_daily_user_date
                    ON voice_daily_stats(user_id, date);
                CREATE INDEX IF NOT EXISTS idx_voice_daily_guild_user
                    ON voice_daily_stats(guild_id, user_id);
            """)

    # ------------------------------------------------------------------
    # User Settings (Timezone & DM Reports)
    # ------------------------------------------------------------------

    def get_user_timezone(self, user_id: int) -> str | None:
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT timezone FROM user_settings WHERE user_id = ?",
                (user_id,),
            )
            row = cur.fetchone()
            return row["timezone"] if row and row["timezone"] else None

    def set_user_timezone(self, user_id: int, tz_name: str) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO user_settings (user_id, timezone, dm_reports_enabled, updated_at)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    timezone = excluded.timezone,
                    updated_at = excluded.updated_at
                """,
                (user_id, tz_name, now_iso),
            )

    def get_user_dm_preference(self, user_id: int) -> bool:
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT dm_reports_enabled FROM user_settings WHERE user_id = ?",
                (user_id,),
            )
            row = cur.fetchone()
            if row is None:
                return True  # Default enabled
            return bool(row["dm_reports_enabled"])

    def set_user_dm_preference(self, user_id: int, enabled: bool) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO user_settings (user_id, timezone, dm_reports_enabled, updated_at)
                VALUES (?, NULL, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    dm_reports_enabled = excluded.dm_reports_enabled,
                    updated_at = excluded.updated_at
                """,
                (user_id, int(enabled), now_iso),
            )

    # ------------------------------------------------------------------
    # Member Identity Persistence (survives restarts & cold member caches)
    # ------------------------------------------------------------------

    def upsert_member_profile(
        self,
        guild_id: int,
        user_id: int,
        display_name: str,
        username: str,
        avatar_url: str | None,
    ) -> None:
        """Remember a member's last-known name/avatar so restarts never lose it."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO voice_member_profiles (
                    guild_id, user_id, display_name, username, avatar_url, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    username = excluded.username,
                    avatar_url = COALESCE(
                        excluded.avatar_url, voice_member_profiles.avatar_url
                    ),
                    updated_at = excluded.updated_at
                """,
                (guild_id, user_id, display_name, username, avatar_url, now_iso),
            )

    def get_member_profiles(
        self,
        guild_id: int,
        user_ids: Sequence[int] | None = None,
    ) -> dict[int, MemberProfile]:
        """Fetch all remembered identities for a guild, optionally filtered to given users."""
        with self._get_connection() as conn:
            if user_ids is not None:
                ids = list(user_ids)
                if not ids:
                    return {}
                placeholders = ", ".join("?" for _ in ids)
                cur = conn.execute(
                    f"""
                    SELECT guild_id, user_id, display_name, username, avatar_url
                    FROM voice_member_profiles
                    WHERE guild_id = ? AND user_id IN ({placeholders})
                    """,
                    (guild_id, *ids),
                )
            else:
                cur = conn.execute(
                    """
                    SELECT guild_id, user_id, display_name, username, avatar_url
                    FROM voice_member_profiles
                    WHERE guild_id = ?
                    """,
                    (guild_id,),
                )
            return {
                int(r["user_id"]): MemberProfile(
                    guild_id=r["guild_id"],
                    user_id=r["user_id"],
                    display_name=r["display_name"],
                    username=r["username"],
                    avatar_url=r["avatar_url"],
                )
                for r in cur.fetchall()
            }

    # ------------------------------------------------------------------
    # Active Session Persistence (crash / restart resilience)
    # ------------------------------------------------------------------

    def upsert_active_session(
        self,
        guild_id: int,
        user_id: int,
        channel_id: int,
        started_at: datetime,
        last_seen_at: datetime,
        is_afk: bool = False,
    ) -> None:
        """Persist (or heartbeat) an in-progress voice session."""
        started_at = _ensure_aware(started_at)
        last_seen_at = _ensure_aware(last_seen_at)
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO voice_active_sessions (
                    guild_id, user_id, channel_id, started_at, last_seen_at, is_afk
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    started_at = excluded.started_at,
                    last_seen_at = excluded.last_seen_at,
                    is_afk = excluded.is_afk
                """,
                (
                    guild_id,
                    user_id,
                    channel_id,
                    started_at.isoformat(),
                    last_seen_at.isoformat(),
                    int(is_afk),
                ),
            )

    def delete_active_session(self, guild_id: int, user_id: int) -> None:
        """Remove a persisted in-progress session (user left, or it was closed)."""
        with self._get_connection() as conn:
            conn.execute(
                "DELETE FROM voice_active_sessions WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )

    def get_active_sessions(self) -> list[PersistedActiveSession]:
        """Fetch every persisted in-progress session across all guilds."""
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                SELECT guild_id, user_id, channel_id, started_at, last_seen_at, is_afk
                FROM voice_active_sessions
                """
            )
            sessions: list[PersistedActiveSession] = []
            for r in cur.fetchall():
                try:
                    started_at = _ensure_aware(datetime.fromisoformat(r["started_at"]))
                    last_seen_at = _ensure_aware(datetime.fromisoformat(r["last_seen_at"]))
                except (TypeError, ValueError):
                    # Skip rows with corrupt timestamps rather than dying at boot
                    continue
                sessions.append(
                    PersistedActiveSession(
                        guild_id=r["guild_id"],
                        user_id=r["user_id"],
                        channel_id=r["channel_id"],
                        started_at=started_at,
                        last_seen_at=last_seen_at,
                        is_afk=bool(r["is_afk"]),
                    )
                )
            return sessions

    # ------------------------------------------------------------------
    # Voice Session Persistence & Midnight Splitting (Section 5 & 6)
    # ------------------------------------------------------------------

    def record_completed_session(
        self,
        guild_id: int,
        user_id: int,
        channel_id: int,
        started_at: datetime,
        ended_at: datetime,
    ) -> None:
        """
        Record a session and accurately split duration across midnight boundaries into daily stats.
        """
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        if ended_at.tzinfo is None:
            ended_at = ended_at.replace(tzinfo=timezone.utc)

        total_duration = max(0, int((ended_at - started_at).total_seconds()))
        if total_duration == 0:
            return

        start_iso = started_at.isoformat()
        end_iso = ended_at.isoformat()

        # Split into daily chunks
        daily_splits = split_session_by_midnight(started_at, ended_at)

        with self._get_connection() as conn:
            # 1. Insert session record
            conn.execute(
                """
                INSERT INTO voice_sessions (
                    guild_id, user_id, channel_id, started_at, ended_at, duration_seconds
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (guild_id, user_id, channel_id, start_iso, end_iso, total_duration),
            )

            # 2. Increment daily aggregates for each affected date
            for date_str, seconds in daily_splits.items():
                if seconds <= 0:
                    continue
                conn.execute(
                    """
                    INSERT INTO voice_daily_stats (guild_id, user_id, date, duration_seconds)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(guild_id, user_id, date) DO UPDATE SET
                        duration_seconds = duration_seconds + excluded.duration_seconds
                    """,
                    (guild_id, user_id, date_str, seconds),
                )

    # ------------------------------------------------------------------
    # Leaderboard & Aggregation Queries
    # ------------------------------------------------------------------

    def get_leaderboard_data(
        self,
        guild_id: int,
        period: str = "today",
        target_date: date | None = None,
    ) -> list[LeaderboardEntry]:
        """
        Get ranked members for a given period: 'today', 'yesterday', 'week', 'month', 'all_time'.
        """
        now = datetime.now(timezone.utc)
        today_date = target_date or now.date()

        with self._get_connection() as conn:
            if period == "today":
                date_str = today_date.isoformat()
                cur = conn.execute(
                    """
                    SELECT user_id, duration_seconds as total_seconds
                    FROM voice_daily_stats
                    WHERE guild_id = ? AND date = ? AND duration_seconds > 0
                    ORDER BY total_seconds DESC, user_id ASC
                    """,
                    (guild_id, date_str),
                )
            elif period == "yesterday":
                yest_str = (today_date - timedelta(days=1)).isoformat()
                cur = conn.execute(
                    """
                    SELECT user_id, duration_seconds as total_seconds
                    FROM voice_daily_stats
                    WHERE guild_id = ? AND date = ? AND duration_seconds > 0
                    ORDER BY total_seconds DESC, user_id ASC
                    """,
                    (guild_id, yest_str),
                )
            elif period == "week":
                start_str = (today_date - timedelta(days=7)).isoformat()
                cur = conn.execute(
                    """
                    SELECT user_id, SUM(duration_seconds) as total_seconds
                    FROM voice_daily_stats
                    WHERE guild_id = ? AND date >= ? AND date <= ?
                    GROUP BY user_id
                    HAVING total_seconds > 0
                    ORDER BY total_seconds DESC, user_id ASC
                    """,
                    (guild_id, start_str, today_date.isoformat()),
                )
            elif period == "month":
                start_str = (today_date - timedelta(days=30)).isoformat()
                cur = conn.execute(
                    """
                    SELECT user_id, SUM(duration_seconds) as total_seconds
                    FROM voice_daily_stats
                    WHERE guild_id = ? AND date >= ? AND date <= ?
                    GROUP BY user_id
                    HAVING total_seconds > 0
                    ORDER BY total_seconds DESC, user_id ASC
                    """,
                    (guild_id, start_str, today_date.isoformat()),
                )
            elif period == "all_time":
                cur = conn.execute(
                    """
                    SELECT user_id, SUM(duration_seconds) as total_seconds
                    FROM voice_daily_stats
                    WHERE guild_id = ?
                    GROUP BY user_id
                    HAVING total_seconds > 0
                    ORDER BY total_seconds DESC, user_id ASC
                    """,
                    (guild_id,),
                )
            else:
                return []

            rows = cur.fetchall()
            entries = []
            for rank, r in enumerate(rows, 1):
                entries.append(
                    LeaderboardEntry(
                        user_id=r["user_id"],
                        total_seconds=int(r["total_seconds"]),
                        rank=rank,
                    )
                )
            return entries

    def get_user_breakdown(
        self,
        guild_id: int,
        user_id: int,
        target_date: date | None = None,
    ) -> dict[str, int]:
        """Return user's voice time across today, yesterday, week, month, and all-time."""
        now = datetime.now(timezone.utc)
        today_date = target_date or now.date()
        today_str = today_date.isoformat()
        yest_str = (today_date - timedelta(days=1)).isoformat()
        week_start_str = (today_date - timedelta(days=7)).isoformat()
        month_start_str = (today_date - timedelta(days=30)).isoformat()

        with self._get_connection() as conn:
            # Single aggregated query
            cur = conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN date = ? THEN duration_seconds ELSE 0 END), 0) as today_sec,
                    COALESCE(SUM(CASE WHEN date = ? THEN duration_seconds ELSE 0 END), 0) as yest_sec,
                    COALESCE(SUM(CASE WHEN date >= ? AND date <= ? THEN duration_seconds ELSE 0 END), 0) as week_sec,
                    COALESCE(SUM(CASE WHEN date >= ? AND date <= ? THEN duration_seconds ELSE 0 END), 0) as month_sec,
                    COALESCE(SUM(duration_seconds), 0) as all_time_sec
                FROM voice_daily_stats
                WHERE guild_id = ? AND user_id = ?
                """,
                (
                    today_str,
                    yest_str,
                    week_start_str,
                    today_str,
                    month_start_str,
                    today_str,
                    guild_id,
                    user_id,
                ),
            )
            row = cur.fetchone()
            if not row:
                return {
                    "today": 0,
                    "yesterday": 0,
                    "week": 0,
                    "month": 0,
                    "all_time": 0,
                }
            return {
                "today": int(row["today_sec"]),
                "yesterday": int(row["yest_sec"]),
                "week": int(row["week_sec"]),
                "month": int(row["month_sec"]),
                "all_time": int(row["all_time_sec"]),
            }

    def get_daily_server_total(self, guild_id: int, date_str: str) -> int:
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT COALESCE(SUM(duration_seconds), 0) as total FROM voice_daily_stats WHERE guild_id = ? AND date = ?",
                (guild_id, date_str),
            )
            row = cur.fetchone()
            return int(row["total"]) if row else 0

    def get_daily_guild_user_records(self, date_str: str) -> list[DailyUserStat]:
        """Fetch all user records for a specific date across all guilds."""
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                SELECT guild_id, user_id, date, duration_seconds
                FROM voice_daily_stats
                WHERE date = ? AND duration_seconds > 0
                """,
                (date_str,),
            )
            return [
                DailyUserStat(
                    guild_id=r["guild_id"],
                    user_id=r["user_id"],
                    date_str=r["date"],
                    duration_seconds=int(r["duration_seconds"]),
                )
                for r in cur.fetchall()
            ]

    def has_dm_report_sent(self, date_str: str, guild_id: int, user_id: int) -> bool:
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT 1 FROM voice_dm_reports WHERE date = ? AND guild_id = ? AND user_id = ?",
                (date_str, guild_id, user_id),
            )
            return cur.fetchone() is not None

    def mark_dm_report_sent(self, date_str: str, guild_id: int, user_id: int) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO voice_dm_reports (date, guild_id, user_id, sent_at)
                VALUES (?, ?, ?, ?)
                """,
                (date_str, guild_id, user_id, now_iso),
            )


def split_session_by_midnight(
    started_at: datetime, ended_at: datetime
) -> dict[str, int]:
    """
    Split a session between started_at and ended_at across midnight boundaries (UTC).
    
    Example:
        23:50 -> 00:20 gives:
        - Day 1: 10m (600s)
        - Day 2: 20m (1200s)
    """
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    if ended_at.tzinfo is None:
        ended_at = ended_at.replace(tzinfo=timezone.utc)

    if ended_at <= started_at:
        return {}

    splits: dict[str, int] = {}
    current_start = started_at

    while current_start < ended_at:
        # Date string for current_start
        date_str = current_start.date().isoformat()
        # Next midnight UTC
        next_day = current_start.date() + timedelta(days=1)
        next_midnight = datetime(
            next_day.year, next_day.month, next_day.day, 0, 0, 0, tzinfo=timezone.utc
        )

        current_end = min(ended_at, next_midnight)
        seconds = int((current_end - current_start).total_seconds())

        splits[date_str] = splits.get(date_str, 0) + seconds
        current_start = current_end

    return splits
