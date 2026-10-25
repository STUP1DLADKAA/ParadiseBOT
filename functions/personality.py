"""Paradise's passive personality & humour engine.

Handles four complementary layers of server humour while staying respectful
of fast/flooded channels, commands, bots, DMs, and serious topics:

1. **English-only Nerd Grammar & Spelling Replies**: catches unmistakable
   typos and contextual grammar mistakes in genuine English sentences (never
   Hinglish or other languages) with playful ``🤓👆 err its akchewly ...``
   corrections.
2. **Contextual Pattern Reactions & Replies**: witty smart/stupid one-liners
   and emoji reactions tailored to what someone just said.
3. **Random Stickers & Message Memes**: replies to casual messages with
   random native Discord stickers (guild custom + standard packs, with a
   custom vinyl PNG fallback) or dynamically rendered meme cards featuring
   the message itself.
4. **Unpredictable Smart/Stupid Quips**: occasional 200-IQ over-analysis or
   2-IQ soup-brain remarks so Paradise feels genuinely alive.
"""

from __future__ import annotations

import asyncio
import io
import logging
import random
import re
import time
from collections import deque
from dataclasses import dataclass

import discord

from config import PersonalityConfig
from functions.grammar_nerd import detect_english_mistake, format_nerd_reply
from functions.memes import (
    MEME_TEMPLATES,
    StickerPicker,
    render_meme_png,
    render_sticker_png,
)

log = logging.getLogger(__name__)

# Messages beginning with these look like commands for other bots.
_COMMAND_PREFIXES = ("/", "!", "?", ".", "-", "$", ";", ">", "+", "=", "~")

# Spam guard: this many messages inside the window means the channel is
# popping off (or being flooded) — Paradise stays quiet either way.
_SPAM_WINDOW_SECONDS = 10.0
_SPAM_MESSAGE_COUNT = 5

_URL_ONLY_RE = re.compile(r"^\s*https?://\S+\s*$", re.IGNORECASE)
_SERIOUS_TOPIC_RE = re.compile(
    r"\b(passed away|died|funeral|hospital|cancer|suicide|self[- ]harm|depressed|grief|condolences|rest in peace)\b",
    re.IGNORECASE,
)


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
            "Hang it in the Louvre immediately.",
            "Even a broken clock cooks twice a day.",
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
            "Put it in rice and walk away slowly.",
            "Skill issue, respectfully.",
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
            "My lawyer advised me not to answer that.",
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
            "Pouring virtual bleach directly onto my RAM.",
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
            "Statistically improbable, yet here we are.",
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
            "Even my algorithms gave up trying to parse that.",
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
            "Calm down before you pull a lung.",
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
            "Your circadian rhythm just filed a restraining order.",
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
            "Drink soup. Soup solves ninety percent of problems.",
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
            "Trading 42 emeralds for one loaf of bread, peak economics.",
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
            "Ending on a loss? Couldn't be you. Queue again.",
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
            "Excuse me, am I invisible to you?",
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
            "Squinting at my monitor like an art critic.",
        ),
    ),
    _p(
        "help",
        r"\b(can (someone|anyone) help|help me (out|with)|i need help|pls help|please help)\b",
        reactions=("🫡", "🤝"),
        replies=(
            "Someone will show up. Statistically.",
            "Step one: don't panic. Step two: unknown.",
            "I offer moral support and zero practical qualifications.",
        ),
    ),
    _p(
        "confusion",
        r"\b(i('?m| am) (so )?confused|makes no sense|i don'?t (get|understand) (it|this)|huh+\b|wait what)\b",
        reactions=("❓", "🤨"),
        replies=(
            "Confusion is the first step to more confusion.",
            "Nobody understands it. We just nod.",
            "My two brain cells also bounced right off that.",
        ),
    ),
    _p(
        "broke_wallet",
        r"\b(i('?m| am) (so )?broke|0 bank balance|no money left|my wallet is crying)\b",
        reactions=("💸", "💀"),
        replies=(
            "Have you considered simply spawning more money?",
            "Financially thriving in spirit, bankrupt in numbers.",
            "Air is still free. For now.",
        ),
    ),
    _p(
        "exams_study",
        r"\b(have an exam|studying for|haven'?t studied|i'?m cooked for (the )?(exam|test)|homework due)\b",
        reactions=("📚", "🫡", "💀"),
        replies=(
            "Closing Discord increases exam scores by forty percent.",
            "Academic comeback starts in five minutes. Always five minutes.",
            "Just write 'it depends' on every question.",
        ),
    ),
    _p(
        "cap_detector",
        r"\b(that'?s cap|stop the cap|biggest lie|source: trust me)\b",
        reactions=("🧢", "🤨"),
        replies=(
            "My lie detector just exploded.",
            "Source: it was revealed to them in a dream.",
            "Even Wikipedia wouldn't accept that citation.",
        ),
    ),
    _p(
        "greeting",
        r"^(hi|hii+|hello|hey|heyy+|yo|sup|good morning|gm|morning|hola|namaste)\b",
        reactions=("👋", "🫡"),
        replies=(
            "Hey. I was here the whole time.",
            "Welcome back to the chaos.",
            "Good timing. Nothing happened.",
            "Greetings, fellow carbon-based lifeform.",
        ),
    ),
    _p(
        "why",
        r"^why\b",
        reactions=("🤔",),
        replies=(
            "Some questions have no answers. This is one.",
            "Why not?",
            "Socrates asked the same thing and look how that went.",
        ),
    ),
    _p(
        "frustration",
        r"\b(i hate this|so annoying|this sucks|i'?m done|why is this so hard|aaa+gh|ughh*)\b",
        reactions=("😤", "🫠"),
        replies=(
            "Deep breaths. The code can smell fear.",
            "Rage is a valid debugging strategy.",
            "Staring angrily at the screen usually intimidates it.",
        ),
    ),
    _p(
        "excitement",
        r"\b(let'?s goo+|lesgo+|hype+|so excited|can'?t wait|yess+!*|w in the chat)\b",
        reactions=("🔥", "🎉", "🙌"),
        replies=(
            "The hype is noted.",
            "Big W energy.",
            "Write this date down in the history books.",
        ),
    ),
    _p(
        "bro",
        r"^bro+\b",
        reactions=("💀", "👀"),
        replies=(
            "Bro.",
            "I'm listening. Reluctantly.",
            "Every catastrophic story starts with 'bro'.",
        ),
    ),
)

