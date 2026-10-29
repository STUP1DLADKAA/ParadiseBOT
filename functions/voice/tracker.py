"""Voice activity tracker, reconciliation engine, and daily DM reporter.

The tracker talks to a :class:`~functions.voice.store.VoiceStore` (Supabase in
production) and never to SQL directly. All persistence semantics — atomic
session closes, midnight splitting, restart recovery, AFK exclusion, bounded
heartbeats and the outage outbox — live in the store implementations.

Write pipeline
--------------
A Discord voice event only mutates the in-memory session map. The resulting
heartbeat is *coalesced* into one row per session and written by a single
batched upsert, so a burst of mute/deafen/move events costs one request per
window instead of one request per event. Two things stay immediate because
crash safety depends on them:

* a brand-new session (join / AFK transition / post-midnight rollover) is
  written before the event handler returns, and
* a session end is closed atomically before the handler returns.

Everything else is written once per ``coalesce_seconds`` window (default 2 s,
``VOICE_WRITE_COALESCE_SECONDS``), so the maximum persistence delay for a
heartbeat is one window. Crash accuracy is unchanged because the periodic
reconciliation heartbeat already bounded it to one reconcile interval.

Every active-row write and every close runs under one persistence lock, which
is what makes the coalescing safe: a delayed heartbeat can never be written
for a session that has already been closed, so no ghost row can appear and
no time can be double counted. Shutdown flushes all pending rows before
closing the remaining sessions, within a bounded timeout.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import time
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable

import discord

from .models import (
    ActiveVoiceSession,
    DailyUserStat,
    LeaderboardEntry,
    LeaderboardMember,
    MemberProfile,
    PersistedActiveSession,
    VoicePeriod,
)
from .render import LeaderboardImageRenderer
from .store import VoiceStore, VoiceStoreError, VoiceStoreUnavailable
from .time_windows import (
    IST,
    ensure_aware,
    ist_day_start_utc,
    next_ist_midnight,
    period_overlap_seconds,
)
from .ui import (
    build_daily_dm_embed,
    build_leaderboard_text_embed,
    leaderboard_image_embed,
)

log = logging.getLogger(__name__)

#: Default operator exempt from the deafened-while-in-VC rule. Override with
#: ``VOICE_DEAF_EXEMPT_USER_IDS`` (comma-separated Discord user IDs).
DEAF_TIME_EXEMPT_USER_ID = 922359954025885737


def deaf_exempt_user_ids() -> tuple[int, ...]:
    """IDs exempt from the "deafened means not credited" rule."""
    raw = (os.environ.get("VOICE_DEAF_EXEMPT_USER_IDS") or "").strip()
    if not raw:
        return (DEAF_TIME_EXEMPT_USER_ID,)
    ids: list[int] = []
    for chunk in raw.replace(";", ",").replace(" ", ",").split(","):
        value = chunk.strip().strip("<>@!")
        if value.isdigit() and int(value) > 0 and int(value) not in ids:
            ids.append(int(value))
    return tuple(ids)
VOICE_ALERT_CHANNEL_ID = 0  # set VOICE_ALERT_CHANNEL_ID to enable alerts
DEAF_WARNING_DELAY_SECONDS = 30.0


def excluded_from_credit(member, channel, state) -> bool:
    """Use the persisted AFK flag for all non-creditable voice time.

    Muted (non-deafened) members remain creditable and tracked; only the AFK
    channel and deafened states (except the exempt user) pause credit.
    """
    return bool(
        channel is not None and member.guild.afk_channel is not None
        and channel.id == member.guild.afk_channel.id
        or member.id not in deaf_exempt_user_ids()
        and state is not None and (state.self_deaf or state.deaf)
    )


def _should_warn_deaf(member, channel, state) -> bool:
    """True when a member is deafened in a non-AFK VC and eligible for warning."""
    if channel is None or state is None:
        return False
    afk_channel = getattr(member.guild, "afk_channel", None)
    if afk_channel is not None and channel.id == afk_channel.id:
        return False
    if member.id in deaf_exempt_user_ids():
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
# Coalescing window for heartbeat writes (seconds). A state change reaches
# Supabase within this window; session starts and ends are written immediately.
DEFAULT_COALESCE_SECONDS = 2.0
# Upper bound on the final shutdown flush (seconds).
DEFAULT_SHUTDOWN_TIMEOUT = 20.0
# How often an in-progress session is committed to voice_daily_stats. This
# bounds how much time a hard crash can lose, and how long an administrative
# Dashboard correction takes to be adopted. One batched RPC covers every live
# session, so the request cost is constant in the number of users.
DEFAULT_CHECKPOINT_SECONDS = 120.0
# A live session whose durable checkpoint is older than this many checkpoint
# intervals is worth a warning: accounting has stopped advancing.
CHECKPOINT_STALE_MULTIPLIER = 4


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
        coalesce_seconds: float = DEFAULT_COALESCE_SECONDS,
        shutdown_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT,
        checkpoint_seconds: float = DEFAULT_CHECKPOINT_SECONDS,
    ) -> None:
        self.bot = bot
        self.db = db
        self.dm_reports_enabled = dm_reports_enabled
        self.alert_channel_id = max(0, int(alert_channel_id))
        self.deaf_warning_seconds = max(0.0, float(deaf_warning_seconds))
        self._renderer = renderer
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.reconcile_interval = max(5.0, float(reconcile_interval))
        # 0 disables coalescing (every state change written immediately).
        self.coalesce_seconds = max(0.0, float(coalesce_seconds))
        self.shutdown_timeout = max(1.0, float(shutdown_timeout))
        # 0 disables periodic checkpointing (time is only durable at close).
        self.checkpoint_seconds = max(0.0, float(checkpoint_seconds))
        self._last_checkpoint_at: datetime | None = None
        self._checkpoint_failures = 0
        self._health_reported_at: datetime | None = None
        self.active_sessions: dict[tuple[int, int], ActiveVoiceSession] = {}
        self._profile_signatures: OrderedDict[
            tuple[int, int], tuple[str, str, str | None]
        ] = OrderedDict()
        # In-memory-only state lock: never held across an await, so Discord
        # events never queue behind network I/O.
        self._state_lock = asyncio.Lock()
        # Serialises active-row writes and closes (ordering + no ghost rows).
        self._persist_lock = asyncio.Lock()
        self._dirty_active: dict[tuple[int, int], PersistedActiveSession] = {}
        self._pending_profiles: dict[tuple[int, int], MemberProfile] = {}
        self._reconciliation_task: asyncio.Task[None] | None = None
        self._dm_report_task: asyncio.Task[None] | None = None
        self._write_flush_task: asyncio.Task[None] | None = None
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
        """Start periodic reconciliation, the write flusher, and DM reporting."""
        if self._reconciliation_task is None or self._reconciliation_task.done():
            self._reconciliation_task = asyncio.create_task(
                self._reconciliation_loop(), name="voice-reconciliation"
            )
        if self._dm_report_task is None or self._dm_report_task.done():
            self._dm_report_task = asyncio.create_task(
                self._dm_report_loop(), name="voice-daily-dm-reports"
            )
        self.start_write_flusher()

    def start_write_flusher(self) -> None:
        """Start (or restart) the coalesced write flusher only."""
        if self.coalesce_seconds <= 0:
            return
        if self._write_flush_task is None or self._write_flush_task.done():
            self._write_flush_task = asyncio.create_task(
                self._write_flush_loop(), name="voice-write-flush"
            )

    async def stop(self, *, timeout: float | None = None) -> None:
        """Flush pending writes, close active sessions, cancel background tasks.

        The flush is bounded (``timeout`` or :attr:`shutdown_timeout`) so a
        hanging database can never block process shutdown indefinitely. If the
        budget is exhausted the pending session time is still durable in the
        store's local mirror/outbox and is reconciled on the next start.
        """
        self._stopped = True
        tasks = [self._reconciliation_task, self._dm_report_task, self._write_flush_task]
        for task in tasks:
            if task is not None:
                task.cancel()
        for task in list(self._deaf_warning_tasks.values()):
            task.cancel()
        pending = [task for task in tasks if task is not None]
        pending.extend(self._deaf_warning_tasks.values())
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._reconciliation_task = None
        self._dm_report_task = None
        self._write_flush_task = None
        self._deaf_warning_tasks.clear()
        self._deaf_warned.clear()

        budget = self.shutdown_timeout if timeout is None else max(1.0, float(timeout))
        try:
            await asyncio.wait_for(self._shutdown_flush(), timeout=budget)
        except asyncio.TimeoutError:
            log.error(
                "Voice shutdown flush exceeded %.0fs; pending session time stays in the "
                "durable local mirror and is reconciled on the next start.",
                budget,
            )
        except Exception:  # noqa: BLE001 - shutdown must always complete
            log.exception("Voice shutdown flush failed")

    async def _shutdown_flush(self) -> None:
        """Write coalesced rows first, then close every open session."""
        async with self._persist_lock:
            now = ensure_aware(self._clock()).astimezone(timezone.utc)
            # Pending heartbeats are not written: each session is about to be
            # closed, which records the same time from the persisted row (or
            # the in-memory fallback) and removes the row. Identity writes are
            # still flushed because nothing else would persist them.
            await self._flush_profiles_locked()
            # Commit the in-progress interval first. If the close below is
            # interrupted the credited time is already durable instead of
            # depending on the shutdown handler completing.
            try:
                await self._checkpoint_sessions_unlocked(now, force=True)
            except Exception as err:  # noqa: BLE001 - shutdown must continue
                log.debug("Shutdown checkpoint skipped: %s", err)
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
            self._dirty_active.clear()

    # ------------------------------------------------------------------
    # Voice State Updates (Section 5)
    # ------------------------------------------------------------------

    async def handle_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Handle Discord voice state transitions: join, leave, move, mute/deaf.

        The in-memory transition is computed first, without any network call.
        Only a new session (written immediately for crash safety) or a session
        end (closed atomically) touches the store synchronously; every other
        transition is coalesced into the next batched heartbeat.
        """
        if member.bot or not member.guild:
            return

        guild_id = member.guild.id
        user_id = member.id
        key = (guild_id, user_id)
        now = ensure_aware(self._clock()).astimezone(timezone.utc)
        excluded = excluded_from_credit(member, after.channel, after)

        # Identity refresh is queued in memory; the batched writer persists it.
        self._queue_member_profile(member)

        closed: ActiveVoiceSession | None = None
        cleared: ActiveVoiceSession | None = None
        started_session: ActiveVoiceSession | None = None
        orphan_close = False

        async with self._state_lock:
            session = self.active_sessions.get(key)
            if after.channel is None:
                self._cancel_deaf_warning(key)
                self.active_sessions.pop(key, None)
                self._dirty_active.pop(key, None)
                if session is not None and not session.is_afk:
                    closed = session
                elif session is not None:
                    cleared = session
                else:
                    orphan_close = True
            else:
                if session is not None and session.is_afk != excluded:
                    if not session.is_afk:
                        closed = session
                    else:
                        cleared = session
                    session = None
                if session is None:
                    session = ActiveVoiceSession(
                        guild_id=guild_id, user_id=user_id,
                        channel_id=after.channel.id, started_at=now, is_afk=excluded,
                    )
                    self.active_sessions[key] = session
                    started_session = session
                else:
                    moved = session.channel_id != after.channel.id
                    if moved:
                        session.channel_id = after.channel.id
                    # A pure mute/deafen toggle does not change any persisted
                    # field, so it is not written at all; the periodic
                    # reconcile heartbeat keeps last_seen_at fresh.
                    if moved or self.coalesce_seconds <= 0:
                        self._queue_active(session, now)
                session.is_muted = bool(after.self_mute or after.mute)
                session.is_deaf = bool(after.self_deaf or after.deaf)

        async with self._persist_lock:
            if closed is not None:
                await self._persist_session(closed, now)
            if cleared is not None:
                await self._clear_active_row(
                    guild_id, user_id, started_before=cleared.started_at
                )
            if after.channel is None:
                if orphan_close:
                    try:
                        await self.db.close_active_session(guild_id, user_id, now)
                    except VoiceStoreError:
                        log.debug("Could not close orphaned active row", exc_info=True)
                return
            if started_session is not None:
                # Crash safety: a brand-new session is durable immediately.
                await self._write_new_session_row(started_session, now)
            elif self.coalesce_seconds <= 0 and self._dirty_active:
                # Coalescing disabled: honour the legacy write-per-event mode.
                await self._flush_active_locked()

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
        """Record a completed session (must hold ``self._persist_lock``).

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
                fallback_credited_until=session.credited_until,
            )
            if recorded is None:
                # No persisted row anywhere — record straight from memory
                # (idempotent on the session identity) and clear any strays.
                await self.db.record_completed_session(
                    guild_id=session.guild_id,
                    user_id=session.user_id,
                    channel_id=session.channel_id,
                    started_at=session.credit_from,
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
    # Coalesced persistence (identities + in-flight sessions)
    #
    # Everything below is called with ``self._persist_lock`` held *unless*
    # the name says otherwise, which is what keeps a delayed heartbeat from
    # ever landing after its session was closed.
    # ------------------------------------------------------------------

    @staticmethod
    def _profile_of(member) -> MemberProfile:
        """Snapshot the identity fields we persist for a guild member."""
        try:
            avatar_url = str(member.display_avatar.url)
        except Exception:  # noqa: BLE001 - avatars are optional
            avatar_url = None
        return MemberProfile(
            guild_id=int(member.guild.id),
            user_id=int(member.id),
            display_name=str(member.display_name),
            username=str(member.name),
            avatar_url=avatar_url,
        )

    def _queue_profile(self, profile: MemberProfile) -> bool:
        """Queue an identity write only when its visible values changed."""
        key = (profile.guild_id, profile.user_id)
        signature = (profile.display_name, profile.username, profile.avatar_url)
        if self._profile_signatures.get(key) == signature:
            self._profile_signatures.move_to_end(key)
            return False
        self._pending_profiles[key] = profile
        while len(self._pending_profiles) > MAX_PROFILE_SIGNATURES:
            self._pending_profiles.pop(next(iter(self._pending_profiles)))
        # Deliberately no wake-up: identity writes are not latency sensitive
        # and waiting for the window keeps them batched behind, not in front
        # of, a join's immediate crash-safety write.
        return True

    def _queue_member_profile(self, member) -> None:
        """Queue a member's last-known identity; best effort, never fatal."""
        try:
            self._queue_profile(self._profile_of(member))
        except Exception:  # noqa: BLE001 - never break an event handler
            log.debug("Could not queue member profile", exc_info=True)

    def _queue_user_profile(self, guild_id: int, user) -> None:
        """Queue identity for a user no longer in the guild (global fallback)."""
        try:
            avatar_url = None
            try:
                avatar_url = str(user.display_avatar.url)
            except Exception:  # noqa: BLE001
                avatar_url = None
            self._queue_profile(
                MemberProfile(
                    guild_id=int(guild_id),
                    user_id=int(user.id),
                    display_name=str(user.display_name),
                    username=str(user.name),
                    avatar_url=avatar_url,
                )
            )
        except Exception:  # noqa: BLE001
            log.debug("Could not queue user profile", exc_info=True)

    @staticmethod
    def _active_snapshot(
        session: ActiveVoiceSession, seen_at: datetime
    ) -> PersistedActiveSession:
        return PersistedActiveSession(
            guild_id=session.guild_id,
            user_id=session.user_id,
            channel_id=session.channel_id,
            started_at=session.started_at,
            last_seen_at=seen_at,
            is_afk=session.is_afk,
            credited_until=session.credited_until,
        )

    def _queue_active(self, session: ActiveVoiceSession, seen_at: datetime) -> None:
        """Coalesce this session's heartbeat (one row per session, last wins)."""
        self._dirty_active[(session.guild_id, session.user_id)] = self._active_snapshot(
            session, seen_at
        )

    async def _write_new_session_row(
        self, session: ActiveVoiceSession, seen_at: datetime
    ) -> None:
        """Write a brand-new session's row immediately (crash safety).

        Must be called with ``self._persist_lock`` held.
        """
        self._dirty_active[(session.guild_id, session.user_id)] = self._active_snapshot(
            session, seen_at
        )
        await self._flush_active_locked()

    def _adopt_durable_state(
        self,
        known: dict[tuple[int, int], tuple[datetime, datetime | None]],
    ) -> None:
        """Reconcile the in-memory sessions with the durable rows.

        Called with the ``started_at`` / ``credited_until`` the database just
        reported for each heartbeated row. The database always wins, because
        after migration 003 it is the only writer of ``started_at`` on an
        existing row — so a difference means either an administrative
        Dashboard correction or a second process advanced the cursor. Either
        way, mirroring it immediately is what stops the displayed total from
        drifting away from what has actually been credited.
        """
        if not known:
            return
        for (guild_id, user_id), (started_at, cursor) in known.items():
            session = self.active_sessions.get((guild_id, user_id))
            if session is None:
                continue
            if started_at != session.started_at:
                log.info(
                    "Adopting durable session start for user %s in guild %s: "
                    "%s -> %s",
                    user_id, guild_id, session.started_at.isoformat(),
                    started_at.isoformat(),
                )
                session.started_at = started_at
            if cursor is not None and (
                session.credited_until is None or cursor > session.credited_until
            ):
                if session.credited_until is not None and cursor > session.credited_until:
                    # Only another writer can have moved the cursor ahead of us.
                    log.warning(
                        "Checkpoint cursor for user %s in guild %s advanced outside "
                        "this process (%s -> %s). If only one ParadiseBOT instance "
                        "should be running, check for an overlapping deployment.",
                        user_id, guild_id, session.credited_until.isoformat(),
                        cursor.isoformat(),
                    )
                session.credited_until = cursor

    async def _flush_active_locked(self) -> int:
        """Write every coalesced heartbeat in ONE batched upsert."""
        if not self._dirty_active:
            return 0
        rows: list[PersistedActiveSession] = []
        for key, row in list(self._dirty_active.items()):
            self._dirty_active.pop(key, None)
            session = self.active_sessions.get(key)
            if (
                session is None
                or session.started_at != row.started_at
                or session.is_afk != row.is_afk
            ):
                # The session ended or restarted after this snapshot was
                # queued: writing it would resurrect a closed row.
                continue
            rows.append(row)
        if not rows:
            return 0
        try:
            known = await self.db.heartbeat_active_sessions(rows)
        except VoiceStoreError as err:
            log.debug("Active session heartbeat failed: %s", err)
            for row in rows:
                self._dirty_active.setdefault((row.guild_id, row.user_id), row)
            return 0
        self._adopt_durable_state(known)
        return len(rows)

    async def _flush_profiles_locked(self) -> int:
        """Persist every queued identity in ONE batched upsert."""
        if not self._pending_profiles:
            return 0
        rows = list(self._pending_profiles.values())
        self._pending_profiles.clear()
        try:
            await self.db.upsert_member_profiles(rows)
        except VoiceStoreError:
            # Not durable yet — keep them queued (bounded) and retry.
            log.debug("Could not persist member profiles", exc_info=True)
            for row in rows:
                self._pending_profiles.setdefault((row.guild_id, row.user_id), row)
            return 0
        for row in rows:
            key = (row.guild_id, row.user_id)
            self._profile_signatures[key] = (
                row.display_name,
                row.username,
                row.avatar_url,
            )
            self._profile_signatures.move_to_end(key)
        while len(self._profile_signatures) > MAX_PROFILE_SIGNATURES:
            self._profile_signatures.popitem(last=False)
        return len(rows)

    async def flush_pending(self) -> int:
        """Force every coalesced write to the store now (tests, shutdown)."""
        async with self._persist_lock:
            active = await self._flush_active_locked()
            profiles = await self._flush_profiles_locked()
        return active + profiles

    async def _flush_profiles(self) -> int:
        async with self._persist_lock:
            return await self._flush_profiles_locked()

    async def _write_flush_loop(self) -> None:
        """Write coalesced heartbeats/identities at most once per window.

        A fixed cadence (rather than "flush on the first event, then again on
        the next") is what actually collapses sustained churn into one batched
        request per ``coalesce_seconds``. Every queued row is durable within
        one window; session starts and ends are still written immediately.
        """
        while not self._stopped:
            try:
                await asyncio.sleep(self.coalesce_seconds)
            except asyncio.CancelledError:
                break
            if self._stopped:
                break
            try:
                await self.flush_pending()
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001 - the flusher must never die
                log.exception("Voice write flush failed")

    async def _clear_active_row(
        self, guild_id: int, user_id: int, *, started_before: datetime | None = None
    ) -> None:
        """Drop a persisted in-progress session row once it has been closed."""
        self._dirty_active.pop((guild_id, user_id), None)
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
        """Scan all guilds and voice channels to recover/synchronize active sessions.

        Two phases: an in-memory scan that never awaits the database, then one
        persistence phase that performs a single batched heartbeat plus any
        closes the scan implied. The expensive part (one RPC per interval)
        is unchanged; what it no longer does is await per member.
        """
        now = ensure_aware(self._clock()).astimezone(timezone.utc)
        connected: set[tuple[int, int]] = set()
        afk_flips: list[ActiveVoiceSession] = []
        stale: list[ActiveVoiceSession] = []

        # Nothing is observable before the gateway has delivered the guild
        # cache. Reconciling now would see an empty server list, conclude every
        # recovered session had ended, and close them at a stale last_seen_at.
        # Waiting costs one interval and cannot lose credited time.
        if not self.bot.guilds:
            log.debug("Voice reconciliation deferred: no guilds cached yet")
            return

        # Phase 1: restart recovery (bounded, once per boot / store bootstrap).
        # The completion flags are set ONLY after recovery actually succeeded,
        # so a store that is still bootstrapping (the normal case: cog_load
        # starts this loop before store.start() finishes) or a transient
        # database failure cannot permanently skip recovery and leave the bot
        # tracking from a fabricated "now" start time.
        async with self._persist_lock:
            store_online = bool(getattr(self.db, "is_bootstrapped", True))
            if not self._recovered or (store_online and not self._recovered_remote):
                async with self._state_lock:
                    try:
                        recovered = await self._recover_persisted_sessions(now)
                    except VoiceStoreError:
                        log.error(
                            "Voice session recovery failed (database unreachable)",
                            exc_info=True,
                        )
                        recovered = False
                    except Exception:  # noqa: BLE001 - never lose the pass
                        log.exception("Voice session recovery failed unexpectedly")
                        recovered = False
                if recovered:
                    self._recovered = True
                    if store_online:
                        self._recovered_remote = True
                        log.info(
                            "Voice session recovery complete: %s active session(s) "
                            "reconciled with Supabase",
                            len(self.active_sessions),
                        )
                else:
                    log.warning(
                        "Voice session recovery incomplete; retrying on the next "
                        "reconcile cycle (no session is closed and no start time "
                        "is rewritten until it succeeds)"
                    )

        # Phase 2: in-memory scan (no I/O, so events never queue behind it).
        async with self._state_lock:
            for guild in self.bot.guilds:
                for channel in guild.voice_channels + getattr(guild, "stage_channels", []):
                    for member in channel.members:
                        if member.bot:
                            continue
                        key = (guild.id, member.id)
                        connected.add(key)
                        is_afk = excluded_from_credit(member, channel, member.voice)

                        self._queue_member_profile(member)

                        session = self.active_sessions.get(key)
                        if session is None:
                            if not self._recovered:
                                # Recovery has not run yet, so the durable row
                                # for this member has not been consulted. Skip
                                # them for this cycle rather than starting the
                                # clock from "now" and throwing away the time
                                # the row already knows about. A user who joins
                                # from now on is handled by the voice-state
                                # event and is unaffected.
                                continue
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
                            if session.is_afk != is_afk:
                                # Creditability changed while we were not
                                # watching: commit the old slice, start fresh.
                                afk_flips.append(session)
                                session = ActiveVoiceSession(
                                    guild_id=guild.id,
                                    user_id=member.id,
                                    channel_id=channel.id,
                                    started_at=now,
                                    is_afk=is_afk,
                                )
                                self.active_sessions[key] = session
                            session.channel_id = channel.id
                            session.is_muted = bool(member.voice.self_mute or member.voice.mute) if member.voice else False
                            session.is_deaf = bool(member.voice.self_deaf or member.voice.deaf) if member.voice else False
                            session.is_afk = is_afk

                        self._sync_deaf_warning(member, channel, member.voice)

            # Fans queued in memory; closed once, below, under the write lock.
            for key in list(self.active_sessions.keys()):
                if key not in connected:
                    self._cancel_deaf_warning(key)
                    self._dirty_active.pop(key, None)
                    stale.append(self.active_sessions.pop(key))

        # Phase 3: persistence (ordered, batched, and never racing a close).
        async with self._persist_lock:
            # Commit the creditable slice of any session whose AFK state
            # changed, then drop the untracked AFK row.
            for previous in afk_flips:
                if not previous.is_afk:
                    await self._persist_session(previous, now)
                else:
                    await self._clear_active_row(
                        previous.guild_id,
                        previous.user_id,
                        started_before=previous.started_at,
                    )

            # Split any ongoing sessions that crossed an IST midnight boundary
            # so pre-midnight hours are committed to the finished day and
            # post-midnight hours accrue cleanly into the new day.
            await self._rollover_midnight_sessions_unlocked(now)

            # Heartbeat every tracked session to the database in ONE batched
            # write (one compact row per user, last_seen_at bumped), so a
            # crash loses at most one heartbeat interval.
            heartbeat_rows = [
                self._active_snapshot(session, now)
                for session in self.active_sessions.values()
            ]
            self._dirty_active.clear()
            try:
                known = await self.db.heartbeat_active_sessions(heartbeat_rows)
                self._adopt_durable_state(known)
            except VoiceStoreError as err:
                # Leave the snapshot queued so the cadence writer retries it
                # instead of waiting for the next reconcile cycle.
                log.debug("Active session heartbeat failed: %s", err)
                for row in heartbeat_rows:
                    self._dirty_active.setdefault(
                        (row.guild_id, row.user_id), row
                    )

            # Commit the in-progress interval so credited time no longer lives
            # only in process memory, and adopt any Dashboard correction.
            await self._checkpoint_sessions_unlocked(now)

            # Identity refresh for everyone seen this scan: one batched upsert.
            await self._flush_profiles_locked()

            # Close sessions for users no longer connected.
            for session in stale:
                if not session.is_afk:
                    await self._persist_session(session, now)
                else:
                    await self._clear_active_row(
                        session.guild_id,
                        session.user_id,
                        started_before=session.started_at,
                    )

    async def rollover_midnight_sessions(self, now: datetime | None = None) -> int:
        """Commit pre-midnight portions of ongoing sessions and reset start to 12:00 AM IST."""
        instant = ensure_aware(now or self._clock()).astimezone(timezone.utc)
        async with self._persist_lock:
            return await self._rollover_midnight_sessions_unlocked(instant)

    async def _rollover_midnight_sessions_unlocked(self, now: datetime) -> int:
        """Midnight split; must be called with ``self._persist_lock`` held."""
        today_start_utc = ist_day_start_utc(now.astimezone(IST).date())
        rolled = 0
        for (guild_id, user_id), session in list(self.active_sessions.items()):
            if session.started_at < today_start_utc <= now:
                if not session.is_afk:
                    # Commits [credit_from, 00:00 IST] and removes the row, so
                    # the finished day keeps exactly its own share.
                    await self._persist_session(session, today_start_utc)
                else:
                    await self._clear_active_row(
                        guild_id, user_id, started_before=session.started_at
                    )
                # Re-open the row at the new day's first instant. The cursor is
                # cleared with it: the row is brand new, so nothing has been
                # credited against it yet.
                session.started_at = today_start_utc
                session.credited_until = None
                self._queue_active(session, now)
                rolled += 1
        if rolled:
            await self._flush_active_locked()
        return rolled

    async def _recover_persisted_sessions(self, now: datetime) -> bool:
        """
        Restore in-flight sessions persisted before a restart/crash.

        - User still in voice  -> resume tracking from the ORIGINAL started_at
          and the ORIGINAL checkpoint cursor (their time is retained exactly,
          as if the restart never happened, and no checkpointed interval is
          credited twice).
        - User gone while down -> close the session at the last heartbeat
          (last time they were confirmed connected).

        Returns ``False`` when the persisted state could not be read, so the
        caller retries instead of marking recovery done. A failed recovery must
        never be mistaken for "there is nothing to recover".
        """
        try:
            persisted = await self.db.get_active_sessions()
        except VoiceStoreError:
            log.error("Could not load persisted voice sessions", exc_info=True)
            return False
        except Exception:  # noqa: BLE001 - recovery must never kill the loop
            log.exception("Unexpected error loading persisted voice sessions")
            return False

        for row in persisted:
            key = (row.guild_id, row.user_id)
            guild = self.bot.get_guild(row.guild_id)

            connected_member = None
            connected_channel_id = None
            currently_afk = False
            if guild is not None:
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
                # Still in voice: resume with the original start time AND the
                # original checkpoint cursor, so the part of the session that
                # was already committed is never credited a second time.
                previous = self.active_sessions.get(key)
                if previous is not None and previous.started_at != row.started_at:
                    log.info(
                        "Recovering session start for user %s in guild %s from the "
                        "durable row (%s) instead of the in-memory value (%s)",
                        row.user_id, row.guild_id, row.started_at.isoformat(),
                        previous.started_at.isoformat(),
                    )
                self.active_sessions[key] = ActiveVoiceSession(
                    guild_id=row.guild_id,
                    user_id=row.user_id,
                    channel_id=connected_channel_id,
                    started_at=row.started_at,
                    credited_until=row.credited_until,
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
        return True

    # ------------------------------------------------------------------
    # Durable checkpointing
    # ------------------------------------------------------------------

    async def _checkpoint_sessions_unlocked(
        self, now: datetime, *, force: bool = False
    ) -> int:
        """Commit ``[cursor, now)`` for every live session, atomically.

        Must be called with ``self._persist_lock`` held.

        This is what stops credited voice time from living only in process
        memory: the interval moves into ``voice_daily_stats`` and the durable
        cursor advances in the same transaction. A crash afterwards can only
        lose the interval since the last successful checkpoint, and a replayed
        or out-of-order checkpoint credits nothing.

        The same round trip echoes each row's own ``started_at``, so an
        administrative Dashboard correction is adopted here too — the tracking
        loop never writes ``started_at`` back over it.
        """
        if self.checkpoint_seconds <= 0:
            return 0
        if not getattr(self.db, "supports_checkpoints", False):
            return 0
        if not force:
            if self._last_checkpoint_at is None:
                # First pass only establishes the cadence. A brand-new session
                # has almost nothing to credit, so checkpointing immediately
                # would spend an RPC on a near-empty interval - and it would
                # set credited_until before an admin had a chance to correct
                # started_at after a bot restart. Crash loss stays bounded by
                # checkpoint_seconds either way.
                self._last_checkpoint_at = now
                return 0
            if (
                now - self._last_checkpoint_at
            ).total_seconds() < self.checkpoint_seconds:
                return 0

        rows = [
            self._active_snapshot(session, now)
            for session in self.active_sessions.values()
            if not session.is_afk
        ]
        if not rows:
            self._last_checkpoint_at = now
            return 0

        try:
            results = await self.db.checkpoint_active_sessions(rows, now)
        except VoiceStoreError as err:
            # Nothing was credited, so no cursor moved. The next checkpoint
            # simply covers a longer interval; nothing is lost or duplicated.
            self._checkpoint_failures += 1
            log.warning(
                "Voice checkpoint failed (%s consecutive); the interval stays "
                "uncredited and will be retried: %s",
                self._checkpoint_failures, err,
            )
            return 0

        self._checkpoint_failures = 0
        # A clock that jumps backwards must not stall checkpointing forever.
        if self._last_checkpoint_at is None or now > self._last_checkpoint_at:
            self._last_checkpoint_at = now

        adopted = 0
        for (guild_id, user_id), (cursor, row_started_at) in results.items():
            session = self.active_sessions.get((guild_id, user_id))
            if session is None:
                continue
            if row_started_at != session.started_at:
                log.info(
                    "Adopting corrected session start for user %s in guild %s: "
                    "%s -> %s",
                    user_id, guild_id, session.started_at.isoformat(),
                    row_started_at.isoformat(),
                )
                session.started_at = row_started_at
                adopted += 1
            if session.credited_until is None or cursor > session.credited_until:
                session.credited_until = cursor
        if adopted:
            log.info("Adopted %s administrative session correction(s)", adopted)
        return len(results)

    async def checkpoint_sessions(self, now: datetime | None = None) -> int:
        """Force a durable checkpoint now (tests, shutdown, manual recovery)."""
        instant = ensure_aware(now or self._clock()).astimezone(timezone.utc)
        async with self._persist_lock:
            return await self._checkpoint_sessions_unlocked(instant, force=True)

    def health_snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        """Cheap operational view of the tracking subsystem.

        Reports the live session count and how stale the durable checkpoints
        are, so a frozen session is visible in logs instead of only on a
        user's leaderboard. Never logs user content.
        """
        instant = ensure_aware(now or self._clock()).astimezone(timezone.utc)
        live = [s for s in self.active_sessions.values() if not s.is_afk]
        uncredited = [
            int((instant - s.credit_from).total_seconds()) for s in live
        ]
        stale_threshold = max(
            60.0, self.checkpoint_seconds * CHECKPOINT_STALE_MULTIPLIER
        )
        stale = [n for n in uncredited if n > stale_threshold]
        return {
            "active_sessions": len(self.active_sessions),
            "creditable_sessions": len(live),
            "recovered": self._recovered,
            "recovered_remote": self._recovered_remote,
            "max_uncredited_seconds": max(uncredited, default=0),
            "stale_sessions": len(stale),
            "checkpoint_interval_seconds": self.checkpoint_seconds,
            "consecutive_checkpoint_failures": self._checkpoint_failures,
        }

    async def report_health(self, now: datetime | None = None) -> dict[str, Any]:
        """Log :meth:`health_snapshot` at most once per checkpoint interval."""
        instant = ensure_aware(now or self._clock()).astimezone(timezone.utc)
        snapshot = self.health_snapshot(instant)
        if not snapshot["creditable_sessions"]:
            return snapshot
        interval = max(60.0, self.checkpoint_seconds)
        if (
            self._health_reported_at is not None
            and (instant - self._health_reported_at).total_seconds() < interval
        ):
            return snapshot
        self._health_reported_at = instant
        if snapshot["stale_sessions"] or snapshot["consecutive_checkpoint_failures"]:
            log.warning(
                "Voice tracking health: %s live session(s), oldest uncredited "
                "interval %ss, %s stale, %s consecutive checkpoint failure(s), "
                "recovered=%s/%s",
                snapshot["creditable_sessions"],
                snapshot["max_uncredited_seconds"],
                snapshot["stale_sessions"],
                snapshot["consecutive_checkpoint_failures"],
                snapshot["recovered"],
                snapshot["recovered_remote"],
            )
        else:
            log.info(
                "Voice tracking healthy: %s live session(s), oldest uncredited "
                "interval %ss, checkpoint every %.0fs",
                snapshot["creditable_sessions"],
                snapshot["max_uncredited_seconds"],
                self.checkpoint_seconds,
            )
        return snapshot

    async def _reconciliation_loop(self) -> None:
        """Periodic background reconciliation (default: every 20 seconds)."""
        await asyncio.sleep(5)  # initial brief delay on startup
        while not self._stopped:
            try:
                await self.reconcile_guilds()
                # Throttled internally to at most one line per checkpoint
                # interval, so a healthy server stays quiet.
                await self.report_health()
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

        # 2. Add only the part of each live session that has NOT been credited
        #    yet, clipped to this exact IST window. Everything up to the
        #    durable checkpoint cursor is already in the rows fetched above, so
        #    counting from session.credit_from (rather than started_at) is what
        #    keeps "committed + live" equal to the true session duration
        #    instead of double-counting the checkpointed prefix.
        for (g_id, u_id), session in self.active_sessions.items():
            if g_id == guild_id and not session.is_afk:
                elapsed = period_overlap_seconds(
                    session.credit_from, now, period.value, today_date, now=now
                )
                if elapsed:
                    time_map[u_id] = time_map.get(u_id, 0) + elapsed

        # 3. Resolve identities for everyone on the board (cache -> HTTP -> DB).
        #    The hydration step already reads (and returns) the persisted
        #    profiles, so a render costs one profile read, not two.
        if hydrate:
            profiles = await self.hydrate_member_profiles(guild, time_map.keys())
        else:
            try:
                profiles = await self.db.get_member_profiles(
                    guild_id, list(time_map.keys())
                )
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
                # Keep the profile fresh so restarts remember this member.
                # Queued in memory; one batched upsert is flushed below.
                self._queue_member_profile(guild_member)
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

        if members:
            # Freshly seen identities are written once per render, batched.
            await self._flush_profiles()

        return members

    async def hydrate_member_profiles(
        self,
        guild: discord.Guild,
        user_ids: Iterable[int],
        *,
        time_budget: float = 2.5,
    ) -> dict[int, MemberProfile]:
        """
        Make sure we know the name/avatar of every given user.

        Order of preference: live member cache, persisted profile, then an
        HTTP fetch (member -> global user) whose result is persisted so it
        is never fetched again and survives restarts.

        Bounded to ``time_budget`` seconds so an interaction response is
        never delayed past Discord's window; every successful fetch is
        persisted, so remaining users resolve on the following render.

        Returns the persisted/known profiles so the caller never has to read
        the same rows a second time.
        """
        ids = [int(u) for u in user_ids]
        if not ids:
            return {}

        try:
            known = await self.db.get_member_profiles(guild.id, ids)
        except VoiceStoreError:
            known = {}

        pending = [u for u in ids if guild.get_member(u) is None and u not in known]
        deadline = time.monotonic() + time_budget
        fetched = False
        for u_id in pending[:MAX_HYDRATE_FETCHES]:
            if time.monotonic() > deadline:
                break
            try:
                member = await guild.fetch_member(u_id)
                profile = self._profile_of(member)
                self._queue_profile(profile)
                known[u_id] = profile
                fetched = True
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
            self._queue_user_profile(guild.id, user)
            known[u_id] = MemberProfile(
                guild_id=guild.id,
                user_id=u_id,
                display_name=str(user.display_name),
                username=str(user.name),
                avatar_url=str(user.avatar_url) if user.avatar_url else None,
            )
            fetched = True

        if fetched or self._pending_profiles:
            # Durable before returning: the next render must not re-fetch.
            await self._flush_profiles()
        return known

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
            # Uncredited remainder only; the checkpointed prefix is already in
            # the breakdown returned by the database.
            credit_from = active_session.credit_from
            active_today = period_overlap_seconds(
                credit_from, now, "today", today_date, now=now
            )
            active_yesterday = period_overlap_seconds(
                credit_from, now, "yesterday", today_date, now=now
            )
            active_week = period_overlap_seconds(
                credit_from, now, "week", today_date, now=now
            )
            active_month = period_overlap_seconds(
                credit_from, now, "month", today_date, now=now
            )
            active_all = period_overlap_seconds(
                credit_from, now, "all_time", today_date, now=now
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
        file: discord.File | None = None
        try:
            image_bytes = await self.renderer.render_leaderboard(
                guild_name=guild.name,
                period=VoicePeriod.TODAY,
                members=members,
                date_str=short_date,
                page=1,
                page_size=10,
                # The day it reports on, named the way a calendar names it.
                title_override=target_date.strftime("%A"),
                subtitle_override=(
                    f"{len(members)} {'person' if len(members) == 1 else 'people'}"
                ),
            )
            file = discord.File(
                io.BytesIO(image_bytes), filename="daily_vc_leaderboard.png"
            )
            # The card already carries the day, the server and the total, so
            # the embed only adds a line when there is something to say.
            embed = leaderboard_image_embed(
                guild,
                VoicePeriod.TODAY,
                filename="daily_vc_leaderboard.png",
                caption=(
                    None
                    if members
                    else "Nobody was in voice that day."
                ),
            )
        except Exception:
            log.warning(
                "Could not render daily VC leaderboard image; using text embed",
                exc_info=True,
            )
            embed = build_leaderboard_text_embed(
                guild, VoicePeriod.TODAY, members, page=1, page_size=10
            )
            embed.title = date_display
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
            # Uncredited remainder only — the checkpointed prefix is already in
            # the daily rows fetched below, so adding the whole session here
            # would double-count it in the report.
            credit_from = session.credit_from
            today_seconds = period_overlap_seconds(
                credit_from, now, "today", target_date, now=now
            )
            if today_seconds:
                live_today[(guild_id, user_id)] = today_seconds
            previous_seconds = period_overlap_seconds(
                credit_from, now, "today", day_before, now=now
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

        # One batched sent-mark lookup per guild instead of one query per
        # member. The durable mark-before-send rule is unchanged below.
        sent_marks: dict[int, set[int]] = {}
        sent_candidates: dict[int, list[int]] = {}
        for stat in stats_list:
            if stat.guild_id in blocked_guilds:
                continue
            sent_candidates.setdefault(stat.guild_id, []).append(stat.user_id)
        for guild_id, user_ids in sent_candidates.items():
            try:
                sent_marks[guild_id] = await self.db.get_sent_dm_reports(
                    target_date_str, guild_id, user_ids
                )
            except VoiceStoreUnavailable as err:
                log.warning(
                    "Daily DM reports for guild %s postponed: %s", guild_id, err
                )
                blocked_guilds.add(guild_id)
                blocked += 1

        # One local settings query instead of one per candidate.
        try:
            preferences = await self.db.get_user_dm_preferences(
                [stat.user_id for stat in stats_list]
            )
        except VoiceStoreError:
            log.debug("Could not read DM preferences; assuming enabled", exc_info=True)
            preferences = {}

        for stat in stats_list:
            guild_id = stat.guild_id
            user_id = stat.user_id

            if guild_id in blocked_guilds:
                continue

            if user_id in sent_marks.get(guild_id, ()):
                continue

            # Check user preference
            if not preferences.get(user_id, True):
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

            # Where this person landed that day, and how big the room was.
            guild_board = board_cache.get(guild_id, [])
            user_rank = next(
                (e.rank for e in guild_board if e.user_id == user_id), None
            )
            people = len(guild_board)

            # The same figure for the day before, so the DM can compare.
            prev_board = prev_board_cache.get(guild_id, [])
            day_before_sec = next(
                (e.total_seconds for e in prev_board if e.user_id == user_id), None
            )

            # A short, human report rather than a data dump.
            dm_embed = build_daily_dm_embed(
                guild_name=getattr(guild, "name", "your server"),
                target_date=target_date,
                seconds=stat.duration_seconds,
                rank=user_rank,
                people=people,
                server_total_seconds=total_cache.get(guild_id, 0),
                previous_seconds=day_before_sec,
            )

            # Mark BEFORE sending: the mark is durable (direct write or the
            # local outbox), so a crash between mark and send can never cause
            # a duplicate DM on retry.
            await self.db.mark_dm_report_sent(target_date_str, guild_id, user_id)

            try:
                await user.send(embed=dm_embed)
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
