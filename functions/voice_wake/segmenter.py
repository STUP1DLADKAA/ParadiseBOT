"""Turn a stream of 20 ms frames into utterances.

This is the cheapest and most important part of listening: a bot that sends
*every* sound it hears to a cloud speech-to-text API is expensive, slow and
rude. So the segmenter waits for something that looks like speech, keeps a
short pre-roll so the first syllable is never clipped, and closes the
utterance after a quiet gap.

Everything here is a pure function of the frames it is given. There is no
audio device, no network and no clock except the one passed in, which means
the endpointing rules can be tested exactly:

    IDLE  ──3 voiced frames──►  ACTIVE  ──700 ms silence──►  emit
      ▲                           │
      └──too short, discard───────┘
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from functions.voice_clip.constants import FRAME_SAMPLES, SAMPLE_RATE

FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE  # 0.02

#: A frame this far above the noise floor counts as speech. 0.012 of full
#: scale is about -38 dBFS: comfortably above a quiet room, well below a
#: normal speaking voice at a desk microphone.
DEFAULT_THRESHOLD = 0.012


def frame_rms(frame: np.ndarray | bytes) -> float:
    """RMS of a frame, normalised to 0.0–1.0 of full scale."""
    if isinstance(frame, bytes):
        if not frame:
            return 0.0
        samples = np.frombuffer(frame, dtype="<i2").astype(np.float32) / 32768.0
    else:
        array = np.asarray(frame)
        samples = array.astype(np.float32)
        # Integer PCM is full-scale, not 0.0–1.0: normalise it, or every
        # frame would look like speech.
        if array.dtype.kind in "iu":
            samples = samples / 32768.0
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples))))


def trim_silence(
    samples: np.ndarray, *, threshold: float = DEFAULT_THRESHOLD, keep_ms: int = 120
) -> np.ndarray:
    """Drop quiet frames from both ends, keeping ``keep_ms`` of context."""
    if samples.size == 0:
        return samples
    frames = samples.size // FRAME_SAMPLES
    if frames == 0:
        return samples
    energies = [
        frame_rms(samples[index * FRAME_SAMPLES : (index + 1) * FRAME_SAMPLES])
        for index in range(frames)
    ]
    voiced = [index for index, value in enumerate(energies) if value >= threshold]
    if not voiced:
        return samples[:0]
    keep = max(1, int(round((keep_ms / 1000.0) / FRAME_SECONDS)))
    first = max(0, voiced[0] - keep)
    last = min(frames, voiced[-1] + 1 + keep)
    return samples[first * FRAME_SAMPLES : last * FRAME_SAMPLES]


@dataclass(slots=True)
class Segment:
    """One closed utterance, ready to be transcribed."""

    user_id: int
    samples: np.ndarray  # int16, 48 kHz, mono
    voiced_seconds: float
    started_at: float
    peak: float = 0.0

    @property
    def duration(self) -> float:
        return self.samples.size / SAMPLE_RATE

    @property
    def empty(self) -> bool:
        return self.samples.size == 0


@dataclass(slots=True)
class _SpeakerState:
    """Per-speaker endpointing state."""

    frames: list[np.ndarray] = field(default_factory=list)
    preroll: list[np.ndarray] = field(default_factory=list)
    voiced: int = 0
    silent_run: int = 0
    active: bool = False
    started_at: float = 0.0
    peak: float = 0.0

    def reset(self) -> None:
        self.frames.clear()
        self.voiced = 0
        self.silent_run = 0
        self.active = False
        self.started_at = 0.0
        self.peak = 0.0


class SpeechSegmenter:
    """Groups frames per speaker into utterances.

    Parameters are in frames (20 ms) so the behaviour does not change with the
    sample rate:

    * ``onset_frames``  — voiced frames needed to start recording (3 = 60 ms);
    * ``end_silence_frames`` — quiet frames that close an utterance (35 = 700 ms);
    * ``min_voiced_frames`` — anything shorter is a door slam, not a sentence;
    * ``max_frames`` — hard stop, so a stuck microphone cannot buffer forever.
    """

    def __init__(
        self,
        *,
        threshold: float = DEFAULT_THRESHOLD,
        onset_frames: int = 3,
        end_silence_frames: int = 35,
        min_voiced_frames: int = 12,
        max_frames: int = 600,  # 12 s
        preroll_frames: int = 5,  # 100 ms of context before the onset
        max_speakers: int = 32,
    ) -> None:
        self.threshold = max(0.0, float(threshold))
        self.onset_frames = max(1, int(onset_frames))
        self.end_silence_frames = max(1, int(end_silence_frames))
        self.min_voiced_frames = max(1, int(min_voiced_frames))
        self.max_frames = max(self.end_silence_frames, int(max_frames))
        self.preroll_frames = max(0, int(preroll_frames))
        self.max_speakers = max(1, int(max_speakers))
        self._states: dict[int, _SpeakerState] = {}
        self.closed = 0
        self.discarded = 0

    # -- API ---------------------------------------------------------------

    def reset(self) -> None:
        self._states.clear()

    def forget(self, user_id: int) -> None:
        """Drop a speaker's half-finished utterance (they left the channel)."""
        self._states.pop(int(user_id), None)

    def feed(self, user_id: int, frame: np.ndarray, *, now: float) -> Segment | None:
        """Add one 20 ms frame; returns a :class:`Segment` when one closes."""
        uid = int(user_id)
        state = self._states.get(uid)
        if state is None:
            if len(self._states) >= self.max_speakers:
                # Evict the least recently started half-utterance.
                oldest = min(self._states, key=lambda key: self._states[key].started_at or 0.0)
                self._states.pop(oldest, None)
            state = _SpeakerState()
            self._states[uid] = state

        samples = np.asarray(frame, dtype=np.int16)
        energy = frame_rms(samples)
        loud = energy >= self.threshold

        if not state.active:
            if not loud:
                if self.preroll_frames:
                    state.preroll.append(samples)
                    if len(state.preroll) > self.preroll_frames:
                        state.preroll.pop(0)
                return None
            state.voiced += 1
            state.frames.append(samples)
            if state.voiced < self.onset_frames:
                return None
            # Confirm the onset: prepend the pre-roll and start for real.
            state.active = True
            state.started_at = now - (len(state.frames) + len(state.preroll)) * FRAME_SECONDS
            state.frames = state.preroll + state.frames
            state.preroll = []
            state.peak = max(state.peak, energy)
            return None

        state.frames.append(samples)
        if loud:
            state.voiced += 1
            state.silent_run = 0
            state.peak = max(state.peak, energy)
        else:
            state.silent_run += 1

        if state.silent_run >= self.end_silence_frames or len(state.frames) >= self.max_frames:
            return self._close(uid, state)
        return None

    def flush(self, user_id: int) -> Segment | None:
        """Close whatever is buffered (used when a speaker leaves)."""
        state = self._states.get(int(user_id))
        if state is None or not state.active:
            self._states.pop(int(user_id), None)
            return None
        return self._close(int(user_id), state)

    # -- internals ---------------------------------------------------------

    def _close(self, user_id: int, state: _SpeakerState) -> Segment | None:
        # Copy first: ``reset()`` clears the list in place, and a reference
        # would quietly become an empty utterance.
        frames = list(state.frames)
        voiced = state.voiced
        started = state.started_at
        peak = state.peak
        state.reset()
        if voiced < self.min_voiced_frames or not frames:
            self.discarded += 1
            return None
        samples = np.concatenate(frames)
        # Trim the trailing quiet gap: 700 ms of silence per request is pure
        # upload cost, and STT models get no benefit from it.
        trimmed = trim_silence(samples, threshold=self.threshold, keep_ms=120)
        if trimmed.size == 0:
            self.discarded += 1
            return None
        self.closed += 1
        return Segment(
            user_id=user_id,
            samples=trimmed.astype(np.int16, copy=False),
            voiced_seconds=voiced * FRAME_SECONDS,
            started_at=started,
            peak=peak,
        )


__all__ = [
    "DEFAULT_THRESHOLD",
    "FRAME_SECONDS",
    "Segment",
    "SpeechSegmenter",
    "frame_rms",
    "trim_silence",
]
