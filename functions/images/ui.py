"""Discord presentation for web image results.

Images are embedded by remote URL — Paradise never downloads or
re-uploads the file. Each embed carries the subject and, when the
provider supplied one, a link back to the page the image came from.
"""

from __future__ import annotations

import discord

from functions.helpers import Colour, shorten
from .models import ImageResponse, ImageResult

# Discord renders up to 10 embeds per message; a handful is plenty and
# keeps the channel readable.
MAX_EMBEDS = 4

NO_IMAGE_LINE = "🖼️ I couldn't find a usable image for that right now."


def _caption(result: ImageResult) -> str:
    bits: list[str] = []
    if result.has_source_page:
        bits.append(f"[{shorten(result.source, 40)}]({result.source_url})")
    elif result.source:
        bits.append(shorten(result.source, 40))
    if result.attribution:
        bits.append(shorten(result.attribution, 48))
    return " · ".join(bits)


def build_image_embeds(response: ImageResponse, limit: int = 1) -> list[discord.Embed]:
    """Build one compact embed per image (never a huge gallery)."""
    count = max(1, min(int(limit or 1), MAX_EMBEDS))
    embeds: list[discord.Embed] = []
    subject = shorten(response.query.strip() or "image", 80)

    for index, result in enumerate(response.results[:count]):
        title = subject if index == 0 else shorten(result.title or subject, 80)
        embed = discord.Embed(title=f"🖼️ {title}", colour=Colour.ACCENT)
        embed.set_image(url=result.image_url)
        caption = _caption(result)
        if caption:
            embed.description = f"-# {caption}"
        if index == len(response.results[:count]) - 1 and result.provider:
            embed.set_footer(text=f"via {result.provider}")
        embeds.append(embed)
    return embeds


__all__ = ["MAX_EMBEDS", "NO_IMAGE_LINE", "build_image_embeds"]
