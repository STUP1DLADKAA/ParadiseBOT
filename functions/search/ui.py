"""Discord UI components for search answers and paginated results."""

from __future__ import annotations

import logging
import re

import discord

from functions.helpers import Colour, shorten
from .models import SearchAnswer, SearchResponse

log = logging.getLogger(__name__)

_MAX_EMBED = 4000


def _drop_repeated_sentences(summary: str, direct_answer: str) -> str:
    """Remove summary sentences already covered by the quick answer."""
    anchor = direct_answer.casefold()
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", summary) if s.strip()]
    kept = [s for s in sentences if s.rstrip(".!?").casefold()[:60] not in anchor]
    return " ".join(kept)


def build_answer_embed(answer: SearchAnswer) -> discord.Embed:
    """The search-engine style card: answer first, sources as evidence."""
    query = shorten(answer.query, 80)
    embed = discord.Embed(title=f"🔎 {query}", colour=Colour.ACCENT)

    if not answer.has_answer and not answer.has_sources:
        embed.description = (
            f"I couldn't retrieve reliable information for "
            f"**{discord.utils.escape_markdown(answer.query)}** right now. "
            "Try again in a moment, or rephrase the search."
        )
        return embed

    description: list[str] = []
    if answer.direct_answer:
        description.append(f"**Quick Answer**\n{shorten(answer.direct_answer, 700)}")
    elif answer.summary:
        description.append(shorten(answer.summary, 700))
    elif answer.has_sources:
        description.append("Here's what the web says — open the sources below for the details.")
    embed.description = "\n\n".join(description)[:_MAX_EMBED]

    if answer.direct_answer and answer.summary and answer.generated_by == "local":
        # Only add the summary when it says something the quick answer didn't.
        extra = answer.summary
        anchor = answer.direct_answer.rstrip(".!?").casefold()
        if extra.rstrip(".!?").casefold()[:80] and anchor[:80] not in extra.casefold():
            extra = extra[:1].upper() + extra[1:] if extra else extra
            embed.add_field(name="In short", value=shorten(extra, 500), inline=False)
        else:
            remainder = _drop_repeated_sentences(extra, answer.direct_answer)
            if len(remainder) >= 40:
                embed.add_field(name="In short", value=shorten(remainder, 500), inline=False)
    if answer.key_points:
        points = "\n".join(f"• {point}" for point in answer.key_points[:4])
        embed.add_field(name="Key points", value=points[:1000], inline=False)

    if answer.warnings:
        embed.add_field(
            name="⚠️ Heads-up",
            value="\n".join(answer.warnings[:2])[:600],
            inline=False,
        )

    if answer.sources:
        source_lines = []
        for index, result in enumerate(answer.sources[:5], start=1):
            badge = " ⭐" if result.authoritative else ""
            source_lines.append(
                f"`{index}.` [{shorten(result.title, 72)}]({result.url}) — {result.source}{badge}"
            )
        embed.add_field(name="Sources", value="\n".join(source_lines)[:1000], inline=False)

    footer_bits = ["answer from retrieved sources"]
    if answer.answer_source and answer.answer_source not in footer_bits:
        footer_bits.append(f"via {answer.answer_source}")
    if answer.cached:
        footer_bits.append("cached")
    embed.set_footer(text=" • ".join(footer_bits)[:220])
    return embed


class SearchAnswerView(discord.ui.View):
    """Buttons on an answer card: swap into full results, or jump to sources."""

    def __init__(
        self,
        answer: SearchAnswer,
        response: SearchResponse | None,
        author_id: int,
        timeout: float = 240.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.answer = answer
        self.response = response
        self.author_id = author_id
        self.message: discord.Message | None = None
        if not (response and response.has_results):
            self.browse_btn.disabled = True
        for result in answer.sources[:2]:
            self.add_item(
                discord.ui.Button(
                    label=shorten(result.source, 40),
                    style=discord.ButtonStyle.link,
                    url=result.url,
                )
            )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "only the person who searched can flip this card.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True  # type: ignore[attr-defined]
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.DiscordException:
                pass

    @discord.ui.button(label="📑 Browse results", style=discord.ButtonStyle.secondary)
    async def browse_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not (self.response and self.response.has_results):
            await interaction.response.send_message("no extra results to browse.", ephemeral=True)
            return
        embed = build_search_embed(self.response, current_index=0)
        view = SearchPaginationView(self.response, author_id=self.author_id)
        await interaction.response.edit_message(embed=embed, view=view)
        view.message = interaction.message
        self.stop()

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
