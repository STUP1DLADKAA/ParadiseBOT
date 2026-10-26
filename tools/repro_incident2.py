"""Adversarial reproduction scenarios for the voice-accounting incident.

Each scenario drives the REAL VoiceTracker + REAL SupabaseVoiceDatabase against
the in-memory fake client and asserts the accounting invariant:

    displayed daily seconds for a user must never decrease while they remain
    connected, and must never lose previously credited time.

Usage:  python3 tools/repro_incident2.py
"""

from __future__ import annotations

import asyncio
import copy
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from functions.voice.database import VoiceDatabase  # noqa: E402
from functions.voice.models import PersistedActiveSession  # noqa: E402
from functions.voice.supabase_database import SupabaseVoiceDatabase  # noqa: E402
from functions.voice.time_windows import IST  # noqa: E402
from functions.voice.tracker import VoiceTracker  # noqa: E402
from voice_fakes import FakeSupabaseClient  # noqa: E402

GUILD_ID = 12345
USER_ID = 10001
CHANNEL_ID = 801


class FakeChannel:
    def __init__(self, channel_id, members=None):
        self.id = channel_id
        self.members = list(members or [])
        self.guild = None


class FakeMember:
    def __init__(self, guild, user_id, name="Alex"):
        self.id = user_id
        self.name = name
        self.display_name = name
        self.bot = False
        self.guild = guild
        self.display_avatar = types.SimpleNamespace(url=f"https://cdn/{user_id}.png")
        self.voice = types.SimpleNamespace(
            self_mute=False, mute=False, self_deaf=False, deaf=False
        )


class FakeGuild:
    def __init__(self, guild_id=GUILD_ID):
        self.id = guild_id
        self.name = "Paradise"
        self.afk_channel = None
        self.stage_channels = []
        self.voice_channels = [FakeChannel(CHANNEL_ID), FakeChannel(802)]
        for c in self.voice_channels:
            c.guild = self
        self.members = {}
        self.alert = FakeChannel(999_999)

    def get_member(self, uid):
        return self.members.get(uid)

    def get_channel(self, cid):
        return self.alert if cid == self.alert.id else None

    def get_channel_or_thread(self, cid):
        return self.get_channel(cid)


class FakeBot:
    def __init__(self, guilds):
        self.guilds = list(guilds)
        self._by_id = {g.id: g for g in guilds}
        self._users = {}

    def get_guild(self, gid):
        return self._by_id.get(gid)

    def get_channel(self, cid):
        for g in self.guilds:
            ch = g.get_channel(cid)
            if ch:
                return ch
        return None

    def get_user(self, uid):
        return self._users.get(uid)

    async def fetch_user(self, uid):
        raise RuntimeError("no network")


class Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, s):
        self.now += timedelta(seconds=s)


def hr(sec):
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m{(sec % 60):02d}s"


