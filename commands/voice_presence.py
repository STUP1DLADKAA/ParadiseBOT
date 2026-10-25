"""Keep Paradise in the busiest accessible human voice channel and announce arrival."""

from __future__ import annotations

import asyncio
import io
import logging

import discord
from discord.ext import commands, tasks

from config import DictionaryConfig
from functions.dictionary.http import HttpClient, HttpError
from functions.dictionary.models import Clip, SpeechRequest
from functions.dictionary.playback import (
    LINGER_SECONDS,
    playback_support,
    resolve_ffmpeg_executable,
)
from functions.dictionary.pronounce import NeuralSpeechProvider, WebVoiceProvider

log = logging.getLogger(__name__)

ARRIVAL_TEXT = "The GOAT has arrived 🐐"
ARRIVAL_SPEECH_TEXT = "The goat has arrived"

CONNECT_TIMEOUT = 10.0
HANDSHAKE_WAIT_SECONDS = 10.0
SETTLE_DELAY_SECONDS = 0.4
MAX_CLIP_SECONDS = 15.0


class VoicePresence(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        *,
        settle_delay: float = SETTLE_DELAY_SECONDS,
        speech_providers: list | None = None,
    ) -> None:
        self.bot = bot
        self.settle_delay = max(0.0, float(settle_delay))
        self._http = HttpClient()
        if speech_providers is None:
            dict_cfg = (
                getattr(getattr(bot, "config", None), "dictionary", None)
                or DictionaryConfig()
            )
            self._speech_providers = [
                NeuralSpeechProvider(
                    self._http,
                    base_url=dict_cfg.speech_base_url,
                    api_key=dict_cfg.speech_api_key,
                    model=dict_cfg.speech_model,
                    voice=dict_cfg.speech_voice,
                    timeout=dict_cfg.timeout,
                ),
                WebVoiceProvider(self._http, timeout=dict_cfg.timeout),
            ]
        else:
            self._speech_providers = list(speech_providers)

        self._cached_clip: Clip | None = None
        self._clip_lock = asyncio.Lock()
        self._guild_locks: dict[int, asyncio.Lock] = {}
        self._last_announced_channel: dict[int, int] = {}
        self._tasks: set[asyncio.Task] = set()

    async def cog_load(self) -> None:
        self.follow_crowd.start()

    async def cog_unload(self) -> None:
        self.follow_crowd.cancel()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self._http.close()

    def _guild_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._guild_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._guild_locks[guild_id] = lock
        return lock

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ------------------------------------------------------------------
    # Periodic crowd-following loop + reactive voice listener
    # ------------------------------------------------------------------

    @tasks.loop(seconds=30)
    async def follow_crowd(self) -> None:
        for guild in self.bot.guilds:
            try:
                await self.join_busiest(guild, disconnect_when_empty=True)
            except (discord.DiscordException, RuntimeError, TimeoutError):
                log.warning("Could not join busiest VC in %s", guild.id, exc_info=True)

    @follow_crowd.before_loop
    async def before_follow(self) -> None:
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:
            # Offline test harnesses load cogs without logging the client in.
            self.follow_crowd.cancel()

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        guild = getattr(member, "guild", None)
        if guild is None:
            return

        before_id = getattr(before.channel, "id", None)
        after_id = getattr(after.channel, "id", None)

        # 1. Paradise itself joined, switched, or left a voice channel
        if self.bot.user is not None and member.id == self.bot.user.id:
            if after.channel is None:
                self._last_announced_channel.pop(guild.id, None)
                return
            if before_id != after_id:
                self._spawn(self._handle_bot_arrival(guild, after.channel))
            return

        # 2. A human joined or switched voice channels — follow promptly
        if not member.bot and after.channel is not None and before_id != after_id:
            self._spawn(self._safe_join_busiest(guild))

    async def _safe_join_busiest(self, guild: discord.Guild) -> None:
        try:
            await self.join_busiest(guild)
        except asyncio.CancelledError:
            raise
        except (discord.DiscordException, RuntimeError, TimeoutError):
            log.warning("Could not join busiest VC in %s", guild.id, exc_info=True)

    async def _handle_bot_arrival(
        self,
        guild: discord.Guild,
        channel: discord.VoiceChannel | discord.StageChannel,
    ) -> None:
        """Announce arrival when Paradise is moved or connected outside join_busiest."""
        try:
            async with self._guild_lock(guild.id):
                if self._last_announced_channel.get(guild.id) == channel.id:
                    return
                await self._wait_for_voice_ready(guild, channel.id)
                client = guild.voice_client
                if (
                    client is None
                    or not client.is_connected()
                    or getattr(client.channel, "id", None) != channel.id
                ):
                    return
                await self._announce_unlocked(guild, channel)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.warning("Could not announce bot VC arrival in %s", channel.id, exc_info=True)

    async def _wait_for_voice_ready(self, guild: discord.Guild, channel_id: int) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + HANDSHAKE_WAIT_SECONDS
        while loop.time() < deadline:
            client = guild.voice_client
            if (
                client is not None
                and client.is_connected()
                and getattr(client.channel, "id", None) == channel_id
            ):
                return
            await asyncio.sleep(0.1)

    async def join_busiest(
        self,
        guild: discord.Guild,
        *,
        disconnect_when_empty: bool = False,
    ) -> None:
        async with self._guild_lock(guild.id):
            candidates = []
            client = guild.voice_client
            for channel in guild.voice_channels:
                permissions = channel.permissions_for(guild.me)
                humans = sum(not member.bot for member in channel.members)
                if (
                    humans
                    and channel != guild.afk_channel
                    and permissions.view_channel
                    and permissions.connect
                    and (
                        not channel.user_limit
                        or len(channel.members) < channel.user_limit
                        or permissions.move_members
                        or (client and client.channel == channel)
                    )
                ):
                    candidates.append((humans, channel))

            if not candidates:
                if (
                    disconnect_when_empty
                    and client
                    and client.is_connected()
                    and not client.is_playing()
                ):
                    self._last_announced_channel.pop(guild.id, None)
                    await client.disconnect()
                return

            # Keep the current channel when tied, and never interrupt playback.
            if client and client.is_playing():
                return

            _, target = max(
                candidates,
                key=lambda item: (
                    item[0],
                    bool(client and client.channel == item[1]),
                    -item[1].id,
                ),
            )
            if client and client.is_connected():
                if client.channel == target:
                    return
                await client.move_to(target)
            else:
                await target.connect(timeout=CONNECT_TIMEOUT, self_deaf=True)

            await self._announce_unlocked(guild, target)

    # ------------------------------------------------------------------
    # VC arrival text + voice announcement
    # ------------------------------------------------------------------

    async def _announce_unlocked(
        self,
        guild: discord.Guild,
        channel: discord.VoiceChannel | discord.StageChannel,
    ) -> None:
        if self._last_announced_channel.get(guild.id) == channel.id:
            return
        self._last_announced_channel[guild.id] = channel.id

        try:
            await channel.send(
                ARRIVAL_TEXT,
                delete_after=120,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.DiscordException:
            log.warning("Could not send VC arrival message in %s", channel.id, exc_info=True)

        try:
            await self._speak_arrival(guild, channel)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.warning("Could not speak VC arrival in %s", channel.id, exc_info=True)

    async def _get_arrival_clip(self) -> Clip | None:
        if self._cached_clip is not None:
            return self._cached_clip

        async with self._clip_lock:
            if self._cached_clip is not None:
                return self._cached_clip

            request = SpeechRequest(word=ARRIVAL_SPEECH_TEXT, language="en")
            for provider in self._speech_providers:
                if not getattr(provider, "available", False):
                    continue
                try:
                    clip = await provider.speak(request)
                except asyncio.CancelledError:
                    raise
                except HttpError as err:
                    log.debug("Arrival TTS provider %s failed: %s", provider.name, err)
                    continue
                except Exception:  # noqa: BLE001
                    log.warning("Arrival TTS provider %s failed", provider.name, exc_info=True)
                    continue
                if clip is not None and clip.data:
                    self._cached_clip = clip
                    return clip
        return None

    def _create_audio_source(self, clip: Clip, ffmpeg_exe: str) -> discord.AudioSource:
        return discord.FFmpegOpusAudio(
            io.BytesIO(clip.data),
            pipe=True,
            executable=ffmpeg_exe,
            options="-loglevel error",
        )

    async def _speak_arrival(
        self,
        guild: discord.Guild,
        channel: discord.VoiceChannel | discord.StageChannel,
    ) -> None:
        if not playback_support():
            log.debug("Skipping VC arrival speech: voice playback unsupported")
            return

        me = getattr(guild, "me", None)
        if me is not None:
            permissions = channel.permissions_for(me)
            if getattr(permissions, "speak", True) is False:
                return
            me_voice = getattr(me, "voice", None)
            if me_voice is not None and getattr(me_voice, "mute", False) is True:
                return

        clip = await self._get_arrival_clip()
        if clip is None:
            return

        if self.settle_delay > 0:
            await asyncio.sleep(self.settle_delay)

        client = guild.voice_client
        if (
            client is None
            or not client.is_connected()
            or getattr(client.channel, "id", None) != channel.id
            or client.is_playing()
        ):
            return

        ffmpeg_exe = resolve_ffmpeg_executable() or "ffmpeg"
        source = self._create_audio_source(clip, ffmpeg_exe)
        finished = asyncio.Event()
        loop = asyncio.get_running_loop()

        def done(error: Exception | None) -> None:
            if error is not None:
                log.debug("VC arrival audio ended with an error: %s", error)
            loop.call_soon_threadsafe(finished.set)

        client.play(source, after=done)
        try:
            await asyncio.wait_for(finished.wait(), timeout=MAX_CLIP_SECONDS)
            if self.settle_delay > 0:
                await asyncio.sleep(LINGER_SECONDS)
        except asyncio.TimeoutError:
            log.debug("VC arrival playback timed out; stopping")
        finally:
            try:
                if client.is_playing():
                    client.pause()
                    client.cleanup()
            except Exception:  # noqa: BLE001
                log.debug("Voice client cleanup failed", exc_info=True)
            try:
                stdout = getattr(source, "_stdout", None)
                if stdout is not None and hasattr(stdout, "close"):
                    stdout.close()
                source.cleanup()
            except Exception:  # noqa: BLE001
                log.debug("FFmpeg source cleanup failed", exc_info=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VoicePresence(bot))
