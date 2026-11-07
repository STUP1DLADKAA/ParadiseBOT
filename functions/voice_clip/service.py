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

from .buffer import ClipIntervalSnapshot, NoUsableAudioError, RollingAudioBuffer
from .constants import (
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

        The receiver exists for exactly one reason now: feeding the rolling
        clip buffer. With ``VOICE_CLIP_ENABLED=false`` there is nothing to
        receive, so the bot connects with the plain client and pays for no
        decryption at all.
        """
        return bool(self.config.enabled)

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
        previous_router: VoiceReceiveRouter | None = None,
    ) -> VoiceReceiveRouter:
        """Start/switch rolling capture for ``guild`` on ``channel_id``.

        ``previous_router`` (the router bound before a rebind) has its live
        state — transport decryptor, SSRC mappings, staged packets, counters —
        inherited by the new router, because a channel move or recovery does
        **not** re-run the voice handshake: there is no new SESSION_DESCRIPTION
        and SPEAKING events only arrive when users start talking again.  A
        fresh router would silently drop every packet until a full reconnect.
        """
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
            router = VoiceReceiveRouter(
                guild_id=gid,
                buffer=buf,
                is_bot_user=_is_bot,
                dave_state_getter=_dave_state,
                clock=self._clock,
            )
            # Inherit from the caller-supplied router first (same live voice
            # connection), else from whatever was registered for this guild.
            inherit_source = previous_router or old_router
            if inherit_source is not None and inherit_source is not router:
                router.inherit_from(inherit_source)
            if old_router is not None and old_router is not inherit_source:
                old_router.reset()
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

    def clear_guild_buffer(self, guild_id: int) -> None:
        """Wipe buffered audio but KEEP the receiver and its live state.

        Used when capture must pause (e.g. while the bot is server-deafened
        and Discord sends no RTP at all) but the voice connection — and with
        it the transport decryptor and SSRC mappings — continues afterwards.
        A full ``detach_guild`` here would throw away state that takes a new
        voice handshake to rebuild, leaving capture dead after recovery.
        """
        gid = int(guild_id)
        with self._lock:
            buf = self._buffers.get(gid)
        if buf is not None:
            buf.clear()

    def buffered_guild_ids(self) -> list[int]:
        """Guilds that currently have a rolling buffer (diagnostics/sweeps)."""
        with self._lock:
            return list(self._buffers.keys())

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

        ``received`` counts every UDP frame that decrypted and ``buffered`` the
        ones that made it into the rolling buffer. Zero ``received`` with the
        bot in a channel means Discord is not sending anything at all — muted,
        deafened or not actually connected — which the ``/clip`` debug panel
        shows instead of leaving people to guess.
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

    def capture_health(self, guild_id: int) -> dict[str, int | float | bool | None]:
        """Everything the diagnosis needs for one guild — counts only.

        Deliberately splits *session* truth (what this recording session has
        actually captured, from the buffer) from *receiver* truth (what the
        UDP/decryption pipeline has seen in its lifetime, from the router).
        A router that buffered packets an hour ago is not "ok" now.
        """
        gid = int(guild_id)
        buf_stats = self.get_buffer(gid).stats() if self.get_buffer(gid) is not None else {}
        router_stats = self.receiver_stats(gid)
        with self._lock:
            router = self._routers.get(gid)
        health: dict[str, int | float | bool | None] = dict(buf_stats)
        health.update(router_stats)
        health["non_silence_buffered"] = (
            int(router.non_silence_buffered) if router is not None else 0
        )
        health["dave_fail_since_success"] = (
            int(router.dave_fail_since_success) if router is not None else 0
        )
        health["last_dave_success_age"] = (
            max(0.0, self._clock() - router.last_dave_success_mono)
            if router is not None and router.last_dave_success_mono is not None
            else None
        )
        health["receiver_armed"] = router.has_transport() if router is not None else False
        return health

    def diagnose_capture(self, guild_id: int) -> str:
        """Classify *why* a guild's rolling buffer has no usable audio.

        Returns a short, actionable, privacy-safe reason (counts only — never
        audio) for the ``/clip`` error message and the debug log.  Judged on
        the CURRENT session, not on lifetime receiver counters: "audio worked
        once" must never read as "audio works".
        """
        gid = int(guild_id)
        h = self.capture_health(gid)
        if not h.get("recording", False):
            return "capture_inactive"
        if not h.get("received"):
            return (
                "no UDP packets at all — Discord is not sending voice to me "
                "(I may be server-deafened, or the voice handshake is not "
                "complete)"
            )
        if not h.get("receiver_armed", False):
            return (
                "voice packets are arriving but the transport decryptor is "
                "not configured — the receiver was rebound without a voice "
                "handshake"
            )
        session_packets = int(h.get("session_packets") or 0)
        session_non_silence = int(h.get("session_non_silence_packets") or 0)
        session_elapsed = float(h.get("session_elapsed_seconds") or 0.0)
        if session_packets == 0 and session_elapsed < 2.0:
            return "session_just_started"
        if session_packets == 0:
            if h.get("buffered", 0) or h.get("non_silence_buffered", 0):
                last_age = h.get("last_packet_age_seconds")
                if last_age is None:
                    return (
                        "this capture session has not received a single audio "
                        "packet yet (the previous session did) — the receiver "
                        "went dark after a rebind"
                    )
                return (
                    f"no audio has arrived for the last {last_age:.0f}s — "
                    "the connection may have gone silent or capture is "
                    "degraded"
                )
            return (
                f"{h.get('received')} packets arrived but none decrypted into "
                "a mapped speaker's voice yet (waiting for users to start "
                "speaking)"
            )
        if session_non_silence == 0:
            return (
                f"only silence frames captured in this session "
                f"({session_packets} packets) — speech frames are failing to "
                "decrypt or nobody has spoken yet"
            )
        return "ok"

    def _empty_capture_error(
        self, guild_id: int, original: NoUsableAudioError | None = None
    ) -> NoUsableAudioError:
        """The accurate ``/clip`` error for an empty capture.

        Logs the precise pipeline state (WARNING) and returns a user message
        that matches the actual failing stage — never a generic "nobody
        talked" when the receiver is actually broken.
        """
        reason = self.diagnose_capture(guild_id)
        h = self.capture_health(guild_id)
        log.warning(
            "Empty /clip in guild %s: %s | pipeline=%s",
            guild_id,
            getattr(original, "args", (None,))[0] if original is not None else "n/a",
            {
                k: h.get(k)
                for k in (
                    "recording",
                    "session_elapsed_seconds",
                    "session_packets",
                    "session_non_silence_packets",
                    "buffered_packets",
                    "last_packet_age_seconds",
                    "received",
                    "buffered",
                    "non_silence_buffered",
                    "transport_errors",
                    "dave_errors",
                    "dave_fail_since_success",
                    "receiver_armed",
                )
            },
        )
        if reason == "capture_inactive":
            return NoUsableAudioError(
                "Voice recording is not currently active in this voice channel."
            )
        if reason == "session_just_started":
            return NoUsableAudioError(
                "Recording just restarted and hasn't captured any audio yet — "
                "give it a couple of seconds and try again."
            )
        if reason == "ok":
            # The session HAS captured speech — the requested window itself
            # was silent.  This is the genuine "try longer / speak now" case.
            return NoUsableAudioError(
                "No audible speech was captured — someone needs to speak in "
                "the channel right before you run /clip (or request a longer "
                "duration)."
            )
        return NoUsableAudioError(f"Voice capture is not receiving speech ({reason}).")

    def capture_recovery_candidates(self) -> list[int]:
        """Guild ids whose voice receiver is too broken to capture speech.

        Two failure shapes are recognised:

        * **never worked**: packets arrive, DAVE decryption has *never*
          succeeded (the E2EE key exchange failed on this connection);
        * **worked, then went dark**: speech used to decode but a long run of
          packets has failed since the last success (stale keys after an MLS
          group change) — the case a "never succeeded" check can never see.

        The only cure for either is a fresh voice connection.  The caller
        (the ``/clip`` cog's periodic sweep) decides when to reconnect and
        caps the attempts.
        """
        with self._lock:
            routers = list(self._routers.items())
        broken: list[int] = []
        for gid, router in routers:
            if router.packets_received < 250:
                continue
            never_worked = (
                not router.dave_ever_succeeded and router.dave_fail_streak >= 250
            )
            went_dark = (
                router.dave_ever_succeeded
                and router.dave_fail_since_success >= 600
                and router.last_dave_success_mono is not None
                and (self._clock() - router.last_dave_success_mono) >= 30.0
            )
            # A receiver that gets packets but decodes nothing AT ALL
            # (no decryptor, or everything dropped pre-buffer) is equally dead.
            dark_pipe = (
                router.packets_received >= 500
                and router.packets_buffered == 0
                and router.transport_errors < router.packets_received // 2
            )
            if never_worked or went_dark or dark_pipe:
                broken.append(gid)
        return broken

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
                    "voice capture is unavailable while I'm deafened — Discord "
                    "doesn't send audio to a deafened client. I lift deafens "
                    "automatically when I have the Mute Members permission; "
                    "give it a few seconds and try again."
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

        snapshot: ClipIntervalSnapshot | None = None
        try:
            snapshot = buf.snapshot(valid_duration)
        except NoUsableAudioError as err:
            raise self._empty_capture_error(guild.id, err) from None
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

        try:
            snapshot = buf.snapshot(valid_duration)
        except NoUsableAudioError as err:
            raise self._empty_capture_error(guild.id, err) from None
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
        """Send a validated clip file to an authorized operator via DM.

        Delivery is locked to the operator allow-list (the lords by default):
        the recipient must be one of ``AUTHORIZED_CLIP_USER_IDS``, which is
        what ``authorize_clip_user`` checks.
        """
        recipient_id = getattr(recipient, "id", None)
        if not authorize_clip_user(recipient_id):
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
