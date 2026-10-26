"""Data models for voice tracking and leaderboard rendering."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class VoicePeriod(str, Enum):
    TODAY = "today"
    YESTERDAY = "yesterday"
    WEEK = "week"
    MONTH = "month"
    ALL_TIME = "all_time"

    @property
    def label(self) -> str:
        return {
            VoicePeriod.TODAY: "Today",
            VoicePeriod.YESTERDAY: "Yesterday",
            VoicePeriod.WEEK: "This Week",
            VoicePeriod.MONTH: "This Month",
            VoicePeriod.ALL_TIME: "All Time",
        }[self]


@dataclass(slots=True)
class ActiveVoiceSession:
    """An ongoing voice connection tracked in memory.

    ``started_at`` is when the session began. ``credited_until`` is the durable
    checkpoint cursor mirrored from ``voice_active_sessions``: every second up
    to that instant is already committed to ``voice_daily_stats``. The part of
    the session that is still only live is therefore exactly
    ``[credit_from, now)``, which is what :attr:`credit_from` exposes.
    """

    guild_id: int
    user_id: int
    channel_id: int
    started_at: datetime
    is_muted: bool = False
    is_deaf: bool = False
    is_afk: bool = False
    credited_until: datetime | None = None

    @property
    def credit_from(self) -> datetime:
        """Start of the interval that has NOT been credited yet.

        Never before ``started_at``, so an administrative correction that moves
        the start later can shrink the live remainder, and one that moves it
        earlier on an uncheckpointed session is honoured in full.
        """
        if self.credited_until is not None and self.credited_until > self.started_at:
            return self.credited_until
        return self.started_at


@dataclass(slots=True)
class LeaderboardMember:
    """A member entry on the server leaderboard."""

    rank: int
    user_id: int
    display_name: str
    username: str
    avatar_url: str | None
    total_seconds: int
    percentage: float = 0.0
    is_active_now: bool = False

    @property
    def formatted_time(self) -> str:
        return format_duration(self.total_seconds)


# ---------------------------------------------------------------------------
# Persistence records shared by every storage backend (SQLite + Supabase)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class VoiceSessionRecord:
    """A completed voice session as stored historically."""

    id: int
    guild_id: int
    user_id: int
    channel_id: int
    started_at: datetime
    ended_at: datetime
    duration_seconds: int


@dataclass(slots=True)
class DailyUserStat:
    """One (guild, user, day) aggregate row."""

    guild_id: int
    user_id: int
    date_str: str
    duration_seconds: int


@dataclass(slots=True)
class LeaderboardEntry:
    """A ranked leaderboard row (identity + seconds only)."""

    user_id: int
    total_seconds: int
    rank: int = 0


@dataclass(slots=True)
class MemberProfile:
    """Last-known Discord identity for a member, kept across restarts."""

    guild_id: int
    user_id: int
    display_name: str
    username: str
    avatar_url: str | None


@dataclass(slots=True)
class PersistedActiveSession:
    """A voice session that was still in progress when it was persisted.

    ``credited_until`` is the durable checkpoint cursor (nullable; ``None``
    means nothing has been credited for this session yet, so the whole
    ``[started_at, now)`` interval is still live).
    """

    guild_id: int
    user_id: int
    channel_id: int
    started_at: datetime
    last_seen_at: datetime
    is_afk: bool
    credited_until: datetime | None = None


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def format_duration(total_seconds: int) -> str:
    """
    Format duration into clean human readable string, e.g. '4h 32m', '42m', '12s'.
    """
    total_seconds = max(0, int(total_seconds))
    if total_seconds == 0:
        return "0m"

    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60

    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0 or (hours > 0 and seconds > 0):
        parts.append(f"{minutes}m")
    if not parts or (hours == 0 and minutes == 0):
        parts.append(f"{seconds}s")

    return " ".join(parts)


def format_duration_diff(diff_seconds: int) -> str:
    """Format compared with yesterday difference e.g. '+42m', '-15m', 'same as yesterday'."""
    if abs(diff_seconds) < 60:
        return "same as yesterday"
    sign = "+" if diff_seconds > 0 else "-"
    abs_dur = format_duration(abs(diff_seconds))
    return f"{sign}{abs_dur}"
