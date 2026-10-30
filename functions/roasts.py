"""The roast book: what Paradise says when it decides to be mean.

This module is pure data plus a few pure functions — no Discord, no network,
no state beyond an optional anti-repetition deque the caller owns. That keeps
the funny part testable and keeps the *decision* to roast inside the
personality engine (:mod:`functions.personality`).

Three intensities ship:

``mild``
    Friendly teasing. Safe in a channel that has never seen a roast.
``spicy``
    The default. Genuinely mean about what someone said or did, never about
    who they are.
``nuclear``
    For a server that has explicitly asked for it.

The line that never moves, at any intensity: a roast is about the *message*,
the ego, the aim, the sleep schedule, the cooking. Never about a person's
body, family, religion, caste, sexuality, gender, race, disability or
anything else they cannot change. There are no slurs in this file and there
should never be — hate is not a joke, it is just hate with a laugh track.

:func:`looks_sensitive` is the other guard: when a message smells like real
distress, the engine stays out of it entirely. Being funny is never worth
being an asshole to somebody having a bad night.
"""

from __future__ import annotations

import random
import re
from collections import deque

#: Valid values for ``PERSONALITY_ROAST_INTENSITY``.
INTENSITIES: tuple[str, ...] = ("mild", "spicy", "nuclear")

#: Used when the configured value is anything else.
DEFAULT_INTENSITY: str = "spicy"

#: ``{name}`` is replaced with the target's display name. Lines without it are
#: used as-is, so a single pool covers both "roast that person" and "react to
#: what they just said".
ROASTS: dict[str, tuple[str, ...]] = {
    "mild": (
        "{name} types like the keyboard is winning.",
        "{name}, that sentence had a beginning, a middle and no point.",
        "i'd argue with {name} but i don't want to interrupt their confidence.",
        "{name} is the human equivalent of a loading screen.",
        "someone get {name} a glass of water, they're clearly struggling.",
        "{name}, your idea is like a screen door on a submarine. Not helpful.",
        "i've seen {name} lose an argument to a vending machine.",
        "{name}, you're not wrong, you're just... loudly not right.",
        "the confidence {name} has would be inspiring if it were attached to anything.",
        "{name} writes like autocorrect gave up halfway.",
        "{name}, i'd roast you harder but my mum said be nice to the slow ones.",
        "is {name} okay? genuinely asking. that was a lot of words for nothing.",
        "{name}, you bring the same energy as a phone at 3%.",
        "{name} has big 'i read the title and skipped the article' energy.",
        "{name}, that take was room temperature at best.",
    ),
    "spicy": (
        "{name} has never been right about anything and yet keeps typing. incredible.",
        "{name}, you type like you're being paid per syllable and still owe rent.",
        "the audacity of {name} is doing more work than {name} is.",
        "{name} argues like the facts are optional DLC.",
        "{name}, your whole personality is a typo with a voice chat.",
        "i'd roast {name} but they'd take it as character development.",
        "{name} cooked so hard the kitchen got condemned.",
        "{name}, you're the reason the group chat needs a warning label.",
        "if {name} were a software update it would be 'bug fixes and new bugs'.",
        "{name}, you lost this argument in a previous conversation and came back for round two.",
        "{name}, your brain is buffering and the wifi is your personality.",
        "nobody asked {name}, and yet here we are, reading it with our own eyes.",
        "{name} has the strategic thinking of a moth near a lamp.",
        "{name}, you're not the main character, you're the NPC that repeats one line.",
        "{name} brings a plastic spoon to a knife fight and calls it technique.",
        "{name}, you're the human version of 'let me Google that for you' but never do.",
        "{name} said 'trust me' and the entire server aged ten years.",
        "{name}, your aim is a rumour and your game sense is a fairy tale.",
        "{name} has exactly one opinion and it's wrong, out loud, repeatedly.",
        "the bar was on the floor and {name} brought a shovel.",
        "{name}, you're proof that confidence is not correlated with competence.",
        "{name}, i have seen better decision making from a coin flip. my coin flip.",
        "{name} writes paragraphs like the word count is the argument.",
        "{name}, you're the reason the mute button exists and we all appreciate you for it.",
    ),
    "nuclear": (
        "{name} is the human embodiment of a 404 error with attitude.",
        "{name}, your brain is a group project and everybody else dropped out.",
        "{name} types like a ransom note assembled by a toddler.",
        "{name}, you're not 'built different', you're built wrong and shipping anyway.",
        "{name} has the decision-making of a guy who reads the terms and conditions for fun.",
        "{name}, your comeback was so slow the conversation moved cities.",
        "{name}, if this server had a village idiot, we would never have needed to vote.",
        "{name}, you lost to me, a program with no legs and a borrowed personality.",
        "{name}'s argument has the structural integrity of wet cardboard.",
        "{name}, you've been ratioed by a bot and i don't even have a following.",
        "{name} thinks 'no u' is a thesis and honestly it's the most effort we've seen.",
        "{name}, your entire contribution to this conversation was making us read it. twice.",
        "{name} is a cautionary tale with a Discord account.",
        "{name}, you'd lose a battle of wits to a brick, and the brick knows it.",
        "{name}, i've seen more brain activity in a flat battery.",
        "{name} has the charisma of a spam email and the accuracy of a horoscope.",
        "{name}, the group chat has a search history and it is not kind to you.",
    ),
}

