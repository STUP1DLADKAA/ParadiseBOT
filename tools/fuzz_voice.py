"""Randomised invariant fuzzer for the voice accounting engine.

Holds one property constant: a user who stays connected in a non-AFK voice
channel, never deafens and never leaves, must see their displayed daily total
monotonically increase. Everything else (gateway cache blips, restarts,
Supabase outages, other users, channel moves) is randomised around them.

Any decrease is printed with the exact seed and event log that produced it.

Usage:  python3 tools/fuzz_voice.py [iterations]
"""

from __future__ import annotations

import asyncio
import random
import sys
import tempfile
import types
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from functions.voice.database import VoiceDatabase  # noqa: E402
from functions.voice.store import VoiceStoreUnavailable  # noqa: E402
from functions.voice.supabase_database import SupabaseVoiceDatabase  # noqa: E402
from functions.voice.time_windows import IST  # noqa: E402
from functions.voice.tracker import VoiceTracker  # noqa: E402
from voice_fakes import FakeSupabaseClient  # noqa: E402

GUILD_ID = 12345
VICTIM = 10001


class Ch:
    def __init__(self, cid):
        self.id = cid
        self.members = []
        self.guild = None


class Mem:
    def __init__(self, g, uid, name="u"):
        self.id = uid
        self.name = name
        self.display_name = name
        self.bot = False
        self.guild = g
        self.display_avatar = types.SimpleNamespace(url=None)
        self.voice = types.SimpleNamespace(
            self_mute=False, mute=False, self_deaf=False, deaf=False
        )


class G:
    def __init__(self, gid):
        self.id = gid
        self.name = "Paradise"
        self.afk_channel = None
        self.stage_channels = []
        self.voice_channels = [Ch(gid * 10 + 1), Ch(gid * 10 + 2)]
        for c in self.voice_channels:
            c.guild = self
        self.members = {}
        self.alert = Ch(999_999)

    def get_member(self, uid):
        return self.members.get(uid)

    def get_channel(self, cid):
        return self.alert if cid == self.alert.id else None

    def get_channel_or_thread(self, cid):
        return self.get_channel(cid)


class Bot:
    def __init__(self, gs):
        self.guilds = list(gs)
        self._by_id = {g.id: g for g in gs}

    def get_guild(self, gid):
        return self._by_id.get(gid)

    def get_channel(self, cid):
        for g in self.guilds:
            c = g.get_channel(cid)
            if c:
                return c
        return None

    def get_user(self, uid):
        return None

    async def fetch_user(self, uid):
        raise RuntimeError("no network")


class Clock:
    def __init__(self, s):
        self.now = s

    def __call__(self):
        return self.now

    def advance(self, s):
        self.now += timedelta(seconds=s)


def st(channel, deaf=False):
    return types.SimpleNamespace(
        channel=channel, self_mute=False, mute=False, self_deaf=deaf, deaf=False
    )


def hr(s):
    return f"{s // 3600}h{(s % 3600) // 60:02d}m{(s % 60):02d}s"


class World:
    def __init__(self, tmp, client, clock, guilds):
        self.tmp = tmp
        self.client = client
        self.clock = clock
        self.guilds = guilds
        self.local = VoiceDatabase(f"{tmp}/local.db")
        self.db = SupabaseVoiceDatabase(self.local, client=client)
        self.db._bootstrapped = True
        # Stand in for a verified schema v3 database.
        self.db._schema_version = 3
        self.db._schema_ok = True
        self.bot = Bot(guilds)
        self.tracker = VoiceTracker(
            self.bot, self.db, reconcile_interval=20.0, dm_reports_enabled=False,
            alert_channel_id=0, clock=clock, coalesce_seconds=2.0,
        )

    async def shown(self, gid=GUILD_ID, uid=VICTIM):
        """None while the database is unreachable (the leaderboard cannot be read)."""
        try:
            board = await self.tracker.get_live_leaderboard(
                self.bot.get_guild(gid), hydrate=False
            )
        except VoiceStoreUnavailable:
            return None
        return next((m.total_seconds for m in board if m.user_id == uid), 0)


