"""Voice activity tracker, reconciliation engine, and daily DM reporter."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Sequence

import discord

from .database import LeaderboardEntry, VoiceDatabase
from .models import (
    ActiveVoiceSession,
    LeaderboardMember,
    VoicePeriod,
    format_duration,
    format_duration_diff,
)

log = logging.getLogger(__name__)


class VoiceTracker:
    """Core tracker for voice channel activity across all guilds."""

    def __init__(self, bot: discord.Client, db: VoiceDatabase | None = None) -> None:
        self.bot = bot
        self.db = db or VoiceDatabase()
        self.active_sessions: dict[tuple[int, int], ActiveVoiceSession] = {}
        self._lock = asyncio.Lock()
        self._reconciliation_task: asyncio.Task[None] | None = None
        self._dm_report_task: asyncio.Task[None] | None = None
        self._stopped = False

    def start_background_tasks(self) -> None:
        """Start periodic reconciliation and daily DM reporting loops."""
        if self._reconciliation_task is None or self._reconciliation_task.done():
            self._reconciliation_task = asyncio.create_task(
                self._reconciliation_loop(), name="voice-reconciliation"
            )
        if self._dm_report_task is None or self._dm_report_task.done():
            self._dm_report_task = asyncio.create_task(
                self._dm_report_loop(), name="voice-daily-dm-reports"
            )

    async def stop(self) -> None:
        """Safely close active sessions and cancel background tasks on bot shutdown."""
        self._stopped = True
        if self._reconciliation_task:
            self._reconciliation_task.cancel()
        if self._dm_report_task:
            self._dm_report_task.cancel()

        # Flush and close all active sessions
        async with self._lock:
            now = datetime.now(timezone.utc)
            for (guild_id, user_id), session in list(self.active_sessions.items()):
                try:
                    self.db.record_completed_session(
                        guild_id=guild_id,
                        user_id=user_id,
                        channel_id=session.channel_id,
                        started_at=session.started_at,
                        ended_at=now,
                    )
                except Exception as err:
                    log.error("Error saving active session on shutdown: %s", err)
            self.active_sessions.clear()

    # ------------------------------------------------------------------
    # Voice State Updates (Section 5)
    # ------------------------------------------------------------------

    async def handle_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Handle Discord voice state transitions: join, leave, move, mute/deaf."""
        if member.bot or not member.guild:
            return

        guild = member.guild
        guild_id = guild.id
        user_id = member.id
        now = datetime.now(timezone.utc)
        key = (guild_id, user_id)

        afk_channel_id = guild.afk_channel.id if guild.afk_channel else None

        async with self._lock:
            # 1. User left voice entirely
            if before.channel is not None and after.channel is None:
                session = self.active_sessions.pop(key, None)
                if session and not session.is_afk:
                    self._persist_session(session, now)
                return

            # 2. User joined voice from disconnected state
            if before.channel is None and after.channel is not None:
                is_afk = (after.channel.id == afk_channel_id)
                self.active_sessions[key] = ActiveVoiceSession(
                    guild_id=guild_id,
                    user_id=user_id,
                    channel_id=after.channel.id,
                    started_at=now,
                    is_muted=after.self_mute or after.mute,
                    is_deaf=after.self_deaf or after.deaf,
                    is_afk=is_afk,
                )
                return

            # 3. User moved between channels in the same guild
            if before.channel is not None and after.channel is not None:
                session = self.active_sessions.get(key)
                was_afk = (before.channel.id == afk_channel_id)
                is_afk = (after.channel.id == afk_channel_id)

                if before.channel.id != after.channel.id:
                    if was_afk and not is_afk:
                        # Moving out of AFK to normal channel -> start tracking
                        self.active_sessions[key] = ActiveVoiceSession(
                            guild_id=guild_id,
                            user_id=user_id,
                            channel_id=after.channel.id,
                            started_at=now,
                            is_muted=after.self_mute or after.mute,
                            is_deaf=after.self_deaf or after.deaf,
                            is_afk=False,
                        )
                    elif not was_afk and is_afk:
                        # Moving into AFK channel -> close session
                        if session:
                            self._persist_session(session, now)
                            self.active_sessions.pop(key, None)
                    else:
                        # Normal channel to normal channel -> update channel ID
                        if session:
                            session.channel_id = after.channel.id
                        else:
                            self.active_sessions[key] = ActiveVoiceSession(
                                guild_id=guild_id,
                                user_id=user_id,
                                channel_id=after.channel.id,
                                started_at=now,
                                is_muted=after.self_mute or after.mute,
                                is_deaf=after.self_deaf or after.deaf,
                                is_afk=False,
                            )

                # Update mute / deaf state if changed
                session = self.active_sessions.get(key)
                if session:
                    session.is_muted = after.self_mute or after.mute
                    session.is_deaf = after.self_deaf or after.deaf

    def _persist_session(self, session: ActiveVoiceSession, ended_at: datetime) -> None:
        """Helper to write session to database."""
        try:
            self.db.record_completed_session(
                guild_id=session.guild_id,
                user_id=session.user_id,
                channel_id=session.channel_id,
                started_at=session.started_at,
                ended_at=ended_at,
            )
        except Exception as err:
            log.error("Failed to record voice session: %s", err)

    # ------------------------------------------------------------------
    # Bot Restart & Reconciliation (Section 5 & 12)
    # ------------------------------------------------------------------

    async def reconcile_guilds(self) -> None:
        """Scan all guilds and voice channels to recover/synchronize active sessions."""
        now = datetime.now(timezone.utc)
        current_connected_keys: set[tuple[int, int]] = set()

        async with self._lock:
            for guild in self.bot.guilds:
                afk_channel_id = guild.afk_channel.id if guild.afk_channel else None
                for channel in guild.voice_channels + getattr(guild, "stage_channels", []):
                    for member in channel.members:
                        if member.bot:
                            continue
                        key = (guild.id, member.id)
                        current_connected_keys.add(key)
                        is_afk = (channel.id == afk_channel_id)

                        if key not in self.active_sessions:
                            # Start new tracking session for user found in VC
                            self.active_sessions[key] = ActiveVoiceSession(
                                guild_id=guild.id,
                                user_id=member.id,
                                channel_id=channel.id,
                                started_at=now,
                                is_muted=member.voice.self_mute or member.voice.mute if member.voice else False,
                                is_deaf=member.voice.self_deaf or member.voice.deaf if member.voice else False,
                                is_afk=is_afk,
                            )
                        else:
                            # Update existing session channel
                            session = self.active_sessions[key]
                            session.channel_id = channel.id
                            session.is_afk = is_afk

            # Safely close sessions for users no longer connected
            for key in list(self.active_sessions.keys()):
                if key not in current_connected_keys:
                    session = self.active_sessions.pop(key)
                    if not session.is_afk:
                        self._persist_session(session, now)

    async def _reconciliation_loop(self) -> None:
        """Periodic background reconciliation every 60 seconds."""
        await asyncio.sleep(5)  # initial brief delay on startup
        while not self._stopped:
            try:
                await self.reconcile_guilds()
            except asyncio.CancelledError:
                break
            except Exception as err:
                log.error("Error during periodic voice reconciliation: %s", err)
            await asyncio.sleep(60)

    # ------------------------------------------------------------------
    # Live Leaderboard & User Stats
    # ------------------------------------------------------------------

    def get_live_leaderboard(
        self,
        guild: discord.Guild,
        period: VoicePeriod = VoicePeriod.TODAY,
        target_date: date | None = None,
    ) -> list[LeaderboardMember]:
        """
        Fetch leaderboard for guild, merging live active session time seamlessly.
        """
        guild_id = guild.id
        now = datetime.now(timezone.utc)
        today_date = target_date or now.date()

        # 1. Fetch base records from DB
        db_entries = self.db.get_leaderboard_data(guild_id, period.value, today_date)
        time_map: dict[int, int] = {e.user_id: e.total_seconds for e in db_entries}

        # 2. Add ongoing active session elapsed time if applicable (for today, week, month, all_time)
        if period in (VoicePeriod.TODAY, VoicePeriod.WEEK, VoicePeriod.MONTH, VoicePeriod.ALL_TIME):
            for (g_id, u_id), session in self.active_sessions.items():
                if g_id == guild_id and not session.is_afk:
                    # Calculate active elapsed seconds for today
                    elapsed = max(0, int((now - session.started_at).total_seconds()))
                    time_map[u_id] = time_map.get(u_id, 0) + elapsed

        # 3. Sort by total_seconds DESC, user_id ASC
        sorted_users = sorted(
            time_map.items(), key=lambda item: (-item[1], item[0])
        )

        total_server_seconds = sum(t for _, t in sorted_users)

        # 4. Build LeaderboardMember objects
        members: list[LeaderboardMember] = []
        for rank, (u_id, total_sec) in enumerate(sorted_users, 1):
            if total_sec <= 0:
                continue
            guild_member = guild.get_member(u_id)
            if guild_member:
                display_name = guild_member.display_name
                username = guild_member.name
                avatar_url = guild_member.display_avatar.url
            else:
                display_name = f"User {u_id}"
                username = f"user_{u_id}"
                avatar_url = None

            pct = (total_sec / total_server_seconds * 100.0) if total_server_seconds > 0 else 0.0
            is_active = (guild_id, u_id) in self.active_sessions

            members.append(
                LeaderboardMember(
                    rank=rank,
                    user_id=u_id,
                    display_name=display_name,
                    username=username,
                    avatar_url=avatar_url,
                    total_seconds=total_sec,
                    percentage=round(pct, 1),
                    is_active_now=is_active,
                )
            )

        return members

    def get_live_user_stats(
        self,
        guild: discord.Guild,
        user_id: int,
        target_date: date | None = None,
    ) -> dict[str, Any]:
        """Fetch full user voice breakdown including current live session."""
        breakdown = self.db.get_user_breakdown(guild.id, user_id, target_date)
        now = datetime.now(timezone.utc)
        key = (guild.id, user_id)

        active_session = self.active_sessions.get(key)
        active_elapsed = 0
        if active_session and not active_session.is_afk:
            active_elapsed = max(0, int((now - active_session.started_at).total_seconds()))

        today_total = breakdown["today"] + active_elapsed
        week_total = breakdown["week"] + active_elapsed
        month_total = breakdown["month"] + active_elapsed
        all_time_total = breakdown["all_time"] + active_elapsed

        # Rank for today
        leaderboard_today = self.get_live_leaderboard(guild, VoicePeriod.TODAY, target_date)
        user_rank = None
        for m in leaderboard_today:
            if m.user_id == user_id:
                user_rank = m.rank
                break

        return {
            "today_seconds": today_total,
            "yesterday_seconds": breakdown["yesterday"],
            "week_seconds": week_total,
            "month_seconds": month_total,
            "all_time_seconds": all_time_total,
            "rank_today": user_rank,
            "is_active_now": active_session is not None,
            "active_channel_id": active_session.channel_id if active_session else None,
            "active_session_elapsed": active_elapsed,
        }

    # ------------------------------------------------------------------
    # Daily DM Reports Engine (Section 7)
    # ------------------------------------------------------------------

    async def _dm_report_loop(self) -> None:
        """Checks every 60s if UTC date has changed and sends daily DM reports."""
        await asyncio.sleep(10)
        last_checked_date: date | None = None

        while not self._stopped:
            try:
                now_utc = datetime.now(timezone.utc)
                today_utc = now_utc.date()

                if last_checked_date is None:
                    last_checked_date = today_utc

                # If date rolled over
                if today_utc > last_checked_date:
                    yesterday = last_checked_date
                    yesterday_str = yesterday.isoformat()
                    log.info("Triggering Daily DM Voice Reports for date %s", yesterday_str)
                    await self.dispatch_daily_dm_reports(yesterday_str)
                    last_checked_date = today_utc

            except asyncio.CancelledError:
                break
            except Exception as err:
                log.error("Error in daily DM report loop: %s", err)

            await asyncio.sleep(60)

    async def dispatch_daily_dm_reports(self, target_date_str: str) -> None:
        """Deliver personalized daily VC reports to all active members for target_date_str."""
        try:
            target_date = date.fromisoformat(target_date_str)
        except ValueError:
            log.error("Invalid target_date_str for DM reports: %s", target_date_str)
            return

        date_display = target_date.strftime("%B %d, %Y")
        stats_list = self.db.get_daily_guild_user_records(target_date_str)

        for stat in stats_list:
            guild_id = stat.guild_id
            user_id = stat.user_id

            if self.db.has_dm_report_sent(target_date_str, guild_id, user_id):
                continue

            # Check user preference
            if not self.db.get_user_dm_preference(user_id):
                continue

            guild = self.bot.get_guild(guild_id)
            if not guild:
                continue

            user = self.bot.get_user(user_id)
            if not user:
                try:
                    user = await self.bot.fetch_user(user_id)
                except Exception:
                    user = None
            if not user:
                continue

            # Calculate server rank for that date
            guild_board = self.db.get_leaderboard_data(guild_id, "today", target_date)
            user_rank = next((e.rank for e in guild_board if e.user_id == user_id), None)
            rank_str = f"#{user_rank}" if user_rank else "Unranked"

            # Server total for that date
            server_total_sec = self.db.get_daily_server_total(guild_id, target_date_str)
            server_total_str = format_duration(server_total_sec)

            # Compared with day before yesterday
            day_before = target_date - timedelta(days=1)
            day_before_records = self.db.get_leaderboard_data(guild_id, "today", day_before)
            day_before_sec = next(
                (e.total_seconds for e in day_before_records if e.user_id == user_id), 0
            )
            diff_sec = stat.duration_seconds - day_before_sec
            diff_str = format_duration_diff(diff_sec)

            # Build DM message
            user_voice_str = format_duration(stat.duration_seconds)
            dm_text = (
                f"**Your Paradise VC Report**\n\n"
                f"📅 {date_display}\n"
                f"🎙️ Voice time: {user_voice_str}\n"
                f"🏆 Server rank: {rank_str}\n"
                f"👥 Server total: {server_total_str}\n"
                f"📈 Compared with yesterday: {diff_str}\n\n"
                f"Keep talking. Paradise was definitely not quiet today."
            )

            try:
                await user.send(dm_text)
                self.db.mark_dm_report_sent(target_date_str, guild_id, user_id)
                log.info("Sent daily VC report to user %s (%s)", user_id, user.name)
            except discord.Forbidden:
                log.debug("Could not DM user %s: DMs disabled/closed", user_id)
                self.db.mark_dm_report_sent(target_date_str, guild_id, user_id)
            except discord.HTTPException as err:
                log.debug("HTTP exception sending DM to user %s: %s", user_id, err)
            except Exception as err:
                log.warning("Unexpected error sending DM to user %s: %s", user_id, err)
