"""`/clip` — save the last N seconds of the voice channel and DM it to you.

Capture is silent and unconditional: no notice is posted, nobody is asked for
permission, and everyone in the channel is included. This is a private
deployment; the only gate is on who may *request* a clip.
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import VoiceClipConfig
from functions.voice_clip import (
    ClipError,
    ClipVoiceClient,
    MAX_CLIP_DURATION,
    MIN_CLIP_DURATION,
    NoUsableAudioError,
    UNAUTHORIZED_CLIP_MESSAGE,
    VoiceClipService,
    authorize_clip_user,
    validate_clip_duration,
)

log = logging.getLogger(__name__)


async def _send_ephemeral(interaction: discord.Interaction, content: str) -> None:
    """Reply privately to ``interaction``, tolerating expired interaction tokens."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)
    except discord.NotFound:
        log.debug("Interaction expired before clip response could be sent")
    except discord.HTTPException:
        log.debug("Could not send ephemeral clip response", exc_info=True)


class VoiceClip(commands.Cog):
    """Slash command `/clip` and rolling voice-buffer lifecycle management."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        raw_cfg = getattr(getattr(bot, "config", None), "voice_clip", None)
        cfg = raw_cfg if isinstance(raw_cfg, VoiceClipConfig) else VoiceClipConfig()
        self.service = VoiceClipService(bot, cfg)
        self.bot.voice_clip_service = self.service  # type: ignore[attr-defined]
        self._tasks: set[asyncio.Task[None]] = set()
        # One "Discord won't send audio while deafened" explanation per guild.
        self._deafen_notice_sent: set[int] = set()
        # Voice reconnects already attempted per guild because the DAVE E2EE
        # key exchange never completed on the current connection (bounded).
        self._e2ee_reconnects: dict[int, int] = {}

    async def cog_load(self) -> None:
        self._expiry_sweep.start()

    async def cog_unload(self) -> None:
        self._expiry_sweep.cancel()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self.service.shutdown()

    def _log_receiver_state(self, guild: discord.Guild | None) -> None:
        """Say *why* a clip came back empty instead of leaving a silent failure.

        The counters are integers only — never audio. The diagnosis tells the
        operator which stage lost the audio: Discord not sending at all,
        transport decryption, E2EE (DAVE) decryption, a dark receiver, or a
        genuinely silent window.
        """
        guild_id = getattr(guild, "id", None)
        if guild_id is None:
            return
        try:
            reason = self.service.diagnose_capture(int(guild_id))
        except Exception:  # noqa: BLE001
            log.debug("Could not read capture diagnosis", exc_info=True)
            return
        # The full counter dump is logged by service._empty_capture_error at
        # WARNING; here we add the classified reason for quick scanning.
        log.info("Empty /clip in guild %s — diagnosis=%s", guild_id, reason)

    @tasks.loop(seconds=15.0)
    async def _expiry_sweep(self) -> None:
        """Evict expired (>180s) packets even during completely silent periods."""
        try:
            self.service.evict_all_expired()
        except Exception:  # noqa: BLE001
            log.debug("Periodic voice buffer expiry sweep error", exc_info=True)
        self._log_capture_health()
        await self._recover_broken_capture()

    def _log_capture_health(self) -> None:
        """One-line pipeline health per guild — only when VOICE_CLIP_DEBUG is on.

        Temporary diagnostic instrumentation for live debugging; counts and
        flags only, never audio. Disable the env var after debugging.
        """
        cfg = getattr(self, "service", None)
        if cfg is None or not getattr(cfg.config, "debug_logging", False):
            return
        for gid in self.service.buffered_guild_ids():
            try:
                h = self.service.capture_health(gid)
            except Exception:  # noqa: BLE001
                continue
            log.info(
                "CAPTURE guild=%s recording=%s armed=%s recv=%s buffered=%s "
                "speech=%s session=%.0fs sess_pkts=%s sess_speech=%s "
                "last_pkt=%s dave_fail=%s",
                gid,
                h.get("recording"),
                h.get("receiver_armed"),
                h.get("received"),
                h.get("buffered"),
                h.get("non_silence_buffered"),
                float(h.get("session_elapsed_seconds") or 0.0),
                h.get("session_packets"),
                h.get("session_non_silence_packets"),
                (
                    f"{h.get('last_packet_age_seconds'):.0f}s"
                    if h.get("last_packet_age_seconds") is not None
                    else "never"
                ),
                h.get("dave_fail_since_success"),
            )

    async def _recover_broken_capture(self) -> None:
        """Re-voice connections whose capture is unrecoverably dark (bounded).

        Covers both failure shapes: an E2EE key exchange that never completed,
        and a receiver that worked but went dark afterwards (stale keys after
        an MLS group change, or a rebind that lost its decryptor).  A fresh
        voice session re-runs the whole handshake.  Capped per guild, and the
        cap resets once capture demonstrably works again.
        """
        if not self.service.enabled:
            return
        try:
            candidates = self.service.capture_recovery_candidates()
        except Exception:  # noqa: BLE001
            log.debug("Capture recovery scan failed", exc_info=True)
            return
        healthy = set(self.service.buffered_guild_ids()) - set(candidates)
        for gid in healthy:
            try:
                h = self.service.capture_health(gid)
            except Exception:  # noqa: BLE001
                continue
            if h.get("non_silence_buffered", 0) and h.get("receiver_armed"):
                # Speech is being decoded again — give future breakages a
                # fresh budget of recovery attempts.
                if self._e2ee_reconnects.pop(gid, None) is not None:
                    log.info(
                        "Voice capture in guild %s is decoding speech again; "
                        "recovery budget reset",
                        gid,
                    )

        for gid in candidates:
            attempts = self._e2ee_reconnects.get(gid, 0)
            if attempts >= 2:
                continue
            guild = self.bot.get_guild(gid)
            vc = getattr(guild, "voice_client", None) if guild is not None else None
            if guild is None or vc is None or not vc.is_connected():
                continue
            self._e2ee_reconnects[gid] = attempts + 1
            log.warning(
                "Voice capture in guild %s is receiving packets but has not "
                "decoded speech (E2EE keys stale or receiver disarmed). "
                "Reconnecting voice to re-run the key exchange "
                "(attempt %d/2).",
                gid,
                attempts + 1,
            )
            self.service.detach_guild(gid)
            try:
                await vc.disconnect(force=True)
            except Exception:  # noqa: BLE001
                log.debug("Capture recovery reconnect failed", exc_info=True)
            # Voice presence re-joins automatically on the next voice-state
            # event or follow-crowd tick.

    @_expiry_sweep.before_loop
    async def _before_expiry_sweep(self) -> None:
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:
            self._expiry_sweep.cancel()

    # ------------------------------------------------------------------
    # Discord Voice Lifecycle & Transparency Notice
    # ------------------------------------------------------------------

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

        # 1. Paradise itself joined, moved, deafened, or left a voice channel.
        if self.bot.user is not None and member.id == self.bot.user.id:
            if after.channel is None:
                self.service.detach_guild(guild.id)
                self._deafen_notice_sent.discard(guild.id)
                self._e2ee_reconnects.pop(guild.id, None)
                return

            if getattr(after, "deaf", False):
                # Server deafen. Discord does not send audio to a
                # server-deafened client at all, so capture is impossible
                # until it is lifted. Drop the unreadable audio but KEEP the
                # receiver state (the voice connection continues), lift the
                # deafen via enforcement, and resume capture on the healthy
                # voice-state update.
                self.service.clear_guild_buffer(guild.id)
                self._schedule_undeafen(guild, after.channel, server_deaf=True)
                return

            if getattr(after, "self_deaf", False):
                # Our own deafen flag (e.g. joined by older code). Drop buffered
                # audio, then clear the flag so capture can resume.
                self.service.clear_guild_buffer(guild.id)
                self._schedule_undeafen(guild, after.channel)
                return

            # Healthy voice state: make sure capture is live for this channel.
            # This self-heals after deafen recovery, reconnects, or any other
            # service-side detach (attach is idempotent per channel).
            self._ensure_capture(guild, after.channel)
            if before_id != after_id:
                buf = self.service.get_buffer(guild.id)
                if buf is not None and buf.channel_id != after_id:
                    buf.start_session(after.channel.id)
            return

        # 2. A human speaker left or moved out of the currently recorded channel.
        if not getattr(member, "bot", False) and before_id != after_id:
            buf = self.service.get_buffer(guild.id)
            if buf is not None and before_id is not None and buf.channel_id == before_id:
                self.service.handle_speaker_leave(guild.id, member.id)

    def _ensure_capture(self, guild: discord.Guild, channel) -> None:
        """Re-attach the rolling capture if it is not live for ``channel``.

        Idempotent: a healthy, matching buffer is left completely untouched,
        so this can run on every one of the bot's voice-state events without
        wiping audio or duplicating sessions.
        """
        if not self.service.enabled:
            return
        buf = self.service.get_buffer(guild.id)
        if (
            buf is not None
            and buf.is_recording
            and buf.channel_id == getattr(channel, "id", None)
        ):
            return
        vc = getattr(guild, "voice_client", None)
        if isinstance(vc, ClipVoiceClient) and vc.is_connected():
            log.info(
                "Re-attaching voice capture in guild %s (buffer was missing or "
                "stale for channel %s)",
                guild.id,
                getattr(channel, "id", "?"),
            )
            vc.rebind_recording()

    def _schedule_undeafen(
        self,
        guild: discord.Guild,
        channel,
        *,
        server_deaf: bool = False,
    ) -> None:
        """Clear a deafen flag so the rolling buffer can keep recording."""
        if not self.service.enabled or channel is None:
            return
        task = asyncio.create_task(
            self._undeafen(guild, channel, server_deaf=server_deaf),
            name=f"paradise-undeafen-{guild.id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _undeafen(
        self,
        guild: discord.Guild,
        channel,
        *,
        server_deaf: bool = False,
    ) -> None:
        try:
            if server_deaf:
                # A server deafen is a member flag only another member with
                # Mute Members (or the enforcement cog) can clear.
                enforcement = self.bot.get_cog("VoiceEnforcement")
                if enforcement is not None:
                    await enforcement.enforce_now(guild)
                else:
                    me = guild.me
                    if me is not None and me.guild_permissions.mute_members:
                        await me.edit(
                            deafen=False,
                            reason="Paradise: restoring voice capture after a server deafen",
                        )
                    else:
                        self._explain_guild_deafen(guild)
                        return
                log.info(
                    "Lifted a server deafen in guild %s so clipping keeps recording",
                    guild.id,
                )
            else:
                await guild.change_voice_state(
                    channel=channel, self_deaf=False, self_mute=False
                )
                log.info(
                    "Cleared the bot's self-deafen in guild %s so clipping keeps "
                    "recording",
                    guild.id,
                )
        except (discord.DiscordException, asyncio.TimeoutError):
            if server_deaf:
                self._explain_guild_deafen(guild)
            log.debug("Could not clear the bot's deafen", exc_info=True)
            return
        # The healthy voice-state update re-attaches capture on its own; do it
        # here too in case that event is delayed or coalesced away.
        self._ensure_capture(guild, channel)

    def _explain_guild_deafen(self, guild: discord.Guild) -> None:
        """Log (once per guild) why clips stop while a moderator deafens the bot.

        This is a Discord protocol limit: a server-deafened voice client is
        never sent any audio by the gateway. Paradise lifts the deafen itself
        when it has the Mute Members permission; this message only appears
        when that is not possible.
        """
        if guild.id in self._deafen_notice_sent:
            return
        self._deafen_notice_sent.add(guild.id)
        log.warning(
            "Voice clipping stopped in guild %s: a moderator server-deafened "
            "Paradise and the deafen could not be lifted automatically. "
            "Discord never sends audio to a server-deafened client, so capture "
            "cannot continue until it is removed — either give Paradise the "
            "'Mute Members' permission (it will un-deafen itself) or lift the "
            "deafen under Server Settings → Members.",
            guild.id,
        )

    # ------------------------------------------------------------------
    # /clip duration:<seconds>
    # ------------------------------------------------------------------

    @app_commands.command(
        name="clip",
        description="Save a mixed clip of recent voice-channel audio and receive it via DM",
    )
    @app_commands.describe(
        duration="Number of recent seconds to clip (whole number from 1 to 180)",
    )
    async def clip(
        self,
        interaction: discord.Interaction,
        duration: app_commands.Range[int, MIN_CLIP_DURATION, MAX_CLIP_DURATION],
    ) -> None:
        # 1. Strict server-side authorization on immutable Discord user ID FIRST.
        user_id = getattr(getattr(interaction, "user", None), "id", None)
        if not authorize_clip_user(user_id):
            log.warning("Denied unauthorized /clip invocation (user_id=%s)", user_id)
            await _send_ephemeral(interaction, UNAUTHORIZED_CLIP_MESSAGE)
            return

        # 2. Server-side duration and preflight checks before deferring or exporting.
        try:
            valid_duration = validate_clip_duration(duration)
            self.service.preflight_check(
                requester_user_id=user_id,
                duration=valid_duration,
                guild=interaction.guild,
            )
        except ClipError as err:
            await _send_ephemeral(interaction, f"⚠️ {err}")
            return

        # 3. Acknowledge authorized interaction promptly before async export/DM.
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=True)
        except (discord.NotFound, discord.HTTPException):
            log.debug("Authorized /clip interaction expired during defer", exc_info=True)
            return

        # 4. Snapshot, mix, encode, deliver by DM, and clean up temporary output.
        try:
            report = await self.service.create_and_deliver_clip(
                requester=interaction.user,
                guild=interaction.guild,
                duration=valid_duration,
            )
        except ClipError as err:
            if isinstance(err, NoUsableAudioError):
                self._log_receiver_state(interaction.guild)
            await _send_ephemeral(interaction, f"⚠️ {err}")
            return
        except Exception:  # noqa: BLE001
            log.exception("Unexpected error during /clip execution")
            await _send_ephemeral(
                interaction,
                "⚠️ Something went wrong while creating the voice clip.",
            )
            return

        captured_str = f"{report.captured_seconds:.1f}s"
        if report.partial_window:
            summary = (
                f"✅ Sent **{discord.utils.escape_markdown(report.filename)}** to your DMs "
                f"(requested `{report.requested_seconds}s`, captured available `{captured_str}`)."
            )
        else:
            summary = (
                f"✅ Sent **{discord.utils.escape_markdown(report.filename)}** to your DMs "
                f"(requested `{report.requested_seconds}s`, captured `{captured_str}`)."
            )
        await _send_ephemeral(interaction, summary)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VoiceClip(bot))
