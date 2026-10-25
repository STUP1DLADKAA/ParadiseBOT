"""Voice activity tracking and leaderboard commands."""

from __future__ import annotations

import io
import logging
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

from config import VoiceConfig
from functions.helpers import Colour
from functions.voice import (
    LeaderboardImageRenderer,
    LeaderboardView,
    VoiceDatabase,
    VoicePeriod,
    VoiceStoreError,
    VoiceStoreUnavailable,
    VoiceTracker,
    build_leaderboard_text_embed,
    build_user_stats_embed,
)
from functions.voice.supabase_database import SupabaseVoiceDatabase

log = logging.getLogger(__name__)

SETUP_NEEDED_MESSAGE = (
    "voice tracking isn't configured yet. the owner needs to set "
    "`SUPABASE_URL` and `SUPABASE_SECRET_KEY` in `.env` and apply "
    "`001_voice_activity.sql` followed by `002_voice_ist_calendar.sql` "
    "in the Supabase SQL Editor (for an existing schema v1, apply only 002; "
    "see `supabase/README.md`)."
)


def cooldown(seconds: float = 5):
    return app_commands.checks.cooldown(1, seconds, key=lambda i: (i.guild_id, i.user.id))


class Voice(commands.GroupCog, group_name="vc", group_description="Voice activity tracking and server leaderboards"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        cfg = getattr(getattr(bot, "config", None), "voice", None) or VoiceConfig()
        db_path = cfg.db_path if cfg.db_path else None
        # Local SQLite: user settings + bounded fallback/migration storage.
        # It is NOT the leaderboard source — Supabase is.
        self.db = VoiceDatabase(db_path) if db_path else VoiceDatabase()

        self.store: SupabaseVoiceDatabase | None = None
        self.tracker: VoiceTracker | None = None
        self.renderer = LeaderboardImageRenderer()

        if cfg.supabase_configured:
            self.store = SupabaseVoiceDatabase(
                self.db,
                url=cfg.supabase_url,
                key=cfg.supabase_secret_key,
                request_timeout=cfg.request_timeout,
                flush_interval=cfg.flush_interval,
                migration_batch_size=cfg.migration_batch_size,
            )
            self.tracker = VoiceTracker(
                bot,
                self.store,
                reconcile_interval=cfg.heartbeat_interval,
                dm_reports_enabled=cfg.dm_reports_enabled,
                alert_channel_id=cfg.alert_channel_id,
                deaf_warning_seconds=cfg.deaf_warning_seconds,
                renderer=self.renderer,
            )
            # Attach tracker to bot so other components can access it if needed
            self.bot.voice_tracker = self.tracker  # type: ignore[attr-defined]
        else:
            log.error(
                "Voice tracking is DISABLED: SUPABASE_URL and/or SUPABASE_SECRET_KEY "
                "are missing. Copy .env.example to .env, add the Supabase project URL "
                "and the server-side SECRET key (never the public/anon key), and make "
                "sure migrations/001_voice_activity.sql and 002_voice_ist_calendar.sql "
                "have been run in the Supabase SQL Editor (see supabase/README.md). Existing local voice "
                "data is untouched and migrates automatically once configured."
            )

    async def cog_load(self) -> None:
        # Start tracking first: while the store bootstraps (schema check +
        # migration), events fall back to the durable local queue, so no
        # session time is lost even if Supabase is slow to come up.
        if self.tracker is not None:
            self.tracker.start_background_tasks()
        if self.store is not None:
            self.store.start()

    async def cog_unload(self) -> None:
        if self.tracker is not None:
            await self.tracker.stop()
        if self.store is not None:
            await self.store.stop()

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
        if self.tracker is None:
            return  # not configured — nothing is tracked or written
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
        if self.tracker is None or self.store is None:
            await interaction.response.send_message(SETUP_NEEDED_MESSAGE, ephemeral=True)
            return

        selected_period = VoicePeriod(period.value) if period else VoicePeriod.TODAY

        await interaction.response.defer()

        try:
            members = await self.tracker.get_live_leaderboard(
                interaction.guild, selected_period
            )
        except VoiceStoreUnavailable:
            await interaction.followup.send(
                "the voice database is temporarily unreachable — your time is "
                "still being tracked. try again in a minute.",
                ephemeral=True,
            )
            return
        except VoiceStoreError:
            log.exception("Leaderboard fetch failed")
            await interaction.followup.send(
                "something went wrong reading the voice database.", ephemeral=True
            )
            return

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
        if self.tracker is None or self.store is None:
            await interaction.response.send_message(SETUP_NEEDED_MESSAGE, ephemeral=True)
            return

        target = member or interaction.user
        try:
            stats_data = await self.tracker.get_live_user_stats(interaction.guild, target.id)
        except VoiceStoreUnavailable:
            await interaction.response.send_message(
                "the voice database is temporarily unreachable — your time is "
                "still being tracked. try again in a minute.",
                ephemeral=True,
            )
            return
        except VoiceStoreError:
            log.exception("Voice stats fetch failed")
            await interaction.response.send_message(
                "something went wrong reading the voice database.", ephemeral=True
            )
            return
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
        # A user setting, intentionally kept in local SQLite (shared with
        # /settimezone) — no Supabase dependency here.
        self.db.set_user_dm_preference(interaction.user.id, enabled)
        status_str = "enabled ✅" if enabled else "disabled ❌"
        await interaction.response.send_message(
            f"Daily voice report DMs are now **{status_str}** for you.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Voice(bot))
