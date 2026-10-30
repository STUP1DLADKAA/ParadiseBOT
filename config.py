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


def _stt_model(raw: str | None) -> str:
    """A Whisper size ("small", "large-v3-turbo") or a path to a model dir.

    Kept forgiving on purpose: CTranslate2 also accepts the output directory of
    its own converter, so anything path-shaped is passed through untouched.
    """
    value = "".join(
        ch for ch in str(raw or "").strip() if ch.isalnum() or ch in "-._/:\\ "
    ).strip()
    return value or "small"


def _ids(raw: str | None) -> tuple[int, ...]:
    """A comma/space separated list of Discord IDs; anything invalid is ignored."""
    text = str(raw or "").strip()
    if not text:
        return ()
    out: list[int] = []
    for chunk in text.replace(";", ",").replace(" ", ",").split(","):
        value = chunk.strip().strip("<>@!")
        if not value.isdigit():
            continue
        number = int(value)
        if number and number not in out:
            out.append(number)
    return tuple(out)


def _csv(raw: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    """A comma/space separated list of identifiers, de-duplicated in order."""
    text = str(raw or "").strip()
    if not text:
        return default
    seen: set[str] = set()
    out: list[str] = []
    for chunk in text.replace(";", ",").split(","):
        for token in chunk.split():
            value = token.strip()
            if not value or value in seen:
                continue
            seen.add(value)
            out.append(value)
    return tuple(out) or default


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
    cache_size: int = 1024
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
            cache_size=max(16, _int(env.get("DICTIONARY_CACHE_SIZE"), 1024)),
            card_theme=card_theme if card_theme in {"dark", "light"} else "dark",
            prefer_native_audio=_bool(env.get("PRONUNCIATION_PREFER_NATIVE"), True),
            voice_playback=_bool(env.get("PRONUNCIATION_VOICE_PLAYBACK"), False),
            speech_base_url=env.get("SPEECH_BASE_URL", "").strip().rstrip("/"),
            speech_api_key=env.get("SPEECH_API_KEY", "").strip(),
            speech_model=env.get("SPEECH_MODEL", "").strip(),
            speech_voice=env.get("SPEECH_VOICE", "").strip(),
        )


@dataclass(frozen=True, slots=True)
class ElevenLabsConfig:
    """The owner's ElevenLabs voice — used for *every* spoken word.

    ``ELEVENLABS_VOICE_ID`` is the custom (cloned) male voice that speaks both
    Hindi and English. The default model is ``eleven_flash_v2_5``: the fastest
    multilingual model ElevenLabs ships, so a reply starts speaking in well
    under a second whenever the key is valid.

    The key travels only in the ``xi-api-key`` header. It is never logged,
    never echoed in a Discord message and never written to disk.
    """

    api_key: str = ""
    voice_id: str = ""
    # eleven_flash_v2_5 = fastest multilingual (Hindi + English). Override with
    # eleven_turbo_v2_5 (slightly better quality) or eleven_multilingual_v2.
    model: str = "eleven_flash_v2_5"
    # 128 kbps 44.1 kHz MP3 by default — noticeably cleaner than the 64 kbps
    # web voices and still tiny for a sentence of speech.
    output_format: str = "mp3_44100_128"
    # Voice shaping. Natural, expressive and close to the captured voice.
    stability: float = 0.45
    similarity_boost: float = 0.85
    style: float = 0.15
    speed: float = 1.0
    # Optional ISO-639-1 hint ("hi" / "en"). Blank = let the model detect it,
    # which is what makes Hinglish sentences come out right.
    language_code: str = ""
    # Speak dictionary headwords with this voice even when a human recording
    # exists (the owner asked for *their* voice everywhere). Set false to let
    # native dictionary recordings win.
    pronunciation_first: bool = True
    # Per-request deadline for one synthesis call.
    timeout: float = 25.0

    @property
    def configured(self) -> bool:
        return bool(self.api_key.strip() and self.voice_id.strip())

    @property
    def masked_key(self) -> str:
        key = self.api_key.strip()
        if len(key) <= 8:
            return "…" if key else ""
        return f"{key[:2]}…{key[-2:]}"

    @classmethod
    def from_env(cls, env) -> "ElevenLabsConfig":
        model = str(env.get("ELEVENLABS_MODEL", "") or "").strip() or "eleven_flash_v2_5"
        output_format = (
            str(env.get("ELEVENLABS_OUTPUT_FORMAT", "") or "").strip()
            or "mp3_44100_128"
        )
        return cls(
            api_key=env.get("ELEVENLABS_API_KEY", "").strip(),
            voice_id=env.get("ELEVENLABS_VOICE_ID", "").strip(),
            model=model,
            output_format=output_format,
            stability=min(1.0, max(0.0, _float(env.get("ELEVENLABS_STABILITY"), 0.45))),
            similarity_boost=min(
                1.0, max(0.0, _float(env.get("ELEVENLABS_SIMILARITY"), 0.85))
            ),
            style=min(1.0, max(0.0, _float(env.get("ELEVENLABS_STYLE"), 0.15))),
            speed=min(1.2, max(0.7, _float(env.get("ELEVENLABS_SPEED"), 1.0))),
            language_code=str(env.get("ELEVENLABS_LANGUAGE", "") or "").strip().lower(),
            pronunciation_first=_bool(
                env.get("ELEVENLABS_PRONUNCIATION_FIRST"), True
            ),
            timeout=max(5.0, _float(env.get("ELEVENLABS_TIMEOUT"), 25.0)),
        )


