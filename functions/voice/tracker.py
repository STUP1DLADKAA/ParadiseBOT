"""Voice activity tracker, reconciliation engine, and daily DM reporter.

The tracker talks to a :class:`~functions.voice.store.VoiceStore` (Supabase in
production) and never to SQL directly. All persistence semantics — atomic
session closes, midnight splitting, restart recovery, AFK exclusion, bounded
heartbeats and the outage outbox — live in the store implementations.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

import discord

from .models import (
    ActiveVoiceSession,
    LeaderboardMember,
    PersistedActiveSession,
    VoicePeriod,
    format_duration,
    format_duration_diff,
)
from .store import VoiceStore, VoiceStoreError, VoiceStoreUnavailable

log = logging.getLogger(__name__)

# Upper bound on HTTP member fetches performed per leaderboard render for
# identities that are neither cached nor previously persisted.
MAX_HYDRATE_FETCHES = 50
# Reconciliation sees every connected member once per heartbeat. Keep only a
# bounded process-local signature cache so unchanged profiles do not hit the
# database on every scan.
MAX_PROFILE_SIGNATURES = 4096
# How long a partly-failed daily DM dispatch keeps retrying before giving up.
DM_REPORT_RETRY_DAYS = 2


class VoiceTracker:
    """Core tracker for voice channel activity across all guilds."""

    def __init__(
        self,
        bot: discord.Client,
        db: VoiceStore,
        *,
        reconcile_interval: float = 20.0,
        dm_reports_enabled: bool = True,
    ) -> None:
        self.bot = bot
        self.db = db
        self.dm_reports_enabled = dm_reports_enabled
        self.reconcile_interval = max(5.0, float(reconcile_interval))
        self.active_sessions: dict[tuple[int, int], ActiveVoiceSession] = {}
        self._profile_signatures: OrderedDict[
            tuple[int, int], tuple[str, str, str | None]
        ] = OrderedDict()
        self._lock = asyncio.Lock()
        self._reconciliation_task: asyncio.Task[None] | None = None
        self._dm_report_task: asyncio.Task[None] | None = None
        self._stopped = False
        # Whether persisted active sessions have been recovered after boot.
        self._recovered = False
        # Recovery runs once from local fallback state, then again once the
        # Supabase store is bootstrapped (outbox drained) so remote rows are
        # recovered too without racing pending outbox closes.
        self._recovered_remote = False

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
                        await self._persist_session(session, now)
                    else:
                        await self._clear_active_row(
                            guild_id, user_id, started_before=session.started_at
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

        await self._remember_member(member)

        afk_channel_id = guild.afk_channel.id if guild.afk_channel else None

        async with self._lock:
            # 1. User left voice entirely
            if before.channel is not None and after.channel is None:
                session = self.active_sessions.pop(key, None)
                if session is not None and not session.is_afk:
                    await self._persist_session(session, now)
                elif session is not None:
                    # AFK time is never tracked; just drop the row.
                    await self._clear_active_row(
                        guild_id, user_id, started_before=session.started_at
                    )
                else:
                    # No in-memory session — let the persisted row decide
                    # (close_active_session records non-AFK rows, skips AFK).
                    try:
                        await self.db.close_active_session(guild_id, user_id, now)
                    except VoiceStoreError as err:
                        log.debug("Could not close orphaned active row: %s", err)
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
                await self._persist_active_row(session, now)
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
                        await self._persist_active_row(session, now)
                    elif not was_afk and is_afk:
                        # Moving into AFK channel -> close session
                        if session:
                            await self._persist_session(session, now)
                            self.active_sessions.pop(key, None)
                        else:
                            await self._clear_active_row(guild_id, user_id)
                    else:
                        # Normal channel to normal channel -> update channel ID
                        if session:
                            session.channel_id = after.channel.id
                            await self._persist_active_row(session, now)
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
                            await self._persist_active_row(session, now)

                # Update mute / deaf state if changed
                session = self.active_sessions.get(key)
                if session:
                    session.is_muted = after.self_mute or after.mute
                    session.is_deaf = after.self_deaf or after.deaf

    async def _persist_session(self, session: ActiveVoiceSession, ended_at: datetime) -> None:
        """Record a completed session.

        Delegates to the store's atomic close (record + row delete in one
        transaction, using the persisted row's own started_at) and falls back
        to the store's durable outbox when Supabase is unreachable.
        """
        try:
            recorded = await self.db.close_active_session(
                session.guild_id,
                session.user_id,
                ended_at,
                fallback_channel_id=session.channel_id,
                fallback_started_at=session.started_at,
            )
            if recorded is None:
                # No persisted row anywhere — record straight from memory
                # (idempotent on the session identity) and clear any strays.
                await self.db.record_completed_session(
                    guild_id=session.guild_id,
                    user_id=session.user_id,
                    channel_id=session.channel_id,
                    started_at=session.started_at,
                    ended_at=ended_at,
                )
                await self.db.delete_active_session(
                    session.guild_id,
                    session.user_id,
                    started_before=session.started_at,
                )
        except VoiceStoreError as err:
            log.error("Failed to record voice session: %s", err)

    # ------------------------------------------------------------------
    # Persistence helpers (identities + in-flight sessions survive restarts)
    # ------------------------------------------------------------------

    async def _remember_profile(
        self,
        guild_id: int,
        user_id: int,
        display_name: str,
        username: str,
        avatar_url: str | None,
    ) -> None:
        """Persist an identity only when its process-local values changed."""
        key = (int(guild_id), int(user_id))
        signature = (display_name, username, avatar_url)
        if self._profile_signatures.get(key) == signature:
            self._profile_signatures.move_to_end(key)
            return
        try:
            await self.db.upsert_member_profile(
                guild_id=key[0],
                user_id=key[1],
                display_name=display_name,
                username=username,
                avatar_url=avatar_url,
            )
        except VoiceStoreError:
            # Not durably stored — do not cache, so a later render retries.
            log.debug("Could not persist member profile", exc_info=True)
            return
        self._profile_signatures[key] = signature
        self._profile_signatures.move_to_end(key)
        while len(self._profile_signatures) > MAX_PROFILE_SIGNATURES:
            self._profile_signatures.popitem(last=False)

    async def _remember_member(self, member: discord.Member) -> None:
        """Persist a member's last-known identity; best effort, never fatal."""
        try:
            avatar_url = None
            try:
                avatar_url = str(member.display_avatar.url)
            except Exception:
                avatar_url = None
            await self._remember_profile(
                guild_id=member.guild.id,
                user_id=member.id,
                display_name=str(member.display_name),
                username=str(member.name),
                avatar_url=avatar_url,
            )
        except Exception:
            log.debug("Could not persist member profile", exc_info=True)

    async def _remember_user(self, guild_id: int, user: discord.User) -> None:
        """Persist identity for a user no longer in the guild (global fallback)."""
        try:
            await self._remember_profile(
                guild_id=guild_id,
                user_id=user.id,
                display_name=str(user.display_name),
                username=str(user.name),
                avatar_url=str(user.display_avatar.url),
            )
        except Exception:
            log.debug("Could not persist user profile", exc_info=True)

    async def _persist_active_row(self, session: ActiveVoiceSession, seen_at: datetime) -> None:
        """Persist/heartbeat an in-progress session so restarts never lose time."""
        try:
            await self.db.upsert_active_session(
                guild_id=session.guild_id,
                user_id=session.user_id,
                channel_id=session.channel_id,
                started_at=session.started_at,
                last_seen_at=seen_at,
                is_afk=session.is_afk,
            )
        except VoiceStoreError:
            log.debug("Could not persist active session row", exc_info=True)

    async def _clear_active_row(
        self, guild_id: int, user_id: int, *, started_before: datetime | None = None
    ) -> None:
        """Drop a persisted in-progress session row once it has been closed."""
        try:
            await self.db.delete_active_session(
                guild_id, user_id, started_before=started_before
            )
        except VoiceStoreError:
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
            # restart so ongoing voice time is not lost. Runs once from local
            # fallback state and once more when the store is fully online.
            store_online = bool(getattr(self.db, "is_bootstrapped", True))
            if not self._recovered or (store_online and not self._recovered_remote):
                self._recovered = True
                if store_online:
                    self._recovered_remote = True
                await self._recover_persisted_sessions(now)

            for guild in self.bot.guilds:
                afk_channel_id = guild.afk_channel.id if guild.afk_channel else None
                for channel in guild.voice_channels + getattr(guild, "stage_channels", []):
                    for member in channel.members:
                        if member.bot:
                            continue
                        key = (guild.id, member.id)
                        current_connected_keys.add(key)
                        is_afk = (channel.id == afk_channel_id)

                        await self._remember_member(member)

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

            # Heartbeat every tracked session to the database in ONE batched
            # write (one compact row per user, last_seen_at bumped), so a
            # crash loses at most one heartbeat interval.
            heartbeat_rows = [
                PersistedActiveSession(
                    guild_id=guild_id,
                    user_id=user_id,
                    channel_id=session.channel_id,
                    started_at=session.started_at,
                    last_seen_at=now,
                    is_afk=session.is_afk,
                )
                for (guild_id, user_id), session in self.active_sessions.items()
            ]
            try:
                await self.db.heartbeat_active_sessions(heartbeat_rows)
            except VoiceStoreError as err:
                log.debug("Active session heartbeat failed: %s", err)

            # Safely close sessions for users no longer connected
            for key in list(self.active_sessions.keys()):
                if key not in current_connected_keys:
                    session = self.active_sessions.pop(key)
                    if not session.is_afk:
                        await self._persist_session(session, now)
                    else:
                        await self._clear_active_row(
                            key[0], key[1], started_before=session.started_at
                        )

    async def _recover_persisted_sessions(self, now: datetime) -> None:
        """
        Restore in-flight sessions persisted before a restart/crash.

        - User still in voice  -> resume tracking from the ORIGINAL started_at
          (their time is retained exactly, as if the restart never happened).
        - User gone while down -> close the session at the last heartbeat
          (last time they were confirmed connected).
        """
        try:
            persisted = await self.db.get_active_sessions()
        except VoiceStoreError:
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
                    await self._clear_active_row(
                        row.guild_id, row.user_id, started_before=row.started_at
                    )
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
                self.active_sessions.pop(key, None)
                try:
                    await self.db.close_active_session(
                        row.guild_id,
                        row.user_id,
                        ended_at,
                        fallback_channel_id=row.channel_id,
                        fallback_started_at=row.started_at,
                    )
                except VoiceStoreError as err:
                    log.error("Could not close stale active session: %s", err)

    async def _reconciliation_loop(self) -> None:
        """Periodic background reconciliation (default: every 20 seconds)."""
        await asyncio.sleep(5)  # initial brief delay on startup
        while not self._stopped:
            try:
                await self.reconcile_guilds()
            except asyncio.CancelledError:
                break
            except Exception as err:
                log.error("Error during periodic voice reconciliation: %s", err)
            await asyncio.sleep(self.reconcile_interval)

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

        # 1. Fetch base records from the authoritative database (Supabase).
        #    A manual Dashboard edit is reflected here immediately — no cache.
        db_entries = await self.db.get_leaderboard_data(guild_id, period.value, today_date)
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
            profiles = await self.db.get_member_profiles(guild_id, list(time_map.keys()))
        except VoiceStoreError:
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
                await self._remember_member(guild_member)
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
            known = await self.db.get_member_profiles(guild.id, ids)
        except VoiceStoreError:
            known = {}

        pending = [u for u in ids if guild.get_member(u) is None and u not in known]
        deadline = time.monotonic() + time_budget
        for u_id in pending[:MAX_HYDRATE_FETCHES]:
            if time.monotonic() > deadline:
                break
            try:
                member = await guild.fetch_member(u_id)
                await self._remember_member(member)
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
            await self._remember_user(guild.id, user)

    async def get_live_user_stats(
        self,
        guild: discord.Guild,
        user_id: int,
        target_date: date | None = None,
    ) -> dict[str, Any]:
        """Fetch full user voice breakdown including current live session."""
        breakdown = await self.db.get_user_breakdown(guild.id, user_id, target_date)
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
        last_completed_date: date | None = None

        while not self._stopped:
            try:
                now_utc = datetime.now(timezone.utc)
                today_utc = now_utc.date()

                if not self.dm_reports_enabled:
                    last_completed_date = today_utc
                    await asyncio.sleep(60)
                    continue

                if last_completed_date is None:
                    # First boot: never send reports for days we never observed.
                    last_completed_date = today_utc

                # If the UTC date rolled over, dispatch reports for the day
                # that just ended. A partly failed dispatch (Supabase hiccup)
                # is retried every minute until complete or too old.
                if today_utc > last_completed_date:
                    target = last_completed_date
                    log.info(
                        "Triggering Daily DM Voice Reports for date %s",
                        target.isoformat(),
                    )
                    blocked = await self.dispatch_daily_dm_reports(target.isoformat())
                    if blocked <= 0:
                        last_completed_date = today_utc
                    elif (today_utc - target).days >= DM_REPORT_RETRY_DAYS:
                        log.error(
                            "Giving up on daily DM reports for %s after %s days "
                            "of retries; the data remains in Supabase.",
                            target.isoformat(),
                            DM_REPORT_RETRY_DAYS,
                        )
                        last_completed_date = today_utc

            except asyncio.CancelledError:
                break
            except Exception as err:
                log.error("Error in daily DM report loop: %s", err)

            await asyncio.sleep(60)

    async def dispatch_daily_dm_reports(self, target_date_str: str) -> int:
        """Deliver personalized daily VC reports for target_date_str.

        Returns the number of users that could not be processed yet (0 when
        the dispatch is complete). Reports are only sent after their mark is
        durable in the database, so a retry can never double-DM anyone.
        """
        try:
            target_date = date.fromisoformat(target_date_str)
        except ValueError:
            log.error("Invalid target_date_str for DM reports: %s", target_date_str)
            return 0

        date_display = target_date.strftime("%B %d, %Y")
        day_before = target_date - timedelta(days=1)

        try:
            stats_list = await self.db.get_daily_guild_user_records(target_date_str)
        except VoiceStoreUnavailable as err:
            log.warning("Daily DM reports postponed (database unreachable): %s", err)
            return 1

        # One leaderboard/total fetch per guild instead of per user.
        guild_ids = {s.guild_id for s in stats_list}
        blocked_guilds: set[int] = set()
        board_cache: dict[int, list] = {}
        prev_board_cache: dict[int, list] = {}
        total_cache: dict[int, int] = {}
        for guild_id in guild_ids:
            try:
                board_cache[guild_id] = await self.db.get_leaderboard_data(
                    guild_id, "today", target_date
                )
                prev_board_cache[guild_id] = await self.db.get_leaderboard_data(
                    guild_id, "today", day_before
                )
                total_cache[guild_id] = await self.db.get_daily_server_total(
                    guild_id, target_date_str
                )
            except VoiceStoreUnavailable as err:
                log.warning(
                    "Daily DM reports for guild %s postponed: %s", guild_id, err
                )
                blocked_guilds.add(guild_id)

        blocked = len(blocked_guilds)

        for stat in stats_list:
            guild_id = stat.guild_id
            user_id = stat.user_id

            if guild_id in blocked_guilds:
                continue

            try:
                already_sent = await self.db.has_dm_report_sent(
                    target_date_str, guild_id, user_id
                )
            except VoiceStoreUnavailable as err:
                log.debug("Cannot verify DM report state for %s: %s", user_id, err)
                blocked += 1
                continue
            if already_sent:
                continue

            # Check user preference
            if not await self.db.get_user_dm_preference(user_id):
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
                blocked += 1  # Discord hiccup — retry next cycle
                continue

            # Calculate server rank for that date
            guild_board = board_cache.get(guild_id, [])
            user_rank = next(
                (e.rank for e in guild_board if e.user_id == user_id), None
            )
            rank_str = f"#{user_rank}" if user_rank else "Unranked"

            # Server total for that date
            server_total_str = format_duration(total_cache.get(guild_id, 0))

            # Compared with the day before
            prev_board = prev_board_cache.get(guild_id, [])
            day_before_sec = next(
                (e.total_seconds for e in prev_board if e.user_id == user_id), 0
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

            # Mark BEFORE sending: the mark is durable (direct write or the
            # local outbox), so a crash between mark and send can never cause
            # a duplicate DM on retry.
            await self.db.mark_dm_report_sent(target_date_str, guild_id, user_id)

            try:
                await user.send(dm_text)
                log.info("Sent daily VC report to user %s (%s)", user_id, user.name)
            except discord.Forbidden:
                log.debug("Could not DM user %s: DMs disabled/closed", user_id)
            except discord.HTTPException as err:
                log.warning(
                    "Marked VC report for %s but could not deliver the DM: %s",
                    user_id,
                    err,
                )
            except Exception as err:
                log.warning("Unexpected error sending DM to user %s: %s", user_id, err)

        if blocked:
            log.info(
                "Daily DM reports for %s: %s user(s) pending, retrying next cycle",
                target_date_str,
                blocked,
            )
        return blocked
