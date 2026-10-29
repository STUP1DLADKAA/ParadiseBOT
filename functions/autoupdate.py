"""Keep this checkout on the latest GitHub revision — automatically.

What it does
------------
Every time Paradise comes online (and, optionally, on a timer while it stays
online) it asks GitHub for the newest commit on the configured branch. If that
commit is newer than the one already applied here, it downloads that exact
revision as a tarball, stages it, and copies the files over this checkout —
then says so, loudly, in the terminal.

    🔄 auto-update: 12 file(s) updated → commit abc1234
    🔄 auto-update: restart Paradise to run the new code

The next start runs the new files. If nothing changed, it logs one quiet line
and carries on exactly as before:

    ✅ auto-update: already up to date (commit abc1234)

Safety rules (this writes to a live checkout, so they matter)
-------------------------------------------------------------
* **Never** touches ``.env``, ``.git``, ``data/``, virtualenvs, caches,
  SQLite files, logs, or anything listed in ``AUTO_UPDATE_PROTECTED``.
* The download is validated *before* a single file is replaced: it must
  contain ``bot.py`` + ``requirements.txt`` and every ``.py`` file must
  compile. A truncated/corrupt tarball changes nothing.
* Only files are copied; a file deleted upstream is only deleted here when
  the previous update wrote it (tracked in the state manifest), so local
  scratch files are never destroyed.
* Path traversal in the archive is rejected outright.
* Everything is best effort: offline, rate-limited, or read-only hosts log a
  warning and the bot keeps running on the code it already has.
* A failed apply restores the previous files from the staging backup.

The state (applied commit + manifest) lives in ``data/autoupdate_state.json``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import aiohttp

log = logging.getLogger("paradise.autoupdate")

GITHUB_API = "https://api.github.com"
CODELOAD = "https://codeload.github.com"

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_FILES = 4000
REQUEST_TIMEOUT = 45.0

# Paths that must survive an update untouched (relative to the repo root).
PROTECTED_NAMES: frozenset[str] = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        ".git",
        ".venv",
        "venv",
        "env",
        "data",
        "logs",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
)
PROTECTED_SUFFIXES: tuple[str, ...] = (
    ".db",
    ".sqlite",
    ".sqlite3",
    ".log",
    ".pid",
    ".session",
)
# Files that identify a Paradise checkout. A tarball without them is rejected.
REQUIRED_FILES: tuple[str, ...] = ("bot.py", "requirements.txt")


# A start-up update is skipped when the process keeps dying before it ever
# reaches the ready event (a bad commit must not own the process forever).
START_GUARD_WINDOW = 900.0
START_GUARD_LIMIT = 3


@dataclass(frozen=True, slots=True)
class UpdateResult:
    """What one check did."""

    status: str  # disabled|up-to-date|updated|available|skipped|error
    commit: str = ""
    changed: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    message: str = ""

    @property
    def applied(self) -> bool:
        return self.status == "updated"


@dataclass
class AutoUpdater:
    """Pulls the configured GitHub branch into this checkout."""

    repository: str = "STUP1DLADKAA/ParadiseBOT"
    branch: str = "main"
    enabled: bool = True
    apply: bool = True
    timeout: float = REQUEST_TIMEOUT
    token: str = ""
    root: Path = field(default_factory=Path.cwd)
    state_path: Path | None = None
    protected: tuple[str, ...] = ()
    # Keep a recoverable copy of every replaced file under data/.
    backup_files: bool = True
    session: aiohttp.ClientSession | None = None

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()
        if self.state_path is None:
            self.state_path = self.root / "data" / "autoupdate_state.json"
        self.state_path = Path(self.state_path)
        self._lock = asyncio.Lock()
        self._owns_session = self.session is None
        self._backup_files = bool(self.backup_files)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, config, *, root: str | os.PathLike[str] | None = None) -> "AutoUpdater":
        """Build from :class:`config.AutoUpdateConfig` (never raises)."""
        cfg = config
        state_dir = str(getattr(cfg, "state_dir", "") or "").strip()
        root_path = Path(root or Path.cwd())
        state_path = Path(state_dir) / "autoupdate_state.json" if state_dir else None
        return cls(
            repository=str(getattr(cfg, "repository", "") or "STUP1DLADKAA/ParadiseBOT"),
            branch=str(getattr(cfg, "branch", "") or "main"),
            enabled=bool(getattr(cfg, "enabled", True)),
            apply=bool(getattr(cfg, "apply", True)),
            timeout=float(getattr(cfg, "timeout", REQUEST_TIMEOUT) or REQUEST_TIMEOUT),
            token=str(getattr(cfg, "token", "") or ""),
            root=root_path,
            state_path=state_path,
            protected=tuple(getattr(cfg, "protected", ()) or ()),
        )

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def check(self) -> UpdateResult:
        """Look for a newer commit and (when allowed) apply it."""
        if not self.enabled:
            return UpdateResult("disabled", message="auto-update is switched off")

        async with self._lock:
            state = self._read_state()
            current = str(state.get("commit") or "")
            try:
                latest = await self._latest_commit()
            except Exception as error:  # noqa: BLE001 - never fatal
                return UpdateResult(
                    "error",
                    commit=current,
                    message=f"could not reach GitHub ({error.__class__.__name__})",
                )

            if not latest:
                return UpdateResult(
                    "error", commit=current, message="GitHub returned no commit"
                )
            if latest == current:
                if state.get("pending_restart"):
                    # The new code is running now — the flag has done its job.
                    self.mark_restarted()
                return UpdateResult("up-to-date", commit=current)
            if state.get("pending_restart") and self.apply:
                # Already applied, waiting for the restart: don't re-download.
                return UpdateResult(
                    "available",
                    commit=current,
                    message="an update is already applied — restart to run it",
                )

            if not self.apply:
                message = (
                    f"a newer revision is available ({latest[:7]})"
                    if current
                    else f"no local baseline yet — latest is {latest[:7]}"
                )
                return UpdateResult("available", commit=latest, message=message)

            try:
                changed, removed = await self._apply_commit(latest, state)
            except Exception as error:  # noqa: BLE001 - never fatal
                log.warning("Auto-update failed: %s", error, exc_info=True)
                return UpdateResult(
                    "error",
                    commit=current,
                    message=f"update could not be applied ({error.__class__.__name__})",
                )

            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if not changed and not removed:
                # The checkout already matched (first run, or an update that
                # only touched protected files). Record the baseline and do
                # NOT ask for a restart.
                self._update_state(
                    commit=latest,
                    applied_at=stamp,
                    files=[],
                    pending_restart=False,
                    repository=self.repository,
                    branch=self.branch,
                )
                return UpdateResult(
                    "up-to-date",
                    commit=latest,
                    message="checkout already matched the latest revision",
                )

            self._update_state(
                commit=latest,
                applied_at=stamp,
                files=sorted(changed),
                pending_restart=True,
                repository=self.repository,
                branch=self.branch,
            )
            return UpdateResult(
                "updated",
                commit=latest,
                changed=tuple(changed),
                removed=tuple(removed),
                message=f"{len(changed)} file(s) updated, {len(removed)} removed",
            )

    async def close(self) -> None:
        if self._owns_session and self.session is not None and not self.session.closed:
            await self.session.close()

    # ------------------------------------------------------------------
    # GitHub
    # ------------------------------------------------------------------

    async def _session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                connector=aiohttp.TCPConnector(limit=8, ttl_dns_cache=300),
            )
            self._owns_session = True
        return self.session

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "ParadiseBot-autoupdate",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _latest_commit(self) -> str:
        url = f"{GITHUB_API}/repos/{self.repository}/commits/{self.branch}"
        session = await self._session()
        async with session.get(url, headers=self._headers()) as response:
            if response.status != 200:
                raise RuntimeError(f"GitHub API returned {response.status}")
            data = await response.json(content_type=None)
        sha = str((data or {}).get("sha") or "").strip()
        if len(sha) < 7:
            raise RuntimeError("GitHub response did not contain a commit sha")
        return sha

    async def _download(self, sha: str) -> bytes:
        url = f"{CODELOAD}/{self.repository}/tar.gz/{sha}"
        session = await self._session()
        async with session.get(url, headers={"User-Agent": "ParadiseBot-autoupdate"}) as response:
            if response.status != 200:
                raise RuntimeError(f"download returned HTTP {response.status}")
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.content.iter_chunked(64 * 1024):
                total += len(chunk)
                if total > MAX_ARCHIVE_BYTES:
                    raise RuntimeError("repository archive is too large")
                chunks.append(chunk)
        if total < 128:
            raise RuntimeError("repository archive was empty")
        return b"".join(chunks)

    # ------------------------------------------------------------------
    # Applying
    # ------------------------------------------------------------------

    async def _apply_commit(
        self, sha: str, previous_state: dict
    ) -> tuple[list[str], list[str]]:
        archive = await self._download(sha)
        previous_files = {
            str(name) for name in previous_state.get("files", []) if str(name).strip()
        }

        with tempfile.TemporaryDirectory(prefix="paradise_update_") as tmp:
            staging = Path(tmp) / "tree"
            backup = Path(tmp) / "backup"
            staging.mkdir()
            backup.mkdir()
            self._extract(archive, staging)
            self._validate(staging)
            changed, written = self._overlay(staging, backup)
            removed = self._remove_orphans(previous_files, changed)
            # A restore never raises; the update is already logged above.
            if not changed and not removed:
                log.info("Auto-update: commit %s is identical on disk", sha[:7])
        return changed, removed

    @staticmethod
    def _extract(archive: bytes, destination: Path) -> None:
        """Safely unpack a GitHub tarball into *destination*."""
        import io

        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            members = []
            for member in tar.getmembers():
                if not (member.isfile() or member.isdir()):
                    continue
                parts = PurePosixPath(member.name).parts
                if not parts or parts[0] in ("..", "/") or ".." in parts:
                    raise RuntimeError("archive contains an unsafe path")
                members.append(member)
                if len(members) > MAX_FILES:
                    raise RuntimeError("archive contains too many files")
            try:
                # Python 3.11.4+ / 3.12: refuse absolute paths, links and
                # files that would escape the destination.
                tar.extractall(destination, members=members, filter="data")
            except TypeError:  # pragma: no cover - older 3.11 patch levels
                # Path traversal is already rejected above.
                tar.extractall(destination, members=members)

        # GitHub archives nest everything under a single top-level folder.
        entries = [entry for entry in destination.iterdir() if entry.is_dir()]
        if len(entries) == 1 and not any(p.is_file() for p in destination.iterdir()):
            inner = entries[0]
            for child in list(inner.iterdir()):
                shutil.move(str(child), str(destination / child.name))
            inner.rmdir()

    @staticmethod
    def _validate(tree: Path) -> None:
        for required in REQUIRED_FILES:
            if not (tree / required).is_file():
                raise RuntimeError(f"downloaded tree is missing {required}")
        checked = 0
        for path in tree.rglob("*.py"):
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as error:
                raise RuntimeError(f"could not read {path.name}: {error}") from error
            try:
                compile(source, str(path), "exec")
            except SyntaxError as error:
                raise RuntimeError(f"syntax error in {path.name}: {error}") from error
            checked += 1
            if checked > MAX_FILES:
                break
        if checked == 0:
            raise RuntimeError("downloaded tree contains no Python files")

    def _is_protected(self, relative: PurePosixPath) -> bool:
        parts = relative.parts
        if not parts:
            return True
        for part in parts:
            if part in PROTECTED_NAMES:
                return True
            if part.startswith(".env") and part != ".env.example":
                return True
            if part.endswith(PROTECTED_SUFFIXES):
                return True
        if any(part.startswith("__pycache__") for part in parts):
            return True
        as_posix = relative.as_posix()
        for extra in self.protected:
            cleaned = extra.strip().strip("/")
            if cleaned and (as_posix == cleaned or as_posix.startswith(cleaned + "/")):
                return True
        return False

    def _overlay(self, tree: Path, backup: Path) -> tuple[list[str], list[str]]:
        """Copy *tree* over the checkout; return (changed, written) paths."""
        changed: list[str] = []
        written: list[str] = []
        for source in sorted(tree.rglob("*")):
            if not source.is_file():
                continue
            relative = PurePosixPath(source.relative_to(tree).as_posix())
            if self._is_protected(relative):
                continue
            target = self.root / relative.as_posix()
            try:
                new_bytes = source.read_bytes()
            except OSError as error:
                raise RuntimeError(f"could not read {relative}: {error}") from error
            if target.is_file():
                try:
                    if target.read_bytes() == new_bytes:
                        continue
                except OSError:
                    pass
                self._backup(target, backup, relative)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(target.name + ".paradise-new")
                tmp.write_bytes(new_bytes)
                os.replace(tmp, target)
            except OSError as error:
                self._restore(backup)
                raise RuntimeError(f"could not write {relative}: {error}") from error
            changed.append(relative.as_posix())
            written.append(relative.as_posix())
        return changed, written

    def _remove_orphans(self, previous: set[str], changed: list[str]) -> list[str]:
        """Delete files a previous update wrote that no longer exist upstream."""
        removed: list[str] = []
        for name in sorted(previous):
            if name in changed:
                continue
            relative = PurePosixPath(name)
            if self._is_protected(relative):
                continue
            target = self.root / relative.as_posix()
            if target.is_file():
                try:
                    target.unlink()
                    removed.append(name)
                except OSError:
                    log.debug("Could not remove orphaned file %s", name)
        return removed

    def _backup(self, target: Path, backup: Path, relative: PurePosixPath) -> None:
        """Copy the current file aside before replacing it.

        Two destinations: a short-lived one used to roll back a failed apply,
        and ``data/autoupdate_backup/latest`` (bounded) so the previous version
        of every replaced file is recoverable after a successful update too.
        """
        for destination in (backup, self._persistent_backup_dir()):
            if destination is None:
                continue
            try:
                target_path = destination / relative.as_posix()
                target_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, target_path)
            except OSError:
                log.debug("Could not back up %s", relative, exc_info=True)

    def _persistent_backup_dir(self) -> Path | None:
        if not self._backup_files:
            return None
        try:
            directory = self.root / "data" / "autoupdate_backup" / "latest"
            directory.mkdir(parents=True, exist_ok=True)
            return directory
        except OSError:
            return None

    def _restore(self, backup: Path) -> None:
        for source in backup.rglob("*"):
            if not source.is_file():
                continue
            target = self.root / source.relative_to(backup).as_posix()
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            except OSError:
                log.warning("Could not restore %s", target, exc_info=True)

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def _read_state(self) -> dict:
        if self.state_path is None:
            return {}
        try:
            with open(self.state_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_state(self, state: dict) -> None:
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_name(self.state_path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2, sort_keys=True)
            os.replace(tmp, self.state_path)
        except OSError:
            log.debug("Could not persist auto-update state", exc_info=True)

    # ------------------------------------------------------------------
    # Crash-loop guard
    #
    # Auto-updating from a moving branch means a bad commit could take the bot
    # down — and a supervisor would restart it straight back into the same bad
    # code. If the process keeps starting without ever reaching the ready
    # event, stop updating on start-up until a human looks at it.
    # ------------------------------------------------------------------

    def register_start(self) -> int:
        """Count this start; consecutive quick starts mean a crash loop."""
        state = self._read_state()
        now = time.time()
        try:
            last = float(state.get("last_start_at") or 0.0)
            starts = int(state.get("starts") or 0)
        except (TypeError, ValueError):
            last, starts = 0.0, 0
        starts = starts + 1 if last and now - last <= START_GUARD_WINDOW else 1
        self._update_state(starts=starts, last_start_at=round(now, 3))
        return starts

    def note_ready(self) -> None:
        """We reached Discord: the current code runs, so reset the counter."""
        if not self._read_state():
            return
        self._update_state(starts=0, last_ready_at=round(time.time(), 3))

    def crash_looping(self) -> bool:
        try:
            return int(self._read_state().get("starts") or 0) > START_GUARD_LIMIT
        except (TypeError, ValueError):
            return False

    def _update_state(self, **fields: object) -> None:
        state = self._read_state()
        state.update(fields)
        self._write_state(state)

    def mark_restarted(self) -> None:
        """Clear the pending-restart flag once the new code is actually live."""
        state = self._read_state()
        if state.pop("pending_restart", None):
            self._write_state(state)


# ---------------------------------------------------------------------------
# Terminal reporting
# ---------------------------------------------------------------------------


def report(result: UpdateResult, *, autoupdater: AutoUpdater | None = None) -> None:
    """Print the outcome in a form that is obvious in a terminal."""
    short = result.commit[:7] if result.commit else "unknown"
    if result.status == "updated":
        log.warning("=" * 68)
        log.warning("🔄 AUTO-UPDATE APPLIED — %s", result.message)
        log.warning("   repository: %s@%s", autoupdater.repository if autoupdater else "?", short)
        for name in list(result.changed)[:25]:
            log.warning("   + %s", name)
        if len(result.changed) > 25:
            log.warning("   + … and %d more file(s)", len(result.changed) - 25)
        for name in list(result.removed)[:10]:
            log.warning("   - %s", name)
        log.warning("   RESTART Paradise to run the new code.")
        log.warning("=" * 68)
    elif result.status == "up-to-date":
        log.info("✅ auto-update: already up to date (commit %s)", short)
    elif result.status == "available":
        log.warning("🔄 auto-update: %s", result.message or "update available")
    elif result.status == "skipped":
        log.warning("⏸️ auto-update: %s", result.message)
    elif result.status == "error":
        log.warning("⚠️ auto-update: %s", result.message)
    else:
        log.info("auto-update: %s", result.message or result.status)


# ---------------------------------------------------------------------------
# Lifecycle helpers
# ---------------------------------------------------------------------------


async def run_startup_check(config, *, root: str | os.PathLike[str] | None = None) -> UpdateResult:
    """One check before the bot logs in (safe, bounded, never raises)."""
    cfg = getattr(config, "autoupdate", None)
    if cfg is None or not bool(getattr(cfg, "check_on_start", True)):
        return UpdateResult("disabled", message="start-up check disabled")
    updater = AutoUpdater.from_config(cfg, root=root)
    updater.timeout = min(
        updater.timeout, float(getattr(cfg, "startup_timeout", 20.0) or 20.0)
    )
    updater.register_start()
    if updater.crash_looping():
        log.warning(
            "auto-update: the bot has restarted %d times without becoming ready; "
            "skipping the start-up update so it can stay up",
            START_GUARD_LIMIT + 1,
        )
        return UpdateResult("skipped", message="start-up update skipped (crash loop guard)")
    try:
        result = await updater.check()
        report(result, autoupdater=updater)
        return result
    finally:
        await updater.close()


def note_ready(config, *, root: str | os.PathLike[str] | None = None) -> None:
    """Reset the crash-loop counter once the bot is actually online."""
    cfg = getattr(config, "autoupdate", None)
    if cfg is None:
        return
    try:
        AutoUpdater.from_config(cfg, root=root).note_ready()
    except Exception:  # noqa: BLE001 - bookkeeping must never break start-up
        log.debug("Could not clear the auto-update start counter", exc_info=True)


async def watch(
    bot,
    config,
    *,
    root: str | os.PathLike[str] | None = None,
) -> None:
    """Check for updates periodically while the bot stays online."""
    cfg = getattr(config, "autoupdate", None)
    if cfg is None or not bool(getattr(cfg, "enabled", True)):
        return
    interval = float(getattr(cfg, "interval", 0.0) or 0.0)
    if interval <= 0:
        return
    updater = AutoUpdater.from_config(cfg, root=root)
    # The start-up check may already have applied a revision.
    if str(updater._read_state().get("commit") or ""):
        log.info(
            "auto-update: watching %s@%s every %ds",
            updater.repository,
            updater.branch,
            int(interval),
        )
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                result = await updater.check()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must never die
                log.debug("Auto-update cycle failed", exc_info=True)
                continue
            report(result, autoupdater=updater)
    except asyncio.CancelledError:
        raise
    finally:
        await updater.close()


__all__ = [
    "AutoUpdater",
    "UpdateResult",
    "report",
    "run_startup_check",
    "note_ready",
    "watch",
]
