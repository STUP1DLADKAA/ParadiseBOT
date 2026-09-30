"""Saying the word out loud.

A deliberately small service: one word in, one short audio clip out. It is
not a music system and has no queue, no players and no state beyond a
bounded cache.

Providers are tried in order and the first one that returns audio wins:

1. :class:`NativeRecordingProvider` — the real human recording the
   dictionary already links to (regional: ``-uk``, ``-us``, ``-au``).
   Always preferred: nothing synthesised beats a native speaker.
2. :class:`NeuralSpeechProvider` — any OpenAI-compatible ``/audio/speech``
   endpoint (OpenAI, a local server, whatever is configured). Optional;
   skipped entirely when unconfigured.
3. :class:`WebVoiceProvider` — a free, key-less neural web voice, so the
   button works out of the box with no setup at all.

Guarantees that matter:

- only the headword is ever sent to a provider, never a definition;
- every clip is verified to actually be audio before it is handed back;
- clips live in memory (no temporary files to leak or clean up) and are
  cached by word + language + region + provider;
- identical concurrent requests are coalesced;
- a provider failure moves to the next one; total failure raises a single
  friendly error the cog can show while keeping the card usable.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Protocol

from functions.helpers import ParadiseError

from .cache import SingleFlight, TTLCache
from .http import HttpClient, HttpError
from .models import Clip, Pronunciation, SpeechRequest, WordEntry

log = logging.getLogger(__name__)

WEB_VOICE_URL = "https://translate.google.com/translate_tts"
WEB_VOICE_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

MAX_CLIP_BYTES = 2 * 1024 * 1024
CLIP_TTL = 24 * 3600.0
CACHE_ITEMS = 96
CACHE_BYTES = 24 * 1024 * 1024
MAX_SPOKEN_CHARS = 64

# Regional voices the free web endpoint understands.
_WEB_VOICE_LOCALES = {
    "us": "en-US",
    "uk": "en-GB",
    "gb": "en-GB",
    "au": "en-AU",
    "in": "en-IN",
    "ca": "en-CA",
    "ie": "en-IE",
    "za": "en-ZA",
}

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


class PronunciationError(ParadiseError):
    """No provider could produce audio. Safe to show to a user."""


def looks_like_audio(data: bytes) -> str:
    """Return a file extension when the bytes really are audio, else ''."""
    if len(data) < 512:
        return ""
    for magic, extension in _AUDIO_MAGIC:
        if data.startswith(magic):
            return extension
    # MP4/M4A containers put 'ftyp' at offset 4.
    if len(data) > 12 and data[4:8] == b"ftyp":
        return "m4a"
    return ""


def speakable(word: str) -> str:
    """The exact text a provider is allowed to say: the headword, nothing else."""
    text = " ".join(str(word or "").split())
    return text[:MAX_SPOKEN_CHARS]


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------


class SpeechProvider(Protocol):
    name: str

    @property
    def available(self) -> bool:
        ...

    async def speak(self, request: SpeechRequest) -> Clip | None:
        ...


class NativeRecordingProvider:
    """Downloads the dictionary's own recording. Best possible quality."""

    name = "recording"

    def __init__(self, client: HttpClient, *, timeout: float = 8.0) -> None:
        self._client = client
        self._timeout = timeout

    @property
    def available(self) -> bool:
        return True

    async def speak(self, request: SpeechRequest) -> Clip | None:
        url = request.audio_url
        if not url or not url.lower().startswith(("http://", "https://")):
            return None
        data, content_type = await self._client.get_bytes(
            url, timeout=self._timeout, max_bytes=MAX_CLIP_BYTES
        )
        extension = looks_like_audio(data)
        if not extension:
            log.debug("Recording for %r was not audio (%s)", request.word, content_type)
            return None
        return Clip(
            data=data,
            extension=extension,
            provider=self.name,
            region=request.region,
            detail="native recording",
        )


