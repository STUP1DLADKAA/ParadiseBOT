"""Permanent voice-state enforcement: Paradise stays server-muted, never deafened.

Two facts about Discord voice make this deliberate design, not an accident:

* **Server mute** (``guild.me.edit(mute=True)``) stops the bot *transmitting*
  to listeners. Paradise is an eavesdropping clipping bot — it should never be
  audible, so the muted state is the desired state and is restored whenever
  someone clears it.
* **Server deafen** stops the bot *receiving* — Discord does not deliver any
  RTP at all to a server-deafened client, which silently kills ``/clip``.
  Paradise therefore never deafens itself, and removes a server deafen the
  moment it appears.

Self mute/self deafen flags are left alone: they are per-client cosmetic flags
(this bot joins with both off) and are *not* what moderators toggle.

How enforcement stays loop- and rate-limit-safe:

* The desired state is a constant: ``muted=True, deafened=False``.
* ``on_voice_state_update`` deduplicates identical (before, after) mute/deaf
  pairs, so the bot's *own* enforcement edit never re-triggers anything and
  repeated voice events (channel hops, self-flag flips) cost zero API calls.
* Edits are coalesced per guild behind a short debounce, guarded by an
  in-flight flag, and skipped entirely when the live state already matches.
* Failures back off exponentially (capped) with bounded retries; a guild
  missing the ``Mute Members`` permission is logged once and never retried
  until permissions change.
* A slow periodic sweep re-asserts the desired state, covering missed events
  and voice-session recoveries without relying on any one-time startup action.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import discord
from discord.ext import commands, tasks

log = logging.getLogger(__name__)

#: Debounce window that coalesces bursts of voice-state updates per guild.
ENFORCE_DEBOUNCE_SECONDS: float = 1.0
#: Backoff schedule (seconds) after a failed enforcement attempt.
ENFORCE_BACKOFF_SECONDS: tuple[float, ...] = (2.0, 8.0, 30.0)
#: Failed-edit attempts within one enforcement pass before deferring to a
#: delayed retry (keeps a persistently failing API call from hot-looping).
MAX_ATTEMPTS_PER_PASS: int = 3
#: How often the safety net re-asserts the desired state across all guilds.
SWEEP_INTERVAL_SECONDS: float = 60.0
#: bots need Mute Members to (un)mute/(un)deafen *other* members — including
#: themselves, because the bot's member is edited like any other member.
REQUIRED_PERMISSION: str = "mute_members"


@dataclass(slots=True)
class _GuildEnforcementState:
    applied_mute: bool | None = None
    applied_deafen: bool | None = None
    inflight: bool = False
    pending: bool = False
    retry_task: asyncio.Task | None = None
    failures: int = 0
    last_signature: tuple[bool, bool] | None = None
    missing_perm_logged: bool = False
    loop_warned: bool = False
    diagnostics: list[str] = field(default_factory=list)


class VoiceEnforcement(commands.Cog):
    """Keep Paradise server-muted and server-undeafened, permanently."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._states: dict[int, _GuildEnforcementState] = {}
        self._tasks: set[asyncio.Task] = set()

    async def cog_load(self) -> None:
        self._sweep.start()

    async def cog_unload(self) -> None:
        self._sweep.cancel()
        for state in self._states.values():
            if state.retry_task is not None and not state.retry_task.done():
                state.retry_task.cancel()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def enforce_now(self, guild: discord.Guild) -> None:
        """Assert the desired voice state immediately (used by recovery paths)."""
        await self._apply_desired(guild, reason="enforced recovery")

    async def wait_for_idle(self, guild_id: int, timeout: float = 5.0) -> None:
        """Wait until no enforcement edit is in flight for ``guild_id``."""
        state = self._states.get(guild_id)
        if state is None:
            return
        async with asyncio.timeout(timeout):
            while state.inflight:
                await asyncio.sleep(0.05)

    def note_external_join(self, guild: discord.Guild) -> None:
        """Re-check a guild after some other component (re)joined voice."""
        self._schedule_enforce(guild, immediate=True)

    # ------------------------------------------------------------------
    # Discord events
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if self.bot.user is None or member.id != self.bot.user.id:
            return
        guild = getattr(member, "guild", None)
        if guild is None:
            return

        mute = bool(getattr(after, "mute", False))
        deaf = bool(getattr(after, "deaf", False))

        # Deduplicate: the bot's own enforcement edit (and every unrelated
        # state flip) produces an identical mute/deaf pair — nothing to do.
        signature = (mute, deaf)
        state = self._states.setdefault(guild.id, _GuildEnforcementState())
        if state.last_signature == signature:
            return
        state.last_signature = signature

        if mute and deaf:
            log.warning(
                "Paradise was server-muted AND server-deafened in guild %s — "
                "restoring the permanent state (muted, undeafened)",
                guild.id,
            )
        elif deaf:
            log.warning(
                "Someone server-deafened Paradise in guild %s — removing it "
                "(a deafened bot receives no voice audio at all)",
                guild.id,
            )
        elif not mute:
            log.info(
                "Someone server-unmuted Paradise in guild %s — restoring the "
                "permanent server mute",
                guild.id,
            )
        self._schedule_enforce(guild)

    # ------------------------------------------------------------------
    # Enforcement core
    # ------------------------------------------------------------------

    def _schedule_enforce(
        self, guild: discord.Guild, *, immediate: bool = False
    ) -> None:
        state = self._states.setdefault(guild.id, _GuildEnforcementState())
        if state.inflight:
            # An edit is running; remember that state may have changed under
            # it and let the post-edit re-check schedule what is needed.
            state.pending = True
            return
        if state.retry_task is not None and not state.retry_task.done():
            if not immediate:
                return  # a backoff retry is already scheduled
            state.retry_task.cancel()

        task = asyncio.create_task(
            self._debounced_enforce(guild, immediate=immediate),
            name=f"paradise-voice-enforce-{guild.id}",
        )
        state.retry_task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _debounced_enforce(
        self, guild: discord.Guild, *, immediate: bool = False
    ) -> None:
        try:
            if not immediate:
                await asyncio.sleep(ENFORCE_DEBOUNCE_SECONDS)
            await self._apply_desired(guild, reason="voice-state update")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - enforcement must never crash dispatch
            log.debug("Voice enforcement pass failed", exc_info=True)

    def _schedule_retry(self, guild: discord.Guild, delay: float) -> None:
        state = self._states.setdefault(guild.id, _GuildEnforcementState())

        async def _retry_later() -> None:
            try:
                await asyncio.sleep(delay)
                await self._apply_desired(guild, reason="delayed retry")
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                log.debug("Delayed voice-enforcement retry failed", exc_info=True)

        if state.retry_task is not None and not state.retry_task.done():
            return
        task = asyncio.create_task(
            _retry_later(), name=f"paradise-voice-enforce-retry-{guild.id}"
        )
        state.retry_task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _apply_desired(self, guild: discord.Guild, *, reason: str) -> None:
        """Make the live voice state match *muted, undeafened* — once."""
        state = self._states.setdefault(guild.id, _GuildEnforcementState())
        if state.inflight:
            state.pending = True
            return
        state.inflight = True
        try:
            while True:
                state.pending = False
                me = guild.me
                voice = getattr(me, "voice", None) if me is not None else None
                if me is None or voice is None or voice.channel is None:
                    # Not in voice: nothing to enforce. Reset so a future
                    # join starts clean.
                    state.applied_mute = None
                    state.applied_deafen = None
                    state.failures = 0
                    return

                live_mute = bool(getattr(voice, "mute", False))
                live_deaf = bool(getattr(voice, "deaf", False))
                state.applied_mute = live_mute
                state.applied_deafen = live_deaf

                if live_mute and not live_deaf:
                    state.failures = 0
                    return  # already the desired state

                if not getattr(getattr(me, "guild_permissions", None), REQUIRED_PERMISSION, False):
                    if not state.missing_perm_logged:
                        state.missing_perm_logged = True
                        log.error(
                            "Cannot enforce Paradise's permanent voice state in "
                            "guild %s: the bot is missing the 'Mute Members' "
                            "permission. Grant it (Server Settings → Roles) so "
                            "Paradise can keep itself server-muted and remove "
                            "server deafens.",
                            guild.id,
                        )
                    return

                try:
                    await me.edit(
                        mute=True,
                        deafen=False,
                        reason=f"Paradise permanent voice-state enforcement ({reason})",
                    )
                except (discord.Forbidden, discord.HTTPException) as err:
                    state.failures += 1
                    if state.failures == 1:
                        log.warning(
                            "Voice-state enforcement edit failed in guild %s: %s",
                            guild.id,
                            err.__class__.__name__,
                        )
                    delay = ENFORCE_BACKOFF_SECONDS[
                        min(state.failures - 1, len(ENFORCE_BACKOFF_SECONDS) - 1)
                    ]
                    if state.failures >= MAX_ATTEMPTS_PER_PASS:
                        # Give up for now; a bounded, backoff-delayed retry
                        # re-asserts the state later without hot-looping.
                        log.debug(
                            "Voice enforcement giving up after %d attempts in "
                            "guild %s; retrying in %.0fs",
                            state.failures,
                            guild.id,
                            delay,
                        )
                        self._schedule_retry(guild, delay)
                        return
                    await asyncio.sleep(delay)
                    continue  # re-read live state and try again
                except asyncio.CancelledError:
                    raise

                state.applied_mute = True
                state.applied_deafen = False
                state.failures = 0
                log.info(
                    "Enforced Paradise's permanent voice state in guild %s "
                    "(server-muted, server-undeafened)",
                    guild.id,
                )
                if state.pending:
                    # Something changed while we were editing; loop once more.
                    continue
                return
        finally:
            state.inflight = False
            if state.pending:
                self._schedule_enforce(guild)

    # ------------------------------------------------------------------
    # Safety net
    # ------------------------------------------------------------------

    @tasks.loop(seconds=SWEEP_INTERVAL_SECONDS)
    async def _sweep(self) -> None:
        """Re-assert the desired state everywhere (missed events, reconnects)."""
        for guild in list(self.bot.guilds):
            me = guild.me
            voice = getattr(me, "voice", None) if me is not None else None
            if me is None or voice is None or voice.channel is None:
                continue
            state = self._states.setdefault(guild.id, _GuildEnforcementState())
            live_mute = bool(getattr(voice, "mute", False))
            live_deaf = bool(getattr(voice, "deaf", False))
            state.applied_mute = live_mute
            state.applied_deafen = live_deaf
            if live_mute and not live_deaf:
                state.failures = 0
                continue
            if state.inflight:
                continue
            log.info(
                "Voice-state sweep re-asserting the permanent state in guild %s",
                guild.id,
            )
            await self._apply_desired(guild, reason="periodic sweep")

    @_sweep.before_loop
    async def _before_sweep(self) -> None:
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:
            # Offline test harnesses load cogs without logging the client in.
            self._sweep.cancel()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VoiceEnforcement(bot))
