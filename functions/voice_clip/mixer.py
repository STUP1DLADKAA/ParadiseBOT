"""Time-aligned multi-speaker Opus decoding, mixing and anti-clipping limiter."""

from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np

from .buffer import BufferedAudioPacket, ClipIntervalSnapshot, SpeakerIntervalPackets
from .constants import FRAME_SAMPLES, NoUsableAudioError, SAMPLE_RATE
from .opus_codec import SpeakerOpusDecoder

# Maximum consecutive lost 20ms frames concealed via Opus FEC/PLC (100 ms);
# longer gaps remain clean silence so concealed tails never drone unnaturally.
MAX_CONCEALED_LOSS_FRAMES: int = 5
# Talkspurt break threshold when RTP vs monotonic arrival diverges (> 200 ms)
# or when sequence/RTP jumps exceed 0.5 s.
# A pause longer than this in the arrival clock means the speaker actually
# stopped talking (rather than the network being 20 ms late), so the timeline
# re-anchors on the clock instead of running on.
TALKSPURT_PAUSE_SECONDS: float = 0.60
MAX_CONTIGUOUS_FRAME_GAP: int = 25
# Anti-clipping ceiling (-0.72 dBFS) and smooth envelope parameters.
LIMITER_CEILING: float = 0.92
# Speech is levelled to this RMS with one *static* gain. A dynamic normaliser
# (loudnorm in single-pass mode) rides the gain up and down between sentences,
# which is audible as a wobble on a short clip — the "jitter" people hear.
TARGET_RMS_DBFS: float = -20.0
LIMITER_BLOCK_SAMPLES: int = 480  # 10 ms @ 48 kHz
SILENCE_PEAK_THRESHOLD: float = 1e-4


@dataclass(frozen=True, slots=True)
class MixResult:
    """Mixed 48 kHz mono float32 audio timeline and diagnostic counters."""

    samples: np.ndarray
    sample_rate: int
    duration_seconds: float
    speaker_count: int
    peak_before_limiter: float
    peak_after_limiter: float
    concealed_frames: int
    decode_errors: int


def _order_and_deduplicate(
    packets: tuple[BufferedAudioPacket, ...],
) -> list[BufferedAudioPacket]:
    """Order RTP packets within continuous bursts and drop duplicate sequences."""
    if len(packets) <= 1:
        return list(packets)

    ordered: list[BufferedAudioPacket] = []
    idx = 0
    n = len(packets)
    while idx < n:
        first = packets[idx]
        burst: list[tuple[int, BufferedAudioPacket]] = [(0, first)]
        unwrapped_seq = 0
        prev_seq = first.sequence
        prev_mono = first.mono_time
        idx += 1

        while idx < n:
            pkt = packets[idx]
            if (
                pkt.stream_epoch != first.stream_epoch
                or pkt.ssrc != first.ssrc
                or (pkt.mono_time - prev_mono) > 0.50
            ):
                break
            delta = ((pkt.sequence - prev_seq + 32768) % 65536) - 32768
            if abs(delta) > 64:
                break
            unwrapped_seq += delta
            prev_seq = pkt.sequence
            prev_mono = pkt.mono_time
            burst.append((unwrapped_seq, pkt))
            idx += 1

        burst.sort(key=lambda item: item[0])
        seen_seq: set[int] = set()
        for u_seq, pkt in burst:
            if u_seq in seen_seq:
                continue
            seen_seq.add(u_seq)
            ordered.append(pkt)

    return ordered


def _add_frame(mix: np.ndarray, offset: int, frame: np.ndarray) -> int:
    """Add ``frame`` into ``mix`` starting at ``offset``, clipping bounds safely."""
    if frame.size == 0:
        return offset
    total = mix.size
    if offset >= total:
        return offset
    start = offset
    src_start = 0
    if start < 0:
        src_start = -start
        start = 0
    if src_start >= frame.size:
        return offset + frame.size
    avail = min(frame.size - src_start, total - start)
    if avail > 0:
        mix[start : start + avail] += frame[src_start : src_start + avail]
    return offset + frame.size


