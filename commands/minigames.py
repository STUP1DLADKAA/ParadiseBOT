"""Automated community mini-games and authorised scheduler controls."""

from __future__ import annotations

import logging
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

from functions.helpers import Colour
from functions.minigames import GAME_REGISTRY
from functions.minigames.manager import MiniGameManager

log = logging.getLogger(__name__)

GAME_CHOICES = [
    app_commands.Choice(name=game.name, value=game.key)
    for game in GAME_REGISTRY.values()
]


def admin_only():
    return app_commands.checks.has_permissions(manage_guild=True)


class MiniGames(commands.GroupCog, group_name="minigames", group_description="Automated Paradise community games"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.manager = MiniGameManager(bot)

    async def cog_load(self) -> None:
        await self.manager.prepare()
        # Normal startup starts the worker from bot.on_ready, after restart
        # reconciliation has completed. An extension reload after Ready must
        # reconcile here because no new on_ready event is guaranteed.
        if self.bot.is_ready():
            await self.manager.recover_after_restart()
            self.manager.start()

    async def cog_unload(self) -> None:
        await self.manager.stop()

    def _home_guild(self, interaction: discord.Interaction) -> bool:
        return interaction.guild_id == self.manager.guild_id

    @app_commands.command(description="See the current game and next scheduled challenge")
    async def status(self, interaction: discord.Interaction) -> None:
        if not self._home_guild(interaction):
            await interaction.response.send_message("mini-games are configured for Paradise's home server.", ephemeral=True)
            return
        config, active = await self.manager.status()
        embed = discord.Embed(title="🌴 Paradise Mini-Games", colour=Colour.ACCENT)
        embed.add_field(
            name="Automatic games",
            value="Enabled" if config.automatic_enabled else "Disabled",
            inline=True,
        )
        embed.add_field(name="Registration", value=f"{config.registration_seconds}s", inline=True)
        embed.add_field(name="Players", value=f"{config.minimum_players}–{config.maximum_players}", inline=True)
        embed.add_field(name="Answer timer", value=f"{config.answer_seconds}s", inline=True)
        if active is not None:
            game = GAME_REGISTRY.get(active.game_key)
            name = game.name if game else active.game_key
            embed.add_field(
                name="Current game",
                value=(
                    f"**{name}** · `{active.state.value}`\n"
                    f"{len(active.participant_ids)} / {active.maximum_players} players"
                ),
                inline=False,
            )
        else:
            next_at = datetime.fromisoformat(config.next_game_at) if config.next_game_at else None
            next_value = discord.utils.format_dt(next_at, style="R") if next_at else "not scheduled"
            absolute = discord.utils.format_dt(next_at, style="F") if next_at else ""
            embed.add_field(name="Next game", value=f"{next_value}\n{absolute}".strip(), inline=False)
        if config.last_result_status:
            embed.set_footer(text=f"Last result status: {config.last_result_status}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(description="Enable or disable automatically scheduled games")
    @app_commands.describe(enabled="Whether the scheduler should start future automatic games")
    @admin_only()
    async def auto(self, interaction: discord.Interaction, enabled: bool) -> None:
        if not self._home_guild(interaction):
            await interaction.response.send_message("this control only applies in Paradise's configured server.", ephemeral=True)
            return
        await self.manager.set_automatic_enabled(enabled)
        message = "Automatic mini-games are now **enabled**." if enabled else "Automatic mini-games are now **disabled**. Any current game can finish normally."
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(description="Manually start one game using the shared scheduler")
    @app_commands.describe(game="Choose a game, or leave blank for a random enabled game")
    @app_commands.choices(game=GAME_CHOICES)
    @admin_only()
    async def start(
        self,
        interaction: discord.Interaction,
        game: app_commands.Choice[str] | None = None,
    ) -> None:
        if not self._home_guild(interaction):
            await interaction.response.send_message("this control only applies in Paradise's configured server.", ephemeral=True)
            return
        if interaction.channel is None or not isinstance(interaction.channel, discord.abc.Messageable):
            await interaction.response.send_message("I need a normal text channel to start a game.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        started, text = await self.manager.start_game(
            game_key=game.value if game else None,
            channel=interaction.channel,
            manual=True,
        )
        await interaction.followup.send(text, ephemeral=True)
        if not started:
            log.info("Manual mini-game start declined: %s", text)

    @app_commands.command(description="Cancel the active mini-game")
    @admin_only()
    async def cancel(self, interaction: discord.Interaction) -> None:
        if not self._home_guild(interaction):
            await interaction.response.send_message("this control only applies in Paradise's configured server.", ephemeral=True)
            return
        ok, text = await self.manager.cancel_game()
        await interaction.response.send_message(text, ephemeral=True)
        if ok:
            log.info("Administrator requested cancellation of the active mini-game")

    @app_commands.command(description="Configure registration, player limits and answer timers")
    @app_commands.describe(
        registration_seconds="Registration window: 30–600 seconds",
        minimum_players="Minimum players required to begin: 1–20",
        maximum_players="Maximum registration size: 2–100",
        answer_seconds="Time allowed to answer each round: 10–180 seconds",
    )
    @admin_only()
    async def configure(
        self,
        interaction: discord.Interaction,
        registration_seconds: app_commands.Range[int, 30, 600] | None = None,
        minimum_players: app_commands.Range[int, 1, 20] | None = None,
        maximum_players: app_commands.Range[int, 2, 100] | None = None,
        answer_seconds: app_commands.Range[int, 10, 180] | None = None,
    ) -> None:
        if not self._home_guild(interaction):
            await interaction.response.send_message("this control only applies in Paradise's configured server.", ephemeral=True)
            return
        values = {
            name: value
            for name, value in (
                ("registration_seconds", registration_seconds),
                ("minimum_players", minimum_players),
                ("maximum_players", maximum_players),
                ("answer_seconds", answer_seconds),
            )
            if value is not None
        }
        if not values:
            config = self.manager.store.get_config(self.manager.guild_id)
            await interaction.response.send_message(
                f"Registration: **{config.registration_seconds}s** · players: **{config.minimum_players}–{config.maximum_players}** · answer timer: **{config.answer_seconds}s**",
                ephemeral=True,
            )
            return
        try:
            config = await self.manager.configure(values)
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await interaction.response.send_message(
            f"Settings saved: registration **{config.registration_seconds}s**, players **{config.minimum_players}–{config.maximum_players}**, answer timer **{config.answer_seconds}s**.",
            ephemeral=True,
        )

    @app_commands.command(description="Set the next automatic game time")
    @app_commands.describe(delay_minutes="Minutes from now (1–10,080)")
    @admin_only()
    async def reschedule(
        self,
        interaction: discord.Interaction,
        delay_minutes: app_commands.Range[int, 1, 10_080],
    ) -> None:
        if not self._home_guild(interaction):
            await interaction.response.send_message("this control only applies in Paradise's configured server.", ephemeral=True)
            return
        try:
            scheduled = await self.manager.reschedule(delay_minutes)
        except RuntimeError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await interaction.response.send_message(
            f"Next game scheduled for {discord.utils.format_dt(scheduled, style='F')} ({discord.utils.format_dt(scheduled, style='R')}).",
            ephemeral=True,
        )

    @app_commands.command(description="View the aggregate Paradise mini-game leaderboard")
    @app_commands.describe(limit="Number of players to show, up to 20")
    async def leaderboard(
        self, interaction: discord.Interaction, limit: app_commands.Range[int, 1, 20] = 10
    ) -> None:
        if not self._home_guild(interaction):
            await interaction.response.send_message("mini-game scores are available in Paradise's home server.", ephemeral=True)
            return
        entries = self.manager.store.leaderboard(self.manager.guild_id, limit=limit)
        embed = discord.Embed(title="🏆 Paradise Mini-Game Leaderboard", colour=Colour.ACCENT)
        if not entries:
            embed.description = "No completed mini-game stats yet. Join the next challenge to get started."
        else:
            lines = []
            for rank, stat in enumerate(entries, start=1):
                lines.append(
                    f"**{rank}.** <@{stat.user_id}> — **{stat.games_won}** wins · "
                    f"{stat.total_points} points · {stat.games_played} games · "
                    f"streak {stat.current_win_streak} (best {stat.best_win_streak})"
                )
            embed.description = "\n".join(lines)
        embed.set_footer(text="Aggregate stats only · no per-game winner history is retained")
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MiniGames(bot))
