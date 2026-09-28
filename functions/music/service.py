"""MusicService: the single source of truth for Paradise's music state.

Slash commands, panel buttons, TTS playback and auto-disconnect all funnel
through the methods on this class. Nothing else touches Lavalink directly.

Beyond plain playback this service owns the Groove-inspired behaviour layers:
per-guild playback history (real Previous support), loop modes (off / track /
queue via Lavalink.py's native player loop), autoplay with duplicate
protection, queue reordering, seeking, and audio-effect presets gated on what
the node actually reports it supports.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

import discord
import lavalink
from lavalink.events import (
    NodeReadyEvent,
    QueueEndEvent,
    TrackExceptionEvent,
    TrackStartEvent,
    TrackStuckEvent,
)
from lavalink.filters import (
    Equalizer,
    Karaoke,
    Rotation,
    Timescale,
    Tremolo,
    Vibrato,
)
from lavalink.server import LoadType

from functions import lyrics
from functions.helpers import (
    MusicError,
    NodePlaybackUnsupported,
    can_bypass_voice_check,
    format_duration,
    normalize_track_key,
    same_voice_channel,
    shorten,
    strip_title_noise,
    voice_channel_name,
)
from functions.music.panel import (
    JoinResult,
    MusicPanelView,
    PanelState,
    PlayResult,
    PreviousResult,
    SkipResult,
    TrackInfo,
    button_states,
)
from functions.music.voice import LavalinkVoiceClient

if TYPE_CHECKING:
    from config import Config
    from functions.settings import SettingsStore

log = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_PLAYLIST_CAP = 50
_MAINTENANCE_INTERVAL = 15
_LAST_ACTION_TTL = 45 * 60
_DEFAULT_HISTORY_LIMIT = 50

# Autoplay: small, deliberate batches — never a wall of tracks.
_AUTOPLAY_BATCH = 3
_AUTOPLAY_SCAN = 25  # how many playlist entries to consider when sampling
_AUTOPLAY_MEMORY = 80  # remembered auto-added keys per guild

_LOOP_VALUES = {"off": 0, "track": 1, "queue": 2}
_LOOP_LABELS = {"off": "Loop Off", "track": "Loop: Track", "queue": "Loop: Queue"}
_LOOP_NEXT = {"off": "track", "track": "queue", "queue": "off"}

# Search prefixes Paradise may use for plain (non-URL) queries, in preference
# order, mapped to the Lavalink source manager that must be loaded for the
# prefix to resolve. The node reports its loaded sources via /v4/info, so the
# fallback only ever tries sources the node actually has — no blind retries,
# no invented prefixes (see lavalink/README.md for the plugin setup).
_SEARCH_PREFIX_SOURCES: tuple[tuple[str, str], ...] = (
    ("ytsearch", "youtube"),  # provided by dev.lavalink.youtube:youtube-plugin
    ("ytmsearch", "youtube"),  # YouTube Music catalog, same source manager
    ("scsearch", "soundcloud"),  # Lavalink's built-in SoundCloud source
)
_FALLBACK_SEARCH_PREFIXES = ("ytsearch", "scsearch")
_MAX_PLUGIN_LABEL = 5

_BASS_BANDS = (0.35, 0.30, 0.22, 0.14, 0.08, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


@dataclass(frozen=True, slots=True)
class FilterPreset:
    """One user-facing audio effect, mapped onto what the node supports."""

    name: str
    label: str
    node_filter: str  # the Lavalink filter name / player.filters key
    description: str
    factory: Callable[[], lavalink.abc.Filter]


# Only effects the pinned Lavalink v4 config actually enables. Each maps to a
# real filter class; nothing here is faked. Support is re-checked against the
# node's own /v4/info report before anything is applied.
_FILTER_PRESETS: tuple[FilterPreset, ...] = (
    FilterPreset(
        "bassboost", "Bass Boost", "equalizer",
        "punchier low end",
        lambda: Equalizer(gains=_BASS_BANDS),
    ),
    FilterPreset(
        "nightcore", "Nightcore", "timescale",
        "sped up and pitched up",
        lambda: Timescale(speed=1.25, pitch=1.25),
    ),
    FilterPreset(
        "daycore", "Daycore", "timescale",
        "slowed and reverb-ish",
        lambda: Timescale(speed=0.85, pitch=0.85),
    ),
    FilterPreset(
        "karaoke", "Karaoke", "karaoke",
        "dials down the vocals",
        lambda: Karaoke(level=1.0, mono_level=1.0, filter_band=220.0, filter_width=100.0),
    ),
    FilterPreset(
        "tremolo", "Tremolo", "tremolo",
        "volume wobble",
        lambda: Tremolo(frequency=2.0, depth=0.3),
    ),
    FilterPreset(
        "vibrato", "Vibrato", "vibrato",
        "pitch wobble",
        lambda: Vibrato(frequency=2.0, depth=0.3),
    ),
    FilterPreset(
        "rotation", "Rotation", "rotation",
        "8D-style spinning",
        lambda: Rotation(rotation_hz=0.2),
    ),
)
_FILTER_BY_NAME = {preset.name: preset for preset in _FILTER_PRESETS}


@dataclass(slots=True)
class HistoryEntry:
    """One previously played track, kept as the original Lavalink object.

    ``resume_ms`` remembers where the track was when the listener navigated
    away from it manually, so Previous can land back at roughly that spot
    instead of restarting or re-searching.
    """

    track: object
    resume_ms: int | None = None


def _track_identity(track) -> tuple:
    """Stable identity for a track: the encoded track string when present,
    otherwise identifier + title (covers decoded and fake tracks alike)."""
    encoded = getattr(track, "track", None)
    if encoded:
        return ("t", encoded)
    return (
        "i",
        str(getattr(track, "identifier", "") or ""),
        str(getattr(track, "title", "") or ""),
    )


def _same_track(a, b) -> bool:
    return _track_identity(a) == _track_identity(b)


class GuildSession:
    """Per-guild music state that lives outside the Lavalink player."""

    __slots__ = (
        "volume",
        "sticky",
        "alone_since",
        "last_action",
        "last_action_at",
        "tts_active",
        "tts_text",
        "tts_stash",
        "tts_loop_value",
        "transition_track",
        "transition_position",
        "playback_history",
        "history_index",
        "history_intent",
        "loop_mode",
        "autoplay_enabled",
        "autoplay_history",
        "autoplay_retry_at",
        "autoplay_failures",
        "active_presets",
    )
    def __init__(self) -> None:
        self.volume: int = 100
        self.sticky: bool = False  # True when joined via /join: stay when alone
        self.alone_since: float | None = None
        self.last_action: str | None = None
        self.last_action_at: float = 0.0
        # TTS interruption state
        self.tts_active: bool = False
        self.tts_text: str | None = None
        self.tts_stash: tuple | None = None  # (track, position_ms, queue, was_paused)
        self.tts_loop_value: int = 0  # loop mode parked while TTS owns the player
        # Short-lived display context while Lavalink transitions between tracks.
        # QueueEnd clears it, so a genuinely finished session still becomes idle.
        self.transition_track: TrackInfo | None = None
        self.transition_position: int = 0
        # Playback history: a bounded list of everything that has started
        # playing, with a cursor. history_index points at the track that is
        # (or was most recently) playing; anything past it was left behind by
        # Previous navigation and gets truncated when normal playback resumes.
        self.playback_history: list[HistoryEntry] = []
        self.history_index: int = -1
        self.history_intent: str | None = None  # "previous" while navigating back
        # Loop mode mirrors the player's native loop setting for the panel.
        self.loop_mode: str = "off"  # off | track | queue
        # Autoplay: per-guild switch + a short memory of what was auto-added,
        # so related-track search never loops the same picks over and over.
        self.autoplay_enabled: bool = False
        self.autoplay_history: deque[str] = deque(maxlen=_AUTOPLAY_MEMORY)
        # Backoff for autoplay retries: never hammer the node when related
        # searches keep coming back empty.
        self.autoplay_retry_at: float = 0.0
        self.autoplay_failures: int = 0
        # Names of active audio-effect presets (the player owns the real state;
        # this survives reconnects and feeds the panel).
        self.active_presets: set[str] = set()

    def note(self, action: str) -> None:
        self.last_action = action
        self.last_action_at = time.monotonic()

    def recent_action(self) -> str | None:
        if self.last_action and time.monotonic() - self.last_action_at < _LAST_ACTION_TTL:
            return self.last_action
        return None

    def reset(self, *, keep_volume: bool = True) -> None:
        self.sticky = False
        self.alone_since = None
        self.last_action = None
        self.last_action_at = 0.0
        self.tts_active = False
        self.tts_text = None
        self.tts_stash = None
        self.tts_loop_value = 0
        self.transition_track = None
        self.transition_position = 0
        self.playback_history = []
        self.history_index = -1
        self.history_intent = None
        self.loop_mode = "off"
        self.autoplay_enabled = False
        self.autoplay_history.clear()
        self.autoplay_retry_at = 0.0
        self.autoplay_failures = 0
        self.active_presets.clear()
        if not keep_volume:
            self.volume = 100


class MusicService:
    def __init__(self, bot: discord.Client, config: "Config", settings: "SettingsStore"):
        self.bot = bot
        self.config = config
        self.settings = settings
        self.tts = None  # wired by the bot after construction
        self.client: lavalink.Client | None = None
        self.sessions: dict[int, GuildSession] = {}
        self._panel_messages: dict[int, discord.Message] = {}
        self._panel_locks: dict[int, asyncio.Lock] = {}
        self._autoplay_locks: dict[int, asyncio.Lock] = {}
        self._maintenance_task: asyncio.Task | None = None
        self._closing = False
        # What the node reported about itself (refreshed on every node ready).
        self.node_plugins: dict[str, str] = {}
        self.node_sources: frozenset[str] | None = None
        self.node_filters: frozenset[str] | None = None
        self.node_version: str | None = None
        # Deliberately separate transport readiness from /v4/info diagnostics.
        # An info request can fail transiently while the websocket is still up.
        self._node_info_ok = False
        self._node_state = "DISCONNECTED"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def session(self, guild_id: int) -> GuildSession:
        session = self.sessions.get(guild_id)
        if session is None:
            session = GuildSession()
            self.sessions[guild_id] = session
        return session

    async def start(self) -> None:
        # Wire optional provider credentials for the lyrics chain (Genius is
        # skipped entirely when the key is unset).
        lyrics.GENIUS_API_KEY = self.config.genius_api_key or None
        if not self.config.music_configured:
            self._node_state = "DISCONNECTED"
            log.info("Lavalink not configured — music and TTS stay offline.")
            return
        if self.bot.user is None:
            raise RuntimeError("MusicService.start() called before login.")

        self._node_state = "CONFIGURED"
        self.client = lavalink.Client(self.bot.user.id)
        self.bot.lavalink = self.client  # the voice bridge looks for this
        protocol = "wss" if self.config.lavalink_secure else "ws"
        log.info(
            "Connecting to Lavalink %s://%s:%s (region=%s; password hidden)",
            protocol, self.config.lavalink_host, self.config.lavalink_port,
            self.config.lavalink_region,
        )
        try:
            self._node_state = "CONNECTING"
            self.client.add_node(
                host=self.config.lavalink_host,
                port=self.config.lavalink_port,
                password=self.config.lavalink_password,
                region=self.config.lavalink_region,
                name="paradise",
                ssl=self.config.lavalink_secure,
            )
        except Exception:
            # add_node normally only schedules the websocket connection; errors
            # here are configuration/constructor failures, not user errors.
            self._node_state = "DISCONNECTED"
            log.exception("Could not register Lavalink node (host/port/configuration failure)")
            self.client = None
            return
        self.client.add_event_hooks(self)
        self._maintenance_task = asyncio.create_task(
            self._maintenance_loop(), name="paradise-music-maintenance"
        )
        asyncio.create_task(self._restore_panels(), name="paradise-panel-restore")
        log.info(
            "Lavalink node registered: %s:%s", self.config.lavalink_host, self.config.lavalink_port
        )

    async def shutdown(self) -> None:
        self._closing = True
        if self._maintenance_task is not None:
            self._maintenance_task.cancel()
            try:
                await self._maintenance_task
            except (asyncio.CancelledError, Exception):  # noqa: B014
                pass
            self._maintenance_task = None
        if self.client is not None:
            for guild in list(getattr(self.bot, "guilds", [])):
                vc = getattr(guild, "voice_client", None)
                if vc is not None and callable(getattr(vc, "disconnect", None)):
                    try:
                        await vc.disconnect(force=True)
                    except Exception:
                        log.debug("Voice disconnect during shutdown failed", exc_info=True)
            try:
                await self.client.close()
            except Exception:
                log.debug("Lavalink client close failed", exc_info=True)
            self.client = None

    # -- lavalink event hooks -------------------------------------------

    @lavalink.listener(TrackStartEvent)
    async def _on_track_start(self, event: TrackStartEvent) -> None:
        self._record_history(event.player, event.track)
        await self.refresh_panel(event.player.guild_id)

    @lavalink.listener(QueueEndEvent)
    async def _on_queue_end(self, event: QueueEndEvent) -> None:
        guild_id = event.player.guild_id
        session = self.session(guild_id)
        if session.tts_active:
            return  # the TTS worker owns the next transition
        if session.autoplay_enabled and not self._closing:
            if await self._maybe_autoplay(event.player, session):
                await self.refresh_panel(guild_id)
                return
        session.last_action = None
        session.last_action_at = 0.0
        session.transition_track = None
        session.transition_position = 0
        await self.refresh_panel(guild_id)

    @lavalink.listener(TrackExceptionEvent)
    async def _on_track_exception(self, event: TrackExceptionEvent) -> None:
        log.warning(
            "Track error in guild %s: %s", event.player.guild_id, getattr(event, "exception", "?")
        )
        # DefaultPlayer does not advance past an exception on its own, so the
        # service owns the recovery — with the loop guard below so a track-loop
        # can never turn one broken track into an exception loop.
        await self._recover_player(
            event.player,
            f"**{shorten(getattr(event.track, 'title', 'the track'), 50)}** errored — moving on",
            force=True,
        )
        await self.refresh_panel(event.player.guild_id)

    @lavalink.listener(TrackStuckEvent)
    async def _on_track_stuck(self, event: TrackStuckEvent) -> None:
        log.warning("Track stuck in guild %s", event.player.guild_id)
        # DefaultPlayer already advances past stuck tracks — unless a track
        # loop is on, in which case it would replay the stuck track forever.
        # Only intervene in that case.
        if (getattr(event.player, "loop", 0) or 0) == 1:
            await self._recover_player(event.player, "last track got stuck — moved on", force=True)
        else:
            self.session(event.player.guild_id).note("last track got stuck — moved on")
        await self.refresh_panel(event.player.guild_id)

    @lavalink.listener(NodeReadyEvent)
    async def _on_node_ready(self, event: NodeReadyEvent) -> None:
        self._node_state = "READY"
        log.info("Lavalink websocket authenticated and node ready (%s)", getattr(event.node, "name", "node"))
        self._maybe_task(self._refresh_node_info(event.node))

    async def _refresh_node_info(self, node) -> None:
        """Ask the node what plugins and sources it actually loaded.

        This is the honest verification step: whatever ``/v4/info`` reports is
        what /play can resolve. Missing expected plugins are logged clearly —
        never faked, never fatal for the rest of the node connection.
        """
        try:
            info = await node.get_info()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._node_info_ok = False
            # Do not turn a healthy websocket into an unavailable node merely
            # because the diagnostic REST endpoint briefly failed.
            self._node_state = "DEGRADED" if self.node_ready else "DISCONNECTED"
            log.warning(
                "Lavalink is ready but /v4/info failed; source diagnostics are unavailable "
                "(music transport remains usable)", exc_info=True
            )
            return

        try:
            plugins = {
                str(entry.get("name")): str(entry.get("version"))
                for entry in (info.get("plugins") or [])
                if entry.get("name")
            }
            sources = frozenset(str(name) for name in (info.get("sourceManagers") or []))
            raw_filters = info.get("filters")
            if isinstance(raw_filters, dict):  # {"equalizer": true, ...}
                filters = frozenset(
                    str(name) for name, enabled in raw_filters.items() if enabled
                )
            elif isinstance(raw_filters, (list, tuple)):  # ["equalizer", ...]
                filters = frozenset(str(name) for name in raw_filters)
            else:
                filters = None  # node did not say — probe at runtime instead
            raw_version = info.get("version")
            if isinstance(raw_version, dict):  # {"semver": "4.2.1", ...}
                version = str(raw_version.get("semver") or "") or None
            elif isinstance(raw_version, str):
                version = raw_version or None
            else:
                version = None
            self.node_plugins = plugins
            self.node_sources = sources or None
            self.node_filters = filters
            self.node_version = version
            self._node_info_ok = True
            self._node_state = "READY" if self.node_ready else "DISCONNECTED"
        except Exception:
            log.warning("Malformed Lavalink node info — using default sources", exc_info=True)
            return

        log.info(
            "Lavalink node version %s (%s sources, %s plugins)",
            self.node_version or "unknown",
            len(self.node_sources or ()),
            len(plugins),
        )
        if self.node_filters is not None:
            log.info("Lavalink filters enabled: %s", ", ".join(sorted(self.node_filters)) or "(none)")
        else:
            log.info("Lavalink did not report filters — effects will be probed at runtime")
        if plugins:
            summary = ", ".join(f"{name}@{version}" for name, version in sorted(plugins.items()))
            log.info("Lavalink plugins loaded: %s", summary)
        else:
            log.info("Lavalink node reports no plugins (built-in sources only)")
        if self.node_sources is not None:
            log.info("Lavalink sources available: %s", ", ".join(sorted(self.node_sources)))

        for name, version in self.config.expected_plugins:
            if name in self.node_plugins:
                actual = self.node_plugins[name]
                if version and actual != version:
                    log.warning(
                        "Lavalink plugin '%s' is version %s, expected %s — update the "
                        "node config (see lavalink/README.md).",
                        name, actual, version,
                    )
                else:
                    log.info("Lavalink expected plugin present: %s%s", name, f"@{actual}" if actual else "")
                continue
            wanted = f"{name}:{version}" if version else name
            log.warning(
                "Lavalink plugin '%s' is configured as expected (LAVALINK_EXPECTED_PLUGINS="
                "%s) but the node did not report it as loaded — sources or features from "
                "it will not resolve. Check the node's lavalink.plugins declarations "
                "(lavalink/README.md).",
                name, wanted,
            )

    def plugins_label(self) -> str | None:
        """Compact, user-safe summary of the plugins the node has loaded."""
        if not self.node_plugins:
            return None
        parts = []
        for name, version in sorted(self.node_plugins.items()):
            version = (version or "").strip()
            parts.append(f"{name}@{version[:12]}…" if len(version) > 12 else f"{name}@{version}")
        if len(parts) > _MAX_PLUGIN_LABEL:
            parts = parts[:_MAX_PLUGIN_LABEL] + [f"+{len(parts) - _MAX_PLUGIN_LABEL} more"]
        return ", ".join(parts)

    def missing_plugins(self) -> list[str]:
        """Expected plugins the node did not report as loaded."""
        missing: list[str] = []
        for name, version in self.config.expected_plugins:
            if name in self.node_plugins:
                continue
            missing.append(f"{name} {version}" if version else name)
        return missing

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    @property
    def node_ready(self) -> bool:
        ready = self.client is not None and bool(self.client.node_manager.available_nodes)
        if self.client is not None and not ready and self._node_state in {"READY", "DEGRADED"}:
            # available_nodes is Lavalink.py's authoritative live connection
            # view; this also makes temporary disconnects recover naturally.
            self._node_state = "DISCONNECTED"
        return ready

    @property
    def node_state(self) -> str:
        """CONFIGURED, CONNECTING, READY, DEGRADED, or DISCONNECTED."""
        self.node_ready
        return self._node_state

    @property
    def configured(self) -> bool:
        return self.client is not None

    @property
    def max_volume(self) -> int:
        return self.config.music_max_volume

    def lavalink_status(self) -> str:
        if not self.configured:
            return "off"
        return "connected" if self.node_ready else "down"

    def player(self, guild_id: int):
        if self.client is None:
            return None
        return self.client.player_manager.get(guild_id)

    def _require_ready(self) -> None:
        if self.client is None:
            raise MusicError("music isn't configured on this bot.")
        if not self.node_ready:
            raise MusicError(
                "music is unavailable right now — the music node isn't responding. "
                "give it a minute."
            )

    def _search_prefixes(self) -> tuple[str, ...]:
        """Search prefixes for plain queries, limited to sources the node has.

        Falls back to the classic defaults when the node hasn't reported its
        sources (or couldn't) — YouTube via the youtube-plugin first, then
        SoundCloud.
        """
        if self.node_sources is None:
            return _FALLBACK_SEARCH_PREFIXES
        return tuple(
            prefix
            for prefix, source in _SEARCH_PREFIX_SOURCES
            if source in self.node_sources
        )

    async def _load_query(self, query: str):
        """Resolve a query through the node's configured sources.

        URLs go straight to the node. Plain queries try each available search
        source once, in order, and stop at the first one that returns tracks.
        Never retries, never loops, and never leaks node errors to the user —
        they are logged with context instead.
        """
        if _URL_RE.match(query):
            results = await self.client.get_tracks(query)
            if results.load_type is LoadType.ERROR:
                _log_load_error(query, results)
            return results, True

        prefixes = self._search_prefixes()
        if not prefixes:
            raise MusicError(
                "the music node has no searchable sources configured — "
                "check its plugins (see lavalink/README.md)."
            )

        reached_node = False
        for prefix in prefixes:
            identifier = f"{prefix}:{query}"
            try:
                results = await self.client.get_tracks(identifier)
            except Exception:
                log.warning("Search via %s failed", prefix, exc_info=True)
                continue
            reached_node = True
            if results.load_type is LoadType.ERROR:
                _log_load_error(identifier, results)
                continue
            if results.tracks:
                return results, True
        # Nothing found on any source. If we never got a single response the
        # node is unreachable; if it responded with empty results the query
        # genuinely has no matches anywhere.
        return None, reached_node

    def _require_connected(self, guild: discord.Guild):
        player = self.player(guild.id)
        vc = getattr(guild, "voice_client", None)
        if player is None or not player.is_connected or vc is None:
            raise MusicError("I'm not connected to voice right now.")
        return player

    def require_connected_control(self, guild: discord.Guild, member: discord.Member) -> None:
        """Shared authorization: connected + same voice channel (or override)."""
        player = self._require_connected(guild)
        if not same_voice_channel(member, player.channel_id) and not can_bypass_voice_check(member):
            name = voice_channel_name(guild, player.channel_id) or "my channel"
            raise MusicError(f"join **{name}** to control me.")

    def panel_state(self, guild_id: int) -> PanelState:
        guild = self.bot.get_guild(guild_id)
        session = self.session(guild_id)
        player = self.player(guild_id)
        connected = (
            guild is not None
            and player is not None
            and player.is_connected
            and getattr(guild, "voice_client", None) is not None
        )

        state = PanelState(
            guild_id=guild_id,
            status="disconnected",
            voice_channel=None,
            volume=session.volume,
            last_action=session.recent_action(),
            lyrics_enabled=self.config.lyrics_enabled,
            loop_mode=session.loop_mode,
            autoplay=session.autoplay_enabled,
            filters=[preset.label for preset in self.active_filter_presets(guild_id)],
            effects_available=self.node_filters is None
            or any(preset.node_filter in self.node_filters for preset in _FILTER_PRESETS),
        )
        if not connected:
            return state
        state.has_previous = session.history_index > 0

        state.voice_channel = voice_channel_name(guild, player.channel_id)
        state.status = "idle"
        if session.tts_active:
            state.status = "speaking"
            state.spoken_text = session.tts_text
            if session.tts_stash is not None:
                raw_track, saved_position, saved_queue, _was_paused = session.tts_stash
                state.resumes_after = TrackInfo.from_track(raw_track)
                state.position_ms = 0 if raw_track.is_stream else saved_position
                # While TTS owns Lavalink's queue, expose the music queue from
                # the stash. This keeps the panel stable through interruption.
                state.upcoming = [TrackInfo.from_track(t) for t in saved_queue]
        elif player.current is not None:
            state.track = TrackInfo.from_track(player.current)
            state.position_ms = player.position if not player.current.is_stream else 0
            state.status = "paused" if player.paused else "playing"
            session.transition_track = state.track
            session.transition_position = state.position_ms
        elif session.transition_track is not None:
            # Lavalink briefly exposes current=None between end/start events.
            # Keep the confirmed track context until TrackStart or QueueEnd
            # settles the transition instead of flashing an empty player.
            state.track = session.transition_track
            state.position_ms = session.transition_position
            state.status = "loading"
        if not session.tts_active:
            state.upcoming = [TrackInfo.from_track(t) for t in player.queue]

        active = state.active_track
        if active is not None and state.lyrics_enabled:
            cached, text = lyrics.CACHE.peek(active.lyrics_key)
            state.lyrics_loaded = cached and text is not None
            if text:
                state.lyrics_preview = next((line.strip() for line in text.splitlines() if line.strip()), None)
        return state

    async def lyrics_for_guild(self, guild_id: int) -> tuple[TrackInfo, lyrics.LyricsResult | None]:
        """Fetch cached lyrics for the currently loaded music track.

        The active key is checked again after the provider returns, preventing a
        slow response for the previous song from being shown after a transition.
        """
        if not self.config.lyrics_enabled:
            raise MusicError("lyrics are disabled on this bot.")
        state = self.panel_state(guild_id)
        track = state.active_track
        if track is None:
            raise MusicError("there isn't a loaded track to find lyrics for.")
        key = track.lyrics_key
        title, artist = lyrics.clean_query(track.title, track.author)
        result = await lyrics.CACHE.get(key, artist, title, track.duration_ms or None)
        current = self.panel_state(guild_id).active_track
        if current is None or current.lyrics_key != key:
            raise MusicError("the track changed while I was finding those lyrics. try again.")
        return track, result

    # ------------------------------------------------------------------
    # Panel management
    # ------------------------------------------------------------------

    def _panel_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._panel_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._panel_locks[guild_id] = lock
        return lock

    async def refresh_panel(self, guild_id: int, *, fallback: discord.abc.Messageable | None = None) -> None:
        """Edit the guild's panel in place, creating one when needed."""
        guild = self.bot.get_guild(guild_id)
        if guild is None or self._closing:
            return
        async with self._panel_lock(guild_id):
            # Build from fresh service state after acquiring the guild lock, so
            # concurrent controls cannot overwrite a newer panel with stale UI.
            state = self.panel_state(guild_id)
            view = MusicPanelView(self, guild_id, button_states(state), state)
            message = self._panel_messages.get(guild_id)
            if message is not None:
                try:
                    # Explicitly clear the old embed when upgrading an existing
                    # panel message to Components V2.
                    await message.edit(embed=None, view=view)
                    return
                except discord.NotFound:
                    self._panel_messages.pop(guild_id, None)
                    await self._forget_panel(guild_id)
                except discord.Forbidden:
                    self._panel_messages.pop(guild_id, None)
                    await self._forget_panel(guild_id)
                    log.debug("Panel became inaccessible in guild %s", guild_id)
                    return
                except discord.HTTPException:
                    log.debug("Panel edit failed", exc_info=True)
                    return

            settings = self.settings.get(guild_id)
            channel = None
            if settings.panel_channel_id:
                channel = guild.get_channel(settings.panel_channel_id)
            if channel is None and fallback is not None:
                raw = getattr(fallback, "id", None)
                channel = guild.get_channel(raw) if raw else None
            if channel is None:
                return
            if not _can_send(channel, guild):
                return
            try:
                message = await channel.send(view=view)
            except discord.DiscordException:
                log.debug("Panel send failed", exc_info=True)
                return
            self._panel_messages[guild_id] = message
            await self.settings.update(
                guild_id,
                panel_channel_id=getattr(channel, "id", None),
                panel_message_id=message.id,
            )

    async def move_panel(self, guild_id: int, channel: discord.abc.GuildChannel) -> bool:
        """Point the guild's panel at a new channel."""
        if not _can_send(channel, self.bot.get_guild(guild_id)):
            return False
        old = self._panel_messages.pop(guild_id, None)
        if old is not None:
            try:
                await old.delete()
            except discord.DiscordException:
                pass
        self.settings.get(guild_id).panel_channel_id = channel.id
        await self.settings.update(guild_id, panel_channel_id=channel.id, panel_message_id=None)
        await self.refresh_panel(guild_id, fallback=channel)
        return True

    async def _forget_panel(self, guild_id: int) -> None:
        try:
            await self.settings.update(guild_id, panel_message_id=None)
        except Exception:
            log.debug("Could not clear stored panel message", exc_info=True)

    async def _restore_panels(self) -> None:
        """After a restart, re-bind and refresh any stored panel messages."""
        try:
            await self.bot.wait_until_ready()
        except Exception:
            return
        for guild_id, settings in list(self.settings.all_items().items()):
            if not settings.panel_channel_id or not settings.panel_message_id:
                continue
            guild = self.bot.get_guild(guild_id)
            channel = guild.get_channel(settings.panel_channel_id) if guild else None
            if guild is None or channel is None:
                continue
            try:
                message = await channel.fetch_message(settings.panel_message_id)
            except discord.DiscordException:
                await self._forget_panel(guild_id)
                continue
            self._panel_messages[guild_id] = message
            session = self.session(guild_id)
            session.note("restarted — queue cleared")
            await self.refresh_panel(guild_id)

    # ------------------------------------------------------------------
    # Voice / playback operations (shared by commands and buttons)
    # ------------------------------------------------------------------

    async def _connect(self, guild: discord.Guild, channel: discord.abc.Connectable) -> None:
        me = guild.me
        perms = channel.permissions_for(me) if me else None
        if perms is None or not (perms.connect and perms.speak):
            raise MusicError("I need permission to connect and speak in that channel.")
        if (
            getattr(channel, "user_limit", 0)
            and perms
            and not perms.move_members
            and len(getattr(channel, "members", [])) >= channel.user_limit
        ):
            raise MusicError("that channel is full.")
        # Clear any zombie voice client before connecting.
        existing = getattr(guild, "voice_client", None)
        if existing is not None:
            try:
                await existing.disconnect(force=True)
            except Exception:
                log.debug("Zombie voice client cleanup failed", exc_info=True)
        try:
            await channel.connect(
                cls=LavalinkVoiceClient, self_deaf=True, timeout=30, reconnect=True
            )
        except asyncio.TimeoutError:
            raise MusicError("couldn't reach Discord voice. try again in a moment.") from None
        except discord.DiscordException:
            log.exception("Voice connect failed")
            raise MusicError("couldn't join that channel. check my permissions.") from None
        player = self.client.player_manager.create(guild_id=guild.id)
        await self._apply_session_player_state(guild.id, player)

    async def _apply_session_player_state(self, guild_id: int, player) -> None:
        """Carry session-owned settings (volume, loop, effects) onto a player.

        Called after every fresh connect so a rejoin doesn't silently drop
        what the guild had configured.
        """
        session = self.session(guild_id)
        try:
            if session.volume != 100:
                await player.set_volume(session.volume)
            if session.loop_mode != "off":
                self._set_player_loop(player, _LOOP_VALUES[session.loop_mode])
            for name in list(session.active_presets):
                preset = _FILTER_BY_NAME.get(name)
                if preset is not None and self.filter_supported(preset):
                    await player.set_filter(preset.factory())
        except Exception:
            # State restore is best-effort; playback must never die for it.
            log.debug("Session player-state restore failed", exc_info=True)

    async def join(self, guild: discord.Guild, member: discord.Member) -> JoinResult:
        self._require_ready()
        voice = member.voice
        if voice is None or voice.channel is None:
            raise MusicError("you're not in a voice channel.")
        destination = voice.channel

        player = self.player(guild.id)
        vc = getattr(guild, "voice_client", None)
        if player is not None and player.is_connected and vc is not None:
            if player.channel_id == destination.id:
                self.session(guild.id).sticky = True
                self.session(guild.id).alone_since = None
                return JoinResult("already", destination.name)
            if player.is_playing and not can_bypass_voice_check(member):
                current = voice_channel_name(guild, player.channel_id) or "another channel"
                raise MusicError(
                    f"I'm playing for people in **{current}**. join them first, "
                    "or ask them to stop the music."
                )
            try:
                await vc.move_to(destination)
            except discord.DiscordException:
                log.exception("Voice move failed")
                raise MusicError("couldn't move to your channel.") from None
            session = self.session(guild.id)
            session.sticky = True
            session.alone_since = None
            self.session(guild.id).note(f"moved to {destination.name}")
            return JoinResult("moved", destination.name)

        await self._connect(guild, destination)
        session = self.session(guild.id)
        session.sticky = True
        session.alone_since = None
        session.note(f"joined {destination.name}")
        return JoinResult("joined", destination.name)

    async def play(
        self, guild: discord.Guild, member: discord.Member, query: str
    ) -> PlayResult:
        self._require_ready()
        voice = member.voice
        if voice is None or voice.channel is None:
            raise MusicError("you're not in a voice channel.")

        query = query.strip().strip("<>").strip()
        if not query:
            raise MusicError("you have to tell me what to play.")
        query = query[:200]

        player = self.player(guild.id)
        if player is None or not player.is_connected or guild.voice_client is None:
            await self._connect(guild, voice.channel)
            player = self.player(guild.id)
            self.session(guild.id).sticky = False  # music session: auto-disconnect applies
        else:
            self.require_connected_control(guild, member)
            player = self.client.player_manager.create(guild_id=guild.id)

        try:
            results, reached_node = await self._load_query(query)
        except MusicError:
            raise
        except Exception:
            log.exception("Track load failed for %r", query)
            raise MusicError("couldn't reach the music node for that one.") from None

        if results is None:
            # Every source was tried; nothing playable came back.
            if not reached_node:
                raise MusicError("couldn't reach the music node for that one.")
            raise MusicError("couldn't find anything for that.")
        if results.load_type is LoadType.ERROR:
            raise MusicError("that didn't load. try a different link or search.")
        if not results.tracks or results.load_type is LoadType.EMPTY:
            raise MusicError("couldn't find anything for that.")

        session = self.session(guild.id)
        # A human adding music re-arms autoplay after it gave up.
        session.autoplay_failures = 0
        session.autoplay_retry_at = 0.0

        if results.load_type is LoadType.PLAYLIST:
            tracks = results.tracks[:_PLAYLIST_CAP]
            for track in tracks:
                player.add(track, requester=member.id)
            started = not player.is_playing
            if started:
                await player.play()
            session.note(f"queued playlist · {len(tracks)} tracks")
            self._maybe_task(self.refresh_panel(guild.id))
            return PlayResult(TrackInfo.from_track(tracks[0]), 0 if started else len(player.queue), started)

        track = results.tracks[0]
        player.add(track, requester=member.id)
        started = not player.is_playing
        if started:
            await player.play()
        position = 0 if started else len(player.queue)
        session.note(f"queued · {shorten(track.title, 60)}")
        self._maybe_task(self.refresh_panel(guild.id))
        return PlayResult(TrackInfo.from_track(track), position, started)

    async def pause(self, guild: discord.Guild, member: discord.Member) -> None:
        player = self._require_connected(guild)
        self.require_connected_control(guild, member)
        if player.current is None:
            raise MusicError("nothing is playing right now.")
        if player.paused:
            raise MusicError("it's already paused.")
        await player.set_pause(True)
        self.session(guild.id).note("paused")

    async def resume(self, guild: discord.Guild, member: discord.Member) -> None:
        player = self._require_connected(guild)
        self.require_connected_control(guild, member)
        if not player.paused:
            raise MusicError("it's not paused.")
        await player.set_pause(False)
        self.session(guild.id).note("resumed")

    async def skip(self, guild: discord.Guild, member: discord.Member) -> SkipResult:
        player = self._require_connected(guild)
        self.require_connected_control(guild, member)
        if player.current is None:
            raise MusicError("nothing is playing right now.")
        session = self.session(guild.id)
        self._note_resume_position(player, session)
        skipped = TrackInfo.from_track(player.current)
        up_next = TrackInfo.from_track(player.queue[0]) if player.queue else None
        await self._advance(player)
        session.note(f"skipped · {shorten(skipped.title, 60)}")
        return SkipResult(skipped, up_next)

    async def previous(self, guild: discord.Guild, member: discord.Member) -> PreviousResult:
        """Jump back to the last played track — the real object, not a re-search.

        History navigation moves a cursor through :attr:`GuildSession.playback_history`;
        it never rewrites the queue. Normal playback from anywhere past the
        cursor truncates the abandoned branch, exactly like a music app.
        """
        player = self._require_connected(guild)
        self.require_connected_control(guild, member)
        session = self.session(guild.id)
        if session.tts_active:
            raise MusicError("hold on — I'm speaking. try previous right after.")
        if session.history_index <= 0 or not session.playback_history:
            raise MusicError("there isn't a previous track.")

        self._note_resume_position(player, session)
        session.history_index -= 1
        entry = session.playback_history[session.history_index]
        session.history_intent = "previous"

        start = entry.resume_ms
        if start is not None:
            track_is_stream = bool(getattr(entry.track, "is_stream", False))
            seekable = getattr(entry.track, "is_seekable", True)
            duration = int(getattr(entry.track, "duration", 0) or 0)
            if track_is_stream or not seekable or duration <= 0:
                start = None
            else:
                start = max(0, min(start, duration - 1000))

        loop_value = getattr(player, "loop", 0) or 0
        if loop_value == 1:
            # DefaultPlayer's track-loop would re-insert the outgoing track at
            # the queue front on an explicit play(); Previous must not mutate
            # the queue, so loop is parked for the duration of the jump.
            self._set_player_loop(player, 0)
        try:
            if start:
                await player.play(entry.track, start_time=start)
            else:
                await player.play(entry.track)
        except (ValueError, TypeError):
            # A node that refuses the start offset still gets the track.
            log.debug("Previous with offset refused; replaying from 0", exc_info=True)
            await player.play(entry.track)
        finally:
            if loop_value == 1:
                self._set_player_loop(player, 1)
        entry.resume_ms = None  # the saved offset has been consumed
        session.note(f"back to · {shorten(str(getattr(entry.track, 'title', 'history')), 60)}")
        return PreviousResult(TrackInfo.from_track(entry.track))

    # -- playback history --------------------------------------------------

    def _record_history(self, player, track) -> None:
        """TrackStart hook: maintain the per-guild playback history.

        Rules that keep the history honest:
        * TTS clips never enter it (they are not music).
        * A track replayed immediately (track loop, TTS restore) does not
          create a second consecutive entry.
        * Navigating back via Previous moves the cursor; starting any new
          track afterwards truncates the abandoned branch.
        """
        guild_id = getattr(player, "guild_id", None)
        if guild_id is None:
            return
        session = self.session(guild_id)
        if session.tts_active or track is None:
            return

        if session.history_intent == "previous":
            session.history_intent = None
            if 0 <= session.history_index < len(session.playback_history):
                entry = session.playback_history[session.history_index]
                if _same_track(entry.track, track):
                    entry.track = track  # keep the freshest decoded object
                    return
            # The node started something else than we asked for — fall through
            # and record it as a normal start instead of corrupting the cursor.

        if (
            session.playback_history
            and session.history_index == len(session.playback_history) - 1
            and _same_track(session.playback_history[-1].track, track)
        ):
            return  # same track playing again right away — no duplicate entry

        if session.history_index < len(session.playback_history) - 1:
            del session.playback_history[session.history_index + 1 :]
        session.playback_history.append(HistoryEntry(track))
        session.history_index = len(session.playback_history) - 1

        limit = max(5, self.config.music_history_limit or _DEFAULT_HISTORY_LIMIT)
        if len(session.playback_history) > limit:
            overflow = len(session.playback_history) - limit
            del session.playback_history[:overflow]
            session.history_index -= overflow

    def _note_resume_position(self, player, session: GuildSession) -> None:
        """Remember where the current track is, for a future Previous jump."""
        if not (0 <= session.history_index < len(session.playback_history)):
            return
        current = getattr(player, "current", None)
        if current is None:
            return
        entry = session.playback_history[session.history_index]
        if not _same_track(entry.track, current):
            return
        if getattr(current, "is_stream", False):
            return
        duration = int(getattr(current, "duration", 0) or 0)
        position = max(0, int(getattr(player, "position", 0) or 0))
        if duration > 0:
            position = min(position, duration - 500)
        entry.resume_ms = position

    async def _advance(self, player) -> None:
        """Skip while keeping track-loop from replaying the skipped track."""
        loop_value = getattr(player, "loop", 0) or 0
        if loop_value == 1:
            self._set_player_loop(player, 0)
            try:
                await player.skip()
            finally:
                self._set_player_loop(player, 1)
        else:
            await player.skip()

    async def _recover_player(self, player, note: str, *, force: bool) -> None:
        """Move past a broken track without letting track-loop resurrect it."""
        guild_id = getattr(player, "guild_id", None)
        if guild_id is not None:
            self.session(guild_id).note(note)
        loop_value = getattr(player, "loop", 0) or 0
        if loop_value == 1:
            self._set_player_loop(player, 0)
            try:
                await player.skip()
            except Exception:
                log.debug("Recovery skip failed", exc_info=True)
            finally:
                self._set_player_loop(player, 1)
        elif force:
            try:
                await player.skip()
            except Exception:
                log.debug("Recovery skip failed", exc_info=True)

    @staticmethod
    def _set_player_loop(player, value: int) -> None:
        try:
            player.set_loop(value)
        except (AttributeError, ValueError):
            log.debug("Player loop could not be set to %s", value, exc_info=True)

    async def set_volume(self, guild: discord.Guild, member: discord.Member, volume: int) -> int:
        player = self._require_connected(guild)
        self.require_connected_control(guild, member)
        if not isinstance(volume, int) or isinstance(volume, bool):
            raise MusicError(f"volume needs to be a number between 0 and {self.max_volume}.")
        if not 0 <= volume <= self.max_volume:
            raise MusicError(
                f"volume needs to be between 0 and {self.max_volume}. "
                "anything louder than that sounds terrible."
            )
        await player.set_volume(volume)
        session = self.session(guild.id)
        session.volume = volume
        session.note(f"volume set to {volume}%")
        return volume

    def clear_queue(self, guild: discord.Guild, member: discord.Member) -> int:
        player = self._require_connected(guild)
        self.require_connected_control(guild, member)
        removed = len(player.queue)
        player.queue.clear()
        if removed:
            self.session(guild.id).note(f"cleared {removed} upcoming track" + ("s" if removed != 1 else ""))
        return removed

    def shuffle_queue(self, guild: discord.Guild, member: discord.Member) -> int:
        player = self._require_connected(guild)
        self.require_connected_control(guild, member)
        if len(player.queue) < 2:
            raise MusicError("nothing to shuffle — the queue needs at least two tracks.")
        random.shuffle(player.queue)
        self.session(guild.id).note(f"shuffled {len(player.queue)} tracks")
        return len(player.queue)

    def remove_track(self, guild: discord.Guild, member: discord.Member, position: int) -> TrackInfo:
        """Remove one upcoming track by its 1-based queue position."""
        player = self._require_connected(guild)
        self.require_connected_control(guild, member)
        if not isinstance(position, int) or isinstance(position, bool):
            raise MusicError("position must be a whole number.")
        if not player.queue:
            raise MusicError("the queue is empty — nothing to remove.")
        if not 1 <= position <= len(player.queue):
            raise MusicError(
                f"position must be between 1 and {len(player.queue)} — that's how "
                "many tracks are queued."
            )
        track = player.queue.pop(position - 1)
        self.session(guild.id).note(f"removed #{position} · {shorten(track.title, 50)}")
        return TrackInfo.from_track(track)

    def move_track(
        self, guild: discord.Guild, member: discord.Member, source: int, target: int
    ) -> TrackInfo:
        """Move an upcoming track from one 1-based queue position to another."""
        player = self._require_connected(guild)
        self.require_connected_control(guild, member)
        for value in (source, target):
            if not isinstance(value, int) or isinstance(value, bool):
                raise MusicError("positions must be whole numbers.")
        size = len(player.queue)
        if not size:
            raise MusicError("the queue is empty — nothing to move.")
        if not (1 <= source <= size and 1 <= target <= size):
            raise MusicError(f"both positions must be between 1 and {size}.")
        track = player.queue.pop(source - 1)
        player.queue.insert(target - 1, track)
        self.session(guild.id).note(
            f"moved · {shorten(track.title, 50)} → position {target}"
        )
        return TrackInfo.from_track(track)

    # -- loop / autoplay ----------------------------------------------------

    async def set_loop(self, guild: discord.Guild, member: discord.Member, mode: str | None) -> str:
        """Set the loop mode (off/track/queue), or cycle when mode is None.

        Uses Lavalink.py's native player loop: track-mode replays the current
        track on end, queue-mode re-appends it — no client-side queue juggling.
        """
        player = self._require_connected(guild)
        self.require_connected_control(guild, member)
        session = self.session(guild.id)
        if mode is None:
            mode = _LOOP_NEXT.get(session.loop_mode, "track")
        mode = str(mode).strip().casefold()
        if mode not in _LOOP_VALUES:
            raise MusicError("loop mode must be **off**, **track** or **queue**.")
        self._set_player_loop(player, _LOOP_VALUES[mode])
        session.loop_mode = mode
        session.note(f"{_LOOP_LABELS[mode].lower()}")
        return mode

    def set_autoplay(self, guild: discord.Guild, member: discord.Member, enabled: bool | None) -> bool:
        """Toggle Groove-style autoplay: keep related music coming on queue end."""
        self._require_connected(guild)  # validation only: autoplay needs a live player
        self.require_connected_control(guild, member)
        session = self.session(guild.id)
        session.autoplay_enabled = (
            not session.autoplay_enabled if enabled is None else bool(enabled)
        )
        session.note("autoplay on — I'll keep the music going" if session.autoplay_enabled else "autoplay off")
        return session.autoplay_enabled

    async def _maybe_autoplay(self, player, session: GuildSession) -> bool:
        """QueueEnd hook: try to continue with related tracks. True = handled."""
        guild_id = player.guild_id
        lock = self._autoplay_locks.get(guild_id)
        if lock is None:
            lock = self._autoplay_locks[guild_id] = asyncio.Lock()
        if lock.locked():
            return True  # a fill is already running for this guild
        async with lock:
            if self._closing or session.tts_active or not session.autoplay_enabled:
                return False
            if getattr(player, "is_playing", False) or player.current is not None:
                return False  # someone else took over the player meanwhile
            if session.autoplay_failures >= 3:
                return False  # gave up quietly; the next /play re-arms it
            if time.monotonic() < session.autoplay_retry_at:
                return False  # still inside the backoff window
            tracks = await self._find_related_tracks(player, session)
            if not tracks:
                session.autoplay_failures += 1
                session.autoplay_retry_at = time.monotonic() + 90 * min(session.autoplay_failures, 3)
                if session.autoplay_failures >= 3:
                    session.note("autoplay couldn't find anything related — add a track to keep going")
                return False
            session.autoplay_failures = 0
            session.autoplay_retry_at = 0.0
            for track in tracks:
                player.add(track, requester=0)
                self._remember_autoplay(session, track)
            try:
                if player.queue and not player.is_playing:
                    await player.play()
            except Exception:
                log.exception("Autoplay could not start playback in guild %s", guild_id)
                return False
            first = shorten(str(getattr(tracks[0], "title", "something")), 40)
            extra = f" +{len(tracks) - 1} more" if len(tracks) > 1 else ""
            session.note(f"autoplay · {first}{extra}")
            return True

    async def _retry_autoplay(self, guild_id: int) -> None:
        """Maintenance-side autoplay nudge (backed off inside _maybe_autoplay)."""
        if self._closing or not self.node_ready:
            return
        player = self.player(guild_id)
        if player is None or not player.is_connected:
            return
        session = self.session(guild_id)
        if not session.autoplay_enabled or session.tts_active:
            return
        if player.current is not None or player.queue:
            return
        if await self._maybe_autoplay(player, session):
            await self.refresh_panel(guild_id)

    def _remember_autoplay(self, session: GuildSession, track) -> None:
        key = normalize_track_key(
            str(getattr(track, "title", "") or ""),
            str(getattr(track, "author", "") or ""),
        )
        if key:
            session.autoplay_history.append(key)
        identifier = str(getattr(track, "identifier", "") or "")
        if identifier:
            session.autoplay_history.append(identifier)

    async def _find_related_tracks(self, player, session: GuildSession) -> list:
        """Search for a small batch of tracks related to what just played."""
        last = None
        if session.playback_history and session.history_index >= 0:
            last = session.playback_history[session.history_index].track
        if last is None:
            last = getattr(player, "current", None)
        if last is None:
            return []

        queries = self._autoplay_queries(last)
        if not queries:
            return []
        excluded = self._autoplay_exclusions(player, session)
        chosen: list = []
        seen: set[str] = set()
        for query in queries:
            if len(chosen) >= _AUTOPLAY_BATCH:
                break
            try:
                results = await self.client.get_tracks(query)
            except Exception:
                log.warning("Autoplay search failed for %r", query[:120], exc_info=True)
                continue
            if results is None or results.load_type is LoadType.ERROR or not results.tracks:
                continue
            candidates = list(results.tracks)
            if results.load_type is LoadType.PLAYLIST and len(candidates) > _AUTOPLAY_SCAN:
                candidates = random.sample(candidates, _AUTOPLAY_SCAN)
            for track in candidates:
                if len(chosen) >= _AUTOPLAY_BATCH:
                    break
                if getattr(track, "is_stream", False):
                    continue  # live streams make poor autoplay neighbours
                key = normalize_track_key(
                    str(getattr(track, "title", "") or ""),
                    str(getattr(track, "author", "") or ""),
                )
                if not key or key in seen or key in excluded:
                    continue
                identifier = str(getattr(track, "identifier", "") or "")
                if identifier and identifier in excluded:
                    continue
                seen.add(key)
                chosen.append(track)
        return chosen

    def _autoplay_queries(self, track) -> list[str]:
        """Related-track queries for one track, gated on the node's sources."""
        queries: list[str] = []
        identifier = getattr(track, "identifier", None)
        source = str(getattr(track, "source_name", "") or "").casefold()
        if (
            identifier
            and source in {"youtube", "ytmusic"}
            and (self.node_sources is None or "youtube" in self.node_sources)
        ):
            # YouTube's own radio mix for this exact track — the strongest
            # related-music signal available without extra credentials.
            queries.append(
                f"https://www.youtube.com/watch?v={identifier}&list=RD{identifier}"
            )
        artist = strip_title_noise(str(getattr(track, "author", "") or ""))
        if artist:
            for prefix in self._search_prefixes():
                queries.append(f"{prefix}:{artist}")
        return queries[:4]

    def _autoplay_exclusions(self, player, session: GuildSession) -> set[str]:
        """Everything autoplay must not pick: queue, history, past auto-adds."""
        excluded: set[str] = set()

        def add(track) -> None:
            key = normalize_track_key(
                str(getattr(track, "title", "") or ""),
                str(getattr(track, "author", "") or ""),
            )
            if key:
                excluded.add(key)
            identifier = str(getattr(track, "identifier", "") or "")
            if identifier:
                excluded.add(identifier)

        for entry in session.playback_history:
            add(entry.track)
        for track in list(getattr(player, "queue", []) or []):
            add(track)
        excluded.update(session.autoplay_history)
        excluded.discard("")
        return excluded

    # -- seeking -------------------------------------------------------------

    def _seekable_current(self, player, guild: discord.Guild):
        track = getattr(player, "current", None)
        if track is None:
            raise MusicError("nothing is playing right now.")
        if getattr(track, "is_stream", False) or not getattr(track, "is_seekable", True):
            raise MusicError("this track can't be seeked — live streams don't have a position.")
        return track

    async def seek(self, guild: discord.Guild, member: discord.Member, position_ms: int) -> int:
        """Seek to an absolute position. Returns the position in ms."""
        player = self._require_connected(guild)
        self.require_connected_control(guild, member)
        track = self._seekable_current(player, guild)
        if not isinstance(position_ms, int) or isinstance(position_ms, bool):
            raise MusicError("that position doesn't make sense — try something like 1:32.")
        if position_ms < 0:
            raise MusicError("positions can't be negative — use /forward or /backward.")
        duration = int(getattr(track, "duration", 0) or 0)
        if duration and position_ms >= duration:
            raise MusicError(
                f"that's past the end of the track — it's only {format_duration(duration)} long."
            )
        await player.seek(position_ms)
        self.session(guild.id).note(f"seeked to {format_duration(position_ms)}")
        return position_ms

    async def nudge(self, guild: discord.Guild, member: discord.Member, delta_ms: int) -> int:
        """Seek forward (positive) or backward (negative), clamped to the track."""
        player = self._require_connected(guild)
        self.require_connected_control(guild, member)
        track = self._seekable_current(player, guild)
        if not isinstance(delta_ms, int) or isinstance(delta_ms, bool) or delta_ms == 0:
            raise MusicError("that amount doesn't make sense — try a number of seconds.")
        duration = int(getattr(track, "duration", 0) or 0)
        position = max(0, int(getattr(player, "position", 0) or 0))
        target = position + delta_ms
        if duration:
            target = max(0, min(target, duration - 500))
        await player.seek(int(target))
        direction = "forward" if delta_ms > 0 else "back"
        self.session(guild.id).note(f"jumped {direction} to {format_duration(target)}")
        return int(target)

    # -- audio effects ---------------------------------------------------------

    def filter_presets(self) -> tuple[FilterPreset, ...]:
        """Every effect Paradise knows how to build for this stack."""
        return _FILTER_PRESETS

    def filter_preset(self, name: str) -> FilterPreset:
        preset = _FILTER_BY_NAME.get(str(name or "").strip().casefold())
        if preset is None:
            raise MusicError("I don't know that effect. try one from the list.")
        return preset

    def filter_supported(self, preset: FilterPreset) -> bool:
        """True when the node's own report says this filter is enabled."""
        return self.node_filters is None or preset.node_filter in self.node_filters

    def unsupported_filter_names(self) -> list[str]:
        if self.node_filters is None:
            return []
        return [
            preset.label
            for preset in _FILTER_PRESETS
            if preset.node_filter not in self.node_filters
        ]

    def active_filter_presets(self, guild_id: int) -> list[FilterPreset]:
        session = self.session(guild_id)
        return [
            preset for preset in _FILTER_PRESETS if preset.name in session.active_presets
        ]

    async def set_filter(self, guild: discord.Guild, member: discord.Member, name: str) -> str:
        """Toggle one audio effect. Returns "applied" or "removed"."""
        player = self._require_connected(guild)
        self.require_connected_control(guild, member)
        preset = self.filter_preset(name)
        if not self.filter_supported(preset):
            raise MusicError(
                f"the music node doesn't support the **{preset.label}** effect — "
                "it isn't enabled there."
            )
        session = self.session(guild.id)
        try:
            if preset.name in session.active_presets:
                session.active_presets.discard(preset.name)
                remaining = [
                    self.filter_preset(other).factory()
                    for other in session.active_presets
                ]
                await player.set_filters(*remaining, replace=True)
                session.note(f"{preset.label} off")
                return "removed"
            # Presets that share a node filter (nightcore/daycore both use
            # timescale) replace each other at the node — reflect that here.
            for other in list(session.active_presets):
                if self.filter_preset(other).node_filter == preset.node_filter:
                    session.active_presets.discard(other)
            await player.set_filter(preset.factory())
            session.active_presets.add(preset.name)
            session.note(f"{preset.label} on")
            return "applied"
        except MusicError:
            raise
        except Exception:
            # The node rejected the filter update (older build, disabled
            # filter, …) — never crash the player over an effect.
            log.warning("Filter %s could not be applied", preset.name, exc_info=True)
            raise MusicError(
                f"the music node wouldn't take the **{preset.label}** effect — "
                "it may not be enabled there."
            ) from None

    async def clear_filters(self, guild: discord.Guild, member: discord.Member) -> int:
        player = self._require_connected(guild)
        self.require_connected_control(guild, member)
        session = self.session(guild.id)
        removed = len(session.active_presets)
        session.active_presets.clear()
        try:
            await player.clear_filters()
        except Exception:
            log.warning("Clearing filters failed", exc_info=True)
            raise MusicError("couldn't clear the effects — the node didn't respond.") from None
        if removed:
            session.note("effects off")
        return removed

    async def stop(self, guild: discord.Guild, member: discord.Member | None = None) -> None:
        """Stop playback, clear queues, disconnect, reset the panel."""
        if member is not None:
            self.require_connected_control(guild, member)
        player = self.player(guild.id)
        if player is not None and player.is_connected:
            player.queue.clear()
            try:
                await player.stop()
            except Exception:
                log.debug("Player stop failed", exc_info=True)
        vc = getattr(guild, "voice_client", None)
        if vc is not None and callable(getattr(vc, "disconnect", None)):
            try:
                await vc.disconnect(force=True)
            except Exception:
                log.debug("Voice disconnect failed", exc_info=True)
        self._reset_session(guild.id)
        if self.tts is not None:
            await self.tts.cancel(guild.id)
        self.session(guild.id).note("stopped and disconnected")

    def _reset_session(self, guild_id: int) -> None:
        self.session(guild_id).reset(keep_volume=True)

    async def handle_forced_disconnect(self, guild_id: int) -> None:
        """Discord kicked us (or the channel vanished): clean up quietly."""
        self._reset_session(guild_id)
        if self.tts is not None:
            await self.tts.cancel(guild_id)
        await self.refresh_panel(guild_id)

    # ------------------------------------------------------------------
    # TTS playback hooks (used by TTSService — never by commands)
    # ------------------------------------------------------------------

    async def play_tts(self, guild_id: int, url: str, text: str):
        """Start speaking an uploaded clip, stashing music for later."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            raise MusicError("I can't find that server anymore.")
        player = self._require_connected(guild)
        try:
            results = await self.client.get_tracks(url)
        except Exception:
            log.exception("TTS track load failed")
            raise MusicError("couldn't queue the voice clip.") from None
        if results.load_type is not LoadType.TRACK or not results.tracks:
            raise NodePlaybackUnsupported("the music node can't play direct audio links")

        track = results.tracks[0]
        session = self.session(guild_id)
        if not session.tts_active:
            session.tts_active = True
            if player.current is not None:
                position = 0 if player.current.is_stream else min(
                    max(player.position, 0), max(player.current.duration - 1000, 0)
                )
                session.tts_stash = (
                    player.current,
                    position,
                    list(player.queue),
                    player.paused,
                )
                player.queue.clear()
            else:
                session.tts_stash = None
            # Park any loop mode while TTS owns the player: the library's
            # loop logic would otherwise re-queue music around the clip and
            # the clip's own end would start the next music track mid-speech.
            session.tts_loop_value = getattr(player, "loop", 0) or 0
            if session.tts_loop_value:
                self._set_player_loop(player, 0)
        session.tts_text = text
        await player.play(track)
        return track

    async def wait_for_track_end(self, guild_id: int, track, timeout: float) -> bool:
        """Wait until the given track stops being the player's current track."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            player = self.player(guild_id)
            guild = self.bot.get_guild(guild_id)
            if (
                player is None
                or not player.is_connected
                or getattr(guild, "voice_client", None) is None
                or self._closing
            ):
                return False
            current = player.current
            if current is None or current.track != getattr(track, "track", None):
                return True
            await asyncio.sleep(0.3)
        return False

    async def finish_tts(self, guild_id: int) -> None:
        """Restore whatever was playing before TTS interrupted it."""
        session = self.session(guild_id)
        if not session.tts_active:
            return
        session.tts_active = False
        stash = session.tts_stash
        session.tts_stash = None
        session.tts_text = None
        parked_loop = session.tts_loop_value
        session.tts_loop_value = 0
        player = self.player(guild_id)
        if player is None or not player.is_connected:
            return
        if stash is not None:
            track, position, queue, was_paused = stash
            try:
                if getattr(track, "is_stream", False):
                    await player.play(track, pause=was_paused)
                else:
                    await player.play(track, start_time=position, pause=was_paused)
            except (ValueError, TypeError):
                try:
                    await player.play(track, pause=was_paused)
                except Exception:
                    log.debug("TTS music restore failed", exc_info=True)
            except Exception:
                log.debug("TTS music restore failed", exc_info=True)
            player.queue.extend(queue)
        elif player.current is not None:
            try:
                await player.stop()
            except Exception:
                log.debug("Stop after TTS failed", exc_info=True)
        if parked_loop:
            self._set_player_loop(player, parked_loop)
        await self.refresh_panel(guild_id)

    def abort_tts(self, guild_id: int) -> None:
        """Drop TTS state without restoring anything (disconnect path)."""
        session = self.session(guild_id)
        session.tts_active = False
        session.tts_stash = None
        session.tts_text = None
        session.tts_loop_value = 0

    # ------------------------------------------------------------------
    # Maintenance loop: auto-disconnect + panel progress refresh
    # ------------------------------------------------------------------

    async def _maintenance_loop(self) -> None:
        await self.bot.wait_until_ready()
        while not self._closing and not self.bot.is_closed():
            try:
                await self._maintenance_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Music maintenance tick failed")
            await asyncio.sleep(_MAINTENANCE_INTERVAL)

    async def _maintenance_tick(self) -> None:
        threshold = max(60, self.config.auto_disconnect_minutes * 60)
        for guild in list(getattr(self.bot, "guilds", [])):
            vc = getattr(guild, "voice_client", None)
            if vc is None or not callable(getattr(vc, "disconnect", None)):
                continue
            player = self.player(guild.id)
            if player is None or not player.is_connected:
                continue
            session = self.session(guild.id)
            settings = self.settings.get(guild.id)

            humans = self._humans_in_channel(guild, player.channel_id)
            now = time.monotonic()
            if humans:
                session.alone_since = None
            elif session.alone_since is None:
                session.alone_since = now
            elif (
                not session.sticky
                and settings.auto_disconnect
                and not session.tts_active
                and now - session.alone_since >= threshold
            ):
                log.info("Auto-disconnecting from guild %s (empty channel)", guild.id)
                await self.stop(guild)
                self.session(guild.id).note("left an empty voice channel")
                await self.refresh_panel(guild.id)
                continue

            if session.tts_active or player.is_playing:
                await self.refresh_panel(guild.id)
            elif session.autoplay_enabled and player.current is None and not player.queue:
                # Autoplay is on but nothing is queued or playing: the last
                # fill found nothing. Give it one quiet retry per tick so a
                # transient search failure doesn't end the session forever.
                self._maybe_task(self._retry_autoplay(guild.id))

    @staticmethod
    def _humans_in_channel(guild: discord.Guild, channel_id: int | None) -> int:
        if channel_id is None:
            return 0
        count = 0
        for member, state in list(guild.voice_states.items()):
            if state.channel and state.channel.id == channel_id and not member.bot:
                count += 1
        return count

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------

    @staticmethod
    def _maybe_task(coro) -> None:
        """Schedule a coroutine without awaiting it (fire and forget, logged)."""
        task = asyncio.ensure_future(coro)

        def _done(future: asyncio.Future) -> None:
            if not future.cancelled() and future.exception() is not None:
                log.debug("Background task failed", exc_info=future.exception())

        task.add_done_callback(_done)


def _can_send(channel: discord.abc.GuildChannel | None, guild: discord.Guild | None) -> bool:
    if channel is None or guild is None:
        return False
    me = guild.me
    if me is None:
        return True
    perms = channel.permissions_for(me)
    return bool(perms and perms.send_messages)


def _log_load_error(identifier: str, results) -> None:
    """Log a node-side load failure privately; users only ever see a short note."""
    error = getattr(results, "error", None)
    detail = f" ({error})" if error else ""
    log.warning("Lavalink could not load %r%s", identifier[:200], detail)
