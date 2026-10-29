"""Discord UI components and views for the Voice Activity Leaderboard."""

from __future__ import annotations

import io
import logging
from datetime import date, timedelta
from typing import Sequence

import discord

from functions.helpers import Colour
from .models import LeaderboardMember, VoicePeriod, format_duration
from .render import LeaderboardImageRenderer
from .store import VoiceStoreError, VoiceStoreUnavailable

log = logging.getLogger(__name__)

RANK_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}

# The image card holds ten rows, and the text view stays at ten so switching
# between them never changes what you are looking at.
IMAGE_PAGE_SIZE = 10
TEXT_PAGE_SIZE = 10


def leaderboard_subtitle(
    members: Sequence[LeaderboardMember],
    *,
    page: int,
    total_pages: int,
    live: int | None = None,
) -> str:
    """The one line of context under the title, in plain words.

    Read out loud it should sound like a person: "23 people · 2 in a call right
    now · page 2 of 3".
    """
    people = len(members)
    parts = [f"{people} {'person' if people == 1 else 'people'}"]
    if live is None:
        live = sum(1 for m in members if m.is_active_now)
    if live == 1:
        parts.append("1 in a call right now")
    elif live:
        parts.append(f"{live} in a call right now")
    if total_pages > 1:
        parts.append(f"page {page} of {total_pages}")
    return "  ·  ".join(parts)


def leaderboard_image_embed(
    guild: discord.Guild | None,
    period: VoicePeriod,
    *,
    page: int = 1,
    total_pages: int = 1,
    filename: str = "voice_leaderboard.png",
    caption: str | None = None,
) -> discord.Embed:
    """The embed that frames the card.

    The card already carries the title, the server, the date and the page
    number, so the embed stays out of its way: no second headline, no emoji
    prefix, and a footer only when there is more than one page to move through.
    """
    embed = discord.Embed(colour=Colour.ACCENT)
    embed.set_image(url=f"attachment://{filename}")
    if caption:
        embed.description = caption
    footer = []
    if guild is not None:
        footer.append(guild.name)
    if total_pages > 1:
        footer.append(f"page {page} of {total_pages}")
    if footer:
        embed.set_footer(text="  ·  ".join(footer))
    return embed