async def run_once(seed, ticks=220, verbose=False):
    """One fuzz run. Returns (ok, detail) for the monotonic invariant."""
    rnd = random.Random(seed)
    tmp = tempfile.mkdtemp()
    client = FakeSupabaseClient()
    clock = Clock(datetime(2026, 3, 10, 6, 0, tzinfo=IST))
    guilds = [G(GUILD_ID + i) for i in range(rnd.choice([1, 1, 2]))]
    world = World(tmp, client, clock, guilds)
    log = []

    # The victim joins and never leaves, never deafens.
    g0 = guilds[0]
    victim = Mem(g0, VICTIM, "victim")
    g0.members[VICTIM] = victim
    ch = g0.voice_channels[0]
    ch.members.append(victim)
    await world.tracker.handle_voice_state_update(victim, st(None), st(ch))

    others = []
    for i in range(rnd.randint(0, 3)):
        g = rnd.choice(guilds)
        m = Mem(g, 20000 + i, f"other{i}")
        g.members[m.id] = m
        others.append((g, m))

    prev = await world.shown() or 0
    for tick in range(ticks):
        clock.advance(20)
        ev = None

        # random noise around the victim (never touching the victim itself)
        r = rnd.random()
        if r < 0.10 and others:
            g, m = rnd.choice(others)
            c = rnd.choice(g.voice_channels)
            if m in c.members:
                c.members.remove(m)
                await world.tracker.handle_voice_state_update(m, st(c), st(None))
                ev = f"other{m.id} leaves"
            else:
                c.members.append(m)
                await world.tracker.handle_voice_state_update(m, st(None), st(c))
                ev = f"other{m.id} joins"
        elif r < 0.16 and others:
            g, m = rnd.choice(others)
            src = next((c for c in g.voice_channels if m in c.members), None)
            dst = rnd.choice(g.voice_channels)
            if src and src is not dst:
                src.members.remove(m)
                dst.members.append(m)
                await world.tracker.handle_voice_state_update(m, st(src), st(dst))
                ev = f"other{m.id} moves"
        elif r < 0.22:
            # transient gateway cache blip: the victim vanishes for one tick
            if victim in ch.members:
                ch.members.remove(victim)
                ev = "gateway blip (victim absent from cache)"
            else:
                ch.members.append(victim)
                ev = "gateway blip resolved"
        elif r < 0.27:
            client.fail_mode = not client.fail_mode
            ev = "supabase down" if client.fail_mode else "supabase up"
        elif r < 0.30:
            # hard process restart: brand-new tracker over the same durable data
            world = World(tmp, client, clock, guilds)
            ev = "PROCESS RESTART"
        elif r < 0.33:
            # a second overlapping instance reconciles the same tables
            ghost = World(tmp, client, clock, guilds)
            await ghost.tracker.reconcile_guilds()
            ev = "second instance reconcile"

        # Mirror the production maintenance loop: the store drains its
        # durable outbox every flush_interval while the database is reachable.
        if not client.fail_mode:
            try:
                await world.db.flush_outbox()
            except Exception:
                pass

        await world.tracker.reconcile_guilds()
        await world.tracker.rollover_midnight_sessions(clock.now)

        shown = await world.shown()
        if shown is None:
            continue  # database unreachable: nothing to compare this tick
        if shown < prev:
            log.append(
                f"    tick={tick} wall={clock.now.astimezone(IST):%H:%M:%S} "
                f"shown {hr(prev)} -> {hr(shown)}  event={ev}"
            )
            if verbose:
                print(f"  DECREASE seed={seed}: {hr(prev)} -> {hr(shown)} at {ev}")
            return False, log, prev, shown
        prev = shown
    client.fail_mode = False
    return True, log, prev, prev


async def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 25
    fails = 0
    print(f"Running {n} fuzz iterations (220 ticks x 20s = ~73 min each)...")
    for seed in range(n):
        ok, log, prev, shown = await run_once(seed)
        if not ok:
            fails += 1
            print(f"\nSEED {seed}: monotonicity VIOLATED {hr(prev)} -> {hr(shown)}")
            for line in log:
                print(line)
        else:
            print(f"  seed {seed}: ok, final shown={hr(prev)}")
    print(f"\n{fails}/{n} iterations violated the invariant")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
