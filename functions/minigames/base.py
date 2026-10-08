"""Game registry contracts shared by modular mini-game implementations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

import discord

from .models import GameOutcome, GameSnapshot


class GameContext(Protocol):
    """The small interface game modules use to interact with the manager."""

    snapshot: GameSnapshot
    answer_seconds: int
    cancelled: bool
    active_participants: set[int]
    winners: tuple[int, ...]

    @property
    def participant_ids(self) -> tuple[int, ...]: ...

    async def set_round(self, round_number: int, payload: dict | None = None) -> None: ...

    async def claim_response(
        self, user_id: int, round_number: int, *, max_attempts: int = 1
    ) -> tuple[bool, int]: ...

    async def add_points(self, user_id: int, points: int) -> None: ...

    async def response_finished(self, user_id: int, round_number: int) -> None: ...

    async def claim_winner(self, user_id: int, *, points: int = 0) -> bool: ...

    async def wait_for_round(self, timeout: float) -> bool: ...

    async def wait_for_winner(self, timeout: float) -> bool: ...

    async def update_game(
        self,
        title: str,
        description: str,
        *,
        view: discord.ui.View | None = None,
        footer: str | None = None,
    ) -> None: ...

    def scores(self) -> dict[int, int]: ...


@dataclass(frozen=True, slots=True)
class GameDefinition:
    key: str
    name: str
    description: str
    rules: str
    play: Callable[[GameContext], Awaitable[GameOutcome]]
    enabled: bool = True


__all__ = ["GameContext", "GameDefinition"]
