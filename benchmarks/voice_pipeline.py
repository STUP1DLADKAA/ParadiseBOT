"""Offline benchmark for Paradise's voice-activity pipeline.

Measures the cost of Discord voice-state handling end to end *inside the
process*: per-event latency, Supabase call counts, persistence lag, event-loop
stalls, reconcile/leaderboard cost, shutdown durability and peak memory.

The Supabase store is the real ``SupabaseVoiceDatabase`` talking to the
in-memory fake client from the test suite, with an optional simulated network
latency per call (default 40 ms, roughly a Delhi -> ap-south-1 round trip).
No network, no credentials and no production data are involved.

Usage::

    python3 benchmarks/voice_pipeline.py --label baseline
    python3 benchmarks/voice_pipeline.py --label after --latency 0.04

Each run writes ``benchmarks/results/<label>.json`` (small, committed as
evidence) and prints a human-readable summary. Compare runs with::

    python3 benchmarks/voice_pipeline.py --compare baseline after
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import tempfile
import time
import tracemalloc
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from functions.voice.database import VoiceDatabase  # noqa: E402
from functions.voice.supabase_database import SupabaseVoiceDatabase  # noqa: E402
from functions.voice.tracker import VoiceTracker  # noqa: E402
from voice_fakes import FakeSupabaseClient  # noqa: E402

TICK_SECONDS = 0.005
COALESCE_WINDOW = 5.0  # max seconds we wait for a deferred write to appear


# ---------------------------------------------------------------------------
# Minimal Discord doubles (only what the tracker touches)
# ---------------------------------------------------------------------------


class FakeChannel:
    def __init__(self, channel_id: int, members: list | None = None) -> None:
        self.id = channel_id
        self.members = list(members or [])
        self.guild = None

    def permissions_for(self, member):  # used by other cogs, not the tracker
        return types.SimpleNamespace(
            view_channel=True, connect=True, speak=True, move_members=True
        )


class FakeMember:
    def __init__(self, guild: "FakeGuild", user_id: int, name: str) -> None:
        self.id = user_id
        self.name = name
        self.display_name = name
        self.bot = False
        self.guild = guild
        self.display_avatar = types.SimpleNamespace(
            url=f"https://cdn.example/avatars/{user_id}.png"
        )
        self.voice = types.SimpleNamespace(
            self_mute=False, mute=False, self_deaf=False, deaf=False
        )


class FakeGuild:
    def __init__(self, guild_id: int = 12345) -> None:
        self.id = guild_id
        self.name = "Paradise"
        self.afk_channel = None
        self.stage_channels: list = []
        self.voice_channels = [FakeChannel(801), FakeChannel(802), FakeChannel(803)]
        for channel in self.voice_channels:
            channel.guild = self
        self.members: dict[int, FakeMember] = {}
        self.alert_channel = FakeChannel(999_999)

    def get_member(self, user_id: int):
        return self.members.get(user_id)

    def get_channel(self, channel_id: int):
        return self.alert_channel if channel_id == self.alert_channel.id else None

    def get_channel_or_thread(self, channel_id: int):
        return self.get_channel(channel_id)

    async def fetch_member(self, user_id: int):
        raise AssertionError("benchmark does not fetch members")


class FakeBot:
    def __init__(self, guild: FakeGuild) -> None:
        self.guilds = [guild]
        self.user = None
        self._guild = guild

    def get_guild(self, guild_id: int):
        return self._guild if guild_id == self._guild.id else None

    def get_channel(self, channel_id: int):
        return self._guild.get_channel(channel_id)

    async def fetch_channel(self, channel_id: int):
        return None

    def get_user(self, user_id: int):
        return None

    async def fetch_user(self, user_id: int):
        raise AssertionError("benchmark does not fetch users")


class FakeState:
    """The slice of ``discord.VoiceState`` the tracker reads."""

    def __init__(self, channel, *, self_mute=False, mute=False, self_deaf=False, deaf=False):
        self.channel = channel
        self.self_mute = self_mute
        self.mute = mute
        self.self_deaf = self_deaf
        self.deaf = deaf


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


async def _ticker(stop: asyncio.Event, gaps: list[float]) -> None:
    last = time.perf_counter()
    while not stop.is_set():
        await asyncio.sleep(TICK_SECONDS)
        now = time.perf_counter()
        gaps.append(now - last)
        last = now


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[index]


async def _wait_for_row_update(
    fake: FakeSupabaseClient, guild_id: int, user_id: int, previous: str | None
) -> float | None:
    """Seconds until the persisted heartbeat row reflects a newer write."""
    started = time.perf_counter()
    key = (str(guild_id), str(user_id))
    while time.perf_counter() - started < COALESCE_WINDOW:
        row = fake.active.get(key)
        if row is not None and row.get("last_seen_at") != previous:
            return time.perf_counter() - started
        await asyncio.sleep(0.01)
    return None


async def run_scenario(
    *,
    latency: float,
    users: int,
    toggles: int,
    coalesce: float | None,
) -> dict:
    temp_dir = tempfile.TemporaryDirectory()
    local = VoiceDatabase(str(Path(temp_dir.name) / "bench.db"))
    fake = FakeSupabaseClient(latency=latency)
    store = SupabaseVoiceDatabase(local, client=fake)
    store._bootstrapped = True

    guild = FakeGuild()
    bot = FakeBot(guild)
    clock = Clock(datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc))
    tracker_kwargs = dict(
        reconcile_interval=20.0,
        dm_reports_enabled=False,
        alert_channel_id=0,
        clock=clock,
    )
    if coalesce is not None and "coalesce_seconds" in VoiceTracker.__init__.__code__.co_varnames:
        tracker_kwargs["coalesce_seconds"] = coalesce
    tracker = VoiceTracker(bot, store, **tracker_kwargs)

    members = [FakeMember(guild, 10_000 + i, f"user{i}") for i in range(users)]
    for member in members:
        guild.members[member.id] = member

    # Only the coalesced writer runs: the reconciliation loop would add its
    # own heartbeat noise to the measured phases. Older trackers (the
    # baseline) have no flusher and write per event instead.
    if hasattr(tracker, "start_write_flusher"):
        tracker.start_write_flusher()

    join_channel, second_channel = guild.voice_channels[0], guild.voice_channels[1]
    timings: dict[str, list[float]] = {
        "join": [],
        "toggle": [],
        "move": [],
        "leave": [],
    }
    call_counts: dict[str, dict] = {}
    persistence_lag: list[float] = []
    loop_gaps: list[float] = []

    stop_ticker = asyncio.Event()
    ticker_task = asyncio.create_task(
        _ticker(stop_ticker, loop_gaps), name="benchmark-loop-ticker"
    )

    # --- joins -------------------------------------------------------------
    fake.reset_calls()
    for member in members:
        started = time.perf_counter()
        await tracker.handle_voice_state_update(
            member, FakeState(None), FakeState(join_channel)
        )
        timings["join"].append(time.perf_counter() - started)
        join_channel.members.append(member)
        clock.advance(1)
    call_counts["join"] = fake.call_counts()

    # --- mute/deafen style state churn (the bulk of real traffic) ----------
    fake.reset_calls()
    muted = False
    for _ in range(toggles):
        for member in members:
            muted = not muted
            state = FakeState(join_channel, self_mute=muted, mute=False)
            started = time.perf_counter()
            await tracker.handle_voice_state_update(
                member, FakeState(join_channel), state
            )
            timings["toggle"].append(time.perf_counter() - started)
            clock.advance(1)
    call_counts["toggle"] = fake.call_counts()

    # --- channel moves (a burst, like people hopping channels together) ----
    fake.reset_calls()
    previous_rows = {
        member.id: (fake.active.get((str(guild.id), str(member.id))) or {}).get(
            "last_seen_at"
        )
        for member in members
    }
    for member in members:
        join_channel.members.remove(member)
        second_channel.members.append(member)
        started = time.perf_counter()
        await tracker.handle_voice_state_update(
            member, FakeState(join_channel), FakeState(second_channel)
        )
        timings["move"].append(time.perf_counter() - started)
        clock.advance(1)
    # How long after the burst does every move become durable, and how many
    # writes did that cost?
    burst_finished = time.perf_counter()
    deadline = burst_finished + COALESCE_WINDOW + 2.0
    while time.perf_counter() < deadline:
        if all(
            (fake.active.get((str(guild.id), str(uid))) or {}).get("last_seen_at")
            != previous
            for uid, previous in previous_rows.items()
        ):
            break
        await asyncio.sleep(0.01)
    persistence_lag.append(time.perf_counter() - burst_finished)
    call_counts["move"] = fake.call_counts()

    # --- coalesced flush (what the cadence writer does each window) --------
    fake.reset_calls()
    flush_started = time.perf_counter()
    flushed_rows = (
        await tracker.flush_pending() if hasattr(tracker, "flush_pending") else 0
    )
    flush_seconds = time.perf_counter() - flush_started
    call_counts["flush"] = fake.call_counts()

    # --- reconcile (the periodic heartbeat) --------------------------------
    fake.reset_calls()
    reconcile_started = time.perf_counter()
    await tracker.reconcile_guilds()
    reconcile_seconds = time.perf_counter() - reconcile_started
    call_counts["reconcile"] = fake.call_counts()

    # --- leaderboard read --------------------------------------------------
    clock.advance(600)
    fake.reset_calls()
    board_started = time.perf_counter()
    board = await tracker.get_live_leaderboard(guild, tracker_period())
    board_seconds = time.perf_counter() - board_started
    call_counts["leaderboard"] = fake.call_counts()

    # --- leaves (session close) -------------------------------------------
    fake.reset_calls()
    for member in members:
        second_channel.members.remove(member)
        started = time.perf_counter()
        await tracker.handle_voice_state_update(
            member, FakeState(second_channel), FakeState(None)
        )
        timings["leave"].append(time.perf_counter() - started)
        clock.advance(1)
    call_counts["leave"] = fake.call_counts()

    stop_ticker.set()
    await ticker_task

    summary = {
        "latency_simulated_ms": round(latency * 1000, 2),
        "users": users,
        "toggles_per_user": toggles,
        "coalesce_seconds": tracker.__dict__.get("coalesce_seconds"),
        "events": {name: len(values) for name, values in timings.items()},
        "event_latency_ms": {
            name: {
                "mean": round(statistics.fmean(values) * 1000, 2) if values else 0.0,
                "p95": round(_percentile(values, 0.95) * 1000, 2),
                "max": round(max(values) * 1000, 2) if values else 0.0,
            }
            for name, values in timings.items()
        },
        "supabase_calls": call_counts,
        "supabase_calls_total": sum(
            sum(counts.values()) for counts in call_counts.values()
        ),
        "supabase_calls_per_event": {
            name: {
                "calls": sum(counts.values()),
                "events": len(timings[name]),
                "ratio": round(
                    sum(counts.values()) / max(1, len(timings[name])), 3
                ),
            }
            for name, counts in call_counts.items()
            if name in timings
        },
        "persistence_lag_ms": {
            # One sample: a burst of move events -> all rows durable.
            "mean": round(statistics.fmean(persistence_lag) * 1000, 2)
            if persistence_lag
            else None,
            "max": round(max(persistence_lag) * 1000, 2) if persistence_lag else None,
            "samples": len(persistence_lag),
            "rows_in_burst": len(previous_rows),
        },
        "loop_max_gap_ms": round(max(loop_gaps) * 1000, 2) if loop_gaps else None,
        "reconcile_ms": round(reconcile_seconds * 1000, 2),
        "flush_ms": round(flush_seconds * 1000, 2),
        "flushed_rows": flushed_rows,
        "leaderboard_ms": round(board_seconds * 1000, 2),
        "leaderboard_rows": len(board),
        "total_event_seconds": round(
            sum(sum(values) for values in timings.values()), 3
        ),
        "daily_seconds_total": int(
            sum(row["duration_seconds"] for row in fake.daily.values())
        ),
    }

    summary["active_row_writes"] = sum(
        call_counts[name].get("rpc:voice_heartbeat_active", 0)
        for name in ("join", "toggle", "move")
    )
    await tracker.stop()
    local.close() if hasattr(local, "close") else None
    temp_dir.cleanup()
    return summary


def tracker_period():
    from functions.voice.models import VoicePeriod

    return VoicePeriod.TODAY


async def run_concurrent_burst(*, latency: float, users: int) -> dict:
    """Join events for many users at once — shows per-key vs global locking."""
    temp_dir = tempfile.TemporaryDirectory()
    local = VoiceDatabase(str(Path(temp_dir.name) / "bench.db"))
    fake = FakeSupabaseClient(latency=latency)
    store = SupabaseVoiceDatabase(local, client=fake)
    store._bootstrapped = True
    guild = FakeGuild()
    bot = FakeBot(guild)
    clock = Clock(datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc))
    tracker = VoiceTracker(bot, store, alert_channel_id=0, clock=clock)
    members = [FakeMember(guild, 20_000 + i, f"burst{i}") for i in range(users)]
    for member in members:
        guild.members[member.id] = member
    channel = guild.voice_channels[0]

    fake.reset_calls()
    started = time.perf_counter()
    await asyncio.gather(
        *(
            tracker.handle_voice_state_update(
                member, FakeState(None), FakeState(channel)
            )
            for member in members
        )
    )
    elapsed = time.perf_counter() - started
    calls = fake.call_counts()
    summary = {
        "users": users,
        "latency_simulated_ms": round(latency * 1000, 2),
        "total_ms": round(elapsed * 1000, 2),
        "per_event_ms": round(elapsed * 1000 / users, 2),
        "calls": calls,
        "calls_total": sum(calls.values()),
        "active_rows": len(fake.active),
    }
    await tracker.stop()
    local.close() if hasattr(local, "close") else None
    temp_dir.cleanup()
    return summary


async def run_shutdown_flush(*, latency: float) -> dict:
    """Pending in-memory updates must survive a clean shutdown."""
    temp_dir = tempfile.TemporaryDirectory()
    local = VoiceDatabase(str(Path(temp_dir.name) / "bench.db"))
    fake = FakeSupabaseClient(latency=latency)
    store = SupabaseVoiceDatabase(local, client=fake)
    store._bootstrapped = True
    guild = FakeGuild()
    bot = FakeBot(guild)
    clock = Clock(datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc))
    tracker = VoiceTracker(bot, store, alert_channel_id=0, clock=clock)
    member = FakeMember(guild, 31_000, "shutdown")
    guild.members[member.id] = member
    channel = guild.voice_channels[0]

    await tracker.handle_voice_state_update(member, FakeState(None), FakeState(channel))
    channel.members.append(member)
    clock.advance(120)
    # A state change whose heartbeat may still be pending in memory.
    await tracker.handle_voice_state_update(
        member, FakeState(channel), FakeState(channel, self_mute=True)
    )
    clock.advance(30)
    started = time.perf_counter()
    await tracker.stop()
    elapsed = time.perf_counter() - started
    daily = sum(row["duration_seconds"] for row in fake.daily.values())
    summary = {
        "latency_simulated_ms": round(latency * 1000, 2),
        "stop_ms": round(elapsed * 1000, 2),
        "active_rows_after_stop": len(fake.active),
        "sessions_recorded": len(fake.sessions),
        "recorded_seconds": int(daily),
        "recorded_expected": 150,
    }
    local.close() if hasattr(local, "close") else None
    temp_dir.cleanup()
    return summary


async def main_async(args) -> dict:
    tracemalloc.start()
    coalesce = args.coalesce  # None = tracker default, 0 = legacy per-event
    scenario = await run_scenario(
        latency=args.latency,
        users=args.users,
        toggles=args.toggles,
        coalesce=coalesce,
    )
    burst = await run_concurrent_burst(latency=args.latency, users=args.users)
    shutdown = await run_shutdown_flush(latency=args.latency)
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "label": args.label,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "scenario": scenario,
        "concurrent_burst": burst,
        "shutdown": shutdown,
        "peak_traced_memory_kib": round(peak / 1024, 1),
    }


def _print_summary(result: dict) -> None:
    s, b, d = result["scenario"], result["concurrent_burst"], result["shutdown"]
    print(f"\n=== {result['label']} ===")
    print(
        f"simulated RTT {s['latency_simulated_ms']} ms · "
        f"{s['users']} users × {s['toggles_per_user']} mute toggles"
    )
    for name, stats in s["event_latency_ms"].items():
        print(
            f"  {name:<7} n={s['events'][name]:>3}  mean={stats['mean']:>8.2f} ms"
            f"  p95={stats['p95']:>8.2f} ms  max={stats['max']:>8.2f} ms"
        )
    print(f"  Supabase calls total: {s['supabase_calls_total']}")
    for name, ratio in s["supabase_calls_per_event"].items():
        print(
            f"    {name:<8} {ratio['calls']:>4} calls / {ratio['events']:>4} events"
            f" = {ratio['ratio']:.2f} per event"
        )
    lag = s["persistence_lag_ms"]
    print(
        f"  persistence delay ({lag['rows_in_burst']}-row move burst): "
        f"{lag['mean']} ms"
    )
    print(
        f"  loop max gap: {s['loop_max_gap_ms']} ms · flush {s['flush_ms']} ms"
        f" ({s['flushed_rows']} rows) · reconcile {s['reconcile_ms']} ms"
        f" · leaderboard {s['leaderboard_ms']} ms ({s['leaderboard_rows']} rows)"
    )
    print(
        f"  burst: {b['users']} concurrent joins {b['total_ms']} ms "
        f"({b['per_event_ms']} ms/event), {b['calls_total']} calls"
    )
    print(
        f"  shutdown: {d['stop_ms']} ms, recorded {d['recorded_seconds']}s "
        f"(expected {d['recorded_expected']}s), active rows left {d['active_rows_after_stop']}"
    )
    print(f"  peak traced memory: {result['peak_traced_memory_kib']} KiB")


def _compare(labels: list[str]) -> None:
    results = []
    for label in labels:
        path = REPO / "benchmarks" / "results" / f"{label}.json"
        if not path.exists():
            raise SystemExit(f"missing result file: {path}")
        results.append(json.loads(path.read_text()))
    baseline, after = results[0], results[-1]
    print(f"\nbaseline={baseline['label']}  after={after['label']}")
    rows = [
        (
            "mute-toggle mean latency ms",
            baseline["scenario"]["event_latency_ms"]["toggle"]["mean"],
            after["scenario"]["event_latency_ms"]["toggle"]["mean"],
        ),
        (
            "mute-toggle p95 latency ms",
            baseline["scenario"]["event_latency_ms"]["toggle"]["p95"],
            after["scenario"]["event_latency_ms"]["toggle"]["p95"],
        ),
        (
            "join mean latency ms",
            baseline["scenario"]["event_latency_ms"]["join"]["mean"],
            after["scenario"]["event_latency_ms"]["join"]["mean"],
        ),
        (
            "Supabase calls (whole workload)",
            baseline["scenario"]["supabase_calls_total"],
            after["scenario"]["supabase_calls_total"],
        ),
        (
            "Supabase calls per toggle event",
            baseline["scenario"]["supabase_calls_per_event"]["toggle"]["ratio"],
            after["scenario"]["supabase_calls_per_event"]["toggle"]["ratio"],
        ),
        (
            "reconcile ms",
            baseline["scenario"]["reconcile_ms"],
            after["scenario"]["reconcile_ms"],
        ),
        (
            "leaderboard ms",
            baseline["scenario"]["leaderboard_ms"],
            after["scenario"]["leaderboard_ms"],
        ),
        (
            "burst total ms",
            baseline["concurrent_burst"]["total_ms"],
            after["concurrent_burst"]["total_ms"],
        ),
        (
            "peak traced KiB",
            baseline["peak_traced_memory_kib"],
            after["peak_traced_memory_kib"],
        ),
        (
            "persistence delay ms (trade-off)",
            baseline["scenario"]["persistence_lag_ms"]["mean"],
            after["scenario"]["persistence_lag_ms"]["mean"],
        ),
    ]
    print(f"\n{'metric':<40}{'baseline':>12}{'after':>12}{'change':>12}")
    for name, before, current in rows:
        change = "n/a"
        if isinstance(before, (int, float)) and before:
            change = f"{(current - before) / before * 100:+.1f}%"
        print(f"{name:<40}{before:>12}{current:>12}{change:>12}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", default="baseline")
    parser.add_argument("--latency", type=float, default=0.04, help="simulated RTT seconds")
    parser.add_argument("--users", type=int, default=12)
    parser.add_argument("--toggles", type=int, default=4)
    parser.add_argument(
        "--coalesce",
        type=float,
        default=None,
        help=(
            "coalescing window passed to the tracker when supported; "
            "--coalesce 0 forces legacy per-event writes, omitting the flag "
            "keeps the tracker default"
        ),
    )
    parser.add_argument("--compare", nargs="+", default=None)
    parser.add_argument("--json", action="store_true", help="print the raw JSON result")
    args = parser.parse_args()

    if args.compare:
        _compare(args.compare)
        return 0

    result = asyncio.run(main_async(args))
    out_dir = REPO / "benchmarks" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{args.label}.json").write_text(json.dumps(result, indent=2) + "\n")
    if args.json:
        print(json.dumps(result, indent=2))
    _print_summary(result)
    print(f"\nwrote {out_dir / (args.label + '.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
