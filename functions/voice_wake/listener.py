"""Listen to the voice channel, hear "hey paradise", hand over the question.

The pipeline, and why each hop is where it is:

```
UDP thread │ router tap ──► deque.append            (never blocks the socket)
           ▼
event loop │ drain ──► Opus decode ──► segmenter    (20 ms frames, 48 kHz)
           ▼
           │ silence detected ──► one WAV          (16 kHz mono, ~192 KB)
           ▼
           │ speech-to-text (ElevenLabs / Gemini)  (runs while you keep talking)
           ▼
           │ "hey paradise …" found? ──► callback  (the cog answers + speaks)
```

Two rules make this safe to run for hours:

* **Nothing expensive happens per frame.** The tap only appends to a bounded
  deque; decoding and endpointing run in one task on the event loop; a
  transcription happens once per *utterance*, and only after speech was
  actually detected.
* **Every limit is explicit.** Utterances are capped in length, speakers are
  capped in number, requests are rate-limited per user and per guild, and a
  user who opted out of voice capture (``/clip`` consent) never reaches the
  tap at all — the router drops them before this code sees a packet.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable

import numpy as np

from functions.voice_clip.constants import FRAME_SAMPLES
from functions.voice_clip.opus_codec import SpeakerOpusDecoder

from .phrases import WakeMatch, find_wake
from .segmenter import Segment, SpeechSegmenter
from .stt import SpeechToTextChain, to_wav

log = logging.getLogger(__name__)

#: Frames (20 ms each) that may wait for the loop to pick them up: 25 s.
MAX_QUEUED_FRAMES = 1250
#: Frames buffered per speaker before decoding backs up (2 s).
MAX_BACKLOG_PER_SPEAKER = 100


@dataclass(slots=True)
class SpokenRequest:
    """A recognised wake phrase and the question that followed it."""

    guild_id: int
    user_id: int
    question: str
    transcript: str
    match: WakeMatch
    followed_up: bool = False


@dataclass(slots=True)
class ListenerStats:
    frames: int = 0
    segments: int = 0
    transcripts: int = 0
    triggers: int = 0
    dropped: int = 0
    last_transcript: str = ""
    last_error: str = ""

    def as_dict(self) -> dict[str, int | str]:
        return {
            "frames": self.frames,
            "segments": self.segments,
            "transcripts": self.transcripts,
            "triggers": self.triggers,
            "dropped": self.dropped,
            "last_transcript": self.last_transcript[:200],
            "last_error": self.last_error[:200],
        }


class SpokenWakeListener:
    """Per-process listener for spoken wake words in every joined guild."""

    def __init__(
        self,
        *,
        stt: SpeechToTextChain,
        config,
        on_request: Callable[[SpokenRequest], Awaitable[None]],
        on_wake_only: Callable[[int, int], Awaitable[None]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.stt = stt
        self.config = config
        self.on_request = on_request
        self.on_wake_only = on_wake_only
        self._clock = clock
        self.segmenter = SpeechSegmenter(
            threshold=float(config.energy_threshold),
            onset_frames=max(1, int(round(config.onset_ms / 20))),
            end_silence_frames=max(1, int(round(config.silence_ms / 20))),
            min_voiced_frames=max(1, int(round(config.min_seconds * 50))),
            max_frames=max(50, int(round(config.max_seconds * 50))),
            max_speakers=int(config.max_speakers),
        )
        self._queue: deque[tuple[int, int, float, bytes, int]] = deque(
            maxlen=MAX_QUEUED_FRAMES
        )
        self._decoders: dict[tuple[int, int], SpeakerOpusDecoder] = {}
        self._epochs: dict[tuple[int, int], int] = {}
        self._user_last: dict[tuple[int, int], float] = {}
        self._followup_until: dict[tuple[int, int], float] = {}
        self._guild_calls: dict[int, deque[float]] = {}
        self._guild_locks: dict[int, asyncio.Lock] = {}
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._closing = False
        self.stats = ListenerStats()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self.stt.available

    def describe(self) -> str:
        return f"{self.stt.describe()} · threshold {self.config.energy_threshold:.3f}"

    async def start(self) -> None:
        if self._task is not None:
            return
        self._closing = False
        self._task = asyncio.create_task(self._run(), name="paradise-spoken-wake")
        log.info("Spoken wake word listening — %s", self.describe())

    async def stop(self) -> None:
        self._closing = True
        self._wake.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self.stt.close()
        self._queue.clear()
        self._decoders.clear()

    # ------------------------------------------------------------------
    # The tap (called from the voice UDP thread — must be cheap)
    # ------------------------------------------------------------------

    def tap(self, guild_id: int, user_id: int, packet) -> None:
        """Receive one buffered Opus packet from the voice receiver."""
        if self._closing:
            return
        try:
            # A silent packet carries no speech but *is* a timing marker: the
            # segmenter needs it to notice that a sentence ended.
            self._queue.append(
                (
                    int(guild_id),
                    int(user_id),
                    float(getattr(packet, "mono_time", 0.0)),
                    packet.opus_data,
                    int(getattr(packet, "stream_epoch", 0)),
                )
            )
            self._wake.set()
        except Exception:  # noqa: BLE001 - never break the audio reader
            log.debug("spoken-wake tap failed", exc_info=True)

    def forget(self, guild_id: int, user_id: int) -> None:
        """Drop a speaker's state (they left or switched channel)."""
        self.segmenter.forget(int(user_id))
        for key in [k for k in self._decoders if k == (int(guild_id), int(user_id))]:
            self._decoders.pop(key, None)

    def reset_guild(self, guild_id: int) -> None:
        gid = int(guild_id)
        self.segmenter.reset()
        self._guild_calls.pop(gid, None)
        self._followup_until = {
            key: value for key, value in self._followup_until.items() if key[0] != gid
        }

    # ------------------------------------------------------------------
    # Event loop side
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        try:
            while not self._closing:
                if not self._queue:
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=0.5)
                    except asyncio.TimeoutError:
                        continue
                await self._drain()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the listener must never kill the bot
            log.exception("Spoken wake listener stopped unexpectedly")
            self.stats.last_error = "listener crashed"

    async def _drain(self) -> None:
        """Decode and endpoint everything queued, without starving the loop."""
        budget = 400  # frames per pass (~8 s of audio), then yield
        while self._queue and budget > 0:
            guild_id, user_id, _arrival, opus, epoch = self._queue.popleft()
            budget -= 1
            self.stats.frames += 1
            frame = self._decode(guild_id, user_id, opus, epoch)
            if frame is None:
                continue
            segment = self.segmenter.feed(user_id, frame, now=self._clock())
            if segment is not None:
                self.stats.segments += 1
                await self._handle_segment(guild_id, segment)
        if budget <= 0:
            await asyncio.sleep(0)

    def _decode(self, guild_id: int, user_id: int, opus: bytes, epoch: int) -> np.ndarray | None:
        key = (guild_id, user_id)
        decoder = self._decoders.get(key)
        if decoder is None:
            if len(self._decoders) > 64:
                self._decoders.clear()
            decoder = SpeakerOpusDecoder()
            self._decoders[key] = decoder
        previous = self._epochs.get(key)
        if previous is not None and epoch != previous:
            decoder.reset()
        self._epochs[key] = epoch
        samples = decoder.decode_packet(opus)
        if samples is None:
            self.stats.dropped += 1
            return None
        # Decoders hand back 48 kHz mono float32; the segmenter wants int16.
        return np.clip(samples * 32767.0, -32768, 32767).astype(np.int16)

    async def _handle_segment(self, guild_id: int, segment: Segment) -> None:
        user_key = (int(guild_id), int(segment.user_id))
        follow_up = self._followup_until.get(user_key, 0.0) > self._clock()
        self._followup_until.pop(user_key, None)

        wav = to_wav(segment.samples)
        if not wav:
            return
        transcript = ""
        try:
            transcript = await self.stt.transcribe(wav, language=self.config.language)
        except Exception as error:  # noqa: BLE001
            self.stats.last_error = f"{error.__class__.__name__}: {error}"
            log.debug("Transcription failed", exc_info=True)
        if not transcript:
            if self.stats.last_error:
                log.debug("No transcript for a %.1fs utterance", segment.duration)
            return
        self.stats.transcripts += 1
        self.stats.last_transcript = transcript

        match = find_wake(transcript)
        if not match.matched:
            if not follow_up:
                log.debug("Heard (no wake word): %r", transcript[:120])
                return
            # A bare "hey paradise" was just said: treat this as the question.
            match = WakeMatch(matched=True, question=transcript.strip(), phrase="(follow-up)")
        elif match.bare and not follow_up:
            if self.on_wake_only is not None:
                self._followup_until[user_key] = self._clock() + float(
                    self.config.follow_up_seconds
                )
                try:
                    await self.on_wake_only(int(guild_id), int(segment.user_id))
                except Exception:  # noqa: BLE001
                    log.debug("wake-only handler failed", exc_info=True)
            return

        if not match.question:
            return
        if not self._allow(int(guild_id), int(segment.user_id)):
            log.info("Spoken request ignored (rate limited): %r", match.question[:80])
            self.stats.dropped += 1
            return

        self.stats.triggers += 1
        log.info(
            "Spoken request from %s in guild %s: %r",
            segment.user_id,
            guild_id,
            match.question[:120],
        )
        try:
            await self.on_request(
                SpokenRequest(
                    guild_id=int(guild_id),
                    user_id=int(segment.user_id),
                    question=match.question,
                    transcript=transcript,
                    match=match,
                    followed_up=follow_up,
                )
            )
        except Exception:  # noqa: BLE001 - never lose the listener
            log.exception("Spoken request handler failed")
            self.stats.last_error = "handler failed"

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    def _allow(self, guild_id: int, user_id: int) -> bool:
        now = self._clock()
        user_key = (guild_id, user_id)
        if now < self._user_last.get(user_key, 0.0) + float(self.config.cooldown):
            return False
        calls = self._guild_calls.setdefault(guild_id, deque(maxlen=64))
        while calls and now - calls[0] > 60.0:
            calls.popleft()
        if len(calls) >= int(self.config.max_per_minute):
            return False
        self._user_last[user_key] = now
        calls.append(now)
        if len(self._user_last) > 512:
            cutoff = now - 300
            self._user_last = {
                key: value for key, value in self._user_last.items() if value > cutoff
            }
        return True

    @property
    def frame_samples(self) -> int:
        return FRAME_SAMPLES


__all__ = [
    "ListenerStats",
    "MAX_QUEUED_FRAMES",
    "SpokenRequest",
    "SpokenWakeListener",
]