class NeuralSpeechProvider:
    """Any OpenAI-compatible ``POST /audio/speech`` endpoint. Optional."""

    name = "neural"

    def __init__(
        self,
        client: HttpClient,
        *,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        voice: str = "",
        timeout: float = 15.0,
    ) -> None:
        self._client = client
        self._base_url = (base_url or "").strip().rstrip("/")
        self._api_key = (api_key or "").strip()
        self._model = (model or "").strip()
        self._voice = (voice or "").strip() or "alloy"
        self._timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self._base_url and self._model)

    async def speak(self, request: SpeechRequest) -> Clip | None:
        if not self.available:
            return None
        text = speakable(request.word)
        if not text:
            return None
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload = {
            "model": self._model,
            "voice": self._voice,
            "input": text,          # the word alone — never a definition
            "response_format": "mp3",
            "speed": 0.95,          # a hair slower: clearer, still natural
        }
        data, _content_type = await self._client.request_bytes(
            "POST",
            f"{self._base_url}/audio/speech",
            json=payload,
            headers=headers,
            timeout=self._timeout,
            max_bytes=MAX_CLIP_BYTES,
        )
        extension = looks_like_audio(data)
        if not extension:
            log.debug("Neural speech provider returned non-audio for %r", text)
            return None
        return Clip(
            data=data,
            extension=extension,
            provider=self.name,
            region=request.region,
            detail=f"neural voice ({self._voice})",
        )


class WebVoiceProvider:
    """Free, key-less neural web voice. The always-there fallback."""

    name = "web"

    def __init__(self, client: HttpClient, *, timeout: float = 8.0, url: str = WEB_VOICE_URL) -> None:
        self._client = client
        self._timeout = timeout
        self._url = url

    @property
    def available(self) -> bool:
        return True

    async def speak(self, request: SpeechRequest) -> Clip | None:
        text = speakable(request.word)
        if not text:
            return None
        language = request.language or "en"
        locale = _WEB_VOICE_LOCALES.get(request.region.lower(), "")
        attempts = [locale, language] if locale and locale != language else [language]
        last_error: Exception | None = None
        for tag in attempts:
            try:
                data, _content_type = await self._client.request_bytes(
                    "GET",
                    self._url,
                    params={
                        "ie": "UTF-8",
                        "q": text,
                        "tl": tag,
                        "total": 1,
                        "idx": 0,
                        "textlen": len(text),
                        "client": "tw-ob",
                    },
                    headers={"User-Agent": WEB_VOICE_AGENT, "Referer": "https://translate.google.com/"},
                    timeout=self._timeout,
                    max_bytes=MAX_CLIP_BYTES,
                )
            except HttpError as error:
                last_error = error
                continue
            extension = looks_like_audio(data)
            if extension:
                return Clip(
                    data=data,
                    extension=extension,
                    provider=self.name,
                    region=request.region,
                    detail=f"synthesised ({tag})",
                )
        if last_error is not None:
            raise last_error
        return None


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PronunciationOutcome:
    clip: Clip
    request: SpeechRequest
    alternates: tuple[Clip, ...] = ()

    @property
    def region_label(self) -> str:
        from .models import REGION_LABELS

        region = self.clip.region
        return REGION_LABELS.get(region, region.upper())


