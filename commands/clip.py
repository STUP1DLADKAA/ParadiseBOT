"""Authorized /clip slash command and voice recording lifecycle cog."""

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
    RecordingConsentView,
    UNAUTHORIZED_CLIP_MESSAGE,
    VoiceClipService,
    authorize_clip_user,
    build_recording_notice_text,
    validate_clip_duration,
)

log = logging.getLogger(__name__)

NOTICE_SETTLE_SECONDS: float = 0.6


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
        self._announced_notice_channel: dict[int, int] = {}
        self._tasks: set[asyncio.Task[None]] = set()

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
                self._announced_notice_channel.pop(guild.id, None)
                self.service.detach_guild(guild.id)
                return

            if getattr(after, "self_deaf", False) or getattr(after, "deaf", False):
                # Do not retain partial audio when deafened.
                self.service.detach_guild(guild.id)
                return

            if before_id != after_id:
                buf = self.service.get_buffer(guild.id)
                if buf is not None and buf.channel_id != after_id:
                    buf.start_session(after.channel.id)
                if self.service.enabled and self.service.config.notice_enabled:
                    task = asyncio.create_task(
                        self._post_recording_notice(guild, after.channel),
                        name=f"paradise-clip-notice-{guild.id}",
                    )
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
            return

        # 2. A human speaker left or moved out of the currently recorded channel.
        if not getattr(member, "bot", False) and before_id != after_id:
            buf = self.service.get_buffer(guild.id)
            if buf is not None and before_id is not None and buf.channel_id == before_id:
                self.service.handle_speaker_leave(guild.id, member.id)

    async def _post_recording_notice(
        self,
        guild: discord.Guild,
        channel: discord.VoiceChannel | discord.StageChannel,
    ) -> None:
        try:
            await asyncio.sleep(NOTICE_SETTLE_SECONDS)
            vc = getattr(guild, "voice_client", None)
            if (
                vc is None
                or not isinstance(vc, ClipVoiceClient)
                or not vc.is_connected()
                or getattr(vc.channel, "id", None) != channel.id
            ):
                return
            if self._announced_notice_channel.get(guild.id) == channel.id:
                return
            self._announced_notice_channel[guild.id] = channel.id

            notice = build_recording_notice_text(
                max_seconds=self.service.config.max_buffer_seconds,
                consent_mode=self.service.config.consent_mode,
            )
            view = RecordingConsentView(self.service.consent, guild.id)
            await channel.send(
                notice,
                view=view,
                delete_after=3600,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except asyncio.CancelledError:
            raise
        except discord.DiscordException:
            log.debug(
                "Could not post recording transparency notice in channel %s",
                getattr(channel, "id", None),
                exc_info=True,
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
