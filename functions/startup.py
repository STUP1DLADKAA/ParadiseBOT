"""Startup command synchronisation — one explicit flow, no competing syncs.

Discord caches a bot's command tree. Global registration can take up to
an hour to propagate, which makes "I removed a command, restart the bot"
feel broken. Guild-scoped registration is instant, so Paradise pushes the
current tree to its home guild on every start:

    load extensions
          ↓
    prune retired public commands
          ↓
    clear the guild tree, copy global commands in, sync guild 1180538130009432126  (instant)
          ↓
    verify what Discord accepted
          ↓
    bot ready

Commands are registered ONLY in the home guild. Global sync is disabled
by default to prevent duplicate commands (global + guild) and to ensure
instant propagation. If you need commands in other servers, enable
``sync_global=True`` — but be aware of the propagation delay.

Properties this module guarantees:

* **single source of truth** — nothing else in the codebase calls
  ``tree.sync``; :meth:`CommandSynchronizer.run` is the only path;
* **idempotent** — a second call (reconnect, ``on_ready`` firing again)
  is a no-op unless explicitly forced, so commands are never duplicated;
* **awaited** — startup waits for the guild sync before the bot reports
  ready;
* **non-fatal** — any failure is logged with a short, secret-free summary
  and the rest of the bot keeps working.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import discord

log = logging.getLogger(__name__)

#: Fallback home server used by this repository's demo deployment. Configure
#: your own with ``DISCORD_GUILD_ID``; leave it unset to publish commands
#: globally (Discord can take up to an hour to propagate those).
PRIMARY_GUILD_ID = 1180538130009432126

#: Public slash commands that were retired in favour of natural mentions.
#: Their capabilities still exist — they are reachable through the ping
#: assistant (``@Paradise weather in Delhi``), not through a command.
RETIRED_COMMANDS = frozenset(
    {
        "search",
        "convert",
        "time",
        "define",
        "dictionary",
        "recommend",
        "weather",
        # Older removals, kept here so a stale guild tree is cleaned up too.
        "example",
        "trending",
        "upcoming",
    }
)

_SYNC_TIMEOUT = 30.0


@dataclass(slots=True)
class SyncReport:
    """What one synchronisation run actually did. Safe to log verbatim."""

    guild_id: int = PRIMARY_GUILD_ID
    pruned: tuple[str, ...] = ()
    guild_commands: tuple[str, ...] = ()
    global_commands: tuple[str, ...] = ()
    guild_ok: bool = False
    global_ok: bool = False
    skipped: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.guild_ok

    def summary(self) -> str:
        if self.skipped:
            return "command sync already completed this session"
        state = "ok" if self.guild_ok else "FAILED"
        target = f"guild {self.guild_id}" if self.guild_id else "global commands"
        return (
            f"{target}: {state} "
            f"({len(self.guild_commands)} commands"
            f"{', pruned ' + ', '.join(self.pruned) if self.pruned else ''})"
        )


class CommandSynchronizer:
    """Owns every command-tree write that happens at startup.

    Commands are published ONLY to the home guild (instant propagation,
    no duplicates). Global sync is intentionally disabled — it causes
    commands to appear twice during propagation and can take up to an
    hour to converge.
    """

    def __init__(
        self,
        bot,
        *,
        guild_id: int = PRIMARY_GUILD_ID,
        retired: frozenset[str] = RETIRED_COMMANDS,
        sync_global: bool | None = None,
    ) -> None:
        self.bot = bot
        self.guild_id = max(0, int(guild_id or 0))
        # Without a home guild there is nothing to publish to, so fall back to
        # global registration instead of silently syncing nowhere.
        if sync_global is None:
            sync_global = not self.guild_id
        self.retired = frozenset(retired)
        self.sync_global = bool(sync_global)
        self._done = False
        self._lock = asyncio.Lock()
        self.last_report: SyncReport | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def completed(self) -> bool:
        """True once a successful guild sync has happened this session."""
        return self._done

    def prune(self) -> tuple[str, ...]:
        """Drop retired commands from the in-memory tree. Idempotent."""
        tree = self.bot.tree
        removed: list[str] = []
        for name in sorted(self.retired):
            try:
                if tree.remove_command(name) is not None:
                    removed.append(name)
                # Also clear any guild-scoped copy left by an older release.
                if tree.remove_command(name, guild=self._guild()) is not None:
                    if name not in removed:
                        removed.append(name)
            except Exception:  # noqa: BLE001 - never block startup on a prune
                log.debug("Could not prune command %s", name, exc_info=True)
        if removed:
            log.info("Pruned retired commands: %s", ", ".join(removed))
        return tuple(removed)

    async def run(self, *, force: bool = False) -> SyncReport:
        """Publish the current command tree. Safe to call more than once."""
        async with self._lock:
            if self._done and not force:
                report = SyncReport(guild_id=self.guild_id, skipped=True)
                report.guild_ok = True
                report.guild_commands = self.expected_names()
                self.last_report = report
                return report

            report = SyncReport(guild_id=self.guild_id)
            report.pruned = self.prune()
            if self.guild_id:
                await self._sync_guild(report)
            elif not self.sync_global:
                report.errors.append("no home guild configured")
            else:
                # Global-only deployment: a guild sync would do nothing.
                report.guild_ok = True
            # Global sync is disabled by default — commands live only in the
            # home guild to avoid duplicates and propagation delays. If you
            # explicitly need global registration, set sync_global=True.
            if self.sync_global:
                await self._sync_global(report)
            if report.guild_ok:
                self._done = True
            self.last_report = report
            log.info("Command sync — %s", report.summary())
            return report

    def expected_names(self) -> tuple[str, ...]:
        """Command names the tree currently defines (after pruning)."""
        try:
            return tuple(sorted(command.name for command in self.bot.tree.get_commands()))
        except Exception:  # noqa: BLE001
            return ()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _guild(self):
        return discord.Object(id=self.guild_id)

    async def _sync_guild(self, report: SyncReport) -> None:
        tree = self.bot.tree
        guild = self._guild()
        try:
            # Replace whatever the guild had with exactly the current tree:
            # this is what makes removals and edits visible immediately, and
            # it cannot create duplicates because the guild set is overwritten.
            tree.clear_commands(guild=guild)
            tree.copy_global_to(guild=guild)
            synced = await asyncio.wait_for(tree.sync(guild=guild), timeout=_SYNC_TIMEOUT)
        except (discord.DiscordException, asyncio.TimeoutError, OSError) as error:
            report.errors.append(f"guild sync failed: {type(error).__name__}")
            log.warning("Guild %s command sync failed", self.guild_id, exc_info=True)
            return
        except Exception as error:  # noqa: BLE001 - startup must not crash
            report.errors.append(f"guild sync error: {type(error).__name__}")
            log.exception("Unexpected guild command sync failure")
            return

        names = tuple(sorted(str(command.name) for command in synced or ()))
        report.guild_commands = names
        report.guild_ok = True
        self._verify(report, names)

    async def _sync_global(self, report: SyncReport) -> None:
        try:
            synced = await asyncio.wait_for(self.bot.tree.sync(), timeout=_SYNC_TIMEOUT)
        except (discord.DiscordException, asyncio.TimeoutError, OSError) as error:
            # Never fatal: the guild already has the authoritative tree.
            report.errors.append(f"global sync failed: {type(error).__name__}")
            log.warning("Global command sync failed", exc_info=True)
            return
        except Exception as error:  # noqa: BLE001
            report.errors.append(f"global sync error: {type(error).__name__}")
            log.exception("Unexpected global command sync failure")
            return
        report.global_commands = tuple(sorted(str(command.name) for command in synced or ()))
        report.global_ok = True

    def _verify(self, report: SyncReport, names: tuple[str, ...]) -> None:
        """Compare what Discord accepted with what the tree defines."""
        expected = set(self.expected_names())
        accepted = set(names)
        leaked = accepted & self.retired
        if leaked:
            report.errors.append(f"retired commands still present: {sorted(leaked)}")
            log.error(
                "Guild %s still lists retired commands: %s",
                self.guild_id,
                ", ".join(sorted(leaked)),
            )
        missing = expected - accepted
        if missing:
            report.errors.append(f"missing after sync: {sorted(missing)}")
            log.warning(
                "Guild %s is missing commands after sync: %s",
                self.guild_id,
                ", ".join(sorted(missing)),
            )
        extra = accepted - expected
        if extra:
            log.info(
                "Guild %s reports extra commands: %s", self.guild_id, ", ".join(sorted(extra))
            )


__all__ = [
    "PRIMARY_GUILD_ID",
    "RETIRED_COMMANDS",
    "CommandSynchronizer",
    "SyncReport",
]
