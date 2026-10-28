"""Bounded in-memory rolling Opus packet buffer with monotonic timing."""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from .constants import (
    FRAME_DURATION_SECONDS,
    MAX_CLIP_DURATION,
    MAX_OPUS_PACKET_BYTES,
    OPUS_SILENCE,
    PACKET_OVERHEAD_BYTES,
    NoUsableAudioError,
    validate_clip_duration,
)

# Up to 2 packets (40 ms) immediately preceding window_start are included as
# pre-roll solely to warm per-speaker Opus decoder state; their samples are
# discarded before sample 0 of the exported clip.
DECODER_WARMUP_SECONDS: float = 0.04


@dataclass(frozen=True, slots=True)
class BufferedAudioPacket:
    """One timestamped, compressed Opus frame from a single speaker."""

    mono_time: float
    rtp_timestamp: int
    sequence: int
    ssrc: int
    stream_epoch: int
    opus_data: bytes
    is_silence: bool

    @property
    def memory_bytes(self) -> int:
        return len(self.opus_data) + PACKET_OVERHEAD_BYTES


@dataclass(frozen=True, slots=True)
class SpeakerIntervalPackets:
    """Immutable per-speaker slice captured for an export job."""

    user_id: int
    warmup_packets: tuple[BufferedAudioPacket, ...]
    packets: tuple[BufferedAudioPacket, ...]


@dataclass(frozen=True, slots=True)
class ClipIntervalSnapshot:
    """Consistent point-in-time snapshot of a requested rolling interval."""

    guild_id: int
    channel_id: int
    requested_seconds: int
    captured_seconds: float
    window_start_mono: float
    window_end_mono: float
    speakers: tuple[SpeakerIntervalPackets, ...]
    total_packets: int
    non_silent_packets: int


class _SpeakerStream:
    __slots__ = ("user_id", "packets", "byte_size", "last_mono", "stream_epoch", "last_ssrc")

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        self.packets: deque[BufferedAudioPacket] = deque()
        self.byte_size: int = 0
        self.last_mono: float = 0.0
        self.stream_epoch: int = 0
        self.last_ssrc: int | None = None


