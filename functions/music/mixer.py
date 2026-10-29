"""The ducking mixer: one song, one voice, 50 frames a second.

Paradise never stops the song to talk. It mixes instead:

    music gain  ──1.0────┐
                          ╲            ← 240 ms fade down
                           0.15 ────  ← while the answer is being spoken
                          ╱
                ──1.0────┘            ← fade back up, never a hard step

A hard volume step is audible as a click, so the gain walks toward its target
one frame at a time. The maths is deliberately plain: two signed-16-bit
streams, multiplied, added, clipped. No resampling, no codec, no rhythm
analysis — 20 ms packets arrive at exactly the same instant, which is the
whole reason the decoder works in frames.
"""

from __future__ import annotations

import logging
import threading
from collections import deque

import discord
import numpy as np

from .decoder import FRAME_BYTES, FRAME_SECONDS, PcmStream, silence_frame

log = logging.getLogger(__name__)

#: 12 frames = 240 ms. Long enough to hide the step, short enough that a
#: one-word answer does not spend half its length fading in.
FADE_FRAMES = 12

_INT16 = np.dtype("<i2")


def frame_count(pcm: bytes) -> int:
    return len(pcm) // FRAME_BYTES


def apply_gain(frame: bytes, gain: float) -> bytes:
    """Scale a signed-16-bit frame, clipping instead of wrapping."""
    if not frame or gain == 1.0:
        return frame
    samples = np.frombuffer(frame, dtype=_INT16).astype(np.int32)
    scaled = np.clip(samples * float(gain), -32768, 32767)
    return scaled.astype(_INT16).tobytes()


def mix_frames(
    music: bytes,
    speech: bytes,
    music_gain: float,
    speech_gain: float = 1.0,
) -> bytes:
    """Add a speech frame on top of a music frame.

    *music* is already scaled by whatever the duck level currently is; *speech*
    is added at full level. The sum is clipped at 16-bit bounds, so a loud
    singer under a loud sentence distorts for a fraction of a second rather
    than wrapping around into white noise (which is what integer overflow
    sounds like).
    """
    if not speech:
        return apply_gain(music, music_gain) if music else silence_frame()
    if not music:
        return apply_gain(speech, speech_gain)

    music_samples = np.frombuffer(music, dtype=_INT16).astype(np.int32)
    speech_samples = np.frombuffer(speech, dtype=_INT16).astype(np.int32)
    length = max(len(music_samples), len(speech_samples))
    out = np.zeros(length, dtype=np.int32)
    if music_gain != 1.0:
        music_samples = (music_samples * float(music_gain)).astype(np.int32)
    if speech_gain != 1.0:
        speech_samples = (speech_samples * float(speech_gain)).astype(np.int32)
    out[: len(music_samples)] += music_samples
    out[: len(speech_samples)] += speech_samples
    np.clip(out, -32768, 32767, out=out)
    return out.astype(_INT16).tobytes()


class GainRamp:
    """Walks the music gain toward a target over a fixed number of frames."""

    def __init__(self, *, start: float = 1.0, fade_frames: int = FADE_FRAMES) -> None:
        self.fade_frames = max(1, int(fade_frames))
        self._value = float(start)
        self._target = float(start)
        self._remaining = 0

    @property
    def value(self) -> float:
        return self._value

    @property
    def target(self) -> float:
        return self._target

    @property
    def settled(self) -> bool:
        return self._remaining == 0 and self._value == self._target

    def set_target(self, target: float) -> None:
        clamped = max(0.0, min(4.0, float(target)))
        if clamped == self._target:
            return
        self._target = clamped
        self._remaining = self.fade_frames

    def step(self) -> float:
        """Advance one frame and return the gain to use for it."""
        if self._remaining > 0:
            delta = self._target - self._value
            self._remaining -= 1
            if self._remaining == 0:
                self._value = self._target
            else:
                self._value += delta / (self._remaining + 1)
        return self._value