def _frame_step_samples(ordered: list[BufferedAudioPacket], index: int) -> int:
    """How many samples one packet of this stream covers.

    Discord normally sends 20 ms frames, but some clients send 60 ms ones. The
    sender's own RTP timestamps say which: look at the next packet of the same
    stream when it is the next sequence number, then at the previous one, and
    fall back to :data:`FRAME_SAMPLES` when neither is usable.
    """
    packet = ordered[index]
    candidates: list[int] = []
    if index + 1 < len(ordered):
        nxt = ordered[index + 1]
        if nxt.stream_epoch == packet.stream_epoch and nxt.ssrc == packet.ssrc:
            delta = ((nxt.sequence - packet.sequence + 32768) % 65536) - 32768
            if delta == 1:
                candidates.append((nxt.rtp_timestamp - packet.rtp_timestamp) & 0xFFFFFFFF)
    if index > 0:
        prev = ordered[index - 1]
        if prev.stream_epoch == packet.stream_epoch and prev.ssrc == packet.ssrc:
            delta = ((packet.sequence - prev.sequence + 32768) % 65536) - 32768
            if delta == 1:
                candidates.append((packet.rtp_timestamp - prev.rtp_timestamp) & 0xFFFFFFFF)
    for candidate in candidates:
        # 2.5 ms … 120 ms: anything else is a clock jump, not a frame size.
        if 120 <= candidate <= 5760:
            return candidate
    return FRAME_SAMPLES


