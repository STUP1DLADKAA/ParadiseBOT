"""The aliveness engine — Paradise checking whether its own server is breathing.

Every couple of minutes (:class:`commands.aliveness.Alive`) the bot looks
around and collects the signals a human member would subconsciously notice:
what time it is, how long since anybody typed anything, who spoke recently and
where they went, who is sitting in a voice channel and for how long, and how
many members are online. When the picture says "this place is dead" or
"everyone moved into the voice call", the engine hands the raw data to the AI
and posts one short in-character line about *that exact situation* — so the
bot feels like a member who noticed, not a nagging announcement bot.

Everything Discord-shaped is gathered in :meth:`AlivenessEngine.snapshot`;
everything after that — the verdict, the prompt, the fallback line — is pure
and unit-tested without a bot, a guild or a network.

Quiet hours are IST (the server's home timezone) and the "no message yet"
case is handled conservatively: until the bot has been online for
``dead_after`` minutes it will not call a server dead, because silence that
started before login is invisible to it.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

__all__ = [
    "AlivenessEngine",
    "AlivenessSignals",
    "DEAD_REASON",
    "EVERYONE_IN_VC_REASON",
    "IST",
    "format_duration",
]

log = logging.getLogger(__name__)

#: The server's home timezone.
IST = ZoneInfo("Asia/Kolkata")

#: Verdict: nobody has typed for ``dead_after`` minutes.
DEAD_REASON = "dead"
#: Verdict: everyone who was chatting is now in a voice call instead.
EVERYONE_IN_VC_REASON = "everyone_in_vc"

#: Canned lines for when the AI is unavailable. Situation-specific buckets;
#: ``{minutes}`` is filled from the signals.
DEAD_LINES: tuple[str, ...] = (
    "it's been {minutes} minutes. i've been counting. someone say something.",
    "hello? anyone? the crickets in here have unionised.",
    "{minutes} minutes of pure silence. i can hear my own cooldowns.",
    "dead server hours. blink twice if you're alive.",
    "i've been standing here alone for {minutes} minutes and nobody even checked on me.",
)
VC_LINES: tuple[str, ...] = (
    "everyone's in the voice call and i'm stuck babysitting an empty text channel. cool. cool cool cool.",
    "i can hear you all laughing in vc from here. not bitter at all.",
    "the whole server moved to vc and left me here with the crickets.",
    "you do realise i can't join the fun, right? enjoy your voice call. i'll be here. waiting.",
)


def format_duration(seconds: float) -> str:
    """Human minutes/hours for "how long has this person been in VC"."""
    total = max(0, int(seconds))
    if total < 60:
        return "just joined"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, leftover = divmod(minutes, 60)
    if leftover:
        return f"{hours}h {leftover}m"
    return f"{hours}h"


@dataclass(frozen=True, slots=True)
class AlivenessSignals:
    """One snapshot of the server, taken entirely at one instant."""

    #: Wall-clock "now" in IST — the hour drives quiet hours and the prompt.
    ist_now: datetime
    #: Seconds since the last human message (or since the bot came online).
    chat_silent_seconds: float
    #: How many distinct humans spoke within the recent window.
    chatters_active: int
    #: Display names of those recent chatters (bounded).
    chatter_names: tuple[str, ...]
    #: Humans currently in a voice channel.
    vc_humans: int
    #: Per-member detail: "Rohit — 43m in General".
    vc_detail: tuple[str, ...]
    #: True when every recent chatter is in a voice call (and enough are).
    everyone_in_vc: bool
    #: Total member count of the guild.
    member_count: int
    #: Humans not offline — ``None`` when the presence intent is disabled.
    online_count: int | None
    #: Which verdict these signals represent (set by :meth:`decision`).
    reason: str = ""


@dataclass
class _GuildState:
    """Per-guild tracking (the engine never assumes a single guild)."""

    #: monotonic timestamp of the last human message seen (None until one is).
    last_chat: float | None = None
    #: author id -> monotonic timestamp of their last message.
    chatters: dict[int, float] = field(default_factory=dict)
    #: True once the first loop pass has tried to seed from channel history.
    primed: bool = False


class AlivenessEngine:
    """Collects the signals and decides whether Paradise should say something."""

    def __init__(
        self,
        config: Any,
        *,
        clock: Callable[[], float] = time.monotonic,
        now_fn: Callable[[], datetime] | None = None,
        presences_enabled: bool = False,
    ) -> None:
        self.config = config
        self._clock = clock
        self._now_fn = now_fn or (lambda: datetime.now(IST))
        #: Whether the presence intent is on (see bot.py: off by default).
        self.presences_enabled = bool(presences_enabled)
        #: When the engine started watching (monotonic).
        self._started = clock()
        self._guilds: dict[int, _GuildState] = {}
        #: Monotonic timestamp of the last posted aliveness line.
        self._last_post: float | None = None

    # -- chat tracking ----------------------------------------------------

    def note_message(self, message: Any) -> None:
        """Record that a human just spoke (called from on_message)."""
        author = getattr(message, "author", None)
        if author is None or getattr(author, "bot", False):
            return
        if getattr(message, "guild", None) is None:
            return
        content = str(getattr(message, "content", "") or "")
        if not content.strip():
            return
        now = self._clock()
        guild_id = int(message.guild.id)
        state = self._guilds.setdefault(guild_id, _GuildState())
        state.last_chat = now
        state.chatters[int(author.id)] = now

    def prime(self, guild_id: int, last_message_at: float | None) -> None:
        """Seed "last chat" from channel history on the first pass."""
        state = self._guilds.setdefault(guild_id, _GuildState())
        state.primed = True
        if last_message_at is not None and (
            state.last_chat is None or last_message_at > state.last_chat
        ):
            state.last_chat = last_message_at

    def is_primed(self, guild_id: int) -> bool:
        """True once the first loop pass has seeded this guild."""
        state = self._guilds.get(guild_id)
        return bool(state and state.primed)

    def reset(self) -> None:
        """Forget all tracking (used by tests and hot reloads)."""
        self._guilds.clear()
        self._last_post = None
        self._started = self._clock()

    # -- signals ----------------------------------------------------------

    def _config_value(self, name: str, default: float) -> float:
        try:
            return float(getattr(self.config, name))
        except (AttributeError, TypeError, ValueError):
            return default

    def snapshot(self, guild: Any) -> AlivenessSignals:
        """Gather everything observable about ``guild`` right now.

        Written against duck typing: ``guild`` only needs ``id``,
        ``voice_states``, ``members``, ``member_count`` and optionally
        ``afk_channel_id`` — which is exactly what a real
        :class:`discord.Guild` provides and what a test double can fake.
        """
        now = self._clock()
        ist_now = self._now_fn()
        guild_id = int(getattr(guild, "id", 0) or 0)
        state = self._guilds.setdefault(guild_id, _GuildState())
        window = self._config_value("recent_window", 120.0) * 60.0

        # --- chat side ---------------------------------------------------
        # Prune stale chatter timestamps while we have the state handy.
        recent_ids = {
            author_id: stamp
            for author_id, stamp in state.chatters.items()
            if now - stamp <= window
        }
        state.chatters = recent_ids
        reference = state.last_chat if state.last_chat is not None else self._started
        chat_silent = max(0.0, now - reference)

        # --- voice side ----------------------------------------------------
        vc_members: dict[int, tuple[str, str, float]] = {}
        afk_id = getattr(guild, "afk_channel_id", None)
        for member, voice_state in dict(getattr(guild, "voice_states", {}) or {}).items():
            if getattr(member, "bot", False):
                continue
            channel = getattr(voice_state, "channel", None)
            if channel is None:
                continue
            channel_id = getattr(channel, "id", None)
            if channel_id is not None and channel_id == afk_id:
                continue
            joined_at = getattr(voice_state, "joined_at", None)
            duration = 0.0
            if joined_at is not None:
                try:
                    delta = datetime.now(timezone.utc) - joined_at
                    duration = max(0.0, delta.total_seconds())
                except (TypeError, ValueError):
                    duration = 0.0
            member_id = int(getattr(member, "id", 0) or 0)
            name = (
                getattr(member, "display_name", None)
                or getattr(member, "name", None)
                or "someone"
            )[:32]
            channel_name = getattr(channel, "name", None) or "voice"
            vc_members[member_id] = (name, str(channel_name), duration)

        vc_detail = sorted(
            f"{name} — {format_duration(duration)} in {channel}"
            for name, channel, duration in vc_members.values()
        )

        # Is everyone who was chatting now in a voice call?
        min_group = int(self._config_value("min_vc_group", 2.0))
        everyone_in_vc = (
            len(recent_ids) >= min_group
            and bool(vc_members)
            and all(author_id in vc_members for author_id in recent_ids)
        )

        # Chatter names for the prompt (bounded, no order guaranteed).
        names_by_id: dict[int, str] = {}
        for member in getattr(guild, "members", []) or []:
            author_id = int(getattr(member, "id", 0) or 0)
            if author_id in recent_ids:
                names_by_id[author_id] = (
                    getattr(member, "display_name", None)
                    or getattr(member, "name", None)
                    or "someone"
                )[:32]
        chatter_names = tuple(
            names_by_id.get(author_id, "someone") for author_id in recent_ids
        )[:12]

        # Online count needs the presence intent; without it, honestly say so.
        online_count: int | None = None
        members = getattr(guild, "members", []) or []
        if getattr(self, "presences_enabled", False):
            online_count = sum(
                1
                for member in members
                if not getattr(member, "bot", False)
                and str(getattr(member, "status", "offline")) != "offline"
            )

        member_count = int(getattr(guild, "member_count", 0) or len(members) or 0)
        return AlivenessSignals(
            ist_now=ist_now,
            chat_silent_seconds=chat_silent,
            chatters_active=len(recent_ids),
            chatter_names=chatter_names,
            vc_humans=len(vc_members),
            vc_detail=tuple(vc_detail),
            everyone_in_vc=everyone_in_vc,
            member_count=member_count,
            online_count=online_count,
        )

    # -- verdict ----------------------------------------------------------

    def in_quiet_hours(self, ist_now: datetime | None = None) -> bool:
        """True between the configured IST quiet hours (let them sleep)."""
        start, end = getattr(self.config, "quiet_hours", (1, 7))
        if start < 0 or end < 0 or start == end:
            return False
        hour = (ist_now or self._now_fn()).hour
        if start <= end:
            return start <= hour < end
        return hour >= start or hour < end  # wraps midnight, e.g. 23-6

    def decision(self, signals: AlivenessSignals) -> str | None:
        """Return the verdict for these signals, or ``None`` to stay quiet.

        Marks the post cooldown the moment it returns a reason, so the caller
        cannot accidentally double-post by asking twice.
        """
        now = self._clock()
        if self._last_post is not None:
            cooldown = self._config_value("post_cooldown", 45.0) * 60.0
            if now - self._last_post < cooldown:
                return None
        if self.in_quiet_hours(signals.ist_now):
            return None
        dead_after = self._config_value("dead_after", 25.0) * 60.0
        vc_quiet = self._config_value("vc_quiet", 10.0) * 60.0
        min_group = int(self._config_value("min_vc_group", 2.0))

        if (
            signals.everyone_in_vc
            and signals.chatters_active >= min_group
            and signals.chat_silent_seconds >= vc_quiet
        ):
            self._last_post = now
            return EVERYONE_IN_VC_REASON
        if signals.chat_silent_seconds >= dead_after:
            # Note: a live voice call does NOT stop this branch — a chat that
            # went silent while people are in a call is worth a line too, and
            # the prompt carries the VC data so the AI can use it.
            self._last_post = now
            return DEAD_REASON
        return None

    # -- what to say ------------------------------------------------------

    def build_prompt(self, signals: AlivenessSignals, reason: str) -> str:
        """Turn the raw signals into the AI's job description for one line.

        Pure: everything quoted comes from the snapshot, formatted plainly,
        so the model sees the same picture a member would.
        """
        minutes = int(signals.chat_silent_seconds // 60)
        when = signals.ist_now.strftime("%H:%M, %A")
        lines = [
            f"It is {when} IST. You just checked on your own Discord server and it looks {('dead quiet' if reason == DEAD_REASON else 'empty because everyone is in a voice call')}.",
            f"- last human message in chat: {minutes} minute(s) ago"
            if signals.chat_silent_seconds > 0
            else "- people were chatting seconds ago",
            f"- members total: {signals.member_count}",
        ]
        if signals.online_count is not None:
            lines.append(f"- members online right now: {signals.online_count}")
        else:
            lines.append("- members online right now: unknown (you can't see statuses)")
        if signals.vc_detail:
            lines.append(
                "- in voice chat: " + "; ".join(signals.vc_detail[:10])
            )
        else:
            lines.append("- in voice chat: nobody")
        if signals.chatter_names:
            lines.append(
                "- people who spoke in the last couple of hours: "
                + ", ".join(signals.chatter_names[:10])
            )
        else:
            lines.append("- people who spoke in the last couple of hours: nobody")
        lines.append(
            "Write ONE short message (1-2 sentences, under 30 words) to post in "
            "the chat to make the server feel alive again — a funny, light "
            "observation about exactly this situation, in your usual casual "
            "voice. Reference the real details above (minutes, names, the voice "
            "call). No mentions, no hashtags, at most one emoji."
        )
        return "\n".join(lines)

    def fallback_line(self, signals: AlivenessSignals, reason: str) -> str:
        """A canned line when the AI is unavailable — still situational."""
        minutes = max(1, int(signals.chat_silent_seconds // 60))
        if reason == EVERYONE_IN_VC_REASON or signals.vc_humans > 0:
            return random.choice(VC_LINES)
        return random.choice(DEAD_LINES).format(minutes=minutes)
