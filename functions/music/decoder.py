"""FFmpeg → PCM, in fixed 20 ms frames.

Discord wants 48 kHz stereo signed-16-bit little-endian audio in 20 ms
packets: exactly 3840 bytes. Everything in this package works in those
frames, which is why mixing a sentence of speech over a song is a simple
per-frame addition rather than a resampling problem.

Reads happen on a dedicated thread into a small bounded queue. That matters
more than it looks: Discord calls ``AudioSource.read()`` on its own voice
thread, and a stalled socket read there freezes the bot's audio for everyone
in the channel. With the queue, a dead stream degrades to silence and the
player notices instead of hanging.
"""

from __future__ import annotations

import logging
import queue
import shutil
import subprocess
import threading

from functions.helpers import ParadiseError

log = logging.getLogger(__name__)

SAMPLE_RATE = 48_000
CHANNELS = 2
SAMPLE_WIDTH = 2
FRAME_SECONDS = 0.02
FRAME_BYTES = int(SAMPLE_RATE * FRAME_SECONDS) * CHANNELS * SAMPLE_WIDTH  # 3840

_SENTINEL_EOF = object()
_QUEUE_FRAMES = 64  # ~1.3 s of buffered audio (256 KB)


class DecodeError(ParadiseError):
    """FFmpeg could not be started or produced nothing."""


def resolve_ffmpeg() -> str | None:
    """FFmpeg on PATH, or the one bundled inside the imageio-ffmpeg wheel."""
    system = shutil.which("ffmpeg")
    if system:
        return system
    try:
        import imageio_ffmpeg

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled:
            return bundled
    except Exception:  # pragma: no cover - optional dependency
        pass
    return None


def _decode_args(executable: str, extra_input: list[str] | None = None) -> list[str]:
    return [
        executable,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        *(extra_input or []),
    ]


def _output_args() -> list[str]:
    return [
        "-vn",
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        str(CHANNELS),
        "pipe:1",
    ]


class PcmStream:
    """A 48 kHz stereo PCM stream read frame by frame (thread safe reads)."""

    def __init__(
        self,
        url: str,
        *,
        executable: str | None = None,
        start: float = 0.0,
    ) -> None:
        self.url = url
        self.executable = executable or resolve_ffmpeg() or ""
        self.start = max(0.0, float(start))
        self._proc: subprocess.Popen | None = None
        self._queue: queue.Queue = queue.Queue(maxsize=_QUEUE_FRAMES)
        self._reader: threading.Thread | None = None
        self._closed = False
        self._eof = False
        self.frames_read = 0
        self.peak: bytes | None = None  # last frame handed out (for debugging)

    # -- lifecycle ----------------------------------------------------------

    def open(self) -> "PcmStream":
        if not self.executable:
            raise DecodeError("ffmpeg is not available on this host.")
        args = _decode_args(self.executable)
        if self.url.lower().startswith(("http://", "https://")):
            # A dropped CDN connection must not end the song 40 seconds in.
            args += [
                "-reconnect",
                "1",
                "-reconnect_streamed",
                "1",
                "-reconnect_delay_max",
                "5",
            ]
        if self.start:
            args += ["-ss", f"{self.start:.3f}"]
        args += ["-i", self.url, *_output_args()]
        try:
            self._proc = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=FRAME_BYTES * 2,
            )
        except OSError as error:
            raise DecodeError(f"could not start ffmpeg: {error}") from error
        self._reader = threading.Thread(
            target=self._read_loop, name="paradise-pcm-reader", daemon=True
        )
        self._reader.start()
        return self

    def _read_loop(self) -> None:
        stdout = self._proc.stdout if self._proc is not None else None
        if stdout is None:  # pragma: no cover - defensive
            self._push(_SENTINEL_EOF)
            return
        try:
            while not self._closed:
                chunk = stdout.read(FRAME_BYTES)
                if not chunk:
                    break
                if len(chunk) < FRAME_BYTES:
                    # A short final frame: pad with silence so the mixer always
                    # receives a complete 20 ms block.
                    chunk = chunk + b"\x00" * (FRAME_BYTES - len(chunk))
                self._push(chunk)
        except (OSError, ValueError):  # pragma: no cover - closed pipe
            pass
        finally:
            self._push(_SENTINEL_EOF)

    def _push(self, item) -> None:
        """Block when the buffer is full.

        Dropping a frame here would *skip 20 ms of the song* — a local file
        decodes far faster than real time, so a non-blocking push would throw
        away almost all of it. Blocking instead applies backpressure through
        the pipe, which is exactly what FFmpeg is designed for.
        """
        while not self._closed:
            try:
                self._queue.put(item, timeout=0.2)
                return
            except queue.Full:
                continue

    # -- consumption --------------------------------------------------------

    def read_frame(self, timeout: float = 1.0) -> bytes:
        """Return one frame, ``b""`` at end of stream, ``None`` on a stall."""
        if self._closed:
            return b""
        if self._eof:
            return b""
        try:
            item = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None if not self._eof else b""
        if item is _SENTINEL_EOF:
            self._eof = True
            return b""
        self.frames_read += 1
        self.peak = item
        return item

    @property
    def position(self) -> float:
        return self.frames_read * FRAME_SECONDS

    @property
    def ended(self) -> bool:
        return self._eof

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2.0)
            except Exception:  # noqa: BLE001 - never block shutdown
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        try:
            while not self._queue.empty():
                self._queue.get_nowait()
        except queue.Empty:  # pragma: no cover
            pass
        reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=1.0)
        self._reader = None


def decode_to_pcm(
    data: bytes,
    *,
    executable: str | None = None,
    timeout: float = 20.0,
    max_bytes: int = FRAME_BYTES * 60 * 60,  # one hour, a safety stop
) -> bytes:
    """Decode an encoded clip (MP3/Opus/WAV/…) into 20 ms-aligned PCM.

    Used for spoken replies: the ElevenLabs clip is MP3, and mixing requires
    PCM. Output is padded to a whole number of frames.
    """
    if not data:
        return b""
    exe = executable or resolve_ffmpeg()
    if not exe:
        raise DecodeError("ffmpeg is not available on this host.")
    args = _decode_args(exe) + ["-i", "pipe:0", *_output_args()]
    try:
        completed = subprocess.run(
            args,
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise DecodeError("that audio took too long to decode.") from None
    except OSError as error:
        raise DecodeError(f"could not start ffmpeg: {error}") from error
    pcm = completed.stdout or b""
    if not pcm:
        return b""
    pcm = pcm[:max_bytes]
    remainder = len(pcm) % FRAME_BYTES
    if remainder:
        pcm += b"\x00" * (FRAME_BYTES - remainder)
    return pcm


def silence_frame() -> bytes:
    return b"\x00" * FRAME_BYTES
