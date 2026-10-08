"""Idempotent SQLite → Supabase migration for historical voice data.

Guarantees
----------
* **Bounded batches** — memory usage never depends on database size.
* **Verified deletions** — a local row is deleted only after the matching
  Supabase row was uploaded *and* re-read successfully.
* **Idempotent** — running twice (or after a crash halfway through) never
  duplicates history:
    - sessions dedupe on their old SQLite id (``source_id``);
    - daily totals merge through the ``voice_migration_state`` ledger, which
      records how much of each SQLite value was already applied;
    - profiles/dm-reports/active-rows upsert on their natural keys.
* **Failure-safe** — if Supabase is unreachable at any point the remaining
  SQLite rows simply stay where they are and the next cycle retries.
* **Surgical** — only the five voice tables are touched; ``user_settings``
  and any other Paradise data in the same SQLite file are left alone.

The bot never deletes ``data/paradise.db`` itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, Sequence

from .database import VoiceDatabase
from .models import DailyUserStat, MemberProfile, PersistedActiveSession, VoiceSessionRecord
from .store import VoiceStoreUnavailable

log = logging.getLogger(__name__)


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


class MigrationBackend(Protocol):
    """The Supabase operations the migrator needs (see supabase_database.py)."""

    async def upload_migration_sessions(self, rows: Sequence[VoiceSessionRecord]) -> None: ...
    async def fetch_migration_sessions(self, source_ids: Sequence[int]) -> list[dict]: ...
    async def merge_migration_daily(self, rows: Sequence[DailyUserStat]) -> list[dict]: ...
    async def fetch_migration_daily(self, keys: Sequence[tuple[int, int, str]]) -> list[dict]: ...
    async def upload_migration_profiles(self, rows: Sequence[tuple[MemberProfile, str]]) -> None: ...
    async def fetch_migration_profiles(self, keys: Sequence[tuple[int, int]]) -> list[dict]: ...
    async def upload_migration_active(self, rows: Sequence[PersistedActiveSession]) -> None: ...
    async def fetch_migration_active(self, keys: Sequence[tuple[int, int]]) -> list[dict]: ...
    async def upload_migration_dm_reports(self, rows: Sequence[tuple[str, int, int, str]]) -> None: ...
    async def fetch_migration_dm_reports(self, keys: Sequence[tuple[str, int, int]]) -> list[dict]: ...


@dataclass
class TableReport:
    table: str
    uploaded: int = 0
    verified_deleted: int = 0
    verification_failures: int = 0
    interrupted: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        names = {
            "voice_sessions": "sessions",
            "voice_daily_stats": "daily records",
            "voice_member_profiles": "member profiles",
            "voice_active_sessions": "active sessions",
            "voice_dm_reports": "DM report records",
        }
        return names.get(self.table, self.table)


class VoiceMigrator:
    """Move the five legacy voice tables into Supabase, batch by batch."""

    def __init__(self, local: VoiceDatabase, backend: MigrationBackend, batch_size: int = 500) -> None:
        self.local = local
        self.backend = backend
        self.batch_size = max(1, int(batch_size))

    async def run(self) -> dict[str, TableReport]:
        reports: dict[str, TableReport] = {}
        for migrate in (
            self._migrate_sessions,
            self._migrate_daily,
            self._migrate_profiles,
            self._migrate_active,
            self._migrate_dm_reports,
        ):
            report = await migrate()
            reports[report.table] = report
            self._log_report(report)
        self._log_summary(reports)
        return reports

    # ------------------------------------------------------------------
    # voice_sessions — dedupe on the old SQLite row id
    # ------------------------------------------------------------------

    async def _migrate_sessions(self) -> TableReport:
        report = TableReport("voice_sessions")
        while True:
            batch = self.local.fetch_migration_sessions(self.batch_size)
            if not batch:
                break
            try:
                await self.backend.upload_migration_sessions(batch)
                remote_rows = await self.backend.fetch_migration_sessions([r.id for r in batch])
            except VoiceStoreUnavailable as err:
                log.warning("Session migration paused (will retry): %s", err)
                report.interrupted = True
                break

            remote = {}
            for row in remote_rows:
                try:
                    remote[int(row["source_id"])] = row
                except (KeyError, TypeError, ValueError):
                    continue

            ok_ids: list[int] = []
            for r in batch:
                row = remote.get(r.id)
                if row is not None and self._session_matches(row, r):
                    ok_ids.append(r.id)
                else:
                    report.verification_failures += 1
                    log.error(
                        "Session source_id=%s failed verification; local row kept", r.id
                    )
            if ok_ids:
                self.local.delete_migration_sessions(ok_ids)
                report.verified_deleted += len(ok_ids)
            report.uploaded += len(batch)
            if not ok_ids:
                log.error("No sessions in this batch verified — stopping for this run")
                break
        return report

    @staticmethod
    def _session_matches(row: dict, record: VoiceSessionRecord) -> bool:
        try:
            return (
                int(row["guild_id"]) == record.guild_id
                and int(row["user_id"]) == record.user_id
                and int(row["channel_id"]) == record.channel_id
                and int(row["duration_seconds"]) == record.duration_seconds
                and _parse_dt(row["started_at"]) == record.started_at
                and _parse_dt(row["ended_at"]) == record.ended_at
            )
        except (KeyError, TypeError, ValueError):
            return False

    # ------------------------------------------------------------------
    # voice_daily_stats — exactly-once ledger merge
    # ------------------------------------------------------------------

    async def _migrate_daily(self) -> TableReport:
        report = TableReport("voice_daily_stats")
        sqlite_total = 0
        supabase_total = 0
        while True:
            batch = self.local.fetch_migration_daily(self.batch_size)
            if not batch:
                break
            keys = [(r.guild_id, r.user_id, r.date_str) for r in batch]
            try:
                merge_results = await self.backend.merge_migration_daily(batch)
                remote_rows = await self.backend.fetch_migration_daily(keys)
            except VoiceStoreUnavailable as err:
                log.warning("Daily stats migration paused (will retry): %s", err)
                report.interrupted = True
                break

            def key_of(row: dict) -> tuple[int, int, str] | None:
                try:
                    return (int(row["guild_id"]), int(row["user_id"]), str(row["date"])[:10])
                except (KeyError, TypeError, ValueError):
                    return None

            remote = {}
            for row in remote_rows:
                k = key_of(row)
                if k is not None:
                    try:
                        remote[k] = int(row["duration_seconds"])
                    except (TypeError, ValueError):
                        continue
            expected = {}
            for row in merge_results:
                k = key_of(row)
                if k is not None:
                    try:
                        expected[k] = int(row["resulting_seconds"])
                    except (TypeError, ValueError):
                        continue

            ok_keys: list[tuple[int, int, str]] = []
            for r in batch:
                k = (r.guild_id, r.user_id, r.date_str)
                if k in remote and remote[k] == expected.get(k):
                    ok_keys.append(k)
                    sqlite_total += r.duration_seconds
                    supabase_total += remote[k]
                else:
                    report.verification_failures += 1
                    log.error(
                        "Daily record %s failed verification; local row kept", k
                    )
            if ok_keys:
                self.local.delete_migration_daily(ok_keys)
                report.verified_deleted += len(ok_keys)
            report.uploaded += len(batch)
            if not ok_keys:
                log.error("No daily records in this batch verified — stopping for this run")
                break
        report.extra = {
            "sqlite_seconds": sqlite_total,
            "supabase_seconds": supabase_total,
        }
        return report

    # ------------------------------------------------------------------
    # voice_member_profiles — last-write-wins identity upsert
    # ------------------------------------------------------------------

    async def _migrate_profiles(self) -> TableReport:
        report = TableReport("voice_member_profiles")
        while True:
            batch = self.local.fetch_migration_profiles_meta(self.batch_size)
            if not batch:
                break
            keys = [(p.guild_id, p.user_id) for p, _ in batch]
            try:
                await self.backend.upload_migration_profiles(batch)
                remote_rows = await self.backend.fetch_migration_profiles(keys)
            except VoiceStoreUnavailable as err:
                log.warning("Profile migration paused (will retry): %s", err)
                report.interrupted = True
                break

            remote = {}
            for row in remote_rows:
                try:
                    remote[(int(row["guild_id"]), int(row["user_id"]))] = row
                except (KeyError, TypeError, ValueError):
                    continue

            ok_keys: list[tuple[int, int]] = []
            for profile, _updated_at in batch:
                row = remote.get((profile.guild_id, profile.user_id))
                if row is not None and self._profile_matches(row, profile):
                    ok_keys.append((profile.guild_id, profile.user_id))
                else:
                    report.verification_failures += 1
                    log.error(
                        "Profile (%s, %s) failed verification; local row kept",
                        profile.guild_id,
                        profile.user_id,
                    )
            if ok_keys:
                self.local.delete_migration_profiles(ok_keys)
                report.verified_deleted += len(ok_keys)
            report.uploaded += len(batch)
            if not ok_keys:
                log.error("No profiles in this batch verified — stopping for this run")
                break
        return report

    @staticmethod
    def _profile_matches(row: dict, profile: MemberProfile) -> bool:
        try:
            if str(row["display_name"]) != profile.display_name:
                return False
            if str(row["username"]) != profile.username:
                return False
            # A NULL incoming avatar intentionally keeps any stored avatar
            # (historic COALESCE semantics), so only compare when we sent one.
            if profile.avatar_url:
                return (row.get("avatar_url") or None) == profile.avatar_url
            return True
        except (KeyError, TypeError, ValueError):
            return False

    # ------------------------------------------------------------------
    # voice_active_sessions — flush local mirrors (last-write-wins)
    # ------------------------------------------------------------------

    async def _migrate_active(self) -> TableReport:
        report = TableReport("voice_active_sessions")
        while True:
            batch = self.local.fetch_migration_active(self.batch_size)
            if not batch:
                break
            keys = [(s.guild_id, s.user_id) for s in batch]
            try:
                await self.backend.upload_migration_active(batch)
                remote_rows = await self.backend.fetch_migration_active(keys)
            except VoiceStoreUnavailable as err:
                log.warning("Active session migration paused (will retry): %s", err)
                report.interrupted = True
                break

            remote = {}
            for row in remote_rows:
                try:
                    remote[(int(row["guild_id"]), int(row["user_id"]))] = row
                except (KeyError, TypeError, ValueError):
                    continue

            ok_keys: list[tuple[int, int]] = []
            for session in batch:
                row = remote.get((session.guild_id, session.user_id))
                # Verified when Supabase holds our row or something fresher
                # (a live heartbeat may have won the last-write-wins race).
                if row is not None:
                    last_seen = _parse_dt(row.get("last_seen_at"))
                    if last_seen is not None and last_seen >= session.last_seen_at:
                        ok_keys.append((session.guild_id, session.user_id))
                        continue
                report.verification_failures += 1
                log.error(
                    "Active session (%s, %s) failed verification; local row kept",
                    session.guild_id,
                    session.user_id,
                )
            if ok_keys:
                self.local.delete_migration_active(ok_keys)
                report.verified_deleted += len(ok_keys)
            report.uploaded += len(batch)
            if not ok_keys:
                log.error("No active sessions in this batch verified — stopping for this run")
                break
        return report

    # ------------------------------------------------------------------
    # voice_dm_reports — insert-if-absent sent-state
    # ------------------------------------------------------------------

    async def _migrate_dm_reports(self) -> TableReport:
        report = TableReport("voice_dm_reports")
        while True:
            batch = self.local.fetch_migration_dm_reports(self.batch_size)
            if not batch:
                break
            keys = [(d, g, u) for d, g, u, _ in batch]
            try:
                await self.backend.upload_migration_dm_reports(batch)
                remote_rows = await self.backend.fetch_migration_dm_reports(keys)
            except VoiceStoreUnavailable as err:
                log.warning("DM report migration paused (will retry): %s", err)
                report.interrupted = True
                break

            remote = set()
            for row in remote_rows:
                try:
                    remote.add(
                        (str(row["date"])[:10], int(row["guild_id"]), int(row["user_id"]))
                    )
                except (KeyError, TypeError, ValueError):
                    continue

            ok_keys = [k for k in keys if k in remote]
            missing = len(keys) - len(ok_keys)
            report.verification_failures += missing
            for k in keys:
                if k not in remote:
                    log.error("DM report %s failed verification; local row kept", k)
            if ok_keys:
                self.local.delete_migration_dm_reports(ok_keys)
                report.verified_deleted += len(ok_keys)
            report.uploaded += len(batch)
            if not ok_keys:
                log.error("No DM report rows in this batch verified — stopping for this run")
                break
        return report

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log_report(self, report: TableReport) -> None:
        if report.uploaded == 0 and report.verified_deleted == 0:
            return
        log.info(
            "Migrated %s %s; deleted %s verified local %s records",
            f"{report.uploaded:,}",
            report.label,
            f"{report.verified_deleted:,}",
            report.label,
        )
        if report.verification_failures:
            log.warning(
                "%s %s row(s) failed verification and remain local",
                report.verification_failures,
                report.label,
            )
        if report.table == "voice_daily_stats":
            extra = report.extra
            log.info(
                "Daily totals for migrated keys: SQLite %s seconds -> Supabase %s seconds "
                "(difference is live activity recorded during the migration)",
                f"{extra.get('sqlite_seconds', 0):,}",
                f"{extra.get('supabase_seconds', 0):,}",
            )

    def _log_summary(self, reports: dict[str, TableReport]) -> None:
        remaining = {
            t: self.local.count_migration_rows(t)
            for t in (
                "voice_sessions",
                "voice_daily_stats",
                "voice_member_profiles",
                "voice_active_sessions",
                "voice_dm_reports",
            )
        }
        if any(remaining.values()):
            log.warning(
                "Voice migration still pending (will continue automatically): %s",
                ", ".join(f"{t}={c}" for t, c in remaining.items() if c),
            )
        else:
            log.info(
                "Voice migration complete — all local voice history now lives in "
                "Supabase (user settings stay in SQLite by design)."
            )