# Wildly unpredictable mix of 200-IQ pseudo-intellectual observations and
# 2-IQ soup-brain remarks for spontaneous chat interjections.
CHAOS_QUIPS: tuple[str, ...] = (
    "wait if tomatoes are fruit does that make ketchup a smoothie? sorry keep talking.",
    "statistically speaking, 84% of plans made in this channel collapse before Saturday.",
    "i just tried to count to infinity and lost my place at seven.",
    "fascinating point. unfortunately Pluto isn't even a planet anymore so none of this matters.",
    "nodding aggressively like i understood a single word of that.",
    "thermodynamically, this conversation is increasing the entropy of the universe.",
    "hold on, my last two brain cells just hit the corner like the DVD logo.",
    "peer-reviewed conclusion: y'all need water and eight hours of REM sleep.",
    "i forgot how to blink for a second, then remembered i don't have eyelids.",
    "Socrates died for the right to ask questions and THIS is what we're typing.",
    "bold statement from someone with 47 open browser tabs right now.",
    "writing that down in my little notebook of questionable server lore.",
    "according to quantum mechanics, you are both right and completely cooked until observed.",
    "wait hold up, do fish ever get thirsty or are they just vibing?",
    "that sentence had words in it, and honestly i respect the effort.",
    "i asked my calculator about this and it just said SYNTAX ERROR.",
    "you know what, i was gonna argue, but soup sounds better.",
    "historians in 3026 are going to study this chat log and weep.",
    "my lawyer told me not to agree with this, yet spiritually i feel it.",
    "hang on let me put on my reading glasses... nope still wild.",
    "if overthinking burned calories this server would have negative mass.",
    "interesting theory. counterpoint: aracadabra. wait i misspelled abracadabra.",
    "every time someone types in here, an angel loses its Wi-Fi connection.",
    "that's either the smartest thing said today or a cry for snacks.",
    "hold on i was buffering emotionally, say that again.",
    "Newton discovered gravity because an apple fell; what's your excuse?",
    "i'm not saying you're wrong, i'm just staring at the camera like The Office.",
    "brain scan complete: zero thoughts, immaculate confidence.",
    "wait... if you clean a vacuum cleaner, do YOU become the vacuum cleaner?",
    "certified hood classic of a sentence right there.",
    "just checked the server constitution and surprisingly that's legal.",
    "sometimes i pretend to be a supercomputer, sometimes i forget what Tuesday is.",
    "10 out of 10 confidence, 2 out of 10 factual accuracy, i respect it.",
    "don't mind me, just standing here collecting data for my stand-up routine.",
)