# Fastest-first Gemini cascade used for spoken "Hey Paradise" answers. The
# first *available* model that returns an answer wins; a missing/quota-blocked
# model is skipped automatically, so the bot never stalls on one model.
DEFAULT_GEMINI_CHAIN: tuple[str, ...] = (
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
)


@dataclass(frozen=True, slots=True)
class WakeConfig:
    """The "Hey Paradise …" voice-chat assistant (see commands/wake.py).

    Someone types ``Hey Paradise <question>`` in a voice channel's text chat;
    Paradise answers in that same chat *and* speaks the answer in the channel
    with the owner's ElevenLabs voice.
    """

    enabled: bool = True
    # Only react inside voice-channel text chats (the "VC chat").
    vc_only: bool = True
    # Fastest-first model cascade. Blank falls back to DEFAULT_GEMINI_CHAIN.
    models: tuple[str, ...] = DEFAULT_GEMINI_CHAIN
    # Answer budget for the spoken/typed reply.
    max_answer_chars: int = 700
    # What is actually spoken aloud (keeps a long answer from monologuing).
    speech_max_chars: int = 600
    # Reuse the same text chat as the reply target; speak when connected.
    speak: bool = True
    # Auto-connect to the asker's voice channel when Paradise is elsewhere.
    connect_if_needed: bool = True
    user_cooldown: float = 4.0
    channel_cooldown: float = 2.0

    @classmethod
    def from_env(cls, env) -> "WakeConfig":
        return cls(
            enabled=_bool(env.get("WAKE_WORD_ENABLED"), True),
            vc_only=_bool(env.get("WAKE_WORD_VC_ONLY"), True),
            models=_csv(env.get("GEMINI_MODEL_CHAIN"), DEFAULT_GEMINI_CHAIN),
            max_answer_chars=max(
                80, min(1500, _int(env.get("WAKE_WORD_MAX_CHARS"), 700))
            ),
            speech_max_chars=max(
                80, min(1200, _int(env.get("WAKE_WORD_SPEECH_CHARS"), 600))
            ),
            speak=_bool(env.get("WAKE_WORD_SPEAK"), True),
            connect_if_needed=_bool(env.get("WAKE_WORD_CONNECT"), True),
            user_cooldown=max(0.0, _float(env.get("WAKE_WORD_USER_COOLDOWN"), 4.0)),
            channel_cooldown=max(
                0.0, _float(env.get("WAKE_WORD_CHANNEL_COOLDOWN"), 2.0)
            ),
        )


@dataclass(frozen=True, slots=True)
class VoicePresenceConfig:
    """Automatic voice-channel presence (see commands/voice_presence.py)."""

    enabled: bool = True
    # Paradise joins silently: no text in the channel, nothing spoken.
    announce: bool = False
    # Seconds between crowd-following scans. Lower = snappier joins.
    follow_interval: float = 20.0

    @classmethod
    def from_env(cls, env) -> "VoicePresenceConfig":
        return cls(
            enabled=_bool(env.get("VOICE_PRESENCE_ENABLED"), True),
            announce=_bool(env.get("VOICE_ARRIVAL_ANNOUNCE"), False),
            follow_interval=max(
                5.0, min(300.0, _float(env.get("VOICE_PRESENCE_INTERVAL"), 20.0))
            ),
        )


