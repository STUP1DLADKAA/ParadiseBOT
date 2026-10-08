"""Five-round Quick Trivia variant with deterministic standings."""

from __future__ import annotations

import random

import discord

from functions.minigames.base import GameContext
from functions.minigames.models import GameOutcome
from functions.minigames.ui import ChoiceView
from .quick_trivia import QUESTIONS, TriviaQuestion

ROUNDS = 5


def _choice_handler(context: GameContext, question: TriviaQuestion, round_number: int):
    async def handle(interaction: discord.Interaction, choice: int) -> None:
        accepted, _used = await context.claim_response(
            interaction.user.id, round_number, max_attempts=1
        )
        if not accepted:
            await interaction.response.send_message(
                "one answer per round — or this round has already closed.", ephemeral=True
            )
            return
        correct = choice == question.answer
        if correct:
            await context.add_points(interaction.user.id, 1)
        await context.response_finished(interaction.user.id, round_number)
        await interaction.response.send_message(
            "Correct — +1 point." if correct else "Answer recorded.", ephemeral=True
        )

    return handle


async def play(context: GameContext) -> GameOutcome:
    questions = random.sample(QUESTIONS, k=min(ROUNDS, len(QUESTIONS)))
    for round_number, question in enumerate(questions, start=1):
        if context.cancelled:
            break
        await context.set_round(round_number, {"round_type": "rapid_quiz", "total_rounds": len(questions)})
        view = ChoiceView(
            context,
            tuple(f"{letter} · {answer}" for letter, answer in zip("ABCD", question.options)),
            _choice_handler(context, question, round_number),
            round_number=round_number,
        )
        await context.update_game(
            f"🚀 Rapid Quiz · Round {round_number}/{len(questions)}",
            f"**{question.prompt}**\n\nChoose one answer. Each player may answer once this round. "
            f"You have **{context.answer_seconds} seconds**.",
            view=view,
            footer="One point for each correct answer · equal scores break by lower Discord user ID",
        )
        await context.wait_for_round(context.answer_seconds)
        view.stop()

    scores = context.scores()
    ranked = sorted(scores.items(), key=lambda entry: (-entry[1], entry[0]))
    winners = (ranked[0][0],) if ranked and ranked[0][1] > 0 and not context.cancelled else ()
    return GameOutcome(
        completed=bool(winners),
        winners=winners,
        scores=scores,
        participated=frozenset(context.active_participants),
        summary="Five rounds; equal totals are ranked by ascending Discord user ID.",
    )
