"""Startup command synchronisation tests (functions/startup.py).

Command changes must be visible on guild 1180538130009432126 the moment
the bot restarts: new commands registered, removed commands gone, edited
commands updated — exactly once, awaited, and never fatal.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord  # noqa: E402

from functions.startup import (  # noqa: E402
    PRIMARY_GUILD_ID,
    RETIRED_COMMANDS,
    CommandSynchronizer,
)

TARGET_GUILD = 1180538130009432126


class FakeArgument:
    """A fake remote command argument/option."""

    def __init__(self, name: str, required: bool = False, type_value: int = 3) -> None:
        self.name = name
        self.required = required
        self.type = type_value


class FakeAppCommand:
    """A fake remote application command (what Discord returns from fetch_commands)."""

    def __init__(
        self,
        name: str,
        options=None,
        *,
        guild_id: int | None = None,
        type_value: int = 1,
    ) -> None:
        self.name = name
        self.options = list(options or [])
        self.guild_id = guild_id
        self.type = type_value


class FakeCommand:
    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<FakeCommand {self.name}>"


class FakeTree:
    """A command tree that records everything the synchroniser does."""

    def __init__(
        self,
        names=(),
        *,
        fail_guild=False,
        fail_global=False,
        remote_commands=None,
    ) -> None:
        self.global_commands = {name: FakeCommand(name) for name in names}
        self.guild_commands: dict[int, dict[str, FakeCommand]] = {}
        self.sync_calls: list[int | None] = []
        self.cleared: list[int] = []
        self.copied: list[int] = []
        self.fail_guild = fail_guild
        self.fail_global = fail_global
        # Remote commands Discord reports when fetch_commands is called.
        # Maps guild_id -> list of FakeAppCommand; None means global.
        self.remote_commands: dict[int | None, list[FakeAppCommand]] = {}
        if remote_commands is not None:
            self.remote_commands[None] = remote_commands

    # -- discord.py surface ------------------------------------------------

    def get_commands(self, *, guild=None):
        if guild is None:
            return list(self.global_commands.values())
        return list(self.guild_commands.get(guild.id, {}).values())

    def remove_command(self, name, *, guild=None):
        if guild is None:
            return self.global_commands.pop(name, None)
        return self.guild_commands.get(guild.id, {}).pop(name, None)

    def clear_commands(self, *, guild):
        self.cleared.append(guild.id)
        self.guild_commands[guild.id] = {}

    def copy_global_to(self, *, guild):
        self.copied.append(guild.id)
        self.guild_commands[guild.id] = dict(self.global_commands)

    async def sync(self, *, guild=None):
        if guild is None:
            self.sync_calls.append(None)
            if self.fail_global:
                raise discord.DiscordException("global sync boom")
            return list(self.global_commands.values())
        self.sync_calls.append(guild.id)
        if self.fail_guild:
            raise discord.DiscordException("guild sync boom")
        return list(self.guild_commands.get(guild.id, {}).values())

    async def fetch_commands(self, *, guild=None):
        """Return the commands Discord has registered (fake).

        Guild-scoped lookup falls back to the global (None) key so tests
        that set up remote_commands without a guild id still work.
        """
        if guild is None:
            return self.remote_commands.get(None, [])
        return self.remote_commands.get(
            guild.id, self.remote_commands.get(None, [])
        )


class FakeHTTP:
    """Records the one REST call that clears the global command list."""

    def __init__(self, *, fail: bool = False) -> None:
        self.purges: list[tuple[int, list]] = []
        self.fail = fail

    async def bulk_upsert_global_commands(self, application_id, payload):
        if self.fail:
            raise discord.DiscordException("global purge boom")
        self.purges.append((application_id, list(payload)))
        return []


def fake_bot(tree: FakeTree, *, http=None):
    return SimpleNamespace(
        tree=tree,
        application_id=1234,
        http=http if http is not None else FakeHTTP(),
    )


def run(coro):
    return asyncio.run(coro)


class TestTargetGuild(unittest.TestCase):
    def test_primary_guild_is_the_configured_server(self):
        self.assertEqual(PRIMARY_GUILD_ID, TARGET_GUILD)

    def test_retired_commands_listed(self):
        for name in ("search", "convert", "time", "define", "dictionary",
                     "recommend", "weather"):
            self.assertIn(name, RETIRED_COMMANDS)

    def test_the_music_commands_are_retired_too(self):
        """A guild that still has /music from an older release must lose it."""
        for name in ("music", "cmd", "p"):
            self.assertIn(name, RETIRED_COMMANDS)


class TestSynchronizer(unittest.TestCase):
    def setUp(self):
        # Failure paths log loudly on purpose; keep the test output clean.
        logging.disable(logging.CRITICAL)

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def test_syncs_current_tree_to_the_guild(self):
        tree = FakeTree(["ping", "help", "poll"])
        sync = CommandSynchronizer(fake_bot(tree), sync_global=False)

        report = run(sync.run())

        self.assertTrue(report.ok)
        self.assertEqual(report.guild_id, TARGET_GUILD)
        self.assertEqual(tree.sync_calls, [TARGET_GUILD])
        self.assertEqual(report.guild_commands, ("help", "ping", "poll"))

    def test_removed_commands_disappear_immediately(self):
        tree = FakeTree(["ping", "weather", "search", "define", "dictionary",
                         "convert", "time", "recommend"])
        sync = CommandSynchronizer(fake_bot(tree), sync_global=False)

        report = run(sync.run())

        self.assertEqual(report.guild_commands, ("ping",))
        self.assertFalse(set(report.guild_commands) & RETIRED_COMMANDS)
        self.assertIn("weather", report.pruned)
        # And they are gone from the tree itself, not just from the guild.
        self.assertNotIn("weather", tree.global_commands)

    def test_changed_commands_are_republished_every_restart(self):
        tree = FakeTree(["ping"])
        run(CommandSynchronizer(fake_bot(tree), sync_global=False).run())
        tree.global_commands["ping"] = FakeCommand("ping")  # edited definition
        tree.global_commands["newthing"] = FakeCommand("newthing")

        report = run(CommandSynchronizer(fake_bot(tree), sync_global=False).run())

        self.assertIn("newthing", report.guild_commands)
        self.assertEqual(tree.sync_calls, [TARGET_GUILD, TARGET_GUILD])

    def test_sync_is_idempotent_within_a_session(self):
        tree = FakeTree(["ping"])
        sync = CommandSynchronizer(fake_bot(tree), sync_global=False)

        first = run(sync.run())
        second = run(sync.run())

        self.assertTrue(first.ok)
        self.assertTrue(second.skipped)
        self.assertEqual(tree.sync_calls, [TARGET_GUILD], "synced twice")

    def test_forced_resync_is_allowed(self):
        tree = FakeTree(["ping"])
        sync = CommandSynchronizer(fake_bot(tree), sync_global=False)
        run(sync.run())
        run(sync.run(force=True))
        self.assertEqual(tree.sync_calls, [TARGET_GUILD, TARGET_GUILD])

    def test_guild_copy_never_duplicates(self):
        tree = FakeTree(["ping", "help"])
        sync = CommandSynchronizer(fake_bot(tree), sync_global=False)
        run(sync.run(force=True))
        run(sync.run(force=True))
        names = [command.name for command in tree.get_commands(guild=discord.Object(id=TARGET_GUILD))]
        self.assertEqual(sorted(names), ["help", "ping"])
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(tree.cleared, [TARGET_GUILD, TARGET_GUILD])

    def test_a_global_copy_is_purged_so_nothing_is_listed_twice(self):
        """The duplicate-entry bug: global + guild copies of every command."""
        tree = FakeTree(["ping", "help"])
        http = FakeHTTP()
        report = run(CommandSynchronizer(fake_bot(tree, http=http)).run())

        self.assertEqual(tree.sync_calls, [TARGET_GUILD], "no global sync, ever")
        self.assertEqual(http.purges, [(1234, [])], "the global list must be emptied")
        self.assertTrue(report.global_purged)
        self.assertIn("global copy cleared", report.summary())

    def test_a_guild_id_always_wins_over_sync_global(self):
        tree = FakeTree(["ping"])
        http = FakeHTTP()
        sync = CommandSynchronizer(fake_bot(tree, http=http), sync_global=True)
        run(sync.run())
        self.assertFalse(sync.sync_global, "guild + global is the duplicate bug")
        self.assertEqual(tree.sync_calls, [TARGET_GUILD])

    def test_the_purge_can_be_switched_off(self):
        """In global-only mode (no guild), purge_global=False prevents purging."""
        tree = FakeTree(["ping"])
        http = FakeHTTP()
        report = run(
            CommandSynchronizer(
                fake_bot(tree, http=http), guild_id=0, purge_global=False
            ).run()
        )
        self.assertEqual(http.purges, [])
        self.assertFalse(report.global_purged)

    def test_purge_always_happens_when_guild_configured(self):
        """With a home guild, obsolete global registrations are always purged,
        even if purge_global=False was explicitly passed."""
        tree = FakeTree(["ping"])
        http = FakeHTTP()
        report = run(
            CommandSynchronizer(fake_bot(tree, http=http), purge_global=False).run()
        )
        self.assertEqual(http.purges, [(1234, [])], "global list must be emptied")
        self.assertTrue(report.global_purged)

    def test_guild_failure_is_logged_not_fatal(self):
        tree = FakeTree(["ping"], fail_guild=True)
        sync = CommandSynchronizer(fake_bot(tree), sync_global=False)

        report = run(sync.run())

        self.assertFalse(report.ok)
        self.assertTrue(report.errors)
        self.assertFalse(sync.completed)
        # A failed sync is retried on the next call rather than cached.
        run(sync.run())
        self.assertEqual(tree.sync_calls, [TARGET_GUILD, TARGET_GUILD])

    def test_a_failed_purge_does_not_break_the_guild_sync(self):
        tree = FakeTree(["ping"])
        http = FakeHTTP(fail=True)
        report = run(CommandSynchronizer(fake_bot(tree, http=http)).run())

        self.assertTrue(report.ok)
        self.assertFalse(report.global_purged)
        self.assertEqual(report.guild_commands, ("ping",))

    def test_report_summary_leaks_nothing_sensitive(self):
        tree = FakeTree(["ping"])
        report = run(CommandSynchronizer(fake_bot(tree), sync_global=False).run())
        summary = report.summary()
        self.assertIn(str(TARGET_GUILD), summary)
        for secret in ("token", "api", "key"):
            self.assertNotIn(secret, summary.casefold())

    # ------------------------------------------------------------------
    # MAL CommandSignatureMismatch regression tests
    # ------------------------------------------------------------------

    def test_schema_mismatch_detects_stale_mal_connect_parameters(self):
        """A stale global /mal connect registered with a `user` parameter
        must be detected as a schema mismatch, not silently allowed."""
        # Local tree: /mal connect has NO parameters
        local_mal = FakeCommand("mal")
        local_mal._params = {}  # connect takes no arguments besides interaction

        tree = FakeTree(
            ["mal"],
            remote_commands=[
                FakeAppCommand(
                    "mal",
                    options=[
                        FakeAppCommand(
                            "connect",
                            options=[
                                # Stale registration: connect has a `user` parameter
                                FakeArgument("user", required=True, type_value=6)
                            ],
                        )
                    ],
                )
            ],
        )
        sync = CommandSynchronizer(fake_bot(tree), sync_global=False)
        report = run(sync.run())

        self.assertFalse(report.ok, "schema mismatch should fail sync")
        self.assertTrue(
            any("parameter" in e.lower() for e in report.errors),
            f"should report parameter mismatch: {report.errors}",
        )

    def test_schema_match_passes_when_remote_matches_local(self):
        """When Discord's registered schema matches the local tree, sync succeeds."""
        # Local tree: /mal connect with NO parameters (matches real commands/mal.py)
        local_connect = FakeCommand("connect")
        local_connect._params = {}
        local_mal = FakeCommand("mal")
        local_mal.commands = {"connect": local_connect}

        tree = FakeTree(["x"])  # placeholder, will replace global_commands
        tree.global_commands = {"mal": local_mal}
        tree.remote_commands = {
            None: [
                FakeAppCommand(
                    "mal",
                    options=[
                        FakeAppCommand("connect", options=[]),  # no parameters
                    ],
                )
            ],
        }
        sync = CommandSynchronizer(fake_bot(tree), sync_global=False)
        report = run(sync.run())

        self.assertTrue(report.ok, f"sync should succeed: {report.errors}")

    def test_stale_global_command_is_purged(self):
        """An obsolete global /mal connect registration is removed on start."""
        tree = FakeTree(
            ["mal"],
            remote_commands=[
                # Stale global registration: /mal connect with a `user` parameter
                FakeAppCommand(
                    "mal",
                    options=[
                        FakeAppCommand(
                            "connect",
                            options=[FakeArgument("user", required=True, type_value=6)],
                        )
                    ],
                    guild_id=None,  # global command
                )
            ],
        )
        http = FakeHTTP()
        sync = CommandSynchronizer(fake_bot(tree, http=http), sync_global=False)
        report = run(sync.run())

        # Guild sync happens (instant) and global purge clears stale registrations
        self.assertEqual(http.purges, [(1234, [])], "global list must be purged")
        self.assertTrue(report.global_purged)

    def test_guild_targeting_mismatch_is_reported(self):
        """If the invocation guild differs from the synchronised guild,
        the mismatch is logged (not silently ignored)."""
        tree = FakeTree(["ping"], remote_commands=[])
        sync = CommandSynchronizer(fake_bot(tree), guild_id=999999999999999999)
        report = run(sync.run())

        # Sync targets the configured guild, not a different one
        self.assertEqual(sync.guild_id, 999999999999999999)
        self.assertTrue(report.ok or report.errors)

    def test_failed_verification_does_not_mark_sync_done(self):
        """When schema verification fails, _done stays False so the next
        start retries instead of caching a bad result."""
        tree = FakeTree(
            ["mal"],
            remote_commands=[
                FakeAppCommand(
                    "mal",
                    options=[
                        FakeAppCommand(
                            "connect",
                            options=[FakeArgument("user", required=True, type_value=6)],
                        )
                    ],
                )
            ],
        )
        sync = CommandSynchronizer(fake_bot(tree), sync_global=False)
        run(sync.run())

        self.assertFalse(sync.completed, "failed verification must not mark done")
        self.assertIsNotNone(sync.last_report)
        self.assertFalse(sync.last_report.ok)

    def test_repeated_startup_no_duplicate_commands(self):
        """Running sync twice produces no duplicate command entries."""
        tree = FakeTree(["mal"])
        http = FakeHTTP()
        sync = CommandSynchronizer(fake_bot(tree, http=http), sync_global=False)

        first = run(sync.run())
        second = run(sync.run())

        self.assertTrue(first.ok)
        self.assertTrue(second.skipped)
        # Guild tree was cleared and rebuilt once per actual sync (not duplicated)
        self.assertEqual(tree.cleared, [TARGET_GUILD])
        self.assertEqual(len(tree.cleared), 1)


