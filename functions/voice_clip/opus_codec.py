"""Per-speaker Opus decoder with native FEC/PLC and PyAV fallback."""

from __future__ import annotations

import glob
import logging
import os
import threading

import discord.opus
import numpy as np

from .constants import FRAME_SAMPLES, SAMPLE_RATE

log = logging.getLogger(__name__)

_OPUS_INIT_LOCK = threading.Lock()
_OPUS_INITIALIZED = False


def ensure_opus_loaded() -> bool:
    """Ensure ``discord.opus`` has loaded ``libopus`` (system or bundled wheel)."""
    global _OPUS_INITIALIZED
    if discord.opus.is_loaded():
        return True

    with _OPUS_INIT_LOCK:
        if discord.opus.is_loaded():
            return True
        if not _OPUS_INITIALIZED:
            _OPUS_INITIALIZED = True
            try:
                discord.opus._load_default()
            except Exception:  # noqa: BLE001
                log.debug("System libopus probe did not succeed", exc_info=True)

            if discord.opus.is_loaded():
                return True

            # Probe prebuilt PyAV wheel library directories (Linux/macOS/Windows).
            try:
                import av

                av_dir = os.path.dirname(os.path.abspath(av.__file__))
                site_dir = os.path.dirname(av_dir)
                patterns = (
                    os.path.join(site_dir, "av.libs", "libopus*"),
                    os.path.join(av_dir, ".dylibs", "libopus*"),
                    os.path.join(av_dir, "libopus*"),
                    os.path.join(av_dir, "*opus*.dll"),
                    os.path.join(site_dir, "av.libs", "*opus*.dll"),
                )
                for pattern in patterns:
                    for candidate in sorted(glob.glob(pattern)):
                        if not os.path.isfile(candidate):
                            continue
                        try:
                            discord.opus.load_opus(candidate)
                            if discord.opus.is_loaded():
                                log.debug("Loaded bundled libopus for voice clip decoding")
                                return True
                        except Exception:  # noqa: BLE001
                            log.debug("Could not load libopus candidate", exc_info=True)
            except ImportError:
                log.debug("PyAV not installed for bundled libopus discovery")

    return discord.opus.is_loaded()


_STEREO_S16_SCALE = np.float32(0.5 / 32768.0)
_MONO_S16_SCALE = np.float32(1.0 / 32768.0)


def _stereo_s16_bytes_to_mono_f32(
    pcm_bytes: bytes,
    scratch: np.ndarray | None = None,
) -> np.ndarray:
    """Convert 16-bit interleaved stereo PCM bytes to mono float32 in [-1.0, 1.0]."""
    raw = np.frombuffer(pcm_bytes, dtype=np.int16)
    if raw.size == 0:
        return np.zeros(0, dtype=np.float32)
    if raw.size % 2 == 0:
        n_frames = raw.size // 2
        out = (
            scratch
            if (scratch is not None and scratch.size == n_frames)
            else np.empty(n_frames, dtype=np.float32)
        )
        np.copyto(out, raw[0::2], casting="unsafe")
        out += raw[1::2]
        out *= _STEREO_S16_SCALE
        return out
    return raw.astype(np.float32) * _MONO_S16_SCALE


class SpeakerOpusDecoder:
    """Isolated 48 kHz mono Opus decoder state for one speaker stream."""

    def __init__(self) -> None:
        self._native: discord.opus.Decoder | None = None
        self._av_ctx = None
        self._av_resampler = None
        self._mono_scratch = np.empty(FRAME_SAMPLES, dtype=np.float32)
        self.decode_errors: int = 0

        if ensure_opus_loaded():
            self._native = discord.opus.Decoder()
        else:
            import av

            self._av_ctx = av.CodecContext.create("opus", "r")
            self._av_resampler = av.AudioResampler(
                format="flt",
                layout="mono",
                rate=SAMPLE_RATE,
            )

    def reset(self) -> None:
        """Re-create fresh codec state across discontinuous stream epochs."""
        if self._native is not None:
            self._native = discord.opus.Decoder()
        elif self._av_ctx is not None:
            import av

            self._av_ctx = av.CodecContext.create("opus", "r")
            self._av_resampler = av.AudioResampler(
                format="flt",
                layout="mono",
                rate=SAMPLE_RATE,
            )

    def decode_packet(self, opus_data: bytes) -> np.ndarray | None:
        """Decode one Opus packet into 48 kHz mono float32 samples, or None on error."""
        if not opus_data:
            return None
        if self._native is not None:
            try:
                pcm_bytes = self._native.decode(opus_data, fec=False)
                return _stereo_s16_bytes_to_mono_f32(pcm_bytes, self._mono_scratch)
            except Exception:  # noqa: BLE001 - malformed packet must never abort export
                self.decode_errors += 1
                return None

        try:
            import av

            chunks: list[np.ndarray] = []
            for frame in self._av_ctx.decode(av.Packet(opus_data)):
                for mono_frame in self._av_resampler.resample(frame):
                    buf = bytes(mono_frame.planes[0])
                    arr = np.frombuffer(buf, dtype=np.float32)[: mono_frame.samples].copy()
                    chunks.append(arr)
            if not chunks:
                return np.zeros(FRAME_SAMPLES, dtype=np.float32)
            return chunks[0] if len(chunks) == 1 else np.concatenate(chunks)
        except Exception:  # noqa: BLE001
            self.decode_errors += 1
            return None

    def decode_fec(self, next_opus_data: bytes) -> np.ndarray:
        """Recover 1 lost frame using next packet's in-band FEC, falling back to PLC."""
        if self._native is not None and next_opus_data:
            try:
                pcm_bytes = self._native.decode(next_opus_data, fec=True)
                out = _stereo_s16_bytes_to_mono_f32(pcm_bytes)
                if out.size > 0:
                    return out
            except Exception:  # noqa: BLE001
                self.decode_errors += 1
        return self.decode_plc()

    def decode_plc(self) -> np.ndarray:
        """Conceal one 20 ms missing Opus frame without shifting timeline."""
        if self._native is not None:
            try:
                pcm_bytes = self._native.decode(None, fec=False)
                out = _stereo_s16_bytes_to_mono_f32(pcm_bytes)
                if out.size > 0:
                    return out
            except Exception:  # noqa: BLE001
                self.decode_errors += 1
        return np.zeros(FRAME_SAMPLES, dtype=np.float32)