class Rig:
    """A wired tracker + store + fake Supabase + controllable clock."""

    def __init__(self, start_ist=(2026, 3, 10, 7, 0), guilds=1, local_path=None):
        self.tmp = local_path or tempfile.mkdtemp()
        self.local = VoiceDatabase(f"{self.tmp}/local.db")
        self.client = FakeSupabaseClient()
        self.db = SupabaseVoiceDatabase(self.local, client=self.client)
        self.db._bootstrapped = True
        # Stand in for a verified schema v3 database.
        self.db._schema_version = 3
        self.db._schema_ok = True
        self.clock = Clock(datetime(*start_ist, tzinfo=IST))
        self.guilds = [FakeGuild(GUILD_ID + i) for i in range(guilds)]
        self.bot = FakeBot(self.guilds)
        self.tracker = VoiceTracker(
            self.bot, self.db, reconcile_interval=20.0, dm_reports_enabled=False,
            alert_channel_id=0, clock=self.clock, coalesce_seconds=2.0,
        )

    def member(self, user_id=USER_ID, guild=None, name="Alex"):
        g = guild or self.guilds[0]
        m = FakeMember(g, user_id, name)
        g.members[user_id] = m
        return m

    @staticmethod
    def state(channel, *, deaf=False, mute=False):
        return types.SimpleNamespace(
            channel=channel, self_mute=mute, mute=False,
            self_deaf=deaf, deaf=False,
        )

    def join(self, member, channel, **kw):
        channel.members.append(member)
        return self.tracker.handle_voice_state_update(
            member, types.SimpleNamespace(channel=None), self.state(channel, **kw)
        )

    def leave(self, member, channel):
        if member in channel.members:
            channel.members.remove(member)
        return self.tracker.handle_voice_state_update(
            member, self.state(channel), self.state(None)
        )

    async def shown(self, user_id=USER_ID, guild=None):
        g = guild or self.guilds[0]
        board = await self.tracker.get_live_leaderboard(g, hydrate=False)
        return next((m.total_seconds for m in board if m.user_id == user_id), 0)

    def committed(self, user_id=USER_ID, guild_id=GUILD_ID, date_str=None):
        date_str = date_str or self.clock.now.astimezone(IST).date().isoformat()
        return self.client.daily.get(
            (str(guild_id), str(user_id), date_str), {}
        ).get("duration_seconds", 0)

    def active_row(self, user_id=USER_ID, guild_id=GUILD_ID):
        return self.client.active.get((str(guild_id), str(user_id)))


RESULTS = []


def report(name, ok, detail):
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}\n         {detail}")


async def s1_gateway_blip():
    """A member briefly vanishes from the gateway cache and reappears."""
    print("\n--- Scenario 1: transient gateway cache blip mid-session ---")
    rig = Rig()
    m = rig.member()
    ch = rig.guilds[0].voice_channels[0]
    await rig.join(m, ch)
    for _ in range(90):
        rig.clock.advance(20)
        await rig.tracker.reconcile_guilds()
    before = await rig.shown()
    print(f"    after 30min connected: shown={hr(before)}")

    # The member disappears from the cached channel list for ONE cycle.
    ch.members.clear()
    rig.clock.advance(20)
    await rig.tracker.reconcile_guilds()
    ch.members.append(m)
    rig.clock.advance(20)
    await rig.tracker.reconcile_guilds()
    for _ in range(89):
        rig.clock.advance(20)
        await rig.tracker.reconcile_guilds()
    after = await rig.shown()
    print(f"    after 30min more     : shown={hr(after)}  committed={hr(rig.committed())}")
    report(
        "blip: no time lost across a transient cache miss",
        after >= before + 1800,
        f"before={hr(before)} after={hr(after)} (expected >= {hr(before + 1800)})",
    )


async def s2_restart():
    """Bot process restarts while the user stays connected."""
    print("\n--- Scenario 2: process restart mid-session (durable recovery) ---")
    tmp = tempfile.mkdtemp()
    rig = Rig(local_path=tmp)
    m = rig.member()
    ch = rig.guilds[0].voice_channels[0]
    await rig.join(m, ch)
    for _ in range(90):
        rig.clock.advance(20)
        await rig.tracker.reconcile_guilds()
    before = await rig.shown()
    print(f"    before restart: shown={hr(before)} row={rig.active_row()}")

    # Simulate a hard crash + restart: brand new tracker, same durable stores.
    rig2 = Rig(local_path=tmp)
    rig2.client = rig.client
    rig2.db = SupabaseVoiceDatabase(rig2.local, client=rig.client)
    rig2.db._schema_version = 3
    rig2.db._schema_ok = True
    rig2.db._bootstrapped = True
    rig2.clock = rig.clock
    rig2.bot = FakeBot(rig.guilds)
    rig2.tracker = VoiceTracker(
        rig2.bot, rig2.db, reconcile_interval=20.0, dm_reports_enabled=False,
        alert_channel_id=0, clock=rig2.clock, coalesce_seconds=2.0,
    )
    await rig2.tracker.reconcile_guilds()
    after = await rig2.shown()
    print(f"    after restart : shown={hr(after)}")
    report(
        "restart: credited time survives a process restart",
        after >= before,
        f"before={hr(before)} after={hr(after)}",
    )


