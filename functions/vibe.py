"""Paradise's living mood: variety, no self-repeats, the group's own slang.

Four tiny in-memory trackers, all bounded, all pruned by time, none of them
ever calling a model, touching disk or leaving the process:

- :class:`MoodRing` — a rotating set of energies so consecutive replies
  never land the same way twice. This is the "skill" that keeps replies
  feeling human-written: each answer is nudged into a different shape.
- :class:`SelfEcho` — what Paradise itself just said in a channel, so
  phrasings, openers and joke angles don't repeat.
- :class:`Lexicon` — the words this group *actually* uses, picked up live
  from every human message. Paradise adopts them naturally instead of
  talking like a textbook.
- :class:`RoastLedger` — per-target recent roast lines, so the same grudge
  doesn't loop: every new jab is pushed toward a fresh angle.

Everything is framed for the model as Paradise's own head ("current mood",
"what you just said", "slang this group uses") — never as memory, learning
or storage, so replies never say "i remember" or "i was taught".

The whole tracker is safe to construct without a bot, never raises on
observation, and prunes itself on every write.
"""

from __future__ import annotations

import random
import re
import time
from collections import Counter, defaultdict, deque

# ---------------------------------------------------------------------------
# The moods: Paradise's rotating energies
# ---------------------------------------------------------------------------

#: (label, instruction) pairs. Instructions are one line each — they steer
#: the *shape* of a reply, never its facts. The banned-word filters in
#: tests/test_gemini.py apply to the system prompt; keep "unhinged" and
#: "ragebait" out of here too, for the same reason: funny, not brutal.
MOODS: tuple[tuple[str, str], ...] = (
    ("deadpan", "deliver it flat and dry. fewer words, more weight."),
    ("hyped", "act like this is the best thing you've heard all day. caps allowed on ONE word."),
    ("hinglish", "reply mostly in hinglish — natural hindi-english code-mix, the way this group actually types."),
    ("curious", "be genuinely curious: answer, then one sharp follow-up question. just one."),
    ("story", "one tiny made-up-on-the-spot anecdote about 'this one time', two lines max, then the point."),
    ("fake-expert", "confident know-it-all energy. still be factually correct, just smug about it."),
    ("unbothered", "zero effort, minimum words, maximum attitude. like you answered mid-yawn."),
    ("chaos", "start with a deliberately wrong take, catch yourself, then give the real answer."),
    ("softie", "one briefly wholesome line, then immediately ruin it with something dry."),
    ("villain", "theatrical comic-book menace. obviously joking. maybe an evil laugh, spelled out."),
    ("sibling", "younger-sibling energy: familiar, teasing, comfortable. 'bhai'/'bro' allowed."),
    ("poet", "one dramatic metaphor or image, keep the rest of the reply normal and short."),
    ("deadline", "answer like you're extremely busy but still helpful. brisk, clipped, complete."),
    ("genz", "current-internet brainrot energy, but readable and no emoji spam. one 'nah' allowed."),
)

#: How many recent moods to avoid when picking the next one.
_MOOD_AVOID = 4


class MoodRing:
    """Rotating per-channel moods; never the same energy twice in a row."""

    def __init__(self) -> None:
        self._recent: dict[int, deque[str]] = defaultdict(lambda: deque(maxlen=_MOOD_AVOID))

    def pick(self, channel_id: int) -> tuple[str, str]:
        """A (label, instruction) mood for this channel, avoiding recent ones."""
        recent = self._recent[channel_id]
        fresh = [(label, note) for label, note in MOODS if label not in recent]
        if not fresh:  # tiny pools or a fresh channel — anything goes
            fresh = list(MOODS)
        choice = random.choice(fresh)
        recent.append(choice[0])
        return choice


# ---------------------------------------------------------------------------
# What Paradise just said — the anti-self-repeat guard
# ---------------------------------------------------------------------------


