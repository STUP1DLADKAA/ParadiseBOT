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


class FakeCommand:
    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<FakeCommand {self.name}>"


class FakeTree:
    """A command tree that records everything the synchroniser does."""

    def __init__(self, names=(), *, fail_guild=False, fail_global=False) -> None:
        self.global_commands = {name: FakeCommand(name) for name in names}
        self.guild_commands: dict[int, dict[str, FakeCommand]] = {}
        self.sync_calls: list[int | None] = []
        self.cleared: list[int] = []
        self.copied: list[int] = []
        self.fail_guild = fail_guild
        self.fail_global = fail_global

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


def fake_bot(tree: FakeTree):
    return SimpleNamespace(tree=tree)


def run(coro):
    return asyncio.run(coro)


class TestTargetGuild(unittest.TestCase):
    def test_primary_guild_is_the_configured_server(self):
        self.assertEqual(PRIMARY_GUILD_ID, TARGET_GUILD)

    def test_retired_commands_listed(self):
        for name in ("search", "convert", "time", "define", "dictionary",
                     "recommend", "weather"):
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

    def test_global_sync_also_runs_when_enabled(self):
        tree = FakeTree(["ping"])
        sync = CommandSynchronizer(fake_bot(tree), sync_global=True)
        report = run(sync.run())
        self.assertEqual(tree.sync_calls, [TARGET_GUILD, None])
        self.assertTrue(report.global_ok)

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

    def test_global_failure_does_not_break_guild_sync(self):
        tree = FakeTree(["ping"], fail_global=True)
        # Global sync is opt-in (production publishes to the home guild only),
        # so this test asks for it explicitly.
        sync = CommandSynchronizer(fake_bot(tree), sync_global=True)

        report = run(sync.run())

        self.assertTrue(report.ok)
        self.assertFalse(report.global_ok)
        self.assertTrue(any("global" in error for error in report.errors))

    def test_report_summary_leaks_nothing_sensitive(self):
        tree = FakeTree(["ping"])
        report = run(CommandSynchronizer(fake_bot(tree), sync_global=False).run())
        summary = report.summary()
        self.assertIn(str(TARGET_GUILD), summary)
        for secret in ("token", "api", "key"):
            self.assertNotIn(secret, summary.casefold())


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
            bot.loop = SimpleNamespace(create_task=lambda coro: coro.close())

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
