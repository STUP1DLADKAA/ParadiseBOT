"""Central Discord presence state and local profile-note rotation.

Discord's public bot API does not expose ordinary account profile-note edits.
The note pool is retained and rotated in memory, but it is never published as
an activity or misrepresented as an account note. Presence itself is managed
only here; no activity payload is ever sent.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections import deque
from datetime import datetime, time, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

import discord

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
ROTATE_SECONDS = 20 * 60
OVERNIGHT_START = time(20, 0)
DAY_START = time(8, 0)

NOTES = (
    "probably doing something questionable",
    "paradise has excellent wifi in my imagination",
    "currently observing the chaos",
    "no thoughts, just paradise",
    "professional overthinker",
    "this server is a social experiment",
    "emotionally attached to the loading screen",
    "someone here owes me an explanation",
    "paradise lore continues",
    "the council is probably asleep",
    "Probably doing something.",
    "Paradise is watching 👀",
    "Someone definitely broke something.",
    "Powered by questionable decisions.",
    "Everything is under control. Probably.",
    "Do not disturb the Paradise.",
    "Checking the server...",
    "Still here.",
    "Pretending to be productive.",
    "Someone said ping?",
    "404: Motivation not found.",
    "Monitoring the chaos.",
    "The bot has opinions.",
    "Currently avoiding responsibilities.",
    "Running entirely on vibes.",
    "This server is interesting...",
    "Who gave me permissions?",
    "I saw that.",
    "Nothing suspicious here.",
    "Paradise has entered the chat.",
    "Keeping an eye on things.",
    "The pixels are working.",
    "Probably shouldn't have said that.",
    "Loading unnecessary thoughts...",
    "Everything is fine.",
    "Definitely not spying.",
    "I have seen enough.",
    "Another day in Paradise.",
    "Why are you all awake?",
    "Processing nonsense...",
    "Server chaos detected.",
    "I could say something, but I won't.",
    "Interesting.",
    "Noted.",
    "I am merely observing.",
    "This place is wild.",
    # -- extra Paradise-flavoured notes so the pool never feels repetitive --
    "Watching. Judging. Quietly.",
    "That message will be remembered.",
    "Uptime is my only personality trait.",
    "Reading the room. Slowly.",
    "Somebody feed the bot.",
    "I know what you did last message.",
    "Statistically, someone is typing.",
    "Emotional support software.",
    "Taking notes nobody asked for.",
    "The audit log knows everything.",
    "Zero thoughts, full uptime.",
    "Buffering an opinion...",
    "This channel needs a nap.",
    "I blinked and missed nothing.",
    "Professional lurker.",
    "Screenshots have been taken.",
    "Trust me, I'm a bot.",
    "Idle, but with intent.",
    "The vibes are being monitored.",
    "Counting messages for no reason.",
    "One of you is muted for a reason.",
    "Petition to rename everything.",
    "Compiling gossip...",
    "My RAM is full of your messages.",
    "Being online is my hobby.",
    "Suspiciously quiet in here.",
    "The lore grows daily.",
    "I remember everything. Unfortunately.",
    "Deleting nothing. Forgetting nothing.",
    "Certified server furniture.",
    "Ping me and find out.",
    "Awake against my will.",
    "The server sleeps. I do not.",
    "Rebooting my patience.",
    "Today's forecast: chaos.",
    "Somewhere between online and done.",
    "Held together by duct tape.",
    "Silently updating my opinions.",
    "You're all my favorite problem.",
    "Observing paradise. Literally.",
    "Nothing happened. Yet.",
    "That was almost funny.",
    "Archiving the nonsense.",
    "Two members typing. Zero sending.",
    "I pretend to work, too.",
    "Not a mod. Still judging.",
    "Insert status here.",
    "Voice channel echoes only.",
    "My other status is funnier.",
    "Waiting for the next incident.",
    "Locally famous in one server.",
    "Doing bot things. Allegedly.",
    "The quiet before the ping storm.",
    "Free trial of consciousness.",
    "History will judge this chat.",
    "Still no coffee module installed.",
    "Everything I see stays seen.",
)



class PresenceManager:
    """Owns status priority, IST boundary changes, and note generation."""

    def __init__(
        self,
        bot: discord.Client,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.bot = bot
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._game_active = False
        self._last_status: discord.Status | None = None
        self._recent_notes: deque[str] = deque(maxlen=8)
        self._current_note: str | None = None
        self._last_note_at: datetime | None = None

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value

    @classmethod
    def _next_status_boundary(cls, now: datetime) -> datetime:
        local = cls._aware(now).astimezone(IST)
        if local.timetz().replace(tzinfo=None) < DAY_START:
            boundary_day, boundary_time = local.date(), DAY_START
        elif local.timetz().replace(tzinfo=None) < OVERNIGHT_START:
            boundary_day, boundary_time = local.date(), OVERNIGHT_START
        else:
            boundary_day, boundary_time = local.date() + timedelta(days=1), DAY_START
        return datetime.combine(boundary_day, boundary_time, tzinfo=IST)

    @classmethod
    def is_overnight(cls, now: datetime | None = None) -> bool:
        local = cls._aware(now or datetime.now(timezone.utc)).astimezone(IST)
        return local.hour >= 20 or local.hour < 8

    def desired_status(self, now: datetime | None = None) -> discord.Status:
        if self._game_active:
            return discord.Status.dnd
        return discord.Status.idle if self.is_overnight(now or self._clock()) else discord.Status.online

    def pick_note(self) -> str:
        """Select a playful note, avoiding the most recent eight selections."""
        choices = [note for note in NOTES if note not in self._recent_notes]
        if not choices:
            self._recent_notes.clear()
            choices = list(NOTES)
        note = random.choice(choices)
        self._recent_notes.append(note)
        return note

    def start(self) -> None:
        """Start one boundary/note task. Repeated calls are harmless."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="paradise-presence")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def set_game_active(self, active: bool) -> None:
        """Set or clear mini-game priority and recalculate immediately."""
        changed = self._game_active != bool(active)
        self._game_active = bool(active)
        self._wake.set()
        is_ready = getattr(self.bot, "is_ready", None)
        if changed and (is_ready is None or is_ready()):
            await self.apply_current()

    async def apply_current(self, *, force: bool = False) -> None:
        """Apply the current status, including after a Discord reconnect."""
        desired = self.desired_status()
        if not force and desired == self._last_status:
            return
        async with self._lock:
            # Re-check under the lock so two callers cannot publish stale state.
            desired = self.desired_status()
            if not force and desired == self._last_status:
                return
            try:
                await asyncio.wait_for(
                    self.bot.change_presence(status=desired, activity=None), timeout=15
                )
            except (discord.DiscordException, asyncio.TimeoutError):
                log.warning("Could not update Discord presence", exc_info=True)
                return
            self._last_status = desired
            log.info("Presence changed to %s", desired.name.lower())

    async def _loop(self) -> None:
        await self.bot.wait_until_ready()
        await self.apply_current(force=True)
        self._current_note = self.pick_note()
        self._last_note_at = self._aware(self._clock()).astimezone(timezone.utc)
        log.debug("Generated profile note locally; Discord bot profile-note editing is unsupported")

        while not self.bot.is_closed():
            now = self._aware(self._clock()).astimezone(timezone.utc)
            next_boundary = self._next_status_boundary(now).astimezone(timezone.utc)
            next_note = self._last_note_at + timedelta(seconds=ROTATE_SECONDS)
            timeout = max(0.25, min((next_boundary - now).total_seconds(), (next_note - now).total_seconds()))
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass

            now = self._aware(self._clock()).astimezone(timezone.utc)
            if now >= next_note:
                self._current_note = self.pick_note()
                self._last_note_at = now
                log.debug("Generated profile note locally; not published (Discord bot API limitation)")
            await self.apply_current()
