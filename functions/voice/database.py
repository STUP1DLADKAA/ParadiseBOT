"""Local SQLite storage for voice tracking.

Since the Supabase migration this file has three jobs:

1. **User settings** (timezones, DM-report preference) — shared with the
   /settimezone command and intentionally still SQLite.
2. **Bounded fallback/outbox** — when Supabase is temporarily unreachable,
   completed sessions, profiles and DM-report marks are queued here and
   flushed to Supabase as soon as it returns; in-progress sessions are
   mirrored one-row-per-user so restarts never lose live time.
3. **Migration source** — the historical voice tables are read out in
   bounded batches and deleted only after the corresponding Supabase write
   has been verified (see functions/voice/migration.py).

The voice tables here are NOT a second leaderboard implementation: they hold
no query API beyond what the migration and fallback need.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from .time_windows import IST, split_session_by_ist_midnight

from .models import (
    DailyUserStat,
    LeaderboardEntry,
    MemberProfile,
    PersistedActiveSession,
    VoiceSessionRecord,
)

log = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "paradise.db"

__all__ = [
    "DailyUserStat",
    "LeaderboardEntry",
    "MemberProfile",
    "PersistedActiveSession",
    "VoiceDatabase",
    "VoiceSessionRecord",
    "split_session_by_midnight",
]


def _ensure_aware(dt: datetime) -> datetime:
    """Guarantee a timezone-aware datetime (assume UTC when naive)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


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
        # journal_mode is selected once during initialisation below. Repeating
        # that potentially locking PRAGMA for every short-lived operation is
        # unnecessary I/O; synchronous remains a per-connection setting.
        conn.execute("PRAGMA synchronous = NORMAL;")
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute("PRAGMA journal_mode = WAL;")
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

                CREATE TABLE IF NOT EXISTS voice_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
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
                WHERE voice_member_profiles.display_name IS NOT excluded.display_name
                   OR voice_member_profiles.username IS NOT excluded.username
                   OR (
                       excluded.avatar_url IS NOT NULL
                       AND voice_member_profiles.avatar_url IS NOT excluded.avatar_url
                   )
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
        """Persist (or heartbeat) an in-progress voice session.

        Mirrors the Supabase upsert: last-write-wins on ``last_seen_at`` so a
        delayed heartbeat can never clobber fresher local fallback state.
        """
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
                WHERE excluded.last_seen_at >= voice_active_sessions.last_seen_at
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

    def get_active_session(self, guild_id: int, user_id: int) -> PersistedActiveSession | None:
        """Fetch the persisted in-progress session for one user, if any."""
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                SELECT guild_id, user_id, channel_id, started_at, last_seen_at, is_afk
                FROM voice_active_sessions
                WHERE guild_id = ? AND user_id = ?
                """,
                (guild_id, user_id),
            )
            r = cur.fetchone()
            if r is None:
                return None
            try:
                started_at = _ensure_aware(datetime.fromisoformat(r["started_at"]))
                last_seen_at = _ensure_aware(datetime.fromisoformat(r["last_seen_at"]))
            except (TypeError, ValueError):
                return None
            return PersistedActiveSession(
                guild_id=r["guild_id"],
                user_id=r["user_id"],
                channel_id=r["channel_id"],
                started_at=started_at,
                last_seen_at=last_seen_at,
                is_afk=bool(r["is_afk"]),
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
        today_date = target_date or datetime.now(IST).date()
        week_start = today_date - timedelta(days=today_date.weekday())
        week_end = min(week_start + timedelta(days=7), today_date + timedelta(days=1))
        month_start = today_date.replace(day=1)
        month_end = min(
            date(month_start.year + 1, 1, 1)
            if month_start.month == 12
            else date(month_start.year, month_start.month + 1, 1),
            today_date + timedelta(days=1),
        )

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
                cur = conn.execute(
                    """
                    SELECT user_id, SUM(duration_seconds) as total_seconds
                    FROM voice_daily_stats
                    WHERE guild_id = ? AND date >= ? AND date < ?
                    GROUP BY user_id
                    HAVING total_seconds > 0
                    ORDER BY total_seconds DESC, user_id ASC
                    """,
                    (guild_id, week_start.isoformat(), week_end.isoformat()),
                )
            elif period == "month":
                cur = conn.execute(
                    """
                    SELECT user_id, SUM(duration_seconds) as total_seconds
                    FROM voice_daily_stats
                    WHERE guild_id = ? AND date >= ? AND date < ?
                    GROUP BY user_id
                    HAVING total_seconds > 0
                    ORDER BY total_seconds DESC, user_id ASC
                    """,
                    (guild_id, month_start.isoformat(), month_end.isoformat()),
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
        today_date = target_date or datetime.now(IST).date()
        today_str = today_date.isoformat()
        yest_str = (today_date - timedelta(days=1)).isoformat()
        week_start_str = (today_date - timedelta(days=today_date.weekday())).isoformat()
        week_end_str = min(
            today_date - timedelta(days=today_date.weekday()) + timedelta(days=7),
            today_date + timedelta(days=1),
        ).isoformat()
        month_start = today_date.replace(day=1)
        month_start_str = month_start.isoformat()
        month_end_str = min(
            date(month_start.year + 1, 1, 1)
            if month_start.month == 12
            else date(month_start.year, month_start.month + 1, 1),
            today_date + timedelta(days=1),
        ).isoformat()

        with self._get_connection() as conn:
            # Single aggregated query
            cur = conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN date = ? THEN duration_seconds ELSE 0 END), 0) as today_sec,
                    COALESCE(SUM(CASE WHEN date = ? THEN duration_seconds ELSE 0 END), 0) as yest_sec,
                    COALESCE(SUM(CASE WHEN date >= ? AND date < ? THEN duration_seconds ELSE 0 END), 0) as week_sec,
                    COALESCE(SUM(CASE WHEN date >= ? AND date < ? THEN duration_seconds ELSE 0 END), 0) as month_sec,
                    COALESCE(SUM(duration_seconds), 0) as all_time_sec
                FROM voice_daily_stats
                WHERE guild_id = ? AND user_id = ?
                """,
                (
                    today_str,
                    yest_str,
                    week_start_str,
                    week_end_str,
                    month_start_str,
                    month_end_str,
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

    # ------------------------------------------------------------------
    # Local outbox (bounded fallback queue for Supabase outages)
    # ------------------------------------------------------------------

    def queue_outbox(self, kind: str, payload: dict) -> None:
        """Queue an operation that could not reach Supabase (never drops it)."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO voice_outbox (kind, payload, created_at) VALUES (?, ?, ?)",
                (kind, json.dumps(payload, separators=(",", ":")), now_iso),
            )

    def count_outbox(self) -> int:
        with self._get_connection() as conn:
            cur = conn.execute("SELECT COUNT(*) AS c FROM voice_outbox")
            return int(cur.fetchone()["c"])

    def fetch_outbox_batch(self, limit: int) -> list[tuple[int, str, dict]]:
        """Oldest-first pending operations (id, kind, payload)."""
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT id, kind, payload FROM voice_outbox ORDER BY id ASC LIMIT ?",
                (limit,),
            )
            rows = []
            for r in cur.fetchall():
                try:
                    payload = json.loads(r["payload"])
                except (TypeError, ValueError):
                    log.error("Dropping corrupt outbox row %s (kind=%s)", r["id"], r["kind"])
                    self.delete_outbox_rows([int(r["id"])])
                    continue
                rows.append((int(r["id"]), str(r["kind"]), payload))
            return rows

    def delete_outbox_rows(self, ids: Sequence[int]) -> int:
        ids = [int(i) for i in ids]
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        with self._get_connection() as conn:
            cur = conn.execute(
                f"DELETE FROM voice_outbox WHERE id IN ({placeholders})",
                ids,
            )
            return cur.rowcount

    # ------------------------------------------------------------------
    # Migration source: bounded batch reads + verified deletions.
    # Every fetch is ordered by primary key so batches are deterministic
    # and re-runs after partial failures never skip or duplicate rows.
    # ------------------------------------------------------------------

    def count_migration_rows(self, table: str) -> int:
        """Row count of a legacy voice table (0 when the table is gone)."""
        if table not in (
            "voice_sessions",
            "voice_daily_stats",
            "voice_member_profiles",
            "voice_active_sessions",
            "voice_dm_reports",
        ):
            raise ValueError(f"unknown voice table: {table}")
        with self._get_connection() as conn:
            try:
                cur = conn.execute(f"SELECT COUNT(*) AS c FROM {table}")
            except sqlite3.OperationalError:
                return 0
            return int(cur.fetchone()["c"])

    def has_pending_voice_data(self) -> bool:
        return any(
            self.count_migration_rows(t) > 0
            for t in (
                "voice_sessions",
                "voice_daily_stats",
                "voice_member_profiles",
                "voice_active_sessions",
                "voice_dm_reports",
            )
        )

    def fetch_migration_sessions(self, limit: int) -> list[VoiceSessionRecord]:
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                SELECT id, guild_id, user_id, channel_id, started_at, ended_at, duration_seconds
                FROM voice_sessions ORDER BY id ASC LIMIT ?
                """,
                (limit,),
            )
            rows = []
            for r in cur.fetchall():
                try:
                    started = _ensure_aware(datetime.fromisoformat(r["started_at"]))
                    ended = _ensure_aware(datetime.fromisoformat(r["ended_at"]))
                except (TypeError, ValueError):
                    log.error("Skipping corrupt voice_sessions row %s", r["id"])
                    continue
                rows.append(
                    VoiceSessionRecord(
                        id=int(r["id"]),
                        guild_id=r["guild_id"],
                        user_id=r["user_id"],
                        channel_id=r["channel_id"],
                        started_at=started,
                        ended_at=ended,
                        duration_seconds=int(r["duration_seconds"]),
                    )
                )
            return rows

    def delete_migration_sessions(self, ids: Sequence[int]) -> int:
        return self._delete_by_ids("voice_sessions", "id", ids)

    def fetch_migration_daily(self, limit: int) -> list[DailyUserStat]:
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                SELECT guild_id, user_id, date, duration_seconds
                FROM voice_daily_stats
                ORDER BY guild_id ASC, user_id ASC, date ASC
                LIMIT ?
                """,
                (limit,),
            )
            return [
                DailyUserStat(
                    guild_id=r["guild_id"],
                    user_id=r["user_id"],
                    date_str=str(r["date"]),
                    duration_seconds=int(r["duration_seconds"]),
                )
                for r in cur.fetchall()
            ]

    def delete_migration_daily(self, keys: Sequence[tuple[int, int, str]]) -> int:
        """Delete daily rows by (guild_id, user_id, date) keys."""
        return self._delete_daily_keys(keys)

    def fetch_migration_profiles(self, limit: int) -> list[MemberProfile]:
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                SELECT guild_id, user_id, display_name, username, avatar_url, updated_at
                FROM voice_member_profiles
                ORDER BY guild_id ASC, user_id ASC
                LIMIT ?
                """,
                (limit,),
            )
            return [
                MemberProfile(
                    guild_id=r["guild_id"],
                    user_id=r["user_id"],
                    display_name=r["display_name"],
                    username=r["username"],
                    avatar_url=r["avatar_url"],
                )
                for r in cur.fetchall()
            ]

    def fetch_migration_profiles_meta(
        self, limit: int
    ) -> list[tuple[MemberProfile, str]]:
        """Profiles plus their SQLite updated_at (for last-write-wins merges)."""
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                SELECT guild_id, user_id, display_name, username, avatar_url, updated_at
                FROM voice_member_profiles
                ORDER BY guild_id ASC, user_id ASC
                LIMIT ?
                """,
                (limit,),
            )
            return [
                (
                    MemberProfile(
                        guild_id=r["guild_id"],
                        user_id=r["user_id"],
                        display_name=r["display_name"],
                        username=r["username"],
                        avatar_url=r["avatar_url"],
                    ),
                    str(r["updated_at"]),
                )
                for r in cur.fetchall()
            ]

    def delete_migration_profiles(self, keys: Sequence[tuple[int, int]]) -> int:
        keys = list(keys)
        if not keys:
            return 0
        deleted = 0
        with self._get_connection() as conn:
            for guild_id, user_id in keys:
                cur = conn.execute(
                    "DELETE FROM voice_member_profiles WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                )
                deleted += cur.rowcount
        return deleted

    def fetch_migration_active(self, limit: int) -> list[PersistedActiveSession]:
        sessions = self.get_active_sessions()
        return sessions[:limit]

    def delete_migration_active(self, keys: Sequence[tuple[int, int]]) -> int:
        keys = list(keys)
        if not keys:
            return 0
        deleted = 0
        with self._get_connection() as conn:
            for guild_id, user_id in keys:
                cur = conn.execute(
                    "DELETE FROM voice_active_sessions WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                )
                deleted += cur.rowcount
        return deleted

    def fetch_migration_dm_reports(self, limit: int) -> list[tuple[str, int, int, str]]:
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                SELECT date, guild_id, user_id, sent_at
                FROM voice_dm_reports
                ORDER BY date ASC, guild_id ASC, user_id ASC
                LIMIT ?
                """,
                (limit,),
            )
            return [
                (str(r["date"]), int(r["guild_id"]), int(r["user_id"]), str(r["sent_at"]))
                for r in cur.fetchall()
            ]

    def delete_migration_dm_reports(self, keys: Sequence[tuple[str, int, int]]) -> int:
        keys = list(keys)
        if not keys:
            return 0
        deleted = 0
        with self._get_connection() as conn:
            for date_str, guild_id, user_id in keys:
                cur = conn.execute(
                    "DELETE FROM voice_dm_reports WHERE date = ? AND guild_id = ? AND user_id = ?",
                    (date_str, guild_id, user_id),
                )
                deleted += cur.rowcount
        return deleted

    # -- internal helpers ------------------------------------------------

    def _delete_by_ids(self, table: str, column: str, ids: Sequence[int]) -> int:
        ids = [int(i) for i in ids]
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        with self._get_connection() as conn:
            cur = conn.execute(
                f"DELETE FROM {table} WHERE {column} IN ({placeholders})",
                ids,
            )
            return cur.rowcount

    def _delete_daily_keys(self, keys: Sequence[tuple[int, int, str]]) -> int:
        keys = list(keys)
        if not keys:
            return 0
        deleted = 0
        with self._get_connection() as conn:
            for guild_id, user_id, date_str in keys:
                cur = conn.execute(
                    "DELETE FROM voice_daily_stats WHERE guild_id = ? AND user_id = ? AND date = ?",
                    (guild_id, user_id, date_str),
                )
                deleted += cur.rowcount
        return deleted


def split_session_by_midnight(
    started_at: datetime, ended_at: datetime
) -> dict[str, int]:
    """Split a voice session across calendar midnights in Asia/Kolkata."""
    return split_session_by_ist_midnight(started_at, ended_at)
