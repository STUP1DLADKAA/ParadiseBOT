"""Discord message components for mini-game registration and rounds."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import discord

from .base import GameContext


class RegistrationView(discord.ui.View):
    """Join/rules components attached to the one registration announcement."""

    def __init__(self, manager, game_id: str, rules: str) -> None:
        super().__init__(timeout=None)
        self.manager = manager
        self.game_id = game_id
        self.rules = rules

        join = discord.ui.Button(
            label="Join Game",
            style=discord.ButtonStyle.success,
            emoji="🌴",
            custom_id=f"paradise:minigame:join:{game_id}",
        )
        join.callback = self._join
        self.add_item(join)

        rules_button = discord.ui.Button(
            label="View Rules",
            style=discord.ButtonStyle.secondary,
            custom_id=f"paradise:minigame:rules:{game_id}",
        )
        rules_button.callback = self._rules
        self.add_item(rules_button)

    async def _join(self, interaction: discord.Interaction) -> None:
        if interaction.user.bot:
            await interaction.response.send_message("bots can't join this one.", ephemeral=True)
            return
        ok, message = await self.manager.join_player(self.game_id, interaction.user.id)
        await interaction.response.send_message(message, ephemeral=True)
        if ok:
            await self.manager.refresh_registration(self.game_id)

    async def _rules(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(self.rules, ephemeral=True)


class ChoiceView(discord.ui.View):
    """Button choices for trivia rounds; the handler owns answer validation."""

    def __init__(
        self,
        context: GameContext,
        labels: tuple[str, ...],
        on_choice: Callable[[discord.Interaction, int], Awaitable[None]],
        *,
        round_number: int,
    ) -> None:
        super().__init__(timeout=None)
        self.context = context
        for index, label in enumerate(labels):
            button = discord.ui.Button(
                label=label[:80],
                style=discord.ButtonStyle.primary,
                custom_id=(
                    f"paradise:minigame:{context.snapshot.game_id[:10]}:"
                    f"r{round_number}:a{index}"
                ),
            )

            async def callback(interaction: discord.Interaction, choice=index) -> None:
                await on_choice(interaction, choice)

            button.callback = callback
            self.add_item(button)


class ModalButtonView(discord.ui.View):
    """A component that opens an ephemeral answer modal."""

    def __init__(
        self,
        *,
        label: str,
        custom_id: str,
        modal_factory: Callable[[], discord.ui.Modal],
        style: discord.ButtonStyle = discord.ButtonStyle.primary,
    ) -> None:
        super().__init__(timeout=None)
        button = discord.ui.Button(label=label, style=style, custom_id=custom_id)

        async def callback(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(modal_factory())

        button.callback = callback
        self.add_item(button)


__all__ = ["ChoiceView", "ModalButtonView", "RegistrationView"]
