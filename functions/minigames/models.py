"""Persistent and transient models for the mini-game state machine."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class GameState(StrEnum):
    SCHEDULED = "SCHEDULED"
    ANNOUNCING = "ANNOUNCING"
    REGISTRATION = "REGISTRATION"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


VALID_TRANSITIONS: dict[GameState, frozenset[GameState]] = {
    GameState.SCHEDULED: frozenset({GameState.ANNOUNCING, GameState.CANCELLED, GameState.FAILED}),
    GameState.ANNOUNCING: frozenset({GameState.REGISTRATION, GameState.CANCELLED, GameState.FAILED}),
    GameState.REGISTRATION: frozenset({GameState.IN_PROGRESS, GameState.EXPIRED, GameState.CANCELLED, GameState.FAILED}),
    GameState.IN_PROGRESS: frozenset({GameState.COMPLETED, GameState.EXPIRED, GameState.CANCELLED, GameState.FAILED}),
    GameState.COMPLETED: frozenset(),
    GameState.EXPIRED: frozenset(),
    GameState.CANCELLED: frozenset(),
    GameState.FAILED: frozenset(),
}


def utc_iso(value: datetime) -> str:
    """Return a consistent ISO-8601 UTC timestamp."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class GameConfig:
    automatic_enabled: bool = True
    registration_seconds: int = 90
    minimum_players: int = 2
    maximum_players: int = 20
    answer_seconds: int = 30
    next_game_at: str | None = None
    last_result_status: str | None = None
    last_completed_at: str | None = None


@dataclass(slots=True)
class GameSnapshot:
    """Only active-game state is stored; terminal games are immediately erased.

    The model intentionally has no winner/result-identity field. Scores and
    participants exist only while an active game can still be resumed or
    cleanly marked interrupted. Completion keeps aggregate player statistics
    and a result status, never an individual game winner record.
    """

    game_id: str
    guild_id: int
    channel_id: int
    message_id: int | None
    game_key: str
    state: GameState
    created_at: str
    registration_deadline: str | None
    registration_seconds: int
    minimum_players: int
    maximum_players: int
    answer_seconds: int
    participant_ids: list[int] = field(default_factory=list)
    scores: dict[str, int] = field(default_factory=dict)
    current_round: int = 0
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["state"] = self.state.value
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "GameSnapshot":
        return cls(
            game_id=str(value["game_id"]),
            guild_id=int(value["guild_id"]),
            channel_id=int(value["channel_id"]),
            message_id=int(value["message_id"]) if value.get("message_id") is not None else None,
            game_key=str(value["game_key"]),
            state=GameState(value["state"]),
            created_at=str(value["created_at"]),
            registration_deadline=value.get("registration_deadline"),
            registration_seconds=int(value.get("registration_seconds", 90)),
            minimum_players=int(value.get("minimum_players", 2)),
            maximum_players=int(value.get("maximum_players", 20)),
            answer_seconds=int(value.get("answer_seconds", 30)),
            participant_ids=[int(player) for player in value.get("participant_ids", [])],
            scores={str(user_id): int(points) for user_id, points in value.get("scores", {}).items()},
            current_round=int(value.get("current_round", 0)),
            payload=dict(value.get("payload") or {}),
        )


@dataclass(frozen=True, slots=True)
class GameOutcome:
    """Transient result used only to compose an immediate chat announcement."""

    completed: bool
    winners: tuple[int, ...] = ()
    scores: dict[int, int] = field(default_factory=dict)
    participated: frozenset[int] = frozenset()
    summary: str = ""


@dataclass(frozen=True, slots=True)
class PlayerStats:
    guild_id: int
    user_id: int
    games_played: int
    games_won: int
    total_points: int
    current_win_streak: int
    best_win_streak: int
    last_participation_at: str | None


__all__ = [
    "GameConfig",
    "GameOutcome",
    "GameSnapshot",
    "GameState",
    "PlayerStats",
    "VALID_TRANSITIONS",
    "utc_iso",
]