class PronunciationService:
    """Picks a provider, gets one clip, caches it. That is the whole job."""

    def __init__(
        self,
        client: HttpClient,
        *,
        providers: list[SpeechProvider] | None = None,
        prefer_native: bool = True,
        neural: NeuralSpeechProvider | None = None,
        timeout: float = 8.0,
    ) -> None:
        self._client = client
        self.prefer_native = prefer_native
        if providers is None:
            providers = [
                NativeRecordingProvider(client, timeout=timeout),
                neural or NeuralSpeechProvider(client),
                WebVoiceProvider(client, timeout=timeout),
            ]
        self.providers = providers
        self._cache: TTLCache[tuple, Clip] = TTLCache(
            max_items=CACHE_ITEMS,
            ttl=CLIP_TTL,
            max_weight=CACHE_BYTES,
            weigher=lambda clip: clip.size,
        )
        self._flight: SingleFlight = SingleFlight()

    # -- introspection -----------------------------------------------------

    @property
    def provider_names(self) -> tuple[str, ...]:
        return tuple(p.name for p in self.providers if p.available)

    @property
    def cached_clips(self) -> int:
        return len(self._cache)

    def describe(self) -> str:
        return f"pronunciation providers: {', '.join(self.provider_names) or 'none'}"

    # -- main entry point --------------------------------------------------

    def choose_variant(
        self, entry: WordEntry | None, region: str = ""
    ) -> Pronunciation | None:
        """Pick which recorded pronunciation to use, honouring a preference."""
        if entry is None:
            return None
        options = entry.audio_options
        if not options:
            return None
        if region:
            for option in options:
                if option.region == region.lower():
                    return option
        return options[0]

    async def pronounce(
        self,
        word: str,
        *,
        entry: WordEntry | None = None,
        region: str = "",
        language: str = "en",
        with_alternates: bool = False,
    ) -> PronunciationOutcome:
        """Get audio for one word. Raises :class:`PronunciationError` if none."""
        text = speakable(word)
        if not text:
            raise PronunciationError("there's nothing to pronounce there.")

        variant = self.choose_variant(entry, region)
        request = SpeechRequest(
            word=text,
            language=language or "en",
            region=(variant.region if variant else region).lower(),
            audio_url=variant.audio_url if variant else "",
        )
        clip = await self._clip_for(request)

        alternates: list[Clip] = []
        if with_alternates and entry is not None:
            for option in entry.audio_options:
                if len(alternates) >= 1:
                    break
                if not option.region or option.region == request.region:
                    continue
                other = SpeechRequest(
                    word=text,
                    language=request.language,
                    region=option.region,
                    audio_url=option.audio_url,
                )
                try:
                    alternates.append(await self._clip_for(other))
                except (PronunciationError, HttpError):
                    log.debug("Alternate pronunciation unavailable for %r", text)
        return PronunciationOutcome(clip=clip, request=request, alternates=tuple(alternates))

    # -- internals ---------------------------------------------------------

    def _cache_key(self, request: SpeechRequest, provider: str) -> tuple:
        return (*request.cache_key, provider, bool(request.audio_url))

    async def _clip_for(self, request: SpeechRequest) -> Clip:
        # A cached clip from any provider is as good as a fresh one.
        for provider in self._ordered_providers():
            cached = self._cache.get(self._cache_key(request, provider.name))
            if cached is not None:
                return cached

        key = ("speech", *request.cache_key)

        async def run() -> Clip:
            return await self._synthesise(request)

        return await self._flight.run(key, run)

    def _ordered_providers(self) -> list[SpeechProvider]:
        usable = [provider for provider in self.providers if provider.available]
        if self.prefer_native:
            return usable
        # Native recordings second: a configured neural voice leads instead.
        native = [p for p in usable if p.name == "recording"]
        return [p for p in usable if p.name != "recording"] + native

    async def _synthesise(self, request: SpeechRequest) -> Clip:
        errors: list[str] = []
        for provider in self._ordered_providers():
            if provider.name == "recording" and not request.audio_url:
                continue
            started = time.monotonic()
            try:
                clip = await provider.speak(request)
            except asyncio.CancelledError:
                raise
            except HttpError as error:
                errors.append(f"{provider.name}: {error}")
                continue
            except Exception as error:  # noqa: BLE001 - a bad provider is not fatal
                log.warning("Speech provider %s failed", provider.name, exc_info=True)
                errors.append(f"{provider.name}: {error.__class__.__name__}")
                continue
            if clip is None or not clip.data:
                continue
            log.info(
                "Pronounced %r via %s in %dms (%d bytes)",
                request.word,
                provider.name,
                int((time.monotonic() - started) * 1000),
                clip.size,
            )
            self._cache.put(self._cache_key(request, provider.name), clip)
            return clip

        if errors:
            log.info("No pronunciation for %r — %s", request.word, "; ".join(errors[:3]))
        raise PronunciationError(
            "couldn't get audio for that one right now. the definition still works."
        )

    def clear(self) -> None:
        self._cache.clear()
