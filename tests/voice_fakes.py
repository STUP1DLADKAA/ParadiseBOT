"""Test doubles for the Supabase voice layer.

``FakeSupabaseClient`` emulates the small slice of the official supabase
client used by ``SupabaseVoiceDatabase`` (rpc + table query builders) against
in-memory tables, reproducing the semantics of the PostgreSQL functions in
``supabase/migrations/001_voice_activity.sql``:

* unique keys (session identity, source_id, daily/active/profile/report keys)
* midnight splitting (reuses the shared ``split_session_by_midnight``)
* last-write-wins heartbeats and profile upserts
* the exactly-once migration ledger
* ISO-week and calendar-month leaderboard windows in IST

This lets the full production store + migrator be tested offline. The SQL
itself is validated separately by ``supabase/validate_locally.mjs`` against a
real PostgreSQL; live verification against the actual Supabase project still
requires the owner's credentials.
"""

from __future__ import annotations

import asyncio
import itertools
from datetime import date, datetime, timedelta, timezone

from functions.voice.database import split_session_by_midnight


class FakeNetworkError(RuntimeError):
    """Raised by the fake client to simulate Supabase being unreachable."""


def _parse_dt(value):
    if value is None:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _parse_date(value):
    if value is None:
        return None
    return date.fromisoformat(str(value)[:10])


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeRpcBuilder:
    def __init__(self, client, fn, params):
        self._client = client
        self._fn = fn
        self._params = params or {}

    async def execute(self):
        await self._client._record("rpc", self._fn, self._params)
        return _FakeResult(await self._client._run_rpc(self._fn, self._params))


class _FakeTableBuilder:
    def __init__(self, client, table):
        self._client = client
        self._table = table
        self._filters = []  # (column, op, value)
        self._orders = []  # (column, desc)
        self._limit = None

    def select(self, *columns):
        self._columns = ",".join(columns).split(",") if columns else None
        return self

    def eq(self, column, value):
        self._filters.append((column, "eq", value))
        return self

    def in_(self, column, values):
        self._filters.append((column, "in", list(values)))
        return self

    def order(self, column, desc=False):
        self._orders.append((column, desc))
        return self

    def limit(self, n):
        self._limit = n
        return self

    def upsert(self, rows, *, on_conflict="", ignore_duplicates=False, **kwargs):
        self._upsert_rows = rows if isinstance(rows, list) else [rows]
        self._on_conflict = on_conflict
        self._ignore_duplicates = ignore_duplicates
        self._mode = "upsert"
        return self

    def insert(self, rows, **kwargs):
        self._upsert_rows = rows if isinstance(rows, list) else [rows]
        self._mode = "insert"
        return self

    def delete(self):
        self._mode = "delete"
        return self

    # -- execution ------------------------------------------------------

    def _matches(self, row):
        for column, op, value in self._filters:
            cell = row.get(column)
            if op == "eq":
                if str(cell) != str(value):
                    return False
            elif op == "in":
                if str(cell) not in {str(v) for v in value}:
                    return False
        return True

    async def execute(self):
        mode = getattr(self, "_mode", "select")
        await self._client._record("table", f"{self._table}:{mode}")
        if self._client.fail_mode:
            raise FakeNetworkError("simulated connection failure")
        table = self._client._tables[self._table]
        if getattr(self, "_mode", None) == "delete":
            removed = [k for k, row in table.items() if self._matches(row)]
            for k in removed:
                del table[k]
            return _FakeResult([])
        if getattr(self, "_mode", None) in ("upsert", "insert"):
            return _FakeResult(self._client._apply_upsert(self._table, self))
        rows = [row for row in table.values() if self._matches(row)]
        for column, desc in reversed(self._orders):
            rows.sort(key=lambda r: (str(r.get(column)),), reverse=desc)
        if self._limit is not None:
            rows = rows[: self._limit]
        columns = getattr(self, "_columns", None)
        if columns:
            rows = [{c: row.get(c) for c in columns} for row in rows]
        return _FakeResult(rows)


