"""Offline tests for the GitHub auto-updater.

Every network call is replaced with a locally-built tarball, so these tests
prove the important part: what happens to the checkout. Nothing here talks to
GitHub.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from functions.autoupdate import AutoUpdater, UpdateResult, report


def make_tarball(files: dict[str, str], *, prefix: str = "ParadiseBOT-main") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(f"{prefix}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class FakeUpdater(AutoUpdater):
    """An updater whose GitHub answers are supplied by the test."""

    def __init__(self, *args, commit: str = "abc1234def5678", archive: bytes = b"", **kwargs):
        super().__init__(*args, **kwargs)
        self.commit = commit
        self.archive = archive
        self.downloads = 0

    async def _latest_commit(self) -> str:
        return self.commit

    async def _download(self, sha: str) -> bytes:
        self.downloads += 1
        return self.archive


class AutoUpdateTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        (self.root / "bot.py").write_text("print('old')\n", encoding="utf-8")
        (self.root / "requirements.txt").write_text("discord.py\n", encoding="utf-8")
        (self.root / "commands").mkdir()
        (self.root / "commands" / "old.py").write_text("OLD = True\n", encoding="utf-8")
        (self.root / "commands" / "orphan.py").write_text("GONE = 1\n", encoding="utf-8")
        (self.root / ".env").write_text("LOCAL_ONLY_SETTING=keep-me\n", encoding="utf-8")
        (self.root / "data").mkdir()
        (self.root / "data" / "paradise.db").write_text("local db bytes", encoding="utf-8")
        self.state = Path(self.tmp.name) / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _updater(self, archive: bytes, **kwargs) -> FakeUpdater:
        return FakeUpdater(
            root=self.root,
            state_path=self.state,
            archive=archive,
            **kwargs,
        )

    def _good_archive(self) -> bytes:
        return make_tarball(
            {
                "bot.py": "print('new')\n",
                "requirements.txt": "discord.py>=2.6\n",
                "commands/new.py": "NEW = True\n",
                "commands/old.py": "OLD = False\n",
                ".env": "LOCAL_ONLY_SETTING=overwritten\n",
                "data/paradise.db": "should never land",
                "README.md": "# updated\n",
            }
        )

    # -- applying ---------------------------------------------------------

    async def test_apply_copies_files_and_protects_local_state(self):
        updater = self._updater(self._good_archive(), commit="abc1234def5678")
        result = await updater.check()

        self.assertEqual(result.status, "updated")
        self.assertEqual(result.commit, "abc1234def5678")
        self.assertTrue(result.applied)

        self.assertEqual((self.root / "bot.py").read_text(), "print('new')\n")
        self.assertEqual((self.root / "commands" / "new.py").read_text(), "NEW = True\n")
        self.assertEqual(
            (self.root / "commands" / "old.py").read_text(), "OLD = False\n"
        )
        # Local secrets, data and untracked files are untouched.
        self.assertEqual(
            (self.root / ".env").read_text(), "LOCAL_ONLY_SETTING=keep-me\n"
        )
        self.assertTrue((self.root / "data" / "paradise.db").is_file())
        # The orphan was not in the previous manifest, so it stays.
        self.assertTrue((self.root / "commands" / "orphan.py").is_file())

        state = json.loads(self.state.read_text())
        self.assertEqual(state["commit"], "abc1234def5678")
        self.assertTrue(state["pending_restart"])
        self.assertIn("bot.py", state["files"])
        await updater.close()

    async def test_second_check_reports_up_to_date(self):
        self.state.write_text(json.dumps({"commit": "abc1234def5678", "files": []}))
        updater = self._updater(self._good_archive(), commit="abc1234def5678")
        result = await updater.check()
        self.assertEqual(result.status, "up-to-date")
        self.assertEqual(updater.downloads, 0)
        await updater.close()

    async def test_previous_manifest_orphans_are_removed(self):
        self.state.write_text(
            json.dumps({"commit": "old", "files": ["commands/orphan.py"]})
        )
        updater = self._updater(self._good_archive())
        result = await updater.check()
        self.assertEqual(result.status, "updated")
        self.assertIn("commands/orphan.py", result.removed)
        self.assertFalse((self.root / "commands" / "orphan.py").exists())
        await updater.close()

    async def test_first_run_with_identical_files_does_not_request_a_restart(self):
        # A fresh checkout that already matches upstream only records a baseline.
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        (other / "bot.py").write_text("print('same')\n", encoding="utf-8")
        (other / "requirements.txt").write_text("discord.py\n", encoding="utf-8")
        state_path = Path(self.tmp.name) / "other_state.json"
        same = FakeUpdater(
            root=other,
            state_path=state_path,
            archive=make_tarball(
                {"bot.py": "print('same')\n", "requirements.txt": "discord.py\n"}
            ),
            commit="abc1234def5678",
        )
        result = await same.check()
        self.assertEqual(result.status, "up-to-date")
        state = json.loads(state_path.read_text())
        self.assertFalse(state["pending_restart"])
        await same.close()

    async def test_replaced_files_are_backed_up_under_data(self):
        updater = self._updater(self._good_archive())
        await updater.check()
        backup = self.root / "data" / "autoupdate_backup" / "latest" / "bot.py"
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_text(), "print('old')\n")
        await updater.close()

    async def test_report_mode_changes_nothing(self):
        updater = self._updater(self._good_archive(), apply=False)
        result = await updater.check()
        self.assertEqual(result.status, "available")
        self.assertEqual((self.root / "bot.py").read_text(), "print('old')\n")
        await updater.close()

    async def test_disabled_returns_early(self):
        updater = self._updater(self._good_archive(), enabled=False)
        result = await updater.check()
        self.assertEqual(result.status, "disabled")
        await updater.close()

    # -- rejecting bad downloads -----------------------------------------

    async def test_syntax_error_is_rejected_and_nothing_changes(self):
        archive = make_tarball(
            {
                "bot.py": "def broken(:\n",
                "requirements.txt": "discord.py\n",
            }
        )
        updater = self._updater(archive)
        result = await updater.check()
        self.assertEqual(result.status, "error")
        self.assertEqual((self.root / "bot.py").read_text(), "print('old')\n")
        self.assertFalse(self.state.exists() and json.loads(self.state.read_text()))
        await updater.close()

    async def test_missing_required_files_are_rejected(self):
        archive = make_tarball({"README.md": "# nope\n"})
        updater = self._updater(archive)
        result = await updater.check()
        self.assertEqual(result.status, "error")
        self.assertEqual((self.root / "bot.py").read_text(), "print('old')\n")
        await updater.close()

    async def test_path_traversal_is_rejected(self):
        archive = make_tarball({"bot.py": "x = 1\n", "requirements.txt": "", "../../evil.py": "pwned = 1\n"})
        updater = self._updater(archive)
        result = await updater.check()
        self.assertEqual(result.status, "error")
        self.assertFalse((Path(self.tmp.name) / "evil.py").exists())
        await updater.close()

    async def test_unreachable_github_is_not_fatal(self):
        class Offline(FakeUpdater):
            async def _latest_commit(self) -> str:
                raise RuntimeError("no network")

        updater = Offline(root=self.root, state_path=self.state)
        result = await updater.check()
        self.assertEqual(result.status, "error")
        self.assertIn("GitHub", result.message)
        await updater.close()

    # -- guard rails ------------------------------------------------------

    def test_protected_paths(self):
        updater = self._updater(b"")
        from pathlib import PurePosixPath

        for protected in (
            ".env",
            ".env.local",
            ".git/config",
            "data/paradise.db",
            "venv/bin/python",
            "__pycache__/x.pyc",
            "app.log",
            "nested/.env.production",
        ):
            self.assertTrue(
                updater._is_protected(PurePosixPath(protected)), protected
            )
        for allowed in ("bot.py", "commands/wake.py", ".env.example", "README.md"):
            self.assertFalse(updater._is_protected(PurePosixPath(allowed)), allowed)

    def test_extra_protected_paths_from_config(self):
        updater = self._updater(b"", protected=("config/local_settings.py",))
        from pathlib import PurePosixPath

        self.assertTrue(updater._is_protected(PurePosixPath("config/local_settings.py")))

    def test_report_mentions_restart_on_update(self):
        # Another test module disables global logging; report() uses log.warning.
        logging.disable(logging.NOTSET)
        result = UpdateResult(
            "updated", commit="deadbeef", changed=("bot.py",), removed=()
        )
        with self.assertLogs("paradise.autoupdate", level="WARNING") as logs:
            report(result, autoupdater=self._updater(b""))
        joined = "\n".join(logs.output)
        self.assertIn("AUTO-UPDATE APPLIED", joined)
        self.assertIn("RESTART", joined)
        self.assertIn("bot.py", joined)

    def test_report_is_quiet_when_up_to_date(self):
        logging.disable(logging.NOTSET)
        with self.assertLogs("paradise.autoupdate", level="INFO") as logs:
            report(UpdateResult("up-to-date", commit="deadbeef"))
        self.assertIn("already up to date", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
