"""Competitive number guessing with bounded, validated private attempts."""

from __future__ import annotations

import asyncio
import random

import discord

from functions.minigames.base import GameContext
from functions.minigames.models import GameOutcome
from functions.minigames.ui import ModalButtonView

LOWER_BOUND = 1
UPPER_BOUND = 100
ATTEMPTS_PER_PLAYER = 3


class NumberGuessModal(discord.ui.Modal, title="Guess the number"):
    guess = discord.ui.TextInput(
        label="Your guess (1–100)",
        placeholder="A whole number from 1 to 100",
        min_length=1,
        max_length=3,
    )

    def __init__(self, context: GameContext, secret: int, round_number: int, lock: asyncio.Lock, guesses: dict[int, set[int]]) -> None:
        super().__init__(timeout=90)
        self.context = context
        self.secret = secret
        self.round_number = round_number
        self.lock = lock
        self.guesses = guesses

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.guess.value.strip()
        try:
            value = int(raw)
        except ValueError:
            await interaction.response.send_message("enter a whole number from 1 to 100.", ephemeral=True)
            return
        if not LOWER_BOUND <= value <= UPPER_BOUND:
            await interaction.response.send_message("that guess is outside 1–100.", ephemeral=True)
            return

        async with self.lock:
            user_guesses = self.guesses.setdefault(interaction.user.id, set())
            if value in user_guesses:
                await interaction.response.send_message(
                    "you already tried that number; duplicate guesses don't use an attempt.",
                    ephemeral=True,
                )
                return
            accepted, attempts_used = await self.context.claim_response(
                interaction.user.id,
                self.round_number,
                max_attempts=ATTEMPTS_PER_PLAYER,
            )
            if not accepted:
                await interaction.response.send_message(
                    "you've used all three guesses, or this round has closed.", ephemeral=True
                )
                return
            user_guesses.add(value)

            if value == self.secret:
                won = await self.context.claim_winner(interaction.user.id, points=3)
                feedback = (
                    "you found it — **3 points**! 🏆"
                    if won
                    else "the number has already been found."
                )
            else:
                remaining = ATTEMPTS_PER_PLAYER - attempts_used
                hint = "higher" if value < self.secret else "lower"
                feedback = f"go {hint}. **{remaining}** guess{'es' if remaining != 1 else ''} left."

        await interaction.response.send_message(feedback, ephemeral=True)


async def play(context: GameContext) -> GameOutcome:
    secret = random.randint(LOWER_BOUND, UPPER_BOUND)
    round_number = 1
    input_lock = asyncio.Lock()
    guesses: dict[int, set[int]] = {}
    await context.set_round(
        round_number,
        {"round_type": "number_guess", "range": [LOWER_BOUND, UPPER_BOUND], "attempts_per_player": ATTEMPTS_PER_PLAYER},
    )
    view = ModalButtonView(
        label="Make a Guess",
        custom_id=f"paradise:minigame:{context.snapshot.game_id[:10]}:number-guess",
        modal_factory=lambda: NumberGuessModal(context, secret, round_number, input_lock, guesses),
    )
    await context.update_game(
        "🎯 Number Guess",
        f"A secret number is set between **{LOWER_BOUND} and {UPPER_BOUND}**.\n\n"
        f"Use **Make a Guess** to submit privately. Each player has **{ATTEMPTS_PER_PLAYER}** "
        f"unique guesses; invalid or repeated values do not count.\n\n"
        f"The first exact guess wins · **{context.answer_seconds} seconds**",
        view=view,
        footer="Higher/lower clues are private · the round ends on a correct guess or timeout",
    )
    found = await context.wait_for_winner(context.answer_seconds)
    view.stop()
    winners = tuple(context.winners)
    return GameOutcome(
        completed=bool(found and winners),
        winners=winners,
        scores=context.scores(),
        participated=frozenset(context.active_participants),
        summary="Three unique, in-range guesses per player.",
    )
