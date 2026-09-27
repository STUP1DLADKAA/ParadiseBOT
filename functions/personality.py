"""Conservative reactions and tiny bits of unprompted personality."""

from __future__ import annotations

import asyncio
import logging
import random
import re
from collections import defaultdict, deque
from datetime import UTC, date, datetime, timedelta

import discord

log = logging.getLogger(__name__)


class Personality:
    def __init__(self, bot: discord.Client) -> None:
        self.bot = bot
        self.message_counts: dict[int, int] = defaultdict(int)
        self.active_messages: dict[int, deque[datetime]] = defaultdict(
            lambda: deque(maxlen=50)
        )
        self.guild_days: dict[int, deque[date]] = defaultdict(lambda: deque(maxlen=60))
        self._reply_cooldowns: dict[int, datetime] = {}
        self._reaction_cooldowns: dict[int, datetime] = {}
        self._last_spontaneous = datetime.min.replace(tzinfo=UTC)

    @staticmethod
    def _ready(last: datetime | None, seconds: int) -> bool:
        return last is None or datetime.now(UTC) - last > timedelta(seconds=seconds)

    def _record(self, message: discord.Message) -> None:
        if not message.guild:
            return
        guild_id = message.guild.id
        now = datetime.now(UTC)
        self.message_counts[guild_id] += 1
        self.active_messages[message.channel.id].append(now)
        days = self.guild_days[guild_id]
        if not days or days[-1] != now.date():
            days.append(now.date())

    def daily_streak(self, guild_id: int) -> int:
        unique = set(self.guild_days[guild_id])
        cursor = datetime.now(UTC).date()
        streak = 0
        while cursor in unique:
            streak += 1
            cursor -= timedelta(days=1)
        return streak

    async def handle(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        self._record(message)
        content = message.content.lower().strip()
        now = datetime.now(UTC)

        reaction = self._reaction_for(content)
        if (
            reaction
            and random.random() < 0.10
            and self._ready(self._reaction_cooldowns.get(message.channel.id), 90)
        ):
            try:
                await asyncio.wait_for(message.add_reaction(reaction), timeout=8)
                self._reaction_cooldowns[message.channel.id] = now
            except (discord.DiscordException, asyncio.TimeoutError):
                log.debug("Reaction failed", exc_info=True)

        reply = self._reply_for(content)
        if (
            reply
            and random.random() < 0.055
            and self._ready(self._reply_cooldowns.get(message.channel.id), 12 * 60)
        ):
            try:
                await asyncio.wait_for(
                    message.reply(reply, mention_author=False), timeout=10
                )
                self._reply_cooldowns[message.channel.id] = now
            except (discord.DiscordException, asyncio.TimeoutError):
                log.debug("Personality reply failed", exc_info=True)

        await self._maybe_spontaneous(message, now)
        self._prune(now)

    @staticmethod
    def _reaction_for(content: str) -> str | None:
        groups = (
            (r"\b(happy birthday|congrats|we won|lets go|let's go|finally)\b", "🎉"),
            (r"\b(lmao|lmfao|that's funny|thats funny)\b", "😭"),
            (r"\b(wait what|what is happening|i'm confused|im confused)\b", "❓"),
            (r"\b(nice|good news|huge win)\b", "👍"),
        )
        return next(
            (emoji for pattern, emoji in groups if re.search(pattern, content)), None
        )

    @staticmethod
    def _reply_for(content: str) -> str | None:
        groups = (
            (
                r"\b(no way|you won't believe|you wont believe)\b",
                ("wait what", "okay I'm listening"),
            ),
            (
                r"\b(i have a question|weird question)\b",
                ("this already sounds dangerous", "go on"),
            ),
            (
                r"\b(what is happening|what just happened)\b",
                ("excellent question", "I looked away for one second"),
            ),
            (
                r"\b(i did it|we did it|finally finished)\b",
                ("actually huge", "look at you go"),
            ),
        )
        for pattern, replies in groups:
            if re.search(pattern, content):
                return random.choice(replies)
        return None

    async def _maybe_spontaneous(self, message: discord.Message, now: datetime) -> None:
        recent = self.active_messages[message.channel.id]
        while recent and now - recent[0] > timedelta(minutes=10):
            recent.popleft()
        if (
            len(recent) < 15
            or now - self._last_spontaneous < timedelta(hours=4)
            or random.random() >= 0.002
        ):
            return
        lines = (
            "this conversation has layers",
            "I leave for five minutes",
            "carry on",
            "noted.",
        )
        try:
            await asyncio.wait_for(
                message.channel.send(random.choice(lines)), timeout=10
            )
            self._last_spontaneous = now
        except (discord.DiscordException, asyncio.TimeoutError):
            log.debug("Spontaneous message failed", exc_info=True)

    def _prune(self, now: datetime) -> None:
        cutoff = now - timedelta(hours=6)
        self._reply_cooldowns = {
            key: value for key, value in self._reply_cooldowns.items() if value > cutoff
        }
        self._reaction_cooldowns = {
            key: value
            for key, value in self._reaction_cooldowns.items()
            if value > cutoff
        }
