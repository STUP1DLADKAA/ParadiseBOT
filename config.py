"""Central environment configuration for Paradise.

Every secret, external service credential and tuning knob lives here and
only here. A safe template is provided in ``.env.example``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(raw: str | None, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(raw: str | None, default: int) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _float(raw: str | None, default: float) -> float:
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _chance(raw: str | None, default: float) -> float:
    """A probability in [0, 1]; anything unparsable or out of range snaps back."""
    value = _float(raw, default)
    return value if 0.0 <= value <= 1.0 else default


@dataclass(frozen=True, slots=True)
class PersonalityConfig:
    """Tuning for Paradise's passive personality & humour (see functions/personality.py).

    Controls contextual pattern reactions/replies, random sticker & meme drops,
    unpredictable smart/stupid quips, and English-only nerd grammar corrections.
    """

    enabled: bool = True
    # Chance to add a contextual reaction to a matching message.
    reaction_chance: float = 0.045
    # Chance to send a short contextual reply instead (rarer; never both).
    reply_chance: float = 0.015
    # Chance to reply to a casual message with a random Discord/vinyl sticker.
    sticker_chance: float = 0.045
    # Chance to reply with a dynamically rendered meme card of the message.
    meme_chance: float = 0.045
    # Chance to drop a spontaneous smart/stupid/unpredictable quip.
    random_quip_chance: float = 0.035
    # Chance to reply in nerd voice when an English sentence has a typo/grammar slip.
    grammar_chance: float = 0.70
    # Seconds between ANY two personality actions, bot-wide.
    global_cooldown: int = 45
    # Seconds between personality actions in the same channel.
    channel_cooldown: int = 120
    # Extra spacing for pattern replies in the same channel.
    reply_channel_cooldown: int = 600
    # Per-channel spacing for stickers, memes, and spontaneous quips.
    humour_cooldown: int = 150
    # Per-channel spacing for English grammar/spelling nerd corrections.
    grammar_cooldown: int = 90

    @classmethod
    def from_env(cls, env) -> "PersonalityConfig":
        return cls(
            enabled=_bool(env.get("PERSONALITY_ENABLED"), True),
            reaction_chance=_chance(env.get("PERSONALITY_REACTION_CHANCE"), 0.045),
            reply_chance=_chance(env.get("PERSONALITY_REPLY_CHANCE"), 0.015),
            sticker_chance=_chance(env.get("PERSONALITY_STICKER_CHANCE"), 0.045),
            meme_chance=_chance(env.get("PERSONALITY_MEME_CHANCE"), 0.045),
            random_quip_chance=_chance(env.get("PERSONALITY_QUIP_CHANCE"), 0.035),
            grammar_chance=_chance(env.get("PERSONALITY_GRAMMAR_CHANCE"), 0.70),
            global_cooldown=_int(env.get("PERSONALITY_GLOBAL_COOLDOWN"), 45),
            channel_cooldown=_int(env.get("PERSONALITY_CHANNEL_COOLDOWN"), 120),
            reply_channel_cooldown=_int(
                env.get("PERSONALITY_REPLY_CHANNEL_COOLDOWN"), 600
            ),
            humour_cooldown=_int(env.get("PERSONALITY_HUMOUR_COOLDOWN"), 150),
            grammar_cooldown=_int(env.get("PERSONALITY_GRAMMAR_COOLDOWN"), 90),
        )


@dataclass(frozen=True, slots=True)
class DictionaryConfig:
    """The /dictionary feature (see functions/dictionary/).

    Works with zero configuration: the lookup providers and the fallback
    pronunciation voice are all free and key-less. Everything below is
    optional tuning or an opt-in upgrade.
    """

    # Answer plain-chat requests like "define hello" as well as the command.
    text_triggers: bool = True
    # Per-request deadline for every outbound dictionary/audio call.
    timeout: float = 8.0
    # Looked-up words are cached; these bound that cache.
    cache_ttl: int = 6 * 3600
    cache_size: int = 512
    # Image attachments cannot read Discord's viewer theme; choose the card
    # surface deliberately ("dark" is the Discord-friendly default).
    card_theme: str = "dark"

    # Prefer the dictionary's own human recording over synthesised speech.
    prefer_native_audio: bool = True
    # Speak the word in the asker's voice channel (needs PyNaCl + ffmpeg;
    # falls back to an audio attachment whenever it can't).
    voice_playback: bool = False

    # Optional neural voice: any OpenAI-compatible /audio/speech endpoint.
    speech_base_url: str = ""
    speech_api_key: str = ""
    speech_model: str = ""
    speech_voice: str = ""

    @property
    def speech_configured(self) -> bool:
        return bool(self.speech_base_url and self.speech_model)

    @classmethod
    def from_env(cls, env) -> "DictionaryConfig":
        card_theme = str(env.get("DICTIONARY_CARD_THEME", "dark") or "dark").strip().lower()
        return cls(
            text_triggers=_bool(env.get("DICTIONARY_TEXT_TRIGGERS"), True),
            timeout=max(2.0, _float(env.get("DICTIONARY_TIMEOUT"), 8.0)),
            cache_ttl=max(60, _int(env.get("DICTIONARY_CACHE_TTL"), 6 * 3600)),
            cache_size=max(16, _int(env.get("DICTIONARY_CACHE_SIZE"), 512)),
            card_theme=card_theme if card_theme in {"dark", "light"} else "dark",
            prefer_native_audio=_bool(env.get("PRONUNCIATION_PREFER_NATIVE"), True),
            voice_playback=_bool(env.get("PRONUNCIATION_VOICE_PLAYBACK"), False),
            speech_base_url=env.get("SPEECH_BASE_URL", "").strip().rstrip("/"),
            speech_api_key=env.get("SPEECH_API_KEY", "").strip(),
            speech_model=env.get("SPEECH_MODEL", "").strip(),
            speech_voice=env.get("SPEECH_VOICE", "").strip(),
        )


DEFAULT_VOICE_ALERT_CHANNEL_ID = 1507796401122181352
DEFAULT_DEAF_WARNING_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class VoiceConfig:
    """Voice activity tracking and daily DM reports configuration.

    Voice data lives in Supabase (the authoritative database). The two
    credentials are required for /vc to work; they are server-side only and
    must never be committed, logged or exposed to clients.
    """

    # Local SQLite file: user settings (timezones, DM preferences), the
    # bounded fallback outbox during Supabase outages, and the historical
    # voice data until it has been migrated and verified.
    db_path: str = ""
    dm_reports_enabled: bool = True

    # Channel for 30s deafen ping warnings and 12:00 AM daily VC leaderboards.
    alert_channel_id: int = DEFAULT_VOICE_ALERT_CHANNEL_ID
    deaf_warning_seconds: float = DEFAULT_DEAF_WARNING_SECONDS

    # Supabase project ("Paradise", ref gxcjtbiyamyyxokgehpe, ap-south-1).
    # SUPABASE_SECRET_KEY must be the server-side SECRET key (service_role),
    # NOT the public/anon key. Create the schema first by running
    # supabase/migrations/001_voice_activity.sql then 002_voice_ist_calendar.sql
    # in the Supabase SQL Editor.
    supabase_url: str = ""
    supabase_secret_key: str = ""

    # How often active VC sessions heartbeat last_seen_at to Supabase
    # (seconds). 15-30s keeps the leaderboard near-real-time without
    # spamming the database.
    heartbeat_interval: int = 20
    # Rows per batch when migrating SQLite history to Supabase.
    migration_batch_size: int = 500
    # Seconds between outbox flush / migration retry cycles.
    flush_interval: int = 30
    # Per-request timeout for Supabase calls (seconds).
    request_timeout: float = 15.0

    @property
    def supabase_configured(self) -> bool:
        return bool(self.supabase_url.strip() and self.supabase_secret_key.strip())

    @property
    def masked_supabase_key(self) -> str:
        """A log-safe rendering of the secret — never the full key."""
        key = self.supabase_secret_key.strip()
        if len(key) <= 8:
            return "…" if key else ""
        return f"{key[:2]}…{key[-2:]}"

    @classmethod
    def from_env(cls, env) -> "VoiceConfig":
        return cls(
            db_path=env.get("VOICE_DB_PATH", "").strip(),
            dm_reports_enabled=_bool(env.get("VOICE_DM_REPORTS_ENABLED"), True),
            alert_channel_id=max(
                0,
                _int(
                    env.get("VOICE_ALERT_CHANNEL_ID"),
                    DEFAULT_VOICE_ALERT_CHANNEL_ID,
                ),
            ),
            deaf_warning_seconds=max(
                0.0,
                _float(
                    env.get("VOICE_DEAF_WARNING_SECONDS"),
                    DEFAULT_DEAF_WARNING_SECONDS,
                ),
            ),
            supabase_url=env.get("SUPABASE_URL", "").strip().rstrip("/"),
            supabase_secret_key=env.get("SUPABASE_SECRET_KEY", "").strip(),
            heartbeat_interval=max(
                10, _int(env.get("VOICE_HEARTBEAT_INTERVAL"), 20)
            ),
            migration_batch_size=max(
                10, _int(env.get("VOICE_MIGRATION_BATCH_SIZE"), 500)
            ),
            flush_interval=max(5, _int(env.get("VOICE_FLUSH_INTERVAL"), 30)),
            request_timeout=max(5.0, _float(env.get("VOICE_SUPABASE_TIMEOUT"), 15.0)),
        )


@dataclass(frozen=True, slots=True)
class MiniGamesConfig:
    """Optional destination override for automated server mini-games."""

    channel_id: int = 0

    @classmethod
    def from_env(cls, env) -> "MiniGamesConfig":
        return cls(channel_id=max(0, _int(env.get("MINIGAMES_CHANNEL_ID"), 0)))


@dataclass(frozen=True, slots=True)
class ImageSearchConfig:
    """Web image search for "@Paradise show me a picture of…" requests.

    Works with zero configuration: Wikipedia, Wikimedia Commons and
    Openverse are all key-less. Setting the optional Google Programmable
    Search pair upgrades general web coverage (100 free queries/day); with
    no key the key-less chain is used and nothing breaks.

    This searches the web for real photos. It never generates images.
    """

    enabled: bool = True
    # Per-request deadline for every outbound image-search call.
    timeout: float = 10.0
    # Found image URLs are cached; these bound that cache. Only short
    # strings are stored — never image bytes.
    cache_ttl: int = 30 * 60
    cache_size: int = 128
    # Optional upgrade: Google Programmable Search (both values required).
    api_key: str = ""
    engine_id: str = ""

    @property
    def api_configured(self) -> bool:
        return bool(self.api_key.strip() and self.engine_id.strip())

    @classmethod
    def from_env(cls, env) -> "ImageSearchConfig":
        return cls(
            enabled=_bool(env.get("IMAGE_SEARCH_ENABLED"), True),
            timeout=max(3.0, _float(env.get("IMAGE_SEARCH_TIMEOUT"), 10.0)),
            cache_ttl=max(60, _int(env.get("IMAGE_SEARCH_CACHE_TTL"), 30 * 60)),
            cache_size=max(8, _int(env.get("IMAGE_SEARCH_CACHE_SIZE"), 128)),
            api_key=env.get("IMAGE_SEARCH_API_KEY", "").strip(),
            engine_id=env.get("IMAGE_SEARCH_ENGINE_ID", "").strip(),
        )


@dataclass(frozen=True, slots=True)
class GeminiConfig:
    """Google Gemini — the optional AI reasoning layer (see functions/gemini.py).

    Entirely optional: with no key the bot still answers definitions,
    conversions, calculations, weather, time, search and
    more — AI is strictly the last-resort reasoning layer.

    The key is only ever sent to Gemini's API in a header. It is never
    logged in full, never shown in bot messages and never committed.
    """

    enabled: bool = True
    api_key: str = ""
    # Model is configurable — only a default is provided.
    model: str = "gemini-2.5-flash"
    # Check the key against the API at startup (never fatal on error).
    validate_on_start: bool = True
    # Per-request deadline for Gemini calls.
    timeout: float = 30.0

    @property
    def configured(self) -> bool:
        """True when a key is present and the owner hasn't disabled Gemini."""
        return self.enabled and bool(self.api_key.strip())

    @property
    def masked_key(self) -> str:
        """A log-safe rendering of the key — never the full secret."""
        key = self.api_key.strip()
        if len(key) <= 8:
            return "…"
        return f"{key[:2]}…{key[-2:]}"

    @classmethod
    def from_env(cls, env) -> "GeminiConfig":
        return cls(
            enabled=_bool(env.get("GEMINI_ENABLED"), True),
            api_key=env.get("GEMINI_API_KEY", "").strip(),
            model=(env.get("GEMINI_MODEL", "") or "").strip() or "gemini-2.5-flash",
            validate_on_start=_bool(env.get("GEMINI_VALIDATE_ON_START"), True),
            timeout=max(5.0, _float(env.get("GEMINI_TIMEOUT"), 30.0)),
        )


