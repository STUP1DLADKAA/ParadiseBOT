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

        The counters are integers only — never audio. If nothing ever decrypted
        the bot is muted, deafened or not really connected; if packets arrived
        but none were buffered, the failure is in the decoder or depacketiser.
        """
        guild_id = getattr(guild, "id", None)
        if guild_id is None:
            return
        try:
            stats = self.service.receiver_stats(int(guild_id))
        except Exception:  # noqa: BLE001
            log.debug("Could not read receiver statistics", exc_info=True)
            return
        log.info(
            "Empty /clip in guild %s — received=%s buffered=%s transport_errors=%s "
            "dave_errors=%s",
            guild_id,
            stats.get("received"),
            stats.get("buffered"),
            stats.get("transport_errors"),
            stats.get("dave_errors"),
        )

    @tasks.loop(seconds=15.0)
    async def _expiry_sweep(self) -> None:
        """Evict expired (>180s) packets even during completely silent periods."""
        try:
            self.service.evict_all_expired()
        except Exception:  # noqa: BLE001
            log.debug("Periodic voice buffer expiry sweep error", exc_info=True)

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
                return

            if getattr(after, "deaf", False):
                # Guild-forced deafen. Discord does not send audio to a
                # server-deafened client at all, so no amount of code here can
                # capture it — explain once and stop pretending to record.
                self.service.detach_guild(guild.id)
                self._explain_guild_deafen(guild)
                return

            if getattr(after, "self_deaf", False):
                # Our own deafen flag (e.g. joined by older code). Drop buffered
                # audio, then clear the flag so capture can resume.
                self.service.detach_guild(guild.id)
                self._schedule_undeafen(guild, after.channel)
                return

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

    def _schedule_undeafen(self, guild: discord.Guild, channel) -> None:
        """Clear a self-deafen flag so the rolling buffer can keep recording."""
        if not self.service.enabled or channel is None:
            return
        task = asyncio.create_task(
            self._undeafen(guild, channel), name=f"paradise-undeafen-{guild.id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _undeafen(self, guild: discord.Guild, channel) -> None:
        try:
            await guild.change_voice_state(
                channel=channel, self_deaf=False, self_mute=False
            )
            log.info(
                "Cleared the bot's self-deafen in guild %s so clipping keeps "
                "recording",
                guild.id,
            )
        except (discord.DiscordException, asyncio.TimeoutError):
            log.debug("Could not clear the bot's self-deafen", exc_info=True)

    def _explain_guild_deafen(self, guild: discord.Guild) -> None:
        """Log (once per guild) why clips stop when a moderator deafens the bot.

        This is a Discord protocol limit, not a bug here: a server-deafened
        voice client is never sent any audio by the gateway, and the bot
        cannot un-deafen itself against a moderator's action.
        """
        if guild.id in self._deafen_notice_sent:
            return
        self._deafen_notice_sent.add(guild.id)
        log.warning(
            "Voice clipping stopped in guild %s: a moderator server-deafened "
            "Paradise. Discord never sends audio to a server-deafened client, "
            "so capture cannot continue until the deafen is lifted (the bot "
            "cannot override it). Un-deafen it under Server Settings → Members.",
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