@dataclass(frozen=True, slots=True)
class AutoUpdateConfig:
    """Pull the latest files from GitHub on start-up and while online."""

    enabled: bool = True
    repository: str = "STUP1DLADKAA/ParadiseBOT"
    branch: str = "main"
    # Seconds between background checks while the bot is online (0 = never).
    interval: float = 1800.0
    check_on_start: bool = True
    # False = report an available update without touching any file.
    apply: bool = True
    # Exit after applying a start-up update so the process manager restarts
    # cleanly on the new code (running a half-old, half-new Python process is
    # never safe). Set false to keep running the already-imported code.
    restart_after_update: bool = True
    # Directory holding the applied-commit state file.
    state_dir: str = ""
    timeout: float = 45.0
    # Deadline for the connect-time check (a slow GitHub must not stall boot).
    startup_timeout: float = 20.0
    # Optional GitHub token (raises the API rate limit for public repos and
    # is required to read a private one). Never logged.
    token: str = ""
    # Extra paths that must never be overwritten (comma separated, relative).
    protected: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env) -> "AutoUpdateConfig":
        repo = (
            str(env.get("AUTO_UPDATE_REPO", "") or "").strip()
            or "STUP1DLADKAA/ParadiseBOT"
        )
        return cls(
            enabled=_bool(env.get("AUTO_UPDATE_ENABLED"), True),
            repository=repo,
            branch=str(env.get("AUTO_UPDATE_BRANCH", "") or "").strip() or "main",
            interval=max(
                0.0, _float(env.get("AUTO_UPDATE_INTERVAL"), 1800.0)
            ),
            check_on_start=_bool(env.get("AUTO_UPDATE_ON_START"), True),
            apply=_bool(env.get("AUTO_UPDATE_APPLY"), True),
            restart_after_update=_bool(env.get("AUTO_UPDATE_RESTART"), True),
            state_dir=str(env.get("AUTO_UPDATE_STATE_DIR", "") or "").strip(),
            timeout=max(10.0, _float(env.get("AUTO_UPDATE_TIMEOUT"), 45.0)),
            startup_timeout=max(
                5.0, _float(env.get("AUTO_UPDATE_STARTUP_TIMEOUT"), 20.0)
            ),
            token=str(env.get("GITHUB_TOKEN", "") or "").strip(),
            protected=_csv(env.get("AUTO_UPDATE_PROTECTED"), ()),
        )


#: Where deafen warnings and the midnight leaderboard are posted. 0 (the
#: default) disables those alerts until you set ``VOICE_ALERT_CHANNEL_ID``.
DEFAULT_VOICE_ALERT_CHANNEL_ID = 0
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

    # Supabase project (the deployment this bot grew up in: ap-south-1).
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
    # Coalescing window (seconds) for voice heartbeat writes. Discord voice
    # state changes inside this window are merged into one batched upsert.
    # 0 restores the legacy write-per-event behaviour.
    write_coalesce_seconds: float = 2.0
    # How often an in-progress session is committed to voice_daily_stats
    # (seconds). This bounds how much voice time a hard crash can lose, and
    # how long a manual Supabase correction takes to be adopted. One batched
    # RPC covers every live session regardless of how many users are in voice.
    # 0 disables periodic checkpointing (time becomes durable only at close).
    checkpoint_seconds: float = 120.0

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
            write_coalesce_seconds=min(
                10.0, max(0.0, _float(env.get("VOICE_WRITE_COALESCE_SECONDS"), 2.0))
            ),
            checkpoint_seconds=min(
                3600.0, max(0.0, _float(env.get("VOICE_CHECKPOINT_SECONDS"), 120.0))
            ),
        )