@dataclass(frozen=True, slots=True)
class Config:
    """Everything Paradise reads from the environment."""

    token: str = ""

    # Gemini API key support (optional AI reasoning layer).
    gemini: GeminiConfig = GeminiConfig()

    # OpenAI-compatible chat completion endpoint (optional fallback provider).
    ai_base_url: str = ""
    ai_api_key: str = ""
    ai_model: str = ""

    # Rare passive personality (reactions / short replies).
    personality: PersonalityConfig = PersonalityConfig()

    # Dictionary lookups, cards and pronunciation.
    dictionary: DictionaryConfig = DictionaryConfig()

    # Voice activity tracking and statistics.
    voice: VoiceConfig = VoiceConfig()

    # Automated mini-game destination; scheduler settings are persisted in SQLite.
    minigames: MiniGamesConfig = MiniGamesConfig()

    # Web image search for mention requests ("show me a picture of …").
    image_search: ImageSearchConfig = ImageSearchConfig()

    @property
    def ai_configured(self) -> bool:
        # Gemini is the primary provider; an OpenAI-compatible endpoint is a
        # fallback (a base URL alone is enough for local providers e.g. Ollama).
        return self.gemini.configured or bool(self.ai_base_url and self.ai_model)

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "Config":
        env = os.environ if environ is None else environ
        return cls(
            token=env.get("DISCORD_TOKEN", "").strip(),
            gemini=GeminiConfig.from_env(env),
            ai_base_url=env.get("AI_BASE_URL", "").strip().rstrip("/"),
            ai_api_key=env.get("AI_API_KEY", "").strip(),
            ai_model=env.get("AI_MODEL", "").strip(),
            personality=PersonalityConfig.from_env(env),
            dictionary=DictionaryConfig.from_env(env),
            voice=VoiceConfig.from_env(env),
            minigames=MiniGamesConfig.from_env(env),
            image_search=ImageSearchConfig.from_env(env),
        )


def load_config() -> Config:
    """Load .env (if present) and parse the environment."""
    try:
        from dotenv import load_dotenv  # optional at runtime

        load_dotenv()
    except ImportError:  # pragma: no cover - dotenv is in requirements
        pass
    return Config.from_env()
