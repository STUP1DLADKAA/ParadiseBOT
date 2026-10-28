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
TALKSPURT_DRIFT_SECONDS: float = 0.20
MAX_CONTIGUOUS_FRAME_GAP: int = 25
# Anti-clipping ceiling (-0.72 dBFS) and smooth envelope parameters.
LIMITER_CEILING: float = 0.92
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

    packets = _order_and_deduplicate(speaker.packets)
    if not packets:
        return 0, decoder.decode_errors

    in_talkspurt = False
    anchor_sample = 0
    anchor_rtp = 0
    prev_seq = 0
    prev_rtp = 0
    prev_epoch = -1
    prev_ssrc = -1
    last_end_sample = 0

    for pkt in packets:
        mono_offset_sec = pkt.mono_time - window_start_mono
        mono_sample = int(round(mono_offset_sec * SAMPLE_RATE))

        start_new_spurt = not in_talkspurt
        seq_delta = 0
        rtp_delta = 0

        if in_talkspurt:
            if pkt.stream_epoch != prev_epoch or pkt.ssrc != prev_ssrc:
                decoder.reset()
                start_new_spurt = True
            else:
                seq_delta = ((pkt.sequence - prev_seq + 32768) % 65536) - 32768
                rtp_delta = (pkt.rtp_timestamp - prev_rtp) & 0xFFFFFFFF
                if (
                    seq_delta <= 0
                    or seq_delta > MAX_CONTIGUOUS_FRAME_GAP
                    or rtp_delta == 0
                    or rtp_delta > MAX_CONTIGUOUS_FRAME_GAP * FRAME_SAMPLES
                ):
                    start_new_spurt = True
                else:
                    projected_sample = anchor_sample + (
                        (pkt.rtp_timestamp - anchor_rtp) & 0xFFFFFFFF
                    )
                    projected_sec = projected_sample / SAMPLE_RATE
                    if abs(projected_sec - mono_offset_sec) > TALKSPURT_DRIFT_SECONDS:
                        start_new_spurt = True

        if start_new_spurt:
            target_sample = max(0, mono_sample)
            # Prevent slight arrival jitter from overlapping the speaker's own tail.
            if in_talkspurt and pkt.stream_epoch == prev_epoch and target_sample < last_end_sample:
                if last_end_sample - target_sample <= FRAME_SAMPLES * 2:
                    target_sample = last_end_sample
            anchor_sample = target_sample
            anchor_rtp = pkt.rtp_timestamp
            in_talkspurt = True
        else:
            target_sample = anchor_sample + (
                (pkt.rtp_timestamp - anchor_rtp) & 0xFFFFFFFF
            )
            # Conceal short packet drops inside an active talkspurt without shifting
            # subsequent packets on the timeline.
            expected_frames_between = max(
                0,
                min(seq_delta - 1, int(round(rtp_delta / FRAME_SAMPLES)) - 1),
            )
            if expected_frames_between > 0 and not pkt.is_silence:
                to_conceal = min(expected_frames_between, MAX_CONCEALED_LOSS_FRAMES)
                conceal_cursor = last_end_sample
                for loss_idx in range(to_conceal):
                    is_last_before_pkt = (
                        loss_idx == to_conceal - 1
                        and to_conceal == expected_frames_between
                    )
                    plc_frame = (
                        decoder.decode_fec(pkt.opus_data)
                        if is_last_before_pkt
                        else decoder.decode_plc()
                    )
                    if conceal_cursor + plc_frame.size <= target_sample + FRAME_SAMPLES // 2:
                        conceal_cursor = _add_frame(mix, conceal_cursor, plc_frame)
                        concealed += 1

        decoded = decoder.decode_packet(pkt.opus_data)
        if decoded is None:
            # Malformed Opus packet: substitute PLC/silence of 20ms so timing never shifts.
            decoded = decoder.decode_plc()
            concealed += 1

        last_end_sample = _add_frame(mix, target_sample, decoded)
        prev_seq = pkt.sequence
        prev_rtp = pkt.rtp_timestamp
        prev_epoch = pkt.stream_epoch
        prev_ssrc = pkt.ssrc

    return concealed, decoder.decode_errors


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
