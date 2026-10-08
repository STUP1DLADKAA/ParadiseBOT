"""Voice tracking, statistics, and leaderboard card rendering package."""

from .database import VoiceDatabase, split_session_by_midnight
from .models import (
    ActiveVoiceSession,
    DailyUserStat,
    LeaderboardEntry,
    LeaderboardMember,
    MemberProfile,
    PersistedActiveSession,
    VoicePeriod,
    VoiceSessionRecord,
    format_duration,
    format_duration_diff,
)
from .render import LeaderboardImageRenderer
from .store import (
    VoiceSchemaError,
    VoiceStore,
    VoiceStoreError,
    VoiceStoreUnavailable,
)
from .sqlite_store import LocalVoiceStore
from .supabase_database import SupabaseVoiceDatabase
from .tracker import VoiceTracker
from .ui import (
    LeaderboardView,
    build_leaderboard_text_embed,
    build_user_stats_embed,
)

__all__ = [
    "ActiveVoiceSession",
    "DailyUserStat",
    "LeaderboardEntry",
    "LeaderboardMember",
    "MemberProfile",
    "PersistedActiveSession",
    "VoiceDatabase",
    "VoicePeriod",
    "VoiceSessionRecord",
    "VoiceSchemaError",
    "VoiceStore",
    "VoiceStoreError",
    "VoiceStoreUnavailable",
    "LocalVoiceStore",
    "SupabaseVoiceDatabase",
    "VoiceTracker",
    "LeaderboardImageRenderer",
    "LeaderboardView",
    "build_leaderboard_text_embed",
    "build_user_stats_embed",
    "format_duration",
    "format_duration_diff",
    "split_session_by_midnight",
]
