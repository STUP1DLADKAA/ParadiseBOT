"""MusicService: the single source of truth for Paradise's music state.

Slash commands, panel buttons, TTS playback and auto-disconnect all funnel
through the methods on this class. Nothing else touches Lavalink directly.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from typing import TYPE_CHECKING

import discord
import lavalink
from lavalink.events import (
    NodeReadyEvent,
    QueueEndEvent,
    TrackExceptionEvent,
    TrackStartEvent,
    TrackStuckEvent,
)
from lavalink.server import LoadType

from functions.helpers import (
    MusicError,
    NodePlaybackUnsupported,
    can_bypass_voice_check,
    same_voice_channel,
    shorten,
    voice_channel_name,
)
from functions.music.panel import (
    JoinResult,
    MusicPanelView,
    PanelState,
    PlayResult,
    SkipResult,
    TrackInfo,
    button_states,
    render_panel,
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
        self._maintenance_task: asyncio.Task | None = None
        self._closing = False
        # What the node reported about itself (refreshed on every node ready).
        self.node_plugins: dict[str, str] = {}
        self.node_sources: frozenset[str] | None = None
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
        await self.refresh_panel(event.player.guild_id)

    @lavalink.listener(QueueEndEvent)
    async def _on_queue_end(self, event: QueueEndEvent) -> None:
        session = self.session(event.player.guild_id)
        if session.tts_active:
            return  # the TTS worker owns the next transition
        session.last_action = None
        session.last_action_at = 0.0
        await self.refresh_panel(event.player.guild_id)

    @lavalink.listener(TrackExceptionEvent)
    async def _on_track_exception(self, event: TrackExceptionEvent) -> None:
        log.warning(
            "Track error in guild %s: %s", event.player.guild_id, getattr(event, "exception", "?")
        )
        self.session(event.player.guild_id).note("last track errored — skipped it")
        try:
            await event.player.skip()
        except Exception:
            log.debug("Skip after track exception failed", exc_info=True)
        await self.refresh_panel(event.player.guild_id)

    @lavalink.listener(TrackStuckEvent)
    async def _on_track_stuck(self, event: TrackStuckEvent) -> None:
        log.warning("Track stuck in guild %s", event.player.guild_id)
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
            raw_version = info.get("version")
            if isinstance(raw_version, dict):  # {"semver": "4.2.1", ...}
                version = str(raw_version.get("semver") or "") or None
            elif isinstance(raw_version, str):
                version = raw_version or None
            else:
                version = None
            self.node_plugins = plugins
            self.node_sources = sources or None
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
        )
        if not connected:
            return state

        state.voice_channel = voice_channel_name(guild, player.channel_id)
        state.status = "idle"
        if session.tts_active:
            state.status = "speaking"
            state.spoken_text = session.tts_text
            if session.tts_stash is not None:
                state.resumes_after = TrackInfo.from_track(session.tts_stash[0])
        elif player.current is not None:
            state.track = TrackInfo.from_track(player.current)
            state.position_ms = player.position if not player.current.is_stream else 0
            state.status = "paused" if player.paused else "playing"
        state.upcoming = [TrackInfo.from_track(t) for t in player.queue]
        return state

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
        state = self.panel_state(guild_id)
        embed = render_panel(state)
        view = MusicPanelView(self, guild_id, button_states(state))

        async with self._panel_lock(guild_id):
            message = self._panel_messages.get(guild_id)
            if message is not None:
                try:
                    await message.edit(embed=embed, view=view)
                    return
                except discord.NotFound:
                    self._panel_messages.pop(guild_id, None)
                    await self._forget_panel(guild_id)
                except discord.HTTPException:
                    log.debug("Panel edit failed (rate limit?)", exc_info=True)
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
                message = await channel.send(embed=embed, view=view)
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
        session = self.session(guild.id)
        if session.volume != 100:
            try:
                await player.set_volume(session.volume)
            except Exception:
                log.debug("Volume restore failed", exc_info=True)

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
        skipped = TrackInfo.from_track(player.current)
        up_next = TrackInfo.from_track(player.queue[0]) if player.queue else None
        await player.skip()
        self.session(guild.id).note(f"skipped · {shorten(skipped.title, 60)}")
        return SkipResult(skipped, up_next)

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
        await self.refresh_panel(guild_id)

    def abort_tts(self, guild_id: int) -> None:
        """Drop TTS state without restoring anything (disconnect path)."""
        session = self.session(guild_id)
        session.tts_active = False
        session.tts_stash = None
        session.tts_text = None

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
                and now - session.alone_since >= threshold
            ):
                log.info("Auto-disconnecting from guild %s (empty channel)", guild.id)
                await self.stop(guild)
                self.session(guild.id).note("left an empty voice channel")
                await self.refresh_panel(guild.id)
                continue

            if session.tts_active or player.is_playing:
                await self.refresh_panel(guild.id)

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
    return bool(perms and perms.send_messages and perms.embed_links)


def _log_load_error(identifier: str, results) -> None:
    """Log a node-side load failure privately; users only ever see a short note."""
    error = getattr(results, "error", None)
    detail = f" ({error})" if error else ""
    log.warning("Lavalink could not load %r%s", identifier[:200], detail)
