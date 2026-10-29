"""Speech-to-text for one short utterance.

Paradise already owns two cloud providers, so there is nothing new to
subscribe to:

* **ElevenLabs Scribe** (``POST /v1/speech-to-text``) — best Hindi/Hinglish
  handling and it uses the key that already speaks the answers;
* **Gemini** — accepts audio inline as base64 in the same request it already
  knows how to make, so it works with the AI key alone.

Both accept a WAV file. Utterances are captured at 48 kHz (Opus) but speech
recognition does not need anything above 16 kHz, so the audio is down-mixed
and decimated first: 6 seconds of speech is 192 KB instead of 576 KB, which
is most of the round-trip time.

No key at all is not an error condition: :class:`SpeechToTextChain` reports
itself unavailable and the listener stays silent.
"""

from __future__ import annotations

import io
import logging
import struct
from typing import Protocol, runtime_checkable

import aiohttp
import numpy as np

from functions.helpers import ParadiseError
from functions.voice_clip.constants import SAMPLE_RATE

log = logging.getLogger(__name__)

ELEVENLABS_API_BASE = "https://api.elevenlabs.io/v1"
#: Scribe v1 is ElevenLabs' multilingual recognition model.
SCRIBE_MODEL = "scribe_v1"
STT_TARGET_RATE = 16_000

_TRANSCRIBE_PROMPT = (
    "Transcribe this audio exactly as spoken, in whatever language it is in "
    "(English, Hindi or Hinglish).\n"
    "Output ONLY the transcript, on one line, with no quotes, no speaker "
    "labels, no timestamps and no commentary.\n"
    "If the audio contains no intelligible speech, output nothing at all."
)


class SpeechToTextUnavailable(ParadiseError):
    """No transcription provider is configured or reachable."""


# ---------------------------------------------------------------------------
# Audio → WAV
# ---------------------------------------------------------------------------


def downmix(samples: np.ndarray) -> np.ndarray:
    """Collapse stereo/multi-channel int16 audio to mono."""
    array = np.asarray(samples)
    if array.ndim == 1:
        return array
    return array.reshape(-1, array.shape[-1]).mean(axis=1).astype(np.int16)


