"""Discord UI components and views for the Voice Activity Leaderboard."""

from __future__ import annotations

import io
import logging
from typing import Sequence

import discord

from functions.helpers import Colour, shorten
from .models import LeaderboardMember, VoicePeriod, format_duration
from .render import LeaderboardImageRenderer

log = logging.getLogger(__name__)

RANK_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def build_leaderboard_text_embed(
    guild: discord.Guild,
    period: VoicePeriod,
    members: Sequence[LeaderboardMember],
    page: int = 1,
    page_size: int = 10,
) -> discord.Embed:
    """Build a clean text-based leaderboard embed."""
    embed = discord.Embed(
        title=f"🎙️ Voice Activity Leaderboard — {period.label}",
        colour=Colour.ACCENT,
    )

    if not members:
        embed.description = (
            f"No voice activity recorded for **{period.label.lower()}** yet.\n"
            "Hop in a voice channel to start tracking!"
        )
        embed.set_footer(text=f"Server: {guild.name}")
        return embed

    total_server_seconds = sum(m.total_seconds for m in members)
    start_idx = (page - 1) * page_size
    page_members = members[start_idx : start_idx + page_size]
    total_pages = max(1, (len(members) + page_size - 1) // page_size)

    lines = []
    for m in page_members:
        medal = RANK_MEDALS.get(m.rank, f"`{m.rank}.`")
        active_badge = " 🟢" if m.is_active_now else ""
        escaped_name = discord.utils.escape_markdown(m.display_name)
        line = f"{medal} **{escaped_name}** — `{m.formatted_time}` ({m.percentage}%){active_badge}"
        lines.append(line)

    embed.description = "\n".join(lines)

    footer_parts = [
        f"Page {page} of {total_pages}",
        f"Server Total: {format_duration(total_server_seconds)}",
        f"{len(members)} tracked members",
    ]
    embed.set_footer(text=" • ".join(footer_parts))
    return embed


def build_user_stats_embed(
    guild: discord.Guild,
    user: discord.User | discord.Member,
    stats: dict,
) -> discord.Embed:
    """Build personal voice stats card embed."""
    embed = discord.Embed(
        title=f"🎙️ Voice Activity for {user.display_name}",
        colour=Colour.ACCENT,
    )
    embed.set_thumbnail(url=user.display_avatar.url)

    rank_str = f"#{stats['rank_today']}" if stats.get("rank_today") else "Unranked"
    active_str = "🟢 Currently connected" if stats.get("is_active_now") else "⚪ Disconnected"

    embed.add_field(
        name="📅 Today",
        value=f"**Time:** `{format_duration(stats['today_seconds'])}`\n**Rank:** `{rank_str}`",
        inline=True,
    )
    embed.add_field(
        name="⏪ Yesterday",
        value=f"**Time:** `{format_duration(stats['yesterday_seconds'])}`",
        inline=True,
    )
    embed.add_field(
        name="📊 This Week",
        value=f"**Time:** `{format_duration(stats['week_seconds'])}`",
        inline=True,
    )
    embed.add_field(
        name="🗓️ This Month",
        value=f"**Time:** `{format_duration(stats['month_seconds'])}`",
        inline=True,
    )
    embed.add_field(
        name="👑 All Time",
        value=f"**Time:** `{format_duration(stats['all_time_seconds'])}`",
        inline=True,
    )
    embed.add_field(
        name="📡 Status",
        value=active_str,
        inline=True,
    )

    embed.set_footer(text=f"Server: {guild.name} • Paradise Voice Tracker")
    return embed


class LeaderboardView(discord.ui.View):
    """Interactive view for switching periods, pages, and format modes."""

    def __init__(
        self,
        tracker,
        guild: discord.Guild,
        renderer: LeaderboardImageRenderer,
        author_id: int,
        initial_period: VoicePeriod = VoicePeriod.TODAY,
        as_image: bool = True,
        timeout: float = 300.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.tracker = tracker
        self.guild = guild
        self.renderer = renderer
        self.author_id = author_id
        self.period = initial_period
        self.as_image = as_image
        self.page = 1
        self.page_size = 10
        self.message: discord.Message | None = None
        self._update_controls()

    def _update_controls(self) -> None:
        # Update format button label
        self.mode_btn.label = "📝 Text View" if self.as_image else "🖼️ Image View"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "only the person who opened the leaderboard can switch views.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, (discord.ui.Button, discord.ui.Select)):
                child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.DiscordException:
                pass

    async def update_view(self, interaction: discord.Interaction) -> None:
        """Re-render response according to current state."""
        members = await self.tracker.get_live_leaderboard(self.guild, self.period)
        total_pages = max(1, (len(members) + self.page_size - 1) // self.page_size)
        self.page = max(1, min(self.page, total_pages))
        self.prev_btn.disabled = self.page <= 1
        self.next_btn.disabled = self.page >= total_pages
        self._update_controls()

        if self.as_image:
            image_bytes = await self.renderer.render_leaderboard(
                guild_name=self.guild.name,
                period=self.period,
                members=members,
                page=self.page,
                page_size=self.page_size,
            )
            file = discord.File(io.BytesIO(image_bytes), filename="voice_leaderboard.png")
            embed = discord.Embed(
                title=f"🎙️ Voice Activity Leaderboard — {self.period.label}",
                colour=Colour.ACCENT,
            )
            embed.set_image(url="attachment://voice_leaderboard.png")
            embed.set_footer(text=f"Server: {self.guild.name} • Page {self.page} of {total_pages}")
            await interaction.response.edit_message(embed=embed, attachments=[file], view=self)
        else:
            embed = build_leaderboard_text_embed(
                self.guild, self.period, members, self.page, self.page_size
            )
            await interaction.response.edit_message(embed=embed, attachments=[], view=self)

    @discord.ui.select(
        placeholder="Select Period...",
        options=[
            discord.SelectOption(label="Today", value="today", emoji="📅", default=True),
            discord.SelectOption(label="Yesterday", value="yesterday", emoji="⏪"),
            discord.SelectOption(label="This Week", value="week", emoji="📊"),
            discord.SelectOption(label="This Month", value="month", emoji="🗓️"),
            discord.SelectOption(label="All Time", value="all_time", emoji="👑"),
        ],
    )
    async def select_period(
        self, interaction: discord.Interaction, select: discord.ui.Select
    ) -> None:
        self.period = VoicePeriod(select.values[0])
        # Update default selection
        for opt in select.options:
            opt.default = (opt.value == select.values[0])
        self.page = 1
        await self.update_view(interaction)

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary, row=1)
    async def prev_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self.page > 1:
            self.page -= 1
        await self.update_view(interaction)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary, row=1)
    async def next_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.page += 1
        await self.update_view(interaction)

    @discord.ui.button(label="📝 Text View", style=discord.ButtonStyle.primary, row=1)
    async def mode_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.as_image = not self.as_image
        await self.update_view(interaction)
