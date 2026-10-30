"""High-level orchestration for rolling voice capture, export and DM delivery."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import logging
import os
import threading
import time

import discord

from config import VoiceClipConfig

from .buffer import ClipIntervalSnapshot, RollingAudioBuffer
from .constants import (
    AUTHORIZED_CLIP_USER_ID,
    ClipAuthorizationError,
    ClipBusyError,
    ClipDeliveryError,
    ClipFileTooLargeError,
    ClipUnavailableError,
    UNAUTHORIZED_CLIP_MESSAGE,
    authorize_clip_user,
    require_clip_authorization,
    validate_clip_duration,
)
from .exporter import ClipExportPipeline, ExportedClip
from .receiver import VoiceReceiveRouter

log = logging.getLogger(__name__)

DM_UPLOAD_TIMEOUT_SECONDS: float = 25.0


@dataclass(frozen=True, slots=True)
class ClipDeliveryReport:
    """Summary of a completed clip export and private DM delivery."""

    guild_id: int
    channel_id: int
    channel_name: str
    requested_seconds: int
    captured_seconds: float
    speaker_count: int
    filename: str
    file_size_bytes: int

    @property
    def partial_window(self) -> bool:
        return self.captured_seconds + 0.25 < float(self.requested_seconds)


def format_clip_dm_text(
    *,
    guild_name: str,
    channel_name: str,
    requested_seconds: int,
    captured_seconds: float,
    speaker_count: int,
) -> str:
    """Format the private DM message accompanying a voice clip attachment."""
    safe_guild = discord.utils.escape_markdown(guild_name or "Server")
    safe_channel = discord.utils.escape_markdown(channel_name or "Voice")
    captured_label = f"{captured_seconds:.1f}s"
    lines = [
        f"🎙️ **Voice Clip — {safe_channel}** ({safe_guild})",
        (
            f"• **Requested duration:** `{requested_seconds}s`\n"
            f"• **Actual captured duration:** `{captured_label}`\n"
            f"• **Captured speakers:** `{speaker_count}`"
        ),
    ]
    if captured_seconds + 0.25 < float(requested_seconds):
        lines.append(
            f"-# Only `{captured_label}` was available because recording in this "
            f"channel started `{captured_label}` ago."
        )
    return "\n".join(lines)


class VoiceClipService:
    """Coordinates per-guild rolling voice buffers, export and DM delivery.

    Capture is unconditional by design: this is a private deployment, so every
    human speaker in the channel is buffered. There is no opt-in, no opt-out
    and no notice posted in the channel.
    """

    def __init__(
        self,
        bot: discord.Client,
        config: VoiceClipConfig | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.bot = bot
        self.config = config or VoiceClipConfig()
        self._clock = clock
        self._lock = threading.Lock()
        self._buffers: dict[int, RollingAudioBuffer] = {}
        self._routers: dict[int, VoiceReceiveRouter] = {}
        #: Observers of accepted packets (the spoken wake word listens here).
        #: Shared list, read on the UDP thread, guarded by ``_lock`` on write.
        self._taps: list = []

        self.exporter = ClipExportPipeline(
            max_concurrent_exports=self.config.max_concurrent_exports,
            max_queued_exports=self.config.max_queued_exports,
            export_timeout_seconds=self.config.export_timeout_seconds,
            max_file_bytes=self.config.max_file_bytes,
            max_temp_disk_bytes=self.config.max_temp_disk_bytes,
            bitrate_kbps=self.config.bitrate_kbps,
            audio_filter=getattr(self.config, "audio_filter", ""),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    @property
    def receiving_wanted(self) -> bool:
        """Should voice connections use the receive-capable client?

        The receiver is what makes a spoken wake word possible, so it is
        needed when *either* the clip buffer or voice listening is on.
        """
        wake = getattr(getattr(self.bot, "config", None), "voice_wake", None)
        return bool(self.config.enabled or getattr(wake, "listening", False))

    # ------------------------------------------------------------------
    # Packet taps (spoken wake word)
    # ------------------------------------------------------------------

    def add_tap(self, tap) -> None:
        """Register an observer called with ``(guild_id, user_id, packet)``."""
        with self._lock:
            if tap not in self._taps:
                self._taps.append(tap)

    def remove_tap(self, tap) -> None:
        with self._lock:
            try:
                self._taps.remove(tap)
            except ValueError:
                pass

    def _fan_out(self, guild_id: int, user_id: int, packet) -> None:
        taps = list(self._taps)
        for tap in taps:
            try:
                tap(guild_id, user_id, packet)
            except Exception:  # noqa: BLE001 - an observer must not break audio
                log.debug("voice clip tap failed", exc_info=True)

    # ------------------------------------------------------------------
    # Buffer & Receiver Attachment
    # ------------------------------------------------------------------

    def get_or_create_buffer(self, guild_id: int) -> RollingAudioBuffer:
        gid = int(guild_id)
        with self._lock:
            buf = self._buffers.get(gid)
            if buf is None:
                buf = RollingAudioBuffer(
                    gid,
                    max_seconds=float(self.config.max_buffer_seconds),
                    max_active_speakers=self.config.max_active_speakers,
                    max_buffer_bytes=self.config.max_buffer_bytes,
                    clock=self._clock,
                )
                self._buffers[gid] = buf
            return buf

    def get_buffer(self, guild_id: int) -> RollingAudioBuffer | None:
        with self._lock:
            return self._buffers.get(int(guild_id))

    def attach_voice_client(
        self,
        *,
        guild: discord.Guild,
        channel_id: int,
        voice_client: discord.VoiceClient,
    ) -> VoiceReceiveRouter:
        """Start/switch rolling capture for ``guild`` on ``channel_id``."""
        gid = int(guild.id)
        cid = int(channel_id)
        buf = self.get_or_create_buffer(gid)
        # Switching channels automatically clears the old channel's audio.
        buf.start_session(cid)

        def _is_bot(uid: int) -> bool:
            bot_user = getattr(self.bot, "user", None)
            if bot_user is not None and uid == bot_user.id:
                return True
            member = guild.get_member(uid)
            return bool(member is not None and getattr(member, "bot", False))

        def _dave_state():
            conn = getattr(voice_client, "_connection", None)
            if conn is None:
                return None, 0
            return getattr(conn, "dave_session", None), int(
                getattr(conn, "dave_protocol_version", 0) or 0
            )

        with self._lock:
            old_router = self._routers.pop(gid, None)
            if old_router is not None:
                old_router.reset()
            gid_for_tap = gid

            def tap(user_id: int, packet, _gid: int = gid_for_tap) -> None:
                self._fan_out(_gid, user_id, packet)

            router = VoiceReceiveRouter(
                guild_id=gid,
                buffer=buf,
                is_bot_user=_is_bot,
                dave_state_getter=_dave_state,
                clock=self._clock,
                taps=[tap],
            )
            self._routers[gid] = router
        return router

    def detach_guild(self, guild_id: int) -> None:
        """Stop capture and immediately wipe all buffered voice packets for ``guild_id``."""
        gid = int(guild_id)
        with self._lock:
            router = self._routers.pop(gid, None)
            buf = self._buffers.pop(gid, None)
        if router is not None:
            router.reset()
        if buf is not None:
            buf.clear()

    def handle_speaker_leave(self, guild_id: int, user_id: int) -> None:
        """Mark speaker stream discontinuity when a member leaves or switches VC."""
        gid = int(guild_id)
        with self._lock:
            router = self._routers.get(gid)
            buf = self._buffers.get(gid)
        if router is not None:
            router.remove_user(int(user_id))
        elif buf is not None:
            buf.mark_speaker_discontinuity(int(user_id))

    def receiver_stats(self, guild_id: int) -> dict[str, int]:
        """Packets the receiver has seen for ``guild_id`` (never audio).

        ``received`` counts every UDP frame that decrypted; ``heard`` counts the
        ones that were handed to listeners. Zero ``received`` with the bot in a
        channel means Discord is not sending anything at all — muted, deafened
        or not actually connected — which is worth being able to see from
        ``/voicewake status`` instead of guessing.
        """
        with self._lock:
            router = self._routers.get(int(guild_id))
        if router is None:
            return {"received": 0, "buffered": 0, "transport_errors": 0, "dave_errors": 0}
        return {
            "received": int(router.packets_received),
            "buffered": int(router.packets_buffered),
            "transport_errors": int(router.transport_errors),
            "dave_errors": int(router.dave_errors),
        }

    def evict_all_expired(self) -> int:
        with self._lock:
            buffers = list(self._buffers.values())
        return sum(buf.evict_expired() for buf in buffers)

    # ------------------------------------------------------------------
    # Authorized Export & Private DM Delivery
    # ------------------------------------------------------------------

    def preflight_check(
        self,
        *,
        requester_user_id: object,
        duration: object,
        guild: discord.Guild | None,
    ) -> tuple[int, discord.abc.GuildChannel]:
        """Fast synchronous check run BEFORE deferring or touching audio."""
        require_clip_authorization(requester_user_id)
        valid_duration = validate_clip_duration(duration)

        if not self.enabled:
            raise ClipUnavailableError("Voice clipping is currently disabled.")

        if guild is None:
            raise ClipUnavailableError("Run `/clip` inside a server with an active voice channel.")

        vc = getattr(guild, "voice_client", None)
        if vc is None or not vc.is_connected() or getattr(vc, "channel", None) is None:
            raise ClipUnavailableError("I am not connected to a voice channel in this server.")

        channel = vc.channel
        me = getattr(guild, "me", None)
        if me is not None and hasattr(channel, "permissions_for"):
            perms = channel.permissions_for(me)
            if not getattr(perms, "view_channel", True) or not getattr(perms, "connect", True):
                raise ClipUnavailableError(
                    "I do not have permission to access that voice channel."
                )
            me_voice = getattr(me, "voice", None)
            if me_voice is not None and (
                getattr(me_voice, "self_deaf", False) or getattr(me_voice, "deaf", False)
            ):
                raise ClipUnavailableError(
                    "voice capture is unavailable while i'm deafened — Discord "
                    "doesn't send audio to a deafened client, and i can't "
                    "override a moderator's deafen. Un-deafen me and try again."
                )

        buf = self.get_buffer(guild.id)
        if buf is None or not buf.is_recording or buf.channel_id != getattr(channel, "id", None):
            raise ClipUnavailableError(
                "Voice recording is not currently active in this voice channel."
            )

        if self.exporter.inflight_count >= self.exporter.max_total_inflight:
            raise ClipBusyError(
                "Clip export is currently busy processing earlier requests. "
                "Please try again in a few seconds."
            )

        return valid_duration, channel

    async def export_clip_for_guild(
        self,
        *,
        requester_user_id: object,
        guild: discord.Guild,
        duration: object,
    ) -> tuple[ExportedClip, discord.abc.GuildChannel]:
        """Authorized internal entry point: snapshot and encode a clip."""
        valid_duration, channel = self.preflight_check(
            requester_user_id=requester_user_id,
            duration=duration,
            guild=guild,
        )
        buf = self.get_buffer(guild.id)
        if buf is None:
            raise ClipUnavailableError("Voice recording is not active in this server.")

        snapshot: ClipIntervalSnapshot = buf.snapshot(valid_duration)
        exported = await self.exporter.export(snapshot)
        return exported, channel

    async def create_and_deliver_clip(
        self,
        *,
        requester: discord.abc.User,
        guild: discord.Guild | None,
        duration: object,
    ) -> ClipDeliveryReport:
        """Authorize, export, deliver via DM to the sole authorized user, and clean up."""
        requester_id = getattr(requester, "id", None)
        if not authorize_clip_user(requester_id):
            log.warning("Rejected unauthorized clip request (user_id=%s)", requester_id)
            raise ClipAuthorizationError(UNAUTHORIZED_CLIP_MESSAGE)

        assert guild is not None or True  # validated in preflight_check
        valid_duration, channel = self.preflight_check(
            requester_user_id=requester_id,
            duration=duration,
            guild=guild,
        )
        assert guild is not None

        buf = self.get_buffer(guild.id)
        if buf is None:
            raise ClipUnavailableError("Voice recording is not active in this server.")

        snapshot = buf.snapshot(valid_duration)
        clip = await self.exporter.export(snapshot)
        try:
            await self._deliver_dm(
                recipient=requester,
                clip=clip,
                guild_name=getattr(guild, "name", "Server"),
                channel_name=getattr(channel, "name", "Voice"),
            )
            return ClipDeliveryReport(
                guild_id=guild.id,
                channel_id=channel.id,
                channel_name=getattr(channel, "name", "Voice"),
                requested_seconds=clip.requested_seconds,
                captured_seconds=clip.captured_seconds,
                speaker_count=clip.speaker_count,
                filename=clip.filename,
                file_size_bytes=clip.file_size_bytes,
            )
        finally:
            clip.cleanup()

    async def _deliver_dm(
        self,
        *,
        recipient: discord.abc.User,
        clip: ExportedClip,
        guild_name: str,
        channel_name: str,
    ) -> None:
        """Send validated clip file exclusively to AUTHORIZED_CLIP_USER_ID via DM."""
        recipient_id = getattr(recipient, "id", None)
        if not authorize_clip_user(recipient_id) or recipient_id != AUTHORIZED_CLIP_USER_ID:
            raise ClipAuthorizationError(UNAUTHORIZED_CLIP_MESSAGE)

        if not os.path.isfile(clip.file_path):
            raise ClipDeliveryError("Generated clip file could not be read for delivery.")

        size = os.path.getsize(clip.file_path)
        if size <= 0:
            raise ClipDeliveryError("Generated clip file was empty.")
        if size > self.config.max_file_bytes:
            raise ClipFileTooLargeError(
                "The generated clip exceeds Discord's upload size limit. "
                "Try requesting a shorter duration."
            )

        content = format_clip_dm_text(
            guild_name=guild_name,
            channel_name=channel_name,
            requested_seconds=clip.requested_seconds,
            captured_seconds=clip.captured_seconds,
            speaker_count=clip.speaker_count,
        )

        try:
            dm_channel = getattr(recipient, "dm_channel", None)
            if dm_channel is None and hasattr(recipient, "create_dm"):
                dm_channel = await asyncio.wait_for(
                    recipient.create_dm(),
                    timeout=10.0,
                )
            target = dm_channel if dm_channel is not None else recipient
            discord_file = discord.File(clip.file_path, filename=clip.filename)
            await asyncio.wait_for(
                target.send(
                    content=content,
                    file=discord_file,
                    allowed_mentions=discord.AllowedMentions.none(),
                ),
                timeout=DM_UPLOAD_TIMEOUT_SECONDS,
            )
        except discord.Forbidden as exc:
            log.info("Clip DM blocked or closed for authorized user")
            raise ClipDeliveryError(
                "Could not send the clip via DM — check that Direct Messages from "
                "server members are enabled."
            ) from exc
        except asyncio.TimeoutError as exc:
            log.warning("Clip DM upload timed out")
            raise ClipDeliveryError(
                "Uploading the clip via DM timed out. Please try again."
            ) from exc
        except discord.HTTPException as exc:
            log.warning(
                "Clip DM HTTP failure (status=%s, code=%s)",
                getattr(exc, "status", None),
                getattr(exc, "code", None),
            )
            if getattr(exc, "status", None) == 413:
                raise ClipFileTooLargeError(
                    "Discord rejected the clip because the file exceeded the upload limit. "
                    "Try a shorter duration."
                ) from exc
            raise ClipDeliveryError(
                "Discord could not deliver the clip via DM right now. Please try again."
            ) from exc

    async def shutdown(self) -> None:
        """Purge all volatile audio buffers and clean up temporary export storage."""
        with self._lock:
            routers = list(self._routers.values())
            buffers = list(self._buffers.values())
            self._routers.clear()
            self._buffers.clear()
        for router in routers:
            router.reset()
        for buf in buffers:
            buf.clear()
        await self.exporter.shutdown()
