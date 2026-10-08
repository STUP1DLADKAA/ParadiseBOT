"""Extensible registry: scheduling chooses among every enabled game module."""

from __future__ import annotations

from .base import GameDefinition
from .games import number_guess, quick_trivia, rapid_quiz, word_scramble

GAME_REGISTRY: dict[str, GameDefinition] = {
    game.key: game
    for game in (
        GameDefinition(
            key="quick_trivia",
            name="Quick Trivia",
            description="One fast multiple-choice question. Submit privately and beat the clock.",
            rules="Join before registration closes. Each player can choose one answer. Correct answers earn one point; tied top scorers share the win.",
            play=quick_trivia.play,
        ),
        GameDefinition(
            key="word_scramble",
            name="Word Scramble",
            description="Untangle a scrambled word before anyone else solves it.",
            rules="Join before registration closes. Submit one case-insensitive answer in the modal. The first correct answer wins three points.",
            play=word_scramble.play,
        ),
        GameDefinition(
            key="rapid_quiz",
            name="Rapid Quiz",
            description="Five quick questions. Correct answers add up across rounds.",
            rules="Join before registration closes. You get one button answer per round. Each correct answer is worth one point; equal totals break by ascending Discord user ID.",
            play=rapid_quiz.play,
        ),
        GameDefinition(
            key="number_guess",
            name="Number Guess",
            description="Find the secret number from 1 to 100 with three private guesses.",
            rules="Join before registration closes. Each player gets three unique in-range guesses. Higher/lower clues are private; the first exact guess wins.",
            play=number_guess.play,
        ),
    )
}


def get_game(key: str) -> GameDefinition:
    try:
        return GAME_REGISTRY[key]
    except KeyError as error:
        raise ValueError(f"Unknown mini-game key: {key!r}") from error


def available_games() -> tuple[GameDefinition, ...]:
    return tuple(game for game in GAME_REGISTRY.values() if game.enabled)


__all__ = ["GAME_REGISTRY", "available_games", "get_game"]
