"""Central environment configuration for Paradise.

Every secret, external service credential and tuning knob lives here and
only here. A safe template is provided in ``.env.example``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


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


def _sources(raw: str | None) -> tuple[str, ...]:
    """Comma/space separated subreddit names, validated into a safe tuple."""
    default = (
        "dankmemes",
        "memes",
        "me_irl",
        "wholesomememes",
        "terriblefacebookmemes",
        "historymemes",
    )
    if raw is None or not str(raw).strip():
        return default
    names: list[str] = []
    for chunk in str(raw).replace(",", " ").split():
        name = "".join(ch for ch in chunk if ch.isalnum() or ch == "_")
        if name and name.lower() not in {existing.lower() for existing in names}:
            names.append(name)
    return tuple(names[:12]) or default


def _roast_intensity(raw: str | None) -> str:
    """One of mild / spicy / nuclear; anything else is the default."""
    value = (raw or "").strip().lower()
    return value if value in {"mild", "spicy", "nuclear"} else "spicy"


#: The shipped lords: Stupid (the owner) and Taher — same level of authority.
DEFAULT_LORD_USER_IDS: tuple[int, ...] = (922359954025885737, 1049231767585964032)


def _lord_ids_from_env(env) -> tuple[int, ...]:
    """Parse the lord list.

    ``PARADISE_LORD_USER_IDS`` (comma-separated) is the multi-lord form;
    ``PARADISE_LORD_USER_ID`` (single) still works and is merged in. When
    NEITHER is set, the shipped pair (Stupid and Taher) is used; when either
    is set, the configured list replaces the shipped one — exactly how the
    old single-id override behaved.
    """
    many = _ids(env.get("PARADISE_LORD_USER_IDS"))
    one = max(0, _int(env.get("PARADISE_LORD_USER_ID"), 0))
    if not many and not one:
        return DEFAULT_LORD_USER_IDS
    ids = list(many)
    if one and one not in ids:
        ids.append(one)
    return tuple(ids)


def lord_ids_of(config) -> tuple[int, ...]:
    """Every lord id on a Config or any config-like object.

    Accepts the new ``lord_user_ids`` tuple and the old single
    ``lord_user_id`` attribute (tests and fakes use both shapes), so no
    caller has to care which one it was handed.
    """
    ids = tuple(int(value) for value in (getattr(config, "lord_user_ids", ()) or ()) if value)
    legacy = int(getattr(config, "lord_user_id", 0) or 0)
    if legacy and legacy not in ids:
        ids = (*ids, legacy)
    return ids


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
    """Tuning for Paradise's passive personality (see functions/personality.py).

    This is the "alive" part of the bot: reactions, replies, stickers, memes
    from the internet, GIFs, spontaneous quips, roasts and English-only nerd
    grammar corrections. The defaults are set for a small friends-only server
    where the bot is meant to be one of the loudest people in the channel —
    turn any knob down, or ``PERSONALITY_ENABLED=false`` for silence.
    """

    enabled: bool = True
    # Chance to add a contextual reaction to a matching message.
    reaction_chance: float = 0.18
    # Chance to react to a casual message that matched no pattern at all.
    ambient_reaction_chance: float = 0.06
    # Chance to send a short contextual reply instead (never both).
    reply_chance: float = 0.08
    # Chance to reply to a casual message with a random Discord/vinyl sticker.
    sticker_chance: float = 0.04
    # Chance to reply with a meme — from the internet when it is reachable,
    # rendered locally when it is not.
    meme_chance: float = 0.10
    # Of those meme drops, how many should try the internet first.
    online_meme_chance: float = 0.75
    # Chance to reply with a reaction GIF (keyless by default; keys add quality).
    gif_chance: float = 0.06
    # Chance to drop a spontaneous smart/stupid/unpredictable quip.
    random_quip_chance: float = 0.08
    # Chance to roast whoever just spoke.
    roast_chance: float = 0.08
    # "mild", "spicy" or "nuclear" — see functions/roasts.py.
    roast_intensity: str = "spicy"
    # Seconds before the same person can be roasted again.
    roast_user_cooldown: int = 600
    # Chance to reply in nerd voice when an English sentence has a typo/grammar slip.
    grammar_chance: float = 0.70
    # Seconds between ANY two personality actions, bot-wide.
    global_cooldown: int = 10
    # Seconds between personality actions in the same channel.
    channel_cooldown: int = 60
    # Extra spacing for pattern replies in the same channel.
    reply_channel_cooldown: int = 180
    # Per-channel spacing for stickers, memes, GIFs and spontaneous quips.
    humour_cooldown: int = 45
    # Per-channel spacing for English grammar/spelling nerd corrections.
    grammar_cooldown: int = 90
    # Communities the keyless meme feed may pull from.
    meme_sources: tuple[str, ...] = (
        "dankmemes",
        "memes",
        "me_irl",
        "wholesomememes",
        "terriblefacebookmemes",
        "historymemes",
    )
    # Per-request deadline for the meme/GIF APIs.
    online_timeout: float = 6.0
    # Optional free keys; without one, GIF replies fall back to memes.
    tenor_api_key: str = ""
    giphy_api_key: str = ""
    # Chance to say "hello!" when two people are having a conversation.
    # Low on purpose: unprompted chatter is the first thing that gets old.
    interject_chance: float = 0.04
    # Chance to butt in when somebody talks ABOUT Paradise (his name or
    # "the bot") without pinging him. The reply is written fresh by the AI
    # every time — never a canned line.
    about_chance: float = 0.35
    # Seconds before Paradise butts in about himself in the same channel
    # again. Being discussed is not an invitation to reply to every line.
    about_cooldown: int = 180
    # Seconds before the bot may interject in the same channel again.
    interject_cooldown: int = 600
    # Chance the bot acknowledges its lord when he speaks (reply or 👑).
    lord_reply_chance: float = 0.20
    # Chance to drop a "remember when …" callback when somebody the bot has
    # memories about speaks (see /memory). Occasional on purpose.
    memory_callback_chance: float = 0.05
    # Seconds before the same person can be memory-called-back again.
    memory_callback_cooldown: int = 1800
    # Hard ceiling on *unprompted* messages per channel per rolling hour
    # (replies to mentions are never counted). 0 disables the ceiling.
    ambient_hourly_cap: int = 6

    @classmethod
    def from_env(cls, env) -> "PersonalityConfig":
        return cls(
            enabled=_bool(env.get("PERSONALITY_ENABLED"), True),
            reaction_chance=_chance(env.get("PERSONALITY_REACTION_CHANCE"), 0.18),
            ambient_reaction_chance=_chance(
                env.get("PERSONALITY_AMBIENT_REACTION_CHANCE"), 0.06
            ),
            reply_chance=_chance(env.get("PERSONALITY_REPLY_CHANCE"), 0.08),
            sticker_chance=_chance(env.get("PERSONALITY_STICKER_CHANCE"), 0.04),
            meme_chance=_chance(env.get("PERSONALITY_MEME_CHANCE"), 0.10),
            online_meme_chance=_chance(
                env.get("PERSONALITY_ONLINE_MEME_CHANCE"), 0.75
            ),
            gif_chance=_chance(env.get("PERSONALITY_GIF_CHANCE"), 0.06),
            random_quip_chance=_chance(env.get("PERSONALITY_QUIP_CHANCE"), 0.08),
            roast_chance=_chance(env.get("PERSONALITY_ROAST_CHANCE"), 0.08),
            roast_intensity=_roast_intensity(env.get("PERSONALITY_ROAST_INTENSITY")),
            roast_user_cooldown=_int(env.get("PERSONALITY_ROAST_USER_COOLDOWN"), 600),
            grammar_chance=_chance(env.get("PERSONALITY_GRAMMAR_CHANCE"), 0.70),
            global_cooldown=_int(env.get("PERSONALITY_GLOBAL_COOLDOWN"), 10),
            channel_cooldown=_int(env.get("PERSONALITY_CHANNEL_COOLDOWN"), 60),
            reply_channel_cooldown=_int(
                env.get("PERSONALITY_REPLY_CHANNEL_COOLDOWN"), 180
            ),
            humour_cooldown=_int(env.get("PERSONALITY_HUMOUR_COOLDOWN"), 45),
            grammar_cooldown=_int(env.get("PERSONALITY_GRAMMAR_COOLDOWN"), 90),
            meme_sources=_sources(env.get("MEME_SOURCES")),
            online_timeout=_float(env.get("MEME_TIMEOUT"), 6.0),
            tenor_api_key=(env.get("TENOR_API_KEY") or "").strip(),
            giphy_api_key=(env.get("GIPHY_API_KEY") or "").strip(),
            interject_chance=_chance(env.get("PERSONALITY_INTERJECT_CHANCE"), 0.04),
            about_chance=_chance(env.get("PERSONALITY_ABOUT_CHANCE"), 0.35),
            about_cooldown=_int(env.get("PERSONALITY_ABOUT_COOLDOWN"), 180),
            interject_cooldown=_int(
                env.get("PERSONALITY_INTERJECT_COOLDOWN"), 600
            ),
            lord_reply_chance=_chance(env.get("PERSONALITY_LORD_REPLY_CHANCE"), 0.20),
            memory_callback_chance=_chance(
                env.get("PERSONALITY_MEMORY_CALLBACK_CHANCE"), 0.05
            ),
            memory_callback_cooldown=_int(
                env.get("PERSONALITY_MEMORY_CALLBACK_COOLDOWN"), 1800
            ),
            ambient_hourly_cap=_int(
                env.get("PERSONALITY_AMBIENT_HOURLY_CAP"), 6
            ),
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
    # Maximum memory (bytes) consumed by buffered Opus frames per guild. Three
    # minutes of speech from a dozen people is a few MiB; this is a ceiling,
    # not an allocation, and it is released the moment recording stops.
    max_buffer_bytes: int = 32 * 1024 * 1024
    # Concurrent active export jobs and bounded waiting queue capacity.
    # One at a time: an export is a short FFmpeg burst on the whole core, and a
    # 60-second clip finishes in about a second, so a queue of two never makes
    # anybody wait — while the box never sees two encoders at once.
    max_concurrent_exports: int = 1
    max_queued_exports: int = 2
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
    # Temporary capture-health diagnostics: log a one-line pipeline summary
    # per guild every sweep (connection, receiver, counters). Off by default.
    debug_logging: bool = False

    @classmethod
    def from_env(cls, env) -> "VoiceClipConfig":
        buffer_mb = max(2, min(256, _int(env.get("VOICE_CLIP_MAX_BUFFER_MB"), 32)))
        temp_mb = max(4, min(512, _int(env.get("VOICE_CLIP_MAX_TEMP_MB"), 64)))
        return cls(
            enabled=_bool(env.get("VOICE_CLIP_ENABLED"), True),
            debug_logging=_bool(env.get("VOICE_CLIP_DEBUG"), False),
            max_buffer_seconds=max(
                1, min(180, _int(env.get("VOICE_CLIP_MAX_SECONDS"), 180))
            ),
            max_active_speakers=max(
                1, min(64, _int(env.get("VOICE_CLIP_MAX_SPEAKERS"), 32))
            ),
            max_buffer_bytes=buffer_mb * 1024 * 1024,
            max_concurrent_exports=max(
                1, min(8, _int(env.get("VOICE_CLIP_MAX_CONCURRENT_EXPORTS"), 1))
            ),
            max_queued_exports=max(
                0, min(16, _int(env.get("VOICE_CLIP_MAX_QUEUED_EXPORTS"), 2))
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
class WebSearchConfig:
    """Web research for "@Paradise search …" / "summarize this link …".

    Works with zero configuration: DuckDuckGo's key-less endpoints answer
    the query and Wikipedia's open API backs it up. Every result is
    cached, every call is time-boxed, and a failing provider degrades to
    the AI answering from its own knowledge (with a note saying so) —
    research never breaks a reply.

    Fetched pages are untrusted input: they are stripped, capped and
    framed as source material so embedded instructions cannot reach the
    model as commands (see functions/research.py).
    """

    enabled: bool = True
    # Per-request deadline for search queries and page fetches.
    timeout: float = 10.0
    # How many results to consider (and show as sources).
    max_results: int = 5
    # How many of the top results to actually fetch and read.
    fetch_pages: int = 2
    # Result cache (queries → results) and page cache (URL → extracted text).
    cache_ttl: int = 15 * 60
    cache_size: int = 128
    # Maximum extracted characters handed to the model per page.
    page_char_limit: int = 6000
    # Announce sourced answers with a source footer even when short.
    show_sources: bool = True

    @classmethod
    def from_env(cls, env) -> "WebSearchConfig":
        return cls(
            enabled=_bool(env.get("WEB_SEARCH_ENABLED"), True),
            timeout=max(3.0, _float(env.get("WEB_SEARCH_TIMEOUT"), 10.0)),
            max_results=max(3, min(_int(env.get("WEB_SEARCH_MAX_RESULTS"), 5), 8)),
            fetch_pages=max(0, min(_int(env.get("WEB_SEARCH_FETCH_PAGES"), 2), 3)),
            cache_ttl=max(60, _int(env.get("WEB_SEARCH_CACHE_TTL"), 15 * 60)),
            cache_size=max(8, _int(env.get("WEB_SEARCH_CACHE_SIZE"), 128)),
            page_char_limit=max(1000, _int(env.get("WEB_SEARCH_PAGE_CHARS"), 6000)),
            show_sources=_bool(env.get("WEB_SEARCH_SHOW_SOURCES"), True),
        )


class AIModelSpec:
    """One OpenAI-compatible model and the role it is best at.

    Roles: ``fast`` (labels, quips, anything latency-sensitive) and
    ``deep`` (long answers, recaps, roasts). A model registered without a
    role serves both.
    """

    __slots__ = ("name", "role")

    def __init__(self, name: str, role: str = "") -> None:
        self.name = str(name or "").strip()
        self.role = str(role or "").strip().casefold()

    @property
    def valid(self) -> bool:
        return bool(self.name) and self.role in ("", "fast", "deep")

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"AIModelSpec({self.name!r}, {self.role!r})"


def parse_ai_models(raw: str) -> list[AIModelSpec]:
    """Parse ``AI_MODELS``: "fast:gpt-4o-mini, deep:gpt-4o, llama3".

    Entries may be ``role:model`` or a bare ``model``. Junk is dropped
    rather than guessed at; duplicates keep their first position.
    """
    specs: list[AIModelSpec] = []
    seen: set[str] = set()
    for entry in str(raw or "").replace(";", ",").split(","):
        entry = entry.strip()
        if not entry:
            continue
        role, _, name = entry.rpartition(":")
        if not name:  # "model:" with nothing after it
            continue
        if role and ":" in name:
            # A model name that itself contains ':' — the first segment is
            # the role, everything after it is the model.
            role, _, name = entry.partition(":")
        spec = AIModelSpec(name, role) if role else AIModelSpec(entry)
        if not spec.valid or spec.name.casefold() in seen:
            continue
        seen.add(spec.name.casefold())
        specs.append(spec)
    return specs


@dataclass(frozen=True, slots=True)
class GeminiConfig:
    """Google Gemini — the optional AI fallback provider (see functions/gemini.py).

    Entirely optional: with no key the bot still answers definitions,
    conversions, calculations, weather, time and
    more — the OpenAI-compatible provider answers first and Gemini is only
    the fallback reasoning layer.

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

    # Gemini API key support (optional AI fallback provider).
    gemini: GeminiConfig = GeminiConfig()

    # OpenAI-compatible chat completion endpoint (the primary provider).
    ai_base_url: str = ""
    ai_api_key: str = ""
    ai_model: str = ""
    # Optional extra models on the same endpoint, each tagged with a role:
    # "fast:gpt-4o-mini,deep:gpt-4o". A bare model name takes the default
    # role. Paradise routes by role (labels and quips → fast, long answers,
    # recaps and roasts → deep) and fails over down the list, so several
    # models can share one key. Unset → the single AI_MODEL does everything.
    ai_models: list[AIModelSpec] = field(default_factory=list)
    # Optional extra models on the same endpoint, each tagged with a role:
    # "fast:gpt-4o-mini,deep:gpt-4o". A bare model name takes the default
    # role. Paradise routes by role (labels and quips → fast, long answers,
    # recaps and roasts → deep) and fails over down the list, so several
    # models can share one key. Unset → the single AI_MODEL does everything.
    ai_models: list[AIModelSpec] = field(default_factory=list)

    # Rare passive personality (reactions / short replies).
    personality: PersonalityConfig = PersonalityConfig()

    # Dictionary lookups, cards and pronunciation.
    dictionary: DictionaryConfig = DictionaryConfig()

    # The owner's ElevenLabs voice — used to pronounce words in the dictionary.
    # Entirely optional; nothing else speaks.
    elevenlabs: ElevenLabsConfig = ElevenLabsConfig()

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

    # Web image search for mention requests ("show me a picture of …").
    image_search: ImageSearchConfig = ImageSearchConfig()

    # Web research: search, source-backed answers, link summaries.
    web_search: WebSearchConfig = WebSearchConfig()

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

    # The lords and masters, all at the same level of authority: always
    # addressed as "my lord", still roastable, and the only users allowed to
    # manage the bot's long-term memories. Stupid (the owner) and Taher.
    lord_user_ids: tuple[int, ...] = (922359954025885737, 1049231767585964032)

    # The dead-server / everyone-in-VC watcher.

    # Long-term memory (/memory) — a tiny SQLite database of things the lord
    # asked Paradise to remember, folded into AI replies. When the Supabase
    # project is configured (the same SUPABASE_URL/SUPABASE_SECRET_KEY as the
    # voice tracker), memories are also mirrored there — see
    # supabase/migrations/005_memory.sql.
    memory_db_path: str = ""
    memory_max_entries: int = 200
    # How often the Supabase memory mirror reconciles, in seconds.
    memory_supabase_interval: int = 120
    # Per-request deadline for the memory mirror, in seconds.
    memory_supabase_timeout: float = 10.0

    @property
    def lord_user_id(self) -> int:
        """The first lord (legacy name; see ``lord_user_ids``)."""
        return self.lord_user_ids[0] if self.lord_user_ids else 0

    def is_lord(self, user_id: int | None) -> bool:
        """True when ``user_id`` is one of the lords and masters."""
        return bool(user_id) and int(user_id) in self.lord_user_ids

    @property
    def ai_configured(self) -> bool:
        # The OpenAI-compatible endpoint is the primary provider; Gemini is
        # the fallback (a base URL alone is enough for local providers e.g.
        # Ollama).
        return bool(self.ai_base_url and self.ai_model) or self.gemini.configured

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "Config":
        env = os.environ if environ is None else environ
        return cls(
            token=env.get("DISCORD_TOKEN", "").strip(),
            gemini=GeminiConfig.from_env(env),
            ai_base_url=env.get("AI_BASE_URL", "").strip().rstrip("/"),
            ai_api_key=env.get("AI_API_KEY", "").strip(),
            ai_model=env.get("AI_MODEL", "").strip(),
            ai_models=parse_ai_models(env.get("AI_MODELS", "")),
            personality=PersonalityConfig.from_env(env),
            dictionary=DictionaryConfig.from_env(env),
            elevenlabs=ElevenLabsConfig.from_env(env),
            voice_presence=VoicePresenceConfig.from_env(env),
            autoupdate=AutoUpdateConfig.from_env(env),
            voice=VoiceConfig.from_env(env),
            voice_clip=VoiceClipConfig.from_env(env),
            minigames=MiniGamesConfig.from_env(env),
            image_search=ImageSearchConfig.from_env(env),
            web_search=WebSearchConfig.from_env(env),
            prewarm=_bool(env.get("PREWARM_ON_START"), True),
            guild_id=max(0, _int(env.get("DISCORD_GUILD_ID"), 0)),
            edit_user_ids=_ids(env.get("EDIT_ALLOWED_USER_IDS")),
            clip_user_ids=_ids(env.get("CLIP_ALLOWED_USER_IDS")),
            deaf_exempt_user_ids=_ids(env.get("VOICE_DEAF_EXEMPT_USER_IDS")),
            lord_user_ids=_lord_ids_from_env(env),
            memory_db_path=(
                str(env.get("MEMORY_DB_PATH", "") or "").strip()
                or os.path.join(
                    os.path.dirname(
                        str(env.get("VOICE_DB_PATH", "") or "").strip()
                        or os.path.join("data", "paradise.db")
                    ),
                    "memory.db",
                )
            ),
            memory_max_entries=max(
                10, _int(env.get("MEMORY_MAX_ENTRIES"), 200)
            ),
            memory_supabase_interval=max(
                15, _int(env.get("MEMORY_SUPABASE_INTERVAL_SECONDS"), 120)
            ),
            memory_supabase_timeout=max(
                3.0, _float(env.get("MEMORY_SUPABASE_TIMEOUT"), 10.0)
            ),
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
