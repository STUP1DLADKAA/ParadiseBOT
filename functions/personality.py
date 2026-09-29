"""Paradise's rare passive personality: contextual reactions and short replies.

Design rules (see PersonalityConfig in config.py for the tuning knobs):

- Most messages get nothing. This must feel like a rare Easter egg.
- A message never gets both a reaction and a reply.
- Bots, webhooks, system messages, command-looking messages, messages
  addressed to Paradise (the AI handles those) and spammy channels are
  all ignored.
- Replies are short, casual and picked from the pattern the message
  actually matched — never a random non-sequitur.
- Global + per-channel cooldowns keep Paradise from ever flooding.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections import deque
from dataclasses import dataclass

import discord

from config import PersonalityConfig

log = logging.getLogger(__name__)

# Messages beginning with these look like commands for other bots.
_COMMAND_PREFIXES = ("/", "!", "?", ".", "-", "$", ";", ">", "+", "=", "~")

# Spam guard: this many messages inside the window means the channel is
# popping off (or being flooded) — Paradise stays quiet either way.
_SPAM_WINDOW_SECONDS = 10.0
_SPAM_MESSAGE_COUNT = 5


@dataclass(slots=True)
class Pattern:
    """One recognizable message context: a regex, reactions and replies."""

    name: str
    regex: re.Pattern
    reactions: tuple[str, ...] = ()
    replies: tuple[str, ...] = ()


def _p(name: str, pattern: str, reactions=(), replies=()) -> Pattern:
    return Pattern(name, re.compile(pattern, re.IGNORECASE), tuple(reactions), tuple(replies))


# Order matters: the first matching pattern wins, so the more specific
# contexts sit above the generic ones.
PATTERNS: tuple[Pattern, ...] = (
    _p(
        "achievement",
        r"\b(i (finally )?(fixed|finished|did|beat|solved|won)|we (won|did it)|it works now|i actually won|first try|100%|finally (fixed|finished|done|works))\b",
        reactions=("🏆", "🎉", "👍", "🫡"),
        replies=(
            "About time. 😭",
            "Actually impressive.",
            "I'll allow it.",
            "Witnessed. It counts.",
            "Adding this to the server lore.",
        ),
    ),
    _p(
        "failure",
        r"\b(i broke (it|something)|it('?s| is) broken|everything('?s| is) broken|i give up|i failed|it crashed|we lost|i lost again|nothing works)\b",
        reactions=("💀", "😭", "🫠"),
        replies=(
            "It was working five minutes ago, wasn't it.",
            "A moment of silence. Okay, moving on.",
            "The bug is undefeated.",
            "Have you tried percussive maintenance?",
        ),
    ),
    _p(
        "who_broke",
        r"\b(who (broke|did|deleted|touched|changed) (this|that|it)|who broke\b)",
        reactions=("👀", "🤔"),
        replies=(
            "I have several theories.",
            "I saw everything. I'm saying nothing.",
            "Check the audit log. Or don't. Some things are better unknown.",
            "Not me. Probably.",
        ),
    ),
    _p(
        "cursed",
        r"\b(so cursed|this is cursed|cursed (image|thing)|why does this exist|unsee)\b",
        reactions=("😭", "💀", "🙈"),
        replies=(
            "I did not need to see that.",
            "Deleting this from my memory. Failed.",
            "This channel needs an exorcism.",
        ),
    ),
    _p(
        "no_way",
        r"\b(no way|no shot|you('?re| are) (kidding|joking)|i can'?t believe|unbelievable)\b",
        reactions=("💀", "😳", "👀"),
        replies=(
            "Way.",
            "Believe it.",
            "I watched it happen in real time.",
        ),
    ),
    _p(
        "wtf",
        r"\b(wtf|what the (heck|hell)|bruh what|what is going on|what just happened)\b",
        reactions=("❓", "💀", "😶"),
        replies=(
            "I looked away for one second.",
            "Don't ask me. I just live here.",
            "Excellent question. No answers.",
        ),
    ),
    _p(
        "funny",
        r"\b(lmao+|lmfao+|lol+|rofl|i'?m (dying|dead|crying)|too funny|😂|🤣)",
        reactions=("😭", "💀", "😂"),
        replies=(
            "It wasn't even that funny. Okay, it was.",
            "Screenshotted.",
            "Comedy is alive and it lives here.",
        ),
    ),
    _p(
        "tired",
        r"\b(so tired|i'?m (tired|sleepy|exhausted)|need (sleep|a nap)|no sleep|going to (bed|sleep)|good night|gn everyone)\b",
        reactions=("😴", "🛌"),
        replies=(
            "You chose to stay awake though.",
            "Sleep is free, you know.",
            "The bed has been waiting for hours.",
            "Log off. This is self-care advice from a bot.",
        ),
    ),
    _p(
        "hungry",
        r"\b(i'?m (hungry|starving)|so hungry|need food|what should i eat|ordering (food|pizza))\b",
        reactions=("🍕", "🍿"),
        replies=(
            "That's between you and the kitchen.",
            "The fridge situation isn't going to improve by chatting.",
            "Eat first. Type later.",
        ),
    ),
    _p(
        "minecraft",
        r"\b(minecraft|creeper|diamonds?|netherite|villagers?|redstone|the nether|end(er)? dragon)\b",
        reactions=("⛏️", "🧱"),
        replies=(
            "Don't dig straight down. Ancient wisdom.",
            "The creeper is already behind you.",
            "One more block. Famous last words.",
        ),
    ),
    _p(
        "gaming",
        r"\b(one more (game|match|round)|i'?m (lagging|so bad at this)|carry me|we lost the (game|match)|clutch(ed)?|gg(s)?\b|ranked)\b",
        reactions=("🎮", "🫡", "🔥"),
        replies=(
            "One more game is never one game.",
            "Blaming the lag. Classic.",
            "gg. Emotionally, at least.",
        ),
    ),
    _p(
        "dead_server",
        r"\b(this server is dead|dead (chat|server)|nobody('?s| is) (online|here)|so quiet (in )?here|anyone (here|alive|awake))\b",
        reactions=("👀", "🦗"),
        replies=(
            "And yet here you are.",
            "I'm online.",
            "Dead? I'm literally right here.",
            "The server sleeps. I do not.",
        ),
    ),
    _p(
        "look",
        r"\b((bro|guys|yo)?,? ?look at this|check this out|look what i (made|found|did))\b",
        reactions=("👀", "🔍"),
        replies=(
            "I'm looking. 👀",
            "Okay, that's actually worth looking at.",
            "Seen. Processing. No comment.",
        ),
    ),
    _p(
        "help",
        r"\b(can (someone|anyone) help|help me (out|with)|i need help|pls help|please help)\b",
        reactions=("🫡", "🤝"),
        replies=(
            "Someone will show up. Statistically.",
            "Step one: don't panic. Step two: unknown.",
        ),
    ),
    _p(
        "confusion",
        r"\b(i('?m| am) (so )?confused|makes no sense|i don'?t (get|understand) (it|this)|huh+\b|wait what)\b",
        reactions=("❓", "🤨"),
        replies=(
            "Confusion is the first step to more confusion.",
            "Nobody understands it. We just nod.",
        ),
    ),
    _p(
        "greeting",
        r"^(hi|hii+|hello|hey|heyy+|yo|sup|good morning|gm|morning|hola|namaste)\b",
        reactions=("👋",),
        replies=(
            "Hey. I was here the whole time.",
            "Welcome back to the chaos.",
            "Good timing. Nothing happened.",
        ),
    ),
    _p(
        "why",
        r"^why\b",
        reactions=("🤔",),
        replies=(
            "Some questions have no answers. This is one.",
            "Why not?",
        ),
    ),
    _p(
        "frustration",
        r"\b(i hate this|so annoying|this sucks|i'?m done|why is this so hard|aaa+gh|ughh*)\b",
        reactions=("😤", "🫠"),
        replies=(
            "Deep breaths. The code can smell fear.",
            "Rage is a valid debugging strategy.",
        ),
    ),
    _p(
        "excitement",
        r"\b(let'?s goo+|lesgo+|hype+|so excited|can'?t wait|yess+!*|w in the chat)\b",
        reactions=("🔥", "🎉", "🙌"),
        replies=(
            "The hype is noted.",
            "Big W energy.",
        ),
    ),
    _p(
        "bro",
        r"^bro+\b",
        reactions=("💀", "👀"),
        replies=(
            "Bro.",
            "I'm listening. Reluctantly.",
        ),
    ),
)


class Personality:
    def __init__(self, bot: discord.Client, config: PersonalityConfig) -> None:
        self.bot = bot
        self.config = config
        # Recency data (all in-memory, all bounded).
        self._channel_activity: dict[int, deque[float]] = {}
        self._last_by_author: dict[int, str] = {}
        # Cooldown clocks (monotonic seconds).
        self._last_global: float = float("-inf")
        self._last_by_channel: dict[int, float] = {}
        self._last_reply_by_channel: dict[int, float] = {}
        # Anti-repetition: recently used replies / reactions.
        self._recent_replies: deque[str] = deque(maxlen=10)
        self._recent_reactions: deque[str] = deque(maxlen=4)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def handle(self, message: discord.Message) -> None:
        if not self.config.enabled:
            return
        if not self._eligible(message):
            return

        content = message.content.strip()
        now = time.monotonic()
        self._track(message, now)

        if self._is_spam(message.channel.id, message.author.id, content, now):
            return
        if not self._cooldowns_ready(message.channel.id, now):
            return

        pattern = self.match(content)
        if pattern is None:
            return

        # One roll decides: reply (rarest), reaction (rare), or nothing
        # (the overwhelming default). Never both for the same message.
        roll = random.random()
        if (
            roll < self.config.reply_chance
            and pattern.replies
            and self._reply_ready(message.channel.id, now)
        ):
            await self._send_reply(message, pattern, now)
        elif roll < self.config.reply_chance + self.config.reaction_chance and pattern.reactions:
            await self._send_reaction(message, pattern, now)

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------

    def _eligible(self, message: discord.Message) -> bool:
        """Every reason Paradise must stay quiet, checked up front."""
        if message.guild is None:
            return False
        author = message.author
        if author.bot:  # other bots — and Paradise itself has .bot = True
            return False
        if message.webhook_id is not None:
            return False
        if self.bot.user is not None and author.id == self.bot.user.id:
            return False
        # Only plain messages and normal replies; no joins, pins, system noise.
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return False
        content = (message.content or "").strip()
        if not content or len(content) > 300:
            return False
        # Command-looking messages (for whatever other bots live here).
        if content.startswith(_COMMAND_PREFIXES):
            return False
        # Addressed to Paradise → the AI answers; the personality stays out.
        if self.bot.user is not None:
            if any(user.id == self.bot.user.id for user in message.mentions):
                return False
        return True

    def _track(self, message: discord.Message, now: float) -> None:
        activity = self._channel_activity.setdefault(
            message.channel.id, deque(maxlen=_SPAM_MESSAGE_COUNT * 4)
        )
        activity.append(now)

    def _is_spam(self, channel_id: int, author_id: int, content: str, now: float) -> bool:
        # Same person repeating themselves — never reward copy-paste spam.
        normalized = " ".join(content.lower().split())
        if self._last_by_author.get(author_id) == normalized:
            return True
        self._last_by_author[author_id] = normalized
        if len(self._last_by_author) > 500:  # bounded memory
            self._last_by_author.pop(next(iter(self._last_by_author)))
        # A channel firing too fast is either hype or a flood; stay out of both.
        activity = self._channel_activity.get(channel_id, ())
        recent = sum(1 for stamp in activity if now - stamp <= _SPAM_WINDOW_SECONDS)
        return recent >= _SPAM_MESSAGE_COUNT

    def _cooldowns_ready(self, channel_id: int, now: float) -> bool:
        if now - self._last_global < self.config.global_cooldown:
            return False
        last_channel = self._last_by_channel.get(channel_id, float("-inf"))
        return now - last_channel >= self.config.channel_cooldown

    def _reply_ready(self, channel_id: int, now: float) -> bool:
        last = self._last_reply_by_channel.get(channel_id, float("-inf"))
        return now - last >= self.config.reply_channel_cooldown

    # ------------------------------------------------------------------
    # Context matching
    # ------------------------------------------------------------------

    @staticmethod
    def match(content: str) -> Pattern | None:
        """The first pattern the message matches, or None (the usual case)."""
        return next(
            (pattern for pattern in PATTERNS if pattern.regex.search(content)), None
        )

    def _pick(self, pool: tuple[str, ...], recent: deque[str]) -> str:
        fresh = [item for item in pool if item not in recent]
        choice = random.choice(fresh or list(pool))
        recent.append(choice)
        return choice

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    async def _send_reaction(
        self, message: discord.Message, pattern: Pattern, now: float
    ) -> None:
        emoji = self._pick(pattern.reactions, self._recent_reactions)
        try:
            await asyncio.wait_for(message.add_reaction(emoji), timeout=8)
        except (discord.DiscordException, asyncio.TimeoutError):
            log.debug("Personality reaction failed", exc_info=True)
            return
        self._mark(message.channel.id, now)

    async def _send_reply(
        self, message: discord.Message, pattern: Pattern, now: float
    ) -> None:
        reply = self._pick(pattern.replies, self._recent_replies)
        try:
            await asyncio.wait_for(
                message.reply(
                    reply,
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                ),
                timeout=10,
            )
        except (discord.DiscordException, asyncio.TimeoutError):
            log.debug("Personality reply failed", exc_info=True)
            return
        self._mark(message.channel.id, now)
        self._last_reply_by_channel[message.channel.id] = now
        self._prune(now)

    def _mark(self, channel_id: int, now: float) -> None:
        self._last_global = now
        self._last_by_channel[channel_id] = now

    def _prune(self, now: float) -> None:
        cutoff = now - 6 * 3600
        self._last_by_channel = {
            key: value for key, value in self._last_by_channel.items() if value > cutoff
        }
        self._last_reply_by_channel = {
            key: value
            for key, value in self._last_reply_by_channel.items()
            if value > cutoff
        }