def plan_speaker_timeline(
    packets: tuple[BufferedAudioPacket, ...],
    window_start_mono: float,
) -> list[tuple[int, int, BufferedAudioPacket]]:
    """Lay one speaker's packets out on a continuous sample timeline.

    Returns ``(start_sample, missing_frames, packet)`` per packet, where
    ``missing_frames`` packets were lost between it and the previous one and
    must be concealed.

    The rule that removes the stutter: inside a talkspurt every frame is placed
    exactly where the previous one ended. Placing each frame at its *arrival*
    time instead — which is what this used to do — lets a 5 ms network wobble,
    or a burst of three packets arriving together, overlap one frame with the
    next and cut a hole before the one after it. That is audible as a flutter,
    even though not a single packet was lost. RTP gives us the real answer:
    consecutive sequence numbers are consecutive frames of a known length.

    A genuine pause (a sequence reset, a long gap, or more than
    :data:`TALKSPURT_PAUSE_SECONDS` between arrivals) re-anchors on the clock,
    but never backwards.
    """
    ordered = _order_and_deduplicate(packets)
    plan: list[tuple[int, int, BufferedAudioPacket]] = []
    cursor: int | None = None
    previous: BufferedAudioPacket | None = None

    for index, packet in enumerate(ordered):
        step = _frame_step_samples(ordered, index)
        arrival_sample = max(
            0, int(round((packet.mono_time - window_start_mono) * SAMPLE_RATE))
        )
        new_stream = (
            previous is None
            or packet.stream_epoch != previous.stream_epoch
            or packet.ssrc != previous.ssrc
        )
        if new_stream:
            start = arrival_sample
            missing = 0
            cursor = start
        else:
            assert previous is not None and cursor is not None
            seq_delta = ((packet.sequence - previous.sequence + 32768) % 65536) - 32768
            rtp_delta = (packet.rtp_timestamp - previous.rtp_timestamp) & 0xFFFFFFFF
            pause = packet.mono_time - previous.mono_time
            if (
                seq_delta <= 0
                or seq_delta > MAX_CONTIGUOUS_FRAME_GAP
                or pause > TALKSPURT_PAUSE_SECONDS
            ):
                start = max(cursor, arrival_sample)
                missing = 0
                cursor = start
            else:
                # Frames the sender emitted but the network ate.
                missing = max(0, min(seq_delta - 1, int(rtp_delta // step) - 1))
                missing = min(missing, MAX_CONCEALED_LOSS_FRAMES)
                start = cursor
        plan.append((start, missing, packet))
        cursor = start + (missing + 1) * step
        previous = packet

    return plan


def _render_speaker_into_mix(
    mix: np.ndarray,
    speaker: SpeakerIntervalPackets,
    window_start_mono: float,
) -> tuple[int, int]:
    """Decode and align one speaker's packets onto the shared ``mix`` buffer."""
    decoder = SpeakerOpusDecoder()
    concealed = 0

    # Warm decoder state with immediately preceding frames; discard PCM output.
    last_warmup_epoch: int | None = None
    last_warmup_ssrc: int | None = None
    for wpkt in speaker.warmup_packets:
        if (
            last_warmup_epoch is not None
            and (wpkt.stream_epoch != last_warmup_epoch or wpkt.ssrc != last_warmup_ssrc)
        ):
            decoder.reset()
        decoder.decode_packet(wpkt.opus_data)
        last_warmup_epoch = wpkt.stream_epoch
        last_warmup_ssrc = wpkt.ssrc

    if not speaker.packets:
        return 0, decoder.decode_errors

    previous_epoch: int | None = None
    previous_ssrc: int | None = None
    written_until: int | None = None
    for start_sample, missing, packet in plan_speaker_timeline(
        speaker.packets, window_start_mono
    ):
        # A new stream (they reconnected, Discord moved their SSRC) needs a
        # fresh codec: reusing the old one decodes the first frame as garbage.
        if (
            previous_epoch is not None
            and (packet.stream_epoch != previous_epoch or packet.ssrc != previous_ssrc)
        ):
            decoder.reset()
        previous_epoch = packet.stream_epoch
        previous_ssrc = packet.ssrc

        # Never write over audio that is already there. The planned position is
        # nominal (it assumes the sender's frame size), while a decoded frame
        # is the ground truth; when they disagree the frame that exists wins.
        cursor = start_sample if written_until is None else max(written_until, start_sample)
        if missing and not packet.is_silence:
            for lost in range(missing):
                # The frame right before a received packet can be rebuilt from
                # its in-band FEC; the rest are extrapolated.
                frame = (
                    decoder.decode_fec(packet.opus_data)
                    if lost == missing - 1
                    else decoder.decode_plc()
                )
                cursor = _add_frame(mix, cursor, frame)
                concealed += 1

        decoded = decoder.decode_packet(packet.opus_data)
        if decoded is None:
            # Malformed Opus: substitute PLC of the same length so timing never
            # shifts and the next packet still lands on the grid.
            decoded = decoder.decode_plc()
            concealed += 1
        written_until = _add_frame(mix, cursor, decoded)

    return concealed, decoder.decode_errors


def apply_static_gain(
    samples: np.ndarray,
    *,
    target_rms_dbfs: float = TARGET_RMS_DBFS,
    peak_ceiling: float = LIMITER_CEILING,
) -> float:
    """Level the whole clip with one constant gain; return the gain applied.

    Constant, not adaptive: every part of the clip is scaled by the same
    factor, so a pause stays as quiet relative to speech as it really was and
    the level never breathes. The gain is capped so the loudest sample lands
    under ``peak_ceiling`` — no pre-limiter clipping, no pumping.
    """
    if samples.size == 0:
        return 1.0
    rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float32))))
    peak = float(np.max(np.abs(samples)))
    if rms <= 1e-7 or peak <= 1e-7:
        return 1.0
    gain = float(10.0 ** (target_rms_dbfs / 20.0) / rms)
    if gain * peak > peak_ceiling:
        gain = peak_ceiling / peak
    samples *= np.float32(gain)
    return gain


