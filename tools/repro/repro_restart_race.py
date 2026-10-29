"""Decisive proof of the restart/bootstrap race that loses in-progress voice time.

Production startup order (commands/vc.py::cog_load):

    tracker.start_background_tasks()   # reconcile loop wakes after 5 s
    store.start()                      # schema check -> outbox drain -> _bootstrapped

So the FIRST reconcile_guilds() almost always runs while the Supabase store is
still bootstrapping. In that state:

    store_online = bool(self.db.is_bootstrapped)   ->  False
    db.get_active_sessions()                       ->  LOCAL MIRROR ROWS ONLY

and heartbeat_active_sessions() DELETES the local mirror after every successful
remote write, so in steady state the mirror is empty. Recovery therefore finds
nothing -- but it has ALREADY set self._recovered = True.

This script measures exactly that.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import types
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from functions.voice.database import VoiceDatabase  # noqa: E402
from functions.voice.supabase_database import SupabaseVoiceDatabase  # noqa: E402
from functions.voice.time_windows import IST  # noqa: E402
from functions.voice.tracker import VoiceTracker  # noqa: E402
from voice_fakes import FakeSupabaseClient  # noqa: E402

GUILD_ID, USER_ID, CH = 12345, 10001, 801


class Ch:
    def __init__(self, cid):
        self.id = cid
        self.members = []
        self.guild = None


class Mem:
    def __init__(self, g, uid):
        self.id = uid
        self.name = "Alex"
        self.display_name = "Alex"
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
        self.voice_channels = [Ch(CH)]
        self.voice_channels[0].guild = self
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


def st(c):
    return types.SimpleNamespace(
        channel=c, self_mute=False, mute=False, self_deaf=False, deaf=False
    )


def hr(s):
    return f"{s // 3600}h{(s % 3600) // 60:02d}m{(s % 60):02d}s"


def world(tmp, client, clock, guilds, bootstrapped):
    local = VoiceDatabase(f"{tmp}/local.db")
    db = SupabaseVoiceDatabase(local, client=client)
    # Stand in for a verified schema v3 database.
    db._schema_version = 3
    db._schema_ok = True
    db._bootstrapped = bootstrapped
    bot = Bot(guilds)
    tr = VoiceTracker(
        bot, db, reconcile_interval=20.0, dm_reports_enabled=False,
        alert_channel_id=0, clock=clock, coalesce_seconds=2.0,
    )
    return local, db, bot, tr


async def shown(tr, bot, gid=GUILD_ID, uid=USER_ID):
    board = await tr.get_live_leaderboard(bot.get_guild(gid), hydrate=False)
    return next((m.total_seconds for m in board if m.user_id == uid), 0)


async def main():
    tmp = tempfile.mkdtemp()
    client = FakeSupabaseClient()
    clock = Clock(datetime(2026, 3, 10, 7, 0, tzinfo=IST))
    guild = G(GUILD_ID)
    m = Mem(guild, USER_ID)
    guild.members[USER_ID] = m
    guild.voice_channels[0].members.append(m)

    # ---- Instance 1: healthy run, user connected for 6 hours ----
    _, db1, bot1, tr1 = world(tmp, client, clock, [guild], bootstrapped=True)
    await tr1.handle_voice_state_update(m, st(None), st(guild.voice_channels[0]))
    for _ in range(1080):
        clock.advance(20)
        await tr1.reconcile_guilds()
    before = await shown(tr1, bot1)
    row = client.active[(str(GUILD_ID), str(USER_ID))]
    print("=" * 78)
    print("Instance 1 (healthy, 6h connected)")
    print(f"  displayed daily total      : {hr(before)}")
    print(f"  voice_daily_stats committed: {hr(client.daily.get((str(GUILD_ID), str(USER_ID), '2026-03-10'), {}).get('duration_seconds', 0))}")
    print(f"  voice_active_sessions row  : started_at={row['started_at']}")
    print(f"  local SQLite mirror rows   : {len(db1._local.get_active_sessions())}")

    # ---- Restart. The reconcile loop wakes 5 s later, BEFORE the store
    #      finishes bootstrap -> is_bootstrapped is False. ----
    _, db2, bot2, tr2 = world(tmp, client, clock, [guild], bootstrapped=False)
    print("\nInstance 2 (restart, store still bootstrapping -> is_bootstrapped=False)")
    await tr2.reconcile_guilds()
    mid = await shown(tr2, bot2)
    row2 = client.active[(str(GUILD_ID), str(USER_ID))]
    print(f"  _recovered={tr2._recovered}  _recovered_remote={tr2._recovered_remote}")
    print(f"  in-memory started_at       : "
          f"{tr2.active_sessions[(GUILD_ID, USER_ID)].started_at.isoformat()}")
    print(f"  displayed daily total      : {hr(mid)}   <-- WAS {hr(before)}")
    print(f"  voice_active_sessions row  : started_at={row2['started_at']}  <-- WAS {row['started_at']}")

    # ---- The store now finishes bootstrapping; the deferred remote recovery
    #      runs. Can it still repair the session? ----
    db2._bootstrapped = True
    for _ in range(3):
        clock.advance(20)
        await tr2.reconcile_guilds()
    after = await shown(tr2, bot2)
    row3 = client.active[(str(GUILD_ID), str(USER_ID))]
    print("\nAfter store bootstrap + deferred remote recovery")
    print(f"  _recovered_remote={tr2._recovered_remote}")
    print(f"  displayed daily total      : {hr(after)}")
    print(f"  voice_active_sessions row  : started_at={row3['started_at']}")

    # The clock advanced by one minute between the two reads, so the fixed
    # behaviour is "the total never went down".
    lost = max(0, before - after)
    print("\n" + "=" * 78)
    print(f"TIME LOST BY THE RESTART RACE: {hr(lost)}")
    print(f"  before={hr(before)}  after={hr(after)}  (after must be >= before)")
    print("=" * 78)
    return 0 if lost == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