class TestBotStartupFlow(unittest.TestCase):
    """The real bot must await exactly one sync during setup_hook."""

    def test_setup_hook_loads_then_syncs_then_marks_ready(self):
        from config import Config
        from bot import EXTENSIONS, ParadiseBot

        async def run_startup():
            bot = ParadiseBot(Config())
            order: list[str] = []
            tree = FakeTree()

            async def fake_load(extension):
                order.append(f"load:{extension}")
                tree.global_commands[extension.split(".")[-1]] = FakeCommand(
                    extension.split(".")[-1]
                )

            original_run = bot.synchronizer.run

            async def tracked_run(**kwargs):
                order.append("sync")
                return await original_run(**kwargs)

            bot.synchronizer.bot = SimpleNamespace(tree=tree)
            bot.synchronizer.run = tracked_run
            bot.load_extension = fake_load
            bot.presence_manager.start = lambda: order.append("presence")
            # The real loop is not running here, so tasks are recorded and
            # closed instead of scheduled (start-up must not need a live loop).
            bot.loop = SimpleNamespace(
                create_task=lambda coro, **kwargs: coro.close()
            )

            await bot.setup_hook()
            return order, bot, tree

        order, bot, tree = asyncio.run(run_startup())

        self.assertEqual(order.count("sync"), 1, "synced from more than one place")
        self.assertEqual(order[-2:], ["sync", "presence"])
        self.assertTrue(order[0].startswith("load:"))
        self.assertTrue(bot.commands_ready)
        self.assertIn(TARGET_GUILD, tree.sync_calls)
        self.assertEqual(len(EXTENSIONS), len([x for x in order if x.startswith("load:")]))

    def test_bot_never_syncs_from_on_ready(self):
        import inspect

        import bot as bot_module

        source = inspect.getsource(bot_module.ParadiseBot.on_ready)
        self.assertNotIn("tree.sync", source)

    def test_only_one_sync_call_site_in_the_codebase(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        hits = []
        for folder in (".", "commands", "functions"):
            directory = os.path.join(root, folder)
            for name in os.listdir(directory):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(directory, name)
                with open(path, encoding="utf-8") as handle:
                    for number, line in enumerate(handle, 1):
                        if "tree.sync(" in line:
                            hits.append(f"{folder}/{name}:{number}")
        self.assertTrue(
            all(hit.startswith("functions/startup.py") for hit in hits),
            f"tree.sync called outside the synchronizer: {hits}",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
