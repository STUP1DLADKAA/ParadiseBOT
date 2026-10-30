"""Spoken wake word: hear "hey paradise" in the voice channel.

The typed wake word (``commands/wake.py``) works in a channel's text chat.
This package makes the same thing work when you simply **say it out loud**:

    you │ 🎙 "hey paradise, what's the capital of Rajasthan?"
        │      ↓ Opus → PCM (48 kHz) → endpointing → WAV (16 kHz)
        │      ↓ speech-to-text (on-device whisper; cloud only if asked)
    bot │ 🗣 "Jaipur is the capital of Rajasthan."

Nothing is uploaded until something that sounds like speech has *ended*, and
each utterance is transcribed exactly once. Every human speaker in the channel
is heard: the receiver hands packets to the listener before the clip buffer has
any say, so recording and listening cannot break each other.

Pieces:

``segmenter``  frames → utterances (energy endpointing, pre-roll, trimming)
``phrases``    transcript → "was that for me, and what did they ask?"
``stt``        WAV → text (local whisper, then ElevenLabs Scribe, then Gemini)
``local_stt``  the on-device engine: faster-whisper on this CPU, no network
``listener``   the tap, the rate limits and the callback into the bot
``cue``        the short chime that says "I heard you" before the answer
"""

from .cue import build_cue_pcm
from .listener import ListenerStats, SpokenRequest, SpokenWakeListener
from .local_stt import (
    INSTALL_HINT,
    LocalWhisperTranscriber,
    download_model_files,
)
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
    "INSTALL_HINT",
    "LocalWhisperTranscriber",
    "download_model_files",
    "GeminiTranscriber",
    "ListenerStats",
    "Segment",
    "SpeechSegmenter",
    "SpeechToTextChain",
    "SpeechToTextUnavailable",
    "SpokenRequest",
    "SpokenWakeListener",
    "WakeMatch",
    "build_cue_pcm",
    "find_wake",
    "frame_rms",
    "normalise",
    "to_wav",
    "wav_from_bytes",
]
