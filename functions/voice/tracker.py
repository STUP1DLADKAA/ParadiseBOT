"""Voice activity tracker, reconciliation engine, and daily DM reporter.

The tracker talks to a :class:`~functions.voice.store.VoiceStore` (Supabase in
production) and never to SQL directly. All persistence semantics — atomic
session closes, midnight splitting, restart recovery, AFK exclusion, bounded
heartbeats and the outage outbox — live in the store implementations.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable

import discord

from functions.helpers import Colour

from .models import (
    ActiveVoiceSession,
    DailyUserStat,
    LeaderboardEntry,
    LeaderboardMember,
    PersistedActiveSession,
    VoicePeriod,
    format_duration,
    format_duration_diff,
)
from .render import LeaderboardImageRenderer
from .store import VoiceStore, VoiceStoreError, VoiceStoreUnavailable
from .time_windows import (
    IST,
    ensure_aware,
    ist_date,
    ist_day_start_utc,
    next_ist_midnight,
    period_overlap_seconds,
)
from .ui import build_leaderboard_text_embed

log = logging.getLogger(__name__)

DEAF_TIME_EXEMPT_USER_ID = 922359954025885737
VOICE_ALERT_CHANNEL_ID = 1507796401122181352
DEAF_WARNING_DELAY_SECONDS = 30.0


def excluded_from_credit(member, channel, state) -> bool:
    """Use the persisted AFK flag for all non-creditable voice time.

    Muted (non-deafened) members remain creditable and tracked; only the AFK
    channel and deafened states (except the exempt user) pause credit.
    """
    return bool(
        channel is not None and member.guild.afk_channel is not None
        and channel.id == member.guild.afk_channel.id
        or member.id != DEAF_TIME_EXEMPT_USER_ID
        and state is not None and (state.self_deaf or state.deaf)
    )


def _should_warn_deaf(member, channel, state) -> bool:
    """True when a member is deafened in a non-AFK VC and eligible for warning."""
    if channel is None or state is None:
        return False
    afk_channel = getattr(member.guild, "afk_channel", None)
    if afk_channel is not None and channel.id == afk_channel.id:
        return False
    if member.id == DEAF_TIME_EXEMPT_USER_ID:
        return False
    return bool(state.self_deaf or state.deaf)


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
        alert_channel_id: int = VOICE_ALERT_CHANNEL_ID,
        deaf_warning_seconds: float = DEAF_WARNING_DELAY_SECONDS,
        renderer: LeaderboardImageRenderer | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.bot = bot
        self.db = db
        self.dm_reports_enabled = dm_reports_enabled
        self.alert_channel_id = max(0, int(alert_channel_id))
        self.deaf_warning_seconds = max(0.0, float(deaf_warning_seconds))
        self._renderer = renderer
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.reconcile_interval = max(5.0, float(reconcile_interval))
        self.active_sessions: dict[tuple[int, int], ActiveVoiceSession] = {}
        self._profile_signatures: OrderedDict[
            tuple[int, int], tuple[str, str, str | None]
        ] = OrderedDict()
        self._lock = asyncio.Lock()
        self._reconciliation_task: asyncio.Task[None] | None = None
        self._dm_report_task: asyncio.Task[None] | None = None
        self._deaf_warning_tasks: dict[tuple[int, int], asyncio.Task[None]] = {}
        self._deaf_warned: set[tuple[int, int]] = set()
        self._stopped = False
        # Whether persisted active sessions have been recovered after boot.
        self._recovered = False
        # Recovery runs once from local fallback state, then again once the
        # Supabase store is bootstrapped (outbox drained) so remote rows are
        # recovered too without racing pending outbox closes.
        self._recovered_remote = False

    @property
    def renderer(self) -> LeaderboardImageRenderer:
        if self._renderer is None:
            self._renderer = LeaderboardImageRenderer()
        return self._renderer

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
        for task in list(self._deaf_warning_tasks.values()):
            task.cancel()
        if self._deaf_warning_tasks:
            await asyncio.gather(*self._deaf_warning_tasks.values(), return_exceptions=True)
        self._deaf_warning_tasks.clear()
        self._deaf_warned.clear()

        # Flush and close all active sessions
        async with self._lock:
            now = ensure_aware(self._clock()).astimezone(timezone.utc)
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
        now = ensure_aware(self._clock()).astimezone(timezone.utc)
        key = (guild_id, user_id)

        await self._remember_member(member)

        excluded = excluded_from_credit(member, after.channel, after)
        async with self._lock:
            session = self.active_sessions.get(key)
            if after.channel is None:
                self._cancel_deaf_warning(key)
                self.active_sessions.pop(key, None)
                if session is not None and not session.is_afk:
                    await self._persist_session(session, now)
                elif session is not None:
                    await self._clear_active_row(guild_id, user_id, started_before=session.started_at)
                else:
                    try:
                        await self.db.close_active_session(guild_id, user_id, now)
                    except VoiceStoreError:
                        log.debug("Could not close orphaned active row", exc_info=True)
                return

            if session is not None and session.is_afk != excluded:
                if not session.is_afk:
                    await self._persist_session(session, now)
                else:
                    await self._clear_active_row(guild_id, user_id, started_before=session.started_at)
                session = None
            if session is None:
                session = ActiveVoiceSession(
                    guild_id=guild_id, user_id=user_id,
                    channel_id=after.channel.id, started_at=now, is_afk=excluded,
                )
                self.active_sessions[key] = session
            session.channel_id = after.channel.id
            session.is_muted = bool(after.self_mute or after.mute)
            session.is_deaf = bool(after.self_deaf or after.deaf)
            await self._persist_active_row(session, now)
            self._sync_deaf_warning(member, after.channel, after)

    # ------------------------------------------------------------------
    # Deafen 30-second warning & alert channel resolution
    # ------------------------------------------------------------------

    def _cancel_deaf_warning(self, key: tuple[int, int]) -> None:
        task = self._deaf_warning_tasks.pop(key, None)
        if task is not None and not task.done():
            task.cancel()
        self._deaf_warned.discard(key)

    def _sync_deaf_warning(self, member: discord.Member, channel: Any, state: Any) -> None:
        key = (member.guild.id, member.id)
        if not _should_warn_deaf(member, channel, state):
            self._cancel_deaf_warning(key)
            return
        if self.alert_channel_id <= 0 or key in self._deaf_warned:
            return
        existing = self._deaf_warning_tasks.get(key)
        if existing is not None and not existing.done():
            return
        self._deaf_warning_tasks[key] = asyncio.create_task(
            self._deaf_warning_worker(key, member),
            name=f"voice-deaf-warning-{key[0]}-{key[1]}",
        )

    async def _deaf_warning_worker(
        self,
        key: tuple[int, int],
        fallback_member: discord.Member,
    ) -> None:
        current_task = asyncio.current_task()
        try:
            await asyncio.sleep(self.deaf_warning_seconds)
        except asyncio.CancelledError:
            return
        finally:
            if self._deaf_warning_tasks.get(key) is current_task:
                self._deaf_warning_tasks.pop(key, None)

        if self._stopped or key in self._deaf_warned:
            return

        guild_id, user_id = key
        session = self.active_sessions.get(key)
        if session is None or not session.is_deaf:
            return

        guild = getattr(self.bot, "get_guild", lambda _id: None)(guild_id)
        if getattr(guild, "id", None) != guild_id:
            guild = getattr(fallback_member, "guild", None)
        if guild is None:
            return

        afk_channel = getattr(guild, "afk_channel", None)
        if afk_channel is not None and getattr(afk_channel, "id", None) == session.channel_id:
            return

        member = None
        if hasattr(guild, "get_member"):
            cand = guild.get_member(user_id)
            if getattr(cand, "id", None) == user_id:
                member = cand
        if member is None:
            member = fallback_member

        _resolved_guild, alert_channel, _transient = await self._resolve_alert_channel(guild)
        if alert_channel is None:
            log.debug(
                "Skipping deafened VC warning for user %s: channel %s unavailable",
                user_id,
                self.alert_channel_id,
            )
            return

        self._deaf_warned.add(key)
        seconds_int = max(1, int(round(self.deaf_warning_seconds)))
        unit = "second" if seconds_int == 1 else "seconds"
        try:
            await alert_channel.send(
                f"{member.mention} no VC farming — you've been deafened in "
                f"<#{session.channel_id}> for {seconds_int} {unit}. "
                "Deafened time doesn't count; undeafen to resume earning VC time.",
                allowed_mentions=discord.AllowedMentions(
                    users=[member], roles=False, everyone=False
                ),
            )
        except discord.DiscordException:
            log.warning("Could not send deafened VC warning", exc_info=True)

    def _is_matching_alert_channel(self, channel: Any) -> bool:
        return (
            channel is not None
            and getattr(channel, "id", None) == self.alert_channel_id
            and callable(getattr(channel, "send", None))
        )

    async def _resolve_alert_channel(
        self, guild: discord.Guild | None = None
    ) -> tuple[discord.Guild | None, Any, bool]:
        """Resolve (guild, channel, transient_error) for self.alert_channel_id."""
        if self.alert_channel_id <= 0:
            return guild, None, False

        if guild is not None:
            for attr in ("get_channel_or_thread", "get_channel"):
                getter = getattr(guild, attr, None)
                if callable(getter):
                    ch = getter(self.alert_channel_id)
                    if self._is_matching_alert_channel(ch):
                        return guild, ch, False

            bot_getter = getattr(self.bot, "get_channel", None)
            if callable(bot_getter):
                ch = bot_getter(self.alert_channel_id)
                if self._is_matching_alert_channel(ch):
                    ch_guild = getattr(ch, "guild", None)
                    ch_guild_id = getattr(ch_guild, "id", None)
                    if not isinstance(ch_guild_id, int) or ch_guild_id == guild.id:
                        return guild, ch, False

            fetcher = getattr(guild, "fetch_channel", None)
            if callable(fetcher):
                try:
                    ch = await fetcher(self.alert_channel_id)
                except (discord.NotFound, discord.Forbidden):
                    return guild, None, False
                except discord.HTTPException:
                    return guild, None, True
                except (TypeError, AttributeError):
                    ch = None
                if self._is_matching_alert_channel(ch):
                    return guild, ch, False
            return guild, None, False

        # Guild not specified: locate the channel across connected guilds first
        guilds = getattr(self.bot, "guilds", None) or ()
        for g in guilds:
            for attr in ("get_channel_or_thread", "get_channel"):
                getter = getattr(g, attr, None)
                if callable(getter):
                    ch = getter(self.alert_channel_id)
                    if self._is_matching_alert_channel(ch):
                        return g, ch, False

        bot_getter = getattr(self.bot, "get_channel", None)
        if callable(bot_getter):
            ch = bot_getter(self.alert_channel_id)
            if self._is_matching_alert_channel(ch):
                ch_guild = getattr(ch, "guild", None)
                if ch_guild is not None and isinstance(getattr(ch_guild, "id", None), int):
                    return ch_guild, ch, False

        bot_fetcher = getattr(self.bot, "fetch_channel", None)
        if callable(bot_fetcher):
            try:
                ch = await bot_fetcher(self.alert_channel_id)
            except (discord.NotFound, discord.Forbidden):
                return None, None, False
            except discord.HTTPException:
                return None, None, True
            except (TypeError, AttributeError):
                ch = None
            if self._is_matching_alert_channel(ch):
                ch_guild = getattr(ch, "guild", None)
                if ch_guild is not None and isinstance(getattr(ch_guild, "id", None), int):
                    return ch_guild, ch, False

        return None, None, False

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
        now = ensure_aware(self._clock()).astimezone(timezone.utc)
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
                        is_afk = excluded_from_credit(member, channel, member.voice)

                        await self._remember_member(member)

                        if key not in self.active_sessions:
                            # Start new tracking session for user found in VC
                            self.active_sessions[key] = ActiveVoiceSession(
                                guild_id=guild.id,
                                user_id=member.id,
                                channel_id=channel.id,
                                started_at=now,
                                is_muted=bool(member.voice.self_mute or member.voice.mute) if member.voice else False,
                                is_deaf=bool(member.voice.self_deaf or member.voice.deaf) if member.voice else False,
                                is_afk=is_afk,
                            )
                        else:
                            # Update existing session channel & mute/deaf flags
                            session = self.active_sessions[key]
                            if session.is_afk != is_afk:
                                if not session.is_afk:
                                    await self._persist_session(session, now)
                                else:
                                    await self._clear_active_row(guild.id, member.id, started_before=session.started_at)
                                session.started_at = now
                            session.channel_id = channel.id
                            session.is_muted = bool(member.voice.self_mute or member.voice.mute) if member.voice else False
                            session.is_deaf = bool(member.voice.self_deaf or member.voice.deaf) if member.voice else False
                            session.is_afk = is_afk

                        self._sync_deaf_warning(member, channel, member.voice)

            # Split any ongoing sessions that crossed an IST midnight boundary
            # so pre-midnight hours are committed to the finished day and
            # post-midnight hours accrue cleanly into the new day.
            await self._rollover_midnight_sessions_unlocked(now)

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
                    self._cancel_deaf_warning(key)
                    session = self.active_sessions.pop(key)
                    if not session.is_afk:
                        await self._persist_session(session, now)
                    else:
                        await self._clear_active_row(
                            key[0], key[1], started_before=session.started_at
                        )

    async def rollover_midnight_sessions(self, now: datetime | None = None) -> int:
        """Commit pre-midnight portions of ongoing sessions and reset start to 12:00 AM IST."""
        instant = ensure_aware(now or self._clock()).astimezone(timezone.utc)
        async with self._lock:
            return await self._rollover_midnight_sessions_unlocked(instant)

    async def _rollover_midnight_sessions_unlocked(self, now: datetime) -> int:
        today_start_utc = ist_day_start_utc(now.astimezone(IST).date())
        rolled = 0
        for (guild_id, user_id), session in list(self.active_sessions.items()):
            if session.started_at < today_start_utc <= now:
                if not session.is_afk:
                    await self._persist_session(session, today_start_utc)
                else:
                    await self._clear_active_row(
                        guild_id, user_id, started_before=session.started_at
                    )
                session.started_at = today_start_utc
                await self._persist_active_row(session, now)
                rolled += 1
        return rolled

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
                        currently_afk = excluded_from_credit(found, channel, found.voice)
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
        now = ensure_aware(self._clock()).astimezone(timezone.utc)
        today_date = target_date or now.astimezone(IST).date()

        # 1. Fetch base records from the authoritative database (Supabase).
        #    A manual Dashboard edit is reflected here immediately — no cache.
        db_entries = await self.db.get_leaderboard_data(guild_id, period.value, today_date)
        time_map: dict[int, int] = {e.user_id: e.total_seconds for e in db_entries}

        # 2. Clip active sessions to this exact IST window. This also handles
        #    an active session that crossed a boundary before it was closed.
        for (g_id, u_id), session in self.active_sessions.items():
            if g_id == guild_id and not session.is_afk:
                elapsed = period_overlap_seconds(
                    session.started_at, now, period.value, today_date, now=now
                )
                if elapsed:
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
            active_sess = self.active_sessions.get((guild_id, u_id))
            is_active = bool(active_sess is not None and not active_sess.is_afk)

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
        now = ensure_aware(self._clock()).astimezone(timezone.utc)
        today_date = target_date or now.astimezone(IST).date()
        key = (guild.id, user_id)

        active_session = self.active_sessions.get(key)
        active_today = active_yesterday = active_week = active_month = active_all = 0
        if active_session and not active_session.is_afk:
            active_today = period_overlap_seconds(
                active_session.started_at, now, "today", today_date, now=now
            )
            active_yesterday = period_overlap_seconds(
                active_session.started_at, now, "yesterday", today_date, now=now
            )
            active_week = period_overlap_seconds(
                active_session.started_at, now, "week", today_date, now=now
            )
            active_month = period_overlap_seconds(
                active_session.started_at, now, "month", today_date, now=now
            )
            active_all = period_overlap_seconds(
                active_session.started_at, now, "all_time", today_date, now=now
            )

        today_total = breakdown["today"] + active_today
        yesterday_total = breakdown["yesterday"] + active_yesterday
        week_total = breakdown["week"] + active_week
        month_total = breakdown["month"] + active_month
        all_time_total = breakdown["all_time"] + active_all

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
            "yesterday_seconds": yesterday_total,
            "week_seconds": week_total,
            "month_seconds": month_total,
            "all_time_seconds": all_time_total,
            "rank_today": user_rank,
            "is_active_now": active_session is not None,
            "active_channel_id": active_session.channel_id if active_session else None,
            "active_session_elapsed": active_all,
        }

    # ------------------------------------------------------------------
    # Daily DM Reports Engine (Section 7)
    # ------------------------------------------------------------------

    async def _dm_report_loop(self) -> None:
        """Dispatch completed-day channel leaderboards & DM reports at 12:00 AM IST.

        The reports themselves are idempotent in storage, so after an offline
        boundary the loop catches up from the previous IST day on restart.
        """
        wait_until_ready = getattr(self.bot, "wait_until_ready", None)
        if callable(wait_until_ready):
            await wait_until_ready()
        # Give the Ready handler time to reconcile restored active sessions
        # before a catch-up report reads daily aggregates.
        await asyncio.sleep(10)
        last_completed_date: date | None = None

        while not self._stopped:
            now_utc = ensure_aware(self._clock()).astimezone(timezone.utc)
            today_ist = now_utc.astimezone(IST).date()
            try:
                # Commit any ongoing pre-midnight sessions at exact 12:00 AM IST
                # so the completed day's totals are durable and post-midnight
                # hours accrue cleanly into the new day's leaderboard.
                await self.rollover_midnight_sessions(now_utc)

                if not self.dm_reports_enabled and self.alert_channel_id <= 0:
                    last_completed_date = today_ist
                else:
                    # On startup, retry the last fully closed IST day. Durable
                    # sent marks prevent duplicate channel posts or DMs after a
                    # restart immediately before or after midnight.
                    if last_completed_date is None:
                        last_completed_date = today_ist - timedelta(days=1)

                    while last_completed_date < today_ist:
                        target = last_completed_date
                        target_str = target.isoformat()
                        log.info(
                            "Triggering daily IST voice reports for %s",
                            target_str,
                        )
                        channel_blocked = await self.dispatch_daily_channel_leaderboard(
                            target_str
                        )
                        dm_blocked = (
                            await self.dispatch_daily_dm_reports(target_str)
                            if self.dm_reports_enabled
                            else 0
                        )
                        blocked = channel_blocked + dm_blocked
                        if blocked <= 0:
                            last_completed_date = target + timedelta(days=1)
                            continue
                        if (today_ist - target).days >= DM_REPORT_RETRY_DAYS:
                            log.error(
                                "Giving up on daily voice reports for %s after %s days "
                                "of retries; the data remains in Supabase.",
                                target_str,
                                DM_REPORT_RETRY_DAYS,
                            )
                            last_completed_date = target + timedelta(days=1)
                            continue
                        break
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("Error in daily IST voice report loop")

            # No reporting reset depends on this wake-up: database queries
            # derive the current period from the actual IST date on every read.
            until_midnight = max(
                1.0, (next_ist_midnight(now_utc) - now_utc).total_seconds()
            )
            await asyncio.sleep(min(60.0, until_midnight))

    async def dispatch_daily_channel_leaderboard(self, target_date_str: str) -> int:
        """Publish the completed day's VC leaderboard to self.alert_channel_id.

        Returns 0 when done (or when the alert channel is disabled / absent),
        and 1 when a transient store or Discord error requires a retry.
        """
        if self.alert_channel_id <= 0:
            return 0
        try:
            target_date = date.fromisoformat(target_date_str)
        except ValueError:
            log.error("Invalid target_date_str for channel leaderboard: %s", target_date_str)
            return 0

        guild, channel, transient = await self._resolve_alert_channel()
        if channel is None or guild is None:
            if transient:
                log.warning(
                    "Daily VC leaderboard for %s postponed: could not fetch channel %s",
                    target_date_str,
                    self.alert_channel_id,
                )
                return 1
            return 0

        # Negative channel ID acts as a collision-free sentinel in voice_dm_reports
        # so channel publication is exactly-once across restarts with no schema change.
        sentinel_user_id = -int(self.alert_channel_id)
        try:
            already_sent = await self.db.has_dm_report_sent(
                target_date_str, guild.id, sentinel_user_id
            )
        except VoiceStoreUnavailable as err:
            log.warning(
                "Daily VC leaderboard for %s postponed (database unreachable): %s",
                target_date_str,
                err,
            )
            return 1
        if already_sent:
            return 0

        try:
            members = await self.get_live_leaderboard(
                guild, VoicePeriod.TODAY, target_date=target_date
            )
        except VoiceStoreUnavailable as err:
            log.warning(
                "Daily VC leaderboard read for %s postponed: %s",
                target_date_str,
                err,
            )
            return 1
        except VoiceStoreError:
            log.exception("Daily VC leaderboard read failed for %s", target_date_str)
            return 1

        date_display = target_date.strftime("%B %d, %Y")
        short_date = target_date.strftime("%b %d, %Y")
        total_server_seconds = sum(m.total_seconds for m in members)

        file: discord.File | None = None
        try:
            image_bytes = await self.renderer.render_leaderboard(
                guild_name=guild.name,
                period=VoicePeriod.TODAY,
                members=members,
                date_str=short_date,
                page=1,
                page_size=10,
                title_override=f"DAILY LEADERBOARD • {short_date.upper()}",
            )
            file = discord.File(
                io.BytesIO(image_bytes), filename="daily_vc_leaderboard.png"
            )
            embed = discord.Embed(
                title=f"🎙️ Daily Voice Leaderboard — {date_display}",
                colour=Colour.ACCENT,
            )
            if not members:
                embed.description = (
                    f"No voice activity was recorded on **{date_display}**."
                )
            embed.set_image(url="attachment://daily_vc_leaderboard.png")
            embed.set_footer(
                text=(
                    f"Server: {guild.name} • "
                    f"Server Total: {format_duration(total_server_seconds)} • "
                    f"{len(members)} tracked member{'s' if len(members) != 1 else ''}"
                )
            )
        except Exception:
            log.warning(
                "Could not render daily VC leaderboard image; using text embed",
                exc_info=True,
            )
            embed = build_leaderboard_text_embed(
                guild, VoicePeriod.TODAY, members, page=1, page_size=10
            )
            embed.title = f"🎙️ Daily Voice Leaderboard — {date_display}"
            file = None

        try:
            if file is not None:
                await channel.send(
                    embed=embed,
                    file=file,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await channel.send(
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except discord.Forbidden:
            log.warning(
                "Missing permission to post daily VC leaderboard in channel %s",
                self.alert_channel_id,
            )
            return 0
        except discord.HTTPException as err:
            log.warning(
                "Transient error posting daily VC leaderboard in %s: %s",
                self.alert_channel_id,
                err,
            )
            return 1

        try:
            await self.db.mark_dm_report_sent(
                target_date_str, guild.id, sentinel_user_id
            )
        except VoiceStoreError:
            log.warning(
                "Sent daily VC leaderboard for %s, but failed to persist mark",
                target_date_str,
                exc_info=True,
            )
        log.info(
            "Published daily VC leaderboard for %s to channel %s",
            target_date_str,
            self.alert_channel_id,
        )
        return 0

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

        # A voice session that is still open has not yet been committed to a
        # daily aggregate. Clip it to each requested IST day so the midnight
        # report includes live time without prematurely closing the session.
        now = ensure_aware(self._clock()).astimezone(timezone.utc)
        live_today: dict[tuple[int, int], int] = {}
        live_day_before: dict[tuple[int, int], int] = {}
        for (guild_id, user_id), session in self.active_sessions.items():
            if session.is_afk:
                continue
            today_seconds = period_overlap_seconds(
                session.started_at, now, "today", target_date, now=now
            )
            if today_seconds:
                live_today[(guild_id, user_id)] = today_seconds
            previous_seconds = period_overlap_seconds(
                session.started_at, now, "today", day_before, now=now
            )
            if previous_seconds:
                live_day_before[(guild_id, user_id)] = previous_seconds

        # Merge only in memory: historical rows and lifetime totals remain
        # untouched until the existing normal session-close path persists them.
        stats_by_key = {
            (stat.guild_id, stat.user_id): DailyUserStat(
                guild_id=stat.guild_id,
                user_id=stat.user_id,
                date_str=stat.date_str,
                duration_seconds=stat.duration_seconds,
            )
            for stat in stats_list
        }
        for (guild_id, user_id), seconds in live_today.items():
            key = (guild_id, user_id)
            if key in stats_by_key:
                stats_by_key[key].duration_seconds += seconds
            else:
                stats_by_key[key] = DailyUserStat(
                    guild_id=guild_id,
                    user_id=user_id,
                    date_str=target_date_str,
                    duration_seconds=seconds,
                )
        stats_list = list(stats_by_key.values())

        # One leaderboard/total fetch per guild instead of per user.
        guild_ids = {s.guild_id for s in stats_list}
        blocked_guilds: set[int] = set()
        board_cache: dict[int, list] = {}
        prev_board_cache: dict[int, list] = {}
        total_cache: dict[int, int] = {}

        def with_live_time(guild_id: int, rows: list, live: dict[tuple[int, int], int]) -> list[LeaderboardEntry]:
            totals = {int(row.user_id): int(row.total_seconds) for row in rows}
            for (live_guild_id, user_id), seconds in live.items():
                if live_guild_id == guild_id:
                    totals[user_id] = totals.get(user_id, 0) + seconds
            ranked = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
            return [
                LeaderboardEntry(user_id=user_id, total_seconds=seconds, rank=rank)
                for rank, (user_id, seconds) in enumerate(ranked, start=1)
            ]

        for guild_id in guild_ids:
            try:
                daily_board = await self.db.get_leaderboard_data(
                    guild_id, "today", target_date
                )
                previous_board = await self.db.get_leaderboard_data(
                    guild_id, "today", day_before
                )
                board_cache[guild_id] = with_live_time(guild_id, daily_board, live_today)
                prev_board_cache[guild_id] = with_live_time(
                    guild_id, previous_board, live_day_before
                )
                total_cache[guild_id] = (
                    await self.db.get_daily_server_total(guild_id, target_date_str)
                    + sum(
                        seconds
                        for (live_guild_id, _user_id), seconds in live_today.items()
                        if live_guild_id == guild_id
                    )
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