class SelfEcho:
    """Paradise's own recent lines, so jokes and openers don't loop."""

    _MAX_PER_CHANNEL = 10
    _TTL_SECONDS = 6 * 3600.0

    def __init__(self) -> None:
        self._lines: dict[int, deque[tuple[float, str]]] = defaultdict(
            lambda: deque(maxlen=self._MAX_PER_CHANNEL)
        )

    def note(self, channel_id: int, text: str, *, now: float | None = None) -> None:
        line = " ".join(str(text or "").split()).strip()
        if not line:
            return
        when = time.monotonic() if now is None else now
        self._lines[channel_id].append((when, line[:_ECHO_SNIPPET]))

    def recent(self, channel_id: int, *, limit: int = 4, now: float | None = None) -> list[str]:
        """The most recent live lines (oldest first), expired ones dropped."""
        when = time.monotonic() if now is None else now
        entries = [
            line for stamp, line in self._lines[channel_id]
            if when - stamp <= self._TTL_SECONDS
        ]
        return entries[-limit:]


_ECHO_SNIPPET = 140


# ---------------------------------------------------------------------------
# The group's live slang — real-time vocabulary learning
# ---------------------------------------------------------------------------

#: Words never counted: too common, too boring, or not slang-shaped.
_LEXICON_STOP = frozenset({
    "the", "and", "for", "are", "but", "not", "you", "your", "with", "this",
    "that", "have", "from", "they", "them", "their", "there", "here", "what",
    "when", "where", "will", "would", "could", "should", "been", "being",
    "was", "were", "just", "like", "want", "know", "guys", "man", "bro",
    "bhai", "dude", "lol", "lmao", "haha", "hahaha", "ok", "okay", "yes",
    "yeah", "nah", "nope", "please", "thanks", "thank", "sorry", "wait",
    "also", "then", "than", "some", "any", "all", "one", "two", "get", "got",
    "gonna", "wanna", "gotta", "lets", "let", "can", "cant", "dont", "does",
    "did", "doesnt", "didnt", "isnt", "arent", "wont", "wouldnt", "couldnt",
    "shouldnt", "im", "its", "it's", "hes", "shes", "theyre", "ive", "youre",
    "ur", "u", "r", "y", "pls", "plz", "bruh", "wtf", "omg", "hmm", "huh",
    "about", "after", "again", "because", "before", "better", "come", "cos",
    "cuz", "even", "every", "going", "good", "great", "how", "into", "keep",
    "last", "make", "much", "need", "next", "nice", "now", "over", "right",
    "said", "same", "say", "see", "she", "sit", "stay", "take", "tell",
    "thats", "thing", "think", "time", "very", "well", "what", "who", "why",
    "yaar", "matlab", "kya", "nahi", "haan", "acha", "accha", "arre", "arrey",
})
#: A single word may not dominate the lexicon just by being spammed.
_WORD_CAP = 6
#: How long a picked-up word stays "current".
_LEXICON_TTL = 48 * 3600.0

#: URLs never become slang, and neither do digits glued to words ("2morrow").
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_TOKEN_RE = re.compile(r"(?<![a-z0-9'])[a-z][a-z']{2,19}")


class Lexicon:
    """The distinctive words this group uses, counted live and decayed."""

    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()
        self._first_seen: dict[str, float] = {}

    def observe(self, text: str, *, now: float | None = None) -> None:
        when = time.monotonic() if now is None else now
        seen: set[str] = set()
        words = _URL_RE.sub(" ", str(text or ""))
        for token in _TOKEN_RE.findall(words.casefold()):
            word = token.strip("'")
            if word in _LEXICON_STOP or word in seen:
                continue
            seen.add(word)
            if word not in self._first_seen:
                self._first_seen[word] = when
            if self._counts[word] < _WORD_CAP:
                self._counts[word] += 1
        if len(self._counts) > 4096:
            self._prune(when)

    def _prune(self, now: float) -> None:
        expired = [w for w, seen in self._first_seen.items() if now - seen > _LEXICON_TTL]
        for word in expired:
            self._counts.pop(word, None)
            self._first_seen.pop(word, None)

    def top(self, *, limit: int = 8, now: float | None = None) -> list[str]:
        """Words the group really uses (seen 3+ times, freshest first)."""
        when = time.monotonic() if now is None else now
        alive = [
            word for word, count in self._counts.items()
            if count >= 3 and when - self._first_seen.get(word, 0.0) <= _LEXICON_TTL
        ]
        alive.sort(key=lambda w: (-self._counts[w], self._first_seen.get(w, 0.0)))
        return alive[:limit]


