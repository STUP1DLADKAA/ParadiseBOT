"""Bounded, non-blocking voice clip export pipeline (decode -> mix -> M4A)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
import logging
import os
import shutil
import tempfile

from functions.dictionary.playback import resolve_ffmpeg_executable

from .buffer import ClipIntervalSnapshot
from .constants import (
    ClipBusyError,
    ClipExportError,
    ClipExportTimeoutError,
    ClipFileTooLargeError,
    ClipUnavailableError,
    SAMPLE_RATE,
)
from .mixer import MixResult, mix_snapshot_to_pcm_f32

log = logging.getLogger(__name__)

# Mastering chain for the mixed clip. The *level* is set in numpy, with one
# constant gain per clip (:func:`functions.voice_clip.mixer.apply_static_gain`)
# — a single-pass ``loudnorm`` instead rides the gain up and down between
# sentences, which is audible as a wobble on a short clip.
#
# What is left is deliberately minimal, because every stage here is a stage
# between the speaker's mouth and the file:
#   * highpass 70 Hz — removes desk rumble and the sub-bass thump Discord's
#     Opus stream carries; nothing speech lives down there.
#   * lowpass 18.5 kHz — keeps the top of the speech band intact (sibilance,
#     the "air" that makes a voice sound present) while staying well under
#     Nyquist for 48 kHz, so nothing aliases. A 16 kHz cut is a telephone
#     master; it is audible as dullness on a good pair of headphones.
#   * alimiter 0.95 — catches the last inter-sample transient. It is an
#     emergency brake, not a level control: the level is already set once,
#     statically, in numpy.
DEFAULT_AUDIO_FILTER = (
    "highpass=f=70,"
    "lowpass=f=18500,"
    "alimiter=limit=0.95:attack=5:release=50:level=disabled"
)


@dataclass(slots=True)
class ExportedClip:
    """Validated temporary M4A clip ready for DM upload, with deterministic cleanup."""

    file_path: str
    filename: str
    file_size_bytes: int
    requested_seconds: int
    captured_seconds: float
    speaker_count: int
    temp_dir: str
    _release_cb: object = None
    _cleaned: bool = False

    def cleanup(self) -> None:
        """Idempotently delete temporary clip file and release disk budget."""
        if self._cleaned:
            return
        self._cleaned = True
        try:
            if self.temp_dir and os.path.exists(self.temp_dir):
                shutil.rmtree(self.temp_dir, ignore_errors=True)
        except OSError:
            log.debug("Could not remove temporary clip directory", exc_info=True)
        finally:
            if callable(self._release_cb):
                try:
                    self._release_cb()
                except Exception:  # noqa: BLE001
                    log.debug("Temp disk accounting release callback failed", exc_info=True)

    def __enter__(self) -> "ExportedClip":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.cleanup()


def build_clip_filename(
    captured_seconds: float,
    *,
    now_utc: datetime | None = None,
) -> str:
    """Create a clean, non-sensitive filename with UTC timestamp and duration."""
    stamp = (now_utc or datetime.now(UTC)).strftime("%Y%m%d_%H%M%SZ")
    whole = max(1, int(round(captured_seconds)))
    return f"paradise_clip_{stamp}_{whole}s.m4a"


class ClipExportPipeline:
    """Bounded concurrency export coordinator for rolling voice clips."""

    def __init__(
        self,
        *,
        max_concurrent_exports: int = 1,
        max_queued_exports: int = 2,
        export_timeout_seconds: float = 30.0,
        max_file_bytes: int = 9_961_472,
        max_temp_disk_bytes: int = 32 * 1024 * 1024,
        bitrate_kbps: int = 128,
        audio_filter: str = "",
        ffmpeg_executable: str | None = None,
    ) -> None:
        self.max_concurrent_exports: int = max(1, int(max_concurrent_exports))
        self.max_queued_exports: int = max(0, int(max_queued_exports))
        self.export_timeout_seconds: float = max(1.0, float(export_timeout_seconds))
        self.max_file_bytes: int = max(65_536, int(max_file_bytes))
        self.max_temp_disk_bytes: int = max(262_144, int(max_temp_disk_bytes))
        self.bitrate_kbps: int = max(64, min(320, int(bitrate_kbps)))
        self.audio_filter: str = (audio_filter or "").strip() or DEFAULT_AUDIO_FILTER
        self._explicit_ffmpeg = ffmpeg_executable

        self._semaphore = asyncio.Semaphore(self.max_concurrent_exports)
        self._inflight: int = 0
        self._active_temp_bytes: int = 0
        self._live_temp_dirs: set[str] = set()
        self._closed: bool = False

    @property
    def max_total_inflight(self) -> int:
        return self.max_concurrent_exports + self.max_queued_exports

    @property
    def inflight_count(self) -> int:
        return self._inflight

    @property
    def active_temp_bytes(self) -> int:
        return self._active_temp_bytes

    def _resolve_ffmpeg(self) -> str:
        exe = self._explicit_ffmpeg or resolve_ffmpeg_executable()
        if not exe:
            raise ClipUnavailableError(
                "Audio encoder (FFmpeg) is not available on this host."
            )
        return exe

    async def export(self, snapshot: ClipIntervalSnapshot) -> ExportedClip:
        """Decode, mix and encode ``snapshot`` into a validated temporary M4A file."""
        if self._closed:
            raise ClipUnavailableError("Clip exporter is shutting down.")

        if self._inflight >= self.max_total_inflight:
            raise ClipBusyError(
                "Clip export is currently busy processing earlier requests. "
                "Please try again in a few seconds."
            )

        self._inflight += 1
        try:
            return await asyncio.wait_for(
                self._run_export_with_semaphore(snapshot),
                timeout=self.export_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            log.warning(
                "Voice clip export timed out after %.1fs (guild=%s)",
                self.export_timeout_seconds,
                snapshot.guild_id,
            )
            raise ClipExportTimeoutError(
                "Clip generation timed out before finishing. Try a shorter duration."
            ) from exc
        finally:
            self._inflight = max(0, self._inflight - 1)

    async def _run_export_with_semaphore(
        self,
        snapshot: ClipIntervalSnapshot,
    ) -> ExportedClip:
        async with self._semaphore:
            if self._closed:
                raise ClipUnavailableError("Clip exporter is shutting down.")
            return await self._decode_mix_and_encode(snapshot)

    async def _decode_mix_and_encode(
        self,
        snapshot: ClipIntervalSnapshot,
    ) -> ExportedClip:
        ffmpeg_exe = self._resolve_ffmpeg()

        # Estimate upper-bound encoded bytes to enforce temporary disk budget up front.
        estimated_bytes = (
            int(snapshot.captured_seconds * self.bitrate_kbps * 1000 / 8 * 1.5)
            + 65_536
        )
        if self._active_temp_bytes + estimated_bytes > self.max_temp_disk_bytes:
            raise ClipBusyError(
                "Temporary clip storage capacity is currently full. Try again shortly."
            )

        # 1. Decode per-speaker Opus streams, align timestamps, and mix in worker thread.
        mix_result: MixResult = await asyncio.to_thread(
            mix_snapshot_to_pcm_f32, snapshot
        )
        pcm_f32_bytes = mix_result.samples.tobytes()

        # 2. Allocate private temporary directory (mode 0700) and reserve disk budget.
        temp_dir = tempfile.mkdtemp(prefix="paradise_clip_")
        try:
            os.chmod(temp_dir, 0o700)
        except OSError:
            log.debug("chmod 0700 not supported on temp dir", exc_info=True)

        reserved_bytes = estimated_bytes
        self._active_temp_bytes += reserved_bytes
        self._live_temp_dirs.add(temp_dir)
        handed_off = False

        filename = build_clip_filename(mix_result.duration_seconds)
        out_path = os.path.join(temp_dir, filename)

        try:
            await self._encode_f32_to_m4a(
                ffmpeg_exe=ffmpeg_exe,
                pcm_f32_bytes=pcm_f32_bytes,
                out_path=out_path,
            )
            del pcm_f32_bytes

            if not os.path.isfile(out_path):
                raise ClipExportError("Audio encoder did not produce an output file.")

            actual_size = os.path.getsize(out_path)
            if actual_size < 256:
                raise ClipExportError("Encoded audio file was unexpectedly empty.")

            if actual_size > self.max_file_bytes:
                raise ClipFileTooLargeError(
                    "The generated clip exceeds Discord's upload size limit. "
                    "Try requesting a shorter duration."
                )

            if self._active_temp_bytes - reserved_bytes + actual_size > self.max_temp_disk_bytes:
                raise ClipBusyError(
                    "Temporary clip storage limit reached. Try again shortly."
                )

            # Adjust reservation from estimate to exact file size until cleanup().
            self._active_temp_bytes = max(
                0, self._active_temp_bytes - reserved_bytes + actual_size
            )
            reserved_bytes = actual_size

            def _release() -> None:
                self._active_temp_bytes = max(0, self._active_temp_bytes - actual_size)
                self._live_temp_dirs.discard(temp_dir)

            clip = ExportedClip(
                file_path=out_path,
                filename=filename,
                file_size_bytes=actual_size,
                requested_seconds=snapshot.requested_seconds,
                captured_seconds=mix_result.duration_seconds,
                speaker_count=mix_result.speaker_count,
                temp_dir=temp_dir,
                _release_cb=_release,
            )
            handed_off = True
            return clip
        finally:
            if not handed_off:
                self._active_temp_bytes = max(0, self._active_temp_bytes - reserved_bytes)
                self._live_temp_dirs.discard(temp_dir)
                shutil.rmtree(temp_dir, ignore_errors=True)

    async def _encode_f32_to_m4a(
        self,
        *,
        ffmpeg_exe: str,
        pcm_f32_bytes: bytes,
        out_path: str,
    ) -> None:
        """Encode 48 kHz mono float32 PCM to AAC-LC in M4A with faststart.

        One pass, no intermediate files: float32 PCM arrives on stdin and the
        finished M4A lands in a temp directory. The clip is decoded once
        (Opus → PCM), mixed once, encoded once — the only lossy step after
        Discord's own Opus is this one encode, at a bitrate chosen to make it
        transparent for speech.

        48 kHz mono is the source's own format (Discord voice is mono), so
        there is no resample and no fake stereo. ``+faststart`` moves the
        moov atom to the front, so Discord can start playing the attachment
        before the download finishes.
        """
        cmd = [
            ffmpeg_exe,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "f32le",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            "1",
            "-i",
            "pipe:0",
            "-af",
            self.audio_filter,
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            "1",
            "-c:a",
            "aac",
            "-profile:a",
            "aac_low",
            "-b:a",
            f"{self.bitrate_kbps}k",
            "-movflags",
            "+faststart",
            out_path,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            log.error("Failed to start FFmpeg encoder process: %s", type(exc).__name__)
            raise ClipExportError("Could not start the audio encoder.") from exc

        try:
            _, stderr_bytes = await proc.communicate(input=pcm_f32_bytes)
        except asyncio.CancelledError:
            try:
                proc.kill()
            except ProcessLookupError:
                log.debug("FFmpeg process already exited on cancel")
            await proc.wait()
            raise

        if proc.returncode != 0:
            err_summary = (stderr_bytes or b"").decode("utf-8", errors="replace").strip()
            log.error(
                "FFmpeg clip encode failed (rc=%s, detail=%s)",
                proc.returncode,
                err_summary[:200],
            )
            raise ClipExportError("Audio encoding failed while building the clip.")

    async def shutdown(self) -> None:
        """Mark pipeline closed and clean up any leftover temporary directories."""
        self._closed = True
        for temp_dir in list(self._live_temp_dirs):
            shutil.rmtree(temp_dir, ignore_errors=True)
        self._live_temp_dirs.clear()
        self._active_temp_bytes = 0
