"""Optional voice-channel playback for a pronunciation.

Paradise is not a music bot and this is not a music system: there is no
queue, no player state and no commands. This module does exactly one
thing — take a two-second clip that already exists in memory, speak it
into the channel the asker is sitting in, and get out again.

It is **off unless everything lines up**:

- ``PRONUNCIATION_VOICE_PLAYBACK=true`` in the environment, and
- PyNaCl is installed (Discord voice encryption), and
- an ``ffmpeg`` binary is on PATH, and
- the asker is actually in a voice channel Paradise may enter and speak in.

Any of those missing is not an error — :func:`playback_support` reports
why, and the cog simply attaches the audio file instead. That fallback is
the normal path for this repository, which ships no voice dependencies.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import shutil
from dataclasses import dataclass

import discord

from functions.helpers import ParadiseError

from .models import Clip

log = logging.getLogger(__name__)

CONNECT_TIMEOUT = 10.0
MAX_CLIP_SECONDS = 20.0
LINGER_SECONDS = 0.35  # let the tail of the word actually reach listeners


class PlaybackUnavailable(ParadiseError):
    """Voice playback is not possible right now. Message is user-safe."""


@dataclass(frozen=True, slots=True)
class PlaybackSupport:
    ok: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.ok


def _has_nacl() -> bool:
    try:
        import nacl  # noqa: F401
    except ImportError:
        return False
    return True


def resolve_ffmpeg_executable() -> str | None:
    """Locate an ffmpeg binary on PATH or from the bundled imageio-ffmpeg wheel."""
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg
    try:
        import imageio_ffmpeg

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled and os.path.isfile(bundled):
            return bundled
    except Exception:  # pragma: no cover - optional fallback probe
        pass
    return None


def playback_support() -> PlaybackSupport:
    """Can this machine speak into a voice channel at all?"""
    if not _has_nacl():
        return PlaybackSupport(False, "PyNaCl is not installed")
    if resolve_ffmpeg_executable() is None:
        return PlaybackSupport(False, "ffmpeg is not on PATH")
    if not hasattr(discord, "FFmpegOpusAudio"):  # pragma: no cover - ancient lib
        return PlaybackSupport(False, "this discord.py build has no voice support")
    return PlaybackSupport(True)


class VoicePlayback:
    """Serialised, self-cleaning playback of single pronunciation clips."""

    def __init__(self, *, enabled: bool = False) -> None:
        self.enabled = bool(enabled)
        self._locks: dict[int, asyncio.Lock] = {}

    # -- capability --------------------------------------------------------

    def support(self) -> PlaybackSupport:
        if not self.enabled:
            return PlaybackSupport(False, "voice playback is switched off")
        return playback_support()

    def channel_for(self, user: object):
        """The voice channel a member is currently in, if any."""
        voice = getattr(user, "voice", None)
        channel = getattr(voice, "channel", None)
        if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            return channel
        return None

    def _lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[guild_id] = lock
            if len(self._locks) > 64:  # pragma: no cover - tidy-up
                for key in [k for k, v in self._locks.items() if not v.locked()][:32]:
                    if key != guild_id:
                        self._locks.pop(key, None)
        return lock

    # -- the one operation -------------------------------------------------

    async def speak(self, member: discord.Member, clip: Clip) -> str:
        """Speak a clip to the member's channel. Returns the channel name."""
        support = self.support()
        if not support:
            raise PlaybackUnavailable(support.reason)

        channel = self.channel_for(member)
        if channel is None:
            raise PlaybackUnavailable("you're not in a voice channel")

        guild = getattr(member, "guild", None)
        me = getattr(guild, "me", None)
        if guild is None or me is None:
            raise PlaybackUnavailable("that only works in a server")

        permissions = channel.permissions_for(me)
        if not permissions.connect or not permissions.speak:
            raise PlaybackUnavailable("I can't speak in that voice channel")

        lock = self._lock(guild.id)
        if lock.locked():
            raise PlaybackUnavailable("I'm already saying something — one sec")

        async with lock:
            client: discord.VoiceClient | None = guild.voice_client  # type: ignore[assignment]
            connected_here = False
            try:
                if client is None or not client.is_connected():
                    client = await asyncio.wait_for(
                        channel.connect(reconnect=False, self_deaf=True),
                        timeout=CONNECT_TIMEOUT,
                    )
                    connected_here = True
                elif client.channel != channel:
                    if client.is_playing():
                        raise PlaybackUnavailable("I'm busy in another channel")
                    await client.move_to(channel)
                elif client.is_playing():
                    raise PlaybackUnavailable("I'm already saying something — one sec")

                await self._render(client, clip)
                return getattr(channel, "name", "voice")

            except PlaybackUnavailable:
                raise
            except asyncio.TimeoutError as error:
                raise PlaybackUnavailable("couldn't get into that voice channel") from error
            except discord.ClientException as error:
                log.debug("Voice playback client error: %s", error)
                raise PlaybackUnavailable("voice playback isn't working right now") from error
            except discord.DiscordException as error:
                log.warning("Voice playback failed", exc_info=True)
                raise PlaybackUnavailable("voice playback isn't working right now") from error
            finally:
                if connected_here:
                    await self._disconnect(guild)

    async def _render(self, client: discord.VoiceClient, clip: Clip) -> None:
        ffmpeg_exe = resolve_ffmpeg_executable() or "ffmpeg"
        source = discord.FFmpegOpusAudio(
            io.BytesIO(clip.data), pipe=True, executable=ffmpeg_exe
        )
        finished = asyncio.Event()
        loop = asyncio.get_running_loop()

        def done(error: Exception | None) -> None:
            if error is not None:
                log.debug("Voice source ended with an error: %s", error)
            loop.call_soon_threadsafe(finished.set)

        client.play(source, after=done)
        try:
            await asyncio.wait_for(finished.wait(), timeout=MAX_CLIP_SECONDS)
            await asyncio.sleep(LINGER_SECONDS)
        except asyncio.TimeoutError:
            log.debug("Pronunciation playback overran; cutting it short")
        finally:
            try:
                if client.is_playing():
                    client.pause()
                    client.cleanup()
            except Exception:  # noqa: BLE001 - cleanup must never raise
                log.debug("Voice source cleanup failed", exc_info=True)
            try:
                source.cleanup()
            except Exception:  # noqa: BLE001
                log.debug("FFmpeg cleanup failed", exc_info=True)

    @staticmethod
    async def _disconnect(guild: discord.Guild) -> None:
        client = guild.voice_client
        if client is None:
            return
        try:
            await asyncio.wait_for(client.disconnect(force=True), timeout=8)
        except (asyncio.TimeoutError, discord.DiscordException, RuntimeError):
            log.debug("Voice disconnect failed", exc_info=True)

    async def shutdown(self) -> None:
        """Drop every connection we own. Called when the bot closes."""
        self._locks.clear()