@dataclass(frozen=True, slots=True)
class VoiceClipConfig:
    """Bounded rolling voice-clip buffer and export resource limits (/clip).

    This deployment is a private one: **every** speaker in the channel is
    captured, with no opt-in, no opt-out and no notice posted in the channel.
    """

    enabled: bool = True
    # Hard-capped at 180 seconds per product requirement.
    max_buffer_seconds: int = 180
    # Maximum distinct active speaker streams retained per voice connection.
    max_active_speakers: int = 32
    # Maximum memory (bytes) consumed by buffered Opus frames per guild.
    # The host is no longer resource-starved, so the rolling window is kept in
    # a generous 48 MiB budget per guild.
    max_buffer_bytes: int = 48 * 1024 * 1024
    # Concurrent active export jobs and bounded waiting queue capacity.
    max_concurrent_exports: int = 2
    max_queued_exports: int = 4
    # Per-export deadline covering decode, mix and FFmpeg encode (seconds).
    export_timeout_seconds: float = 45.0
    # Safe Discord DM upload cap (~9.5 MiB, well under the 10 MiB default limit).
    max_file_bytes: int = 9_961_472
    # Upper bound on temporary output bytes across in-flight exports.
    max_temp_disk_bytes: int = 64 * 1024 * 1024
    # AAC-LC bitrate (kbps) at 48 kHz mono. 128 kbps is clear speech and about
    # a third smaller than 192 — a 3-minute clip is ~3 MB instead of ~4.5 MB.
    bitrate_kbps: int = 128
    # Mastering chain applied while encoding the mixed clip (empty = default).
    audio_filter: str = ""

    @classmethod
    def from_env(cls, env) -> "VoiceClipConfig":
        buffer_mb = max(2, min(256, _int(env.get("VOICE_CLIP_MAX_BUFFER_MB"), 48)))
        temp_mb = max(4, min(512, _int(env.get("VOICE_CLIP_MAX_TEMP_MB"), 64)))
        return cls(
            enabled=_bool(env.get("VOICE_CLIP_ENABLED"), True),
            max_buffer_seconds=max(
                1, min(180, _int(env.get("VOICE_CLIP_MAX_SECONDS"), 180))
            ),
            max_active_speakers=max(
                1, min(64, _int(env.get("VOICE_CLIP_MAX_SPEAKERS"), 32))
            ),
            max_buffer_bytes=buffer_mb * 1024 * 1024,
            max_concurrent_exports=max(
                1, min(8, _int(env.get("VOICE_CLIP_MAX_CONCURRENT_EXPORTS"), 2))
            ),
            max_queued_exports=max(
                0, min(16, _int(env.get("VOICE_CLIP_MAX_QUEUED_EXPORTS"), 4))
            ),
            export_timeout_seconds=max(
                5.0, min(180.0, _float(env.get("VOICE_CLIP_EXPORT_TIMEOUT"), 45.0))
            ),
            max_file_bytes=max(
                262_144,
                min(25 * 1024 * 1024, _int(env.get("VOICE_CLIP_MAX_FILE_BYTES"), 9_961_472)),
            ),
            max_temp_disk_bytes=temp_mb * 1024 * 1024,
            bitrate_kbps=max(
                64, min(320, _int(env.get("VOICE_CLIP_BITRATE_KBPS"), 128))
            ),
            audio_filter=str(env.get("VOICE_CLIP_AUDIO_FILTER", "") or "").strip(),
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
class VoiceWakeConfig:
    """The wake word spoken out loud in a voice channel (functions/voice_wake).

    Paradise listens to the channel it is already sitting in, waits for an
    utterance to *finish*, transcribes it once, and only then decides whether
    it was addressed — "hey paradise …" in English, Hinglish or Hindi.

    Recognition runs **on this machine** by default: faster-whisper, in this
    process, on this CPU, with no key and no upload (``functions/voice_wake/
    local_stt.py``). The cloud providers still exist for machines that cannot
    run Whisper, and ``VOICE_WAKE_STT=local`` forbids them outright.

    Because a local transcription is never uploaded, nothing leaves the bot.
    Capture is unconditional — every human in the channel is heard, with no
    opt-out to guard — and ``VOICE_WAKE_ANNOUNCE`` controls the one-time notice.
    Turn it off with ``VOICE_WAKE_ENABLED=false`` and everything else keeps
    working.
    """

    enabled: bool = True
    #: "auto" (local whisper, then ElevenLabs Scribe, then Gemini), "local",
    #: "elevenlabs", "gemini" or "off".
    stt: str = "auto"
    #: Whisper size for the local engine: tiny, base, small, medium, large-v3…
    stt_model: str = "small"
    #: torch-free CTranslate2 device: "cpu" (default) or "cuda".
    stt_device: str = "cpu"
    #: CTranslate2 precision: int8 (fast, default), int8_float32 or float32.
    stt_compute: str = "int8"
    #: CPU threads for inference; 0 lets CTranslate2 pick.
    stt_threads: int = 0
    #: Directory holding the model weights (downloads there on first run).
    stt_model_dir: str = ""
    #: Never touch the network: only use weights that are already on disk.
    stt_offline: bool = False
    #: Fetch the weights at start-up (once) instead of on the first utterance.
    stt_preload: bool = True
    #: Drop the weights from RAM after this much silence; 0 keeps them loaded.
    stt_unload_after: float = 900.0
    #: ISO-639-1 hint for the transcriber; blank lets it detect.
    language: str = ""
    #: Framed speech detection: a 20 ms frame above this fraction of full
    #: scale counts as voice. ~0.012 is -38 dBFS.
    energy_threshold: float = 0.012
    #: How much voice must be heard before recording starts.
    onset_ms: int = 60
    #: Quiet gap that closes an utterance.
    silence_ms: int = 700
    #: Anything shorter than this is a door slam, not a sentence.
    min_seconds: float = 0.6
    #: Hard cap on one utterance (protects memory and the API bill).
    max_seconds: float = 12.0
    #: Per-user gap between two accepted requests.
    cooldown: float = 3.0
    #: Guild-wide ceiling, asked per minute, whatever the crowd size.
    max_per_minute: int = 8
    #: After a bare "hey paradise", how long the question may follow.
    follow_up_seconds: float = 6.0
    #: Speakers tracked at once.
    max_speakers: int = 32
    #: Post a one-time "listening for 'Hey Paradise'" notice in the channel.
    announce: bool = True
    #: Post the answer in the voice channel's text chat as well as speaking it.
    post_text: bool = True
    #: Say "haan?" when only the wake word was heard.
    acknowledge: bool = True
    #: Play a short chime the instant the phrase is recognised, so nobody has
    #: to wonder whether they were heard while the answer is being built.
    cue: bool = True

    @property
    def listening(self) -> bool:
        return bool(self.enabled and self.stt != "off")

    @classmethod
    def from_env(cls, env) -> "VoiceWakeConfig":
        stt = str(env.get("VOICE_WAKE_STT", "auto") or "auto").strip().lower()
        if stt not in {"auto", "local", "elevenlabs", "gemini", "off"}:
            stt = "auto"
        return cls(
            enabled=_bool(env.get("VOICE_WAKE_ENABLED"), True),
            stt=stt,
            stt_model=_stt_model(env.get("VOICE_WAKE_STT_MODEL")),
            stt_device=str(env.get("VOICE_WAKE_STT_DEVICE", "cpu") or "cpu")
            .strip()
            .lower(),
            stt_compute=str(env.get("VOICE_WAKE_STT_COMPUTE", "int8") or "int8")
            .strip()
            .lower(),
            stt_threads=max(0, min(64, _int(env.get("VOICE_WAKE_STT_THREADS"), 0))),
            stt_model_dir=str(env.get("VOICE_WAKE_STT_MODEL_DIR", "") or "").strip(),
            stt_offline=_bool(env.get("VOICE_WAKE_STT_OFFLINE"), False),
            stt_preload=_bool(env.get("VOICE_WAKE_STT_PRELOAD"), True),
            stt_unload_after=max(
                0.0, min(86_400.0, _float(env.get("VOICE_WAKE_STT_UNLOAD_AFTER"), 900.0))
            ),
            language=str(env.get("VOICE_WAKE_LANGUAGE", "") or "").strip().lower()[:5],
            energy_threshold=max(
                0.0005, min(0.2, _float(env.get("VOICE_WAKE_THRESHOLD"), 0.012))
            ),
            onset_ms=max(20, min(400, _int(env.get("VOICE_WAKE_ONSET_MS"), 60))),
            silence_ms=max(200, min(3000, _int(env.get("VOICE_WAKE_SILENCE_MS"), 700))),
            min_seconds=max(
                0.2, min(5.0, _float(env.get("VOICE_WAKE_MIN_SECONDS"), 0.6))
            ),
            max_seconds=max(
                2.0, min(30.0, _float(env.get("VOICE_WAKE_MAX_SECONDS"), 12.0))
            ),
            cooldown=max(0.0, min(60.0, _float(env.get("VOICE_WAKE_COOLDOWN"), 3.0))),
            max_per_minute=max(
                1, min(60, _int(env.get("VOICE_WAKE_MAX_PER_MINUTE"), 8))
            ),
            follow_up_seconds=max(
                2.0, min(30.0, _float(env.get("VOICE_WAKE_FOLLOWUP_SECONDS"), 6.0))
            ),
            max_speakers=max(1, min(128, _int(env.get("VOICE_WAKE_MAX_SPEAKERS"), 32))),
            announce=_bool(env.get("VOICE_WAKE_ANNOUNCE"), True),
            post_text=_bool(env.get("VOICE_WAKE_POST_TEXT"), True),
            acknowledge=_bool(env.get("VOICE_WAKE_ACKNOWLEDGE"), True),
            cue=_bool(env.get("VOICE_WAKE_CUE"), True),
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

    # The owner's ElevenLabs voice — used for pronunciation and every spoken
    # voice-channel reply. Entirely optional.
    elevenlabs: ElevenLabsConfig = ElevenLabsConfig()

    # "Hey Paradise <question>" in voice-chat (typed answer + spoken answer).
    wake: WakeConfig = WakeConfig()

    # Automatic voice-channel presence (silent by default).
    voice_presence: VoicePresenceConfig = VoicePresenceConfig()

    # Pull the latest files from GitHub on start-up / while online.
    autoupdate: AutoUpdateConfig = AutoUpdateConfig()

    # Voice activity tracking and statistics.
    voice: VoiceConfig = VoiceConfig()

    # Bounded rolling voice clip capture and DM export (/clip).
    voice_clip: VoiceClipConfig = VoiceClipConfig()

    # Automated mini-game destination; scheduler settings are persisted in SQLite.
    minigames: MiniGamesConfig = MiniGamesConfig()

    # The wake word spoken out loud in a voice channel.
    voice_wake: VoiceWakeConfig = VoiceWakeConfig()

    # Web image search for mention requests ("show me a picture of …").
    image_search: ImageSearchConfig = ImageSearchConfig()

    # Where preloaded speech clips are cached on disk (default data/tts_cache).
    tts_cache_dir: str = ""

    # Warm fonts, card backdrops and the render pool at start-up so the first
    # command is as fast as the thousandth. Costs one short burst of CPU.
    prewarm: bool = True

    # Home guild for slash commands. They are *only* ever registered here:
    # Discord publishes guild commands instantly, and a global copy would be a
    # duplicate entry until its cache expired. Set ``DISCORD_GUILD_ID``.
    guild_id: int = 0

    # Operator-only features (/edit, /clip, the deafened-VC exemption). These
    # are Discord user IDs, not secrets: configure your own in .env.
    edit_user_ids: tuple[int, ...] = ()
    clip_user_ids: tuple[int, ...] = ()
    deaf_exempt_user_ids: tuple[int, ...] = ()

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
            elevenlabs=ElevenLabsConfig.from_env(env),
            wake=WakeConfig.from_env(env),
            voice_wake=VoiceWakeConfig.from_env(env),
            voice_presence=VoicePresenceConfig.from_env(env),
            autoupdate=AutoUpdateConfig.from_env(env),
            voice=VoiceConfig.from_env(env),
            voice_clip=VoiceClipConfig.from_env(env),
            minigames=MiniGamesConfig.from_env(env),
            image_search=ImageSearchConfig.from_env(env),
            prewarm=_bool(env.get("PREWARM_ON_START"), True),
            guild_id=max(0, _int(env.get("DISCORD_GUILD_ID"), 0)),
            edit_user_ids=_ids(env.get("EDIT_ALLOWED_USER_IDS")),
            clip_user_ids=_ids(env.get("CLIP_ALLOWED_USER_IDS")),
            deaf_exempt_user_ids=_ids(env.get("VOICE_DEAF_EXEMPT_USER_IDS")),
            tts_cache_dir=(
                str(env.get("TTS_CACHE_DIR", "") or "").strip()
                or os.path.join(
                    os.path.dirname(
                        str(env.get("VOICE_DB_PATH", "") or "").strip()
                        or os.path.join("data", "paradise.db")
                    ),
                    "tts_cache",
                )
            ),
        )


def load_config() -> Config:
    """Load .env (if present) and parse the environment."""
    try:
        from dotenv import load_dotenv  # optional at runtime

        load_dotenv()
    except ImportError:  # pragma: no cover - dotenv is in requirements
        pass
    return Config.from_env()
