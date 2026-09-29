"""Paradise's presence: always «👀 Watching Paradise», with a rotating note.

The activity itself never changes — only the small status note attached to
it (the activity's ``state`` line, visible on the profile) rotates every
20 minutes, never repeating back-to-back.
"""

from __future__ import annotations

import asyncio
import logging
import random

import discord

log = logging.getLogger(__name__)

# The one and only activity. Do not rotate this.
WATCHING = "Paradise"
# How often the note rotates.
ROTATE_SECONDS = 20 * 60

NOTES = (
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
    """Owns the single presence loop.

    ``start`` is idempotent — calling it twice never creates a second timer —
    and ``apply_current`` re-asserts the current presence after reconnects so
    the activity survives restarts without advancing the rotation.
    """

    def __init__(self, bot: discord.Client) -> None:
        self.bot = bot
        self._task: asyncio.Task[None] | None = None
        self._last_note: str | None = None
        self._current_note: str | None = None
        self._lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return  # never run two rotation timers
        self._task = asyncio.create_task(self._loop(), name="paradise-presence")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    # -- rotation ------------------------------------------------------------

    def pick_note(self) -> str:
        """A random note, never the same one twice in a row."""
        choices = [note for note in NOTES if note != self._last_note]
        note = random.choice(choices)
        self._last_note = note
        return note

    @staticmethod
    def build_activity(note: str) -> discord.Activity:
        # The activity is always "Watching Paradise"; the note rides along as
        # the activity's state line and is the only thing that rotates.
        return discord.Activity(
            type=discord.ActivityType.watching, name=WATCHING, state=note
        )

    async def apply_current(self) -> None:
        """Re-assert the current presence (used after reconnects)."""
        if self._current_note is not None:
            await self._set(self.build_activity(self._current_note))

    async def _set(self, activity: discord.BaseActivity) -> None:
        async with self._lock:
            try:
                await asyncio.wait_for(
                    self.bot.change_presence(
                        status=discord.Status.online, activity=activity
                    ),
                    timeout=15,
                )
            except (discord.DiscordException, asyncio.TimeoutError):
                log.warning("Could not update presence", exc_info=True)

    async def _loop(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            self._current_note = self.pick_note()
            await self._set(self.build_activity(self._current_note))
            await asyncio.sleep(ROTATE_SECONDS)