def build_leaderboard_text_embed(
    guild: discord.Guild,
    period: VoicePeriod,
    members: Sequence[LeaderboardMember],
    page: int = 1,
    page_size: int = 10,
) -> discord.Embed:
    """The text version of the board, for servers that prefer it."""
    embed = discord.Embed(title=period.label, colour=Colour.ACCENT)

    if not members:
        embed.description = (
            "Nobody has been in a call yet.\n"
            "Join a voice channel and this fills itself in."
        )
        embed.set_footer(text=guild.name)
        return embed

    total_server_seconds = sum(m.total_seconds for m in members)
    start_idx = (page - 1) * page_size
    page_members = members[start_idx : start_idx + page_size]
    total_pages = max(1, (len(members) + page_size - 1) // page_size)

    lines = []
    for m in page_members:
        medal = RANK_MEDALS.get(m.rank)
        place = f"{medal} " if medal else f"{m.rank}. "
        active = "  ·  in a call now" if m.is_active_now else ""
        name = discord.utils.escape_markdown(m.display_name)
        lines.append(f"{place}**{name}** — {m.formatted_time}{active}")
    embed.description = "\n".join(lines)

    footer = [f"{format_duration(total_server_seconds)} together"]
    if total_pages > 1:
        footer.append(f"page {page} of {total_pages}")
    footer.append(guild.name)
    embed.set_footer(text="  ·  ".join(footer))
    return embed


def build_user_stats_embed(
    guild: discord.Guild,
    user: discord.User | discord.Member,
    stats: dict,
) -> discord.Embed:
    """One person's own numbers, grouped the way a settings screen would be."""
    embed = discord.Embed(
        title=user.display_name,
        description=(
            "In a call right now" if stats.get("is_active_now") else "Not in a call"
        ),
        colour=Colour.ACCENT,
    )
    embed.set_thumbnail(url=user.display_avatar.url)

    rank = stats.get("rank_today")
    embed.add_field(
        name="Today",
        value=(
            f"{format_duration(stats['today_seconds'])}"
            + (f"\n#{rank} on the board" if rank else "")
        ),
        inline=True,
    )
    embed.add_field(
        name="Yesterday",
        value=format_duration(stats["yesterday_seconds"]),
        inline=True,
    )
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    embed.add_field(
        name="This week",
        value=format_duration(stats["week_seconds"]),
        inline=True,
    )
    embed.add_field(
        name="This month",
        value=format_duration(stats["month_seconds"]),
        inline=True,
    )
    embed.add_field(
        name="All time",
        value=format_duration(stats["all_time_seconds"]),
        inline=True,
    )

    embed.set_footer(text=guild.name)
    return embed


def _comparison_sentence(seconds: int, previous: int, day_name: str) -> str:
    """How this day compares with the one before, in ordinary words."""
    if previous <= 0:
        return "" if seconds <= 0 else "The day before, you weren't in voice at all."
    difference = seconds - previous
    if abs(difference) < 300:
        return f"About the same as {day_name}."
    moment = format_duration(abs(difference))
    if difference > 0:
        return f"That's {moment} more than {day_name}."
    return f"That's {moment} less than {day_name}."


def _closing_sentence(seconds: int, rank: int | None, people: int) -> str:
    """One short, true line — or nothing at all. Never filler."""
    if seconds <= 0:
        return ""  # the first sentence already says it
    if rank == 1 and people > 1:
        return "You had the most time in voice that day."
    if rank in (2, 3) and people > 3:
        return "You were one of the three most present people that day."
    if seconds < 600:
        return "A quick hello still counts."
    return ""


def build_daily_dm_embed(
    *,
    guild_name: str,
    target_date: date,
    seconds: int,
    rank: int | None,
    people: int,
    server_total_seconds: int,
    previous_seconds: int | None,
) -> discord.Embed:
    """The daily voice report as a DM.

    Written the way a person would tell you: the day, one sentence about your
    own time, one about how it compares, then the three numbers worth keeping.
    """
    day_name = target_date.strftime("%A")
    heading = f"{day_name}, {target_date.day} {target_date.strftime('%B')}"
    seconds = max(0, int(seconds))

    sentences: list[str] = []
    if seconds > 0:
        sentences.append(f"You spent **{format_duration(seconds)}** in voice on {day_name}.")
    else:
        sentences.append(f"You weren't in voice on {day_name}.")

    if previous_seconds is not None:
        compared = _comparison_sentence(
            seconds, previous_seconds, (target_date - timedelta(days=1)).strftime("%A")
        )
        if compared:
            sentences.append(compared)

    closing = _closing_sentence(seconds, rank, people)
    if closing:
        sentences.append(closing)

    embed = discord.Embed(
        title=heading,
        description=" ".join(sentences),
        colour=0x007AFF,
    )

    # The description already says your own time, so the fields only add what
    # the sentence cannot: where you landed, and what the whole server did.
    if rank is not None and people > 1:
        rank_line = f"#{rank}, out of {people}"
    elif rank is not None:
        rank_line = "the only one in there"
    else:
        rank_line = "—"
    embed.add_field(name="On the board", value=rank_line, inline=True)
    embed.add_field(
        name="Everyone together",
        value=format_duration(server_total_seconds),
        inline=True,
    )
    embed.set_footer(text=guild_name)
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
        self.page_size = IMAGE_PAGE_SIZE
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
        try:
            members = await self.tracker.get_live_leaderboard(self.guild, self.period)
        except VoiceStoreUnavailable:
            await interaction.response.send_message(
                "the voice database is temporarily unreachable — try again in a "
                "minute. your time is still being tracked.",
                ephemeral=True,
            )
            return
        except VoiceStoreError:
            log.exception("Leaderboard refresh failed")
            await interaction.response.send_message(
                "something went wrong reading the voice database.", ephemeral=True
            )
            return
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
                subtitle_override=leaderboard_subtitle(
                    members, page=self.page, total_pages=total_pages
                ),
            )
            file = discord.File(io.BytesIO(image_bytes), filename="voice_leaderboard.png")
            embed = leaderboard_image_embed(
                self.guild, self.period, page=self.page, total_pages=total_pages
            )
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
