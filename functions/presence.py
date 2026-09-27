"""Slow, independent presence rotation."""

from __future__ import annotations

import asyncio
import logging
import random

import discord

log = logging.getLogger(__name__)

CUSTOM_STATUSES = (
    "probably sleeping",
    "looking for food",
    "minecraft time",
    "not today",
    "thinking",
    "brb",
    "I need snacks",
    "doing absolutely nothing",
    "why am I awake",
    "somewhere in paradise",
    "I forgot",
    "busy existing",
    "don't ask",
    "everything is fine",
    "just here",
    "away, spiritually",
    "one moment",
    "alive probably",
    "out of office (the office is my room)",
    "waiting for something",
    "no thoughts",
    "taking it easy",
    "currently online",
    "figuring it out",
    "maybe later",
    "the usual",
    "lost in the menu",
    "weather pending",
    "restarting my brain",
    "still loading",
    "quiet hours",
    "doing laundry eventually",
    "somewhere nearby",
    "normal status",
    "checking the fridge again",
    "in my lane",
    "nothing to report",
    "temporarily a concept",
    "thinking about garlic bread",
    "back soon-ish",
)

ACTIVITIES = (
    (discord.ActivityType.playing, "Minecraft"),
    (discord.ActivityType.watching, "everyone"),
    (discord.ActivityType.playing, "Thinking"),
    (discord.ActivityType.playing, "Doing important research"),
    (discord.ActivityType.playing, "Looking for snacks"),
    (discord.ActivityType.watching, "the ceiling"),
    (discord.ActivityType.playing, "Existing"),
    (discord.ActivityType.playing, "Avoiding responsibilities"),
    (discord.ActivityType.playing, "Counting pixels"),
    (discord.ActivityType.listening, "nothing"),
    (discord.ActivityType.playing, "Investigating"),
    (discord.ActivityType.playing, "Touching grass"),
    (discord.ActivityType.playing, "Doing absolutely nothing"),
    (discord.ActivityType.watching, "a loading bar"),
    (discord.ActivityType.playing, "with the settings"),
    (discord.ActivityType.listening, "the rain"),
    (discord.ActivityType.watching, "old videos"),
    (discord.ActivityType.playing, "One more game"),
    (discord.ActivityType.playing, "Finding the good spoon"),
    (discord.ActivityType.watching, "the group chat unfold"),
    (discord.ActivityType.listening, "background noise"),
    (discord.ActivityType.playing, "Solitaire badly"),
    (discord.ActivityType.playing, "Rock Paper Scissors alone"),
    (discord.ActivityType.watching, "paint remain dry"),
    (discord.ActivityType.playing, "A perfectly normal game"),
    (discord.ActivityType.listening, "someone in the kitchen"),
    (discord.ActivityType.playing, "Remembering why I came here"),
    (discord.ActivityType.watching, "the clock"),
)


class PresenceManager:
    """Keeps both pools fresh without hammering Discord's presence endpoint.

    Discord exposes one activity slot for bot accounts. The two independent loops
    therefore take turns displaying either a custom-style line or an activity.
    """

    def __init__(self, bot: discord.Client) -> None:
        self.bot = bot
        self._tasks: list[asyncio.Task[None]] = []
        self._last_status: str | None = None
        self._last_activity: tuple[discord.ActivityType, str] | None = None
        self._lock = asyncio.Lock()

    def start(self) -> None:
        if self._tasks:
            return
        self._tasks = [
            asyncio.create_task(self._status_loop(), name="custom-status"),
            asyncio.create_task(self._activity_loop(), name="activity"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    @staticmethod
    def _different(pool: tuple, previous):
        choices = [item for item in pool if item != previous]
        return random.choice(choices)

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

    async def _status_loop(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            self._last_status = self._different(CUSTOM_STATUSES, self._last_status)
            await self._set(discord.CustomActivity(name=self._last_status))
            await asyncio.sleep(random.uniform(2.5 * 3600, 6 * 3600))

    async def _activity_loop(self) -> None:
        await self.bot.wait_until_ready()
        # Let the initial custom status stay visible for a while.
        await asyncio.sleep(random.uniform(20 * 60, 35 * 60))
        while not self.bot.is_closed():
            self._last_activity = self._different(ACTIVITIES, self._last_activity)
            kind, name = self._last_activity
            await self._set(discord.Activity(type=kind, name=name))
            await asyncio.sleep(random.uniform(20 * 60, 60 * 60))
