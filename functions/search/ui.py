"""Discord UI components for paginated search results."""

from __future__ import annotations

import logging
from typing import Sequence

import discord

from functions.helpers import Colour, shorten
from .models import SearchResponse, SearchResult

log = logging.getLogger(__name__)


def build_search_embed(
    response: SearchResponse, current_index: int = 0
) -> discord.Embed:
    """Build a rich embed for a single search result with pagination metadata."""
    if not response.results:
        embed = discord.Embed(
            title="🔍 Web Search",
            description=f"No results found for **{discord.utils.escape_markdown(response.query)}**.\n\nTry refining your query or keywords.",
            colour=Colour.ACCENT,
        )
        return embed

    total = len(response.results)
    idx = max(0, min(current_index, total - 1))
    result = response.results[idx]

    embed = discord.Embed(
        title=f"🔍 {shorten(response.query, 60)}",
        colour=Colour.ACCENT,
    )

    # Clean clickable title link
    auth_badge = " ⭐ (Authoritative)" if result.authoritative else ""
    embed.add_field(
        name=f"[{idx + 1}/{total}] {shorten(result.title, 80)}{auth_badge}",
        value=f"**Link:** [{result.source}]({result.url})\n\n{result.snippet or '_No preview snippet available._'}",
        inline=False,
    )

    # Overview of other results
    other_lines = []
    for i, other in enumerate(response.results):
        if i == idx:
            other_lines.append(f"**▶ `{i + 1}.` [{shorten(other.title, 40)}]({other.url})**")
        else:
            other_lines.append(f"`{i + 1}.` [{shorten(other.title, 40)}]({other.url})")

    if len(response.results) > 1:
        embed.add_field(
            name="All Results",
            value="\n".join(other_lines[:6]),
            inline=False,
        )

    footer_parts = [f"Result {idx + 1} of {total}", f"Source: {result.source}"]
    if response.cached:
        footer_parts.append("cached")
    embed.set_footer(text=" • ".join(footer_parts))

    return embed


class SearchPaginationView(discord.ui.View):
    """Interactive view for browsing search results."""

    def __init__(
        self,
        response: SearchResponse,
        author_id: int,
        timeout: float = 180.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.response = response
        self.author_id = author_id
        self.current_index = 0
        self.message: discord.Message | None = None
        self._link_button: discord.ui.Button | None = None
        self._rebuild_items()

    def _rebuild_items(self) -> None:
        total = len(self.response.results)
        self.prev_btn.disabled = self.current_index <= 0
        self.next_btn.disabled = self.current_index >= total - 1

        # Remove previous link button if any
        if self._link_button is not None:
            self.remove_item(self._link_button)
            self._link_button = None

        if total > 0 and 0 <= self.current_index < total:
            url = self.response.results[self.current_index].url
            self._link_button = discord.ui.Button(
                label="Open Link",
                style=discord.ButtonStyle.link,
                url=url,
            )
            # Insert before dismiss button
            self.add_item(self._link_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "only the person who searched can click these buttons.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, (discord.ui.Button, discord.ui.Select)):
                item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.DiscordException:
                pass

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self.current_index > 0:
            self.current_index -= 1
        self._rebuild_items()
        embed = build_search_embed(self.response, self.current_index)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self.current_index < len(self.response.results) - 1:
            self.current_index += 1
        self._rebuild_items()
        embed = build_search_embed(self.response, self.current_index)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="🗑️", style=discord.ButtonStyle.danger)
    async def dismiss_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.stop()
        try:
            await interaction.message.delete()
        except discord.DiscordException:
            await interaction.response.edit_message(
                content="Search closed.", embed=None, view=None
            )
