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
from .tracker import (
    DEAF_TIME_EXEMPT_USER_ID,
    DEAF_WARNING_DELAY_SECONDS,
    VOICE_ALERT_CHANNEL_ID,
    VoiceTracker,
)
from .ui import (
    IMAGE_PAGE_SIZE,
    TEXT_PAGE_SIZE,
    leaderboard_subtitle,
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
    "DEAF_TIME_EXEMPT_USER_ID",
    "DEAF_WARNING_DELAY_SECONDS",
    "VOICE_ALERT_CHANNEL_ID",
    "VoiceTracker",
    "LeaderboardImageRenderer",
    "LeaderboardView",
    "build_leaderboard_text_embed",
    "IMAGE_PAGE_SIZE",
    "TEXT_PAGE_SIZE",
    "leaderboard_subtitle",
    "build_user_stats_embed",
    "format_duration",
    "format_duration_diff",
    "split_session_by_midnight",
]
