"""The little "I heard you" chime.

When the wake phrase lands, Paradise has work to do before it can speak: build
the question, ask the model, synthesise the answer. On a slow link that is a
second or two of silence, which is indistinguishable from being ignored — so a
short chime plays the instant the phrase is recognised.

The tone is generated here, not shipped as a file: two soft sine notes a fifth
apart with short fades, which reads as "yes?" rather than as a notification.
It is 48 kHz stereo 16-bit PCM, the exact format ``discord.PCMAudio`` wants, so
it plays without FFmpeg and cannot fail on a machine where speech synthesis
would.

    heard!  ┌─── 0.42 s ───┐
            │  A5  →  E6    │  (880 Hz, then 1318.5 Hz)
            └───────────────┘
"""

from __future__ import annotations

import math

import numpy as np

SAMPLE_RATE = 48_000
#: Total length of the cue. Anything longer starts to feel like an interruption.
CUE_SECONDS = 0.42
#: The two notes, in Hz. A5 then E6 — a rising fifth, unmistakably "yes?".
NOTE_LOW = 880.0
NOTE_HIGH = 1_318.51
#: Peak level. Quiet on purpose: it must sit under the conversation.
CUE_PEAK = 0.22
#: Fade in / out, so the tone starts and stops without a click.
FADE_SECONDS = 0.02


def _note(count: int, frequency: float, *, peak: float = CUE_PEAK) -> np.ndarray:
    """One note: a sine with a short attack and release envelope."""
    t = np.arange(count, dtype=np.float32) / SAMPLE_RATE
    wave = np.sin(2.0 * math.pi * frequency * t)
    envelope = np.ones(count, dtype=np.float32)

    fade = min(max(1, int(FADE_SECONDS * SAMPLE_RATE)), max(1, count // 2))
    ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
    envelope[:fade] = ramp
    envelope[-fade:] = ramp[::-1]
    return wave * envelope * np.float32(peak)


def build_cue_pcm(*, seconds: float = CUE_SECONDS, rate: int = SAMPLE_RATE) -> bytes:
    """Return the chime as stereo 16-bit PCM at ``rate``, ready for PCMAudio.

    Two notes are placed one after the other (60 % / 40 % of the length); they
    are mixed, not summed, so the peak never doubles.
    """
    total = max(1, int(rate * max(0.1, float(seconds))))
    split = int(total * 0.6)

    mono = np.zeros(total, dtype=np.float32)
    mono[:split] = _note(split, NOTE_LOW)[:split]

    rest = total - split
    if rest > 0:
        # A fifth up, a touch quieter: a question, not an alarm.
        mono[split:] = _note(rest, NOTE_HIGH, peak=CUE_PEAK * 0.9)[:rest]

    pcm = np.clip(mono * 32767.0, -32768, 32767).astype("<i2")
    stereo = np.empty(pcm.size * 2, dtype="<i2")
    stereo[0::2] = pcm
    stereo[1::2] = pcm
    return stereo.tobytes()


__all__ = ["CUE_SECONDS", "build_cue_pcm"]
