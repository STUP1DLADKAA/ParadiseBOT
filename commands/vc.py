"""Voice activity tracking and leaderboard commands."""

from __future__ import annotations

import io
import logging
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

from functions.helpers import Colour
from functions.voice import (
    LeaderboardImageRenderer,
    LeaderboardView,
    VoiceDatabase,
    VoicePeriod,
    VoiceTracker,
    build_leaderboard_text_embed,
    build_user_stats_embed,
)

log = logging.getLogger(__name__)


def cooldown(seconds: float = 5):
    return app_commands.checks.cooldown(1, seconds, key=lambda i: (i.guild_id, i.user.id))


class Voice(commands.GroupCog, group_name="vc", group_description="Voice activity tracking and server leaderboards"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        db_path = getattr(getattr(bot, "config", None), "voice", None)
        db_path_str = db_path.db_path if db_path and db_path.db_path else None
        self.db = VoiceDatabase(db_path_str) if db_path_str else VoiceDatabase()
        self.tracker = VoiceTracker(bot, self.db)
        self.renderer = LeaderboardImageRenderer()
        # Attach tracker to bot so other components can access it if needed
        self.bot.voice_tracker = self.tracker  # type: ignore[attr-defined]

    async def cog_load(self) -> None:
        # Start tracking and reconciliation loops
        self.tracker.start_background_tasks()

    async def cog_unload(self) -> None:
        await self.tracker.stop()

    # ------------------------------------------------------------------
    # Discord Event Listeners
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        try:
            await self.tracker.handle_voice_state_update(member, before, after)
        except Exception:
            log.exception("Voice state update handling failed")

    # ------------------------------------------------------------------
    # Slash Commands: /vc leaderboard & /vc stats
    # ------------------------------------------------------------------

    @app_commands.command(
        name="leaderboard",
        description="Show the server's voice activity ranking card or list",
    )
    @app_commands.describe(
        period="Time window to display (default: Today)",
        image="Render as a graphical statistics card (default: True)",
    )
    @app_commands.choices(
        period=[
            app_commands.Choice(name="📅 Today", value="today"),
            app_commands.Choice(name="⏪ Yesterday", value="yesterday"),
            app_commands.Choice(name="📊 This Week", value="week"),
            app_commands.Choice(name="🗓️ This Month", value="month"),
            app_commands.Choice(name="👑 All Time", value="all_time"),
        ]
    )
    @cooldown(5)
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        period: app_commands.Choice[str] | None = None,
        image: bool = True,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Voice activity only exists inside servers.", ephemeral=True
            )
            return

        selected_period = VoicePeriod(period.value) if period else VoicePeriod.TODAY

        await interaction.response.defer()

        members = self.tracker.get_live_leaderboard(interaction.guild, selected_period)
        view = LeaderboardView(
            tracker=self.tracker,
            guild=interaction.guild,
            renderer=self.renderer,
            author_id=interaction.user.id,
            initial_period=selected_period,
            as_image=image,
        )

        if image:
            image_bytes = await self.renderer.render_leaderboard(
                guild_name=interaction.guild.name,
                period=selected_period,
                members=members,
                page=1,
                page_size=10,
            )
            file = discord.File(io.BytesIO(image_bytes), filename="voice_leaderboard.png")
            embed = discord.Embed(
                title=f"🎙️ Voice Activity Leaderboard — {selected_period.label}",
                colour=Colour.ACCENT,
            )
            embed.set_image(url="attachment://voice_leaderboard.png")
            total_pages = max(1, (len(members) + 9) // 10)
            embed.set_footer(text=f"Server: {interaction.guild.name} • Page 1 of {total_pages}")
            msg = await interaction.followup.send(embed=embed, file=file, view=view)
            view.message = msg
        else:
            embed = build_leaderboard_text_embed(
                interaction.guild, selected_period, members, page=1, page_size=10
            )
            msg = await interaction.followup.send(embed=embed, view=view)
            view.message = msg

    @app_commands.command(
        name="stats",
        description="View your personal voice activity statistics",
    )
    @app_commands.describe(member="Optional member to view stats for")
    @cooldown(3)
    async def stats(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Voice stats only exist inside servers.", ephemeral=True
            )
            return

        target = member or interaction.user
        stats_data = self.tracker.get_live_user_stats(interaction.guild, target.id)
        embed = build_user_stats_embed(interaction.guild, target, stats_data)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="dmreports",
        description="Toggle daily voice activity summary DMs",
    )
    @app_commands.describe(enabled="Whether you want to receive daily voice report DMs")
    @cooldown(3)
    async def dmreports(
        self,
        interaction: discord.Interaction,
        enabled: bool,
    ) -> None:
        self.db.set_user_dm_preference(interaction.user.id, enabled)
        status_str = "enabled ✅" if enabled else "disabled ❌"
        await interaction.response.send_message(
            f"Daily voice report DMs are now **{status_str}** for you.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Voice(bot))