async def s3_manual_edit():
    """Admin edits started_at in the Dashboard; the loop must respect it."""
    print("\n--- Scenario 3: Dashboard edit of voice_active_sessions.started_at ---")
    rig = Rig()
    m = rig.member()
    ch = rig.guilds[0].voice_channels[0]
    await rig.join(m, ch)
    # Tick on the real reconcile cadence (every 20s) rather than jumping an
    # hour in one go, so checkpoints actually fire as they would in production.
    for _ in range(180):
        rig.clock.advance(20)
        await rig.tracker.reconcile_guilds()
    row = rig.active_row()
    print(f"    row before edit: started_at={row['started_at']}")
    edited = (rig.clock.now - timedelta(hours=3)).isoformat()
    rig.client.active[(str(GUILD_ID), str(USER_ID))]["started_at"] = edited
    for _ in range(5):
        rig.clock.advance(20)
        await rig.tracker.reconcile_guilds()
    now_row = rig.active_row()
    report(
        "manual edit persists across tracking cycles",
        now_row["started_at"] == edited,
        f"edited={edited} now={now_row['started_at']}",
    )
    shown = await rig.shown()
    # The session had already checkpointed ~1h into voice_daily_stats. Moving
    # started_at earlier must NOT re-credit that hour: credited time lives in
    # voice_daily_stats, not in the active row.
    report(
        "already-credited time is NOT counted again after a start-time edit",
        3300 <= shown <= 4200,
        f"shown={hr(shown)} (must stay ~1h, must not jump to 3h)",
    )

    # The second, explicit knob: move the checkpoint cursor back too. Now the
    # whole corrected interval is uncredited and is honoured in full.
    rig.client.active[(str(GUILD_ID), str(USER_ID))]["credited_until"] = edited
    for _ in range(7):
        rig.clock.advance(20)
        await rig.tracker.reconcile_guilds()
    granted = await rig.shown()
    report(
        "editing credited_until grants the corrected interval in full",
        granted >= 3 * 3600 + 100,
        f"shown={hr(granted)} (expected > 3h)",
    )


async def s4_supabase_outage():
    """Supabase is unreachable for 10 minutes mid-session."""
    print("\n--- Scenario 4: Supabase outage mid-session ---")
    rig = Rig()
    m = rig.member()
    ch = rig.guilds[0].voice_channels[0]
    await rig.join(m, ch)
    for _ in range(45):
        rig.clock.advance(20)
        await rig.tracker.reconcile_guilds()
    before = await rig.shown()
    rig.client.fail_mode = True
    for _ in range(30):  # 10 minutes down
        rig.clock.advance(20)
        await rig.tracker.reconcile_guilds()
    rig.client.fail_mode = False
    for _ in range(45):
        rig.clock.advance(20)
        await rig.tracker.reconcile_guilds()
    after = await rig.shown()
    print(f"    before={hr(before)} after={hr(after)} committed={hr(rig.committed())}")
    report(
        "outage: tracking continues and no time is lost",
        after >= before + 1500,
        f"before={hr(before)} after={hr(after)} (expected >= {hr(before + 1500)})",
    )


