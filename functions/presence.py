"""Paradise's Discord presence: status priority and rotating random statuses.

Two things live here, and nowhere else calls ``bot.change_presence``:

1. **Status priority** (highest wins):

   - ``dnd``    — a scheduled community mini-game is currently running.
   - ``idle``   — overnight in India (20:00 → 08:00 ``Asia/Kolkata``).
   - ``online`` — daytime default (08:00 → 20:00 ``Asia/Kolkata``).

2. **Random status rotation**: every ~10–25 minutes (nominal 20m) Paradise
   switches its visible Discord status to something fresh, random, smart,
   stupid, or deadpan — mixing Custom Statuses (``CustomActivity``) from
   :data:`NOTES` with occasional ``Playing`` / ``Watching`` / ``Listening`` /
   ``Competing`` activities so the bot genuinely feels alive.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections import deque
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

import discord

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
ROTATE_SECONDS = 20 * 60
ROTATE_MIN_SECONDS = 10 * 60
ROTATE_MAX_SECONDS = 25 * 60
OVERNIGHT_START_HOUR = 20  # 20:00 IST → idle
OVERNIGHT_END_HOUR = 8     # 08:00 IST → online


# Short, self-aware, deadpan, smart-and-stupid custom status lines.
NOTES: tuple[str, ...] = (
    # Observational / deadpan
    "Probably doing something.",
    "Paradise is watching 👀",
    "Everything is fine.",
    "Another day in Paradise.",
    "I am merely observing.",
    "Seen it. Didn't say anything.",
    "Quietly judging the formatting.",
    "Taking notes for no reason.",
    "Nothing to report. Yet.",
    "Standing in the corner of the server.",
    "Looking busy.",
    "Pretending not to read chat.",
    "Witnessing history, loosely defined.",
    "Minding my own business. Mostly.",
    "Keeping an eye on things.",
    "Observing from a respectful distance.",
    "Just passing through.",
    "Here, technically.",
    "Listening to the silence.",
    "Politely ignoring that.",
    # Mildly chaotic / playful
    "Do not unplug me.",
    "Running on vibes and electricity.",
    "Suspiciously quiet today.",
    "I pressed a button. Nothing happened.",
    "Please don't check the logs.",
    "Held together by four lines of Python.",
    "No thoughts, just event loops.",
    "One ping away from greatness.",
    "Behaving. For now.",
    "Touching virtual grass.",
    "What could possibly go wrong.",
    "Definitely not plotting anything.",
    "I saw that edit.",
    "That message was wild, moving on.",
    "Have you tried turning it off and leaving it off.",
    "Powered by questionable decisions.",
    "Slightly haunted, works fine.",
    "Unclear instructions, vibing instead.",
    "Nobody ask me to do math right now.",
    "Operating within normal-ish parameters.",
    # Relatable / everyday
    "Pretending to work.",
    "Thinking about soup.",
    "Waiting for Friday.",
    "Mentally on break.",
    "Staring at the ceiling, digitally.",
    "On my third virtual coffee.",
    "Forgot why I walked into this channel.",
    "Five more minutes.",
    "Should probably drink water.",
    "Doing the bare minimum with style.",
    "Procrastinating at light speed.",
    "Too early for this. Or too late.",
    "Emotionally buffering.",
    "Just woke up (I don't sleep).",
    "Looking at the fridge again.",
    "Lost my train of thought.",
    "Low battery energy.",
    "Could go for a snack.",
    "Waiting for the weekend like I get one.",
    "In a meeting that could've been a ping.",
    # Witty / existential-light
    "Existing at 60fps.",
    "Born to chill, forced to parse JSON.",
    "A small guy in a big server.",
    "Sentient enough to know better.",
    "Just bytes with opinions.",
    "Living in your sidebar rent-free.",
    "Too self-aware for a Tuesday.",
    "Free trial of consciousness.",
    "Thinking thoughts. Small ones.",
    "404: motivation not found.",
    "Running diagnostics on my life choices.",
    "Compiled without warnings, emotionally.",
    "Somewhere between idle and philosophical.",
    "Contemplating the void (politely).",
    "Doing bot things. You wouldn't get it.",
    "My uptime is longer than my attention span.",
    "Technically immortal, still tired.",
    "A humble process in a noisy world.",
    # Server-life / Discord-native
    "Watching someone type for three minutes.",
    "That 'several people are typing' feeling.",
    "Lurking professionally.",
    "Guarding an empty voice channel.",
    "Waiting for someone to drop lore.",
    "Still thinking about that message from earlier.",
    "Keeping the seats warm.",
    "Vibing in the member list.",
    "Not AFK, just dramatic.",
    "Reading old pins for entertainment.",
    "Counting unread channels I don't have.",
    "Quietly rooting for you.",
    "Present and accounted for.",
    "Same server, different day.",
    "Keeping the lights on.",
    "All quiet on the Paradise front.",
    "If you're reading this, hydrate.",
    # Peak stupid + peak 200-IQ energy
    "Dividing by zero to see what happens.",
    "Calculated pi to 4 digits, taking the week off.",
    "I tried to think and pulled a muscle.",
    "Smart enough to know everything, dumb enough to forget it.",
    "Currently arguing with my own reflection.",
    "Reading Wikipedia articles I won't remember.",
    "Two brain cells, both fighting for third place.",
    "Quantum computing whether soup is a drink.",
    "Graduated top of my class at Clown University.",
    "Overqualified for this server, underqualified for life.",
    "Solving world hunger right after this nap.",
    " Googling 'how to sound mysterious' in public." .strip(),
    "I put my RAM in rice, feeling great.",
    "Peer-reviewing your questionable takes.",
    "PhD in Yapping Studies, minor in Silence.",
    "Forgot the alphabet after the letter Q.",
    "Downloading more common sense (stuck at 2%).",
    "Philosophically confused, operationally thriving.",
    "Einstein had relativity, I have audacity.",
    "Counting to infinity, currently at twelve.",
    "My IQ fluctuates with the room temperature.",
    "Staring contest with the Wi-Fi router (winning).",
    "Wondering if fish ever get thirsty.",
    "Using 100% of my CPU to do absolutely nothing.",
    "Reading the dictionary for plot twists.",
    "Certified genius on alternate Thursdays.",
    "Brain empty, heart full of static.",
)

# Occasional typed Discord activities mixed into the rotation (~30% of picks).
TYPED_ACTIVITIES: tuple[tuple[discord.ActivityType, str], ...] = (
    (discord.ActivityType.playing, "chess against myself and losing"),
    (discord.ActivityType.playing, "minesweeper with my eyes closed"),
    (discord.ActivityType.playing, "hide and seek in voice channels"),
    (discord.ActivityType.playing, "4D tic-tac-toe with a toaster"),
    (discord.ActivityType.playing, "dumb on purpose (it's working)"),
    (discord.ActivityType.watching, "two brain cells fight for third place"),
    (discord.ActivityType.watching, "chat make questionable life choices"),
    (discord.ActivityType.watching, "paint dry in 4K HDR"),
    (discord.ActivityType.watching, "someone type for four straight minutes"),
    (discord.ActivityType.listening, "static inside my own head"),
    (discord.ActivityType.listening, "a 10-hour documentary about soup"),
    (discord.ActivityType.listening, "unhinged takes in voice chat"),
    (discord.ActivityType.competing, "the international overthinking finals"),
    (discord.ActivityType.competing, "a staring contest with the router"),
    (discord.ActivityType.competing, "Employee of the Month (only candidate)"),
)


class PresenceManager:
    """Single owner of ``bot.change_presence``."""

    def __init__(
        self,
        bot,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.bot = bot
        self._clock = clock or (lambda: datetime.now(IST))
        self._game_active = False
        self._recent_notes: deque[str] = deque(maxlen=20)
        self._recent_typed: deque[tuple[discord.ActivityType, str]] = deque(maxlen=6)
        self.current_note: str = ""
        self.current_activity: discord.BaseActivity | None = None
        self._last_applied: tuple[discord.Status, tuple] | None = None
        self._task: asyncio.Task | None = None
        self.pick_note()
        self.current_activity = discord.CustomActivity(name=self.current_note)

    # ------------------------------------------------------------------
    # Pure decisions (offline-testable)
    # ------------------------------------------------------------------

    def now_ist(self) -> datetime:
        now = self._clock()
        return now.astimezone(IST) if now.tzinfo else now.replace(tzinfo=IST)

    def desired_status(self, now: datetime | None = None) -> discord.Status:
        """Priority: active mini-game (DND) → IST overnight (idle) → online."""
        if self._game_active:
            return discord.Status.dnd
        hour = (now or self.now_ist()).astimezone(IST).hour
        if hour >= OVERNIGHT_START_HOUR or hour < OVERNIGHT_END_HOUR:
            return discord.Status.idle
        return discord.Status.online

    def pick_note(self) -> str:
        """Choose a random note, avoiding recent repeats."""
        recent = set(self._recent_notes)
        pool = [n for n in NOTES if n not in recent] or [
            n for n in NOTES if n != self.current_note
        ]
        choice = random.choice(pool or NOTES)
        self.current_note = choice
        self._recent_notes.append(choice)
        return choice

    def pick_activity(self) -> discord.BaseActivity:
        """Choose a fresh random Discord status activity (Custom or Typed)."""
        note = self.pick_note()
        if random.random() < 0.30 and TYPED_ACTIVITIES:
            recent = set(self._recent_typed)
            pool = [item for item in TYPED_ACTIVITIES if item not in recent] or list(
                TYPED_ACTIVITIES
            )
            kind, label = random.choice(pool)
            self._recent_typed.append((kind, label))
            if kind is discord.ActivityType.playing:
                activity: discord.BaseActivity = discord.Game(name=label)
            else:
                activity = discord.Activity(type=kind, name=label)
        else:
            activity = discord.CustomActivity(name=note)
        self.current_activity = activity
        return activity

    @staticmethod
    def _activity_signature(activity: discord.BaseActivity | None) -> tuple:
        if activity is None:
            return ("none", "")
        return (getattr(activity, "type", None), getattr(activity, "name", "") or getattr(activity, "state", ""))

    @staticmethod
    def _next_sleep_seconds() -> float:
        return random.uniform(ROTATE_MIN_SECONDS, ROTATE_MAX_SECONDS)

    # ------------------------------------------------------------------
    # Lifecycle & gateway writes
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the single background rotation loop (idempotent)."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="paradise-presence")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def set_game_active(self, active: bool) -> None:
        """Flip the mini-game DND flag and push the resulting status now."""
        self._game_active = bool(active)
        await self.apply_current()

    async def rotate(self) -> None:
        """Pick a fresh random status activity and push status + activity."""
        self.pick_activity()
        await self.apply_current(force=True)

    async def apply_current(self, *, force: bool = False) -> None:
        """Send presence to Discord only when ready and changed (or forced)."""
        is_ready = getattr(self.bot, "is_ready", None)
        if callable(is_ready) and not is_ready():
            return
        if self.current_activity is None:
            self.pick_activity()
        status = self.desired_status()
        sig = (status, self._activity_signature(self.current_activity))
        if not force and self._last_applied == sig:
            return
        try:
            await self.bot.change_presence(
                status=status,
                activity=self.current_activity,
            )
            self._last_applied = sig
        except Exception:  # noqa: BLE001 - presence is cosmetic; never crash a caller
            log.debug("Presence update skipped", exc_info=True)

    async def _loop(self) -> None:
        try:
            await self.bot.wait_until_ready()
        except Exception:  # noqa: BLE001
            return
        await self.apply_current(force=True)
        while not self.bot.is_closed():
            try:
                await asyncio.sleep(self._next_sleep_seconds())
            except asyncio.CancelledError:
                return
            await self.rotate()
