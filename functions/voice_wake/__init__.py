"""Spoken wake word: hear "hey paradise" in the voice channel.

The typed wake word (``commands/wake.py``) works in a channel's text chat.
This package makes the same thing work when you simply **say it out loud**:

    you │ 🎙 "hey paradise, what's the capital of Rajasthan?"
        │      ↓ Opus → PCM (48 kHz) → endpointing → WAV (16 kHz)
        │      ↓ speech-to-text (ElevenLabs Scribe, or Gemini)
    bot │ 🗣 "Jaipur is the capital of Rajasthan."

Nothing is uploaded until something that sounds like speech has *ended*, and
each utterance is transcribed exactly once. A user who opted out of voice
capture never reaches this code at all — the receiver drops them first.

Pieces:

``segmenter``  frames → utterances (energy endpointing, pre-roll, trimming)
``phrases``    transcript → "was that for me, and what did they ask?"
``stt``        WAV → text (ElevenLabs Scribe → Gemini, in that order)
``listener``   the tap, the rate limits and the callback into the bot
"""

from .listener import ListenerStats, SpokenRequest, SpokenWakeListener
from .phrases import WakeMatch, find_wake, normalise
from .segmenter import Segment, SpeechSegmenter, frame_rms
from .stt import (
    ElevenLabsTranscriber,
    GeminiTranscriber,
    SpeechToTextChain,
    SpeechToTextUnavailable,
    to_wav,
    wav_from_bytes,
)

__all__ = [
    "ElevenLabsTranscriber",
    "GeminiTranscriber",
    "ListenerStats",
    "Segment",
    "SpeechSegmenter",
    "SpeechToTextChain",
    "SpeechToTextUnavailable",
    "SpokenRequest",
    "SpokenWakeListener",
    "WakeMatch",
    "find_wake",
    "frame_rms",
    "normalise",
    "to_wav",
    "wav_from_bytes",
]