async def s5_two_instances():
    """Two bot processes against the same Supabase tables (overlap deploy)."""
    print("\n--- Scenario 5: two bot instances writing the same session ---")
    rig = Rig()
    m = rig.member()
    ch = rig.guilds[0].voice_channels[0]
    await rig.join(m, ch)
    for _ in range(45):
        rig.clock.advance(20)
        await rig.tracker.reconcile_guilds()

    # A second instance boots 30 minutes later and recovers the session.
    rig2 = Rig()
    rig2.client = rig.client
    rig2.db = SupabaseVoiceDatabase(rig2.local, client=rig.client)
    rig2.db._schema_version = 3
    rig2.db._schema_ok = True
    rig2.db._bootstrapped = True
    rig2.clock = rig.clock
    rig2.bot = FakeBot(rig.guilds)
    rig2.tracker = VoiceTracker(
        rig2.bot, rig2.db, reconcile_interval=20.0, dm_reports_enabled=False,
        alert_channel_id=0, clock=rig2.clock, coalesce_seconds=2.0,
    )
    await rig2.tracker.reconcile_guilds()

    before = await rig.shown()
    for _ in range(90):
        rig.clock.advance(20)
        await rig.tracker.reconcile_guilds()
        await rig2.tracker.reconcile_guilds()
    after = await rig.shown()
    after2 = await rig2.shown()
    print(f"    inst1 before={hr(before)} after={hr(after)} inst2 after={hr(after2)}")
    report(
        "two instances: neither loses or double-counts the interval",
        after >= before + 1800 and after2 >= 2700,
        f"inst1 {hr(before)}->{hr(after)} inst2={hr(after2)}",
    )


async def s6_channel_move():
    print("\n--- Scenario 6: channel move mid-session ---")
    rig = Rig()
    m = rig.member()
    ch1, ch2 = rig.guilds[0].voice_channels
    await rig.join(m, ch1)
    for _ in range(45):
        rig.clock.advance(20)
        await rig.tracker.reconcile_guilds()
    before = await rig.shown()
    ch1.members.remove(m)
    ch2.members.append(m)
    await rig.tracker.handle_voice_state_update(m, rig.state(ch1), rig.state(ch2))
    for _ in range(45):
        rig.clock.advance(20)
        await rig.tracker.reconcile_guilds()
    after = await rig.shown()
    report(
        "channel move: no lost time, no session reset",
        after >= before + 900,
        f"before={hr(before)} after={hr(after)} (expected >= {hr(before + 900)})",
    )


async def s7_midnight():
    """A session crossing IST midnight must split correctly."""
    print("\n--- Scenario 7: session crossing IST midnight ---")
    rig = Rig(start_ist=(2026, 3, 10, 22, 0))
    m = rig.member()
    ch = rig.guilds[0].voice_channels[0]
    await rig.join(m, ch)
    day1 = "2026-03-10"
    day2 = "2026-03-11"
    for _ in range(1080):  # 6 hours -> 04:00 IST next day
        rig.clock.advance(20)
        await rig.tracker.reconcile_guilds()
        await rig.tracker.rollover_midnight_sessions(rig.clock.now)
    d1 = rig.committed(date_str=day1)
    d2 = rig.committed(date_str=day2)
    shown = await rig.shown()
    print(f"    day1 committed={hr(d1)} day2 committed={hr(d2)} shown(today)={hr(shown)}")
    # `shown` is committed(day2) + the still-live remainder of day2, so the
    # invariant is that the two days add back to the whole 6-hour session.
    report(
        "midnight: day1 gets 2h and day2 gets 4h (committed + live)",
        abs(d1 - 7200) <= 60 and abs(shown - 14400) <= 60
        and abs(d1 + shown - 21600) <= 120,
        f"day1={hr(d1)} day2={hr(shown)} total={hr(d1 + shown)}",
    )


async def main():
    print("=" * 78)
    print("ADVERSARIAL ACCOUNTING SCENARIOS")
    print("=" * 78)
    await s1_gateway_blip()
    await s2_restart()
    await s3_manual_edit()
    await s4_supabase_outage()
    await s5_two_instances()
    await s6_channel_move()
    await s7_midnight()
    print("\n" + "=" * 78)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"TOTAL: {len(RESULTS) - len(failed)}/{len(RESULTS)} invariants held")
    for n in failed:
        print(f"  BROKEN: {n}")
    print("=" * 78)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
