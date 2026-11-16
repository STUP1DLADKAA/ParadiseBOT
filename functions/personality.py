"""Paradise's passive reaction engine.

Paradise never interrupts chat with automatic text, GIF, image, meme or sticker
replies. On ordinary unprompted messages the only passive action it can take is
an occasional, context-fitting emoji reaction — gated by spam detection,
serious-topic detection, global/channel/user/lord cooldowns and a rolling
hourly channel cap.

Explicit banter services (``meme_feed``, ``gif_search`` and ``roast_intensity``)
are still owned here so ``/meme``, ``/gif``, ``/roast`` and ``@Paradise`` pings
continue to work on demand.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass

import discord

from config import PersonalityConfig, lord_ids_of
from functions.meme_feed import GifSearch, MemeFeed
from functions.roasts import looks_sensitive, sanitize_intensity

log = logging.getLogger(__name__)

# Messages beginning with these look like commands for other bots.
_COMMAND_PREFIXES = ("/", "!", "?", ".", "-", "$", ";", ">", "+", "=", "~")

# Spam guard: this many messages inside the window means the channel is
# popping off (or being flooded) — Paradise stays quiet either way.
_SPAM_WINDOW_SECONDS = 10.0
_SPAM_MESSAGE_COUNT = 5
#: How long "stop roasting me" / "leave me alone" pauses passive reactions.
_OPTOUT_SECONDS = 6 * 3600.0

_URL_ONLY_RE = re.compile(r"^\s*https?://\S+\s*$", re.IGNORECASE)
_STOP_RE = re.compile(
    r"\b(stop (roasting|bullying|mocking|insulting|reacting to) me|leave me alone|"
    r"be nice to me|don'?t roast me|that'?s enough|please stop)\b",
    re.IGNORECASE,
)

#: Occasional loyalty reactions when a lord speaks.
LORD_REACTIONS: tuple[str, ...] = ("👑", "🗿", "🫡")

#: Word-boundary check for messages discussing Paradise without an @-mention.
_ABOUT_PARADISE_RE = re.compile(
    r"(?<![\w@<])paradise(?:\s?bot)?(?:'s)?(?![\w])"
    r"(?!\s+(?:hotel|city|lost|club|beach|island|bay|lake|park|resort|inn"
    r"|cafe|café|lounge|point|lane))"
    r"|\bthe\s+bot\b|\bthis\s+bot\b",
    re.IGNORECASE,
)


def is_about_paradise(content: str) -> bool:
    """True when the message discusses Paradise rather than addressing him."""
    return _ABOUT_PARADISE_RE.search(str(content or "")) is not None


_SERIOUS_TOPIC_RE = re.compile(
    r"\b(passed away|died|funeral|hospital|cancer|suicide|self[- ]harm|"
    r"depressed|grief|condolences|rest in peace)\b",
    re.IGNORECASE,
)


@dataclass(slots=True)
class Pattern:
    """One recognizable message context mapped to fitting emoji reactions."""

    name: str
    regex: re.Pattern
    reactions: tuple[str, ...] = ()


def _p(name: str, pattern: str, reactions=()) -> Pattern:
    return Pattern(name, re.compile(pattern, re.IGNORECASE), tuple(reactions))


# Order matters: the first matching pattern wins, so more specific contexts sit
# above generic ones. Patterns carry reactions only — never automatic replies.
PATTERNS: tuple[Pattern, ...] = (
    _p("loyalty", r"\b(stupid|stup1dladkaa)\b", reactions=("👑", "🗿", "⚔️")),
    _p("loyalty-taher", r"\b(taher|t4her)\b", reactions=("👑", "🗿", "⚔️")),
    _p(
        "achievement",
        r"\b(i (finally )?(fixed|finished|did|beat|solved|won)|we (won|did it)|"
        r"it works now|i actually won|first try|100%|finally (fixed|finished|done|works))\b",
        reactions=("🏆", "🎉", "👍", "🫡"),
    ),
    _p(
        "failure",
        r"\b(i broke (it|something)|it('?s| is) broken|everything('?s| is) broken|"
        r"i give up|i failed|it crashed|we lost|i lost again|nothing works)\b",
        reactions=("💀", "😭", "🫠"),
    ),
    _p(
        "who_broke",
        r"\b(who (broke|did|deleted|touched|changed) (this|that|it)|who broke\b)",
        reactions=("👀", "🤔"),
    ),
    _p(
        "cursed",
        r"\b(so cursed|this is cursed|cursed (image|thing)|why does this exist|unsee)\b",
        reactions=("😭", "💀", "🙈"),
    ),
    _p(
        "no_way",
        r"\b(no way|no shot|you('?re| are) (kidding|joking)|i can'?t believe|unbelievable)\b",
        reactions=("💀", "😳", "👀"),
    ),
    _p(
        "wtf",
        r"\b(wtf|what the (heck|hell)|bruh what|what is going on|what just happened)\b",
        reactions=("❓", "💀", "😶"),
    ),
    _p(
        "funny",
        r"\b(lmao+|lmfao+|lol+|rofl|i'?m (dying|dead|crying)|too funny|😂|🤣)",
        reactions=("😭", "💀", "😂"),
    ),
    _p(
        "tired",
        r"\b(so tired|i'?m (tired|sleepy|exhausted)|need (sleep|a nap)|no sleep|"
        r"going to (bed|sleep)|good night|gn everyone)\b",
        reactions=("😴", "🛌"),
    ),
    _p(
        "hungry",
        r"\b(i'?m (hungry|starving)|so hungry|need food|what should i eat|"
        r"ordering (food|pizza))\b",
        reactions=("🍕", "🍿"),
    ),
    _p(
        "minecraft",
        r"\b(minecraft|creeper|diamonds?|netherite|villagers?|redstone|"
        r"the nether|end(er)? dragon)\b",
        reactions=("⛏️", "🧱"),
    ),
    _p(
        "gaming",
        r"\b(one more (game|match|round)|i'?m (lagging|so bad at this)|carry me|"
        r"we lost the (game|match)|clutch(ed)?|gg(s)?\b|ranked)\b",
        reactions=("🎮", "🫡", "🔥"),
    ),
    _p(
        "dead_server",
        r"\b(this server is dead|dead (chat|server)|nobody('?s| is) (online|here)|"
        r"so quiet (in )?here|anyone (here|alive|awake))\b",
        reactions=("👀", "🦗"),
    ),
    _p(
        "look",
        r"\b((bro|guys|yo)?,? ?look at this|check this out|look what i (made|found|did))\b",
        reactions=("👀", "🔍"),
    ),
    _p(
        "help",
        r"\b(can (someone|anyone) help|help me (out|with)|i need help|pls help|please help)\b",
        reactions=("🫡", "🤝"),
    ),
    _p(
        "confusion",
        r"\b(i('?m| am) (so )?confused|makes no sense|"
        r"i don'?t (get|understand) (it|this)|huh+\b|wait what)\b",
        reactions=("❓", "🤨"),
    ),
    _p(
        "broke_wallet",
        r"\b(i('?m| am) (so )?broke|0 bank balance|no money left|my wallet is crying)\b",
        reactions=("💸", "💀"),
    ),
    _p(
        "exams_study",
        r"\b(have an exam|studying for|haven'?t studied|"
        r"i'?m cooked for (the )?(exam|test)|homework due)\b",
        reactions=("📚", "🫡", "💀"),
    ),
    _p(
        "cap_detector",
        r"\b(that'?s cap|stop the cap|biggest lie|source: trust me)\b",
        reactions=("🧢", "🤨"),
    ),
    _p(
        "copium",
        r"\b(copium|cope harder|coping|cope\b|seethe|malding|delusional)\b",
        reactions=("😭", "💀", "🥤"),
    ),
    _p(
        "ratio",
        r"\b(ratio|L take|L \+|big L|lmao ratioed|negative aura)\b",
        reactions=("💀", "📉", "🤡"),
    ),
    _p(
        "sus",
        r"\b(sus|sussy|impostor|among us|kinda sus)\b",
        reactions=("🔍", "👀", "🚨"),
    ),
    _p(
        "based",
        r"\b(based|w take|gigachad|chad|absolute unit|respectfully based)\b",
        reactions=("🗿", "🔥", "👑"),
    ),
    _p(
        "rizz",
        r"\b(rizz|rizzler|flirt(ing)?|she replied|he replied|my rizz)\b",
        reactions=("💀", "😏", "🫣"),
    ),
    _p(
        "gng",
        r"\b(gng|gang|ngl|fr fr|on god|no cap|deadass|lowkey|highkey)\b",
        reactions=("💯", "🗣️", "🫡"),
    ),
    _p(
        "gym",
        r"\b(gym|leg day|bench press|deadlift|personal best|protein|gains)\b",
        reactions=("💪", "🏋️", "🥛"),
    ),
    _p(
        "school",
        r"\b(school|class|teacher|exam tomorrow|homework|assignments? due|professor)\b",
        reactions=("📚", "🫡", "💀"),
    ),
    _p(
        "bored",
        r"\b(i(?:'?m| am) (?:so )?bored|nothing to do|someone talk to me|entertain me)\b",
        reactions=("🫠", "👀", "🎲"),
    ),
    _p("birthday", r"\b(happy birthday|hbd|bday)\b", reactions=("🎂", "🎉", "🥳")),
    _p(
        "greeting",
        r"^(hi|hii+|hello|hey|heyy+|yo|sup|good morning|gm|morning|hola|namaste)\b",
        reactions=("👋", "🫡"),
    ),
    _p("why", r"^why\b", reactions=("🤔",)),
    _p(
        "frustration",
        r"\b(i hate this|so annoying|this sucks|i'?m done|why is this so hard|aaa+gh|ughh*)\b",
        reactions=("😤", "🫠"),
    ),
    _p(
        "excitement",
        r"\b(let'?s goo+|lesgo+|hype+|so excited|can'?t wait|yess+!*|w in the chat)\b",
        reactions=("🔥", "🎉", "🙌"),
    ),
    _p("bro", r"^bro+\b", reactions=("💀", "👀")),
)

#: Subtle emoji pool used on rare occasions when no specific pattern matched.
AMBIENT_REACTIONS: tuple[str, ...] = (
    "👀",
    "💀",
    "😭",
    "🔥",
    "🗿",
    "🫡",
    "🤨",
    "😂",
    "📌",
    "🍿",
    "🧠",
    "🫠",
)


class Personality:
    """Occasional passive emoji reactor; never sends unprompted messages."""

    def __init__(
        self,
        bot: discord.Client,
        config: PersonalityConfig,
        *,
        meme_feed: MemeFeed | None = None,
        gif_search: GifSearch | None = None,
    ) -> None:
        self.bot = bot
        self.config = config
        #: Internet memes — used strictly by explicit ``/meme`` & ``@Paradise meme``.
        self.meme_feed = meme_feed if meme_feed is not None else MemeFeed(
            sources=tuple(config.meme_sources),
            timeout=float(config.online_timeout),
        )
        #: Reaction GIFs — used strictly by explicit ``/gif`` & ``@Paradise gif``.
        self.gif_search = gif_search if gif_search is not None else GifSearch(
            tenor_key=config.tenor_api_key,
            giphy_key=config.giphy_api_key,
            timeout=float(config.online_timeout),
        )
        self.roast_intensity = sanitize_intensity(config.roast_intensity)
        bot_config = getattr(bot, "config", None)
        self.lord_user_ids: tuple[int, ...] = lord_ids_of(bot_config)

        # In-memory bounded activity & cooldown state.
        self._channel_activity: dict[int, deque[float]] = {}
        self._last_by_author: dict[int, str] = {}
        self._last_global: float = float("-inf")
        self._last_by_channel: dict[int, float] = {}
        self._last_by_user: dict[int, float] = {}
        self._last_lord_by_channel: dict[int, float] = {}
        self._ambient_log: dict[int, deque[float]] = defaultdict(deque)
        self._user_optout: dict[int, float] = {}
        self._recent_reactions: deque[str] = deque(maxlen=8)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def handle(self, message: discord.Message) -> None:
        """Observe one unaddressed message and at most add one emoji reaction."""
        if not self.config.enabled:
            return
        if not self._eligible(message):
            return

        content = message.content.strip()
        now = time.monotonic()
        channel_id = int(message.channel.id)
        author_id = int(message.author.id)
        self._track(channel_id, now)

        if self._is_spam(channel_id, author_id, content, now):
            return
        if _SERIOUS_TOPIC_RE.search(content) or looks_sensitive(content):
            return

        if _STOP_RE.search(content):
            self._user_optout[author_id] = now + _OPTOUT_SECONDS
            return
        if self._user_optout.get(author_id, 0.0) > now:
            return

        if not self._cooldowns_ready(channel_id, author_id, now):
            return
        if not self._ambient_budget_ok(channel_id, now):
            return

        # 1. Rare loyalty reaction when a lord speaks.
        if self._is_lord(author_id) and self._lord_ready(channel_id, now):
            lord_chance = float(self.config.lord_reaction_chance)
            if lord_chance > 0 and random.random() < lord_chance:
                if await self._add_reaction(message, LORD_REACTIONS, now):
                    self._last_lord_by_channel[channel_id] = now
                    self._record_ambient(channel_id, now)
                    return

        # 2. Contextual reaction for known phrases.
        pattern = self.match(content)
        if pattern is not None and pattern.reactions:
            chance = float(self.config.reaction_chance)
            if chance > 0 and random.random() < chance:
                if await self._add_reaction(message, pattern.reactions, now):
                    self._record_ambient(channel_id, now)
            return

        # 3. Very rare ambient reaction on unmatched casual lines.
        if not self._is_casual_chat(content):
            return
        ambient_chance = float(self.config.ambient_reaction_chance)
        if ambient_chance > 0 and random.random() < ambient_chance:
            if await self._add_reaction(message, AMBIENT_REACTIONS, now):
                self._record_ambient(channel_id, now)

    # ------------------------------------------------------------------
    # Guards & pacing
    # ------------------------------------------------------------------

    def _eligible(self, message: discord.Message) -> bool:
        """Every reason Paradise must stay completely still, checked up front."""
        if message.guild is None:
            return False
        author = message.author
        if author.bot:
            return False
        if message.webhook_id is not None:
            return False
        if self.bot.user is not None and author.id == self.bot.user.id:
            return False
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return False
        content = (message.content or "").strip()
        if not content or len(content) > 300:
            return False
        if content.startswith(_COMMAND_PREFIXES) or _URL_ONLY_RE.match(content):
            return False
        if self.bot.user is not None:
            if any(user.id == self.bot.user.id for user in message.mentions):
                return False
        return True

    @staticmethod
    def _is_casual_chat(content: str) -> bool:
        if len(content) < 8 or _URL_ONLY_RE.match(content):
            return False
        return len(content.split()) >= 2 and any(ch.isalpha() for ch in content)

    def _is_lord(self, user_id: int) -> bool:
        return bool(self.lord_user_ids) and int(user_id) in self.lord_user_ids

    def _track(self, channel_id: int, now: float) -> None:
        activity = self._channel_activity.setdefault(
            channel_id, deque(maxlen=_SPAM_MESSAGE_COUNT * 4)
        )
        activity.append(now)
        if len(self._channel_activity) > 256:
            self._channel_activity.pop(next(iter(self._channel_activity)))

    def _is_spam(self, channel_id: int, author_id: int, content: str, now: float) -> bool:
        normalized = " ".join(content.lower().split())
        if self._last_by_author.get(author_id) == normalized:
            return True
        self._last_by_author[author_id] = normalized
        if len(self._last_by_author) > 500:
            self._last_by_author.pop(next(iter(self._last_by_author)))
        activity = self._channel_activity.get(channel_id, ())
        recent = sum(1 for stamp in activity if now - stamp <= _SPAM_WINDOW_SECONDS)
        return recent >= _SPAM_MESSAGE_COUNT

    def _cooldowns_ready(self, channel_id: int, author_id: int, now: float) -> bool:
        if now - self._last_global < self.config.global_cooldown:
            return False
        if now - self._last_by_channel.get(channel_id, float("-inf")) < self.config.channel_cooldown:
            return False
        user_cd = float(getattr(self.config, "user_cooldown", 0) or 0)
        if user_cd > 0 and now - self._last_by_user.get(author_id, float("-inf")) < user_cd:
            return False
        return True

    def _lord_ready(self, channel_id: int, now: float) -> bool:
        lord_cd = float(getattr(self.config, "lord_cooldown", 0) or 0)
        last = self._last_lord_by_channel.get(channel_id, float("-inf"))
        return now - last >= lord_cd

    def _ambient_budget_ok(self, channel_id: int, now: float) -> bool:
        """True while the channel still has passive reactions left this hour."""
        cap = int(getattr(self.config, "ambient_hourly_cap", 0) or 0)
        if cap <= 0:
            return True
        entries = self._ambient_log[channel_id]
        cutoff = now - 3600.0
        while entries and entries[0] <= cutoff:
            entries.popleft()
        return len(entries) < cap

    def _record_ambient(self, channel_id: int, now: float) -> None:
        self._ambient_log[channel_id].append(now)

    # ------------------------------------------------------------------
    # Context matching & single reaction action
    # ------------------------------------------------------------------

    @staticmethod
    def match(content: str) -> Pattern | None:
        """The first pattern the message matches, or None (the usual case)."""
        return next(
            (pattern for pattern in PATTERNS if pattern.regex.search(content)),
            None,
        )

    @staticmethod
    def _pick(pool: tuple[str, ...], recent: deque[str]) -> str:
        fresh = [item for item in pool if item not in recent]
        choice = random.choice(fresh or pool)
        recent.append(choice)
        return choice

    async def _add_reaction(
        self,
        message: discord.Message,
        pool: tuple[str, ...],
        now: float,
    ) -> bool:
        if not pool:
            return False
        emoji = self._pick(pool, self._recent_reactions)
        try:
            await asyncio.wait_for(message.add_reaction(emoji), timeout=10)
        except (discord.DiscordException, asyncio.TimeoutError):
            log.debug("Passive reaction skipped (permissions/network)", exc_info=True)
            return False
        self._mark(int(message.channel.id), int(message.author.id), now)
        self._prune(now)
        return True

    def _mark(self, channel_id: int, author_id: int, now: float) -> None:
        self._last_global = now
        self._last_by_channel[channel_id] = now
        self._last_by_user[author_id] = now

    def _prune(self, now: float) -> None:
        cutoff = now - 6 * 3600
        for bucket in (
            self._last_by_channel,
            self._last_by_user,
            self._last_lord_by_channel,
        ):
            stale = [key for key, value in bucket.items() if value <= cutoff]
            for key in stale:
                bucket.pop(key, None)
        for channel_id, entries in list(self._ambient_log.items()):
            while entries and entries[0] <= now - 3600.0:
                entries.popleft()
            if not entries:
                self._ambient_log.pop(channel_id, None)
        for user_id, expiry in list(self._user_optout.items()):
            if expiry <= now:
                self._user_optout.pop(user_id, None)


__all__ = [
    "AMBIENT_REACTIONS",
    "LORD_REACTIONS",
    "PATTERNS",
    "Pattern",
    "Personality",
    "is_about_paradise",
]
