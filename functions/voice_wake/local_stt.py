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

``pip`` installs the *engine*; the weights are not a Python package and no
requirements file can carry them. So the bot fetches them itself: at start-up it
checks for the model files and, if they are missing, downloads them once in the
background (75 MB for ``small``) while Discord is still connecting. By the time
anyone says "hey paradise" the files are already local, and from then on the
feature works with no network at all. :func:`prepare_model_files` is the same
logic as a standalone call, for building images ahead of time::

    python tools/fetch_whisper_model.py small

Set ``VOICE_WAKE_STT_MODEL_DIR`` to a directory that already holds the model to
skip the download entirely, ``VOICE_WAKE_STT_OFFLINE=true`` to refuse to touch
the network even if the model is missing, and ``VOICE_WAKE_STT_PRELOAD=false``
to keep start-up silent and only download on first use.

Nothing here blocks the event loop: loading and inference both run on a
dedicated one-thread executor, so Whisper never delays cards, commands or
gateway heartbeats. The model is dropped again after ``VOICE_WAKE_STT_UNLOAD_AFTER``
seconds of silence (15 minutes by default), so an idle bot keeps its RAM.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
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

# Outcome of :meth:`LocalWhisperTranscriber.prepare` / :func:`prepare_model_files`.
STATUS_READY = "ready"          # the files are on disk
STATUS_DOWNLOADED = "downloaded"  # they were fetched just now
STATUS_NOT_INSTALLED = "not-installed"  # faster-whisper itself is missing
STATUS_OFFLINE = "offline"      # offline mode and no files to use
STATUS_FAILED = "failed"        # the network, or the disk, said no


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


def default_cache_dir() -> str:
    """Where faster-whisper would put the weights if asked for a model by name."""
    try:  # huggingface_hub is a faster-whisper dependency, so this is free
        from huggingface_hub.constants import HF_HUB_CACHE

        return str(HF_HUB_CACHE)
    except Exception:  # noqa: BLE001 - older/absent hub: mirror its documented default
        pass
    try:
        home = os.environ.get("HF_HOME") or ""
        base = Path(home).expanduser() if home else Path.home() / ".cache" / "huggingface"
        return str(base / "hub")
    except (OSError, RuntimeError):  # no HOME on this machine
        return ""


def _repo_id(model: str) -> str:
    """The Hugging Face repo a size resolves to (``small`` → Systran/…-small)."""
    try:
        from faster_whisper.utils import _MODELS

        if model in _MODELS:
            return str(_MODELS[model])
    except Exception:  # noqa: BLE001 - engine absent: fall back to the default org
        pass
    return model if "/" in model else f"Systran/faster-whisper-{model}"


def _repo_dir_name(model: str) -> str:
    """The Hugging Face cache folder for a model, e.g. ``small``."""
    return "models--" + _repo_id(model).replace("/", "--")


def find_cached_model(model: str, cache_dir: str = "") -> str:
    """Return the cached snapshot for ``model``, or ``""`` when it is not there.

    Checked before any network call so a machine that already has the weights
    never touches the internet just to be told so.
    """
    root = Path(cache_dir or default_cache_dir()).expanduser()
    snapshots = root / _repo_dir_name(model) / "snapshots"
    try:
        entries = sorted(snapshots.iterdir(), key=lambda item: item.name)
    except (OSError, AttributeError):
        return ""
    for entry in entries:
        if (entry / "model.bin").is_file():
            return str(entry)
    return ""


def looks_like_model_dir(path: str) -> bool:
    """True for a converted-model directory (``model.bin`` + ``config.json``)."""
    if not path:
        return False
    folder = Path(path).expanduser()
    return (folder / "model.bin").is_file()


def directory_size(path: str) -> int:
    """Bytes under ``path``, following the snapshot's symlinks."""
    total = 0
    try:
        for item in Path(path).expanduser().rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0
    return total


def human_size(size: int) -> str:
    if size <= 0:
        return "unknown size"
    megabytes = size / (1024 * 1024)
    if megabytes >= 1024:
        return f"{megabytes / 1024:.1f} GB"
    if megabytes >= 10:
        return f"{megabytes:.0f} MB"
    return f"{megabytes:.1f} MB"


