"""Deterministic lifecycle, scheduler, and recovery tests for mini-games."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from functions.minigames.manager import (
    SUCCESS_DELAY,
    UNSUCCESSFUL_DELAY,
    MiniGameContext,
    MiniGameManager,
)
from functions.minigames.models import GameOutcome, GameSnapshot, GameState, utc_iso
from functions.minigames.registry import get_game
from functions.minigames.store import MiniGameStore


UTC = timezone.utc


def _snapshot(now: datetime, *, state: GameState = GameState.IN_PROGRESS) -> GameSnapshot:
    return GameSnapshot(
        game_id="game-test-001",
        guild_id=1234,
        channel_id=5678,
        message_id=9012,
        game_key="quick_trivia",
        state=state,
        created_at=utc_iso(now),
        registration_deadline=utc_iso(now),
        registration_seconds=90,
        minimum_players=2,
        maximum_players=20,
        answer_seconds=30,
        participant_ids=[101, 202],
        scores={"101": 5, "202": 1},
    )


class MiniGameTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "paradise.db"
        self.store = MiniGameStore(self.db_path)
        self.now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
        self.presence = SimpleNamespace(set_game_active=AsyncMock())
        self.bot = SimpleNamespace(presence_manager=self.presence)
        self.manager = MiniGameManager(
            self.bot,
            store=self.store,
            guild_id=1234,
            clock=lambda: self.now,
        )
        self.store.ensure_guild(1234, now=self.now)

    def tearDown(self):
        self.temp_dir.cleanup()

    def reserve(self, *, state: GameState = GameState.IN_PROGRESS):
        snapshot = _snapshot(self.now, state=state)
        self.assertTrue(self.store.start_active_game(snapshot, self.now))
        return snapshot

    def context(self, snapshot: GameSnapshot):
        message = SimpleNamespace(id=snapshot.message_id, edit=AsyncMock(), delete=AsyncMock())
        channel = SimpleNamespace(send=AsyncMock())
        context = MiniGameContext(
            self.manager,
            snapshot,
            get_game(snapshot.game_key),
            channel,
            message=message,
        )
        return context, message, channel


class TestMiniGameExactlyOnce(MiniGameTestBase):
    async def test_success_records_aggregates_once_and_schedules_from_cleanup(self):
        snapshot = self.reserve()
        context, message, _channel = self.context(snapshot)
        outcome = GameOutcome(
            completed=True,
            winners=(101,),
            scores={101: 5, 202: 1},
            participated=frozenset({101, 202}),
            summary="Trivia complete.",
        )

        await self.manager._terminate(context, GameState.COMPLETED, outcome, outcome.summary)
        expected_at = self.now + SUCCESS_DELAY
        config = self.store.get_config(1234)
        self.assertEqual(datetime.fromisoformat(config.next_game_at), expected_at)
        self.assertEqual(config.last_result_status, GameState.COMPLETED.value)
        self.assertIsNone(self.store.get_active_game(1234))
        self.assertEqual(message.edit.await_count, 1)

        winner_stats = self.store.player_stats(1234, 101)
        other_stats = self.store.player_stats(1234, 202)
        self.assertEqual((winner_stats.games_played, winner_stats.games_won, winner_stats.total_points), (1, 1, 5))
        self.assertEqual((other_stats.games_played, other_stats.games_won, other_stats.total_points), (1, 0, 1))

        # A duplicate terminal callback cannot award, announce, or schedule twice.
        await self.manager._terminate(context, GameState.COMPLETED, outcome, outcome.summary)
        self.assertEqual(message.edit.await_count, 1)
        self.assertEqual(self.store.player_stats(1234, 101).games_played, 1)
        self.assertEqual(self.store.player_stats(1234, 101).games_won, 1)

        # Persistence is aggregate-only: no completed-game/winner table exists.
        with sqlite3.connect(self.db_path) as connection:
            table_names = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            scheduler_row = connection.execute(
                "SELECT active_game_json FROM minigames_scheduler WHERE guild_id = 1234"
            ).fetchone()
        self.assertEqual(table_names, {"minigames_scheduler", "minigames_player_stats"})
        self.assertIsNone(scheduler_row[0])

    async def test_unsuccessful_game_uses_twenty_minutes_and_has_no_rewards(self):
        snapshot = self.reserve()
        context, _message, _channel = self.context(snapshot)
        outcome = GameOutcome(
            completed=False,
            scores={101: 2},
            participated=frozenset({101}),
            summary="No one solved it.",
        )

        await self.manager._terminate(context, GameState.EXPIRED, outcome, outcome.summary)
        config = self.store.get_config(1234)
        self.assertEqual(
            datetime.fromisoformat(config.next_game_at), self.now + UNSUCCESSFUL_DELAY
        )
        self.assertEqual(config.last_result_status, GameState.EXPIRED.value)
        self.assertIsNone(self.store.player_stats(1234, 101))
        self.assertIsNone(self.store.get_active_game(1234))

    async def test_scheduler_default_is_not_reset_by_repeated_prepare(self):
        first = self.store.get_config(1234).next_game_at
        self.manager._prepared = False
        self.now += timedelta(hours=7)
        await self.manager.prepare()
        self.assertEqual(self.store.get_config(1234).next_game_at, first)

    async def test_manual_reschedule_is_exact_and_rejects_active_game(self):
        scheduled = await self.manager.reschedule(37)
        self.assertEqual(scheduled, self.now + timedelta(minutes=37))
        snapshot = self.reserve()
        with self.assertRaises(RuntimeError):
            await self.manager.reschedule(5)
        self.assertEqual(self.store.get_active_game(1234).game_id, snapshot.game_id)


class TestMiniGameRecovery(MiniGameTestBase):
    async def test_restart_marks_interrupted_game_failed_once(self):
        snapshot = self.reserve()
        message = SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())
        channel = SimpleNamespace(
            fetch_message=AsyncMock(return_value=message),
            send=AsyncMock(),
        )
        self.bot.get_channel = lambda channel_id: channel if channel_id == snapshot.channel_id else None
        self.bot.fetch_channel = AsyncMock(return_value=channel)

        await self.manager.prepare()
        self.presence.set_game_active.assert_awaited_once_with(True)
        await self.manager.recover_after_restart()

        config = self.store.get_config(1234)
        self.assertEqual(config.last_result_status, GameState.FAILED.value)
        self.assertEqual(
            datetime.fromisoformat(config.next_game_at), self.now + UNSUCCESSFUL_DELAY
        )
        self.assertIsNone(self.store.get_active_game(1234))
        self.assertEqual(message.edit.await_count, 1)
        self.assertIsNone(self.store.player_stats(1234, 101))

        # Ready can fire again after reconnect; the same interrupted game is not
        # re-announced, re-awarded, or rescheduled.
        await self.manager.recover_after_restart()
        self.assertEqual(message.edit.await_count, 1)
        self.assertEqual(self.store.get_config(1234).next_game_at, config.next_game_at)

    async def test_corrupt_snapshot_is_cleared_with_failed_retry_time(self):
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "UPDATE minigames_scheduler SET active_game_json = 'not-json' WHERE guild_id = 1234"
            )
        await self.manager.prepare()
        config = self.store.get_config(1234)
        self.assertEqual(config.last_result_status, GameState.FAILED.value)
        self.assertEqual(
            datetime.fromisoformat(config.next_game_at), self.now + UNSUCCESSFUL_DELAY
        )
        self.assertIsNone(self.store.get_active_game(1234))

    async def test_cancellation_is_not_a_completed_game(self):
        snapshot = self.reserve(state=GameState.REGISTRATION)
        context, _message, _channel = self.context(snapshot)
        context.active_participants.add(101)
        await self.manager._terminate(context, GameState.CANCELLED, None, "Cancelled.")
        self.assertEqual(
            datetime.fromisoformat(self.store.get_config(1234).next_game_at),
            self.now + UNSUCCESSFUL_DELAY,
        )
        self.assertIsNone(self.store.player_stats(1234, 101))


class TestMiniGameStateAndResponses(MiniGameTestBase):
    async def test_state_transition_and_response_claims_are_bounded(self):
        snapshot = self.reserve(state=GameState.REGISTRATION)
        context, _message, _channel = self.context(snapshot)
        await self.manager._transition(context, GameState.IN_PROGRESS)
        await context.set_round(1, {"round_type": "test"})

        accepted, attempts = await context.claim_response(101, 1, max_attempts=1)
        self.assertTrue(accepted)
        self.assertEqual(attempts, 1)
        self.assertEqual(await context.claim_response(101, 1, max_attempts=1), (False, 1))
        await context.response_finished(101, 1)
        self.assertTrue(await context.wait_for_round(0))
        self.assertFalse((await context.claim_response(101, 2))[0])

        with self.assertRaises(RuntimeError):
            await self.manager._transition(context, GameState.REGISTRATION)

    async def test_registration_closes_against_the_persisted_deadline(self):
        snapshot = self.reserve(state=GameState.REGISTRATION)
        snapshot.registration_deadline = utc_iso(self.now + timedelta(seconds=90))
        context, _message, _channel = self.context(snapshot)
        self.manager._active_context = context

        self.assertEqual(await self.manager.join_player(snapshot.game_id, 303), (True, "you're in — see you at the starting line."))
        self.now += timedelta(seconds=90)
        joined, message = await self.manager.join_player(snapshot.game_id, 404)
        self.assertFalse(joined)
        self.assertEqual(message, "registration has closed.")

    async def test_only_one_transient_winner_can_claim(self):
        snapshot = self.reserve(state=GameState.IN_PROGRESS)
        context, _message, _channel = self.context(snapshot)
        self.assertTrue(await context.claim_winner(101, points=3))
        self.assertFalse(await context.claim_winner(202, points=3))
        self.assertEqual(context.winners, (101,))
        self.assertEqual(context.scores()[101], 8)
        # The winner exists only on this live context, not in a persisted result.
        persisted = self.store.get_active_game(1234)
        self.assertNotIn("winner", persisted.to_dict())
        self.assertNotIn("winners", persisted.to_dict())


if __name__ == "__main__":
    unittest.main()
