"""One-shot, case-insensitive Word Scramble."""

from __future__ import annotations

import random

import discord

from functions.minigames.base import GameContext
from functions.minigames.models import GameOutcome
from functions.minigames.ui import ModalButtonView

WORDS = (
    "paradise", "lighthouse", "puzzle", "starlight", "mango", "keyboard",
    "rainbow", "planet", "friendship", "midnight", "telescope", "sunflower",
    "butterfly", "backpack", "waterfall", "pineapple", "headphones", "moonlight",
)


class ScrambleAnswerModal(discord.ui.Modal, title="Solve the scramble"):
    answer = discord.ui.TextInput(
        label="Your answer",
        placeholder="Type the unscrambled word",
        min_length=1,
        max_length=40,
    )

    def __init__(self, context: GameContext, target: str, round_number: int) -> None:
        super().__init__(timeout=90)
        self.context = context
        self.target = target
        self.round_number = round_number

    async def on_submit(self, interaction: discord.Interaction) -> None:
        accepted, _used = await self.context.claim_response(
            interaction.user.id, self.round_number, max_attempts=1
        )
        if not accepted:
            await interaction.response.send_message(
                "you've already submitted, or this round has closed.", ephemeral=True
            )
            return
        if self.answer.value.strip().casefold() != self.target.casefold():
            await interaction.response.send_message(
                "not quite — each player gets one private submission.", ephemeral=True
            )
            return
        if await self.context.claim_winner(interaction.user.id, points=3):
            await interaction.response.send_message(
                "you solved it first — **3 points**! 🏆", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "the scramble has already been solved.", ephemeral=True
            )


async def play(context: GameContext) -> GameOutcome:
    word = random.choice(WORDS)
    letters = list(word)
    for _ in range(10):
        random.shuffle(letters)
        if "".join(letters).casefold() != word.casefold():
            break
    scrambled = "".join(letters)
    round_number = 1
    await context.set_round(
        round_number,
        {"round_type": "word_scramble", "attempts_per_player": 1},
    )
    view = ModalButtonView(
        label="Submit Answer",
        custom_id=f"paradise:minigame:{context.snapshot.game_id[:10]}:scramble-answer",
        modal_factory=lambda: ScrambleAnswerModal(context, word, round_number),
    )
    await context.update_game(
        "🔤 Word Scramble",
        f"Unscramble this word:\n\n# `{scrambled.upper()}`\n\n"
        f"Tap **Submit Answer** and enter your guess. Answers are case-insensitive; "
        f"one attempt per player. First correct answer wins.\n\n"
        f"Time left: **{context.answer_seconds} seconds**",
        view=view,
        footer="One correct solve ends the round · answer privately in the modal",
    )
    found = await context.wait_for_winner(context.answer_seconds)
    view.stop()
    winners = tuple(context.winners)
    return GameOutcome(
        completed=bool(found and winners),
        winners=winners,
        scores=context.scores(),
        participated=frozenset(context.active_participants),
        summary="The first valid case-insensitive solution wins.",
    )
