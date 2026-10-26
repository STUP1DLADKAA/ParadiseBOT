"""Offline reproduction harness for the voice-accounting incident.

Simulates the exact production timeline reported in the incident:
a user joins voice at 07:00 IST and stays connected. Every 30 minutes we
read the SAME authoritative leaderboard the /vc command reads, plus the raw
Supabase rows, and print them.

Uses the real production store (SupabaseVoiceDatabase) against the in-memory
fake client that emulates the PostgreSQL functions, and the real VoiceTracker.
No network, no credentials, no production data.

Usage:
    python3 tools/repro_incident.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from functions.voice.database import VoiceDatabase  # noqa: E402
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
        self.voice_channels = [FakeChannel(CHANNEL_ID)]
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
    def __init__(self, guild):
        self.guilds = [guild]
        self._guild = guild
        self._users = {}

    def get_guild(self, gid):
        return self._guild if gid == self._guild.id else None

    def get_channel(self, cid):
        return self._guild.get_channel(cid)

    def get_user(self, uid):
        return self._users.get(uid)

    async def fetch_user(self, uid):
        raise RuntimeError("no network in repro")


class Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


async def build(start_ist_hour=7, guilds=1):
    """Build a tracker whose clock starts at 07:00 IST on 2026-03-10."""
    tmp = tempfile.mkdtemp()
    local = VoiceDatabase(f"{tmp}/local.db")
    client = FakeSupabaseClient()
    db = SupabaseVoiceDatabase(local, client=client)
    db._schema_version = 3
    db._schema_ok = True
    db._bootstrapped = True

    day = datetime(2026, 3, 10, start_ist_hour, 0, tzinfo=IST)
    clock = Clock(day)
    guild_list = [FakeGuild(GUILD_ID + i) for i in range(guilds)]
    bot = FakeBot(guild_list[0])
    bot.guilds = guild_list
    bot._guild_by_id = {g.id: g for g in guild_list}
    bot.get_guild = lambda gid: bot._guild_by_id.get(gid)

    tracker = VoiceTracker(
        bot, db, reconcile_interval=20.0, dm_reports_enabled=False,
        alert_channel_id=0, clock=clock, coalesce_seconds=2.0,
    )
    return tracker, db, client, clock, guild_list, local


async def snapshot(tracker, db, client, guild_id=GUILD_ID):
    """Read exactly what /vc leaderboard reads, plus the raw rows."""
    board = await tracker.get_live_leaderboard(
        tracker.bot.get_guild(guild_id), hydrate=False
    )
    shown = next((m.total_seconds for m in board if m.user_id == USER_ID), 0)
    key = (str(guild_id), str(USER_ID))
    daily = client.daily.get(key, {}).get("duration_seconds", 0)
    active = client.active.get(key)
    started = active["started_at"] if active else None
    return shown, daily, started


def hr(sec):
    return f"{sec//3600}h{(sec%3600)//60:02d}m"


async def scenario_continuous(label="Issue A/B: continuous session 07:00 -> 15:00 IST"):
    print("=" * 78)
    print(label)
    print("=" * 78)
    tracker, db, client, clock, guilds, _ = await build()
    guild = guilds[0]
    member = FakeMember(guild, USER_ID, "Alex")
    guild.members[USER_ID] = member
    guild.voice_channels[0].members.append(member)

    state = types.SimpleNamespace(
        channel=guild.voice_channels[0], self_mute=False, mute=False,
        self_deaf=False, deaf=False,
    )
    await tracker.handle_voice_state_update(
        member, types.SimpleNamespace(channel=None), state
    )

    prev = None
    regressions = []
    # 8h31m of simulated time at the real 20s reconcile cadence.
    for step in range(1531):
        clock.advance(20)
        await tracker.reconcile_guilds()
        if step % 90 == 0 or step == 1530:
            shown, daily, started = await snapshot(tracker, db, client)
            wall = clock.now.astimezone(IST).strftime("%H:%M")
            flag = ""
            if prev is not None and shown < prev:
                flag = "   <-- REGRESSION"
                regressions.append((wall, prev, shown))
            prev = shown
            print(f"  {wall} IST | shown={hr(shown):>8} | committed={hr(daily):>8} "
                  f"| active_started_at={started} |{flag}")
    print()
    print(f"  EXPECTED after 8h30m connected: 8h30m")
    print(f"  REGRESSIONS OBSERVED: {regressions if regressions else 'none'}")
    return regressions


async def scenario_manual_edit():
    print()
    print("=" * 78)
    print("Issue D: admin edits voice_active_sessions.started_at in the Dashboard")
    print("=" * 78)
    tracker, db, client, clock, guilds, _ = await build()
    guild = guilds[0]
    member = FakeMember(guild, USER_ID, "Alex")
    guild.members[USER_ID] = member
    guild.voice_channels[0].members.append(member)
    state = types.SimpleNamespace(
        channel=guild.voice_channels[0], self_mute=False, mute=False,
        self_deaf=False, deaf=False,
    )
    await tracker.handle_voice_state_update(
        member, types.SimpleNamespace(channel=None), state
    )
    clock.advance(3600)  # one hour in
    await tracker.reconcile_guilds()
    key = (str(GUILD_ID), str(USER_ID))
    print(f"  before edit : started_at = {client.active[key]['started_at']}")

    # --- the admin moves the start one hour earlier in the Dashboard ---
    edited = (clock.now - timedelta(hours=2)).isoformat()
    client.active[key]["started_at"] = edited
    print(f"  admin edits : started_at = {edited}  (-2h from now)")

    for i in range(5):
        clock.advance(20)
        await tracker.reconcile_guilds()
    print(f"  after 100s  : started_at = {client.active[key]['started_at']}")
    preserved = client.active[key]["started_at"] == edited
    print(f"  EDIT PRESERVED: {preserved}")
    return not preserved


async def main():
    regressions = await scenario_continuous()
    reverted = await scenario_manual_edit()
    print()
    print("=" * 78)
    print("SUMMARY")
    print(f"  Issue A/B daily total regression reproduced : {bool(regressions)}")
    print(f"  Issue D manual started_at edit reverted     : {reverted}")
    print("=" * 78)


if __name__ == "__main__":
    asyncio.run(main())