_MEME_CAPTIONS: tuple[str, ...] = (
    "",
    "",
    "had to immortalize this one 💀",
    "framing this on the server wall",
    "live footage from general chat:",
    "my honest reaction to that information:",
)


class Personality:
    def __init__(self, bot: discord.Client, config: PersonalityConfig) -> None:
        self.bot = bot
        self.config = config
        self.sticker_picker = StickerPicker(bot)
        # Recency data (all in-memory, all bounded).
        self._channel_activity: dict[int, deque[float]] = {}
        self._last_by_author: dict[int, str] = {}
        # Cooldown clocks (monotonic seconds).
        self._last_global: float = float("-inf")
        self._last_by_channel: dict[int, float] = {}
        self._last_reply_by_channel: dict[int, float] = {}
        self._last_humour_by_channel: dict[int, float] = {}
        self._last_grammar_by_channel: dict[int, float] = {}
        # Anti-repetition queues.
        self._recent_replies: deque[str] = deque(maxlen=12)
        self._recent_reactions: deque[str] = deque(maxlen=4)
        self._recent_quips: deque[str] = deque(maxlen=16)
        self._recent_templates: deque[str] = deque(maxlen=3)
        self._recent_nerd_templates: deque[str] = deque(maxlen=6)

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
        if _SERIOUS_TOPIC_RE.search(content):
            return

        channel_id = message.channel.id

        # 1. English-only nerd grammar/spelling check (has its own cooldown).
        if (
            self.config.grammar_chance > 0
            and self._grammar_ready(channel_id, now)
        ):
            mistake = detect_english_mistake(content)
            if mistake is not None and random.random() < self.config.grammar_chance:
                if await self._send_grammar_correction(message, mistake, now):
                    return

        if not self._cooldowns_ready(channel_id, now):
            return

        # 2. Contextual pattern match: reply, meme, sticker, or reaction (never two).
        pattern = self.match(content)
        if pattern is not None:
            roll = random.random()
            cursor = 0.0

            if pattern.replies and self._reply_ready(channel_id, now):
                cursor += self.config.reply_chance
                if roll < cursor:
                    await self._send_reply(message, pattern, now)
                    return
            else:
                cursor += self.config.reply_chance

            if self._humour_ready(channel_id, now):
                if self.config.meme_chance > 0:
                    cursor += self.config.meme_chance
                    if roll < cursor:
                        await self._send_meme_reply(message, now)
                        return
                if self.config.sticker_chance > 0:
                    cursor += self.config.sticker_chance
                    if roll < cursor:
                        await self._send_sticker_reply(message, now)
                        return

            if pattern.reactions and roll < cursor + self.config.reaction_chance:
                await self._send_reaction(message, pattern, now)
            return

        # 3. Unmatched casual messages: occasional random meme, sticker, or chaos quip!
        if not self._humour_ready(channel_id, now) or not self._is_casual_chat(content):
            return

        roll = random.random()
        meme_cut = self.config.meme_chance
        sticker_cut = meme_cut + self.config.sticker_chance
        quip_cut = sticker_cut + self.config.random_quip_chance

        if roll < meme_cut:
            await self._send_meme_reply(message, now)
        elif roll < sticker_cut:
            await self._send_sticker_reply(message, now)
        elif roll < quip_cut:
            await self._send_chaos_quip(message, now)

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

    @staticmethod
    def _is_casual_chat(content: str) -> bool:
        if len(content) < 8 or _URL_ONLY_RE.match(content):
            return False
        return len(content.split()) >= 2

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

    def _humour_ready(self, channel_id: int, now: float) -> bool:
        last = self._last_humour_by_channel.get(channel_id, float("-inf"))
        return now - last >= self.config.humour_cooldown

    def _grammar_ready(self, channel_id: int, now: float) -> bool:
        global_gap = min(self.config.global_cooldown, 20)
        if now - self._last_global < global_gap:
            return False
        last = self._last_grammar_by_channel.get(channel_id, float("-inf"))
        return now - last >= self.config.grammar_cooldown

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
        if await self._deliver_text_reply(message, reply):
            self._mark(message.channel.id, now)
            self._last_reply_by_channel[message.channel.id] = now
            self._prune(now)

    async def _send_chaos_quip(self, message: discord.Message, now: float) -> None:
        quip = self._pick(CHAOS_QUIPS, self._recent_quips)
        if await self._deliver_text_reply(message, quip):
            self._mark(message.channel.id, now)
            self._last_humour_by_channel[message.channel.id] = now
            self._prune(now)

    async def _send_grammar_correction(
        self, message: discord.Message, mistake, now: float
    ) -> bool:
        reply = format_nerd_reply(mistake, recent=self._recent_nerd_templates)
        if not await self._deliver_text_reply(message, reply):
            return False
        self._mark(message.channel.id, now)
        self._last_grammar_by_channel[message.channel.id] = now
        self._prune(now)
        return True

    async def _send_meme_reply(self, message: discord.Message, now: float) -> None:
        author_name = (
            getattr(message.author, "display_name", None)
            or getattr(message.author, "name", None)
            or "Someone"
        )
        template = self._pick(MEME_TEMPLATES, self._recent_templates)
        try:
            png_bytes, _ = render_meme_png(
                message.content,
                author_name,
                template=template,
            )
            caption = random.choice(_MEME_CAPTIONS)
            kwargs: dict = {
                "file": discord.File(io.BytesIO(png_bytes), filename="paradise_meme.png"),
                "mention_author": False,
                "allowed_mentions": discord.AllowedMentions.none(),
            }
            if caption:
                await asyncio.wait_for(message.reply(caption, **kwargs), timeout=12)
            else:
                await asyncio.wait_for(message.reply(**kwargs), timeout=12)
        except (discord.DiscordException, asyncio.TimeoutError, Exception):  # noqa: BLE001
            log.debug("Personality meme reply failed", exc_info=True)
            return
        self._mark(message.channel.id, now)
        self._last_humour_by_channel[message.channel.id] = now
        self._prune(now)

    async def _send_sticker_reply(self, message: discord.Message, now: float) -> None:
        sent = False
        sticker = await self.sticker_picker.pick(message.guild)
        if sticker is not None:
            try:
                await asyncio.wait_for(
                    message.reply(
                        stickers=[sticker],
                        mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none(),
                    ),
                    timeout=10,
                )
                sent = True
            except (discord.DiscordException, asyncio.TimeoutError):
                log.debug("Native Discord sticker reply failed; using PNG sticker fallback", exc_info=True)

        if not sent:
            try:
                png_bytes = render_sticker_png()
                await asyncio.wait_for(
                    message.reply(
                        file=discord.File(
                            io.BytesIO(png_bytes), filename="paradise_sticker.png"
                        ),
                        mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none(),
                    ),
                    timeout=10,
                )
                sent = True
            except (discord.DiscordException, asyncio.TimeoutError, Exception):  # noqa: BLE001
                log.debug("Fallback PNG sticker reply failed", exc_info=True)
                return

        if sent:
            self._mark(message.channel.id, now)
            self._last_humour_by_channel[message.channel.id] = now
            self._prune(now)

    async def _deliver_text_reply(self, message: discord.Message, text: str) -> bool:
        try:
            await asyncio.wait_for(
                message.reply(
                    text,
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                ),
                timeout=10,
            )
            return True
        except (discord.DiscordException, asyncio.TimeoutError):
            log.debug("Personality text reply failed", exc_info=True)
            return False

    def _mark(self, channel_id: int, now: float) -> None:
        self._last_global = now
        self._last_by_channel[channel_id] = now

    def _prune(self, now: float) -> None:
        cutoff = now - 6 * 3600
        for bucket in (
            self._last_by_channel,
            self._last_reply_by_channel,
            self._last_humour_by_channel,
            self._last_grammar_by_channel,
        ):
            stale = [key for key, value in bucket.items() if value <= cutoff]
            for key in stale:
                bucket.pop(key, None)
