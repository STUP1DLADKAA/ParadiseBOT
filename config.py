"""Central environment configuration for Paradise.

Every secret or external service credential lives here and only here.
A safe template is provided in ``.env.example``.
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


@dataclass(frozen=True, slots=True)
class Config:
    """Everything Paradise reads from the environment."""

    token: str = ""

    # Lavalink node (music + voice playback). Music is disabled when unset.
    lavalink_host: str = ""
    lavalink_port: int = 2333
    lavalink_password: str = ""
    lavalink_secure: bool = False
    lavalink_region: str = "in"

    # OpenAI-compatible chat completion endpoint (optional).
    ai_base_url: str = ""
    ai_api_key: str = ""
    ai_model: str = ""

    # Text-to-speech (edge-tts neural voices, free).
    tts_enabled: bool = True
    tts_voice_hi: str = "hi-IN-MadhurNeural"
    tts_voice_en: str = "en-IN-PrabhatNeural"
    tts_max_length: int = 220
    tts_user_cooldown: int = 30
    tts_global_cooldown: int = 8

    # Music behaviour.
    music_max_volume: int = 150
    auto_disconnect_minutes: int = 10

    # Optional integrations.
    lyrics_enabled: bool = True

    data_dir: str = "data"

    @property
    def music_configured(self) -> bool:
        return bool(self.lavalink_host and self.lavalink_password)

    @property
    def ai_configured(self) -> bool:
        # A base URL alone is enough for local providers (e.g. Ollama).
        return bool(self.ai_base_url and self.ai_model)

    @property
    def tts_configured(self) -> bool:
        return self.tts_enabled and self.music_configured

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "Config":
        env = os.environ if environ is None else environ
        return cls(
            token=env.get("DISCORD_TOKEN", "").strip(),
            lavalink_host=env.get("LAVALINK_HOST", "").strip(),
            lavalink_port=_int(env.get("LAVALINK_PORT"), 2333),
            lavalink_password=env.get("LAVALINK_PASSWORD", "").strip(),
            lavalink_secure=_bool(env.get("LAVALINK_SECURE"), False),
            lavalink_region=env.get("LAVALINK_REGION", "in").strip() or "in",
            ai_base_url=env.get("AI_BASE_URL", "").strip().rstrip("/"),
            ai_api_key=env.get("AI_API_KEY", "").strip(),
            ai_model=env.get("AI_MODEL", "").strip(),
            tts_enabled=_bool(env.get("TTS_ENABLED"), True),
            tts_voice_hi=env.get("TTS_VOICE_HI", "hi-IN-MadhurNeural").strip(),
            tts_voice_en=env.get("TTS_VOICE_EN", "en-IN-PrabhatNeural").strip(),
            tts_max_length=_int(env.get("TTS_MAX_LENGTH"), 220),
            tts_user_cooldown=_int(env.get("TTS_USER_COOLDOWN"), 30),
            tts_global_cooldown=_int(env.get("TTS_GLOBAL_COOLDOWN"), 8),
            music_max_volume=_int(env.get("MUSIC_MAX_VOLUME"), 150),
            auto_disconnect_minutes=_int(env.get("AUTO_DISCONNECT_MINUTES"), 10),
            lyrics_enabled=_bool(env.get("LYRICS_ENABLED"), True),
            data_dir=env.get("PARADISE_DATA_DIR", "data").strip() or "data",
        )


def load_config() -> Config:
    """Load .env (if present) and parse the environment."""
    try:
        from dotenv import load_dotenv  # optional at runtime

        load_dotenv()
    except ImportError:  # pragma: no cover - dotenv is in requirements
        pass
    return Config.from_env()
