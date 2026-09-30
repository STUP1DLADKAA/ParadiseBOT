"""Discord UI embeds and formatters for time and timezone information."""

from __future__ import annotations

import discord

from functions.helpers import Colour
from .zones import TimeInfo


def build_time_embed(info: TimeInfo, user_target: discord.User | discord.Member | None = None) -> discord.Embed:
    """Build a rich embed displaying resolved local time."""
    if user_target is not None:
        title = f"🕒 Local Time for {user_target.display_name}"
    else:
        title = f"🕒 Local Time in {info.location_or_target}"

    embed = discord.Embed(
        title=title,
        colour=Colour.ACCENT,
    )

    embed.add_field(
        name="🕐 Current Time",
        value=f"### {info.formatted_12h}\n`24-hour: {info.formatted_24h}`",
        inline=False,
    )

    embed.add_field(
        name="📅 Date",
        value=info.formatted_date,
        inline=True,
    )

    embed.add_field(
        name="🌐 Timezone & Offset",
        value=f"`{info.tz_name}` ({info.abbreviation})\n**Offset:** `{info.utc_offset}`",
        inline=True,
    )

    embed.add_field(
        name="⏱️ Discord Timestamp",
        value=f"<t:{info.epoch_timestamp}:F>\n(<t:{info.epoch_timestamp}:R>)",
        inline=False,
    )

    if user_target is not None:
        embed.set_author(name=user_target.display_name, icon_url=user_target.display_avatar.url)
        embed.set_footer(text=f"Configured timezone for {user_target.display_name} • Change with /settimezone")
    else:
        embed.set_footer(text=f"Timezone: {info.tz_name} ({info.utc_offset})")

    return embed
