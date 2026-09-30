"""Discord UI embed for conversion results."""

from __future__ import annotations

import discord

from functions.helpers import Colour
from .models import ConversionCategory, ConversionResult


def build_conversion_embed(result: ConversionResult) -> discord.Embed:
    """Build a clean, distinct Discord Embed for conversion results."""
    title = f"🔄 {result.category.value}"
    embed = discord.Embed(
        title=title,
        description=f"### {result.display_text}",
        colour=Colour.ACCENT,
    )

    if result.category == ConversionCategory.CURRENCY:
        fields = []
        if result.rate_info:
            fields.append(f"**Rate:** `{result.rate_info}`")
        if result.rate_timestamp:
            fields.append(f"**Status:** {result.rate_timestamp}")
        if fields:
            embed.add_field(
                name="Exchange Details",
                value="\n".join(fields),
                inline=False,
            )
        embed.set_footer(text="Live rates updated hourly • Reference data fallback")
    else:
        embed.set_footer(text=f"Unit Conversion • {result.category.value}")

    return embed
