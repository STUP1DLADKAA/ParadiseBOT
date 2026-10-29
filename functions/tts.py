"""Paradise's voice: the owner's ElevenLabs voice first, free voices last.

One job only — turn a sentence into a small audio clip that can be played
straight into a voice channel or attached to a message.

Provider order
--------------
1. **ElevenLabs** (``ELEVENLABS_API_KEY`` + ``ELEVENLABS_VOICE_ID``) — the
   owner's custom male voice, multilingual (Hindi + English). The default
   model is ``eleven_flash_v2_5``: the fastest multilingual model, so a short
   reply is ready in a fraction of a second. Nothing is pitch-shifted or
   filtered: this is the actual voice the owner configured.
2. **OpenAI-compatible** ``/audio/speech`` (``SPEECH_*``) — optional.
3. **Free web voice** (Google translate TTS) — key-less last resort so the
   channel is never silent when a key expires.

Design rules
------------
- The API key lives in the ``xi-api-key`` header only; it never appears in a
  URL, a log line, a Discord message or an error string.
- Every request has a deadline, a byte cap and one bounded retry.
- Clips are cached in memory (bounded) and — for preloaded lines such as the
  rude refusal — on disk, so a repeat is instant even after a restart.
- A provider failure is never fatal: the next provider is tried and, if all
  fail, :class:`SpeechUnavailable` tells the caller to stay text-only.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Protocol

import aiohttp

from functions.helpers import ParadiseError

log = logging.getLogger(__name__)

ELEVENLABS_API_BASE = "https://api.elevenlabs.io/v1"
WEB_VOICE_URL = "https://translate.google.com/translate_tts"
WEB_VOICE_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

MAX_CLIP_BYTES = 4 * 1024 * 1024
CACHE_ITEMS = 128
CACHE_BYTES = 32 * 1024 * 1024
DISK_CACHE_ENTRIES = 64

# ElevenLabs models we can fall back to when the configured one is not
# available on the owner's plan. Ordered fastest → highest quality.
_MODEL_FALLBACKS: tuple[str, ...] = (
    "eleven_flash_v2_5",
    "eleven_turbo_v2_5",
    "eleven_multilingual_v2",
)

_DEVANAGARI = re.compile(r"[\u0900-\u097F]")
_MARKDOWN = re.compile(r"[*_`~#>|]+")
_URL = re.compile(r"https?://\S+")
_WHITESPACE = re.compile(r"\s+")

_AUDIO_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"ID3", "mp3"),
    (b"\xff\xfb", "mp3"),
    (b"\xff\xf3", "mp3"),
    (b"\xff\xf2", "mp3"),
    (b"\xff\xe3", "mp3"),
    (b"OggS", "ogg"),
    (b"RIFF", "wav"),
    (b"fLaC", "flac"),
)


class SpeechUnavailable(ParadiseError):
    """No provider could produce audio right now. Safe to show to a user."""


@dataclass(frozen=True, slots=True)
class SpeechClip:
    """A short piece of synthesised speech held in memory."""

    data: bytes
    extension: str = "mp3"
    provider: str = ""
    text: str = ""
    detail: str = field(default="")

    @property
    def size(self) -> int:
        return len(self.data)


def looks_like_audio(data: bytes) -> str:
    """Return a file extension when the bytes really are audio, else ''."""
    if len(data) < 256:
        return ""
    for magic, extension in _AUDIO_MAGIC:
        if data.startswith(magic):
            return extension
    if len(data) > 12 and data[4:8] == b"ftyp":
        return "m4a"
    return ""


def speakable_text(text: str, *, max_chars: int = 600) -> str:
    """Flatten *text* into something a TTS engine reads naturally.

    Markdown, links and emoji are stripped so the voice never says
    "asterisk asterisk important asterisk asterisk".
    """
    cleaned = _URL.sub("", str(text or ""))
    cleaned = _MARKDOWN.sub("", cleaned)
    cleaned = cleaned.replace("\u200b", " ").replace("\n", " ")
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    if len(cleaned) <= max_chars:
        return cleaned
    cut = cleaned[:max_chars]
    # Prefer ending on a sentence boundary.
    boundary = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if boundary > max_chars * 0.5:
        return cut[: boundary + 1].strip()
    return cut.rsplit(" ", 1)[0].strip()


def detect_language(text: str) -> str:
    """A coarse language tag. Devanagari → Hindi, everything else → English."""
    return "hi" if _DEVANAGARI.search(str(text or "")) else "en"


class SpeechProvider(Protocol):
    name: str

    @property
    def available(self) -> bool: ...

    async def speak(self, text: str, *, language: str) -> SpeechClip | None: ...


# ---------------------------------------------------------------------------
# ElevenLabs — the owner's voice
# ---------------------------------------------------------------------------


class ElevenLabsVoice:
    """Thin, defensive client for ElevenLabs' text-to-speech endpoint.

    One shared :class:`aiohttp.ClientSession` per service; the key travels in
    the ``xi-api-key`` header only.
    """

    name = "elevenlabs"

    def __init__(
        self,
        config=None,
        *,
        session: aiohttp.ClientSession | None = None,
        api_key: str = "",
        voice_id: str = "",
        model: str = "",
        output_format: str = "",
        timeout: float = 25.0,
    ) -> None:
        self._config = config
        self._session = session
        self._owns_session = session is None
        self._api_key = (api_key or getattr(config, "api_key", "") or "").strip()
        self._voice_id = (voice_id or getattr(config, "voice_id", "") or "").strip()
        self._model = (
            model or getattr(config, "model", "") or "eleven_flash_v2_5"
        ).strip()
        self._output_format = (
            output_format or getattr(config, "output_format", "") or "mp3_44100_128"
        ).strip()
        self._timeout = float(timeout or getattr(config, "timeout", 25.0) or 25.0)
        # Models that already failed with this key (avoids repeat round-trips).
        self._dead_models: set[str] = set()
        self._resolved_model: str = ""

    # -- capability ---------------------------------------------------------

    @property
    def available(self) -> bool:
        return bool(self._api_key and self._voice_id)

    @property
    def voice_id(self) -> str:
        return self._voice_id

    @property
    def model(self) -> str:
        return self._resolved_model or self._model

    def describe(self) -> str:
        if not self.available:
            return "not configured"
        return f"voice {self._voice_id[:6]}… · {self.model} · {self._output_format}"

    # -- request ------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "xi-api-key": self._api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }

    def _voice_settings(self, *, minimal: bool = False) -> dict[str, object]:
        """Voice shaping. ``minimal`` is the retry-safe subset.

        Some models/plans reject the optional knobs (``speed``, ``style``) with
        a 422; the retry with only ``stability`` + ``similarity_boost`` always
        works and still sounds like the configured voice.
        """
        cfg = self._config
        settings: dict[str, object] = {
            "stability": float(getattr(cfg, "stability", 0.45) or 0.45),
            "similarity_boost": float(getattr(cfg, "similarity_boost", 0.85) or 0.85),
        }
        if minimal:
            return settings
        style = float(getattr(cfg, "style", 0.0) or 0.0)
        speed = float(getattr(cfg, "speed", 1.0) or 1.0)
        if style > 0:
            settings["style"] = style
        if abs(speed - 1.0) > 0.01:
            settings["speed"] = speed
        settings["use_speaker_boost"] = True
        return settings

    def _payload(
        self, text: str, language: str, *, model: str = "", minimal: bool = False
    ) -> dict[str, object]:
        target = model or self.model
        payload: dict[str, object] = {
            "text": text,
            "model_id": target,
            "voice_settings": self._voice_settings(minimal=minimal),
        }
        forced = str(getattr(self._config, "language_code", "") or "").strip()
        if forced:
            payload["language_code"] = forced
        elif language in {"hi", "en"} and _supports_language_code(target):
            # Flash/Turbo v2.5 accept an ISO-639-1 hint; it makes Hinglish
            # sentences switch cleanly between the two languages.
            payload["language_code"] = language
        return payload

    async def speak(self, text: str, *, language: str = "en") -> SpeechClip | None:
        if not self.available:
            return None
        cleaned = speakable_text(text)
        if not cleaned:
            return None

        models = [self._model] + [
            model for model in _MODEL_FALLBACKS if model != self._model
        ] + sorted(self._dead_models - {self._model})
        models = [model for model in models if model not in self._dead_models]
        # A previously-resolved working model goes first after a success.
        if self._resolved_model and self._resolved_model not in self._dead_models:
            models = [self._resolved_model] + [
                model for model in models if model != self._resolved_model
            ]
        last_error: Exception | None = None

        for model in models:
            for minimal in (False, True):
                started = time.perf_counter()
                try:
                    data = await self._request(
                        cleaned, language=language, model=model, minimal=minimal
                    )
                except SpeechUnavailable as error:
                    last_error = error
                    message = str(error).lower()
                    needs_minimal = (
                        not minimal
                        and (
                            "voice_settings" in message
                            or "speed" in message
                            or "style" in message
                            or "422" in message
                        )
                    )
                    if needs_minimal:
                        continue  # same model, safe settings
                    if "model" in message or "400" in message or "404" in message:
                        # This key's plan cannot use the model — stop asking.
                        self._dead_models.add(model)
                    break  # move to the next model
                extension = looks_like_audio(data)
                if not extension:
                    last_error = SpeechUnavailable("ElevenLabs returned non-audio bytes")
                    break
                self._resolved_model = model
                log.info(
                    "ElevenLabs spoke %d chars with %s in %dms (%d bytes)",
                    len(cleaned),
                    model,
                    int((time.perf_counter() - started) * 1000),
                    len(data),
                )
                return SpeechClip(
                    data=data,
                    extension=extension,
                    provider=self.name,
                    text=cleaned,
                    detail=f"elevenlabs {model}",
                )

        if last_error is not None:
            log.warning("ElevenLabs synthesis failed: %s", last_error)
        return None

    async def _request(
        self,
        text: str,
        *,
        language: str,
        model: str,
        minimal: bool = False,
    ) -> bytes:
        url = (
            f"{ELEVENLABS_API_BASE}/text-to-speech/{self._voice_id}"
            f"?output_format={self._output_format}"
        )
        session = await self._get_session()
        try:
            async with session.post(
                url,
                headers=self._headers(),
                json=self._payload(text, language, model=model, minimal=minimal),
            ) as response:
                if response.status != 200:
                    body = (await response.text())[:200]
                    body = body.replace(self._api_key, "[redacted]")
                    log.warning(
                        "ElevenLabs returned %s (model=%s): %s",
                        response.status,
                        model,
                        body,
                    )
                    raise SpeechUnavailable(
                        f"ElevenLabs returned HTTP {response.status} (model {model})"
                    )
                data = await response.read()
        except SpeechUnavailable:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
            raise SpeechUnavailable(
                f"ElevenLabs request failed: {error.__class__.__name__}"
            ) from error
        if len(data) > MAX_CLIP_BYTES:
            raise SpeechUnavailable("ElevenLabs returned an oversized clip")
        if not data:
            raise SpeechUnavailable("ElevenLabs returned an empty clip")
        return data

    # -- plumbing -----------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or (self._owns_session and self._session.closed):
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout),
                connector=aiohttp.TCPConnector(limit=16, ttl_dns_cache=300),
            )
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()


def _supports_language_code(model: str) -> bool:
    model = (model or "").lower()
    return "_v2_5" in model or "turbo" in model or "flash" in model


# ---------------------------------------------------------------------------
# Optional OpenAI-compatible provider (same endpoint the dictionary uses)
# ---------------------------------------------------------------------------


class OpenAISpeechVoice:
    """Any OpenAI-compatible ``POST /audio/speech`` endpoint. Optional."""

    name = "openai"

    def __init__(
        self,
        *,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        voice: str = "",
        timeout: float = 20.0,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._base_url = (base_url or "").strip().rstrip("/")
        self._api_key = (api_key or "").strip()
        self._model = (model or "").strip()
        self._voice = (voice or "").strip() or "onyx"
        self._timeout = timeout
        self._session = session
        self._owns_session = session is None

    @property
    def available(self) -> bool:
        return bool(self._base_url and self._model)

    async def speak(self, text: str, *, language: str = "en") -> SpeechClip | None:
        if not self.available:
            return None
        cleaned = speakable_text(text)
        if not cleaned:
            return None
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload = {
            "model": self._model,
            "voice": self._voice,
            "input": cleaned,
            "response_format": "mp3",
            "speed": 1.0,
        }
        session = await self._get_session()
        try:
            async with session.post(
                f"{self._base_url}/audio/speech", headers=headers, json=payload
            ) as response:
                if response.status != 200:
                    body = (await response.text())[:160]
                    if self._api_key:
                        body = body.replace(self._api_key, "[redacted]")
                    log.warning("Speech endpoint returned %s: %s", response.status, body)
                    return None
                data = await response.read()
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            log.warning("Speech endpoint request failed", exc_info=True)
            return None
        extension = looks_like_audio(data)
        if not extension:
            return None
        return SpeechClip(
            data=data, extension=extension, provider=self.name, text=cleaned,
            detail=f"neural voice ({self._voice})",
        )

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or (self._owns_session and self._session.closed):
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout)
            )
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()


# ---------------------------------------------------------------------------
# Free web voice — the always-there fallback
# ---------------------------------------------------------------------------


class WebVoice:
    """Key-less Google translate voice. Used only when nothing else works."""

    name = "web"

    def __init__(self, *, timeout: float = 10.0) -> None:
        self._timeout = timeout
        self._session: aiohttp.ClientSession | None = None

    @property
    def available(self) -> bool:
        return True

    async def speak(self, text: str, *, language: str = "en") -> SpeechClip | None:
        cleaned = speakable_text(text)
        if not cleaned:
            return None
        session = await self._get_session()
        params = {
            "ie": "UTF-8",
            "q": cleaned,
            "tl": "hi" if language == "hi" else "en",
            "total": 1,
            "idx": 0,
            "textlen": len(cleaned),
            "client": "tw-ob",
        }
        headers = {
            "User-Agent": WEB_VOICE_AGENT,
            "Referer": "https://translate.google.com/",
        }
        try:
            async with session.get(
                WEB_VOICE_URL, params=params, headers=headers
            ) as response:
                if response.status != 200:
                    return None
                data = await response.read()
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            log.debug("Free web voice request failed", exc_info=True)
            return None
        extension = looks_like_audio(data)
        if not extension:
            return None
        return SpeechClip(
            data=data, extension=extension, provider=self.name, text=cleaned,
            detail="free web voice",
        )

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout)
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


class SpeechService:
    """Owns the provider chain, the caches and the preloaded lines."""

    def __init__(
        self,
        elevenlabs=None,
        *,
        openai_compatible=None,
        web=None,
        cache_dir: str = "",
        providers: list[SpeechProvider] | None = None,
    ) -> None:
        self.elevenlabs = elevenlabs
        if providers is None:
            providers = [
                provider
                for provider in (
                    elevenlabs,
                    openai_compatible,
                    web if web is not None else WebVoice(),
                )
                if provider is not None
            ]
        self.providers: list[SpeechProvider] = list(providers)
        self.cache_dir = cache_dir.strip()
        self._cache: OrderedDict[tuple[str, str], SpeechClip] = OrderedDict()
        self._cache_bytes = 0
        self._preloaded: dict[str, SpeechClip] = {}

    # -- introspection ------------------------------------------------------

    @property
    def available(self) -> bool:
        return any(provider.available for provider in self.providers)

    @property
    def provider_names(self) -> tuple[str, ...]:
        return tuple(p.name for p in self.providers if p.available)

    @property
    def using_elevenlabs(self) -> bool:
        return bool(self.elevenlabs is not None and self.elevenlabs.available)

    def describe(self) -> str:
        if not self.providers:
            return "speech: none"
        names = ", ".join(self.provider_names) or "none"
        return f"speech: {names}"

    # -- main entry point ---------------------------------------------------

    async def synthesize(
        self, text: str, *, language: str = "", use_cache: bool = True
    ) -> SpeechClip:
        """Synthesise *text*, trying every provider in order."""
        cleaned = speakable_text(text)
        if not cleaned:
            raise SpeechUnavailable("there's nothing to say there.")
        tag = language or detect_language(cleaned)
        key = (cleaned, tag)

        if use_cache:
            cached = self._cache_get(key)
            if cached is not None:
                return cached

        errors: list[str] = []
        for provider in self.providers:
            if not provider.available:
                continue
            try:
                clip = await provider.speak(cleaned, language=tag)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - one bad provider is not fatal
                errors.append(f"{provider.name}: {error.__class__.__name__}")
                log.debug("Speech provider %s failed", provider.name, exc_info=True)
                continue
            if clip is None or not clip.data:
                errors.append(f"{provider.name}: no audio")
                continue
            self._cache_put(key, clip)
            return clip

        if errors:
            log.info("No speech for %r — %s", cleaned[:60], "; ".join(errors[:3]))
        raise SpeechUnavailable("i can't speak right now — text still works.")

    # -- preloading (the rude refusal line) ---------------------------------

    async def preload(self, key: str, text: str, *, language: str = "") -> SpeechClip | None:
        """Synthesise *text* once and keep it (memory + disk) for later use."""
        disk = self._disk_path(key)
        if disk:
            clip = self._read_disk(disk, text)
            if clip is not None:
                self._preloaded[key] = clip
                log.info("Preloaded speech line '%s' from disk cache", key)
                return clip
        try:
            clip = await self.synthesize(text, language=language)
        except SpeechUnavailable:
            log.info("Could not preload speech line '%s'", key)
            return None
        self._preloaded[key] = clip
        if disk:
            self._write_disk(disk, clip)
            log.info("Preloaded speech line '%s' (%d bytes)", key, clip.size)
        return clip

    def preloaded(self, key: str) -> SpeechClip | None:
        return self._preloaded.get(key)

    async def speak_preloaded(self, key: str, text: str, *, language: str = "") -> SpeechClip | None:
        """Return the preloaded clip, or synthesise it now if preloading failed."""
        clip = self._preloaded.get(key)
        if clip is not None:
            return clip
        return await self.preload(key, text, language=language)

    # -- caches -------------------------------------------------------------

    def _cache_get(self, key: tuple[str, str]) -> SpeechClip | None:
        clip = self._cache.get(key)
        if clip is None:
            return None
        self._cache.move_to_end(key)
        return clip

    def _cache_put(self, key: tuple[str, str], clip: SpeechClip) -> None:
        if clip.size > CACHE_BYTES:
            return
        old = self._cache.pop(key, None)
        if old is not None:
            self._cache_bytes -= old.size
        self._cache[key] = clip
        self._cache_bytes += clip.size
        while len(self._cache) > CACHE_ITEMS or self._cache_bytes > CACHE_BYTES:
            _, evicted = self._cache.popitem(last=False)
            self._cache_bytes -= evicted.size

    def _disk_path(self, key: str) -> str:
        if not self.cache_dir:
            return ""
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
        except OSError:
            return ""
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        return os.path.join(self.cache_dir, f"{digest}.audio")

    @staticmethod
    def _read_disk(path: str, text: str) -> SpeechClip | None:
        try:
            with open(path, "rb") as handle:
                data = handle.read(MAX_CLIP_BYTES + 1)
        except OSError:
            return None
        if not data or len(data) > MAX_CLIP_BYTES:
            return None
        extension = looks_like_audio(data)
        if not extension:
            return None
        return SpeechClip(
            data=data, extension=extension, provider="preloaded", text=text,
            detail="preloaded voice line",
        )

    @staticmethod
    def _write_disk(path: str, clip: SpeechClip) -> None:
        tmp = f"{path}.{os.getpid()}.tmp"
        try:
            with open(tmp, "wb") as handle:
                handle.write(clip.data)
            os.replace(tmp, path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    async def close(self) -> None:
        for provider in self.providers:
            close = getattr(provider, "close", None)
            if close is None:
                continue
            try:
                await close()
            except Exception:  # noqa: BLE001 - shutdown is best effort
                log.debug("Closing speech provider %s failed", provider.name)


def build_speech_service(config, *, cache_dir: str = "") -> SpeechService:
    """Wire the configured providers into one :class:`SpeechService`."""
    eleven = ElevenLabsVoice(getattr(config, "elevenlabs", None))
    dictionary = getattr(config, "dictionary", None)
    openai_voice = OpenAISpeechVoice(
        base_url=getattr(dictionary, "speech_base_url", "") or "",
        api_key=getattr(dictionary, "speech_api_key", "") or "",
        model=getattr(dictionary, "speech_model", "") or "",
        voice=getattr(dictionary, "speech_voice", "") or "",
        timeout=float(getattr(dictionary, "timeout", 8.0) or 8.0) * 2,
    )
    return SpeechService(
        eleven,
        openai_compatible=openai_voice if openai_voice.available else None,
        web=WebVoice(timeout=float(getattr(dictionary, "timeout", 8.0) or 8.0) + 2),
        cache_dir=cache_dir,
    )


__all__ = [
    "ELEVENLABS_API_BASE",
    "ElevenLabsVoice",
    "OpenAISpeechVoice",
    "SpeechClip",
    "SpeechService",
    "SpeechUnavailable",
    "WebVoice",
    "build_speech_service",
    "detect_language",
    "looks_like_audio",
    "speakable_text",
]
