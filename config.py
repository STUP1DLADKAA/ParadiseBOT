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
class Config:
    """Everything Paradise reads from the environment."""

    token: str = ""

    # OpenAI-compatible chat completion endpoint (optional).
    ai_base_url: str = ""
    ai_api_key: str = ""
    ai_model: str = ""

    # Rare passive personality (reactions / short replies).
    personality: PersonalityConfig = PersonalityConfig()

    # Dictionary lookups, cards and pronunciation.
    dictionary: DictionaryConfig = DictionaryConfig()

    @property
    def ai_configured(self) -> bool:
        # A base URL alone is enough for local providers (e.g. Ollama).
        return bool(self.ai_base_url and self.ai_model)

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "Config":
        env = os.environ if environ is None else environ
        return cls(
            token=env.get("DISCORD_TOKEN", "").strip(),
            ai_base_url=env.get("AI_BASE_URL", "").strip().rstrip("/"),
            ai_api_key=env.get("AI_API_KEY", "").strip(),
            ai_model=env.get("AI_MODEL", "").strip(),
            personality=PersonalityConfig.from_env(env),
            dictionary=DictionaryConfig.from_env(env),
        )


def load_config() -> Config:
    """Load .env (if present) and parse the environment."""
    try:
        from dotenv import load_dotenv  # optional at runtime

        load_dotenv()
    except ImportError:  # pragma: no cover - dotenv is in requirements
        pass
    return Config.from_env()
