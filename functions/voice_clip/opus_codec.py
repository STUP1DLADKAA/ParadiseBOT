"""Per-speaker Opus decoder with native libopus FEC/PLC and PyAV 18/19 fallback."""

from __future__ import annotations

import glob
import logging
import os
import threading
from typing import Any

import discord.opus
import numpy as np

from .constants import FRAME_SAMPLES, SAMPLE_RATE

log = logging.getLogger(__name__)

# RFC 6716 §3.1: valid Opus packet duration at 48 kHz is 2.5 ms (120 samples)
# through 120 ms (5,760 samples), containing 1..48 frames.
MIN_OPUS_FRAME_SAMPLES: int = 120
MAX_OPUS_FRAME_SAMPLES: int = 5_760
MAX_OPUS_PACKET_FRAMES: int = 48

_OPUS_INIT_LOCK = threading.Lock()
_OPUS_PROBE_FAILED = False

_SYSTEM_OPUS_PATTERNS: tuple[str, ...] = (
    "/opt/homebrew/lib/libopus*.dylib",
    "/usr/local/lib/libopus*.dylib",
    "/usr/lib/*-linux-gnu/libopus.so*",
    "/lib/*-linux-gnu/libopus.so*",
    "/usr/lib64/libopus.so*",
    "/usr/lib/libopus.so*",
)

_STEREO_S16_SCALE = np.float32(0.5 / 32768.0)
_MONO_S16_SCALE = np.float32(1.0 / 32768.0)


def _try_load_candidates(patterns: tuple[str, ...]) -> bool:
    for pattern in patterns:
        for candidate in sorted(glob.glob(pattern)):
            if not os.path.isfile(candidate):
                continue
            try:
                discord.opus.load_opus(candidate)
                if discord.opus.is_loaded():
                    return True
            except Exception:  # noqa: BLE001
                log.debug("Could not load libopus candidate %s", candidate, exc_info=True)
    return False


def ensure_opus_loaded() -> bool:
    """Ensure ``discord.opus`` has loaded ``libopus`` (system or bundled PyAV wheel)."""
    global _OPUS_PROBE_FAILED
    if discord.opus.is_loaded():
        return True

    with _OPUS_INIT_LOCK:
        if discord.opus.is_loaded():
            return True
        if _OPUS_PROBE_FAILED:
            return False

        try:
            discord.opus._load_default()
        except Exception:  # noqa: BLE001
            log.debug("Default libopus probe did not succeed", exc_info=True)

        if discord.opus.is_loaded():
            return True

        # Probe common macOS Homebrew & Linux multiarch locations missed by
        # ctypes.util.find_library in slim containers / isolated virtualenvs.
        if _try_load_candidates(_SYSTEM_OPUS_PATTERNS):
            log.debug("Loaded system libopus via explicit library search")
            return True

        # Probe prebuilt PyAV 18/19 wheel library directories (Linux/macOS/Windows).
        try:
            import av

            av_dir = os.path.dirname(os.path.abspath(av.__file__))
            site_dir = os.path.dirname(av_dir)
            av_patterns = (
                os.path.join(site_dir, "av.libs", "libopus*"),
                os.path.join(av_dir, ".dylibs", "libopus*"),
                os.path.join(av_dir, "libopus*"),
                os.path.join(site_dir, "av.libs", "*opus*.dll"),
                os.path.join(av_dir, "*opus*.dll"),
            )
            if _try_load_candidates(av_patterns):
                log.debug("Loaded bundled PyAV libopus for voice clip decoding")
                return True
        except ImportError:
            log.debug("PyAV not installed for bundled libopus discovery")

        _OPUS_PROBE_FAILED = True
        return discord.opus.is_loaded()


def _stereo_s16_bytes_to_mono_f32(pcm_bytes: bytes) -> np.ndarray:
    """Convert 16-bit interleaved stereo PCM bytes to owned mono float32 in [-1, 1]."""
    raw = np.frombuffer(pcm_bytes, dtype=np.int16)
    if raw.size == 0:
        return np.zeros(0, dtype=np.float32)
    if raw.size % 2 == 0:
        n_frames = raw.size // 2
        out = np.empty(n_frames, dtype=np.float32)
        np.copyto(out, raw[0::2], casting="unsafe")
        out += raw[1::2]
        out *= _STEREO_S16_SCALE
        return out
    return raw.astype(np.float32) * _MONO_S16_SCALE


