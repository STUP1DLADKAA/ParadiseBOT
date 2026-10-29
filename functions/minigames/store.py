"""Small transactional SQLite persistence for mini-games.

The tables live in Paradise's existing ``data/paradise.db`` file (the same
local SQLite store used for voice preferences and fallback data). No Supabase
schema is duplicated and no completed-game rows are kept. Only one active
snapshot, scheduler metadata, and aggregate player statistics are stored.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from functions.voice.database import DEFAULT_DB_PATH

from .models import GameConfig, GameSnapshot, GameState, PlayerStats, utc_iso


class MiniGameStore:
    """Durable, exactly-once state and aggregate-stat store."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or DEFAULT_DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path), timeout=20, isolation_level=None, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS minigames_scheduler (
                    guild_id INTEGER PRIMARY KEY,
                    automatic_enabled INTEGER NOT NULL DEFAULT 1,
                    registration_seconds INTEGER NOT NULL DEFAULT 90,
                    minimum_players INTEGER NOT NULL DEFAULT 2,
                    maximum_players INTEGER NOT NULL DEFAULT 20,
                    answer_seconds INTEGER NOT NULL DEFAULT 30,
                    next_game_at TEXT,
                    last_result_status TEXT,
                    last_completed_at TEXT,
                    active_game_json TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS minigames_player_stats (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    games_played INTEGER NOT NULL DEFAULT 0,
                    games_won INTEGER NOT NULL DEFAULT 0,
                    total_points INTEGER NOT NULL DEFAULT 0,
                    current_win_streak INTEGER NOT NULL DEFAULT 0,
                    best_win_streak INTEGER NOT NULL DEFAULT 0,
                    last_participation_at TEXT,
                    PRIMARY KEY (guild_id, user_id)
                );
                CREATE INDEX IF NOT EXISTS minigames_stats_leaderboard_idx
                    ON minigames_player_stats(guild_id, games_won DESC, total_points DESC);
                """
            )

    def ensure_guild(
        self,
        guild_id: int,
        *,
        now: datetime,
        initial_delay_seconds: int = 30 * 60,
    ) -> GameConfig:
        """Create default scheduler state once; never reset an existing timer."""
        current = utc_iso(now)
        first_run = utc_iso(now + timedelta(seconds=initial_delay_seconds))
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO minigames_scheduler
                    (guild_id, next_game_at, updated_at)
                VALUES (?, ?, ?)
                """,
                (int(guild_id), first_run, current),
            )
            row = connection.execute(
                "SELECT * FROM minigames_scheduler WHERE guild_id = ?",
                (int(guild_id),),
            ).fetchone()
        return self._config_from_row(row)

    @staticmethod
    def _config_from_row(row: sqlite3.Row) -> GameConfig:
        return GameConfig(
            automatic_enabled=bool(row["automatic_enabled"]),
            registration_seconds=int(row["registration_seconds"]),
            minimum_players=int(row["minimum_players"]),
            maximum_players=int(row["maximum_players"]),
            answer_seconds=int(row["answer_seconds"]),
            next_game_at=row["next_game_at"],
            last_result_status=row["last_result_status"],
            last_completed_at=row["last_completed_at"],
        )

    def get_config(self, guild_id: int) -> GameConfig:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM minigames_scheduler WHERE guild_id = ?",
                (int(guild_id),),
            ).fetchone()
        if row is None:
            raise KeyError(f"mini-game scheduler not initialized for guild {guild_id}")
        return self._config_from_row(row)

    def get_active_game(self, guild_id: int) -> GameSnapshot | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT active_game_json FROM minigames_scheduler WHERE guild_id = ?",
                (int(guild_id),),
            ).fetchone()
        if row is None or not row["active_game_json"]:
            return None
        try:
            return GameSnapshot.from_dict(json.loads(row["active_game_json"]))
        except (ValueError, TypeError, KeyError):
            # Corrupt state is reconciled by the manager as a failed game.
            raise RuntimeError("persisted mini-game snapshot is invalid")

    def set_automatic_enabled(self, guild_id: int, enabled: bool, now: datetime) -> None:
        self._update_config(guild_id, {"automatic_enabled": int(bool(enabled))}, now)

    def update_settings(
        self, guild_id: int, values: dict[str, int], now: datetime
    ) -> GameConfig:
        allowed = {"registration_seconds", "minimum_players", "maximum_players", "answer_seconds"}
        if not values or not values.keys() <= allowed:
            raise ValueError("unsupported mini-game setting")
        assignments = ", ".join(f"{key} = ?" for key in values)
        params = [int(value) for value in values.values()]
        params.extend([utc_iso(now), int(guild_id)])
        with self._lock, self._connect() as connection:
            connection.execute(
                f"UPDATE minigames_scheduler SET {assignments}, updated_at = ? WHERE guild_id = ?",
                params,
            )
        return self.get_config(guild_id)

    def set_next_game_at(self, guild_id: int, timestamp: datetime, now: datetime) -> None:
        self._update_config(
            guild_id, {"next_game_at": utc_iso(timestamp)}, now
        )

    def _update_config(self, guild_id: int, values: dict[str, object], now: datetime) -> None:
        if not values:
            return
        assignments = ", ".join(f"{key} = ?" for key in values)
        params = [*values.values(), utc_iso(now), int(guild_id)]
        with self._lock, self._connect() as connection:
            connection.execute(
                f"UPDATE minigames_scheduler SET {assignments}, updated_at = ? WHERE guild_id = ?",
                params,
            )

    def fail_corrupt_active(
        self, guild_id: int, *, completed_at: datetime, next_game_at: datetime
    ) -> None:
        """Clear an unreadable snapshot as one unsuccessful interrupted run."""
        finished = utc_iso(completed_at)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE minigames_scheduler
                SET active_game_json = NULL, last_result_status = ?,
                    last_completed_at = ?, next_game_at = ?, updated_at = ?
                WHERE guild_id = ?
                """,
                (GameState.FAILED.value, finished, utc_iso(next_game_at), finished, int(guild_id)),
            )

    def start_active_game(self, snapshot: GameSnapshot, now: datetime) -> bool:
        """Atomically reserve the guild's only active-game slot."""
        payload = json.dumps(snapshot.to_dict(), separators=(",", ":"), sort_keys=True)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT active_game_json FROM minigames_scheduler WHERE guild_id = ?",
                    (snapshot.guild_id,),
                ).fetchone()
                if row is None or row["active_game_json"]:
                    connection.rollback()
                    return False
                connection.execute(
                    "UPDATE minigames_scheduler SET active_game_json = ?, updated_at = ? WHERE guild_id = ?",
                    (payload, utc_iso(now), snapshot.guild_id),
                )
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

    def save_active_game(self, snapshot: GameSnapshot, now: datetime) -> bool:
        """Replace active snapshot only if the same game still owns the slot."""
        payload = json.dumps(snapshot.to_dict(), separators=(",", ":"), sort_keys=True)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT active_game_json FROM minigames_scheduler WHERE guild_id = ?",
                    (snapshot.guild_id,),
                ).fetchone()
                current = json.loads(row["active_game_json"]) if row and row["active_game_json"] else None
                if not current or current.get("game_id") != snapshot.game_id:
                    connection.rollback()
                    return False
                connection.execute(
                    "UPDATE minigames_scheduler SET active_game_json = ?, updated_at = ? WHERE guild_id = ?",
                    (payload, utc_iso(now), snapshot.guild_id),
                )
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

    def finish_game(
        self,
        *,
        guild_id: int,
        game_id: str,
        status: GameState,
        completed_at: datetime,
        next_game_at: datetime,
        participated: Iterable[int] = (),
        scores: dict[int, int] | None = None,
        winners: Iterable[int] = (),
    ) -> bool:
        """Commit aggregate rewards and scheduler state exactly once.

        The active snapshot is erased in the same transaction. Winner ids are
        consumed only to increment aggregate wins and are never written to a
        completed-game row, result object, log, or scheduler field.
        """
        scores = {int(user_id): max(0, int(points)) for user_id, points in (scores or {}).items()}
        eligible = {int(user_id) for user_id in participated}
        winner_ids = {int(user_id) for user_id in winners} & eligible
        finish_iso = utc_iso(completed_at)
        next_iso = utc_iso(next_game_at)

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT active_game_json FROM minigames_scheduler WHERE guild_id = ?",
                    (int(guild_id),),
                ).fetchone()
                active = json.loads(row["active_game_json"]) if row and row["active_game_json"] else None
                if not active or active.get("game_id") != game_id:
                    connection.rollback()
                    return False

                if status == GameState.COMPLETED:
                    for user_id in sorted(eligible):
                        won = int(user_id in winner_ids)
                        points = int(scores.get(user_id, 0))
                        connection.execute(
                            """
                            INSERT INTO minigames_player_stats
                                (guild_id, user_id, games_played, games_won, total_points,
                                 current_win_streak, best_win_streak, last_participation_at)
                            VALUES (?, ?, 1, ?, ?, ?, ?, ?)
                            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                                games_played = minigames_player_stats.games_played + 1,
                                games_won = minigames_player_stats.games_won + excluded.games_won,
                                total_points = minigames_player_stats.total_points + excluded.total_points,
                                current_win_streak = CASE WHEN excluded.games_won > 0
                                    THEN minigames_player_stats.current_win_streak + 1 ELSE 0 END,
                                best_win_streak = max(minigames_player_stats.best_win_streak,
                                    CASE WHEN excluded.games_won > 0
                                        THEN minigames_player_stats.current_win_streak + 1 ELSE 0 END),
                                last_participation_at = excluded.last_participation_at
                            """,
                            (int(guild_id), user_id, won, points, won, won, finish_iso),
                        )

                connection.execute(
                    """
                    UPDATE minigames_scheduler
                    SET active_game_json = NULL,
                        last_result_status = ?,
                        last_completed_at = ?,
                        next_game_at = ?,
                        updated_at = ?
                    WHERE guild_id = ?
                    """,
                    (status.value, finish_iso, next_iso, finish_iso, int(guild_id)),
                )
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

    def leaderboard(self, guild_id: int, limit: int = 10) -> list[PlayerStats]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT guild_id, user_id, games_played, games_won, total_points,
                       current_win_streak, best_win_streak, last_participation_at
                FROM minigames_player_stats
                WHERE guild_id = ?
                ORDER BY games_won DESC, total_points DESC, games_played DESC, user_id ASC
                LIMIT ?
                """,
                (int(guild_id), max(1, min(50, int(limit)))),
            ).fetchall()
        return [
            PlayerStats(
                guild_id=int(row["guild_id"]),
                user_id=int(row["user_id"]),
                games_played=int(row["games_played"]),
                games_won=int(row["games_won"]),
                total_points=int(row["total_points"]),
                current_win_streak=int(row["current_win_streak"]),
                best_win_streak=int(row["best_win_streak"]),
                last_participation_at=row["last_participation_at"],
            )
            for row in rows
        ]

    def player_stats(self, guild_id: int, user_id: int) -> PlayerStats | None:
        rows = [stats for stats in self.leaderboard(guild_id, limit=50) if stats.user_id == int(user_id)]
        return rows[0] if rows else None


__all__ = ["MiniGameStore"]
