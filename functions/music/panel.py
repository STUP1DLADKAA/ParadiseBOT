"""The Paradise music panel: rendering, buttons, modals, pagination.

This module knows nothing about Lavalink. It renders plain view-models
(``PanelState``) and calls back into the music service, so buttons and
slash commands share one validation path.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

import discord

from functions.helpers import (
    Colour,
    MusicError,
    format_duration,
    progress_bar,
    send_ephemeral,
    shorten,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# View models
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TrackInfo:
    title: str
    author: str
    uri: str | None
    duration_ms: int
    is_stream: bool
    requester_id: int | None
    source: str

    @classmethod
    def from_track(cls, track) -> "TrackInfo":
        requester = getattr(track, "requester", 0) or None
        return cls(
            title=getattr(track, "title", "unknown"),
            author=getattr(track, "author", "unknown"),
            uri=getattr(track, "uri", None),
            duration_ms=getattr(track, "duration", 0) or 0,
            is_stream=bool(getattr(track, "is_stream", False)),
            requester_id=requester,
            source=getattr(track, "source_name", "unknown"),
        )

    @property
    def duration_label(self) -> str:
        return "live" if self.is_stream else format_duration(self.duration_ms)


@dataclass(slots=True)
class PanelState:
    guild_id: int
    status: str  # disconnected | idle | playing | paused | speaking
    voice_channel: str | None
    track: TrackInfo | None = None
    spoken_text: str | None = None
    resumes_after: TrackInfo | None = None
    position_ms: int = 0
    upcoming: list[TrackInfo] = field(default_factory=list)
    volume: int = 100
    last_action: str | None = None

    @property
    def connected(self) -> bool:
        return self.status != "disconnected"

    @property
    def queue_duration_ms(self) -> int:
        if any(track.is_stream for track in self.upcoming):
            return 0
        return sum(track.duration_ms for track in self.upcoming)


@dataclass(slots=True)
class PlayResult:
    track: TrackInfo
    position: int  # 1-based queue position; 0 == started immediately
    started: bool


@dataclass(slots=True)
class JoinResult:
    action: str  # joined | moved | already
    channel_name: str


@dataclass(slots=True)
class SkipResult:
    skipped: TrackInfo
    up_next: TrackInfo | None


_STATUS_LABEL = {
    "playing": "▶ Playing",
    "paused": "⏸ Paused",
    "speaking": "🔊 Speaking",
    "idle": "Idle",
    "disconnected": "Disconnected",
}

_STATUS_COLOUR = {
    "playing": Colour.PLAYING,
    "paused": Colour.PAUSED,
    "speaking": Colour.SPEAKING,
    "idle": Colour.IDLE,
    "disconnected": Colour.DISCONNECTED,
}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_panel(state: PanelState) -> discord.Embed:
    embed = discord.Embed(
        title="🎵 Paradise Music",
        colour=_STATUS_COLOUR.get(state.status, Colour.DISCONNECTED),
    )

    if state.status == "disconnected":
        embed.description = (
            "🔌 I'm not in voice right now.\n"
            "use `/join` or the ▶ button to bring me into your channel."
        )
    elif state.status == "idle":
        embed.description = (
            "📭 nothing is currently playing.\n"
            "add something with `/play` or the ▶ button."
        )
    elif state.status == "speaking":
        spoken = shorten(state.spoken_text or "", 120) or "…"
        embed.description = f"🔊 *“{spoken}”*"
        if state.resumes_after is not None:
            embed.description += (
                f"\n\nresuming **{shorten(state.resumes_after.title, 60)}** after this."
            )
    else:
        assert state.track is not None
        embed.description = (
            f"**{shorten(state.track.title, 90)}**\n"
            f"{shorten(state.track.author, 80)}\n"
            f"requested by <@{state.track.requester_id}>"
            if state.track.requester_id
            else f"**{shorten(state.track.title, 90)}**\n{shorten(state.track.author, 80)}"
        )

    if state.last_action and state.status != "disconnected":
        embed.description = (
            f"{embed.description}\n-# {shorten(state.last_action, 90)}"
        )

    if state.connected:
        embed.add_field(
            name="Status", value=_STATUS_LABEL.get(state.status, "—"), inline=True
        )
        if state.track and state.status in ("playing", "paused"):
            if state.track.is_stream:
                progress = "🔴 live"
            else:
                bar = progress_bar(state.position_ms, state.track.duration_ms)
                progress = (
                    f"`{format_duration(state.position_ms)} / "
                    f"{format_duration(state.track.duration_ms)}`\n{bar}"
                )
            embed.add_field(name="Progress", value=progress, inline=True)
        else:
            embed.add_field(name="Progress", value="—", inline=True)

        queue_value = f"{len(state.upcoming)} upcoming"
        if state.upcoming and state.queue_duration_ms:
            queue_value += f" · {format_duration(state.queue_duration_ms)}"
        embed.add_field(name="Queue", value=queue_value, inline=True)
        embed.add_field(name="Volume", value=f"{state.volume}%", inline=True)
        embed.add_field(
            name="Voice", value=state.voice_channel or "unknown", inline=True
        )

    embed.set_footer(text="buttons below mirror the commands · /help · /settings")
    return embed


def render_now_playing(state: PanelState) -> discord.Embed:
    embed = discord.Embed(colour=_STATUS_COLOUR.get(state.status, Colour.IDLE))
    if state.status == "disconnected":
        embed.title = "🎵 Now playing"
        embed.description = "🔌 I'm not connected to voice right now."
        return embed
    if state.status == "idle":
        embed.title = "🎵 Now playing"
        embed.description = "📭 nothing is playing right now."
        embed.set_footer(text="add something with /play or the ▶ button")
        return embed

    if state.status == "speaking":
        embed.title = "🔊 Speaking"
        embed.description = f"*“{shorten(state.spoken_text or '', 200)}”*"
        if state.resumes_after is not None:
            embed.description += (
                f"\n\nresuming **{shorten(state.resumes_after.title, 60)}** after this."
            )
        embed.add_field(name="Status", value="Speaking", inline=True)
        embed.add_field(name="Voice", value=state.voice_channel or "—", inline=True)
        embed.add_field(name="Volume", value=f"{state.volume}%", inline=True)
        return embed

    track = state.track
    embed.title = "🎵 Now playing"
    embed.description = f"**{shorten(track.title, 120)}**\n{shorten(track.author, 100)}"
    if track.uri:
        embed.description += f"\n-# [source]({track.uri})"
    embed.add_field(
        name="Status",
        value=_STATUS_LABEL.get(state.status, "—"),
        inline=True,
    )
    if track.is_stream:
        embed.add_field(name="Progress", value="🔴 live", inline=True)
        embed.add_field(name="Duration", value="live", inline=True)
    else:
        bar = progress_bar(state.position_ms, track.duration_ms)
        embed.add_field(
            name="Progress",
            value=f"`{format_duration(state.position_ms)} / {format_duration(track.duration_ms)}`\n{bar}",
            inline=True,
        )
        embed.add_field(name="Duration", value=track.duration_label, inline=True)
    embed.add_field(
        name="Requested by",
        value=f"<@{track.requester_id}>" if track.requester_id else "someone",
        inline=True,
    )
    embed.add_field(name="Source", value=track.source, inline=True)
    embed.add_field(name="Volume", value=f"{state.volume}%", inline=True)
    embed.set_footer(
        text=f"{len(state.upcoming)} in queue · {state.voice_channel or 'voice'}"
    )
    return embed


def queue_page_count(state: PanelState, per_page: int = 8) -> int:
    return max(1, math.ceil(len(state.upcoming) / per_page))


def render_queue_page(state: PanelState, page: int, per_page: int = 8) -> discord.Embed:
    embed = discord.Embed(title="📜 Queue", colour=Colour.IDLE)
    if state.status == "disconnected":
        embed.description = "🔌 I'm not connected to voice right now."
        return embed

    if state.track is not None:
        now = f"▶ **{shorten(state.track.title, 70)}** · {shorten(state.track.author, 40)}"
        if not state.track.is_stream:
            now += f" · {state.track.duration_label}"
        embed.description = now
    else:
        embed.description = "not playing anything right now."

    if not state.upcoming:
        embed.add_field(
            name="Up next",
            value="📭 queue is empty.\nadd something with `/play` or the ▶ button.",
            inline=False,
        )
        return embed

    pages = queue_page_count(state, per_page)
    page = max(0, min(page, pages - 1))
    start = page * per_page
    lines = []
    for offset, track in enumerate(state.upcoming[start : start + per_page]):
        position = start + offset + 1
        line = f"`{position}.` **{shorten(track.title, 70)}**"
        meta = [track.duration_label]
        if track.requester_id:
            meta.append(f"<@{track.requester_id}>")
        lines.append(f"{line} · {' · '.join(meta)}")
    embed.add_field(name="Up next", value="\n".join(lines), inline=False)

    total = f"{len(state.upcoming)} queued"
    if state.queue_duration_ms:
        total += f" · {format_duration(state.queue_duration_ms)} total"
    embed.set_footer(text=f"page {page + 1}/{pages} · {total} · /shuffle · /clearqueue")
    return embed


def button_states(state: PanelState) -> dict[str, bool]:
    return {
        "play": True,
        "pause": state.status == "playing",
        "resume": state.status == "paused",
        "skip": state.status in ("playing", "paused", "speaking"),
        "stop": state.connected,
        "queue": state.connected,
        "nowplaying": state.connected,
        "volume": state.connected,
        "join": True,
    }


# ---------------------------------------------------------------------------
# Buttons / modals
# ---------------------------------------------------------------------------


def _play_confirmation(result: PlayResult) -> str:
    track = result.track
    body = f"**{shorten(track.title, 90)}**\n{shorten(track.author, 80)}"
    if result.started:
        return f"🎵 Now playing\n\n{body}"
    return f"🎵 Added to queue\n\n{body}\n\nPosition #{result.position}"


class PlayModal(discord.ui.Modal, title="Play something"):
    query = discord.ui.TextInput(
        label="Song name or URL",
        placeholder="never gonna give you up",
        max_length=200,
        required=True,
    )

    def __init__(self, service, guild_id: int):
        super().__init__(timeout=300)
        self.service = service
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        query = (self.query.value or "").strip()
        if not query:
            await send_ephemeral(interaction, "you have to tell me what to play.")
            return
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await send_ephemeral(interaction, "music only works in a server.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await self.service.play(guild, member, query)
        except MusicError as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        except Exception:
            log.exception("Play modal failed")
            await interaction.followup.send(
                "that didn't work on my end. it's been logged.", ephemeral=True
            )
            return
        await interaction.followup.send(_play_confirmation(result), ephemeral=True)
        fallback = (
            guild.get_channel(interaction.channel_id)
            if interaction.channel_id is not None
            else None
        )
        await self.service.refresh_panel(self.guild_id, fallback=fallback)


class VolumeModal(discord.ui.Modal, title="Volume"):
    def __init__(self, service, guild_id: int, max_volume: int):
        super().__init__(timeout=300)
        self.service = service
        self.guild_id = guild_id
        self.max_volume = max_volume
        self.value = discord.ui.TextInput(
            label=f"Volume (0–{max_volume})",
            placeholder="75",
            max_length=4,
            required=True,
        )
        self.add_item(self.value)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = (self.value.value or "").strip().rstrip("%").strip()
        try:
            volume = int(raw)
        except ValueError:
            await send_ephemeral(
                interaction, f"volume needs to be a number between 0 and {self.max_volume}."
            )
            return
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await send_ephemeral(interaction, "music only works in a server.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.service.set_volume(guild, member, volume)
        except MusicError as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        except Exception:
            log.exception("Volume modal failed")
            await interaction.followup.send(
                "that didn't work on my end. it's been logged.", ephemeral=True
            )
            return
        await interaction.followup.send(
            f"🔊 volume set to {volume}%.", ephemeral=True
        )
        await self.service.refresh_panel(self.guild_id)


class MusicPanelView(discord.ui.View):
    """Persistent music panel controls. One view per guild panel message."""

    def __init__(self, service, guild_id: int, enabled: dict[str, bool]):
        super().__init__(timeout=None)
        self.service = service
        self.guild_id = guild_id
        states = {
            "play": True, "pause": False, "resume": False, "skip": False,
            "stop": False, "queue": False, "nowplaying": False, "volume": False,
            "join": True,
        }
        states.update(enabled or {})
        self.play_button.disabled = not states["play"]
        self.pause_button.disabled = not states["pause"]
        self.resume_button.disabled = not states["resume"]
        self.skip_button.disabled = not states["skip"]
        self.stop_button.disabled = not states["stop"]
        self.queue_button.disabled = not states["queue"]
        self.nowplaying_button.disabled = not states["nowplaying"]
        self.volume_button.disabled = not states["volume"]
        self.join_button.disabled = not states["join"]

    # -- helpers ----------------------------------------------------------

    async def _member(self, interaction: discord.Interaction):
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await send_ephemeral(interaction, "music only works in a server.")
            return None, None
        return guild, member

    async def _run(self, interaction: discord.Interaction, action, *, quiet: bool = False):
        """Ack, run a service action, then refresh the panel in place."""
        guild, member = await self._member(interaction)
        if guild is None:
            return
        try:
            await interaction.response.defer()
        except discord.DiscordException:
            pass
        try:
            result = await action(guild, member)
        except MusicError as error:
            await send_ephemeral(interaction, str(error))
            return
        except Exception:
            log.exception("Panel action failed")
            await send_ephemeral(interaction, "that broke on my end. it's been logged.")
            return
        if not quiet and result is not None:
            await send_ephemeral(interaction, str(result))
        await self.service.refresh_panel(guild.id)

    # -- row 1 ------------------------------------------------------------

    @discord.ui.button(
        label="Play", emoji="▶️", style=discord.ButtonStyle.primary,
        custom_id="paradise:music:play", row=0,
    )
    async def play_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(
            PlayModal(self.service, self.guild_id)
        )

    @discord.ui.button(
        label="Pause", emoji="⏸️", style=discord.ButtonStyle.secondary,
        custom_id="paradise:music:pause", row=0,
    )
    async def pause_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        async def action(guild, member):
            await self.service.pause(guild, member)
            return "⏸️ paused."

        await self._run(interaction, action)

    @discord.ui.button(
        label="Resume", emoji="▶️", style=discord.ButtonStyle.secondary,
        custom_id="paradise:music:resume", row=0,
    )
    async def resume_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        async def action(guild, member):
            await self.service.resume(guild, member)
            return "▶️ playing again."

        await self._run(interaction, action)

    @discord.ui.button(
        label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary,
        custom_id="paradise:music:skip", row=0,
    )
    async def skip_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        async def action(guild, member):
            result = await self.service.skip(guild, member)
            if result.up_next is None:
                return "⏭️ skipped. nothing else in the queue."
            return f"⏭️ skipped. next: **{shorten(result.up_next.title, 60)}**"

        await self._run(interaction, action)

    # -- row 2 ------------------------------------------------------------

    @discord.ui.button(
        label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger,
        custom_id="paradise:music:stop", row=1,
    )
    async def stop_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        async def action(guild, member):
            await self.service.stop(guild, member)
            return "⏹️ stopped and disconnected."

        await self._run(interaction, action)

    @discord.ui.button(
        label="Queue", emoji="📜", style=discord.ButtonStyle.secondary,
        custom_id="paradise:music:queue", row=1,
    )
    async def queue_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        guild, member = await self._member(interaction)
        if guild is None:
            return
        try:
            await self.service.require_connected_control(guild, member)
        except MusicError as error:
            await send_ephemeral(interaction, str(error))
            return
        state = self.service.panel_state(guild.id)
        view = QueuePaginator(self.service, guild.id)
        try:
            await interaction.response.send_message(
                embed=render_queue_page(state, 0),
                view=view,
                ephemeral=True,
            )
        except discord.DiscordException:
            log.debug("Queue button response failed", exc_info=True)

    @discord.ui.button(
        label="Now Playing", emoji="🎵", style=discord.ButtonStyle.secondary,
        custom_id="paradise:music:nowplaying", row=1,
    )
    async def nowplaying_button(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ):
        guild, member = await self._member(interaction)
        if guild is None:
            return
        try:
            await self.service.require_connected_control(guild, member)
        except MusicError as error:
            await send_ephemeral(interaction, str(error))
            return
        state = self.service.panel_state(guild.id)
        try:
            await interaction.response.send_message(
                embed=render_now_playing(state), ephemeral=True
            )
        except discord.DiscordException:
            log.debug("Now playing button response failed", exc_info=True)

    # -- row 3 ------------------------------------------------------------

    @discord.ui.button(
        label="Volume", emoji="🔊", style=discord.ButtonStyle.secondary,
        custom_id="paradise:music:volume", row=2,
    )
    async def volume_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(
            VolumeModal(self.service, self.guild_id, self.service.max_volume)
        )

    @discord.ui.button(
        label="Join", emoji="🔗", style=discord.ButtonStyle.secondary,
        custom_id="paradise:music:join", row=2,
    )
    async def join_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        async def action(guild, member):
            result = await self.service.join(guild, member)
            if result.action == "joined":
                return f"🔗 joined **{result.channel_name}**."
            if result.action == "moved":
                return f"🔗 moved to **{result.channel_name}**."
            return f"🔗 already in **{result.channel_name}**."

        await self._run(interaction, action)


class QueuePaginator(discord.ui.View):
    """Previous / Next navigation for the detailed queue view."""

    def __init__(self, service, guild_id: int):
        super().__init__(timeout=300)
        self.service = service
        self.guild_id = guild_id
        self.page = 0
        self._update_buttons()

    def _update_buttons(self) -> None:
        state = self.service.panel_state(self.guild_id)
        pages = queue_page_count(state)
        self.previous_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= pages - 1

    async def _show(self, interaction: discord.Interaction) -> None:
        state = self.service.panel_state(self.guild_id)
        self._update_buttons()
        try:
            await interaction.response.edit_message(
                embed=render_queue_page(state, self.page), view=self
            )
        except discord.DiscordException:
            log.debug("Queue pagination edit failed", exc_info=True)

    @discord.ui.button(
        label="Previous", emoji="◀️", style=discord.ButtonStyle.secondary, row=0
    )
    async def previous_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.page = max(0, self.page - 1)
        await self._show(interaction)

    @discord.ui.button(
        label="Next", emoji="▶️", style=discord.ButtonStyle.secondary, row=0
    )
    async def next_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.page += 1
        await self._show(interaction)