class RollingAudioBuffer:
    """Bounded in-memory rolling voice buffer for one guild voice connection.

    Retains at most ``max_seconds`` (capped at 180s) of compressed per-speaker
    Opus packets indexed on a monotonic clock, bounded simultaneously by time,
    active speaker count, and total byte budget.
    """

    def __init__(
        self,
        guild_id: int,
        *,
        max_seconds: float = float(MAX_CLIP_DURATION),
        max_active_speakers: int = 32,
        max_buffer_bytes: int = 24 * 1024 * 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.guild_id: int = int(guild_id)
        self.max_seconds: float = min(float(MAX_CLIP_DURATION), max(1.0, float(max_seconds)))
        self.max_active_speakers: int = max(1, int(max_active_speakers))
        self.max_buffer_bytes: int = max(65_536, int(max_buffer_bytes))
        self._clock: Callable[[], float] = clock

        self._lock = threading.Lock()
        self._channel_id: int | None = None
        self._recording_active: bool = False
        self._started_mono: float | None = None
        self._speakers: dict[int, _SpeakerStream] = {}
        self._total_bytes: int = 0
        self._total_packets: int = 0
        self._received_any_audio: bool = False
        self._evicted_packets: int = 0
        self._dropped_budget_packets: int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def channel_id(self) -> int | None:
        with self._lock:
            return self._channel_id

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._recording_active and self._channel_id is not None

    def start_session(self, channel_id: int) -> None:
        """Start (or switch to) recording ``channel_id``; clears prior channel data."""
        now = self._clock()
        cid = int(channel_id)
        with self._lock:
            if self._recording_active and self._channel_id == cid:
                return
            self._clear_unlocked()
            self._channel_id = cid
            self._recording_active = True
            self._started_mono = now

    def stop_session(self) -> None:
        """Stop recording and immediately purge all buffered audio in memory."""
        with self._lock:
            self._clear_unlocked()

    def clear(self) -> None:
        """Purge all buffered audio and reset state."""
        with self._lock:
            self._clear_unlocked()

    def _clear_unlocked(self) -> None:
        for stream in self._speakers.values():
            stream.packets.clear()
        self._speakers.clear()
        self._channel_id = None
        self._recording_active = False
        self._started_mono = None
        self._total_bytes = 0
        self._total_packets = 0
        self._received_any_audio = False

    # ------------------------------------------------------------------
    # Capture & Eviction
    # ------------------------------------------------------------------

    def append_packet(
        self,
        *,
        user_id: int,
        opus_data: bytes,
        rtp_timestamp: int,
        sequence: int,
        ssrc: int = 0,
        mono_time: float | None = None,
        channel_id: int | None = None,
    ) -> bool:
        """Append one Opus packet for ``user_id`` if recording is active."""
        if not opus_data or len(opus_data) > MAX_OPUS_PACKET_BYTES:
            return False
        if not isinstance(user_id, int) or user_id <= 0:
            return False

        raw_now = self._clock()
        pkt_mono = raw_now if mono_time is None else float(mono_time)
        now = max(raw_now, pkt_mono)
        is_silence = opus_data == OPUS_SILENCE
        pkt_bytes = len(opus_data) + PACKET_OVERHEAD_BYTES
        if pkt_bytes > self.max_buffer_bytes:
            return False

        with self._lock:
            if not self._recording_active or self._channel_id is None:
                return False
            if channel_id is not None and int(channel_id) != self._channel_id:
                return False

            # 1. Evict all expired packets before admitting new data.
            cutoff = now - self.max_seconds
            self._evict_expired_unlocked(cutoff)

            # Drop stale packet that is already older than the rolling window.
            if pkt_mono < cutoff:
                return False

            if self._started_mono is not None and pkt_mono < self._started_mono:
                pkt_mono = self._started_mono

            stream = self._speakers.get(user_id)
            if stream is None:
                if len(self._speakers) >= self.max_active_speakers:
                    self._evict_oldest_speaker_unlocked()
                stream = _SpeakerStream(user_id)
                self._speakers[user_id] = stream

            if stream.last_ssrc is not None and stream.last_ssrc != ssrc:
                stream.stream_epoch += 1
            stream.last_ssrc = ssrc

            # Enforce strict monotonic non-decreasing packet order within stream.
            if stream.packets and pkt_mono < stream.packets[-1].mono_time:
                pkt_mono = stream.packets[-1].mono_time

            # 2. Enforce total byte budget by dropping globally oldest packets.
            while self._total_bytes + pkt_bytes > self.max_buffer_bytes and self._total_packets > 0:
                if not self._pop_globally_oldest_packet_unlocked():
                    break
                self._dropped_budget_packets += 1

            if self._total_bytes + pkt_bytes > self.max_buffer_bytes:
                return False

            packet = BufferedAudioPacket(
                mono_time=pkt_mono,
                rtp_timestamp=rtp_timestamp & 0xFFFFFFFF,
                sequence=sequence & 0xFFFF,
                ssrc=ssrc & 0xFFFFFFFF,
                stream_epoch=stream.stream_epoch,
                opus_data=bytes(opus_data),
                is_silence=is_silence,
            )
            stream.packets.append(packet)
            stream.byte_size += pkt_bytes
            stream.last_mono = pkt_mono
            self._total_bytes += pkt_bytes
            self._total_packets += 1
            if not is_silence:
                self._received_any_audio = True
            return True

    def mark_speaker_discontinuity(self, user_id: int) -> None:
        """Increment stream epoch when a speaker leaves/reconnects mid-buffer."""
        with self._lock:
            stream = self._speakers.get(user_id)
            if stream is not None:
                stream.stream_epoch += 1
                stream.last_ssrc = None

    def purge_speaker(self, user_id: int) -> int:
        """Immediately remove all buffered packets for ``user_id`` (e.g. opt-out)."""
        with self._lock:
            stream = self._speakers.pop(user_id, None)
            if stream is None:
                return 0
            removed = len(stream.packets)
            self._total_bytes = max(0, self._total_bytes - stream.byte_size)
            self._total_packets = max(0, self._total_packets - removed)
            stream.packets.clear()
            return removed

    def evict_expired(self) -> int:
        """Explicitly run expiry sweep against the current monotonic clock."""
        now = self._clock()
        with self._lock:
            return self._evict_expired_unlocked(now - self.max_seconds)

    def _evict_expired_unlocked(self, cutoff: float) -> int:
        evicted = 0
        empty_users: list[int] = []
        for uid, stream in self._speakers.items():
            q = stream.packets
            while q and q[0].mono_time < cutoff:
                old = q.popleft()
                cost = old.memory_bytes
                stream.byte_size = max(0, stream.byte_size - cost)
                self._total_bytes = max(0, self._total_bytes - cost)
                self._total_packets = max(0, self._total_packets - 1)
                evicted += 1
            if not q:
                empty_users.append(uid)
        for uid in empty_users:
            self._speakers.pop(uid, None)
        self._evicted_packets += evicted
        return evicted

    def _evict_oldest_speaker_unlocked(self) -> None:
        if not self._speakers:
            return
        oldest_uid = min(
            self._speakers,
            key=lambda uid: self._speakers[uid].last_mono,
        )
        stream = self._speakers.pop(oldest_uid)
        self._total_bytes = max(0, self._total_bytes - stream.byte_size)
        self._total_packets = max(0, self._total_packets - len(stream.packets))
        stream.packets.clear()

    def _pop_globally_oldest_packet_unlocked(self) -> bool:
        oldest_uid: int | None = None
        oldest_time: float = float("inf")
        for uid, stream in self._speakers.items():
            if stream.packets and stream.packets[0].mono_time < oldest_time:
                oldest_time = stream.packets[0].mono_time
                oldest_uid = uid
        if oldest_uid is None:
            return False
        stream = self._speakers[oldest_uid]
        pkt = stream.packets.popleft()
        cost = pkt.memory_bytes
        stream.byte_size = max(0, stream.byte_size - cost)
        self._total_bytes = max(0, self._total_bytes - cost)
        self._total_packets = max(0, self._total_packets - 1)
        if not stream.packets:
            self._speakers.pop(oldest_uid, None)
        return True

    # ------------------------------------------------------------------
    # Fast Point-in-Time Snapshot
    # ------------------------------------------------------------------

    def snapshot(self, requested_seconds: int) -> ClipIntervalSnapshot:
        """Capture an immutable slice of the requested interval and release lock fast."""
        req = validate_clip_duration(requested_seconds)
        now = self._clock()

        with self._lock:
            if not self._recording_active or self._channel_id is None or self._started_mono is None:
                raise NoUsableAudioError("Voice recording is not active in any channel right now.")

            cutoff = now - self.max_seconds
            self._evict_expired_unlocked(cutoff)

            elapsed_recording = max(0.0, now - self._started_mono)
            available_window = min(self.max_seconds, elapsed_recording)

            if not self._received_any_audio or self._total_packets == 0:
                if elapsed_recording < 2.0:
                    raise NoUsableAudioError(
                        "Recording just started and no voice audio has been captured yet."
                    )
                raise NoUsableAudioError(
                    "No incoming voice audio has been received in this channel yet."
                )

            # Determine captured duration: bounded by requested_seconds, max_seconds,
            # and actual elapsed recording time when session is shorter than requested.
            captured_seconds = min(float(req), available_window)
            # Ensure at least one Opus frame (20ms) if packets arrived at session start.
            if captured_seconds < FRAME_DURATION_SECONDS:
                captured_seconds = FRAME_DURATION_SECONDS

            window_end = now
            window_start = max(cutoff, self._started_mono, window_end - captured_seconds)
            captured_seconds = max(FRAME_DURATION_SECONDS, window_end - window_start)
            warmup_start = max(cutoff, window_start - DECODER_WARMUP_SECONDS)

            speaker_slices: list[SpeakerIntervalPackets] = []
            total_pkts = 0
            non_silent_pkts = 0

            for uid, stream in self._speakers.items():
                if not stream.packets:
                    continue
                if stream.packets[-1].mono_time < window_start:
                    continue

                warmup: list[BufferedAudioPacket] = []
                in_window: list[BufferedAudioPacket] = []
                for pkt in stream.packets:
                    t = pkt.mono_time
                    if t < warmup_start:
                        continue
                    if t < window_start:
                        warmup.append(pkt)
                    elif t <= window_end:
                        in_window.append(pkt)
                        total_pkts += 1
                        if not pkt.is_silence:
                            non_silent_pkts += 1

                if in_window:
                    speaker_slices.append(
                        SpeakerIntervalPackets(
                            user_id=uid,
                            warmup_packets=tuple(warmup[-2:]),
                            packets=tuple(in_window),
                        )
                    )

            channel_id = self._channel_id

        if non_silent_pkts == 0:
            raise NoUsableAudioError(
                f"No audible speech was captured in the last {req}s (only silence)."
            )

        return ClipIntervalSnapshot(
            guild_id=self.guild_id,
            channel_id=channel_id,
            requested_seconds=req,
            captured_seconds=captured_seconds,
            window_start_mono=window_start,
            window_end_mono=window_end,
            speakers=tuple(speaker_slices),
            total_packets=total_pkts,
            non_silent_packets=non_silent_pkts,
        )

    # ------------------------------------------------------------------
    # Diagnostics / Metrics (privacy-safe: counts & sizes only, never payloads)
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, int | float | bool | None]:
        now = self._clock()
        with self._lock:
            self._evict_expired_unlocked(now - self.max_seconds)
            oldest_age = 0.0
            for stream in self._speakers.values():
                if stream.packets:
                    age = max(0.0, now - stream.packets[0].mono_time)
                    if age > oldest_age:
                        oldest_age = age
            elapsed = (
                max(0.0, now - self._started_mono)
                if self._started_mono is not None and self._recording_active
                else 0.0
            )
            return {
                "recording": self._recording_active,
                "channel_id": self._channel_id,
                "active_speakers": len(self._speakers),
                "buffered_packets": self._total_packets,
                "buffered_bytes": self._total_bytes,
                "oldest_packet_age_seconds": oldest_age,
                "session_elapsed_seconds": elapsed,
                "evicted_packets": self._evicted_packets,
                "dropped_budget_packets": self._dropped_budget_packets,
            }
