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
    """Tuning for Paradise's rare passive personality (see functions/personality.py).

    All chances are rolled only on messages that actually match a known
    pattern — most messages never even qualify. Keep these conservative:
    the goal is "LMAO why did Paradise just say that?", never spam.
    """

    enabled: bool = True
    # Chance to add a contextual reaction to a matching message.
    reaction_chance: float = 0.045
    # Chance to send a short contextual reply instead (rarer; never both).
    reply_chance: float = 0.015
    # Seconds between ANY two personality actions, bot-wide.
    global_cooldown: int = 45
    # Seconds between personality actions in the same channel.
    channel_cooldown: int = 120
    # Extra spacing for replies in the same channel (replies are the rare treat).
    reply_channel_cooldown: int = 600

    @classmethod
    def from_env(cls, env) -> "PersonalityConfig":
        return cls(
            enabled=_bool(env.get("PERSONALITY_ENABLED"), True),
            reaction_chance=_chance(env.get("PERSONALITY_REACTION_CHANCE"), 0.045),
            reply_chance=_chance(env.get("PERSONALITY_REPLY_CHANCE"), 0.015),
            global_cooldown=_int(env.get("PERSONALITY_GLOBAL_COOLDOWN"), 45),
            channel_cooldown=_int(env.get("PERSONALITY_CHANNEL_COOLDOWN"), 120),
            reply_channel_cooldown=_int(
                env.get("PERSONALITY_REPLY_CHANNEL_COOLDOWN"), 600
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


@dataclass(frozen=True, slots=True)
class VoiceConfig:
    """Voice activity tracking and daily DM reports configuration."""

    db_path: str = ""
    dm_reports_enabled: bool = True

    @classmethod
    def from_env(cls, env) -> "VoiceConfig":
        return cls(
            db_path=env.get("VOICE_DB_PATH", "").strip(),
            dm_reports_enabled=_bool(env.get("VOICE_DM_REPORTS_ENABLED"), True),
        )


@dataclass(frozen=True, slots=True)
class EntertainmentConfig:
    """The movies/anime/series discovery system (see functions/entertainment/).

    Works with zero configuration: every source (IMDb suggestions, TVMaze,
    Kitsu, AniList, MyAnimeList via Jikan, Wikipedia/Wikidata, Apple's
    iTunes search, DuckDuckGo) is key-less. Everything below is optional
    tuning.
    """

    # Answer plain-chat requests ("recommend anime like X", "what's trending?").
    text_triggers: bool = True
    # Per-request deadline for every outbound entertainment call.
    timeout: float = 8.0
    # How long a full title card stays cached before a fresh lookup.
    stable_cache_ttl: int = 24 * 3600
    # How long volatile facts (ratings, streaming, status) stay fresh.
    volatile_cache_ttl: int = 45 * 60
    # Bounded number of cached cards.
    cache_size: int = 256

    @classmethod
    def from_env(cls, env) -> "EntertainmentConfig":
        return cls(
            text_triggers=_bool(env.get("ENTERTAINMENT_TEXT_TRIGGERS"), True),
            timeout=max(3.0, _float(env.get("ENTERTAINMENT_TIMEOUT"), 8.0)),
            stable_cache_ttl=max(600, _int(env.get("ENTERTAINMENT_STABLE_TTL"), 24 * 3600)),
            volatile_cache_ttl=max(60, _int(env.get("ENTERTAINMENT_VOLATILE_TTL"), 45 * 60)),
            cache_size=max(16, _int(env.get("ENTERTAINMENT_CACHE_SIZE"), 256)),
        )


@dataclass(frozen=True, slots=True)
class GeminiConfig:
    """Google Gemini — the optional AI reasoning layer (see functions/gemini.py).

    Entirely optional: with no key the bot still answers definitions,
    conversions, calculations, weather, time, search, entertainment and
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

    # Movies / anime / series discovery (/example, /trending, /upcoming…).
    entertainment: EntertainmentConfig = EntertainmentConfig()

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
            entertainment=EntertainmentConfig.from_env(env),
        )


def load_config() -> Config:
    """Load .env (if present) and parse the environment."""
    try:
        from dotenv import load_dotenv  # optional at runtime

        load_dotenv()
    except ImportError:  # pragma: no cover - dotenv is in requirements
        pass
    return Config.from_env()