def decimate(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    """Cheap, safe downsampling by an integer factor with a box filter.

    Speech recognition does not need hi-fi: averaging the samples of each
    output window removes the top octave (which is where aliasing would fold
    back from) before they are dropped.
    """
    array = np.asarray(samples, dtype=np.float32)
    if source_rate <= target_rate or target_rate <= 0:
        return np.asarray(samples, dtype=np.int16)
    factor = max(1, int(round(source_rate / target_rate)))
    if factor == 1:
        return np.asarray(samples, dtype=np.int16)
    usable = (array.size // factor) * factor
    if usable == 0:
        return np.zeros(0, dtype=np.int16)
    folded = array[:usable].reshape(-1, factor).mean(axis=1)
    return np.clip(np.rint(folded), -32768, 32767).astype(np.int16)


def to_wav(
    samples: np.ndarray | bytes,
    *,
    rate: int = SAMPLE_RATE,
    target_rate: int = STT_TARGET_RATE,
) -> bytes:
    """Encode int16 mono PCM as a RIFF/WAVE file at ``target_rate``."""
    if isinstance(samples, bytes):
        array = np.frombuffer(samples, dtype="<i2")
    else:
        array = np.asarray(samples).astype(np.int16)
    mono = decimate(downmix(array), rate, target_rate)
    payload = mono.astype("<i2").tobytes()
    channels = 1
    bits = 16
    block_align = channels * bits // 8
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(payload),
        b"WAVE",
        b"fmt ",
        16,
        1,  # PCM
        channels,
        target_rate,
        target_rate * block_align,
        block_align,
        bits,
        b"data",
        len(payload),
    )
    return header + payload


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


@runtime_checkable
class Transcriber(Protocol):
    """One speech-to-text backend."""

    name: str

    @property
    def available(self) -> bool: ...

    async def transcribe(self, wav: bytes, *, language: str = "") -> str: ...


class ElevenLabsTranscriber:
    """ElevenLabs Scribe: multipart upload, ``xi-api-key`` header only."""

    name = "elevenlabs"

    def __init__(
        self,
        api_key: str = "",
        *,
        model: str = SCRIBE_MODEL,
        timeout: float = 25.0,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._api_key = (api_key or "").strip()
        self.model = (model or SCRIBE_MODEL).strip()
        self.timeout = float(timeout)
        self._session = session

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def describe(self) -> str:
        return "elevenlabs scribe" if self.available else "elevenlabs (no key)"

    async def transcribe(self, wav: bytes, *, language: str = "") -> str:
        if not self.available:
            raise SpeechToTextUnavailable("no ElevenLabs key configured")
        form = aiohttp.FormData()
        form.add_field("model_id", self.model)
        form.add_field(
            "file", wav, filename="speech.wav", content_type="audio/wav"
        )
        if language:
            form.add_field("language_code", language)
        url = f"{ELEVENLABS_API_BASE}/speech-to-text"
        headers = {"xi-api-key": self._api_key}
        try:
            session = await self._get_session()
            async with session.post(url, data=form, headers=headers) as response:
                if response.status != 200:
                    body = await response.text()
                    log.info(
                        "ElevenLabs STT failed (%s): %s",
                        response.status,
                        body[:160].replace("\n", " "),
                    )
                    raise SpeechToTextUnavailable(
                        "the transcription service refused that request"
                    )
                data = await response.json(content_type=None)
        except SpeechToTextUnavailable:
            raise
        except (aiohttp.ClientError, asyncio_timeout_errors()) as error:
            log.info("ElevenLabs STT unreachable: %s", error)
            raise SpeechToTextUnavailable("the transcription service is unreachable") from None
        return _response_text(data)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout, connect=5.0)
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None


class GeminiTranscriber:
    """Gemini with the audio inline — no extra provider, no extra key."""

    name = "gemini"

    def __init__(self, gemini=None) -> None:
        self._gemini = gemini

    @property
    def available(self) -> bool:
        return bool(self._gemini is not None and getattr(self._gemini, "available", False))

    def describe(self) -> str:
        return "gemini audio" if self.available else "gemini (not configured)"

    async def transcribe(self, wav: bytes, *, language: str = "") -> str:
        if not self.available:
            raise SpeechToTextUnavailable("Gemini is not configured")
        prompt = _TRANSCRIBE_PROMPT
        if language:
            prompt += f"\nThe speaker is using: {language}."
        try:
            text = await self._gemini.transcribe_audio(wav, prompt=prompt)
        except ParadiseError as error:
            raise SpeechToTextUnavailable(str(error)) from None
        return " ".join(str(text or "").split()).strip()


class SpeechToTextChain:
    """Try each backend in order; report the first usable transcript."""

    def __init__(self, *providers: Transcriber) -> None:
        self.providers: list[Transcriber] = [p for p in providers if p is not None]
        self.last_error = ""

    @property
    def available(self) -> bool:
        return any(provider.available for provider in self.providers)

    def describe(self) -> str:
        if not self.providers:
            return "speech-to-text: none"
        names = ", ".join(
            f"{provider.name}{'' if provider.available else ' (off)'}"
            for provider in self.providers
        )
        return f"speech-to-text: {names}"

    async def transcribe(self, wav: bytes, *, language: str = "") -> str:
        if not wav:
            return ""
        errors: list[str] = []
        for provider in self.providers:
            if not provider.available:
                continue
            try:
                text = await provider.transcribe(wav, language=language)
            except SpeechToTextUnavailable as error:
                errors.append(f"{provider.name}: {error}")
                continue
            except Exception as error:  # noqa: BLE001 - one backend is not fatal
                errors.append(f"{provider.name}: {error.__class__.__name__}")
                log.debug("STT provider %s failed", provider.name, exc_info=True)
                continue
            if text:
                return text
            errors.append(f"{provider.name}: empty")
        self.last_error = "; ".join(errors)
        return ""

    async def close(self) -> None:
        for provider in self.providers:
            closer = getattr(provider, "close", None)
            if closer is None:
                continue
            try:
                await closer()
            except Exception:  # noqa: BLE001 - shutdown is best effort
                log.debug("STT provider close failed", exc_info=True)


def asyncio_timeout_errors() -> tuple[type[BaseException], ...]:
    """The exception types aiohttp raises for a slow/dead endpoint."""
    import asyncio

    return (asyncio.TimeoutError, OSError)


def _response_text(data) -> str:
    """Pull the transcript out of whichever response shape came back."""
    if not isinstance(data, dict):
        return ""
    for key in ("text", "transcript", "transcription"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split()).strip()
    segments = data.get("segments") or data.get("words")
    if isinstance(segments, list):
        pieces = [
            str(item.get("text") or "").strip()
            for item in segments
            if isinstance(item, dict)
        ]
        joined = " ".join(piece for piece in pieces if piece)
        return " ".join(joined.split()).strip()
    return ""


def wav_from_bytes(data: bytes) -> tuple[np.ndarray, int]:
    """Parse a 16-bit PCM WAV back into (samples, rate). Used by tests."""
    buffer = io.BytesIO(data)
    header = buffer.read(12)
    if len(header) < 12 or header[:4] != b"RIFF":
        return np.zeros(0, dtype=np.int16), 0
    rate = 0
    # Walk the chunk table with an explicit bound: a truncated or hostile file
    # ends the loop instead of reading past the buffer.
    remaining = len(data) - 12
    while remaining >= 8:
        chunk = buffer.read(8)
        if len(chunk) < 8:
            break
        name = chunk[:4]
        size = struct.unpack("<I", chunk[4:])[0]
        remaining -= 8 + size
        if name == b"fmt ":
            rate = struct.unpack("<I", buffer.read(size)[4:8])[0]
            continue
        if name == b"data":
            return np.frombuffer(buffer.read(size), dtype="<i2"), rate
        buffer.seek(size, io.SEEK_CUR)
    return np.zeros(0, dtype=np.int16), rate


__all__ = [
    "ELEVENLABS_API_BASE",
    "ElevenLabsTranscriber",
    "GeminiTranscriber",
    "SCRIBE_MODEL",
    "SpeechToTextChain",
    "SpeechToTextUnavailable",
    "STT_TARGET_RATE",
    "Transcriber",
    "decimate",
    "downmix",
    "to_wav",
    "wav_from_bytes",
]
