"""On-device speech-to-text: the audio never leaves the machine.

Paradise already owns two cloud providers for recognition, but neither has to
run. :class:`LocalWhisperTranscriber` transcribes the utterance with
`faster-whisper <https://github.com/SYSTRAN/faster-whisper>`_ — a CTranslate2
build of OpenAI's Whisper that runs on the bot's own CPU, in the bot's own
process, with no key, no account and no upload:

    you │ 🎙 "hey paradise, what's the capital of Rajasthan?"
        │      ↓ Opus → PCM → endpointing → WAV (16 kHz mono)
        │      ↓ faster-whisper (int8, on this CPU)   ← no network at all
    bot │ 🗣 "Jaipur is the capital of Rajasthan."

Installing it is one command and one dependency tree::

    pip install -r requirements-local-stt.txt

The first run downloads the model once (75 MB for ``small``) into the usual
Hugging Face cache; after that it is a local file and the feature works
offline. Set ``VOICE_WAKE_STT_MODEL_DIR`` to a directory that already holds the
model to skip the download entirely, and ``VOICE_WAKE_STT_OFFLINE=true`` to
refuse to touch the network even if the model is missing.

Nothing here blocks the event loop: loading and inference both run on a
dedicated one-thread executor, so Whisper never delays music, cards or gateway
heartbeats. The model is dropped again after ``VOICE_WAKE_STT_UNLOAD_AFTER``
seconds of silence (15 minutes by default), so an idle bot keeps its RAM.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import numpy as np

from .stt import STT_TARGET_RATE, SpeechToTextUnavailable, decimate, wav_from_bytes

log = logging.getLogger(__name__)

#: Where the optional dependency is declared.
INSTALL_HINT = "pip install -r requirements-local-stt.txt"
#: ``small`` is the smallest Whisper size that handles Hindi/Hinglish well;
#: ``tiny``/``base`` are ~3× faster and noticeably worse on non-English.
DEFAULT_MODEL = "small"
#: int8 on CPU is roughly twice as fast as float32 with no useful loss here.
DEFAULT_COMPUTE = "int8"
#: Whisper's decoder is steered toward the phrase we are looking for, which
#: stops "hey paradise" coming back as "hey pair of dice" on a thin microphone.
WAKE_PROMPT = "Hey Paradise."
#: How long one utterance may take end to end before it is abandoned.
DEFAULT_TIMEOUT = 120.0
#: After a failed load (usually "no network the very first time"), wait this
#: long before trying again instead of re-downloading on every utterance.
LOAD_RETRY_SECONDS = 60.0


#: Exception names that mean "the weights are not here and cannot be fetched".
_MISSING_WEIGHTS = {
    "LocalEntryNotFoundError",
    "EntryNotFoundError",
    "RepositoryNotFoundError",
    "FileNotFoundError",
    "ConnectionError",
    "ConnectError",
    "ConnectTimeout",
    "ReadTimeout",
    "Timeout",
    "MaxRetryError",
    "NewConnectionError",
    "SSLError",
}


def describe_load_failure(error: BaseException) -> str:
    """Turn a model-loading exception into something an operator can act on."""
    name = error.__class__.__name__
    if name in _MISSING_WEIGHTS or "connection" in name.lower() or "timeout" in name.lower():
        return (
            "the on-device model is not on this machine yet and it could not be "
            f"downloaded ({name}). Run the bot once with internet so whisper "
            f"{DEFAULT_MODEL} can download, or point VOICE_WAKE_STT_MODEL_DIR at a "
            "directory that already holds it"
        )
    return (
        f"the on-device model could not be loaded ({name}); check "
        "VOICE_WAKE_STT_MODEL_DIR, VOICE_WAKE_STT_COMPUTE and the first-run download"
    )


def _import_backend() -> Any:
    """Import faster-whisper, or explain precisely what is missing."""
    try:
        from faster_whisper import WhisperModel
    except Exception as error:  # noqa: BLE001 - any import failure is fatal here
        log.info("faster-whisper unavailable: %s", error)
        raise SpeechToTextUnavailable(
            f"on-device speech-to-text is not installed ({INSTALL_HINT})"
        ) from None
    return WhisperModel


class LocalWhisperTranscriber:
    """Whisper, on this machine, with no API call of any kind."""

    name = "local"

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        device: str = "cpu",
        compute_type: str = DEFAULT_COMPUTE,
        threads: int = 0,
        model_dir: str = "",
        offline: bool = False,
        unload_after: float = 900.0,
        timeout: float = DEFAULT_TIMEOUT,
        prompt: str = WAKE_PROMPT,
        beam_size: int = 1,
        loader: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.model = (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        self.device = (device or "cpu").strip() or "cpu"
        self.compute_type = (compute_type or DEFAULT_COMPUTE).strip() or DEFAULT_COMPUTE
        self.threads = max(0, int(threads or 0))
        self.model_dir = (model_dir or "").strip()
        self.offline = bool(offline)
        self.unload_after = max(0.0, float(unload_after or 0.0))
        self.timeout = max(5.0, float(timeout or DEFAULT_TIMEOUT))
        self.prompt = prompt or ""
        self.beam_size = max(1, int(beam_size or 1))

        self._loader = loader
        self._clock = clock
        self._model: Any = None
        self._loaded_error = ""
        self._next_load_attempt = 0.0
        self._permanent_failure = False
        self._installed: bool | None = None
        self._last_used = 0.0
        self._lock = asyncio.Lock()
        self._unload_handle: asyncio.TimerHandle | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._closed = False

        #: Seconds the last utterance took to transcribe (for /voicewake status).
        self.last_seconds = 0.0
        #: Length of the last utterance in seconds.
        self.last_audio_seconds = 0.0

    # -- capability ------------------------------------------------------

    @property
    def installed(self) -> bool:
        """Is the optional dependency present? (Cheap: no import, no model.)"""
        if self._loader is not None:
            return True
        if self._installed is None:
            try:
                self._installed = importlib.util.find_spec("faster_whisper") is not None
            except (ImportError, ValueError):  # pragma: no cover - broken install
                self._installed = False
        return self._installed

    @property
    def available(self) -> bool:
        return bool(self.installed and not self._permanent_failure and not self._closed)

    @property
    def load_error(self) -> str:
        """Why the last load attempt failed, in words a user can act on."""
        return self._loaded_error

    @property
    def loaded(self) -> bool:
        """Whether the weights are in memory right now."""
        return self._model is not None

    def describe(self) -> str:
        if not self.installed:
            return "local whisper (not installed)"
        return (
            f"local whisper {self.model} ({self.compute_type} on {self.device}, "
            f"{'loaded' if self.loaded else 'idle'})"
        )

    # -- transcription ---------------------------------------------------

    async def transcribe(self, wav: bytes, *, language: str = "") -> str:
        if self._closed:
            raise SpeechToTextUnavailable("the local transcriber is shut down")
        if not self.installed:
            raise SpeechToTextUnavailable(
                f"on-device speech-to-text is not installed ({INSTALL_HINT})"
            )
        audio = self._pcm(wav)
        if audio.size == 0:
            return ""
        self._last_used = self._clock()
        self._remember_loop()
        executor = self._executor
        started = self._clock()
        loop = asyncio.get_running_loop()
        try:
            text = await asyncio.wait_for(
                loop.run_in_executor(executor, self._transcribe_sync, audio, language),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            log.warning(
                "local speech-to-text gave up after %.0fs on %.1fs of audio",
                self.timeout,
                self.last_audio_seconds,
            )
            raise SpeechToTextUnavailable(
                "on-device transcription took too long on this machine"
            ) from None
        finally:
            self.last_seconds = max(0.0, self._clock() - started)
            self._schedule_unload()
        return text

    def _pcm(self, wav: bytes) -> np.ndarray:
        """RIFF/WAVE → mono float32 at 16 kHz, the form Whisper expects."""
        samples, rate = wav_from_bytes(wav)
        if samples.size == 0:
            return np.zeros(0, dtype=np.float32)
        array = np.asarray(samples)
        if array.ndim > 1:  # defensive: WAV parsing always gives mono
            array = array.reshape(-1, array.shape[-1]).mean(axis=1)
        if rate and rate > STT_TARGET_RATE:
            array = decimate(array.astype(np.int16), rate, STT_TARGET_RATE)
        elif rate and rate < STT_TARGET_RATE:
            log.debug("transcribing %s Hz audio as-is; whisper expects 16 kHz", rate)
        audio = np.asarray(array, dtype=np.float32) / 32768.0
        self.last_audio_seconds = audio.size / float(STT_TARGET_RATE)
        return audio

    def _transcribe_sync(self, audio: np.ndarray, language: str) -> str:
        """Runs on the executor thread: load once, then decode."""
        model = self._model or self._ensure_model()
        options: dict[str, Any] = {
            "language": (language or "").strip() or None,
            "beam_size": self.beam_size,
            "condition_on_previous_text": False,
            "vad_filter": False,  # the segmenter already trimmed the silence
            "without_timestamps": True,
        }
        if self.prompt:
            options["initial_prompt"] = self.prompt
        try:
            segments, info = model.transcribe(audio, **options)
            text = " ".join(
                str(getattr(segment, "text", "") or "") for segment in segments
            )
        except SpeechToTextUnavailable:
            raise
        except Exception as error:  # noqa: BLE001 - decoder failures are not fatal
            log.info("local speech-to-text failed: %s", error)
            raise SpeechToTextUnavailable("on-device transcription failed") from None
        detected = getattr(info, "language", "") or ""
        log.debug(
            "local transcription: %.1fs of audio in %.2fs (language %s) → %r",
            self.last_audio_seconds,
            max(0.0, self.last_seconds),
            detected or "?",
            " ".join(text.split())[:120],
        )
        return " ".join(text.split()).strip()

    # -- model lifecycle -------------------------------------------------

    def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        if self._permanent_failure:
            raise SpeechToTextUnavailable(self._loaded_error or "the model failed to load")
        now = self._clock()
        if now < self._next_load_attempt:
            raise SpeechToTextUnavailable(
                self._loaded_error or "the on-device model is still not ready"
            )
        started = now
        try:
            model = self._loader() if self._loader is not None else self._build_model()
        except SpeechToTextUnavailable as error:
            self._loaded_error = str(error)
            self._next_load_attempt = now + LOAD_RETRY_SECONDS
            raise
        except Exception as error:  # noqa: BLE001 - network, disk, bad config
            log.info("local speech-to-text model failed to load: %s", error)
            self._loaded_error = describe_load_failure(error)
            self._next_load_attempt = now + LOAD_RETRY_SECONDS
            raise SpeechToTextUnavailable(self._loaded_error) from None
        self._model = model
        self._loaded_error = ""
        self._next_load_attempt = 0.0
        log.info(
            "on-device speech-to-text ready: whisper %s (%s on %s) in %.1fs",
            self.model,
            self.compute_type,
            self.device,
            max(0.0, self._clock() - started),
        )
        return model

    def _build_model(self) -> Any:
        WhisperModel = _import_backend()
        options: dict[str, Any] = {
            "device": self.device,
            "compute_type": self.compute_type,
            "num_workers": 1,
        }
        if self.threads:
            options["cpu_threads"] = self.threads
        if self.model_dir:
            options["download_root"] = self.model_dir
        if self.offline:
            options["local_files_only"] = True
        source = self.model_dir if self.offline and self.model_dir else self.model
        return WhisperModel(source, **options)

    # -- idle unload -----------------------------------------------------

    def _remember_loop(self) -> None:
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:  # pragma: no cover - always called in a coroutine
                self._loop = None
        if self._executor is None and not self._closed:
            self._executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="paradise-whisper"
            )

    def _schedule_unload(self) -> None:
        """Drop the weights when nobody has spoken for a while."""
        if self.unload_after <= 0 or self._loop is None or self._closed:
            return
        if self._unload_handle is not None:
            self._unload_handle.cancel()
        self._unload_handle = self._loop.call_later(self.unload_after, self._unload_if_idle)

    def _unload_if_idle(self) -> None:
        self._unload_handle = None
        if self._model is None:
            return
        if self._clock() - self._last_used < self.unload_after:
            self._schedule_unload()  # a request landed in between; look again later
            return
        self._model = None
        log.info(
            "on-device speech-to-text idle for %.0fs; model unloaded", self.unload_after
        )

    async def close(self) -> None:
        self._closed = True
        if self._unload_handle is not None:
            self._unload_handle.cancel()
            self._unload_handle = None
        self._model = None
        executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)


__all__ = [
    "DEFAULT_COMPUTE",
    "DEFAULT_MODEL",
    "INSTALL_HINT",
    "LocalWhisperTranscriber",
    "WAKE_PROMPT",
    "describe_load_failure",
]
