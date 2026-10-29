"""Coordinated mini-game lifecycle, scheduler, and restart reconciliation."""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import discord

from functions.helpers import Colour
from functions.startup import PRIMARY_GUILD_ID

from .base import GameDefinition
from .models import (
    GameConfig,
    GameOutcome,
    GameSnapshot,
    GameState,
    VALID_TRANSITIONS,
    utc_iso,
)
from .registry import available_games, get_game
from .store import MiniGameStore
from .ui import RegistrationView

log = logging.getLogger(__name__)

SUCCESS_DELAY = timedelta(minutes=30)
UNSUCCESSFUL_DELAY = timedelta(minutes=20)
INITIAL_DELAY = timedelta(minutes=30)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


class MiniGameContext:
    """Runtime interaction state for exactly one persisted active game."""

    def __init__(
        self,
        manager: "MiniGameManager",
        snapshot: GameSnapshot,
        definition: GameDefinition,
        channel: discord.abc.Messageable,
        *,
        message: discord.Message | None = None,
    ) -> None:
        self.manager = manager
        self.snapshot = snapshot
        self.definition = definition
        self.channel = channel
        self.message = message
        self.answer_seconds = snapshot.answer_seconds
        self.active_participants: set[int] = set()
        self._winners: set[int] = set()
        self._input_lock = asyncio.Lock()
        self._condition = asyncio.Condition()
        self._attempts: dict[tuple[int, int], int] = {}
        self._round_responders: dict[int, set[int]] = {}
        self._round_in_flight: dict[int, set[int]] = {}
        self.cancel_event = asyncio.Event()

    @property
    def participant_ids(self) -> tuple[int, ...]:
        return tuple(sorted(set(self.snapshot.participant_ids)))

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    @property
    def winners(self) -> tuple[int, ...]:
        return tuple(sorted(self._winners))

    def scores(self) -> dict[int, int]:
        return {int(user_id): int(points) for user_id, points in self.snapshot.scores.items()}

    async def set_round(self, round_number: int, payload: dict[str, Any] | None = None) -> None:
        self.snapshot.current_round = int(round_number)
        if payload is not None:
            # Only non-secret, non-identity round configuration belongs here.
            self.snapshot.payload = dict(payload)
        await self.manager.persist_active(self.snapshot)

    async def claim_response(
        self, user_id: int, round_number: int, *, max_attempts: int = 1
    ) -> tuple[bool, int]:
        """Atomically accept at most N inputs by an enrolled player per round."""
        user_id = int(user_id)
        async with self._input_lock:
            if (
                self.cancelled
                or self.snapshot.state != GameState.IN_PROGRESS
                or self.snapshot.current_round != int(round_number)
                or user_id not in self.snapshot.participant_ids
            ):
                return False, 0
            key = (int(round_number), user_id)
            used = self._attempts.get(key, 0)
            if used >= max(1, int(max_attempts)):
                return False, used
            used += 1
            self._attempts[key] = used
            self.active_participants.add(user_id)
            self._round_responders.setdefault(int(round_number), set()).add(user_id)
            self._round_in_flight.setdefault(int(round_number), set()).add(user_id)
            return True, used

    async def response_finished(self, user_id: int, round_number: int) -> None:
        async with self._condition:
            self._round_in_flight.setdefault(int(round_number), set()).discard(int(user_id))
            self._condition.notify_all()

    async def add_points(self, user_id: int, points: int) -> None:
        user_id, points = int(user_id), max(0, int(points))
        if user_id not in self.snapshot.participant_ids or self.cancelled:
            return
        self.snapshot.scores[str(user_id)] = int(self.snapshot.scores.get(str(user_id), 0)) + points
        await self.manager.persist_active(self.snapshot)
        async with self._condition:
            self._condition.notify_all()

    async def claim_winner(self, user_id: int, *, points: int = 0) -> bool:
        """Claim a first-to-solve win and persist its points, never its identity."""
        user_id, points = int(user_id), max(0, int(points))
        async with self._input_lock:
            if (
                self.cancelled
                or self.snapshot.state != GameState.IN_PROGRESS
                or user_id not in self.snapshot.participant_ids
                or self._winners
            ):
                return False
            self._winners.add(user_id)
            self.active_participants.add(user_id)
            if points:
                self.snapshot.scores[str(user_id)] = int(self.snapshot.scores.get(str(user_id), 0)) + points
                await self.manager.persist_active(self.snapshot)
            async with self._condition:
                self._condition.notify_all()
            return True

    async def wait_for_round(self, timeout: float) -> bool:
        round_number = self.snapshot.current_round
        target_count = len(self.participant_ids)
        async with self._condition:
            try:
                await asyncio.wait_for(
                    self._condition.wait_for(
                        lambda: self.cancelled
                        or (
                            len(self._round_responders.get(round_number, set())) >= target_count
                            and not self._round_in_flight.get(round_number, set())
                        )
                    ),
                    timeout=max(0.0, float(timeout)),
                )
            except asyncio.TimeoutError:
                pass
            try:
                await asyncio.wait_for(
                    self._condition.wait_for(
                        lambda: not self._round_in_flight.get(round_number, set())
                    ),
                    timeout=2.0,
                )
            except asyncio.TimeoutError:
                log.warning("An answer callback did not finish before round cleanup")
        return not self.cancelled

    async def wait_for_winner(self, timeout: float) -> bool:
        async with self._condition:
            try:
                await asyncio.wait_for(
                    self._condition.wait_for(lambda: self.cancelled or bool(self._winners)),
                    timeout=max(0.0, float(timeout)),
                )
            except asyncio.TimeoutError:
                pass
        return bool(self._winners) and not self.cancelled

    async def notify_cancel(self) -> None:
        self.cancel_event.set()
        async with self._condition:
            self._condition.notify_all()

    async def update_game(
        self,
        title: str,
        description: str,
        *,
        view: discord.ui.View | None = None,
        footer: str | None = None,
    ) -> None:
        embed = discord.Embed(
            title=f"🌴 PARADISE MINI-GAME · {title}",
            description=description,
            colour=Colour.ACCENT,
        )
        embed.add_field(
            name="Players",
            value=f"{len(self.snapshot.participant_ids)} / {self.snapshot.maximum_players}",
            inline=True,
        )
        if footer:
            embed.set_footer(text=footer)
        await self.manager.edit_current(self, embed=embed, view=view)