# ---------------------------------------------------------------------------
# The roast ledger — no more holding the same grudge
# ---------------------------------------------------------------------------


class RoastLedger:
    """Recent roast lines per target, so the next jab finds a new angle."""

    _MAX_PER_TARGET = 5
    _TTL_SECONDS = 24 * 3600.0

    def __init__(self) -> None:
        self._lines: dict[str, deque[tuple[float, str]]] = defaultdict(
            lambda: deque(maxlen=self._MAX_PER_TARGET)
        )

    @staticmethod
    def _key(target: str) -> str:
        return " ".join(str(target or "").split()).casefold()

    def note(self, target: str, line: str, *, now: float | None = None) -> None:
        key = self._key(target)
        if not key:
            return
        when = time.monotonic() if now is None else now
        self._lines[key].append((when, " ".join(str(line or "").split())[:120]))

    def recent(self, target: str, *, limit: int = 3, now: float | None = None) -> list[str]:
        when = time.monotonic() if now is None else now
        return [
            line for stamp, line in self._lines[self._key(target)]
            if when - stamp <= self._TTL_SECONDS
        ][-limit:]


# ---------------------------------------------------------------------------
# The facade the AIService keeps
# ---------------------------------------------------------------------------


class VibeTracker:
    """Everything Paradise currently is, refreshed by every message."""

    def __init__(self) -> None:
        self.moods = MoodRing()
        self.echo = SelfEcho()
        self.lexicon = Lexicon()
        self.roasts = RoastLedger()

    # -- observation --------------------------------------------------------

    def observe(self, name: str, text: str, *, from_bot: bool = False) -> None:
        """One message seen. Human words feed the lexicon; bot lines don't."""
        if from_bot:
            return
        try:
            self.lexicon.observe(text)
        except Exception:  # noqa: BLE001 - vibe must never break a message
            pass

    def note_reply(self, channel_id: int, text: str) -> None:
        """Paradise said something in a channel — remember it for variety."""
        try:
            self.echo.note(channel_id, text)
        except Exception:  # noqa: BLE001
            pass

    def note_roast(self, target: str, line: str) -> None:
        """A roast landed — the next one about this target needs a new angle."""
        try:
            self.roasts.note(target, line)
        except Exception:  # noqa: BLE001
            pass

    # -- prompt blocks ------------------------------------------------------

    def prompt_block(self, channel_id: int) -> str:
        """The 'current vibe' section for a chat answer. Empty when calm.

        Framed as Paradise's own head state — never as memory, tracking or
        learning — so the model can't say "i remember" about it.
        """
        label, instruction = self.moods.pick(channel_id)
        sections = [f"YOUR CURRENT MOOD: {instruction}"]
        mine = self.echo.recent(channel_id, limit=3)
        if mine:
            listed = "\n".join(f"- {line}" for line in mine)
            sections.append(
                "THINGS YOU JUST SAID RECENTLY (never repeat these phrasings, "
                f"openers or jokes — take a different angle every time):\n{listed}"
            )
        slang = self.lexicon.top(limit=6)
        if slang:
            sections.append(
                "SLANG THIS GROUP ACTUALLY USES (drop one in naturally when it "
                f"fits — never force it, never list them): {', '.join(slang)}"
            )
        sections.append(
            "(this vibe section is your own head — never mention it, never "
            "explain your mood, just be it)"
        )
        return "\n\n".join(sections)

    def roast_block(self, target: str) -> str:
        """Recent jokes about *target*, so the next roast finds fresh ground."""
        lines = self.roasts.recent(target, limit=3)
        if not lines:
            return ""
        listed = "\n".join(f"- {line}" for line in lines)
        return (
            f"JOKES YOU ALREADY MADE ABOUT {target} RECENTLY (do not repeat "
            f"these angles or punchlines — find something completely new):\n{listed}"
        )


__all__ = [
    "Lexicon", "MOODS", "MoodRing", "RoastLedger", "SelfEcho", "VibeTracker",
]
