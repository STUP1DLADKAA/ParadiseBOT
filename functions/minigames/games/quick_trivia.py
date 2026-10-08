"""Single-round multiple-choice Quick Trivia."""

from __future__ import annotations

from dataclasses import dataclass

import discord

from functions.minigames.base import GameContext
from functions.minigames.models import GameOutcome
from functions.minigames.ui import ChoiceView


@dataclass(frozen=True, slots=True)
class TriviaQuestion:
    prompt: str
    options: tuple[str, str, str, str]
    answer: int


QUESTIONS = (
    TriviaQuestion("Which planet has the most moons confirmed so far?", ("Earth", "Jupiter", "Saturn", "Mars"), 2),
    TriviaQuestion("What is the chemical symbol for gold?", ("Ag", "Au", "Gd", "Go"), 1),
    TriviaQuestion("Which ocean is the largest?", ("Atlantic", "Indian", "Arctic", "Pacific"), 3),
    TriviaQuestion("How many sides does a dodecagon have?", ("10", "11", "12", "14"), 2),
    TriviaQuestion("Which language runs natively in most web browsers?", ("JavaScript", "Rust", "Go", "Swift"), 0),
    TriviaQuestion("What is the capital of New Zealand?", ("Auckland", "Wellington", "Christchurch", "Dunedin"), 1),
    TriviaQuestion("Which instrument measures atmospheric pressure?", ("Hygrometer", "Barometer", "Anemometer", "Thermometer"), 1),
    TriviaQuestion("What is the smallest prime number?", ("0", "1", "2", "3"), 2),
)


def _choice_handler(context: GameContext, question: TriviaQuestion, round_number: int):
    async def handle(interaction: discord.Interaction, choice: int) -> None:
        accepted, _used = await context.claim_response(
            interaction.user.id, round_number, max_attempts=1
        )
        if not accepted:
            await interaction.response.send_message(
                "your answer was already recorded, or this round has closed.",
                ephemeral=True,
            )
            return
        if choice == question.answer:
            await context.add_points(interaction.user.id, 1)
            feedback = "Correct — one point. 🌟"
        else:
            feedback = "Answer recorded. The result will be revealed when the round ends."
        await context.response_finished(interaction.user.id, round_number)
        await interaction.response.send_message(feedback, ephemeral=True)

    return handle


async def play(context: GameContext) -> GameOutcome:
    import random

    question = random.choice(QUESTIONS)
    round_number = 1
    await context.set_round(round_number, {"round_type": "quick_trivia"})
    view = ChoiceView(
        context,
        tuple(f"{letter} · {answer}" for letter, answer in zip("ABCD", question.options)),
        _choice_handler(context, question, round_number),
        round_number=round_number,
    )
    await context.update_game(
        "⚡ Quick Trivia · 1 question",
        f"**{question.prompt}**\n\nChoose one answer. You have **{context.answer_seconds} seconds**; each player gets one answer.",
        view=view,
        footer="Correct answers earn one point · the round closes automatically",
    )
    await context.wait_for_round(context.answer_seconds)
    view.stop()

    scores = context.scores()
    best = max(scores.values(), default=0)
    winners = tuple(sorted(user_id for user_id, points in scores.items() if points == best and points > 0))
    return GameOutcome(
        completed=bool(winners),
        winners=winners,
        scores=scores,
        participated=frozenset(context.active_participants),
        summary="The answer timer ended.",
    )