class MiniGameManager:
    """One timestamp-driven scheduler and state machine for Paradise's home guild."""

    def __init__(
        self,
        bot,
        *,
        store: MiniGameStore | None = None,
        guild_id: int | None = None,
        clock: Callable[[], datetime] | None = None,
        chooser: Callable[[tuple[GameDefinition, ...]], GameDefinition] | None = None,
    ) -> None:
        self.bot = bot
        config = getattr(bot, "config", None)
        voice_cfg = getattr(config, "voice", None)
        db_path = getattr(voice_cfg, "db_path", "") or None
        self.store = store or MiniGameStore(db_path)
        synchronizer = getattr(bot, "synchronizer", None)
        configured_guild = getattr(synchronizer, "guild_id", PRIMARY_GUILD_ID)
        self.guild_id = int(guild_id or configured_guild)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._chooser = chooser or random.choice
        self._scheduler_task: asyncio.Task[None] | None = None
        self._active_task: asyncio.Task[None] | None = None
        self._active_context: MiniGameContext | None = None
        self._scheduler_wake = asyncio.Event()
        self._recovery_gate = asyncio.Event()
        self._recovery_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._prepared = False
        self._recovery_complete = False
        self._pending_recovery: GameSnapshot | None = None

    def now(self) -> datetime:
        value = self._clock()
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

    async def prepare(self) -> None:
        """Load durable state before scheduling; never reset an existing timestamp."""
        if self._prepared:
            return
        self.store.ensure_guild(
            self.guild_id,
            now=self.now(),
            initial_delay_seconds=int(INITIAL_DELAY.total_seconds()),
        )
        try:
            self._pending_recovery = self.store.get_active_game(self.guild_id)
        except RuntimeError:
            log.exception("Mini-game snapshot is corrupt; reconciling as interrupted")
            self.store.fail_corrupt_active(
                self.guild_id,
                completed_at=self.now(),
                next_game_at=self.now() + UNSUCCESSFUL_DELAY,
            )
            self._pending_recovery = None
            self._recovery_complete = True
            self._recovery_gate.set()
        if self._pending_recovery is not None:
            presence = getattr(self.bot, "presence_manager", None)
            if presence is not None:
                await presence.set_game_active(True)
            log.info(
                "Recovering interrupted mini-game guild=%s game=%s state=%s",
                self.guild_id,
                self._pending_recovery.game_key,
                self._pending_recovery.state.value,
            )
        else:
            self._recovery_complete = True
            self._recovery_gate.set()
        self._prepared = True

    def start(self) -> None:
        """Start the single scheduler worker. Safe to call repeatedly."""
        if self._scheduler_task is None or self._scheduler_task.done():
            self._scheduler_task = asyncio.create_task(
                self._scheduler_loop(), name="paradise-minigame-scheduler"
            )

    async def stop(self) -> None:
        """Stop this process cleanly while leaving any game recoverable on disk."""
        task, self._scheduler_task = self._scheduler_task, None
        if task is not None:
            task.cancel()
        active, self._active_task = self._active_task, None
        if active is not None:
            active.cancel()
        await asyncio.gather(*(t for t in (task, active) if t is not None), return_exceptions=True)

    async def recover_after_restart(self) -> None:
        """Mark an interrupted live game failed; it is never silently replayed."""
        async with self._recovery_lock:
            if self._recovery_complete:
                self._recovery_gate.set()
                return
            snapshot = self._pending_recovery
            if snapshot is None:
                self._recovery_complete = True
                self._recovery_gate.set()
                return

            channel = await self._fetch_channel(snapshot.channel_id)
            try:
                game_name = get_game(snapshot.game_key).name
            except ValueError:
                game_name = "The mini-game"
            embed = discord.Embed(
                title="🌴 PARADISE MINI-GAME · Interrupted",
                description=(
                    f"**{game_name}** was interrupted by a bot restart.\n\n"
                    "No result or rewards were recorded. The next challenge has been scheduled."
                ),
                colour=Colour.ACCENT,
            )
            await self._deliver_recovery_notice(channel, snapshot.message_id, embed)

            finished = self.now()
            self.store.finish_game(
                guild_id=self.guild_id,
                game_id=snapshot.game_id,
                status=GameState.FAILED,
                completed_at=finished,
                next_game_at=finished + UNSUCCESSFUL_DELAY,
            )
            presence = getattr(self.bot, "presence_manager", None)
            if presence is not None:
                await presence.set_game_active(False)
            self._pending_recovery = None
            self._recovery_complete = True
            self._recovery_gate.set()
            self._scheduler_wake.set()
            log.info("Interrupted mini-game recovered; next attempt scheduled after 20 minutes")

    async def _deliver_recovery_notice(
        self, channel: discord.abc.Messageable | None, message_id: int | None, embed: discord.Embed
    ) -> None:
        if channel is None:
            log.warning("Could not find channel for interrupted mini-game announcement")
            return
        allowed = discord.AllowedMentions.none()
        if message_id is not None:
            try:
                message = await channel.fetch_message(message_id)  # type: ignore[attr-defined]
                await message.edit(content="", embed=embed, view=None, allowed_mentions=allowed)
                return
            except discord.NotFound:
                pass
            except (discord.DiscordException, AttributeError):
                log.warning("Could not edit interrupted mini-game announcement", exc_info=True)
        try:
            await channel.send(embed=embed, allowed_mentions=allowed)
        except discord.DiscordException:
            log.warning("Could not send interrupted mini-game notice", exc_info=True)

    async def _fetch_channel(self, channel_id: int):
        channel = self.bot.get_channel(int(channel_id))
        if channel is not None:
            return channel
        try:
            return await self.bot.fetch_channel(int(channel_id))
        except (discord.DiscordException, AttributeError):
            return None

    async def start_game(
        self,
        *,
        game_key: str | None = None,
        channel: discord.abc.Messageable | None = None,
        manual: bool = False,
    ) -> tuple[bool, str]:
        """Reserve and announce one game, with the role ping only on first send."""
        async with self._start_lock:
            existing = self.store.get_active_game(self.guild_id)
            if existing is not None:
                return False, "a mini-game is already active."
            guild = self.bot.get_guild(self.guild_id)
            if guild is None:
                return False, "the configured Paradise server is not available right now."
            target_channel = channel or await self.resolve_announcement_channel(guild)
            if target_channel is None or not isinstance(target_channel, discord.abc.Messageable):
                return False, "I couldn't find a sendable mini-game channel. set MINIGAMES_CHANNEL_ID or configure a server text channel."
            game = get_game(game_key) if game_key else self._chooser(available_games())
            if not game.enabled:
                return False, "that mini-game is currently disabled."

            config = self.store.get_config(self.guild_id)
            now = self.now()
            snapshot = GameSnapshot(
                game_id=uuid.uuid4().hex,
                guild_id=self.guild_id,
                channel_id=int(target_channel.id),
                message_id=None,
                game_key=game.key,
                state=GameState.SCHEDULED,
                created_at=utc_iso(now),
                registration_deadline=None,
                registration_seconds=config.registration_seconds,
                minimum_players=config.minimum_players,
                maximum_players=config.maximum_players,
                answer_seconds=config.answer_seconds,
                participant_ids=[],
                scores={},
            )
            if not self.store.start_active_game(snapshot, now):
                return False, "another game has already reserved the scheduler."

            context = MiniGameContext(self, snapshot, game, target_channel)
            self._active_context = context
            presence = getattr(self.bot, "presence_manager", None)
            if presence is not None:
                await presence.set_game_active(True)

            try:
                await self._transition(context, GameState.ANNOUNCING)
                role = await self._find_minigames_role(guild)
                countdown_at = now + timedelta(seconds=config.registration_seconds)
                embed = self.registration_embed(snapshot, game, deadline=countdown_at)
                view = RegistrationView(self, snapshot.game_id, game.rules)
                allowed = (
                    discord.AllowedMentions(roles=[role], users=False, everyone=False)
                    if role is not None
                    else discord.AllowedMentions.none()
                )
                content = role.mention if role is not None else None
                context.message = await target_channel.send(
                    content=content,
                    embed=embed,
                    view=view,
                    allowed_mentions=allowed,
                )
                snapshot.message_id = int(context.message.id)
                registration_start = self.now()
                snapshot.registration_deadline = utc_iso(
                    registration_start + timedelta(seconds=snapshot.registration_seconds)
                )
                await self._transition(context, GameState.REGISTRATION)
                await self.edit_current(
                    context,
                    embed=self.registration_embed(
                        snapshot, game, deadline=_parse_time(snapshot.registration_deadline)
                    ),
                    view=view,
                )
                self._active_task = asyncio.create_task(
                    self._execute_game(context),
                    name=f"paradise-minigame-{game.key}",
                )
                self._scheduler_wake.set()
                log.info(
                    "Mini-game started guild=%s game=%s manual=%s players=0",
                    self.guild_id,
                    game.key,
                    bool(manual),
                )
                return True, f"{game.name} registration is open."
            except Exception:
                log.exception("Mini-game announcement failed guild=%s game=%s", self.guild_id, game.key)
                await self._terminate(context, GameState.FAILED, None, "The game couldn't be started because of an internal error.")
                return False, "the game announcement failed and was marked for a 20-minute retry."

    async def resolve_announcement_channel(self, guild: discord.Guild):
        configured_id = int(getattr(getattr(getattr(self.bot, "config", None), "minigames", None), "channel_id", 0) or 0)
        if configured_id:
            channel = guild.get_channel(configured_id)
            if channel is None:
                channel = self.bot.get_channel(configured_id)
            if channel is not None and getattr(channel, "guild", None) == guild:
                return channel
            log.warning("Configured mini-game channel %s is unavailable in guild %s", configured_id, guild.id)
        if guild.system_channel is not None:
            return guild.system_channel
        me = guild.me
        for candidate in guild.text_channels:
            if me is None:
                return candidate
            permissions = candidate.permissions_for(me)
            if permissions.view_channel and permissions.send_messages and permissions.embed_links:
                return candidate
        return None

    async def _find_minigames_role(self, guild: discord.Guild) -> discord.Role | None:
        role = discord.utils.find(lambda item: item.name.casefold() == "minigames", guild.roles)
        if role is not None:
            return role
        try:
            roles = await guild.fetch_roles()
            return discord.utils.find(lambda item: item.name.casefold() == "minigames", roles)
        except (discord.DiscordException, AttributeError):
            log.warning("Could not resolve the minigames role in guild %s", guild.id, exc_info=True)
            return None

    def registration_embed(
        self,
        snapshot: GameSnapshot,
        game: GameDefinition,
        *,
        deadline: datetime | None = None,
    ) -> discord.Embed:
        ends = _parse_time(snapshot.registration_deadline) or deadline
        countdown = f"<t:{int(ends.timestamp())}:R>" if ends else "opening now"
        embed = discord.Embed(
            title="🌴 PARADISE MINI-GAME",
            description=(
                "A new challenge is approaching.\n\n"
                f"**{game.name}**\n{game.description}\n\n"
                "Compete with your friends, earn bragging rights, and climb the Paradise game leaderboard."
            ),
            colour=Colour.ACCENT,
        )
        embed.add_field(name="Status", value="Registration Open", inline=True)
        embed.add_field(
            name="Players",
            value=f"{len(snapshot.participant_ids)} / {snapshot.maximum_players}",
            inline=True,
        )
        embed.add_field(name="Registration closes", value=countdown, inline=True)
        embed.add_field(name="Quick rules", value=game.rules[:1000], inline=False)
        embed.set_footer(text=f"Minimum {snapshot.minimum_players} players · select View Rules for details")
        return embed

    async def join_player(self, game_id: str, user_id: int) -> tuple[bool, str]:
        async with self._state_lock:
            context = self._active_context
            if context is None or context.snapshot.game_id != game_id:
                return False, "that registration has closed."
            snapshot = context.snapshot
            if snapshot.state != GameState.REGISTRATION:
                return False, "registration has closed."
            deadline = _parse_time(snapshot.registration_deadline)
            if deadline is not None and self.now() >= deadline:
                return False, "registration has closed."
            if int(user_id) in snapshot.participant_ids:
                return False, "you're already in. good luck!"
            if len(snapshot.participant_ids) >= snapshot.maximum_players:
                return False, "this game is full."
            snapshot.participant_ids.append(int(user_id))
            snapshot.participant_ids.sort()
            snapshot.scores[str(int(user_id))] = 0
            if not self.store.save_active_game(snapshot, self.now()):
                snapshot.participant_ids.remove(int(user_id))
                snapshot.scores.pop(str(int(user_id)), None)
                return False, "registration changed; try again in a moment."
            return True, "you're in — see you at the starting line."

    async def refresh_registration(self, game_id: str) -> None:
        context = self._active_context
        if context is None or context.snapshot.game_id != game_id:
            return
        if context.snapshot.state != GameState.REGISTRATION:
            return
        try:
            await self.edit_current(
                context,
                embed=self.registration_embed(
                    context.snapshot,
                    context.definition,
                    deadline=_parse_time(context.snapshot.registration_deadline),
                ),
                view=RegistrationView(self, game_id, context.definition.rules),
            )
        except discord.DiscordException:
            log.warning("Registration announcement update failed", exc_info=True)

    async def persist_active(self, snapshot: GameSnapshot) -> None:
        async with self._state_lock:
            if not self.store.save_active_game(snapshot, self.now()):
                raise RuntimeError("the active mini-game snapshot was replaced")

    async def _transition(self, context: MiniGameContext, new_state: GameState) -> None:
        async with self._state_lock:
            current = context.snapshot.state
            if new_state not in VALID_TRANSITIONS[current]:
                raise RuntimeError(f"invalid mini-game transition: {current.value} -> {new_state.value}")
            context.snapshot.state = new_state
            if not self.store.save_active_game(context.snapshot, self.now()):
                raise RuntimeError("mini-game state changed before transition could be saved")

    async def edit_current(
        self,
        context: MiniGameContext,
        *,
        embed: discord.Embed,
        view: discord.ui.View | None,
        content: str | None = None,
        allowed_mentions: discord.AllowedMentions | None = None,
    ) -> None:
        if context.message is None:
            raise RuntimeError("mini-game announcement message is not available")
        edit_kwargs = {
            "embed": embed,
            "view": view,
            "allowed_mentions": allowed_mentions or discord.AllowedMentions.none(),
        }
        if content is not None:
            edit_kwargs["content"] = content
        await context.message.edit(**edit_kwargs)

    async def _execute_game(self, context: MiniGameContext) -> None:
        try:
            deadline = _parse_time(context.snapshot.registration_deadline)
            remaining = max(0.0, (deadline - self.now()).total_seconds()) if deadline else 0.0
            try:
                await asyncio.wait_for(context.cancel_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                pass
            if context.cancelled:
                await self._terminate(context, GameState.CANCELLED, None, "An administrator cancelled this game. No result was recorded.")
                return

            if len(context.snapshot.participant_ids) < context.snapshot.minimum_players:
                await self._terminate(
                    context,
                    GameState.EXPIRED,
                    None,
                    "Registration closed without enough players. No winner this time.",
                )
                return

            await self._transition(context, GameState.IN_PROGRESS)
            await context.update_game(
                f"{context.definition.name} · Starting",
                "Registration is closed. The challenge is starting now — good luck!",
                footer=context.definition.rules,
            )
            outcome = await context.definition.play(context)
            if context.cancelled:
                await self._terminate(context, GameState.CANCELLED, None, "An administrator cancelled this game. No result was recorded.")
            elif outcome.completed and outcome.winners:
                await self._terminate(context, GameState.COMPLETED, outcome, outcome.summary)
            else:
                await self._terminate(
                    context,
                    GameState.EXPIRED,
                    outcome,
                    outcome.summary or "The round ended without a winner. No result was recorded.",
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Mini-game failed internally guild=%s game=%s", self.guild_id, context.definition.key)
            try:
                await self._terminate(
                    context,
                    GameState.FAILED,
                    None,
                    "The game ran into an internal error. No result or rewards were recorded.",
                )
            except Exception:
                log.exception("Mini-game failure cleanup also failed guild=%s", self.guild_id)
        finally:
            if self._active_context is context and self.store.get_active_game(self.guild_id) is None:
                self._active_context = None

    async def _terminate(
        self,
        context: MiniGameContext,
        status: GameState,
        outcome: GameOutcome | None,
        summary: str,
    ) -> None:
        """Announce and clean up first, then set a recoverable next timestamp."""
        current = context.snapshot.state
        if status not in VALID_TRANSITIONS[current]:
            # A repeated or stale terminal request is idempotently ignored.
            if self.store.get_active_game(self.guild_id) is None:
                return
            raise RuntimeError(f"invalid mini-game terminal transition {current.value} -> {status.value}")
        context.snapshot.state = status  # transient only; not saved as a result row

        scores = outcome.scores if outcome is not None else context.scores()
        eligible = outcome.participated if outcome is not None else frozenset()
        winners = outcome.winners if status == GameState.COMPLETED and outcome is not None else ()
        content, embed = self._result_announcement(context, status, scores, winners, summary)
        allowed = discord.AllowedMentions(
            users=[discord.Object(id=user_id) for user_id in winners],
            roles=False,
            everyone=False,
        ) if winners else discord.AllowedMentions.none()

        delivered = False
        if context.message is not None:
            try:
                await context.message.edit(
                    content=content,
                    embed=embed,
                    view=None,
                    allowed_mentions=allowed,
                )
                await context.message.delete(delay=120)
                delivered = True
            except discord.DiscordException:
                log.warning("Could not edit mini-game result message", exc_info=True)
        if not delivered:
            try:
                await context.channel.send(content=content or None, embed=embed, allowed_mentions=allowed, delete_after=120)
            except discord.DiscordException:
                log.warning("Could not send mini-game result announcement", exc_info=True)

        if context.message is not None:
            context.message = None
        presence = getattr(self.bot, "presence_manager", None)
        if presence is not None:
            await presence.set_game_active(False)

        finished = self.now()
        interval = SUCCESS_DELAY if status == GameState.COMPLETED else UNSUCCESSFUL_DELAY
        committed = False
        for attempt in range(3):
            try:
                committed = self.store.finish_game(
                    guild_id=self.guild_id,
                    game_id=context.snapshot.game_id,
                    status=status,
                    completed_at=finished,
                    next_game_at=finished + interval,
                    participated=eligible,
                    scores=scores,
                    winners=winners,
                )
                break
            except Exception:
                log.exception("Could not persist mini-game outcome; retry=%s", attempt + 1)
                await asyncio.sleep(0.1 * (attempt + 1))
        if not committed and self.store.get_active_game(self.guild_id) is not None:
            log.error("Mini-game cleanup remains pending; scheduler will recover the active snapshot")
            return

        log.info(
            "Mini-game ended guild=%s game=%s status=%s participants=%s total_points=%s",
            self.guild_id,
            context.definition.key,
            status.value,
            len(context.snapshot.participant_ids),
            sum(max(0, int(points)) for points in scores.values()),
        )
        self._scheduler_wake.set()

    def _result_announcement(
        self,
        context: MiniGameContext,
        status: GameState,
        scores: dict[int, int],
        winners: tuple[int, ...],
        summary: str,
    ) -> tuple[str, discord.Embed]:
        if status == GameState.COMPLETED:
            title = "🏆 Challenge complete"
            descriptions = ["**Winner**" if len(winners) == 1 else "**Winners — a tie**"]
            descriptions.append(", ".join(f"<@{user_id}>" for user_id in winners))
        elif status == GameState.CANCELLED:
            title = "⏹️ Game cancelled"
            descriptions = ["The game was cancelled by an administrator.", "No result or rewards were recorded."]
        elif status == GameState.FAILED:
            title = "⚠️ Game interrupted"
            descriptions = ["The game could not finish normally.", "No result or rewards were recorded."]
        else:
            title = "⌛ Game ended"
            descriptions = ["No winner this time.", "This attempt was not counted as a completed game."]

        embed = discord.Embed(
            title=f"🌴 PARADISE MINI-GAME · {title}",
            description="\n".join(descriptions),
            colour=Colour.ACCENT,
        )
        standings = sorted(scores.items(), key=lambda entry: (-entry[1], entry[0]))
        if standings:
            rows = [
                f"**{rank}.** <@{user_id}> — **{max(0, int(points))}** point{'s' if points != 1 else ''}"
                for rank, (user_id, points) in enumerate(standings[:15], start=1)
            ]
            if len(standings) > 15:
                rows.append(f"…and {len(standings) - 15} more player(s)")
            embed.add_field(name="Final standings", value="\n".join(rows), inline=False)
        else:
            embed.add_field(name="Final standings", value="No registered players.", inline=False)
        if summary:
            embed.set_footer(text=summary[:200])

        content = (
            "🏆 Winner: " + ", ".join(f"<@{user_id}>" for user_id in winners)
            if status == GameState.COMPLETED and winners
            else ""
        )
        return content, embed

    async def cancel_game(self) -> tuple[bool, str]:
        context = self._active_context
        if context is None:
            return False, "there isn't an active game to cancel."
        if context.snapshot.state not in (GameState.ANNOUNCING, GameState.REGISTRATION, GameState.IN_PROGRESS):
            return False, "the active game is already closing."
        await context.notify_cancel()
        self._scheduler_wake.set()
        return True, "cancellation requested; the current announcement will be updated shortly."

    async def set_automatic_enabled(self, enabled: bool) -> None:
        self.store.set_automatic_enabled(self.guild_id, enabled, self.now())
        self._scheduler_wake.set()

    async def configure(self, values: dict[str, int]) -> GameConfig:
        current = self.store.get_config(self.guild_id)
        proposed = {
            "registration_seconds": values.get("registration_seconds", current.registration_seconds),
            "minimum_players": values.get("minimum_players", current.minimum_players),
            "maximum_players": values.get("maximum_players", current.maximum_players),
            "answer_seconds": values.get("answer_seconds", current.answer_seconds),
        }
        if proposed["minimum_players"] > proposed["maximum_players"]:
            raise ValueError("minimum players cannot exceed maximum players")
        return self.store.update_settings(self.guild_id, values, self.now())

    async def reschedule(self, delay_minutes: int) -> datetime:
        if self.store.get_active_game(self.guild_id) is not None:
            raise RuntimeError("finish or cancel the active game before rescheduling")
        at = self.now() + timedelta(minutes=max(1, int(delay_minutes)))
        self.store.set_next_game_at(self.guild_id, at, self.now())
        self._scheduler_wake.set()
        return at

    async def status(self) -> tuple[GameConfig, GameSnapshot | None]:
        return self.store.get_config(self.guild_id), self.store.get_active_game(self.guild_id)

    async def _scheduler_loop(self) -> None:
        await self.bot.wait_until_ready()
        while not self._recovery_gate.is_set() and not self.bot.is_closed():
            try:
                await self.recover_after_restart()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Mini-game recovery is still pending; retrying shortly")
                await self._wait_for_scheduler_wake(30.0)
        await self._recovery_gate.wait()
        while not self.bot.is_closed():
            try:
                active = self.store.get_active_game(self.guild_id)
                if active is not None:
                    task = self._active_task
                    if task is not None and not task.done():
                        await asyncio.gather(task, return_exceptions=True)
                        self._active_task = None
                        continue
                    # An active record without its runtime task is unsafe to
                    # resume. Treat it like an interrupted game immediately.
                    self._pending_recovery = active
                    self._recovery_complete = False
                    await self.recover_after_restart()
                    continue

                config = self.store.get_config(self.guild_id)
                if not config.automatic_enabled:
                    await self._wait_for_scheduler_wake(60.0)
                    continue

                next_at = _parse_time(config.next_game_at)
                now = self.now()
                if next_at is not None and next_at <= now:
                    guild = self.bot.get_guild(self.guild_id)
                    channel = await self.resolve_announcement_channel(guild) if guild else None
                    if channel is None:
                        retry_at = now + timedelta(minutes=1)
                        self.store.set_next_game_at(self.guild_id, retry_at, now)
                        log.error("Automatic mini-game postponed: no sendable channel; retry in one minute")
                        await self._wait_for_scheduler_wake(60.0)
                        continue
                    started, _message = await self.start_game(channel=channel, manual=False)
                    if not started:
                        # Avoid a hot loop when Discord/channel readiness races
                        # the scheduler. Manual starts remain immediately usable.
                        retry_at = self.now() + timedelta(minutes=1)
                        if self.store.get_active_game(self.guild_id) is None:
                            self.store.set_next_game_at(self.guild_id, retry_at, self.now())
                        await self._wait_for_scheduler_wake(60.0)
                    continue

                delay = 60.0 if next_at is None else max(0.5, (next_at - now).total_seconds())
                await self._wait_for_scheduler_wake(min(delay, 60.0))
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("Mini-game scheduler iteration failed; bot remains online")
                await self._wait_for_scheduler_wake(30.0)

    async def _wait_for_scheduler_wake(self, seconds: float) -> None:
        self._scheduler_wake.clear()
        try:
            await asyncio.wait_for(self._scheduler_wake.wait(), timeout=max(0.1, seconds))
        except asyncio.TimeoutError:
            pass


__all__ = ["MiniGameContext", "MiniGameManager"]
