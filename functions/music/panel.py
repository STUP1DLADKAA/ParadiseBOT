"""Discord-native music panel, view models and interactions.

The component layer consumes :class:`PanelState` only; it never talks to
Lavalink.  Service methods remain the single validation and mutation path —
every button here is a thin adapter over a MusicService call, exactly like
the slash commands.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

import discord

from functions import lyrics
from functions.helpers import (
    Colour,
    MusicError,
    format_duration,
    progress_bar,
    send_ephemeral,
    shorten,
)

log = logging.getLogger(__name__)


def _safe(value: str, width: int) -> str:
    value = discord.utils.escape_markdown(discord.utils.escape_mentions(value or "unknown"))
    return shorten(value, width)


@dataclass(slots=True)
class TrackInfo:
    title: str
    author: str
    uri: str | None
    duration_ms: int
    is_stream: bool
    requester_id: int | None
    source: str
    identifier: str | None = None
    artwork_url: str | None = None

    @classmethod
    def from_track(cls, track) -> "TrackInfo":
        requester = getattr(track, "requester", 0) or None
        artwork = getattr(track, "artwork_url", None)
        source = getattr(track, "source_name", "unknown") or "unknown"
        identifier = getattr(track, "identifier", None)
        # Older Lavalink payloads omit artworkUrl. YouTube thumbnails are stable
        # and need no network request or local file.
        if not artwork and identifier and source.casefold() in {"youtube", "ytmusic"}:
            artwork = f"https://i.ytimg.com/vi/{identifier}/hqdefault.jpg"
        if artwork and not str(artwork).startswith(("https://", "http://")):
            artwork = None
        return cls(
            title=str(getattr(track, "title", "unknown") or "unknown"),
            author=str(getattr(track, "author", "unknown") or "unknown"),
            uri=getattr(track, "uri", None),
            duration_ms=getattr(track, "duration", 0) or 0,
            is_stream=bool(getattr(track, "is_stream", False)),
            requester_id=requester,
            source=str(source),
            identifier=str(identifier) if identifier else None,
            artwork_url=str(artwork) if artwork else None,
        )

    @property
    def duration_label(self) -> str:
        return "LIVE" if self.is_stream else format_duration(self.duration_ms)

    @property
    def lyrics_key(self) -> str:
        return lyrics.lyrics_key(self.title, self.author, self.identifier)


@dataclass(slots=True)
class PanelState:
    guild_id: int
    status: str  # disconnected | idle | playing | paused | speaking | loading
    voice_channel: str | None
    track: TrackInfo | None = None
    spoken_text: str | None = None
    resumes_after: TrackInfo | None = None
    position_ms: int = 0
    upcoming: list[TrackInfo] = field(default_factory=list)
    volume: int = 100
    last_action: str | None = None
    lyrics_loaded: bool = False
    lyrics_preview: str | None = None
    lyrics_enabled: bool = True
    # Groove-style playback state surfaced by the service.
    loop_mode: str = "off"  # off | track | queue
    autoplay: bool = False
    has_previous: bool = False
    filters: list[str] = field(default_factory=list)  # active effect labels
    effects_available: bool = True

    @property
    def connected(self) -> bool:
        return self.status != "disconnected"

    @property
    def active_track(self) -> TrackInfo | None:
        return self.track or self.resumes_after

    @property
    def queue_duration_ms(self) -> int:
        if any(track.is_stream for track in self.upcoming):
            return 0
        return sum(track.duration_ms for track in self.upcoming)


@dataclass(slots=True)
class PlayResult:
    track: TrackInfo
    position: int
    started: bool


@dataclass(slots=True)
class JoinResult:
    action: str
    channel_name: str


@dataclass(slots=True)
class SkipResult:
    skipped: TrackInfo
    up_next: TrackInfo | None


@dataclass(slots=True)
class PreviousResult:
    track: TrackInfo


_STATUS_LABEL = {
    "playing": "▶ Playing", "paused": "⏸ Paused", "speaking": "Speaking",
    "loading": "Loading", "idle": "Idle", "disconnected": "Disconnected",
}
_STATUS_COLOUR = {
    "playing": Colour.PLAYING, "paused": Colour.PAUSED, "speaking": Colour.SPEAKING,
    "loading": Colour.IDLE, "idle": Colour.IDLE, "disconnected": Colour.DISCONNECTED,
}
_SOURCE_LABELS = {
    "youtube": "YouTube", "ytmusic": "YouTube Music", "soundcloud": "SoundCloud",
    "spotify": "Spotify", "http": "Web audio", "local": "Local",
}
_LOOP_LABEL = {"off": "Loop: Off", "track": "Loop: Track", "queue": "Loop: Queue"}


def _source_label(source: str) -> str:
    return _SOURCE_LABELS.get(source.casefold(), shorten(source.title(), 24))


def _progress_line(state: PanelState, track: TrackInfo) -> str:
    if track.is_stream:
        return "`LIVE`  ━━━━━━━━━━━"
    position = min(max(state.position_ms, 0), max(track.duration_ms, 0))
    return f"`{format_duration(position)}` {progress_bar(position, track.duration_ms, 13)} `{track.duration_label}`"


def _queue_preview(state: PanelState, limit: int = 3) -> str:
    if not state.upcoming:
        return "*Queue is empty — add another track to keep the music going.*"
    lines: list[str] = []
    for index, track in enumerate(state.upcoming[:limit], 1):
        lines.append(
            f"`{index:02}` **{_safe(track.title, 58)}**\n"
            f"-# {_safe(track.author, 45)} · {track.duration_label}"
        )
    remaining = len(state.upcoming) - limit
    if remaining > 0:
        lines.append(f"-# + {remaining} more in queue")
    return "\n".join(lines)


def _state_chips(state: PanelState) -> str:
    """One compact status line: state · volume · loop · autoplay · effects · voice."""
    chips = [f"**{_STATUS_LABEL.get(state.status, 'Idle')}**", f"Volume **{state.volume}%**"]
    loop_label = _LOOP_LABEL.get(state.loop_mode)
    if state.loop_mode != "off" and loop_label:
        chips.append(f"🔁 {loop_label.split(': ', 1)[-1]}")
    if state.autoplay:
        chips.append("♾️ Autoplay")
    if state.filters:
        chips.append(f"🎚 {' · '.join(shorten(label, 18) for label in state.filters)}")
    chips.append(f"**{_safe(state.voice_channel or 'Voice', 36)}**")
    return "  ·  ".join(chips)


# Legacy renderers remain useful for /nowplaying and backwards-compatible API
# consumers. The persistent panel and queue use Components V2 below.
def render_panel(state: PanelState) -> discord.Embed:
    embed = discord.Embed(title="🎵 Paradise Music", colour=_STATUS_COLOUR.get(state.status, Colour.IDLE))
    track = state.active_track
    if state.status == "disconnected":
        embed.description = "I'm not in voice right now.\nuse `/join` or Play to get started."
    elif state.status == "speaking":
        embed.description = f"*“{shorten(state.spoken_text or '…', 120)}”*"
        if track is not None:
            embed.description += f"\n\nresuming **{_safe(track.title, 60)}** after this."
    elif track is None:
        embed.description = "nothing is currently playing.\nadd something with `/play` or Play."
    else:
        requester = f"\nrequested by <@{track.requester_id}>" if track.requester_id else ""
        embed.description = f"**{_safe(track.title, 90)}**\n{_safe(track.author, 80)}{requester}"
    if state.last_action and state.status != "disconnected":
        embed.description = f"{embed.description}\n-# {shorten(state.last_action, 90)}"
    if state.connected:
        embed.add_field(name="Status", value=_STATUS_LABEL.get(state.status, "—"), inline=True)
        if track and track.is_stream:
            legacy_progress = "🔴 live"
        elif track:
            legacy_progress = (
                f"`{format_duration(state.position_ms)} / {track.duration_label}`\n"
                f"{progress_bar(state.position_ms, track.duration_ms)}"
            )
        else:
            legacy_progress = "—"
        embed.add_field(name="Progress", value=legacy_progress, inline=True)
        queue = f"{len(state.upcoming)} upcoming"
        if state.upcoming and state.queue_duration_ms:
            queue += f" · {format_duration(state.queue_duration_ms)}"
        embed.add_field(name="Queue", value=queue, inline=True)
        embed.add_field(name="Volume", value=f"{state.volume}%", inline=True)
        embed.add_field(name="Voice", value=state.voice_channel or "unknown", inline=True)
        loop_value = _LOOP_LABEL.get(state.loop_mode, "Loop: Off").split(": ")[1]
        embed.add_field(
            name="Loop / Autoplay",
            value=f"{loop_value}{' · autoplay' if state.autoplay else ''}",
            inline=True,
        )
    return embed


def render_now_playing(state: PanelState) -> discord.Embed:
    embed = discord.Embed(title="Now Playing", colour=_STATUS_COLOUR.get(state.status, Colour.IDLE))
    track = state.active_track
    if state.status == "disconnected" and track is None:
        embed.description = "I'm not connected to voice right now."
        return embed
    if track is None:
        embed.description = "nothing is playing right now."
        return embed
    embed.description = f"**{_safe(track.title, 120)}**\n{_safe(track.author, 100)}"
    if state.status == "speaking":
        embed.description += "\n\nSpeaking now; this track resumes after the voice message."
    embed.add_field(name="Status", value=_STATUS_LABEL.get(state.status, "—"), inline=True)
    embed.add_field(name="Progress", value=_progress_line(state, track), inline=True)
    embed.add_field(name="Requested by", value=f"<@{track.requester_id}>" if track.requester_id else "—", inline=True)
    embed.add_field(name="Source", value=track.source, inline=True)
    embed.add_field(name="Volume", value=f"{state.volume}%", inline=True)
    if track.artwork_url:
        embed.set_thumbnail(url=track.artwork_url)
    embed.set_footer(text=f"{len(state.upcoming)} in queue · {state.voice_channel or 'voice'}")
    return embed


def queue_page_count(state: PanelState, per_page: int = 8) -> int:
    return max(1, math.ceil(len(state.upcoming) / per_page))


def _queue_page_text(state: PanelState, page: int, per_page: int = 8) -> tuple[str, int, int]:
    pages = queue_page_count(state, per_page)
    page = max(0, min(page, pages - 1))
    if not state.upcoming:
        return "*queue is empty.*\nAdd music to keep the session going.", page, pages
    start = page * per_page
    lines = []
    for offset, track in enumerate(state.upcoming[start : start + per_page]):
        number = start + offset + 1
        requester = f" · <@{track.requester_id}>" if track.requester_id else ""
        lines.append(
            f"`{number:02}` **{_safe(track.title, 58)}**\n"
            f"-# {_safe(track.author, 40)} · {track.duration_label}{requester}"
        )
    return "\n".join(lines), page, pages


def render_queue_page(state: PanelState, page: int, per_page: int = 8) -> discord.Embed:
    """Legacy queue renderer retained for callers that explicitly need embeds."""
    embed = discord.Embed(title="Queue", colour=Colour.IDLE)
    track = state.active_track
    embed.description = (
        f"▶ **{_safe(track.title, 70)}** · {_safe(track.author, 40)}" if track
        else "not playing anything right now."
    )
    text, page, pages = _queue_page_text(state, page, per_page)
    embed.add_field(name="Up next", value=text, inline=False)
    total = f"{len(state.upcoming)} queued"
    if state.queue_duration_ms:
        total += f" · {format_duration(state.queue_duration_ms)} total"
    embed.set_footer(text=f"page {page + 1}/{pages} · {total}")
    return embed


def button_states(state: PanelState) -> dict[str, bool]:
    track = state.active_track
    return {
        "play": True,
        "pause": state.status == "playing",
        "resume": state.status == "paused" and track is not None,
        "skip": state.status in ("playing", "paused", "speaking"),
        "stop": state.connected,
        "queue": state.connected,
        "nowplaying": track is not None,
        "volume": state.connected,
        "lyrics": track is not None and state.lyrics_enabled,
        "shuffle": state.connected and len(state.upcoming) >= 2,
        "join": state.status == "disconnected",
        # Groove-style controls: history navigation, loop, autoplay, effects.
        "previous": state.connected and state.has_previous,
        "loop": state.connected,
        "autoplay": state.connected,
        "effects": state.connected and state.effects_available,
    }


def _play_confirmation(result: PlayResult) -> str:
    body = f"**{_safe(result.track.title, 90)}**\n{_safe(result.track.author, 80)}"
    return f"Now playing\n\n{body}" if result.started else f"Added to queue\n\n{body}\n\nPosition #{result.position}"


class PlayModal(discord.ui.Modal, title="Add music"):
    query = discord.ui.TextInput(label="Song or URL", placeholder="Search for a song…", max_length=200)

    def __init__(self, service, guild_id: int):
        super().__init__(timeout=300)
        self.service, self.guild_id = service, guild_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        query = (self.query.value or "").strip()
        guild, member = interaction.guild, interaction.user
        if not query:
            await send_ephemeral(interaction, "tell me what you want to play.")
            return
        if guild is None or not isinstance(member, discord.Member):
            await send_ephemeral(interaction, "music only works in a server.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await self.service.play(guild, member, query)
            await interaction.followup.send(_play_confirmation(result), ephemeral=True)
            fallback = guild.get_channel(interaction.channel_id) if interaction.channel_id else None
            await self.service.refresh_panel(self.guild_id, fallback=fallback)
        except MusicError as error:
            await interaction.followup.send(str(error), ephemeral=True)
        except Exception:
            log.exception("Play modal failed")
            await interaction.followup.send("that didn't work on my end. it's been logged.", ephemeral=True)


class VolumeModal(discord.ui.Modal, title="Set volume"):
    def __init__(self, service, guild_id: int, max_volume: int):
        super().__init__(timeout=300)
        self.service, self.guild_id, self.max_volume = service, guild_id, max_volume
        self.value = discord.ui.TextInput(
            label=f"Volume · 0–{max_volume}%", placeholder="75", max_length=4
        )
        self.add_item(self.value)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            volume = int((self.value.value or "").strip().rstrip("%"))
        except ValueError:
            await send_ephemeral(interaction, f"enter a number from 0 to {self.max_volume}.")
            return
        guild, member = interaction.guild, interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await send_ephemeral(interaction, "music only works in a server.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            value = await self.service.set_volume(guild, member, volume)
            await interaction.followup.send(f"Volume set to **{value}%**.", ephemeral=True)
            await self.service.refresh_panel(self.guild_id)
        except MusicError as error:
            await interaction.followup.send(str(error), ephemeral=True)
        except Exception:
            log.exception("Volume modal failed")
            await interaction.followup.send("that didn't work on my end. it's been logged.", ephemeral=True)


class MusicPanelView(discord.ui.LayoutView):
    """Persistent Components V2 player. One instance is attached per refresh."""

    def __init__(self, service, guild_id: int, enabled: dict[str, bool] | None = None, state: PanelState | None = None):
        super().__init__(timeout=None)
        self.service, self.guild_id = service, guild_id
        self.state = state or service.panel_state(guild_id)
        self.enabled = enabled or button_states(self.state)
        self._build()

    def _button(self, *, label: str, custom_id: str, callback, emoji=None,
                style=discord.ButtonStyle.secondary, disabled=False) -> discord.ui.Button:
        button = discord.ui.Button(label=label, custom_id=custom_id, emoji=emoji, style=style, disabled=disabled)
        button.callback = callback
        return button

    def _build(self) -> None:
        state, track = self.state, self.state.active_track
        container = discord.ui.Container(accent_colour=_STATUS_COLOUR.get(state.status, Colour.IDLE))
        heading = "### PARADISE MUSIC"
        if track:
            title = f"{heading}\n-# NOW PLAYING\n**{_safe(track.title, 82)}**\n{_safe(track.author, 64)}"
            if track.artwork_url:
                container.add_item(discord.ui.Section(
                    discord.ui.TextDisplay(title),
                    accessory=discord.ui.Thumbnail(track.artwork_url, description="Track artwork"),
                ))
            else:
                container.add_item(discord.ui.TextDisplay(title))
            requester = f"<@{track.requester_id}>" if track.requester_id else "Autoplay"
            meta = f"-# {requester} · {_source_label(track.source)}\n{_progress_line(state, track)}"
            container.add_item(discord.ui.TextDisplay(meta))
        elif state.status == "disconnected":
            container.add_item(discord.ui.TextDisplay(
                f"{heading}\n**Not connected to a voice channel.**\n-# Join voice, then add a song to begin."
            ))
        else:
            container.add_item(discord.ui.TextDisplay(
                f"{heading}\n**Nothing is playing.**\n-# Add a track to start the session."
            ))

        if state.status == "speaking":
            spoken = _safe(state.spoken_text or "…", 130)
            resuming = f"\n-# Resuming: **{_safe(track.title, 55)}**" if track else ""
            container.add_item(discord.ui.TextDisplay(f"**Paradise is speaking**\n*“{spoken}”*{resuming}"))
        elif state.status == "paused":
            container.add_item(discord.ui.TextDisplay("**Paused** · your place is saved"))

        if state.connected:
            container.add_item(discord.ui.TextDisplay(_state_chips(state)))
        if state.last_action and state.status != "disconnected":
            container.add_item(discord.ui.TextDisplay(f"-# {shorten(state.last_action, 100)}"))

        container.add_item(discord.ui.Separator())

        # Row 1 — transport: Previous | Play/Pause | Next
        primary = discord.ui.ActionRow()
        primary.add_item(self._button(
            label="Previous", emoji="◀", custom_id="paradise:music:previous",
            callback=self._previous, disabled=not self.enabled.get("previous", False),
        ))
        if state.status == "playing":
            main = self._button(label="Pause", emoji="⏸", style=discord.ButtonStyle.primary,
                                custom_id="paradise:music:primary", callback=self._pause)
        elif state.status == "paused":
            main = self._button(label="Resume", emoji="▶", style=discord.ButtonStyle.success,
                                custom_id="paradise:music:primary", callback=self._resume)
        elif state.status == "disconnected":
            main = self._button(label="Join", emoji="🎧", style=discord.ButtonStyle.primary,
                                custom_id="paradise:music:primary", callback=self._join)
        elif state.status == "speaking":
            main = self._button(label="Speaking", emoji="🔊", custom_id="paradise:music:primary",
                                callback=self._unavailable, disabled=True)
        elif state.status == "loading":
            main = self._button(label="Loading", emoji="…", custom_id="paradise:music:primary",
                                callback=self._unavailable, disabled=True)
        else:
            main = self._button(label="Play", emoji="▶", style=discord.ButtonStyle.primary,
                                custom_id="paradise:music:primary", callback=self._play)
        primary.add_item(main)
        primary.add_item(self._button(
            label="Next", emoji="▶", custom_id="paradise:music:next",
            callback=self._skip, disabled=not self.enabled.get("skip", False),
        ))
        container.add_item(primary)

        # Row 2 — queue and session controls
        secondary = discord.ui.ActionRow()
        secondary.add_item(self._button(
            label="Shuffle", emoji="🔀", custom_id="paradise:music:shuffle",
            callback=self._shuffle, disabled=not self.enabled.get("shuffle", False),
        ))
        secondary.add_item(self._button(
            label="Queue", emoji="☰", custom_id="paradise:music:queue",
            callback=self._queue, disabled=not self.enabled.get("queue", False),
        ))
        secondary.add_item(self._button(
            label="Volume", emoji="🔊", custom_id="paradise:music:volume",
            callback=self._volume, disabled=not self.enabled.get("volume", False),
        ))
        secondary.add_item(self._button(
            label="Lyrics", emoji="♪", custom_id="paradise:music:lyrics",
            callback=self._lyrics, disabled=not self.enabled.get("lyrics", False),
        ))
        secondary.add_item(self._button(
            label="Stop", emoji="■", style=discord.ButtonStyle.danger,
            custom_id="paradise:music:stop", callback=self._stop,
            disabled=not self.enabled.get("stop", False),
        ))
        container.add_item(secondary)

        # Row 3 — playback behaviour: Loop | Autoplay | Effects | Add
        tertiary = discord.ui.ActionRow()
        loop_active = state.loop_mode != "off"
        tertiary.add_item(self._button(
            label=_LOOP_LABEL.get(state.loop_mode, "Loop: Off"), emoji="🔁",
            custom_id="paradise:music:loop", callback=self._loop,
            style=discord.ButtonStyle.success if loop_active else discord.ButtonStyle.secondary,
            disabled=not self.enabled.get("loop", False),
        ))
        tertiary.add_item(self._button(
            label="Autoplay", emoji="♾️", custom_id="paradise:music:autoplay",
            callback=self._autoplay,
            style=discord.ButtonStyle.success if state.autoplay else discord.ButtonStyle.secondary,
            disabled=not self.enabled.get("autoplay", False),
        ))
        effects_label = f"Effects{' · ' + str(len(state.filters)) if state.filters else ''}"
        tertiary.add_item(self._button(
            label=effects_label, emoji="🎚", custom_id="paradise:music:effects",
            callback=self._effects, disabled=not self.enabled.get("effects", False),
        ))
        tertiary.add_item(self._button(
            label="Add Music", emoji="＋", custom_id="paradise:music:add", callback=self._play,
        ))
        container.add_item(tertiary)

        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(f"**UP NEXT**\n{_queue_preview(state)}"))
        if state.lyrics_loaded and state.lyrics_preview:
            preview = shorten(state.lyrics_preview.replace("\n", " · "), 150)
            container.add_item(discord.ui.TextDisplay(f"-# Lyrics ready · {preview}"))
        self.add_item(container)

    # -- shared plumbing ---------------------------------------------------

    async def _member(self, interaction: discord.Interaction):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await send_ephemeral(interaction, "music only works in a server.")
            return None, None
        return interaction.guild, interaction.user

    async def _run(self, interaction: discord.Interaction, action) -> None:
        guild, member = await self._member(interaction)
        if guild is None:
            return
        try:
            await interaction.response.defer()
            message = await action(guild, member)
            if message:
                await send_ephemeral(interaction, message)
            await self.service.refresh_panel(guild.id)
        except MusicError as error:
            await send_ephemeral(interaction, str(error))
        except Exception:
            log.exception("Panel action failed")
            await send_ephemeral(interaction, "that broke on my end. it's been logged.")

    async def _unavailable(self, interaction: discord.Interaction) -> None:
        await send_ephemeral(interaction, "that control isn't available right now.")

    # -- callbacks (every one delegates to MusicService) --------------------

    async def _play(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(PlayModal(self.service, self.guild_id))

    async def _pause(self, interaction: discord.Interaction) -> None:
        async def action(g, m):
            await self.service.pause(g, m); return "Paused."
        await self._run(interaction, action)

    async def _resume(self, interaction: discord.Interaction) -> None:
        async def action(g, m):
            await self.service.resume(g, m); return "Playing again."
        await self._run(interaction, action)

    async def _skip(self, interaction: discord.Interaction) -> None:
        async def action(g, m):
            result = await self.service.skip(g, m)
            return "Skipped." if result.up_next is None else f"Next: **{_safe(result.up_next.title, 60)}**"
        await self._run(interaction, action)

    async def _previous(self, interaction: discord.Interaction) -> None:
        async def action(g, m):
            result = await self.service.previous(g, m)
            return f"Back to **{_safe(result.track.title, 60)}**."
        await self._run(interaction, action)

    async def _stop(self, interaction: discord.Interaction) -> None:
        async def action(g, m):
            await self.service.stop(g, m); return "Stopped and disconnected."
        await self._run(interaction, action)

    async def _join(self, interaction: discord.Interaction) -> None:
        async def action(g, m):
            result = await self.service.join(g, m); return f"Connected to **{_safe(result.channel_name, 60)}**."
        await self._run(interaction, action)

    async def _shuffle(self, interaction: discord.Interaction) -> None:
        async def action(g, m):
            count = self.service.shuffle_queue(g, m); return f"Shuffled {count} tracks."
        await self._run(interaction, action)

    async def _loop(self, interaction: discord.Interaction) -> None:
        async def action(g, m):
            mode = await self.service.set_loop(g, m, None)  # cycle Off → Track → Queue → Off
            labels = {"off": "Loop off.", "track": "Looping this track.", "queue": "Looping the whole queue."}
            return labels.get(mode, f"Loop: {mode}.")
        await self._run(interaction, action)

    async def _autoplay(self, interaction: discord.Interaction) -> None:
        async def action(g, m):
            enabled = self.service.set_autoplay(g, m, None)
            return "Autoplay on — I'll keep the music going." if enabled else "Autoplay off."
        await self._run(interaction, action)

    async def _effects(self, interaction: discord.Interaction) -> None:
        guild, member = await self._member(interaction)
        if guild is None:
            return
        try:
            self.service.require_connected_control(guild, member)
            await interaction.response.send_message(
                view=FilterPicker(self.service, guild.id), ephemeral=True
            )
        except MusicError as error:
            await send_ephemeral(interaction, str(error))
        except discord.DiscordException:
            log.debug("Effects picker response failed", exc_info=True)

    async def _volume(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(VolumeModal(self.service, self.guild_id, self.service.max_volume))

    async def _queue(self, interaction: discord.Interaction) -> None:
        guild, member = await self._member(interaction)
        if guild is None:
            return
        try:
            self.service.require_connected_control(guild, member)
            await interaction.response.send_message(view=QueuePaginator(self.service, guild.id), ephemeral=True)
        except MusicError as error:
            await send_ephemeral(interaction, str(error))
        except discord.DiscordException:
            log.debug("Queue response failed", exc_info=True)

    async def _lyrics(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=False, thinking=True)
        try:
            track, result = await self.service.lyrics_for_guild(self.guild_id)
            if result is None or not result.text:
                await interaction.followup.send(
                    f"Lyrics aren't available for **{_safe(track.title, 70)}**.", ephemeral=True
                )
                return
            pages = lyrics.paginate(result.text)
            await interaction.followup.send(
                view=lyrics.LyricsPaginator(pages).bind(track.title, track.author, result.source),
            )
            await self.service.refresh_panel(self.guild_id)
        except MusicError as error:
            await interaction.followup.send(str(error), ephemeral=True)
        except Exception:
            log.exception("Panel lyrics failed")
            await interaction.followup.send("lyrics are unavailable right now.", ephemeral=True)


class FilterPicker(discord.ui.LayoutView):
    """Ephemeral Components V2 toggle board for the node's audio effects."""

    def __init__(self, service, guild_id: int):
        super().__init__(timeout=180)
        self.service, self.guild_id = service, guild_id
        self._build()

    def _build(self) -> None:
        self.clear_items()
        presets = self.service.filter_presets()
        active = {preset.name for preset in self.service.active_filter_presets(self.guild_id)}
        supported_names = {
            preset.name for preset in presets if self.service.filter_supported(preset)
        }
        container = discord.ui.Container(accent_colour=Colour.ACCENT)
        if not supported_names:
            container.add_item(discord.ui.TextDisplay(
                "### EFFECTS\n-# The music node doesn't report any usable filters — "
                "effects are unavailable here."
            ))
            self.add_item(container)
            return
        header = "### EFFECTS\n-# Tap to toggle. Applies instantly; volume stays untouched."
        container.add_item(discord.ui.TextDisplay(header))
        container.add_item(discord.ui.Separator())
        row = discord.ui.ActionRow()
        for index, preset in enumerate(presets):
            if index and index % 5 == 0:
                container.add_item(row)
                row = discord.ui.ActionRow()
            button = discord.ui.Button(
                label=preset.label,
                custom_id=f"paradise:fx:{preset.name}",
                style=discord.ButtonStyle.success if preset.name in active else discord.ButtonStyle.secondary,
                disabled=preset.name not in supported_names,
            )
            button.callback = self._make_toggle(preset.name)
            row.add_item(button)
        row.add_item(self._clear_button(active))
        container.add_item(row)
        if active:
            names = " · ".join(sorted(
                preset.label for preset in presets if preset.name in active
            ))
            container.add_item(discord.ui.TextDisplay(f"-# Active: {shorten(names, 120)}"))
        else:
            container.add_item(discord.ui.TextDisplay("-# No effects active"))
        self.add_item(container)

    def _clear_button(self, active: set[str]) -> discord.ui.Button:
        button = discord.ui.Button(
            label="Clear All", emoji="✕", style=discord.ButtonStyle.danger,
            custom_id="paradise:fx:clear", disabled=not active,
        )
        button.callback = self._clear
        return button

    def _make_toggle(self, name: str):
        async def toggle(interaction: discord.Interaction) -> None:
            await self._toggle(interaction, name)
        return toggle

    async def _toggle(self, interaction: discord.Interaction, name: str) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await send_ephemeral(interaction, "music only works in a server.")
            return
        try:
            outcome = await self.service.set_filter(
                interaction.guild, interaction.user, name
            )
            self._build()
            await interaction.response.edit_message(view=self)
            await self.service.refresh_panel(self.guild_id)
            if outcome == "removed":
                await send_ephemeral(interaction, "Effect off.")
            else:
                await send_ephemeral(interaction, "Effect on.")
        except MusicError as error:
            await send_ephemeral(interaction, str(error))
        except discord.DiscordException:
            log.debug("Filter toggle response failed", exc_info=True)
        except Exception:
            log.exception("Filter toggle failed")
            await send_ephemeral(interaction, "that broke on my end. it's been logged.")

    async def _clear(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await send_ephemeral(interaction, "music only works in a server.")
            return
        try:
            await self.service.clear_filters(interaction.guild, interaction.user)
            self._build()
            await interaction.response.edit_message(view=self)
            await self.service.refresh_panel(self.guild_id)
        except MusicError as error:
            await send_ephemeral(interaction, str(error))
        except Exception:
            log.exception("Filter clear failed")
            await send_ephemeral(interaction, "that broke on my end. it's been logged.")


class RemoveTrackModal(discord.ui.Modal, title="Remove from queue"):
    position = discord.ui.TextInput(
        label="Queue position", placeholder="e.g. 3", max_length=4, required=True
    )

    def __init__(self, service, guild_id: int):
        super().__init__(timeout=300)
        self.service, self.guild_id = service, guild_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild, member = interaction.guild, interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await send_ephemeral(interaction, "music only works in a server.")
            return
        raw = (self.position.value or "").strip().lstrip("#")
        try:
            position = int(raw)
        except ValueError:
            await send_ephemeral(interaction, "that's not a position — enter a number like 3.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            removed = self.service.remove_track(guild, member, position)
        except MusicError as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        except Exception:
            log.exception("Remove modal failed")
            await interaction.followup.send("that broke on my end. it's been logged.", ephemeral=True)
            return
        await interaction.followup.send(
            f"Removed **{_safe(removed.title, 70)}** from position #{position}.", ephemeral=True
        )
        try:
            await interaction.followup.send(view=QueuePaginator(self.service, guild.id), ephemeral=True)
            await self.service.refresh_panel(guild.id)
        except discord.DiscordException:
            log.debug("Queue refresh after remove failed", exc_info=True)


class QueuePaginator(discord.ui.LayoutView):
    """Components V2 queue browser; pagination edits the same response."""

    def __init__(self, service, guild_id: int):
        super().__init__(timeout=300)
        self.service, self.guild_id, self.page = service, guild_id, 0
        self.previous_button = None
        self.next_button = None
        self._build()

    def _build(self) -> None:
        self.clear_items()
        state = self.service.panel_state(self.guild_id)
        text, self.page, pages = _queue_page_text(state, self.page)
        container = discord.ui.Container(accent_colour=Colour.ACCENT)
        track = state.active_track
        current = (
            f"**{_safe(track.title, 68)}**\n-# {_safe(track.author, 44)} · {_STATUS_LABEL.get(state.status, 'Idle')}"
            if track else "*Nothing playing right now.*"
        )
        if track is not None and track.artwork_url:
            container.add_item(discord.ui.Section(
                discord.ui.TextDisplay(f"### QUEUE\n-# CURRENT TRACK\n{current}"),
                accessory=discord.ui.Thumbnail(track.artwork_url, description="Track artwork"),
            ))
        else:
            container.add_item(discord.ui.TextDisplay(f"### QUEUE\n-# CURRENT TRACK\n{current}"))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(f"**UP NEXT**\n{text}"))
        container.add_item(discord.ui.Separator())

        nav = discord.ui.ActionRow()
        self.previous_button = discord.ui.Button(label="Previous", emoji="◀", custom_id="paradise:queue:previous", disabled=self.page <= 0)
        self.next_button = discord.ui.Button(label="Next", emoji="▶", custom_id="paradise:queue:next", disabled=self.page >= pages - 1)
        self.previous_button.callback = self._previous
        self.next_button.callback = self._next
        nav.add_item(self.previous_button)
        nav.add_item(discord.ui.Button(label=f"Page {self.page + 1} / {pages}", custom_id="paradise:queue:page", disabled=True))
        nav.add_item(self.next_button)
        container.add_item(nav)

        actions = discord.ui.ActionRow()
        remove = discord.ui.Button(label="Remove", emoji="✕", custom_id="paradise:queue:remove", disabled=not state.upcoming)
        remove.callback = self._remove
        shuffle = discord.ui.Button(label="Shuffle", emoji="🔀", custom_id="paradise:queue:shuffle", disabled=len(state.upcoming) < 2)
        shuffle.callback = self._shuffle
        clear = discord.ui.Button(label="Clear", emoji="🗑", style=discord.ButtonStyle.danger, custom_id="paradise:queue:clear", disabled=not state.upcoming)
        clear.callback = self._clear
        actions.add_item(remove)
        actions.add_item(shuffle)
        actions.add_item(clear)
        container.add_item(actions)

        total = f"{len(state.upcoming)} track{'s' if len(state.upcoming) != 1 else ''}"
        if state.queue_duration_ms:
            total += f" · {format_duration(state.queue_duration_ms)} total"
        elif any(t.is_stream for t in state.upcoming):
            total += " · includes live audio"
        container.add_item(discord.ui.TextDisplay(f"-# {total}"))
        self.add_item(container)

    async def _show(self, interaction: discord.Interaction) -> None:
        self._build()
        try:
            await interaction.response.edit_message(view=self)
        except discord.DiscordException:
            log.debug("Queue pagination edit failed", exc_info=True)

    async def _previous(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        await self._show(interaction)

    async def _next(self, interaction: discord.Interaction) -> None:
        self.page += 1
        await self._show(interaction)

    async def _remove(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(RemoveTrackModal(self.service, self.guild_id))

    async def _shuffle(self, interaction: discord.Interaction) -> None:
        guild, member = interaction.guild, interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await send_ephemeral(interaction, "music only works in a server.")
            return
        try:
            count = self.service.shuffle_queue(guild, member)
            await self._show(interaction)
            await self.service.refresh_panel(guild.id)
            await send_ephemeral(interaction, f"Shuffled {count} tracks.")
        except MusicError as error:
            await send_ephemeral(interaction, str(error))
        except Exception:
            log.exception("Queue shuffle failed")
            await send_ephemeral(interaction, "that broke on my end. it's been logged.")

    async def _clear(self, interaction: discord.Interaction) -> None:
        guild, member = interaction.guild, interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await send_ephemeral(interaction, "music only works in a server.")
            return
        try:
            removed = self.service.clear_queue(guild, member)
            await self._show(interaction)
            await self.service.refresh_panel(guild.id)
            plural = "track" if removed == 1 else "tracks"
            await send_ephemeral(interaction, f"Cleared {removed} upcoming {plural}.")
        except MusicError as error:
            await send_ephemeral(interaction, str(error))
        except Exception:
            log.exception("Queue clear failed")
            await send_ephemeral(interaction, "that broke on my end. it's been logged.")
