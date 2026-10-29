"""Embeds for the music panel, the queue and "now playing".

Kept apart from the cog so the rendering can be tested by handing it a
:class:`PlayerState` — no gateway, no player, no audio.
"""

from __future__ import annotations

import discord

from functions.helpers import Colour

from .models import PlayerState, Track, format_duration

ACCENT = Colour.ACCENT
FOOTER = "Paradise · music"
BAR_WIDTH = 14


def progress_bar(position: float, duration: float, width: int = BAR_WIDTH) -> str:
    """``▬▬▬▬🔘▬▬▬▬▬`` — a fixed-width bar that never jitters."""
    if duration <= 0:
        return "🔴 live"
    ratio = max(0.0, min(1.0, position / duration))
    index = min(width, max(0, round(ratio * width)))
    return "▬" * index + "🔘" + "▬" * (width - index)


def _requester(track: Track) -> str:
    return f" · added by {track.requester_label}" if track.requester_label else ""


def now_playing_embed(state: PlayerState) -> discord.Embed:
    """The card shown by ``/music nowplaying`` and at the top of the panel."""
    if state.current is None:
        embed = discord.Embed(
            title="Nothing playing",
            description="the queue is empty. try `/music play <song>` or type `!p <song>`.",
            colour=ACCENT,
        )
        if state.upcoming:
            embed.add_field(
                name=f"Up next ({len(state.upcoming)})",
                value=_queue_lines(state.upcoming, limit=5),
                inline=False,
            )
        return embed

    track = state.current
    embed = discord.Embed(
        title=track.title[:240],
        url=track.url or None,
        colour=ACCENT,
        description=(
            f"{progress_bar(state.position, track.duration)}\n"
            f"`{format_duration(state.position)}` / `{track.duration_text}`"
        ),
    )
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    embed.add_field(name="Volume", value=f"{round(state.volume * 100)}%", inline=True)
    embed.add_field(
        name="Loop", value=state.loop.label, inline=True
    )
    embed.add_field(
        name="Status",
        value=(
            "⏸ paused"
            if state.paused
            else ("🗣 talking over it" if state.speaking else "▶ playing")
        ),
        inline=True,
    )
    if state.upcoming:
        embed.add_field(
            name=f"Up next ({len(state.upcoming)})",
            value=_queue_lines(state.upcoming, limit=5),
            inline=False,
        )
    embed.set_footer(text=f"{FOOTER} · {track.source}{_requester(track)}")
    return embed


def _queue_lines(tracks, *, limit: int = 10, start: int = 1) -> str:
    lines = []
    for offset, track in enumerate(tracks[:limit]):
        lines.append(
            f"`{start + offset}.` {track.title[:70]} — `{track.duration_text}`"
        )
    if len(tracks) > limit:
        lines.append(f"…and {len(tracks) - limit} more")
    return "\n".join(lines) or "empty"


def queue_embed(state: PlayerState, *, per_page: int = 10, page: int = 1) -> discord.Embed:
    """The queue, paged, with the current song at the top."""
    embed = discord.Embed(title="Queue", colour=ACCENT)
    if state.current is not None:
        embed.add_field(
            name="Now playing",
            value=(
                f"{state.current.title[:120]} — `{state.current.duration_text}`"
                f"{_requester(state.current)}"
            ),
            inline=False,
        )
    upcoming = state.upcoming
    if not upcoming:
        embed.add_field(name="Up next", value="nothing queued.", inline=False)
    else:
        total_pages = max(1, (len(upcoming) + per_page - 1) // per_page)
        page = max(1, min(total_pages, page))
        start = (page - 1) * per_page
        chunk = upcoming[start : start + per_page]
        embed.add_field(
            name=f"Up next ({len(upcoming)})",
            value=_queue_lines(chunk, limit=per_page, start=start + 1),
            inline=False,
        )
        embed.set_footer(
            text=f"{FOOTER} · page {page}/{total_pages} · "
            f"{format_duration(sum(t.duration for t in upcoming))} queued"
        )
    return embed


def panel_embed(state: PlayerState, *, prefix: str = "!", extra: str = "") -> discord.Embed:
    """The `/cmd` panel: what is playing, what is next, and how to drive it."""
    embed = discord.Embed(title="🎛 Music controls", colour=ACCENT)
    if state.current is None:
        embed.description = "nothing is playing right now."
    else:
        track = state.current
        embed.description = (
            f"**{track.title[:180]}**\n"
            f"{progress_bar(state.position, track.duration)} "
            f"`{format_duration(state.position)}` / `{track.duration_text}`"
        )
        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)
    embed.add_field(name="Volume", value=f"{round(state.volume * 100)}%", inline=True)
    embed.add_field(name="Loop", value=state.loop.label, inline=True)
    embed.add_field(
        name="Status", value="⏸ paused" if state.paused else "▶ playing", inline=True
    )
    if state.upcoming:
        embed.add_field(
            name=f"Up next ({len(state.upcoming)})",
            value=_queue_lines(state.upcoming, limit=4),
            inline=False,
        )
    embed.add_field(
        name="Type these in chat",
        value=(
            f"`{prefix}p <song>` play · `{prefix}pause` · `{prefix}r` resume · "
            f"`{prefix}s` skip · `{prefix}stop`\n"
            f"`{prefix}q` queue · `{prefix}np` now playing · "
            f"`{prefix}vol 40` volume · `{prefix}loop` · `{prefix}leave`"
        ),
        inline=False,
    )
    embed.add_field(
        name="Or just talk",
        value=(
            "**Hey Paradise play billie jean**\n"
            "**Hey Paradise skip** · **Hey Paradise pause** · "
            "**Hey Paradise volume 30**"
        ),
        inline=False,
    )
    if extra:
        embed.add_field(name="​", value=extra, inline=False)
    embed.set_footer(text=FOOTER)
    return embed


def help_embed(prefix: str = "!") -> discord.Embed:
    """A compact command reference (`/cmd` without a running player)."""
    embed = discord.Embed(
        title="Music commands",
        description=(
            "Paradise plays music in the voice channel you are sitting in, and "
            "**talks over it** — the song drops to 15 % while an answer is "
            "spoken, then comes straight back up."
        ),
        colour=ACCENT,
    )
    embed.add_field(
        name="Slash",
        value=(
            "`/music play <song>` · `/music pause` · `/music resume`\n"
            "`/music skip` · `/music stop` · `/music queue`\n"
            "`/music volume <0-200>` · `/music loop` · `/music leave`\n"
            "`/cmd` — the button panel"
        ),
        inline=False,
    )
    embed.add_field(
        name="Text",
        value=(
            f"`{prefix}p <song>` · `{prefix}pause` · `{prefix}r` · `{prefix}s` "
            f"· `{prefix}stop` · `{prefix}q` · `{prefix}np` · `{prefix}vol 40` "
            f"· `{prefix}loop` · `{prefix}shuffle` · `{prefix}leave`"
        ),
        inline=False,
    )
    embed.add_field(
        name="Voice",
        value=(
            "“**Hey Paradise** play billie jean” · “**Hey Paradise** skip” · "
            "“**Hey Paradise** pause” · “**Hey Paradise** volume 20” · "
            "“billie jean baja do”"
        ),
        inline=False,
    )
    embed.set_footer(text=FOOTER)
    return embed

