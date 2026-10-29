"""One voice connection per guild, and everything that happens on it.

The rule that keeps this simple: **Paradise owns exactly one audio source per
guild.** A song is a source; a spoken answer is not a second source, it is
frames pushed into the *same* mixer, which ducks the song underneath it. That
is why "answer a question while music is playing" needs no queue juggling,
no reconnect and no pause — and why the song resumes at full volume the
instant the sentence ends.

Skipping is expressed with a generation counter, not with locks: every source
that starts gets a number, and a callback whose number is stale is ignored.
Discord fires the ``after`` callback for a source that was stopped on purpose
too, so without the counter a single "skip" would advance the queue twice.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Iterable

import discord

from config import MusicConfig
from functions.helpers import ParadiseError

from .decoder import FRAME_SECONDS, DecodeError, PcmStream, resolve_ffmpeg
from .mixer import MusicMixer
from .models import LoopMode, PlayerState, Track
from .queue import MusicQueue, QueueFull
from .search import NoResults, SearchUnavailable, TrackResolver, YtDlpResolver

log = logging.getLogger(__name__)

#: Never let a backlog of answers pile up behind the music.
MAX_SPEECH_SECONDS = 120.0
#: How many tracks to try before giving up on a broken queue entry.
MAX_ADVANCE_ATTEMPTS = 3


class MusicError(ParadiseError):
    """A music action could not be completed. The message is user-safe."""


class NotConnected(MusicError):
    """Nobody is in a voice channel Paradise can join."""


class GuildPlayer:
    """The audio state of a single guild."""

    def __init__(self, service: "MusicService", guild_id: int) -> None:
        self.service = service
        self.guild_id = guild_id
        self.config: MusicConfig = service.config
        self.queue = MusicQueue(self.config.max_queue)
        self._voice: discord.VoiceClient | None = None
        self._mixer: MusicMixer | None = None
        self._speech_mixer: MusicMixer | None = None
        self._current: Track | None = None
        self._last_finished: Track | None = None
        self._paused = False
        self._volume = self.config.volume
        self._loop_mode = LoopMode.OFF
        self._generation = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._idle_task: asyncio.Task | None = None
        self._repause_task: asyncio.Task | None = None
        self.last_error = ""

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    @property
    def voice(self) -> discord.VoiceClient | None:
        return self._voice

    @property
    def channel_id(self) -> int:
        channel = getattr(self._voice, "channel", None)
        return int(getattr(channel, "id", 0) or 0)

    async def connect(self, channel) -> discord.VoiceClient:
        """Join *channel* (or move there) through the one shared helper."""
        self._loop = asyncio.get_running_loop()
        client = self._voice
        if client is not None and client.is_connected():
            if channel is None:
                return client  # already somewhere; no move requested
            same = getattr(client.channel, "id", None) == getattr(channel, "id", None)
            if not same and not client.is_playing():
                try:
                    await client.move_to(channel)
                except discord.DiscordException:
                    log.debug("Could not move to %s", getattr(channel, "id", "?"), exc_info=True)
            return client

        bot = self.service.bot
        guild = getattr(channel, "guild", None)
        presence = bot.get_cog("VoicePresence") if hasattr(bot, "get_cog") else None
        helper = getattr(presence, "ensure_connected", None)
        connected = None
        if helper is not None and guild is not None:
            connected = await helper(guild, channel)
        if connected is None:
            try:
                connected = await channel.connect(
                    timeout=15.0, self_deaf=False, cls=self._voice_client_class()
                )
            except (asyncio.TimeoutError, discord.DiscordException) as error:
                raise NotConnected(
                    "i couldn't join your voice channel. check my **Connect** and "
                    "**Speak** permissions."
                ) from error
        if connected is None:
            raise NotConnected(
                "i couldn't join that voice channel. check my permissions."
            )
        self._voice = connected
        return connected

    def _voice_client_class(self):
        presence = self.service.bot.get_cog("VoicePresence") if hasattr(
            self.service.bot, "get_cog"
        ) else None
        factory = getattr(presence, "_voice_client_class", None)
        if callable(factory):
            try:
                return factory()
            except Exception:  # noqa: BLE001 - fall back to the default client
                log.debug("voice client class lookup failed", exc_info=True)
        # Without the presence cog, decide here: a receive-capable client is
        # required whenever the clip buffer or the spoken wake word is on.
        config = getattr(self.service.bot, "config", None)
        if bool(getattr(getattr(config, "voice_clip", None), "enabled", False)) or bool(
            getattr(getattr(config, "voice_wake", None), "listening", False)
        ):
            try:
                from functions.voice_clip import ClipVoiceClient

                return ClipVoiceClient
            except Exception:  # noqa: BLE001 - optional dependency
                log.debug("ClipVoiceClient unavailable", exc_info=True)
        return discord.VoiceClient

    def detach(self) -> None:
        """Forget the voice client (it was disconnected behind our back)."""
        self._voice = None
        self._cancel_idle()

    # ------------------------------------------------------------------
    # Queue + playback
    # ------------------------------------------------------------------

    async def play_query(self, query: str, *, requester_id: int = 0, requester_name: str = "") -> list[Track]:
        """Search and start (or enqueue) a request. Returns what was added."""
        tracks = await self.service.resolver.search(
            query,
            requester_id=requester_id,
            requester_name=requester_name,
            limit=self.config.search_results,
        )
        if self.config.max_track_seconds:
            too_long = [t for t in tracks if t.duration > self.config.max_track_seconds]
            tracks = [t for t in tracks if t.duration <= self.config.max_track_seconds]
            if not tracks and too_long:
                raise MusicError(
                    f"“{too_long[0].title}” is longer than the "
                    f"{self.config.max_track_seconds // 60} minute limit."
                )
        if not tracks:
            raise NoResults("i couldn't find anything for that.")
        await self.enqueue(tracks)
        return tracks

    async def enqueue(self, tracks: Iterable[Track]) -> list[Track]:
        added: list[Track] = []
        for track in tracks:
            if self._current is None and self._mixer is None:
                await self.start(track)
            else:
                try:
                    self.queue.add(track)
                except QueueFull as error:
                    raise MusicError(str(error)) from error
            added.append(track)
        return added

    async def start(self, track: Track) -> Track:
        """Resolve, decode and play a single track right now."""
        if self._voice is None or not self._voice.is_connected():
            raise NotConnected("i'm not in a voice channel. use `/music play` first.")
        try:
            url = await self.service.resolver.stream_url(track)
        except (SearchUnavailable, NoResults) as error:
            raise MusicError(str(error)) from error

        stream = self.service.open_stream(url)
        try:
            await asyncio.get_running_loop().run_in_executor(None, stream.open)
        except DecodeError as error:
            raise MusicError(str(error)) from error

        mixer = MusicMixer(
            stream,
            volume=self._volume,
            duck_volume=self.config.duck_volume,
            fade_frames=max(1, round(self.config.fade_ms / 20)),
        )
        self._current = track
        self._paused = False
        self._cancel_idle()
        # Stop whatever was playing without letting its callback advance us.
        self._generation += 1
        if self._voice.is_playing() or self._voice.is_paused():
            try:
                self._voice.stop()
            except Exception:  # noqa: BLE001 - a dead client is not fatal
                log.debug("could not stop the previous source", exc_info=True)
        self._play_source(mixer, kind="music")
        log.info("Music: now playing “%s” in guild %s", track.title, self.guild_id)
        return track

    def _play_source(self, mixer: MusicMixer, *, kind: str) -> None:
        if self._voice is None:
            raise NotConnected("i'm not connected to a voice channel.")
        self._generation += 1
        generation = self._generation
        if kind == "music":
            self._mixer = mixer
        else:
            self._speech_mixer = mixer
        self._voice.play(
            mixer,
            after=lambda error: self._on_source_done(generation, kind, error),
        )

    def _on_source_done(self, generation: int, kind: str, error: Exception | None) -> None:
        """Discord's ``after`` callback — always runs on the event loop."""
        if generation != self._generation:
            return  # a stale source we already replaced on purpose
        if error is not None:
            self.last_error = f"{error.__class__.__name__}: {error}"
            log.debug("Music source ended with an error: %s", error)

        if kind == "speech":
            mixer, self._speech_mixer = self._speech_mixer, None
            if mixer is not None:
                mixer.cleanup()
            self._schedule_idle()
            return

        mixer, self._mixer = self._mixer, None
        if mixer is not None:
            mixer.cleanup()
        self._last_finished = self._current
        self._current = None
        self._paused = False
        loop = self._event_loop()
        if loop is not None and loop.is_running():
            loop.create_task(self._advance())
        else:  # pragma: no cover - shutting down
            log.debug("no running loop; queue left as it was")

    async def _advance(self) -> None:
        """Play the next thing, honouring the loop mode."""
        finished, self._last_finished = self._last_finished, None
        attempts = 0
        while attempts < MAX_ADVANCE_ATTEMPTS:
            attempts += 1
            upcoming: Track | None
            if self._loop_mode is LoopMode.TRACK and finished is not None:
                upcoming = finished
            else:
                if self._loop_mode is LoopMode.QUEUE and finished is not None:
                    try:
                        self.queue.add(finished)
                    except QueueFull:
                        pass
                upcoming = self.queue.pop()
            if upcoming is None:
                self._schedule_idle()
                return
            try:
                await self.start(upcoming)
                return
            except NotConnected:
                return
            except MusicError as error:
                self.last_error = str(error)
                log.info("Skipping unplayable track: %s", error)
        self._schedule_idle()

    # ------------------------------------------------------------------
    # Controls
    # ------------------------------------------------------------------

    def pause(self) -> bool:
        if self._voice is None or not self._voice.is_playing():
            return False
        self._voice.pause(True)
        self._paused = True
        return True

    def resume(self) -> bool:
        if self._voice is None or not self._paused:
            return False
        self._voice.pause(False)
        self._paused = False
        return True

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def playing_music(self) -> bool:
        return self._mixer is not None and self._current is not None

    async def skip(self, count: int = 1) -> Track | None:
        """Drop the current song and start the next one."""
        steps = max(1, int(count))
        for _ in range(steps - 1):
            self.queue.pop()
        current = self._current
        self._last_finished = current
        self._current = None
        self._generation += 1
        if self._voice is not None and (self._voice.is_playing() or self._voice.is_paused()):
            try:
                self._voice.stop()
            except Exception:  # noqa: BLE001
                log.debug("stop failed during skip", exc_info=True)
        mixer, self._mixer = self._mixer, None
        if mixer is not None:
            mixer.cleanup()
        self._paused = False
        await self._advance()
        return self._current

    async def stop(self) -> None:
        """Stop the music and empty the queue (stays in the channel)."""
        self.queue.clear()
        self._last_finished = None
        self._current = None
        self._generation += 1
        self._paused = False
        if self._voice is not None:
            try:
                self._voice.stop()
            except Exception:  # noqa: BLE001
                log.debug("stop failed", exc_info=True)
        for attr in ("_mixer", "_speech_mixer"):
            mixer = getattr(self, attr)
            setattr(self, attr, None)
            if mixer is not None:
                mixer.cleanup()
        self._schedule_idle()

    async def leave(self) -> bool:
        """Disconnect and forget everything."""
        await self.stop()
        self._cancel_idle()
        if self._repause_task is not None:
            self._repause_task.cancel()
            self._repause_task = None
        client = self._voice
        self._voice = None
        if client is not None:
            try:
                await client.disconnect(force=True)
            except Exception:  # noqa: BLE001 - already gone
                log.debug("disconnect failed", exc_info=True)
            return True
        return False

    def set_volume(self, value: float) -> float:
        self._volume = max(0.0, min(2.0, float(value)))
        if self._mixer is not None:
            self._mixer.master_volume = self._volume
        return self._volume

    @property
    def volume(self) -> float:
        return self._volume

    def set_loop(self, mode: LoopMode | str) -> LoopMode:
        self._loop_mode = mode if isinstance(mode, LoopMode) else LoopMode.parse(mode)
        return self._loop_mode

    @property
    def loop_mode(self) -> LoopMode:
        return self._loop_mode

    def shuffle(self) -> int:
        return self.queue.shuffle()

    # ------------------------------------------------------------------
    # Speaking over the music
    # ------------------------------------------------------------------

    async def speak(self, pcm: bytes) -> bool:
        """Play decoded speech, mixing it over the song at the duck level."""
        if not pcm:
            return False
        if self._voice is None or not self._voice.is_connected():
            return False

        mixer = self._mixer
        if mixer is not None and (self._voice.is_playing() or self._voice.is_paused()):
            if mixer.speech_seconds > MAX_SPEECH_SECONDS:
                log.debug("speech backlog full; dropping this answer")
                return False
            if not mixer.push_speech(pcm):
                return False
            if self._paused:
                # A paused song must still let the answer through, then pause
                # again — otherwise "pause" silences the bot entirely.
                self._voice.pause(False)
                self._schedule_repause()
            return True

        if self._voice.is_playing() or self._voice.is_paused():
            return False  # something else owns the source

        speech_mixer = MusicMixer(None, volume=1.0, duck_volume=1.0)
        speech_mixer.push_speech(pcm)
        try:
            self._play_source(speech_mixer, kind="speech")
        except NotConnected:
            return False
        return True

    def _schedule_repause(self) -> None:
        loop = self._event_loop()
        if loop is None:
            return
        if self._repause_task is not None:
            self._repause_task.cancel()
        self._repause_task = loop.create_task(self._repause_when_silent())

    async def _repause_when_silent(self) -> None:
        try:
            while self._mixer is not None and self._mixer.speech_pending:
                await asyncio.sleep(0.1)
            if self._mixer is not None and self._paused and self._voice is not None:
                self._voice.pause(True)
        except asyncio.CancelledError:
            raise
        finally:
            self._repause_task = None

    # ------------------------------------------------------------------
    # Idle handling + state
    # ------------------------------------------------------------------

    def _event_loop(self) -> asyncio.AbstractEventLoop | None:
        if self._loop is not None:
            return self._loop
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return None

    def _cancel_idle(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
            self._idle_task = None

    def _schedule_idle(self) -> None:
        if self.config.idle_disconnect <= 0:
            return
        loop = self._event_loop()
        if loop is None:
            return
        self._cancel_idle()
        self._idle_task = loop.create_task(self._idle_watchdog())

    async def _idle_watchdog(self) -> None:
        try:
            await asyncio.sleep(self.config.idle_disconnect)
            if self._current is None and self._mixer is None and self._speech_mixer is None:
                log.info("Music idle in guild %s — leaving", self.guild_id)
                await self.leave()
        except asyncio.CancelledError:
            raise
        finally:
            self._idle_task = None

    @property
    def state(self) -> PlayerState:
        mixer = self._mixer
        return PlayerState(
            guild_id=self.guild_id,
            channel_id=self.channel_id,
            current=self._current,
            position=mixer.position if mixer is not None else 0.0,
            volume=self._volume,
            paused=self._paused,
            playing=mixer is not None,
            speaking=bool(mixer is not None and (mixer.speech_pending or mixer.ducked)),
            loop=self._loop_mode,
            upcoming=self.queue.snapshot(),
        )

    @property
    def current(self) -> Track | None:
        return self._current


class MusicService:
    """Bot-wide music: a player per guild, one resolver, one config."""

    def __init__(
        self,
        bot,
        *,
        config: MusicConfig | None = None,
        resolver: TrackResolver | None = None,
        stream_factory: Callable[[str], PcmStream] | None = None,
    ) -> None:
        self.bot = bot
        raw = config or getattr(getattr(bot, "config", None), "music", None)
        self.config: MusicConfig = raw if isinstance(raw, MusicConfig) else MusicConfig()
        self.resolver: TrackResolver = resolver or YtDlpResolver(
            search_prefix=self.config.search_source,
            timeout=self.config.search_timeout,
            cookies_file=self.config.cookies_file,
            proxy=self.config.proxy,
        )
        self.stream_factory: Callable[[str], PcmStream] = (
            stream_factory or _default_stream
        )
        self._players: dict[int, GuildPlayer] = {}
        log.info(
            "Music ready — %s · music %d%% · duck %s",
            getattr(self.resolver, "describe", lambda: "resolver")(),
            round(self.config.volume * 100),
            self.config.duck_label,
        )

    def open_stream(self, url: str) -> PcmStream:
        """Open a decodable PCM stream for *url* (injectable for tests)."""
        return self.stream_factory(url)

    @property
    def available(self) -> bool:
        return bool(
            self.config.enabled and getattr(self.resolver, "available", True)
        )

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def player(self, guild_id: int) -> GuildPlayer:
        player = self._players.get(guild_id)
        if player is None:
            player = GuildPlayer(self, guild_id)
            self._players[guild_id] = player
        return player

    def existing(self, guild_id: int) -> GuildPlayer | None:
        return self._players.get(guild_id)

    def is_playing(self, guild_id: int) -> bool:
        player = self._players.get(guild_id)
        return bool(player is not None and player.playing_music)

    async def play(
        self,
        guild,
        channel,
        query: str,
        *,
        requester_id: int = 0,
        requester_name: str = "",
    ) -> list[Track]:
        if not self.config.enabled:
            raise MusicError("music is switched off on this bot.")
        if not getattr(self.resolver, "available", True):
            raise MusicError(
                "music needs **yt-dlp** installed (`pip install yt-dlp`). "
                "the owner has to do that once."
            )
        player = self.player(guild.id)
        await player.connect(channel)
        return await player.play_query(
            query, requester_id=requester_id, requester_name=requester_name
        )

    async def speak(self, guild_id: int, pcm: bytes) -> bool:
        """Mix speech over whatever this guild is playing (if anything)."""
        player = self._players.get(guild_id)
        if player is None:
            return False
        return await player.speak(pcm)

    async def close(self) -> None:
        players = list(self._players.values())
        self._players.clear()
        for player in players:
            try:
                await player.leave()
            except Exception:  # noqa: BLE001 - shutdown is best effort
                log.debug("could not leave on shutdown", exc_info=True)


def _default_stream(url: str) -> PcmStream:
    return PcmStream(url, executable=resolve_ffmpeg())


__all__ = [
    "GuildPlayer",
    "MusicError",
    "MusicService",
    "NotConnected",
    "FRAME_SECONDS",
]