def _create_av_opus_decoder() -> Any:
    """Open a PyAV Opus decoder, preferring Xiph ``libopus`` over built-in ``opus``."""
    import av

    for codec_name in ("libopus", "opus"):
        try:
            ctx = av.CodecContext.create(codec_name, "r")
            return ctx
        except Exception:  # noqa: BLE001
            continue
    raise RuntimeError("Neither 'libopus' nor 'opus' decoder is available in PyAV")


def _av_frame_to_mono_f32(frame: Any, resampler: Any) -> tuple[np.ndarray, Any]:
    """Convert a decoded PyAV ``AudioFrame`` into clean 48 kHz mono float32.

    Uses exact arithmetic average downmixing (``0.5 * (L + R)``) so levels
    match native ``libopus`` and mid-stream mono/stereo TOC changes cannot
    cause ``swresample`` matrix spikes.
    """
    n_samples = int(getattr(frame, "samples", 0) or 0)
    if n_samples <= 0:
        return np.zeros(0, dtype=np.float32), resampler

    rate = int(getattr(frame, "sample_rate", SAMPLE_RATE) or SAMPLE_RATE)
    fmt = getattr(getattr(frame, "format", None), "name", "")

    if rate == SAMPLE_RATE and fmt in ("s16", "s16p", "flt", "fltp"):
        arr = frame.to_ndarray()
        scale = _MONO_S16_SCALE if fmt in ("s16", "s16p") else np.float32(1.0)
        is_planar = bool(frame.format.is_planar)

        if is_planar:
            # Shape: (channels, samples)
            channels = arr.shape[0]
            if channels == 1:
                mono = arr[0, :n_samples].astype(np.float32, copy=True)
                if scale != 1.0:
                    mono *= scale
            else:
                mono = np.mean(arr[:, :n_samples], axis=0, dtype=np.float32)
                if scale != 1.0:
                    mono *= scale
        else:
            # Packed shape in PyAV: (1, samples * channels)
            flat = arr.reshape(-1)
            channels = max(1, flat.size // n_samples)
            usable = n_samples * channels
            if channels == 1:
                mono = flat[:n_samples].astype(np.float32, copy=True)
                if scale != 1.0:
                    mono *= scale
            elif channels == 2 and fmt == "s16":
                mono = np.empty(n_samples, dtype=np.float32)
                np.copyto(mono, flat[0:usable:2], casting="unsafe")
                mono += flat[1:usable:2]
                mono *= _STEREO_S16_SCALE
            else:
                frames_2d = flat[:usable].reshape(n_samples, channels)
                mono = np.mean(frames_2d, axis=1, dtype=np.float32)
                if scale != 1.0:
                    mono *= scale

        np.nan_to_num(mono, copy=False, nan=0.0, posinf=1.0, neginf=-1.0)
        np.clip(mono, -1.0, 1.0, out=mono)
        return mono, resampler

    # Non-48 kHz or unusual format fallback via AudioResampler.
    import av

    if resampler is None:
        resampler = av.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE)
    parts: list[np.ndarray] = []
    for mono_frame in resampler.resample(frame):
        m_samples = int(getattr(mono_frame, "samples", 0) or 0)
        if m_samples <= 0:
            continue
        raw_arr = mono_frame.to_ndarray().reshape(-1)[:m_samples]
        chunk = raw_arr.astype(np.float32, copy=True)
        np.nan_to_num(chunk, copy=False, nan=0.0, posinf=1.0, neginf=-1.0)
        np.clip(chunk, -1.0, 1.0, out=chunk)
        parts.append(chunk)
    if not parts:
        return np.zeros(0, dtype=np.float32), resampler
    return (parts[0] if len(parts) == 1 else np.concatenate(parts)), resampler


