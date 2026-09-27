"""Paradise's voice: text-to-speech in voice channels.

Design:
- Provider-based synthesis (default: free Microsoft Edge neural voices with
  natural Hindi / Indian-English voices). Providers are replaceable.
- Playback goes through Lavalink: the clip is uploaded as a Discord
  attachment and played via the node's http source. Music is paused and
  resumed around each spoken line.
- Strict limits: per-user + global cooldowns, short messages, one queue
  per guild, bots ignored, never speaks unless connected to that channel.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from collections import deque
from dataclasses import dataclass, field

import discord

from functions.helpers import (
    MusicError,
    NodePlaybackUnsupported,
    TTSError,
    can_bypass_voice_check,
    clean_for_speech,
    is_hindi,
    same_voice_channel,
)

log = logging.getLogger(__name__)

QUEUE_CAP = 5
HTTP_PROBE_RETRY_SECONDS = 30 * 60


class TTSProvider:
    """Minimal provider interface — swap implementations freely."""

    name = "none"

    async def synthesize(self, text: str, voice: str) -> bytes | None:
        raise NotImplementedError


class EdgeTTSProvider(TTSProvider):
    """Free neural voices via edge-tts. Needs outbound internet."""

    name = "edge-tts"

    async def synthesize(self, text: str, voice: str) -> bytes | None:
        try:
            import edge_tts
        except ImportError:
            log.error("edge-tts is not installed; TTS unavailable")
            return None
        try:
            communicate = edge_tts.Communicate(text, voice)
            buffer = bytearray()
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    buffer.extend(chunk["data"])
            return bytes(buffer) or None
        except Exception:
            log.exception("edge-tts synthesis failed")
            return None


@dataclass(slots=True)
class TTSItem:
    guild_id: int
    user_id: int
    text: str
    channel: discord.abc.Messageable | None = None
    interaction: discord.Interaction | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    error: str | None = None
    upload_message: discord.Message | None = None


class TTSService:
    def __init__(self, bot, config, music, settings):
        self.bot = bot
        self.config = config
        self.music = music
        self.settings = settings
        self.provider: TTSProvider = EdgeTTSProvider()
        self._queues: dict[int, deque[TTSItem]] = {}
        self._workers: dict[int, asyncio.Task] = {}
        self._user_last: dict[tuple[int, int], float] = {}
        self._guild_last: dict[int, float] = {}
        self._http_blocked_until: float = 0.0
        self._shutting_down = False

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    def status(self) -> str:
        if not self.config.tts_enabled:
            return "off"
        if not self.config.music_configured or not self.music.configured:
            return "off (no music node)"
        if time.monotonic() < self._http_blocked_until:
            return "unavailable on node"
        return "ready" if self.music.node_ready else "node down"

    def _http_supported(self) -> bool:
        return time.monotonic() >= self._http_blocked_until

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(self, guild: discord.Guild, member: discord.Member, raw_text: str) -> str:
        """Run every limit check. Returns cleaned text or raises TTSError.

        Shared by the natural chat trigger and /say so both paths obey the
        exact same rules.
        """
        if not self.config.tts_enabled:
            raise TTSError("TTS is switched off.")
        if not self.config.music_configured or not self.music.configured:
            raise TTSError("TTS needs a music node, and none is configured.")
        if not self.music.node_ready:
            raise TTSError("can't reach the music node right now, so I can't speak.")
        if not self._http_supported():
            raise TTSError("TTS isn't available on the music node right now.")

        player = self.music.player(guild.id)
        if player is None or not player.is_connected or guild.voice_client is None:
            raise TTSError("I'm not in a voice channel here. use /join first.")
        if not same_voice_channel(member, player.channel_id) and not can_bypass_voice_check(
            member
        ):
            raise TTSError("join my voice channel first — I only speak for people in there.")

        text = clean_for_speech(raw_text, self.config.tts_max_length + 80)
        if not text:
            raise TTSError("there's nothing in there I can actually say.")
        if len(text) > self.config.tts_max_length:
            raise TTSError(
                f"keep it under {self.config.tts_max_length} characters — "
                f"that one is {len(text)}."
            )

        now = time.monotonic()
        settings = self.settings.get(guild.id)
        user_key = (guild.id, member.id)
        user_wait = self._user_last.get(user_key, 0) + settings.tts_cooldown - now
        if user_wait > 0:
            raise TTSError(f"easy — {user_wait:.0f}s before I speak for you again.")
        guild_wait = self._guild_last.get(guild.id, 0) + self.config.tts_global_cooldown - now
        if guild_wait > 0:
            raise TTSError(f"I'm already talking. {guild_wait:.0f}s.")

        if len(self._queues.get(guild.id, ())) >= QUEUE_CAP:
            raise TTSError("the speech queue is full. let me catch my breath.")
        return text

    async def say(
        self,
        guild: discord.Guild,
        member: discord.Member,
        raw_text: str,
        *,
        channel: discord.abc.Messageable | None = None,
        interaction: discord.Interaction | None = None,
    ) -> TTSItem:
        """Validate and enqueue a spoken line. Raises TTSError on any limit."""
        text = self.check(guild, member, raw_text)
        now = time.monotonic()
        self._user_last[(guild.id, member.id)] = now
        self._guild_last[guild.id] = now

        item = TTSItem(
            guild_id=guild.id,
            user_id=member.id,
            text=text,
            channel=channel,
            interaction=interaction,
        )
        queue = self._queues.setdefault(guild.id, deque())
        queue.append(item)
        self._ensure_worker(guild.id)
        return item

    async def cancel(self, guild_id: int) -> None:
        """Drop pending speech for a guild (stop / leave / disconnect)."""
        queue = self._queues.pop(guild_id, None)
        worker = self._workers.pop(guild_id, None)
        if worker is not None and not worker.done():
            worker.cancel()
        if queue:
            for item in queue:
                item.error = "cancelled"
                item.done.set()
        self.music.abort_tts(guild_id)

    async def shutdown(self) -> None:
        self._shutting_down = True
        for guild_id in list(self._workers):
            await self.cancel(guild_id)

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    def _ensure_worker(self, guild_id: int) -> None:
        worker = self._workers.get(guild_id)
        if worker is None or worker.done():
            self._workers[guild_id] = asyncio.create_task(
                self._worker(guild_id), name=f"paradise-tts-{guild_id}"
            )

    async def _worker(self, guild_id: int) -> None:
        try:
            while True:
                queue = self._queues.get(guild_id)
                if not queue:
                    return
                item = queue.popleft()
                try:
                    await self._speak(item)
                except asyncio.CancelledError:
                    item.error = item.error or "cancelled"
                    raise
                except Exception:
                    log.exception("TTS worker crashed on an item")
                    item.error = "that didn't work on my end."
                    self._tell_user(item)
                finally:
                    item.done.set()
                    await self._shielded_cleanup(item)
        except asyncio.CancelledError:
            queue = self._queues.pop(guild_id, None)
            if queue:
                for pending in queue:
                    pending.error = "cancelled"
                    pending.done.set()
            raise

    async def _shielded_cleanup(self, item: TTSItem) -> None:
        """Delete the upload message even when the worker is being cancelled."""
        try:
            await asyncio.shield(self._cleanup_upload(item))
        except (asyncio.CancelledError, Exception):  # noqa: B014
            pass

    async def _speak(self, item: TTSItem) -> None:
        guild = self.bot.get_guild(item.guild_id)
        if guild is None:
            item.error = "I can't find that server anymore."
            self._tell_user(item)
            return

        voice = self.config.tts_voice_hi if is_hindi(item.text) else self.config.tts_voice_en
        audio = await self.provider.synthesize(item.text, voice)
        if not audio:
            item.error = "couldn't generate the voice clip right now."
            self._tell_user(item)
            return

        url = await self._upload(item, audio)
        if url is None:
            item.error = "couldn't upload the voice clip."
            self._tell_user(item)
            return

        try:
            track = await self.music.play_tts(item.guild_id, url, item.text)
        except NodePlaybackUnsupported:
            # The node can't play direct links; remember for half an hour.
            self._http_blocked_until = time.monotonic() + HTTP_PROBE_RETRY_SECONDS
            log.warning("Music node has no http source — disabling TTS temporarily")
            item.error = "TTS isn't available on the music node right now."
            self._tell_user(item)
            return
        except MusicError as error:
            item.error = str(error)
            self._tell_user(item)
            return

        duration_s = max(getattr(track, "duration", 0) or 0, 1000) / 1000
        finished = await self.music.wait_for_track_end(
            item.guild_id, track, timeout=duration_s + 20
        )
        if not finished:
            item.error = "playback was interrupted."
        await self.music.finish_tts(item.guild_id)

    async def _upload(self, item: TTSItem, audio: bytes) -> str | None:
        file = discord.File(io.BytesIO(audio), filename="paradise.mp3")
        if item.interaction is not None:
            # /say: the ephemeral confirmation doubles as the file host.
            try:
                if not item.interaction.response.is_done():
                    await item.interaction.response.defer(ephemeral=True)
                message = await item.interaction.followup.send(
                    f"🔊 saying: *{item.text[:80]}*", file=file, ephemeral=True
                )
                item.upload_message = None
                return message.attachments[0].url if message.attachments else None
            except discord.DiscordException:
                log.debug("Ephemeral TTS upload failed", exc_info=True)
                # fall through to channel upload
        channel = item.channel
        if channel is None:
            return None
        try:
            item.upload_message = await channel.send(
                f"🔊 *{item.text[:80]}", file=file
            )
        except discord.DiscordException:
            log.debug("TTS channel upload failed", exc_info=True)
            return None
        return item.upload_message.attachments[0].url if item.upload_message.attachments else None

    async def _cleanup_upload(self, item: TTSItem) -> None:
        if item.upload_message is None:
            return
        try:
            await item.upload_message.delete()
        except discord.DiscordException:
            pass
        item.upload_message = None

    def _tell_user(self, item: TTSItem) -> None:
        """Surface a worker-stage failure without spamming the channel."""
        if not item.error:
            return
        if item.interaction is not None:
            try:
                if not item.interaction.response.is_done():
                    asyncio.ensure_future(
                        item.interaction.response.send_message(item.error, ephemeral=True)
                    )
                    return
                asyncio.ensure_future(
                    item.interaction.followup.send(item.error, ephemeral=True)
                )
            except discord.DiscordException:
                pass
        elif item.channel is not None:
            async def _note() -> None:
                try:
                    await item.channel.send(item.error, delete_after=15)  # type: ignore[union-attr]
                except discord.DiscordException:
                    pass

            asyncio.ensure_future(_note())