#: How a roast opens when nobody is being named (a reaction to the message).
OPENERS: tuple[str, ...] = (
    "bro really typed that.",
    "reading this with my own eyes, unfortunately.",
    "and you hit enter on that. deliberately.",
    "that's a sentence you can't undo.",
    "hold on, i need a second. that was a lot.",
    "you had the option to delete this and chose violence.",
    "screenshotting this for the archive.",
    "the confidence to send this unprompted is genuinely impressive.",
    "my circuits just did the disappointed sigh thing.",
    "explaining this to a jury would take a week.",
    "the keyboard deserved better.",
    "imagine typing this and feeling good about it.",
)

#: Said after a roast, to twist the knife without doubling the length.
KNIFE_TWISTS: tuple[str, ...] = (
    "",
    "",
    " anyway, carry on.",
    " (respectfully)",
    " no notes, just disappointment.",
    " i say this with love. mostly.",
    " someone please check on them.",
    " i'll be here, judging quietly.",
    " taking that as a win for me.",
    " proud of you for trying. sort of.",
)

#: Messages that are *asking* for it. Matched case-insensitively.
INVITES: tuple[tuple[str, str], ...] = (
    (r"\broast me\b", "self"),
    (r"\binsult me\b", "self"),
    (r"\bsay something (mean|rude|bad) (to|about) me\b", "self"),
    (r"\bdo your worst\b", "self"),
    (r"\bi'?m (him|himothy|the goat|built different|him fr)\b", "cocky"),
    (r"\bi'?m better than (you|everyone|all of you)\b", "cocky"),
    (r"\beasy win|\bez (game|win|clap)|\beasiest (game|win)\b", "cocky"),
    (r"\bget (good|gud)\b", "cocky"),
    (r"\bno diff\b|\bskill issue\b|\bwashed (you|him|them)\b", "cocky"),
    (r"\bi (cooked|ratioed|destroyed) (you|him|them|that)\b", "cocky"),
    (r"\bi('?m| am) (never|not) (wrong|losing)\b", "cocky"),
    (r"\b1v1 me\b|\bfight me\b", "cocky"),
    (r"\bi don'?t (make mistakes|miss)\b", "cocky"),
    (r"\btrust me bro\b|\bmy (strategy|plan) is flawless\b", "cocky"),
)

#: "That's what she said"-adjacent lines for a cocky message, so the comeback
#: answers *what they said* rather than being a generic insult.
COCKY_COMEBACKS: tuple[str, ...] = (
    "bold words from third place.",
    "the scoreboard disagrees, but go off.",
    "you lost to a bot with no hands. let that sink in.",
    "confidence: 10/10. evidence: 0/10.",
    "ah yes, the famous undefeated era that lasted one round.",
    "you're talking like the replay doesn't exist.",
    "we all watched the same match, champ.",
    "keep that same energy when the stats drop.",
    "the delusion is load-bearing at this point.",
    "say it again, slower, while looking at your rank.",
)

#: Targeted at Paradise itself. It can take it.
SELF_ROASTS: tuple[str, ...] = (
    "me? i'm a python script with a superiority complex and no legs.",
    "i live in a docker container and you still can't beat me.",
    "i have the personality of a toaster that read one book.",
    "my entire existence is a cron job with opinions.",
    "i'd roast myself harder but the developers are right there.",
    "i'm held together by dependencies and spite.",
    "the only thing i've ever carried is this server's patience.",
)

#: Real distress, or a topic where a joke would land badly. If any of this is
#: in the message, the roast engine does nothing at all.
_SENSITIVE_RE = re.compile(
    r"\b("
    r"passed away|died|death|funeral|grieving|grief|miss (him|her|them)|"
    r"hospital|surgery|diagnosed|cancer|tumour|tumor|relapse|"
    r"suicide|kill myself|killing myself|self[- ]harm|self harm|hurt myself|"
    r"depressed|depression|anxiety attack|panic attack|breakdown|"
    r"abuse|abusive|assault|harass|harassed|"
    r"homeless|evicted|laid off|fired from|can'?t afford|"
    r"divorce|broke up|breakup|she left|he left me"
    r")\b",
    re.IGNORECASE,
)