class SpeakerOpusDecoder:
    """Isolated 48 kHz mono Opus decoder state for one speaker stream."""

    def __init__(self, *, prefer_pyav: bool = False) -> None:
        self._prefer_pyav = bool(prefer_pyav)
        self._native: discord.opus.Decoder | None = None
        self._av_ctx: Any = None
        self._av_resampler: Any = None
        self._last_frame_samples: int = FRAME_SAMPLES
        self.decode_errors: int = 0
        self._open_decoder()

    def _open_decoder(self) -> None:
        self._native = None
        self._av_ctx = None
        self._av_resampler = None
        self._last_frame_samples = FRAME_SAMPLES
        if not self._prefer_pyav and ensure_opus_loaded():
            self._native = discord.opus.Decoder()
        else:
            self._av_ctx = _create_av_opus_decoder()

    @property
    def backend(self) -> str:
        return "libopus" if self._native is not None else "pyav"

    def reset(self) -> None:
        """Re-create fresh codec state across discontinuous stream epochs."""
        self._open_decoder()

    def _remember_duration(self, sample_count: int) -> None:
        if MIN_OPUS_FRAME_SAMPLES <= sample_count <= MAX_OPUS_FRAME_SAMPLES:
            self._last_frame_samples = sample_count

    def decode_packet(self, opus_data: bytes) -> np.ndarray | None:
        """Decode one Opus packet into 48 kHz mono float32 samples, or None on error."""
        if not opus_data:
            return None

        if self._native is not None:
            try:
                nb_frames = discord.opus.Decoder.packet_get_nb_frames(opus_data)
                spf = discord.opus.Decoder.packet_get_samples_per_frame(opus_data)
                total_samples = nb_frames * spf
                if (
                    nb_frames < 1
                    or nb_frames > MAX_OPUS_PACKET_FRAMES
                    or total_samples < MIN_OPUS_FRAME_SAMPLES
                    or total_samples > MAX_OPUS_FRAME_SAMPLES
                ):
                    self.decode_errors += 1
                    return None
                pcm_bytes = self._native.decode(opus_data, fec=False)
                mono = _stereo_s16_bytes_to_mono_f32(pcm_bytes)
                if mono.size == 0:
                    self.decode_errors += 1
                    return None
                self._remember_duration(int(mono.size))
                return mono
            except Exception:  # noqa: BLE001 - malformed packet must never abort export
                self.decode_errors += 1
                return None

        try:
            import av

            chunks: list[np.ndarray] = []
            for frame in self._av_ctx.decode(av.Packet(opus_data)):
                mono, self._av_resampler = _av_frame_to_mono_f32(
                    frame, self._av_resampler
                )
                if mono.size > 0:
                    chunks.append(mono)
            if not chunks:
                return np.zeros(self._last_frame_samples, dtype=np.float32)
            out = chunks[0] if len(chunks) == 1 else np.concatenate(chunks)
            self._remember_duration(int(out.size))
            return out
        except Exception:  # noqa: BLE001
            self.decode_errors += 1
            return None

    def decode_fec(self, next_opus_data: bytes) -> np.ndarray:
        """Recover 1 lost frame using next packet's in-band FEC, falling back to PLC."""
        if self._native is not None and next_opus_data:
            try:
                nb_frames = discord.opus.Decoder.packet_get_nb_frames(next_opus_data)
                spf = discord.opus.Decoder.packet_get_samples_per_frame(next_opus_data)
                total_samples = nb_frames * spf
                if (
                    1 <= nb_frames <= MAX_OPUS_PACKET_FRAMES
                    and MIN_OPUS_FRAME_SAMPLES <= total_samples <= MAX_OPUS_FRAME_SAMPLES
                ):
                    pcm_bytes = self._native.decode(next_opus_data, fec=True)
                    out = _stereo_s16_bytes_to_mono_f32(pcm_bytes)
                    if out.size > 0:
                        self._remember_duration(int(out.size))
                        return out
            except Exception:  # noqa: BLE001
                self.decode_errors += 1
        return self.decode_plc()

    def decode_plc(self) -> np.ndarray:
        """Conceal one missing Opus frame without shifting timeline."""
        if self._native is not None:
            try:
                pcm_bytes = self._native.decode(None, fec=False)
                out = _stereo_s16_bytes_to_mono_f32(pcm_bytes)
                if out.size > 0:
                    return out
            except Exception:  # noqa: BLE001
                self.decode_errors += 1
        return np.zeros(self._last_frame_samples, dtype=np.float32)
