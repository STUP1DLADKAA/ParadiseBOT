"""Data models for voice tracking and leaderboard rendering."""

from __future__ import annotations

from dataclasses import dataclass, field
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
    """An ongoing voice connection tracked in memory."""

    guild_id: int
    user_id: int
    channel_id: int
    started_at: datetime
    is_muted: bool = False
    is_deaf: bool = False
    is_afk: bool = False


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