def _download_backend() -> Callable[..., str]:
    """faster-whisper's own downloader (a thin wrapper over huggingface_hub)."""
    try:
        from faster_whisper.utils import download_model
    except Exception as error:  # noqa: BLE001 - any import failure is fatal here
        log.info("faster-whisper unavailable: %s", error)
        raise SpeechToTextUnavailable(
            f"on-device speech-to-text is not installed ({INSTALL_HINT})"
        ) from None
    return download_model


def download_model_files(
    model: str = DEFAULT_MODEL,
    *,
    model_dir: str = "",
    offline: bool = False,
    downloader: Callable[..., str] | None = None,
) -> tuple[str, str, str]:
    """Make sure the weights for ``model`` are on this machine.

    Returns ``(status, path, message)`` where ``status`` is one of the
    ``STATUS_*`` constants. Never raises for a missing network: the caller gets
    ``STATUS_FAILED`` and a sentence an operator can act on, so start-up can
    carry on and the next attempt can succeed.
    """
    model = (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    raw = (model_dir or "").strip()
    cache_dir = Path(raw).expanduser() if raw else None

    # A path to a converted model is the model; nothing to fetch.
    if looks_like_model_dir(model):
        return STATUS_READY, str(Path(model).expanduser()), "the model is a local directory"

    cached = find_cached_model(model, "" if cache_dir is None else str(cache_dir))
    if cached:
        return STATUS_READY, cached, "already on disk"

    if offline:
        return (
            STATUS_OFFLINE,
            "",
            f"whisper {model} is not on this machine and VOICE_WAKE_STT_OFFLINE=true "
            "forbids downloading it; copy a model directory over and point "
            "VOICE_WAKE_STT_MODEL_DIR at it",
        )

    fetch = downloader
    if fetch is None:
        try:
            fetch = _download_backend()
        except SpeechToTextUnavailable as error:
            return STATUS_NOT_INSTALLED, "", str(error)

    options: dict[str, Any] = {"local_files_only": False}
    if cache_dir is not None:
        options["cache_dir"] = str(cache_dir)
    started = time.monotonic()
    try:
        path = fetch(model, **options)
    except Exception as error:  # noqa: BLE001 - network, disk, hub outage…
        log.debug("whisper %s download failed", model, exc_info=True)
        return STATUS_FAILED, "", describe_load_failure(error)

    path = str(path or "") or find_cached_model(
        model, "" if cache_dir is None else str(cache_dir)
    )
    log.info(
        "downloaded whisper %s (%s) in %.1fs → %s",
        model,
        human_size(directory_size(path)),
        max(0.0, time.monotonic() - started),
        path or "the Hugging Face cache",
    )
    return STATUS_DOWNLOADED, path, "downloaded just now"


def prepare_model_files(
    model: str = DEFAULT_MODEL,
    *,
    model_dir: str = "",
    offline: bool = False,
    downloader: Callable[..., str] | None = None,
) -> tuple[str, str, str]:
    """Alias kept explicit for readers of ``tools/fetch_whisper_model.py``."""
    return download_model_files(
        model, model_dir=model_dir, offline=offline, downloader=downloader
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
        downloader: Callable[..., str] | None = None,
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
        self._downloader = downloader
        self._clock = clock
        self._model: Any = None
        self._loaded_error = ""
        self._next_load_attempt = 0.0
        self._permanent_failure = False
        self._installed: bool | None = None
        self._last_used = 0.0
        self._lock = asyncio.Lock()
        self._prepare_lock = asyncio.Lock()
        self._unload_handle: asyncio.TimerHandle | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._closed = False

        #: Where the weights were found/fetched (blank until prepare()).
        self.model_path = ""
        #: One of the STATUS_* constants; blank means "not looked yet".
        self.prepare_status = ""
        #: Why the last prepare() did not end with usable files, in words.
        self.prepare_message = ""

        #: Seconds the last utterance took to transcribe (for /voicewake status).
        self.last_seconds = 0.0
        #: Length of the last utterance in seconds.
        self.last_audio_seconds = 0.0

    # -- capability ------------------------------------------------------

    @property
    def installed(self) -> bool:
        """Is the optional dependency present? (Cheap: no import, no model.)"""
        if self._loader is not None or self._downloader is not None:
            # An injected loader/downloader *is* the engine for this instance.
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

    @property
    def prepared(self) -> bool:
        """Whether the weights are known to be on disk (downloaded or found)."""
        return self.prepare_status in {STATUS_READY, STATUS_DOWNLOADED}

    def describe_model(self) -> str:
        """One line about the *files*, for logs and ``/voicewake status``."""
        if not self.installed:
            return f"whisper {self.model}: not installed ({INSTALL_HINT})"
        if self.prepared:
            where = self.model_path or self.model_dir or "the Hugging Face cache"
            return f"whisper {self.model}: on disk · {where}"
        if self.prepare_status == STATUS_OFFLINE:
            return f"whisper {self.model}: not on disk, and OFFLINE=true forbids fetching it"
        if self.prepare_status == STATUS_FAILED:
            return f"whisper {self.model}: download failed — {self.prepare_message}"
        if self.loaded:
            return f"whisper {self.model}: loaded in memory"
        return f"whisper {self.model}: not downloaded yet"

    def describe(self) -> str:
        if not self.installed:
            return "local whisper (not installed)"
        return (
            f"local whisper {self.model} ({self.compute_type} on {self.device}, "
            f"{'loaded' if self.loaded else 'idle'})"
        )

    # -- model files -----------------------------------------------------

    async def prepare(self, *, timeout: float = 900.0) -> str:
        """Fetch the weights now instead of on the first utterance.

        Called once at start-up so "hey paradise" works the moment somebody
        says it, rather than after a one-time download. Runs on the same
        one-thread executor as inference, so the gateway heartbeat and every
        command keep running while it downloads. Never raises: the outcome is
        returned as a ``STATUS_*`` string and kept for :meth:`describe_model`.
        """
        if self.prepared or self._closed:
            return self.prepare_status
        if self._loader is not None:
            # A loader hands the model over directly (tests, embeddings): there
            # is nothing on disk to fetch.
            self.prepare_status = STATUS_READY
            self.prepare_message = ""
            return self.prepare_status
        if not self.installed and self._downloader is None:
            self.prepare_status = STATUS_NOT_INSTALLED
            self.prepare_message = f"on-device speech-to-text is not installed ({INSTALL_HINT})"
            return self.prepare_status
        async with self._prepare_lock:
            if self.prepared or self._closed:  # another caller got there first
                return self.prepare_status
            self._remember_loop()
            loop = asyncio.get_running_loop()
            try:
                status, path, message = await asyncio.wait_for(
                    loop.run_in_executor(
                        self._executor,
                        lambda: download_model_files(
                            self.model,
                            model_dir=self.model_dir,
                            offline=self.offline,
                            downloader=self._downloader,
                        ),
                    ),
                    timeout=max(1.0, float(timeout or 900.0)),
                )
            except asyncio.TimeoutError:
                status, path, message = (
                    STATUS_FAILED,
                    "",
                    "the model download took too long; it will be retried on the next start-up",
                )
            except Exception as error:  # noqa: BLE001 - start-up must not die here
                status, path, message = STATUS_FAILED, "", describe_load_failure(error)
            self.prepare_status = status
            self.prepare_message = "" if status in {STATUS_READY, STATUS_DOWNLOADED} else message
            if path:
                self.model_path = path
            log.info("on-device model %s", self.describe_model())
            return status

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
        # Offline: hand CTranslate2 a real model directory, not the download
        # root it was cached under (the snapshot holds model.bin; the root does
        # not). A path the operator pointed at directly still wins.
        source = self.model
        if self.offline:
            if looks_like_model_dir(self.model):
                source = str(Path(self.model).expanduser())
            elif self.model_dir and looks_like_model_dir(self.model_dir):
                source = str(Path(self.model_dir).expanduser())
            else:
                cached = find_cached_model(
                    self.model, self.model_dir or ""
                ) or find_cached_model(self.model)
                source = cached or self.model_dir or self.model
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
    "STATUS_DOWNLOADED",
    "STATUS_FAILED",
    "STATUS_NOT_INSTALLED",
    "STATUS_OFFLINE",
    "STATUS_READY",
    "WAKE_PROMPT",
    "default_cache_dir",
    "describe_load_failure",
    "directory_size",
    "download_model_files",
    "find_cached_model",
    "human_size",
    "looks_like_model_dir",
    "prepare_model_files",
]
