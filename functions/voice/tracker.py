"""Voice activity tracker, reconciliation engine, and daily DM reporter."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

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

# Upper bound on HTTP member fetches performed per leaderboard render for
# identities that are neither cached nor previously persisted.
MAX_HYDRATE_FETCHES = 50


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
        # Whether persisted active sessions have been recovered after boot.
        self._recovered = False

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
                    if not session.is_afk:
                        self.db.record_completed_session(
                            guild_id=guild_id,
                            user_id=user_id,
                            channel_id=session.channel_id,
                            started_at=session.started_at,
                            ended_at=now,
                        )
                    self.db.delete_active_session(guild_id, user_id)
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

        self._remember_member(member)

        afk_channel_id = guild.afk_channel.id if guild.afk_channel else None

        async with self._lock:
            # 1. User left voice entirely
            if before.channel is not None and after.channel is None:
                session = self.active_sessions.pop(key, None)
                if session and not session.is_afk:
                    self._persist_session(session, now)
                self._clear_active_row(guild_id, user_id)
                return

            # 2. User joined voice from disconnected state
            if before.channel is None and after.channel is not None:
                is_afk = (after.channel.id == afk_channel_id)
                session = ActiveVoiceSession(
                    guild_id=guild_id,
                    user_id=user_id,
                    channel_id=after.channel.id,
                    started_at=now,
                    is_muted=after.self_mute or after.mute,
                    is_deaf=after.self_deaf or after.deaf,
                    is_afk=is_afk,
                )
                self.active_sessions[key] = session
                self._persist_active_row(session, now)
                return

            # 3. User moved between channels in the same guild
            if before.channel is not None and after.channel is not None:
                session = self.active_sessions.get(key)
                was_afk = (before.channel.id == afk_channel_id)
                is_afk = (after.channel.id == afk_channel_id)

                if before.channel.id != after.channel.id:
                    if was_afk and not is_afk:
                        # Moving out of AFK to normal channel -> start tracking
                        session = ActiveVoiceSession(
                            guild_id=guild_id,
                            user_id=user_id,
                            channel_id=after.channel.id,
                            started_at=now,
                            is_muted=after.self_mute or after.mute,
                            is_deaf=after.self_deaf or after.deaf,
                            is_afk=False,
                        )
                        self.active_sessions[key] = session
                        self._persist_active_row(session, now)
                    elif not was_afk and is_afk:
                        # Moving into AFK channel -> close session
                        if session:
                            self._persist_session(session, now)
                            self.active_sessions.pop(key, None)
                        self._clear_active_row(guild_id, user_id)
                    else:
                        # Normal channel to normal channel -> update channel ID
                        if session:
                            session.channel_id = after.channel.id
                            self._persist_active_row(session, now)
                        else:
                            session = ActiveVoiceSession(
                                guild_id=guild_id,
                                user_id=user_id,
                                channel_id=after.channel.id,
                                started_at=now,
                                is_muted=after.self_mute or after.mute,
                                is_deaf=after.self_deaf or after.deaf,
                                is_afk=False,
                            )
                            self.active_sessions[key] = session
                            self._persist_active_row(session, now)

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
    # Persistence helpers (identities + in-flight sessions survive restarts)
    # ------------------------------------------------------------------

    def _remember_member(self, member: discord.Member) -> None:
        """Persist a member's last-known identity; best effort, never fatal."""
        try:
            avatar_url = None
            try:
                avatar_url = str(member.display_avatar.url)
            except Exception:
                avatar_url = None
            self.db.upsert_member_profile(
                guild_id=int(member.guild.id),
                user_id=int(member.id),
                display_name=str(member.display_name),
                username=str(member.name),
                avatar_url=avatar_url,
            )
        except Exception:
            log.debug("Could not persist member profile", exc_info=True)

    def _remember_user(self, guild_id: int, user: discord.User) -> None:
        """Persist identity for a user no longer in the guild (global fallback)."""
        try:
            self.db.upsert_member_profile(
                guild_id=int(guild_id),
                user_id=int(user.id),
                display_name=str(user.display_name),
                username=str(user.name),
                avatar_url=str(user.display_avatar.url),
            )
        except Exception:
            log.debug("Could not persist user profile", exc_info=True)

    def _persist_active_row(self, session: ActiveVoiceSession, seen_at: datetime) -> None:
        """Persist/heartbeat an in-progress session so restarts never lose time."""
        try:
            self.db.upsert_active_session(
                guild_id=session.guild_id,
                user_id=session.user_id,
                channel_id=session.channel_id,
                started_at=session.started_at,
                last_seen_at=seen_at,
                is_afk=session.is_afk,
            )
        except Exception:
            log.debug("Could not persist active session row", exc_info=True)

    def _clear_active_row(self, guild_id: int, user_id: int) -> None:
        """Drop a persisted in-progress session row once it has been closed."""
        try:
            self.db.delete_active_session(guild_id, user_id)
        except Exception:
            log.debug("Could not delete active session row", exc_info=True)

    # ------------------------------------------------------------------
    # Bot Restart & Reconciliation (Section 5 & 12)
    # ------------------------------------------------------------------

    async def reconcile_guilds(self) -> None:
        """Scan all guilds and voice channels to recover/synchronize active sessions."""
        now = datetime.now(timezone.utc)
        current_connected_keys: set[tuple[int, int]] = set()

        async with self._lock:
            # On first run after boot, resurrect sessions persisted before the
            # restart so ongoing voice time is not lost. Must run before the
            # scan so resumed sessions keep their original started_at.
            if not self._recovered:
                self._recovered = True
                self._recover_persisted_sessions(now)

            for guild in self.bot.guilds:
                afk_channel_id = guild.afk_channel.id if guild.afk_channel else None
                for channel in guild.voice_channels + getattr(guild, "stage_channels", []):
                    for member in channel.members:
                        if member.bot:
                            continue
                        key = (guild.id, member.id)
                        current_connected_keys.add(key)
                        is_afk = (channel.id == afk_channel_id)

                        self._remember_member(member)

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

            # Heartbeat every tracked session to the database (once per
            # reconciliation cycle), so a crash loses at most one cycle.
            for (g_id, u_id), session in self.active_sessions.items():
                self._persist_active_row(session, now)

            # Safely close sessions for users no longer connected
            for key in list(self.active_sessions.keys()):
                if key not in current_connected_keys:
                    session = self.active_sessions.pop(key)
                    if not session.is_afk:
                        self._persist_session(session, now)
                    self._clear_active_row(*key)

    def _recover_persisted_sessions(self, now: datetime) -> None:
        """
        Restore in-flight sessions persisted before a restart/crash.

        - User still in voice  -> resume tracking from the ORIGINAL started_at
          (their time is retained exactly, as if the restart never happened).
        - User gone while down -> close the session at the last heartbeat
          (last time they were confirmed connected).
        """
        try:
            persisted = self.db.get_active_sessions()
        except Exception:
            log.error("Could not load persisted voice sessions", exc_info=True)
            return

        for row in persisted:
            key = (row.guild_id, row.user_id)
            guild = self.bot.get_guild(row.guild_id)

            connected_member = None
            connected_channel_id = None
            currently_afk = False
            if guild is not None:
                afk_channel_id = guild.afk_channel.id if guild.afk_channel else None
                for channel in (
                    guild.voice_channels + getattr(guild, "stage_channels", [])
                ):
                    found = next(
                        (m for m in channel.members if not m.bot and m.id == row.user_id),
                        None,
                    )
                    if found is not None:
                        connected_member = found
                        connected_channel_id = channel.id
                        currently_afk = (channel.id == afk_channel_id)
                        break

            ended_at = min(row.last_seen_at, now)

            if row.is_afk:
                # AFK time is never tracked. Keep the (untracked) session only
                # if the user is still sitting in the AFK channel.
                if connected_member is not None and currently_afk:
                    self.active_sessions[key] = ActiveVoiceSession(
                        guild_id=row.guild_id,
                        user_id=row.user_id,
                        channel_id=connected_channel_id,
                        started_at=row.started_at,
                        is_afk=True,
                    )
                else:
                    self._clear_active_row(row.guild_id, row.user_id)
                continue

            if connected_member is not None and not currently_afk:
                # Still in voice: resume with the original start time.
                self.active_sessions[key] = ActiveVoiceSession(
                    guild_id=row.guild_id,
                    user_id=row.user_id,
                    channel_id=connected_channel_id,
                    started_at=row.started_at,
                    is_muted=(
                        connected_member.voice.self_mute or connected_member.voice.mute
                        if connected_member.voice else False
                    ),
                    is_deaf=(
                        connected_member.voice.self_deaf or connected_member.voice.deaf
                        if connected_member.voice else False
                    ),
                    is_afk=False,
                )
            else:
                # Left while the bot was down, or slid into the AFK channel:
                # close at the last confirmed heartbeat and clean up the row.
                self._persist_session(
                    ActiveVoiceSession(
                        guild_id=row.guild_id,
                        user_id=row.user_id,
                        channel_id=row.channel_id,
                        started_at=row.started_at,
                    ),
                    ended_at,
                )
                self._clear_active_row(row.guild_id, row.user_id)

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

    async def get_live_leaderboard(
        self,
        guild: discord.Guild,
        period: VoicePeriod = VoicePeriod.TODAY,
        target_date: date | None = None,
        hydrate: bool = True,
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

        # 3. Resolve identities for everyone on the board (cache -> HTTP -> DB)
        if hydrate:
            await self.hydrate_member_profiles(guild, time_map.keys())
        try:
            profiles = self.db.get_member_profiles(guild_id, list(time_map.keys()))
        except Exception:
            profiles = {}

        # 4. Sort by total_seconds DESC, user_id ASC
        sorted_users = sorted(
            time_map.items(), key=lambda item: (-item[1], item[0])
        )

        total_server_seconds = sum(t for _, t in sorted_users)

        # 5. Build LeaderboardMember objects
        members: list[LeaderboardMember] = []
        for rank, (u_id, total_sec) in enumerate(sorted_users, 1):
            if total_sec <= 0:
                continue
            guild_member = guild.get_member(u_id)
            if guild_member:
                display_name = guild_member.display_name
                username = guild_member.name
                avatar_url = guild_member.display_avatar.url
                # Keep the profile fresh so restarts remember this member
                self._remember_member(guild_member)
            else:
                profile = profiles.get(u_id)
                if profile:
                    display_name = profile.display_name
                    username = profile.username
                    avatar_url = profile.avatar_url
                else:
                    display_name = "Unknown User"
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

    async def hydrate_member_profiles(
        self,
        guild: discord.Guild,
        user_ids: Iterable[int],
        *,
        time_budget: float = 2.5,
    ) -> None:
        """
        Make sure we know the name/avatar of every given user.

        Order of preference: live member cache, persisted profile, then an
        HTTP fetch (member -> global user) whose result is persisted so it
        is never fetched again and survives restarts.

        Bounded to ``time_budget`` seconds so an interaction response is
        never delayed past Discord's window; every successful fetch is
        persisted, so remaining users resolve on the following render.
        """
        ids = [int(u) for u in user_ids]
        if not ids:
            return

        try:
            known = self.db.get_member_profiles(guild.id, ids)
        except Exception:
            known = {}

        pending = [u for u in ids if guild.get_member(u) is None and u not in known]
        deadline = time.monotonic() + time_budget
        for u_id in pending[:MAX_HYDRATE_FETCHES]:
            if time.monotonic() > deadline:
                break
            try:
                member = await guild.fetch_member(u_id)
                self._remember_member(member)
                continue
            except discord.NotFound:
                pass  # No longer in this guild; fall back to the global user
            except discord.HTTPException as err:
                log.debug("fetch_member(%s) failed: %s", u_id, err)
                continue

            try:
                user = await self.bot.fetch_user(u_id)
            except discord.HTTPException as err:
                log.debug("fetch_user(%s) failed: %s", u_id, err)
                continue
            self._remember_user(guild.id, user)

    async def get_live_user_stats(
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

        # Rank for today (ids and seconds only — skip identity hydration)
        leaderboard_today = await self.get_live_leaderboard(
            guild, VoicePeriod.TODAY, target_date, hydrate=False
        )
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