class MusicMixer(discord.AudioSource):
    """Discord audio source: music, speech, and the duck between them."""

    def __init__(
        self,
        music: PcmStream | None,
        *,
        volume: float = 0.5,
        duck_volume: float = 0.15,
        fade_frames: int = FADE_FRAMES,
        frame_timeout: float = 1.0,
    ) -> None:
        self.music = music
        self.master_volume = max(0.0, min(2.0, float(volume)))
        self.duck_volume = max(0.0, min(1.0, float(duck_volume)))
        self.frame_timeout = frame_timeout
        self._ramp = GainRamp(start=1.0, fade_frames=fade_frames)
        self._speech: deque[bytes] = deque()
        self._speech_bytes = 0
        self._lock = threading.Lock()
        self._music_ended = music is None
        self._closed = False
        self._starved = 0
        self.frames_out = 0
        self.mixed_frames = 0

    # -- audio source API ---------------------------------------------------

    def is_opus(self) -> bool:
        return False

    def read(self) -> bytes:
        if self._closed:
            return b""

        music_frame = self._next_music()
        speech_frame = self._next_speech()

        if music_frame == b"" and not speech_frame:
            return b""

        gain = 1.0
        if music_frame:
            self._ramp.set_target(self.duck_volume if speech_frame else 1.0)
            gain = self._ramp.step() * self.master_volume
        else:
            # Speech only (no song, or the song just ended): no ducking needed.
            self._ramp.set_target(1.0)
            gain = self._ramp.step()

        frame = mix_frames(music_frame, speech_frame, gain, 1.0)
        self.frames_out += 1
        if music_frame and speech_frame:
            self.mixed_frames += 1
        return frame

    def cleanup(self) -> None:
        self._closed = True
        with self._lock:
            self._speech.clear()
            self._speech_bytes = 0
        if self.music is not None:
            try:
                self.music.close()
            except Exception:  # noqa: BLE001 - cleanup is best effort
                log.debug("music stream cleanup failed", exc_info=True)
            self.music = None

    # -- speech overlay -----------------------------------------------------

    def push_speech(self, pcm: bytes) -> int:
        """Queue decoded speech; returns how many frames were accepted."""
        if not pcm:
            return 0
        frames = 0
        with self._lock:
            for offset in range(0, len(pcm), FRAME_BYTES):
                chunk = pcm[offset : offset + FRAME_BYTES]
                if len(chunk) < FRAME_BYTES:
                    chunk = chunk + b"\x00" * (FRAME_BYTES - len(chunk))
                self._speech.append(chunk)
                self._speech_bytes += len(chunk)
                frames += 1
        return frames

    @property
    def speech_pending(self) -> bool:
        with self._lock:
            return bool(self._speech)

    @property
    def speech_seconds(self) -> float:
        with self._lock:
            return self._speech_bytes / (FRAME_BYTES / FRAME_SECONDS)

    @property
    def ducked(self) -> bool:
        return self._ramp.target < 1.0

    @property
    def position(self) -> float:
        return self.music.position if self.music is not None else 0.0

    @property
    def ended(self) -> bool:
        return self._music_ended and not self.speech_pending

    # -- internals ----------------------------------------------------------

    def _next_speech(self) -> bytes:
        with self._lock:
            if not self._speech:
                return b""
            frame = self._speech.popleft()
            self._speech_bytes -= len(frame)
            return frame

    def _next_music(self) -> bytes:
        if self.music is None or self._music_ended:
            return b""
        frame = self.music.read_frame(timeout=self.frame_timeout)
        if frame is None:
            # A stalled read is silence, not the end of the song.
            self._starved += 1
            if self._starved % 50 == 0:  # ~once a second
                log.debug("music stream starved (%d frames)", self._starved)
            return silence_frame()
        self._starved = 0
        if frame == b"":
            self._music_ended = True
            return b""
        return frame