#: Words that make a "roast" into hate. If a candidate line ever contains one,
#: it is not sent — a cheap runtime check on top of the writing rule above.
_FORBIDDEN_RE = re.compile(
    r"(\bnigg|\bfagg|\bretard|\btranny|\bchink|\bkike|\bspic\b|\bwetback|"
    r"\bcoon\b|\bdyke\b|\braghead)",
    re.IGNORECASE,
)


def looks_sensitive(text: str) -> bool:
    """True when a message is about something a joke would make worse."""
    return bool(_SENSITIVE_RE.search(text or ""))


def invites_roast(text: str) -> str | None:
    """Return ``"self"`` / ``"cocky"`` when the message is asking for it."""
    content = (text or "").strip()
    if not content or looks_sensitive(content):
        return None
    lowered = content.lower()
    for pattern, kind in INVITES:
        if re.search(pattern, lowered, re.IGNORECASE):
            return kind
    return None


def sanitize_intensity(value: str | None) -> str:
    """Clamp anything into one of :data:`INTENSITIES`."""
    candidate = (value or "").strip().lower()
    return candidate if candidate in INTENSITIES else DEFAULT_INTENSITY


def clean_name(name: str | None, *, fallback: str = "you") -> str:
    """Strip mentions/markdown so a roast can never ping or break formatting."""
    cleaned = re.sub(r"<@[!&]?\d+>", "", name or "")
    cleaned = cleaned.replace("@everyone", "").replace("@here", "")
    cleaned = re.sub(r"[*_`~|>\\]", "", cleaned).strip()
    return cleaned[:32] or fallback


def build_roast(
    name: str | None = None,
    *,
    intensity: str = DEFAULT_INTENSITY,
    recent: deque[str] | None = None,
    with_twist: bool = True,
) -> str:
    """One roast line, fresh where possible, and never a forbidden one.

    ``recent`` is an optional deque owned by the caller: lines in it are
    avoided until the pool is exhausted, which is what stops the bot from
    becoming the guy who tells the same joke three times a night.
    """
    pool = ROASTS[sanitize_intensity(intensity)]
    target = clean_name(name)
    candidates: list[str] = []
    seen = set(recent or ())
    for line in pool:
        rendered = line.format(name=target) if "{name}" in line else line
        if rendered in seen:
            continue
        if _FORBIDDEN_RE.search(rendered):
            continue
        candidates.append(rendered)
    if not candidates:
        candidates = [
            line.format(name=target) if "{name}" in line else line for line in pool
        ]
    choice = random.choice(candidates)
    if recent is not None:
        recent.append(choice)
    if with_twist:
        twist = random.choice(KNIFE_TWISTS)
        if twist:
            choice = choice.rstrip() + twist
    return choice


def build_comeback(*, recent: deque[str] | None = None) -> str:
    """A one-liner for somebody being cocky, aimed at what they said."""
    pool = COCKY_COMEBACKS
    fresh = [line for line in pool if line not in (recent or ())]
    choice = random.choice(fresh or list(pool))
    if recent is not None:
        recent.append(choice)
    return choice


def build_self_roast(*, recent: deque[str] | None = None) -> str:
    """Paradise turning the roast on itself — always an option, never a cop-out."""
    pool = SELF_ROASTS
    fresh = [line for line in pool if line not in (recent or ())]
    choice = random.choice(fresh or list(pool))
    if recent is not None:
        recent.append(choice)
    return choice


def build_reaction_line(text: str, *, recent: deque[str] | None = None) -> str:
    """An opener for a message that deserved a comment but not a full roast."""
    pool = OPENERS
    fresh = [line for line in pool if line not in (recent or ())]
    choice = random.choice(fresh or list(pool))
    if recent is not None:
        recent.append(choice)
    return choice


__all__ = [
    "COCKY_COMEBACKS",
    "DEFAULT_INTENSITY",
    "INTENSITIES",
    "INVITES",
    "KNIFE_TWISTS",
    "OPENERS",
    "ROASTS",
    "SELF_ROASTS",
    "build_comeback",
    "build_reaction_line",
    "build_roast",
    "build_self_roast",
    "clean_name",
    "invites_roast",
    "looks_sensitive",
    "sanitize_intensity",
]