def apply_smooth_peak_limiter(
    samples: np.ndarray,
    *,
    ceiling: float = LIMITER_CEILING,
) -> tuple[float, float]:
    """Prevent multi-speaker clipping via a 10ms-lookahead smooth envelope limiter.

    Leaves audio completely untouched (unity gain 1.0) whenever peaks are already
    at or below ``ceiling``.
    """
    if samples.size == 0:
        return 0.0, 0.0

    peak_before = float(np.max(np.abs(samples)))
    if peak_before <= ceiling or peak_before <= 0.0:
        return peak_before, peak_before

    block = LIMITER_BLOCK_SAMPLES
    n = samples.size
    n_blocks = (n + block - 1) // block
    padded_len = n_blocks * block

    if padded_len != n:
        abs_padded = np.zeros(padded_len, dtype=np.float32)
        abs_padded[:n] = np.abs(samples)
    else:
        abs_padded = np.abs(samples)

    block_peaks = np.max(abs_padded.reshape(n_blocks, block), axis=1)
    safe_peaks = np.maximum(block_peaks, np.float32(1e-9))
    raw_gains = np.minimum(np.float32(1.0), np.float32(ceiling) / safe_peaks)

    # 1-block (10 ms) lookahead so sudden multi-speaker transients never clip.
    lookahead_gains = raw_gains.copy()
    if n_blocks > 1:
        lookahead_gains[:-1] = np.minimum(raw_gains[:-1], raw_gains[1:])

    # Instantaneous attack, smooth 80 ms exponential release across 10 ms blocks.
    release_coeff = math.exp(-0.010 / 0.080)
    env_gains = np.empty(n_blocks + 1, dtype=np.float32)
    g = float(lookahead_gains[0])
    env_gains[0] = g
    for i in range(n_blocks):
        target = float(lookahead_gains[i])
        if target < g:
            g = target
        else:
            g = target + release_coeff * (g - target)
        env_gains[i + 1] = g

    # Linearly interpolate block boundary gains across every sample (click-free).
    sample_idx = np.arange(n, dtype=np.float32) / np.float32(block)
    b_idx = np.minimum(sample_idx.astype(np.int32), n_blocks - 1)
    frac = sample_idx - b_idx.astype(np.float32)
    gain_curve = env_gains[b_idx] * (np.float32(1.0) - frac) + env_gains[b_idx + 1] * frac

    samples *= gain_curve
    np.clip(samples, -ceiling, ceiling, out=samples)
    peak_after = float(np.max(np.abs(samples)))
    return peak_before, peak_after


def mix_snapshot_to_pcm_f32(snapshot: ClipIntervalSnapshot) -> MixResult:
    """Decode, time-align, mix and limit all speakers in ``snapshot``."""
    total_samples = max(
        FRAME_SAMPLES,
        int(round(snapshot.captured_seconds * SAMPLE_RATE)),
    )
    mix = np.zeros(total_samples, dtype=np.float32)

    total_concealed = 0
    total_decode_errors = 0
    active_speakers = 0

    for speaker in snapshot.speakers:
        if not speaker.packets:
            continue
        active_speakers += 1
        concealed, dec_err = _render_speaker_into_mix(
            mix,
            speaker,
            snapshot.window_start_mono,
        )
        total_concealed += concealed
        total_decode_errors += dec_err

    apply_static_gain(mix)
    peak_before, peak_after = apply_smooth_peak_limiter(mix)
    if peak_after < SILENCE_PEAK_THRESHOLD:
        raise NoUsableAudioError(
            f"No audible speech was captured in the last {snapshot.requested_seconds}s."
        )

    return MixResult(
        samples=mix,
        sample_rate=SAMPLE_RATE,
        duration_seconds=total_samples / SAMPLE_RATE,
        speaker_count=active_speakers,
        peak_before_limiter=peak_before,
        peak_after_limiter=peak_after,
        concealed_frames=total_concealed,
        decode_errors=total_decode_errors,
    )


__all__ = [
    "MAX_CONCEALED_LOSS_FRAMES",
    "MixResult",
    "apply_smooth_peak_limiter",
    "apply_static_gain",
    "mix_snapshot_to_pcm_f32",
    "plan_speaker_timeline",
]
