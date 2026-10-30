"""Paradise's personality engine — the part that makes it feel alive.

Six complementary layers, all of them on by default in a friends-only server:

1. **English-only nerd grammar**: playful ``🤓👆 err its akchewly …`` fixes for
   unmistakable English slips (never Hinglish, never serious topics).
2. **Contextual reactions & replies**: emoji reactions and one-liners chosen
   from what somebody actually said.
3. **Memes from the internet**: real memes from Reddit communities through
   the keyless meme feed, with the locally rendered meme card as the fallback
   whenever the internet is unreachable.
4. **Reaction GIFs**: Tenor/Giphy when a free key is configured, skipped
   silently otherwise.
5. **Roasts**: see :mod:`functions.roasts`. Aimed at what someone said or did,
   scaled by ``PERSONALITY_ROAST_INTENSITY``, never at who somebody is, and
   never when the message looks like real distress.
6. **Spontaneous quips**: 200-IQ over-analysis and 2-IQ soup-brain remarks.

The engine is *chatty* on purpose: every message is a chance, each kind of
action has its own chance, and only the cooldowns keep it from becoming a
wall of bot noise. Every knob is a `PERSONALITY_*` setting.
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
from functions.meme_feed import GifSearch, MemeFeed, OnlineMeme
from functions.memes import (
    MEME_TEMPLATES,
    StickerPicker,
    render_meme_png,
    render_sticker_png,
)
from functions.roasts import (
    build_comeback,
    build_reaction_line,
    build_roast,
    invites_roast,
    looks_sensitive,
    sanitize_intensity,
)

log = logging.getLogger(__name__)

# Messages beginning with these look like commands for other bots.
_COMMAND_PREFIXES = ("/", "!", "?", ".", "-", "$", ";", ">", "+", "=", "~")

# Spam guard: this many messages inside the window means the channel is
# popping off (or being flooded) — Paradise stays quiet either way.
_SPAM_WINDOW_SECONDS = 10.0
_SPAM_MESSAGE_COUNT = 5
#: How long "stop roasting me" is remembered (six hours, in-memory).
_ROAST_OPTOUT_SECONDS = 6 * 3600.0

_URL_ONLY_RE = re.compile(r"^\s*https?://\S+\s*$", re.IGNORECASE)
#: "stop roasting me" — honoured for the rest of the session, no argument.
_STOP_ROAST_RE = re.compile(
    r"\b(stop (roasting|bullying|mocking|insulting) me|leave me alone|"
    r"be nice to me|don'?t roast me|that'?s enough|please stop)\b",
    re.IGNORECASE,
)

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
        "copium",
        r"\b(copium|cope harder|coping|cope\b|seethe|malding|delusional)\b",
        reactions=("😭", "💀", "🥤"),
        replies=(
            "the copium is being administered intravenously i see.",
            "cope detected. opinion discarded.",
            "bro is inhaling that copium like it's oxygen.",
            "malding in 4k. beautiful.",
            "denial is a river in egypt and you are swimming.",
        ),
    ),
    _p(
        "ratio",
        r"\b(ratio|L take|L \+|big L|lmao ratioed|negative aura)\b",
        reactions=("💀", "📉", "🤡"),
        replies=(
            "ratioed by a bot. career ending.",
            "an L so big it needs its own postcode.",
            "the aura went negative and kept going.",
            "that's a ratio i'd frame.",
            "you got ratioed in a server with 12 people.",
        ),
    ),
    _p(
        "sus",
        r"\b(sus|sussy|impostor|among us|kinda sus)\b",
        reactions=("🔍", "👀", "🚨"),
        replies=(
            "sus. extremely sus. somebody call a meeting.",
            "i was doing tasks. unlike SOMEONE.",
            "the impostor is typing in this very channel.",
            "emergency meeting. i have no evidence, just vibes.",
        ),
    ),
    _p(
        "based",
        r"\b(based|w take|gigachad|chad|absolute unit|respectfully based)\b",
        reactions=("🗿", "🔥", "👑"),
        replies=(
            "based and paradise-pilled.",
            "rare W. i'm writing it down.",
            "chad behaviour detected. logging it.",
            "the take was so based my fans spun up.",
            "respect. reluctantly, but respect.",
        ),
    ),
    _p(
        "rizz",
        r"\b(rizz|rizzler|flirt(ing)?|she replied|he replied|my rizz)\b",
        reactions=("💀", "😏", "🫣"),
        replies=(
            "unspoken rizz. emphasis on unspoken.",
            "the rizz is theoretical at this point.",
            "she replied 'k'. that's a W to somebody.",
            "bro is rizzing up a loading screen.",
            "charisma: pending. buffering. still buffering.",
        ),
    ),
    _p(
        "gng",
        r"\b(gng|gang|ngl|fr fr|on god|no cap|deadass|lowkey|highkey)\b",
        reactions=("💯", "🗣️", "🫡"),
        replies=(
            "gng i hear you and i'm choosing to keep it quiet.",
            "no cap detected. unusual for this channel.",
            "deadass? in this economy?",
            "lowkey everyone already knew.",
            "fr fr and yet somehow still wrong.",
        ),
    ),
    _p(
        "gym",
        r"\b(gym|leg day|bench press|deadlift|personal best|protein|gains)\b",
        reactions=("💪", "🏋️", "🥛"),
        replies=(
            "skipping leg day again i see.",
            "protein shake or personality? both, ideally.",
            "you've been 'about to start' for three months.",
            "gains in the gym, losses in the group chat.",
            "the only thing you're lifting is the mood.",
        ),
    ),
    _p(
        "school",
        r"\b(school|class|teacher|exam tomorrow|homework|assignments? due|professor)\b",
        reactions=("📚", "🫡", "💀"),
        replies=(
            "closing discord raises your marks. science.",
            "the assignment is not going to write itself. allegedly.",
            "attendance is 10% of the grade. the other 90 is vibes.",
            "you'll do it at 3am like a real student.",
            "the teacher can smell the last-minute work.",
        ),
    ),
    _p(
        "bored",
        r"\b(i(?:'?m| am) (?:so )?bored|nothing to do|someone talk to me|entertain me)\b",
        reactions=("🫠", "👀", "🎲"),
        replies=(
            "and yet you chose to type into the void. respect.",
            "i'm right here. i'm always right here. that's the problem.",
            "boredom is just your brain asking for a nap.",
            "touch grass. i say this with love, as a bot with no grass.",
            "congratulations, you're now the entertainment.",
        ),
    ),
    _p(
        "birthday",
        r"\b(happy birthday|hbd|bday)\b",
        reactions=("🎂", "🎉", "🥳"),
        replies=(
            "happy birthday. you're one year closer to being the oldest in the call.",
            "another year of this. incredible stamina.",
            "cake now. responsibilities later.",
            "happy birthday, legend. no notes.",
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

#: Emoji used when a message matched nothing at all — the bot stretched,
#: nodded, or laughed. Presence without a paragraph.
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

_MEME_CAPTIONS: tuple[str, ...] = (
    "",
    "",
    "had to immortalize this one 💀",
    "framing this on the server wall",
    "live footage from general chat:",
    "my honest reaction to that information:",
)


class Personality:
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
        self.sticker_picker = StickerPicker(bot)
        #: Internet memes. Keyless; every failure degrades to a rendered card.
        self.meme_feed = meme_feed if meme_feed is not None else MemeFeed(
            sources=tuple(config.meme_sources),
            timeout=float(config.online_timeout),
        )
        #: Reaction GIFs. Silent no-op until a free Tenor/Giphy key is set.
        self.gif_search = gif_search if gif_search is not None else GifSearch(
            tenor_key=config.tenor_api_key,
            giphy_key=config.giphy_api_key,
            timeout=float(config.online_timeout),
        )
        self.roast_intensity = sanitize_intensity(config.roast_intensity)
        # Recency data (all in-memory, all bounded).
        self._channel_activity: dict[int, deque[float]] = {}
        self._last_by_author: dict[int, str] = {}
        # Cooldown clocks (monotonic seconds).
        self._last_global: float = float("-inf")
        self._last_by_channel: dict[int, float] = {}
        self._last_reply_by_channel: dict[int, float] = {}
        self._last_humour_by_channel: dict[int, float] = {}
        self._last_grammar_by_channel: dict[int, float] = {}
        self._last_roast_by_user: dict[int, float] = {}
        self._last_roast_by_channel: dict[int, float] = {}
        #: ``channel -> last action`` so the bot never does the same thing twice
        #: in a row. Two memes back to back reads as a broken bot, not a funny
        #: one, and the variety is most of the charm.
        self._last_action_by_channel: dict[int, str] = {}
        #: ``author_id -> expiry``: people who asked not to be roasted.
        self._roast_optout: dict[int, float] = {}
        # Anti-repetition queues.
        self._recent_replies: deque[str] = deque(maxlen=12)
        self._recent_reactions: deque[str] = deque(maxlen=4)
        self._recent_quips: deque[str] = deque(maxlen=16)
        self._recent_templates: deque[str] = deque(maxlen=3)
        self._recent_nerd_templates: deque[str] = deque(maxlen=6)
        self._recent_roasts: deque[str] = deque(maxlen=24)
        self._recent_comebacks: deque[str] = deque(maxlen=10)
        self._recent_openers: deque[str] = deque(maxlen=10)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def handle(self, message: discord.Message) -> None:
        """One message in — at most one thing out.

        The order matters. Grammar comes first because a correction is a
        *reply to the words*, and it has the narrowest trigger. An open roast
        invitation ("roast me", "1v1 me") comes next, because someone asking
        for it should get an answer even if the channel is mid-conversation.
        Everything else goes through the shared channel cooldowns.
        """
        if not self.config.enabled:
            return
        if not self._eligible(message):
            return

        content = message.content.strip()
        now = time.monotonic()
        self._track(message, now)

        if self._is_spam(message.channel.id, message.author.id, content, now):
            return
        # Real distress or a genuinely heavy topic: Paradise says nothing at
        # all. This check deliberately runs before every joke.
        if _SERIOUS_TOPIC_RE.search(content) or looks_sensitive(content):
            return

        channel_id = message.channel.id

        # Somebody asked for peace. Honour it immediately and silently.
        if _STOP_ROAST_RE.search(content):
            self._roast_optout[message.author.id] = now + _ROAST_OPTOUT_SECONDS
            return

        # 1. English-only nerd grammar/spelling check (has its own cooldown).
        if self.config.grammar_chance > 0 and self._grammar_ready(channel_id, now):
            mistake = detect_english_mistake(content)
            if mistake is not None and random.random() < self.config.grammar_chance:
                if await self._send_grammar_correction(message, mistake, now):
                    return

        # 2. Somebody asked for it. Answer them.
        invitation = invites_roast(content)
        if invitation is not None and self._roast_ready(
            message.author.id, channel_id, now, invited=True
        ):
            if await self._send_invited_roast(message, invitation, now):
                return

        if not self._cooldowns_ready(channel_id, now):
            return

        # 3. Contextual pattern match: one of reply / meme / GIF / sticker /
        #    roast / reaction, never two.
        pattern = self.match(content)
        if pattern is not None:
            if await self._run_pattern_menu(message, pattern, channel_id, now):
                return
            return

        # 4. Unmatched casual messages: memes, stickers, GIFs, quips, roasts
        #    and idle reactions.
        if not self._is_casual_chat(content):
            return
        await self._run_casual_menu(message, channel_id, now)

    # ------------------------------------------------------------------
    # The two menus: what Paradise might do about one message
    # ------------------------------------------------------------------

    async def _run_pattern_menu(
        self,
        message: discord.Message,
        pattern: Pattern,
        channel_id: int,
        now: float,
    ) -> bool:
        roll = random.random()
        cursor = 0.0
        humour_ready = self._humour_ready(channel_id, now)

        if pattern.replies and self._reply_ready(channel_id, now):
            cursor += self.config.reply_chance
            if roll < cursor:
                await self._send_reply(message, pattern, now)
                return True
        else:
            cursor += self.config.reply_chance

        if humour_ready:
            if self._roast_candidate(message, channel_id, now):
                cursor += self._weigh("roast", channel_id, self.config.roast_chance)
                if roll < cursor:
                    return await self._send_roast(message, now)
            if self.config.meme_chance > 0:
                cursor += self._weigh("meme", channel_id, self.config.meme_chance)
                if roll < cursor:
                    return await self._send_meme_reply(message, now)
            if self.config.gif_chance > 0 and self.gif_search.available:
                cursor += self._weigh("gif", channel_id, self.config.gif_chance)
                if roll < cursor:
                    return await self._send_gif_reply(message, now)
            if self.config.sticker_chance > 0:
                cursor += self._weigh("sticker", channel_id, self.config.sticker_chance)
                if roll < cursor:
                    return await self._send_sticker_reply(message, now)

        if pattern.reactions and roll < cursor + self.config.reaction_chance:
            await self._send_reaction(message, pattern, now)
            return True
        return False

    async def _run_casual_menu(
        self, message: discord.Message, channel_id: int, now: float
    ) -> None:
        humour_ready = self._humour_ready(channel_id, now)
        if not humour_ready:
            # Outside the humour cooldown a bare reaction is still on the table:
            # it is the cheapest way for the bot to feel present.
            if self.config.ambient_reaction_chance > 0 and random.random() < (
                self.config.ambient_reaction_chance
            ):
                await self._send_ambient_reaction(message, now)
            return

        segments: list[tuple[float, str]] = []

        def add(action: str, weight: float) -> None:
            if weight > 0:
                segments.append((self._weigh(action, channel_id, weight), action))

        add("meme", self.config.meme_chance)
        add("gif", self.config.gif_chance if self.gif_search.available else 0.0)
        add("sticker", self.config.sticker_chance)
        add("quip", self.config.random_quip_chance)
        if self._roast_candidate(message, channel_id, now):
            add("roast", self.config.roast_chance)
        add("reaction", self.config.ambient_reaction_chance)

        roll = random.random()
        cursor = 0.0
        for weight, action in segments:
            cursor += weight
            if roll >= cursor:
                continue
            if action == "meme":
                if await self._send_meme_reply(message, now):
                    return
            elif action == "gif":
                if await self._send_gif_reply(message, now):
                    return
            elif action == "sticker":
                if await self._send_sticker_reply(message, now):
                    return
            elif action == "quip":
                if await self._send_chaos_quip(message, now):
                    return
            elif action == "roast":
                if await self._send_roast(message, now):
                    return
            elif action == "reaction":
                if await self._send_ambient_reaction(message, now):
                    return
            return

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

    async def _send_reaction(
        self, message: discord.Message, pattern: Pattern, now: float
    ) -> None:
        emoji = self._pick(pattern.reactions, self._recent_reactions)
        try:
            await asyncio.wait_for(message.add_reaction(emoji), timeout=8)
        except (discord.DiscordException, asyncio.TimeoutError):
            log.debug("Personality reaction failed", exc_info=True)
            return
        self._note_action("reaction", message.channel.id)
        self._mark(message.channel.id, now)

    async def _send_ambient_reaction(
        self, message: discord.Message, now: float
    ) -> bool:
        """React to a message that matched nothing at all. Pure presence."""
        emoji = random.choice(AMBIENT_REACTIONS)
        try:
            await asyncio.wait_for(message.add_reaction(emoji), timeout=8)
        except (discord.DiscordException, asyncio.TimeoutError):
            log.debug("Ambient reaction failed", exc_info=True)
            return False
        self._note_action("reaction", message.channel.id)
        self._mark(message.channel.id, now)
        return True

    async def _send_reply(
        self, message: discord.Message, pattern: Pattern, now: float
    ) -> None:
        reply = self._pick(pattern.replies, self._recent_replies)
        if await self._deliver_text_reply(message, reply):
            self._note_action("reply", message.channel.id)
            self._mark(message.channel.id, now)
            self._last_reply_by_channel[message.channel.id] = now
            self._prune(now)

    async def _send_chaos_quip(self, message: discord.Message, now: float) -> bool:
        quip = self._pick(CHAOS_QUIPS, self._recent_quips)
        if not await self._deliver_text_reply(message, quip):
            return False
        self._note_action("quip", message.channel.id)
        self._mark(message.channel.id, now)
        self._last_humour_by_channel[message.channel.id] = now
        self._prune(now)
        return True

    # -- roasts ---------------------------------------------------------

    def _roast_candidate(
        self, message: discord.Message, channel_id: int, now: float
    ) -> bool:
        """Roasting is possible: enabled, cooled down, and not a bad night."""
        if self.config.roast_chance <= 0:
            return False
        if looks_sensitive(message.content or ""):
            return False
        if self._roast_optout.get(message.author.id, 0.0) > now:
            return False
        return self._roast_ready(message.author.id, channel_id, now)

    #: Weight multiplier for an action that just happened in this channel.
    REPEAT_PENALTY: float = 0.25

    def _weigh(self, action: str, channel_id: int, weight: float) -> float:
        if self._last_action_by_channel.get(channel_id) == action:
            return weight * self.REPEAT_PENALTY
        return weight

    def _note_action(self, action: str, channel_id: int) -> None:
        self._last_action_by_channel[channel_id] = action

    def _roast_ready(
        self,
        author_id: int,
        channel_id: int,
        now: float,
        *,
        invited: bool = False,
    ) -> bool:
        """Per-person and per-channel pacing, so nobody gets bullied by a bot.

        An *invited* roast ("roast me", "1v1 me") skips the channel pacing —
        it was asked for — but never the per-person cap: the person who says
        "roast me" five times in a row gets one roast and then a straight
        "that's all you get".
        """
        last_user = self._last_roast_by_user.get(author_id, float("-inf"))
        if now - last_user < self.config.roast_user_cooldown:
            return False
        if self._roast_optout.get(author_id, 0.0) > now:
            return False
        if not invited:
            last_channel = self._last_roast_by_channel.get(channel_id, float("-inf"))
            if now - last_channel < max(30, self.config.channel_cooldown):
                return False
        return True

    async def _send_roast(self, message: discord.Message, now: float) -> bool:
        author_name = (
            getattr(message.author, "display_name", None)
            or getattr(message.author, "name", None)
            or ""
        )
        line = build_roast(
            author_name, intensity=self.roast_intensity, recent=self._recent_roasts
        )
        if not await self._deliver_text_reply(message, line):
            return False
        self._finish_roast(message, now)
        return True

    async def _send_invited_roast(
        self, message: discord.Message, kind: str, now: float
    ) -> bool:
        """Answer an explicit invitation with the right kind of comeback."""
        if kind == "cocky":
            line = build_comeback(recent=self._recent_comebacks)
        else:
            author_name = (
                getattr(message.author, "display_name", None)
                or getattr(message.author, "name", None)
                or ""
            )
            line = build_roast(
                author_name,
                intensity=self.roast_intensity,
                recent=self._recent_roasts,
            )
        if not await self._deliver_text_reply(message, line):
            return False
        self._finish_roast(message, now)
        return True

    def _finish_roast(self, message: discord.Message, now: float) -> None:
        channel_id = message.channel.id
        self._note_action("roast", channel_id)
        self._mark(channel_id, now)
        self._last_roast_by_user[message.author.id] = now
        self._last_roast_by_channel[channel_id] = now
        self._last_humour_by_channel[channel_id] = now
        self._prune(now)

    # -- memes, GIFs, stickers ------------------------------------------

    async def _send_meme_reply(self, message: discord.Message, now: float) -> bool:
        """A meme about this message: from the internet when possible.

        The internet is never allowed to break the feature — no session, no
        route, a dead API, a bad payload, all of it lands on the locally
        rendered card instead.
        """
        if (
            self.config.online_meme_chance > 0
            and random.random() < self.config.online_meme_chance
        ):
            meme = await self._online_meme()
            if meme is not None and await self._post_online_meme(message, meme):
                self._finish_humour(message, now, "meme")
                return True
        if not await self._post_rendered_meme(message):
            return False
        self._finish_humour(message, now, "meme")
        return True

    #: An automatic reply must not stall the conversation waiting for Reddit.
    #: The cache is warmed at start-up and after every use, so a hit is instant;
    #: a miss gets a short budget and then the locally drawn card wins.
    ONLINE_REPLY_BUDGET: float = 3.0

    async def _online_meme(self) -> OnlineMeme | None:
        budget = min(float(self.config.online_timeout), self.ONLINE_REPLY_BUDGET)
        try:
            return await asyncio.wait_for(self.meme_feed.pick(), timeout=budget)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            log.debug("Online meme unavailable", exc_info=True)
            return None

    async def _post_online_meme(
        self, message: discord.Message, meme: OnlineMeme
    ) -> bool:
        embed = discord.Embed(title=meme.title or None)
        embed.set_image(url=meme.url)
        footer = " · ".join(part for part in ("r/" + meme.source if meme.source else "", meme.page_url) if part)
        if footer:
            embed.set_footer(text=discord.utils.escape_markdown(footer)[:200])
        try:
            await asyncio.wait_for(
                message.reply(
                    embed=embed,
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                ),
                timeout=10,
            )
            return True
        except (discord.DiscordException, asyncio.TimeoutError):
            log.debug("Online meme reply failed", exc_info=True)
            return False

    async def _post_rendered_meme(self, message: discord.Message) -> bool:
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
        except Exception:  # noqa: BLE001 - never let a render break the chat
            log.debug("Rendered meme reply failed", exc_info=True)
            return False
        return True

    async def _send_gif_reply(self, message: discord.Message, now: float) -> bool:
        """A reaction GIF. Only ever runs when a Tenor/Giphy key exists."""
        if not self.gif_search.available:
            return False
        query = self._gif_query(message.content)
        try:
            gif = await asyncio.wait_for(
                self.gif_search.search(query),
                timeout=float(self.config.online_timeout) + 2.0,
            )
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            log.debug("GIF search failed", exc_info=True)
            gif = None
        if gif is None:
            # No GIF today: a meme is a better outcome than silence.
            return await self._send_meme_reply(message, now)
        caption = build_reaction_line(message.content, recent=self._recent_openers)
        try:
            await asyncio.wait_for(
                message.reply(
                    caption,
                    embed=discord.Embed().set_image(url=gif.url),
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                ),
                timeout=10,
            )
        except (discord.DiscordException, asyncio.TimeoutError):
            log.debug("GIF reply failed", exc_info=True)
            return False
        self._finish_humour(message, now, "gif")
        return True

    @staticmethod
    def _gif_query(content: str) -> str:
        """A short, safe search phrase for the GIF provider."""
        words = re.findall(r"[a-zA-Z']+", content or "")
        if not words:
            return "bruh"
        text = " ".join(words[:6]).lower()
        return text[:60] or "bruh"

    async def _send_sticker_reply(self, message: discord.Message, now: float) -> bool:
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
            except Exception:  # noqa: BLE001
                log.debug("Fallback PNG sticker reply failed", exc_info=True)
                return False

        if sent:
            self._finish_humour(message, now, "sticker")
        return sent

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

    def _finish_humour(
        self, message: discord.Message, now: float, action: str = "meme"
    ) -> None:
        self._note_action(action, message.channel.id)
        self._mark(message.channel.id, now)
        self._last_humour_by_channel[message.channel.id] = now
        self._prune(now)

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
            self._last_roast_by_user,
            self._last_roast_by_channel,
        ):
            stale = [key for key, value in bucket.items() if value <= cutoff]
            for key in stale:
                bucket.pop(key, None)
        for channel_id in list(self._last_action_by_channel):
            if self._last_by_channel.get(channel_id, float("-inf")) <= cutoff:
                self._last_action_by_channel.pop(channel_id, None)
        for user_id, expiry in list(self._roast_optout.items()):
            if expiry <= now:
                self._roast_optout.pop(user_id, None)