class FakeSupabaseClient:
    """In-memory stand-in for ``supabase.AsyncClient`` (voice tables only)."""

    def __init__(self, *, latency: float = 0.0):
        self.fail_mode = False
        # Optional simulated network latency (seconds) applied to every call.
        # Used by benchmarks and by tests that assert batching shapes.
        self.latency = max(0.0, float(latency))
        # Every executed call, in order: {"kind", "name", "detail"}.
        self.calls: list[dict] = []
        self._id_counter = itertools.count(1)
        self._tables = {
            "voice_sessions": {},
            "voice_daily_stats": {},
            "voice_active_sessions": {},
            "voice_member_profiles": {},
            "voice_dm_reports": {},
            "voice_migration_state": {},
        }
        # (guild_id, user_id, started_at, ended_at) -> row
        self._session_identity = {}
        self.schema_version = 3
        self.schema_installed = True

    # -- instrumentation ---------------------------------------------------

    async def _record(self, kind: str, name: str, detail=None) -> None:
        self.calls.append({"kind": kind, "name": name, "detail": detail})
        if self.latency > 0:
            await asyncio.sleep(self.latency)

    def reset_calls(self) -> None:
        self.calls.clear()

    def call_counts(self) -> dict:
        """Counters keyed by ``rpc:<fn>`` / ``table:<name:mode>``."""
        counts: dict[str, int] = {}
        for call in self.calls:
            key = f"{call['kind']}:{call['name']}"
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items()))

    # -- client surface ---------------------------------------------------

    def rpc(self, fn, params=None):
        return _FakeRpcBuilder(self, fn, params)

    def table(self, name):
        if name not in self._tables:
            raise KeyError(f"unknown table {name}")
        return _FakeTableBuilder(self, name)

    # -- helpers for tests -------------------------------------------------

    @property
    def sessions(self):
        return self._tables["voice_sessions"]

    @property
    def daily(self):
        return self._tables["voice_daily_stats"]

    @property
    def active(self):
        return self._tables["voice_active_sessions"]

    @property
    def profiles(self):
        return self._tables["voice_member_profiles"]

    @property
    def dm_reports(self):
        return self._tables["voice_dm_reports"]

    @property
    def ledger(self):
        return self._tables["voice_migration_state"]

    # -- upsert/insert ------------------------------------------------------

    def _apply_upsert(self, table_name, builder):
        table = self._tables[table_name]
        inserted = []
        for row in builder._upsert_rows:
            key = self._pk_for(table_name, row)
            conflict_key = None
            if builder._mode == "upsert" and getattr(builder, "_on_conflict", ""):
                cols = tuple(builder._on_conflict.split(","))
                conflict_key = tuple(str(row.get(c)) for c in cols)
            existing = None
            if conflict_key is not None:
                for k, existing_row in table.items():
                    if tuple(str(existing_row.get(c)) for c in cols) == conflict_key:
                        existing = existing_row
                        break
            if existing is not None:
                if builder._mode == "upsert" and not builder._ignore_duplicates:
                    existing.update({k: v for k, v in row.items() if k != "id"})
                # ignore_duplicates=True -> ON CONFLICT DO NOTHING
                continue
            stored = dict(row)
            if "id" not in stored or stored.get("id") is None:
                if table_name == "voice_sessions":
                    stored["id"] = next(self._id_counter)
                    stored.setdefault("created_at", _now_iso())
            # Emulate the extra unique indexes Postgres enforces.
            if table_name == "voice_sessions":
                identity = (
                    stored["guild_id"],
                    stored["user_id"],
                    stored["started_at"],
                    stored["ended_at"],
                )
                if identity in self._session_identity:
                    raise FakeNetworkError(
                        "duplicate key value violates unique constraint "
                        "voice_sessions_identity_uidx"
                    )
                self._session_identity[identity] = stored
                if stored.get("source_id") is not None:
                    for other in self.sessions.values():
                        if other.get("source_id") == stored["source_id"]:
                            raise FakeNetworkError(
                                "duplicate key value violates unique constraint "
                                "voice_sessions_source_id_uidx"
                            )
            table[self._pk_for(table_name, stored)] = stored
            inserted.append(stored)
        return inserted

    @staticmethod
    def _pk_for(table_name, row):
        pks = {
            "voice_sessions": ("id",),
            "voice_daily_stats": ("guild_id", "user_id", "date"),
            "voice_active_sessions": ("guild_id", "user_id"),
            "voice_member_profiles": ("guild_id", "user_id"),
            "voice_dm_reports": ("date", "guild_id", "user_id"),
            "voice_migration_state": ("guild_id", "user_id", "date"),
        }
        return tuple(str(row.get(c)) for c in pks[table_name])

    # -- rpc implementations -------------------------------------------------

    async def _run_rpc(self, fn, p):
        if self.fail_mode:
            raise FakeNetworkError("simulated connection failure")
        handler = getattr(self, f"_rpc_{fn}", None)
        if handler is None:
            raise FakeNetworkError(
                f"Could not find the function public.{fn} in the schema cache"
            )
        return handler(p)

    def _rpc_voice_schema_version(self, p):
        if not self.schema_installed:
            raise FakeNetworkError(
                "Could not find the function public.voice_schema_version in the schema cache"
            )
        return self.schema_version

    def _rpc_voice_record_session(self, p):
        started = _parse_dt(p["p_started_at"])
        ended = _parse_dt(p["p_ended_at"])
        if started is None or ended is None or ended <= started:
            return 0
        duration = int((ended - started).total_seconds())
        if duration <= 0:
            return 0
        identity = (p["p_guild_id"], p["p_user_id"], started, ended)
        if identity in self._session_identity:
            return 0  # duplicate replay
        row = {
            "id": next(self._id_counter),
            "guild_id": p["p_guild_id"],
            "user_id": p["p_user_id"],
            "channel_id": p["p_channel_id"],
            "started_at": started.isoformat(),
            "ended_at": ended.isoformat(),
            "duration_seconds": duration,
            "source_id": None,
            "created_at": _now_iso(),
        }
        self.sessions[row["id"]] = row
        self._session_identity[identity] = row
        self._add_daily_splits(
            p["p_guild_id"], p["p_user_id"], started, ended
        )
        return duration

    def _add_daily_splits(self, guild_id, user_id, started, ended):
        for date_str, seconds in split_session_by_midnight(started, ended).items():
            if seconds <= 0:
                continue
            key = (str(guild_id), str(user_id), date_str)
            row = self.daily.get(key)
            if row is None:
                self.daily[key] = {
                    "guild_id": guild_id,
                    "user_id": user_id,
                    "date": date_str,
                    "duration_seconds": seconds,
                    "updated_at": _now_iso(),
                }
            else:
                row["duration_seconds"] += seconds
                row["updated_at"] = _now_iso()

    def _rpc_voice_close_active_session(self, p):
        key = (str(p["p_guild_id"]), str(p["p_user_id"]))
        row = self.active.get(key)
        if row is None:
            return None
        del self.active[key]
        if row["is_afk"]:
            return 0
        # Credit only the part a checkpoint has not already credited.
        started = row["started_at"]
        cursor = row.get("credited_until")
        if cursor is not None and _parse_dt(cursor) > _parse_dt(started):
            started = cursor
        return self._rpc_voice_record_session(
            {
                "p_guild_id": row["guild_id"],
                "p_user_id": row["user_id"],
                "p_channel_id": row["channel_id"],
                "p_started_at": started,
                "p_ended_at": p["p_ended_at"],
            }
        )

    def _rpc_voice_heartbeat_active(self, p):
        applied = 0
        for r in p["p_rows"]:
            key = (str(r["guild_id"]), str(r["user_id"]))
            existing = self.active.get(key)
            last_seen = _parse_dt(r["last_seen_at"])
            if existing is not None and _parse_dt(existing["last_seen_at"]) > last_seen:
                continue  # last-write-wins guard
            if existing is not None:
                # v3 contract: a heartbeat refreshes liveness only. started_at
                # and credited_until are the durable values (and any Dashboard
                # correction to them) and are never rewritten here.
                existing["channel_id"] = r["channel_id"]
                existing["last_seen_at"] = r["last_seen_at"]
                existing["is_afk"] = bool(r.get("is_afk", False))
                existing["updated_at"] = _now_iso()
                applied += 1
                continue
            self.active[key] = {
                "guild_id": r["guild_id"],
                "user_id": r["user_id"],
                "channel_id": r["channel_id"],
                "started_at": r["started_at"],
                "last_seen_at": r["last_seen_at"],
                "is_afk": bool(r.get("is_afk", False)),
                "credited_until": None,
                "updated_at": _now_iso(),
            }
            applied += 1
        return applied

    def _rpc_voice_heartbeat_active_v2(self, p):
        """Edit-safe heartbeat that echoes the durable values back."""
        self._rpc_voice_heartbeat_active(p)
        out = []
        for r in p["p_rows"]:
            key = (str(r["guild_id"]), str(r["user_id"]))
            row = self.active.get(key)
            if row is None:
                continue
            out.append(
                {
                    "o_guild_id": row["guild_id"],
                    "o_user_id": row["user_id"],
                    "o_started_at": row["started_at"],
                    "o_cursor": row.get("credited_until"),
                }
            )
        return out

    def _rpc_voice_checkpoint_active_sessions(self, p):
        """Mirrors the plpgsql function: locked, idempotent, monotonic."""
        out = []
        for r in p["p_rows"]:
            key = (str(r["guild_id"]), str(r["user_id"]))
            row = self.active.get(key)
            if row is None:
                continue  # already closed; nothing to checkpoint
            if row["is_afk"]:
                continue  # AFK time is never counted
            up_to = _parse_dt(r["up_to"])
            started = _parse_dt(row["started_at"])
            cursor_raw = row.get("credited_until")
            credit_from = _parse_dt(cursor_raw) if cursor_raw else started
            new_cursor = max(credit_from, up_to)  # monotonic: never rewinds
            credited = 0
            if new_cursor > credit_from:
                credited = self._rpc_voice_record_session(
                    {
                        "p_guild_id": row["guild_id"],
                        "p_user_id": row["user_id"],
                        "p_channel_id": row["channel_id"],
                        "p_started_at": credit_from.isoformat(),
                        "p_ended_at": new_cursor.isoformat(),
                    }
                )
            row["credited_until"] = new_cursor.isoformat()
            if _parse_dt(row["last_seen_at"]) < up_to:
                row["last_seen_at"] = up_to.isoformat()
            row["updated_at"] = _now_iso()
            out.append(
                {
                    "o_guild_id": row["guild_id"],
                    "o_user_id": row["user_id"],
                    "o_credited_seconds": credited,
                    "o_cursor": row["credited_until"],
                    "o_started_at": row["started_at"],
                }
            )
        return out

    def _rpc_voice_delete_active_if_older(self, p):
        key = (str(p["p_guild_id"]), str(p["p_user_id"]))
        row = self.active.get(key)
        if row is None:
            return 0
        threshold = _parse_dt(p.get("p_started_before"))
        if threshold is not None and _parse_dt(row["started_at"]) > threshold:
            return 0
        del self.active[key]
        return 1

    def _rpc_voice_upsert_profiles(self, p):
        applied = 0
        now = _parse_dt(_now_iso())
        for r in p["p_rows"]:
            key = (str(r["guild_id"]), str(r["user_id"]))
            avatar = r.get("avatar_url") or None
            incoming_ts = _parse_dt(r.get("updated_at")) or now
            existing = self.profiles.get(key)
            if existing is None:
                self.profiles[key] = {
                    "guild_id": r["guild_id"],
                    "user_id": r["user_id"],
                    "display_name": r["display_name"],
                    "username": r["username"],
                    "avatar_url": avatar,
                    "updated_at": incoming_ts.isoformat(),
                }
                applied += 1
                continue
            differs = (
                existing["display_name"] != r["display_name"]
                or existing["username"] != r["username"]
                or (avatar is not None and existing["avatar_url"] != avatar)
            )
            if differs and incoming_ts >= _parse_dt(existing["updated_at"]):
                existing["display_name"] = r["display_name"]
                existing["username"] = r["username"]
                if avatar is not None:
                    existing["avatar_url"] = avatar
                existing["updated_at"] = incoming_ts.isoformat()
                applied += 1
        return applied

    # -- reads ------------------------------------------------------------

    @staticmethod
    def _period_filter(period, day, target: date):
        if period == "today":
            return day == target
        if period == "yesterday":
            return day == target - timedelta(days=1)
        if period == "week":
            week_start = target - timedelta(days=target.weekday())
            week_end = min(week_start + timedelta(days=7), target + timedelta(days=1))
            return week_start <= day < week_end
        if period == "month":
            month_start = target.replace(day=1)
            month_end = (
                date(month_start.year + 1, 1, 1)
                if month_start.month == 12
                else date(month_start.year, month_start.month + 1, 1)
            )
            return month_start <= day < min(month_end, target + timedelta(days=1))
        if period == "all_time":
            return True
        return False

    def _rpc_voice_leaderboard(self, p):
        target = _parse_date(p.get("p_target_date")) or datetime.now(timezone.utc).date()
        totals: dict[int, int] = {}
        for row in self.daily.values():
            if row["guild_id"] != p["p_guild_id"]:
                continue
            if not self._period_filter(p["p_period"], _parse_date(row["date"]), target):
                continue
            totals[row["user_id"]] = totals.get(row["user_id"], 0) + row["duration_seconds"]
        ranked = sorted(
            ((u, s) for u, s in totals.items() if s > 0),
            key=lambda item: (-item[1], item[0]),
        )
        return [{"user_id": u, "total_seconds": s} for u, s in ranked]

    def _rpc_voice_user_breakdown(self, p):
        target = _parse_date(p.get("p_target_date")) or datetime.now(timezone.utc).date()
        totals = {"today": 0, "yesterday": 0, "week": 0, "month": 0, "all_time": 0}
        for row in self.daily.values():
            if row["guild_id"] != p["p_guild_id"] or row["user_id"] != p["p_user_id"]:
                continue
            day = _parse_date(row["date"])
            seconds = row["duration_seconds"]
            totals["all_time"] += seconds
            if day == target:
                totals["today"] += seconds
            if day == target - timedelta(days=1):
                totals["yesterday"] += seconds
            week_start = target - timedelta(days=target.weekday())
            if week_start <= day <= target:
                totals["week"] += seconds
            if day >= target.replace(day=1) and day <= target:
                totals["month"] += seconds
        return [
            {
                "today_seconds": totals["today"],
                "yesterday_seconds": totals["yesterday"],
                "week_seconds": totals["week"],
                "month_seconds": totals["month"],
                "all_time_seconds": totals["all_time"],
            }
        ]

    def _rpc_voice_daily_server_total(self, p):
        day = _parse_date(p["p_day"])
        return sum(
            row["duration_seconds"]
            for row in self.daily.values()
            if row["guild_id"] == p["p_guild_id"] and _parse_date(row["date"]) == day
        )

    def _rpc_voice_daily_records(self, p):
        day = _parse_date(p["p_day"])
        return [
            {
                "guild_id": row["guild_id"],
                "user_id": row["user_id"],
                "date": row["date"],
                "duration_seconds": row["duration_seconds"],
            }
            for row in self.daily.values()
            if _parse_date(row["date"]) == day and row["duration_seconds"] > 0
        ]

    # -- migration -----------------------------------------------------------

    def _rpc_voice_merge_daily_batch(self, p):
        results = []
        for r in p["p_rows"]:
            key = (str(r["guild_id"]), str(r["user_id"]), str(r["date"])[:10])
            daily_key = key
            ledger_row = self.ledger.get(key)
            if ledger_row is None:
                prev = 0
                self.ledger[key] = {
                    "guild_id": r["guild_id"],
                    "user_id": r["user_id"],
                    "date": str(r["date"])[:10],
                    "applied_seconds": r["duration_seconds"],
                    "applied_at": _now_iso(),
                }
            else:
                prev = ledger_row["applied_seconds"]
                ledger_row["applied_seconds"] = r["duration_seconds"]
            existing = self.daily.get(daily_key)
            if existing is None:
                self.daily[daily_key] = {
                    "guild_id": r["guild_id"],
                    "user_id": r["user_id"],
                    "date": str(r["date"])[:10],
                    "duration_seconds": r["duration_seconds"],
                    "updated_at": _now_iso(),
                }
            else:
                existing["duration_seconds"] += r["duration_seconds"] - prev
                existing["updated_at"] = _now_iso()
            results.append(
                {
                    "guild_id": r["guild_id"],
                    "user_id": r["user_id"],
                    "date": str(r["date"])[:10],
                    "resulting_seconds": self.daily[daily_key]["duration_seconds"],
                }
            )
        return results

    def _rpc_voice_fetch_sessions_batch(self, p):
        ids = {str(i) for i in p["p_source_ids"]}
        return [
            row
            for row in self.sessions.values()
            if str(row.get("source_id")) in ids
        ]

    def _rpc_voice_fetch_daily_batch(self, p):
        keys = {
            (str(r["guild_id"]), str(r["user_id"]), str(r["date"])[:10]) for r in p["p_rows"]
        }
        return [
            row
            for row in self.daily.values()
            if (str(row["guild_id"]), str(row["user_id"]), str(row["date"])) in keys
        ]

    def _rpc_voice_fetch_profiles_batch(self, p):
        keys = {(str(r["guild_id"]), str(r["user_id"])) for r in p["p_keys"]}
        return [
            row
            for row in self.profiles.values()
            if (str(row["guild_id"]), str(row["user_id"])) in keys
        ]

    def _rpc_voice_fetch_active_batch(self, p):
        keys = {(str(r["guild_id"]), str(r["user_id"])) for r in p["p_keys"]}
        return [
            row
            for row in self.active.values()
            if (str(row["guild_id"]), str(row["user_id"])) in keys
        ]

    def _rpc_voice_fetch_dm_report_batch(self, p):
        keys = {
            (str(r["date"])[:10], str(r["guild_id"]), str(r["user_id"]))
            for r in p["p_keys"]
        }
        return [
            row
            for row in self.dm_reports.values()
            if (str(row["date"])[:10], str(row["guild_id"]), str(row["user_id"])) in keys
        ]
