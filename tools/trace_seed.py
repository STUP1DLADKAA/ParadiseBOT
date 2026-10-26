"""Trace the per-tick accounting for one fuzz seed to locate a double count."""
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

sys.path.insert(0, str(REPO / "tools"))
import fuzz_voice as fz  # noqa: E402


def hr(s):
    return f"{s // 3600}h{(s % 3600) // 60:02d}m{(s % 60):02d}s"


async def main():
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 38
    ticks = int(sys.argv[2]) if len(sys.argv) > 2 else 12
    rnd = random.Random(seed)
    tmp = tempfile.mkdtemp()
    client = FakeSupabaseClient()
    clock = fz.Clock(datetime(2026, 3, 10, 6, 0, tzinfo=IST))
    guilds = [fz.G(fz.GUILD_ID + i) for i in range(rnd.choice([1, 1, 2]))]
    world = fz.World(tmp, client, clock, guilds)
    g0 = guilds[0]
    victim = fz.Mem(g0, fz.VICTIM, "victim")
    g0.members[fz.VICTIM] = victim
    ch = g0.voice_channels[0]
    ch.members.append(victim)
    await world.tracker.handle_voice_state_update(victim, fz.st(None), fz.st(ch))

    others = []
    for i in range(rnd.randint(0, 3)):
        g = rnd.choice(guilds)
        m = fz.Mem(g, 20000 + i, f"other{i}")
        g.members[m.id] = m
        others.append((g, m))

    key = (str(fz.GUILD_ID), str(fz.VICTIM))
    print(f"seed={seed}  started_at={victim and clock.now.isoformat()}")
    for tick in range(ticks):
        clock.advance(20)
        ev = None
        r = rnd.random()
        if r < 0.10 and others:
            g, m = rnd.choice(others)
            c = rnd.choice(g.voice_channels)
            if m in c.members:
                c.members.remove(m)
                await world.tracker.handle_voice_state_update(m, fz.st(c), fz.st(None))
                ev = f"other{m.id} leaves"
            else:
                c.members.append(m)
                await world.tracker.handle_voice_state_update(m, fz.st(None), fz.st(c))
                ev = f"other{m.id} joins"
        elif r < 0.16 and others:
            g, m = rnd.choice(others)
            src = next((c for c in g.voice_channels if m in c.members), None)
            dst = rnd.choice(g.voice_channels)
            if src and src is not dst:
                src.members.remove(m)
                dst.members.append(m)
                await world.tracker.handle_voice_state_update(m, fz.st(src), fz.st(dst))
                ev = f"other{m.id} moves"
        elif r < 0.22:
            if victim in ch.members:
                ch.members.remove(victim)
                ev = "blip out"
            else:
                ch.members.append(victim)
                ev = "blip in"
        elif r < 0.27:
            client.fail_mode = not client.fail_mode
            ev = "supabase down" if client.fail_mode else "supabase up"
        elif r < 0.30:
            world = fz.World(tmp, client, clock, guilds)
            ev = "RESTART"
        elif r < 0.33:
            ghost = fz.World(tmp, client, clock, guilds)
            await ghost.tracker.reconcile_guilds()
            ev = "second instance"

        if not client.fail_mode:
            try:
                await world.db.flush_outbox()
            except Exception:
                pass
        await world.tracker.reconcile_guilds()
        await world.tracker.rollover_midnight_sessions(clock.now)

        sess = world.tracker.active_sessions.get((fz.GUILD_ID, fz.VICTIM))
        committed = client.daily.get(
            (str(fz.GUILD_ID), str(fz.VICTIM), "2026-03-10"), {}
        ).get("duration_seconds", 0)
        row = client.active.get(key)
        try:
            shown = await world.shown()
        except VoiceStoreUnavailable:
            shown = None
        cf = sess.credit_from if sess else None
        live = (
            int((clock.now - cf).total_seconds()) if cf else None
        )
        print(
            f"t{tick:02d} {clock.now.astimezone(IST):%H:%M:%S} ev={str(ev):22s} "
            f"shown={str(shown):>6} committed={hr(committed):>10} live={str(live):>6} "
            f"mem_credit_from={cf.isoformat() if cf else None} "
            f"db_cursor={row.get('credited_until') if row else None}"
        )


asyncio.run(main())
