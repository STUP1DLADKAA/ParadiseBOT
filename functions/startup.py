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

Commands are registered ONLY in the home guild — always. That is what makes
them appear and disappear instantly, and it is the only way to guarantee a
command is never listed twice: a global registration and a guild registration
of the same command show up as two entries until Discord's global cache
converges (up to an hour later). Whatever an older version published globally
is purged on the next start, so an existing deployment heals itself.

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
#: your own with ``DISCORD_GUILD_ID``. Commands are never registered globally:
#: a global copy would be a duplicate next to the guild copy and can take an
#: hour to disappear.
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
        # Deleted outright: music and minigames are gone and their commands go with them.
        "minigames",
        "music",
        "cmd",
        "p",
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
    global_purged: bool = False
    skipped: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.guild_ok

    def summary(self) -> str:
        if self.skipped:
            return "command sync already completed this session"
        state = "ok" if self.guild_ok else "FAILED"
        purged = ", global copy cleared" if self.global_purged else ""
        return (
            f"guild {self.guild_id}: {state} "
            f"({len(self.guild_commands)} commands"
            f"{', pruned ' + ', '.join(self.pruned) if self.pruned else ''}"
            f"{purged})"
        )


class CommandSynchronizer:
    """Owns every command-tree write that happens at startup.

    Commands are published ONLY to the home guild (instant propagation, never
    listed twice) and any global copy left behind by an older release is
    removed. ``sync_global`` is kept as an explicit escape hatch for a bot with
    no home server at all; it must never be combined with a guild id, because
    that is exactly how the duplicate entries happened.
    """

    def __init__(
        self,
        bot,
        *,
        guild_id: int = PRIMARY_GUILD_ID,
        retired: frozenset[str] = RETIRED_COMMANDS,
        sync_global: bool = False,
        purge_global: bool = True,
    ) -> None:
        self.bot = bot
        self.guild_id = max(0, int(guild_id or 0))
        self.retired = frozenset(retired)
        # A configured home guild is the sole command namespace. Never
        # publish a second global copy: stale global schemas can remain visible
        # and trigger CommandSignatureMismatch for edited subcommands.
        self.sync_global = bool(sync_global) and not bool(self.guild_id)
        self.purge_global = bool(purge_global) or bool(self.guild_id)
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

            # Sync global commands first (if enabled). These propagate to all
            # servers but can take up to an hour to appear outside the home
            # guild; the guild sync below makes them instant at home.
            if self.sync_global:
                await self._sync_global(report)

            # Then sync to the home guild (instant propagation).
            if self.guild_id:
                await self._sync_guild(report)
                if self.purge_global:
                    await self._purge_global(report)
            elif not self.sync_global:
                report.errors.append("no home guild configured")

        if report.guild_ok or report.global_ok:
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
        # Verify Discord's actual registered schema matches the local tree.
        # This catches stale registrations (e.g. an old global /mal connect
        # with different parameters) that would cause CommandSignatureMismatch
        # at invocation time.
        await self._verify_schema(report, guild)

    async def _purge_global(self, report: SyncReport) -> None:
        """Delete the global command list, so nothing is listed twice.

        Uses the REST endpoint directly instead of ``tree.sync()``: clearing the
        in-memory tree would also empty ``/help`` (it lists the live tree) and
        break any later ``copy_global_to``.
        """
        http = getattr(self.bot, "http", None)
        application_id = getattr(self.bot, "application_id", None)
        upsert = getattr(http, "bulk_upsert_global_commands", None)
        if upsert is None or not application_id:
            log.debug("Global command purge unavailable on this client")
            return
        try:
            await asyncio.wait_for(
                upsert(application_id, []), timeout=_SYNC_TIMEOUT
            )
        except (discord.DiscordException, asyncio.TimeoutError, OSError) as error:
            # Cosmetic only: the guild tree is already authoritative.
            log.info("Could not clear the global command list (%s)", type(error).__name__)
            return
        except Exception as error:  # noqa: BLE001 - never block startup
            log.debug("Global command purge failed: %s", type(error).__name__)
            return
        report.global_purged = True
        log.info("Cleared the global command list (commands live in guild %s)", self.guild_id)

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

    async def _verify_schema(self, report: SyncReport, guild: discord.Object) -> None:
        """Fetch Discord's actual registered schema and compare it with the local tree.

        Catches stale registrations (e.g. an old global /mal connect with
        different parameters) that would cause CommandSignatureMismatch at
        invocation time. Sets report.guild_ok = False on any mismatch.
        """
        tree = self.bot.tree
        try:
            remote = await asyncio.wait_for(
                tree.fetch_commands(guild=guild), timeout=_SYNC_TIMEOUT
            )
        except (discord.DiscordException, asyncio.TimeoutError, OSError) as error:
            report.errors.append(f"schema verification failed: {type(error).__name__}")
            report.guild_ok = False
            log.warning(
                "Could not fetch registered commands for guild %s",
                self.guild_id,
                exc_info=True,
            )
            return
        except Exception:  # noqa: BLE001
            report.errors.append("schema verification error")
            report.guild_ok = False
            log.exception("Unexpected schema verification failure")
            return

        remote_by_name = {cmd.name: cmd for cmd in remote}
        local_commands = tree.get_commands(guild=guild)
        local_by_name = {cmd.name: cmd for cmd in local_commands}

        # If Discord reports no commands at all, there is nothing to compare
        # against (fresh install or test environment).  Command presence is
        # already checked by _verify; schema comparison only applies where both
        # sides have the command.
        if not remote_by_name:
            return

        # Commands missing from Discord's registration are a sync problem
        # (already surfaced by _verify as "missing after sync").
        for name, local_cmd in local_by_name.items():
            remote_cmd = remote_by_name.get(name)
            if remote_cmd is None:
                continue
            self._compare_command_schemas(local_cmd, remote_cmd, report)

        # Remote commands not in the local tree are stale registrations.
        for name in remote_by_name:
            if name not in local_by_name:
                log.info(
                    "Remote has extra command not in local tree: %s", name
                )

    def _compare_command_schemas(
        self, local_cmd: object, remote_cmd: object, report: SyncReport
    ) -> None:
        """Recursively compare a local command with its remote registration."""
        local_params = self._get_local_params(local_cmd)
        remote_options = self._get_remote_options(remote_cmd)
        qualified = getattr(local_cmd, "qualified_name", local_cmd.name)

        for name, local_info in local_params.items():
            if name not in remote_options:
                report.errors.append(f"parameter missing remotely: {qualified} {name}")
                report.guild_ok = False
            else:
                remote_info = remote_options[name]
                if local_info.get("required") != remote_info.get("required"):
                    report.errors.append(
                        f"parameter required mismatch: {qualified} {name}"
                    )
                    report.guild_ok = False
                if local_info.get("type") != remote_info.get("type"):
                    report.errors.append(
                        f"parameter type mismatch: {qualified} {name}"
                    )
                    report.guild_ok = False
                # Recurse into subcommand groups
                if "subcommands" in local_info and "subcommands" in remote_info:
                    self._compare_subcommand_groups(
                        local_info["subcommands"],
                        remote_info["subcommands"],
                        report,
                        qualified,
                    )

        for name in remote_options:
            if name not in local_params:
                report.errors.append(f"extra parameter remotely: {qualified} {name}")
                report.guild_ok = False

    def _compare_subcommand_groups(
        self,
        local_subs: dict[str, dict],
        remote_subs: dict[str, dict],
        report: SyncReport,
        parent_qualified: str,
    ) -> None:
        """Compare subcommand groups recursively."""
        for name, local_info in local_subs.items():
            if name not in remote_subs:
                report.errors.append(
                    f"subcommand missing remotely: {parent_qualified} {name}"
                )
                report.guild_ok = False
            else:
                remote_info = remote_subs[name]
                if local_info.get("required") != remote_info.get("required"):
                    report.errors.append(
                        f"subcommand required mismatch: {parent_qualified} {name}"
                    )
                    report.guild_ok = False
                if local_info.get("type") != remote_info.get("type"):
                    report.errors.append(
                        f"subcommand type mismatch: {parent_qualified} {name}"
                    )
                    report.guild_ok = False
                if "subcommands" in local_info and "subcommands" in remote_info:
                    self._compare_subcommand_groups(
                        local_info["subcommands"],
                        remote_info["subcommands"],
                        report,
                        f"{parent_qualified} {name}",
                    )

        for name in remote_subs:
            if name not in local_subs:
                report.errors.append(
                    f"extra subcommand remotely: {parent_qualified} {name}"
                )
                report.guild_ok = False

    def _get_local_params(self, local_cmd: object) -> dict[str, dict]:
        """Extract parameter info from a local command object."""
        params: dict[str, dict] = {}
        param_dict = getattr(local_cmd, "_params", None)
        if param_dict:
            for name, param in param_dict.items():
                params[name] = {
                    "type": getattr(param, "type", None),
                    "required": getattr(param, "required", False),
                    "display_name": getattr(param, "display_name", name),
                }
        subcommands = getattr(local_cmd, "commands", None)
        if subcommands:
            for sub_name, sub_cmd in subcommands.items():
                params[sub_name] = {
                    "type": "group",
                    "required": True,
                    "subcommands": self._get_local_params(sub_cmd),
                }
        return params

    def _get_remote_options(self, remote_cmd: object) -> dict[str, dict]:
        """Extract option info from a remote AppCommand.

        Uses duck typing so it works with both real discord.py objects
        (AppCommandGroup / Argument) and test fakes.
        """
        options: dict[str, dict] = {}
        for opt in getattr(remote_cmd, "options", []) or []:
            if hasattr(opt, "options") and hasattr(opt, "name"):
                # Subcommand group: has nested options
                options[opt.name] = {
                    "type": "group",
                    "required": True,
                    "subcommands": self._get_remote_options(opt),
                }
            elif hasattr(opt, "required") and hasattr(opt, "name"):
                # Argument / parameter
                options[opt.name] = {
                    "type": getattr(opt, "type", None),
                    "required": opt.required,
                }
        return options

__all__ = [
    "PRIMARY_GUILD_ID",
    "RETIRED_COMMANDS",
    "CommandSynchronizer",
    "SyncReport",
]
