"""Comprehensive tests for ParadiseBOT's rolling voice clip system (/clip)."""

from __future__ import annotations

import asyncio
import glob
import io
import math
import os
import socket
import struct
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import av
import discord
import discord.opus
import nacl.secret
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import EXTENSIONS, ParadiseBot  # noqa: E402
from commands.clip import VoiceClip  # noqa: E402
from commands.voice_presence import VoicePresence  # noqa: E402
from config import Config, VoiceClipConfig  # noqa: E402
from functions.voice_clip import (  # noqa: E402
    AUTHORIZED_CLIP_USER_ID,
    ClipAuthorizationError,
    ClipBusyError,
    ClipDeliveryError,
    ClipExportError,
    ClipExportPipeline,
    ClipExportTimeoutError,
    ClipFileTooLargeError,
    ClipUnavailableError,
    ClipValidationError,
    ClipVoiceClient,
    ConsentManager,
    MAX_CLIP_DURATION,
    MIN_CLIP_DURATION,
    NoUsableAudioError,
    OPUS_SILENCE,
    RecordingConsentView,
    RollingAudioBuffer,
    SAMPLE_RATE,
    TransportDecryptor,
    VoiceClipService,
    VoiceReceiveRouter,
    authorize_clip_user,
    ensure_opus_loaded,
    mix_snapshot_to_pcm_f32,
    validate_clip_duration,
)


# ---------------------------------------------------------------------------
# Helpers: Real Opus encoding, RTP AEAD encryption, and M4A decoding
# ---------------------------------------------------------------------------


def _encode_opus_tone(
    freq_hz: float,
    duration_sec: float,
    *,
    amplitude: float = 0.35,
) -> list[bytes]:
    """Encode a clean 48 kHz sine signal into real 20 ms Discord Opus packets."""
    assert ensure_opus_loaded(), "libopus must be loadable for Opus fixture encoding"
    encoder = discord.opus.Encoder()
    n_frames = max(1, int(round(duration_sec / 0.02)))
    packets: list[bytes] = []
    sample_cursor = 0
    for _ in range(n_frames):
        idx = np.arange(sample_cursor, sample_cursor + 960, dtype=np.float64)
        mono = (amplitude * 32767.0) * np.sin(2.0 * math.pi * freq_hz * idx / SAMPLE_RATE)
        s16 = np.clip(mono, -32768, 32767).astype(np.int16)
        stereo = np.column_stack((s16, s16)).reshape(-1)
        packets.append(encoder.encode(stereo.tobytes(), 960))
        sample_cursor += 960
    return packets


def _build_rtpsize_aead_datagram(
    *,
    secret_key: bytes,
    ssrc: int,
    sequence: int,
    rtp_timestamp: int,
    payload: bytes,
    nonce_int: int,
    with_bede_extension: bool = True,
) -> bytes:
    """Construct a real Discord `aead_xchacha20_poly1305_rtpsize` UDP packet."""
    box = nacl.secret.Aead(secret_key)
    b0 = 0x90 if with_bede_extension else 0x80  # v=2, X=1 if extension else 0
    b1 = 0x78  # payload type 120 (Discord Opus)
    fixed = struct.pack(">BBHII", b0, b1, sequence & 0xFFFF, rtp_timestamp & 0xFFFFFFFF, ssrc)

    if with_bede_extension:
        # RFC 5285 0xBEDE 1-word extension header in unencrypted AAD + 4-byte body in plaintext
        ext_preamble = struct.pack(">2sH", b"\xbe\xde", 1)
        aad = fixed + ext_preamble
        ext_body = b"\x10\x2a\x00\x00"  # id=1 (audio power), len=1, val=42 + padding
        plaintext = ext_body + payload
    else:
        aad = fixed
        plaintext = payload

    nonce = bytearray(24)
    nonce[:4] = struct.pack(">I", nonce_int & 0xFFFFFFFF)
    ciphertext = box.encrypt(plaintext, aad, bytes(nonce)).ciphertext
    return aad + ciphertext + bytes(nonce[:4])


def _decode_m4a_to_mono_f32(path_or_bytes: str | bytes) -> tuple[np.ndarray, int, str]:
    """Decode an exported M4A file via PyAV and return (samples_f32, sample_rate, codec_name)."""
    src = io.BytesIO(path_or_bytes) if isinstance(path_or_bytes, bytes) else path_or_bytes
    with av.open(src, "r") as container:
        audio_streams = [s for s in container.streams if s.type == "audio"]
        assert len(audio_streams) == 1
        stream = audio_streams[0]
        codec_name = stream.codec_context.name
        rate = int(stream.codec_context.rate)
        resampler = av.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE)
        chunks: list[np.ndarray] = []
        for frame in container.decode(stream):
            for out_f in resampler.resample(frame):
                arr = np.frombuffer(bytes(out_f.planes[0]), dtype=np.float32)[: out_f.samples].copy()
                chunks.append(arr)
    samples = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
    return samples, rate, codec_name


def _dominant_power_at_freq(samples: np.ndarray, target_hz: float, bandwidth_hz: float = 15.0) -> float:
    """Measure normalized FFT magnitude around ``target_hz``."""
    windowed = samples * np.hanning(samples.size)
    spectrum = np.abs(np.fft.rfft(windowed)) / max(1, samples.size)
    freqs = np.fft.rfftfreq(samples.size, d=1.0 / SAMPLE_RATE)
    mask = np.abs(freqs - target_hz) <= bandwidth_hz
    return float(np.max(spectrum[mask])) if np.any(mask) else 0.0


class _FakeClock:
    def __init__(self, initial: float = 1000.0) -> None:
        self.now = initial

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


def _make_guild_with_vc(guild_id: int = 777, channel_id: int = 888, *, connected: bool = True):
    perms = SimpleNamespace(view_channel=True, connect=True, speak=True)
    channel = MagicMock(spec=discord.VoiceChannel)
    channel.id = channel_id
    channel.name = "General VC"
    channel.permissions_for = MagicMock(return_value=perms)
    channel.send = AsyncMock()

    vc = MagicMock(spec=ClipVoiceClient)
    vc.channel = channel
    vc.is_connected = MagicMock(return_value=connected)

    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    guild.name = "Paradise Server"
    guild.voice_client = vc if connected else None
    guild.me = SimpleNamespace(
        id=999000,
        voice=SimpleNamespace(self_deaf=False, deaf=False, mute=False),
    )
    guild.get_member = MagicMock(return_value=SimpleNamespace(bot=False))
    return guild, channel, vc


def _make_interaction(
    *,
    user_id: object,
    guild,
    username: str = "someone",
    captured_files: list[tuple[str, bytes, str]] | None = None,
    dm_exception: Exception | None = None,
):
    dm_channel = MagicMock()

    async def _dm_send(*, content: str = "", file: discord.File | None = None, **kwargs):
        if dm_exception is not None:
            raise dm_exception
        if file is not None and captured_files is not None:
            file.fp.seek(0)
            data = file.fp.read()
            captured_files.append((file.filename, data, content))
        return MagicMock()

    dm_channel.send = AsyncMock(side_effect=_dm_send)

    user = MagicMock(spec=discord.Member)
    user.id = user_id
    user.name = username
    user.display_name = username
    user.dm_channel = None
    user.create_dm = AsyncMock(return_value=dm_channel)
    user.send = AsyncMock(side_effect=_dm_send)

    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = user
    interaction.guild = guild
    interaction.guild_id = getattr(guild, "id", None)

    done_state = {"done": False}
    interaction.response = MagicMock()
    interaction.response.is_done = MagicMock(side_effect=lambda: done_state["done"])

    async def _defer(**kwargs):
        done_state["done"] = True

    async def _send_msg(*args, **kwargs):
        done_state["done"] = True

    interaction.response.defer = AsyncMock(side_effect=_defer)
    interaction.response.send_message = AsyncMock(side_effect=_send_msg)
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction, dm_channel


# ===========================================================================
# 1. Authorization & Command Validation Tests
# ===========================================================================


class TestClipAuthorizationAndValidation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.clock = _FakeClock()
        self.bot = SimpleNamespace(
            user=SimpleNamespace(id=999000),
            config=Config(),
            wait_until_ready=AsyncMock(),
        )
        self.cog = VoiceClip(self.bot)  # type: ignore[arg-type]
        self.cog.service = VoiceClipService(self.bot, VoiceClipConfig(), clock=self.clock)  # type: ignore[arg-type]
        self.bot.voice_clip_service = self.cog.service
        self.guild, self.channel, self.vc = _make_guild_with_vc()
        self.cog.service.attach_voice_client(
            guild=self.guild, channel_id=self.channel.id, voice_client=self.vc
        )
        # Seed 3 seconds of real Opus tone so authorized calls succeed
        buf = self.cog.service.get_buffer(self.guild.id)
        assert buf is not None
        for idx, pkt in enumerate(_encode_opus_tone(440.0, 1.0)):
            buf.append_packet(
                user_id=5001,
                opus_data=pkt,
                rtp_timestamp=idx * 960,
                sequence=idx,
                ssrc=111,
                mono_time=self.clock.advance(0.02),
            )

    async def asyncTearDown(self) -> None:
        await self.cog.service.shutdown()

    def test_exact_authorized_id_and_type_checks(self) -> None:
        self.assertEqual(AUTHORIZED_CLIP_USER_ID, 922359954025885737)
        self.assertTrue(authorize_clip_user(922359954025885737))
        for bad in (
            922359954025885736,
            922359954025885738,
            0,
            -1,
            "922359954025885737",
            True,
            False,
            None,
            922359954025885737.0,
        ):
            self.assertFalse(authorize_clip_user(bad), f"Unexpectedly authorized {bad!r}")

    def test_duration_validation_accepts_valid_and_rejects_invalid(self) -> None:
        for valid in (1, 15, 30, 60, 180):
            self.assertEqual(validate_clip_duration(valid), valid)

        for invalid in (0, -1, -180, 181, 300, 15.5, 1.0, "30", "abc", None, True, False):
            with self.assertRaises(ClipValidationError, msg=f"Should reject {invalid!r}"):
                validate_clip_duration(invalid)

    async def test_authorized_user_succeeds_for_1_30_60_180_and_username_irrelevant(self) -> None:
        for requested in (1, 30, 60, 180):
            delivered: list[tuple[str, bytes, str]] = []
            interaction, _ = _make_interaction(
                user_id=AUTHORIZED_CLIP_USER_ID,
                guild=self.guild,
                username=f"completely_renamed_{requested}",
                captured_files=delivered,
            )
            await self.cog.clip.callback(self.cog, interaction, requested)
            self.assertEqual(len(delivered), 1)
            filename, raw_bytes, dm_text = delivered[0]
            self.assertTrue(filename.endswith(".m4a"))
            self.assertGreater(len(raw_bytes), 512)
            self.assertIn(f"`{requested}s`", dm_text)

    async def test_unauthorized_user_denied_ephemerally_without_audio_work_or_leak(self) -> None:
        buf = self.cog.service.get_buffer(self.guild.id)
        assert buf is not None
        buf.snapshot = MagicMock(wraps=buf.snapshot)  # type: ignore[method-assign]
        self.cog.service.exporter.export = AsyncMock()  # type: ignore[method-assign]

        delivered: list[tuple[str, bytes, str]] = []
        interaction, dm_channel = _make_interaction(
            user_id=111222333444555666,
            guild=self.guild,
            username="owner",  # impersonating username must NEVER pass
            captured_files=delivered,
        )
        await self.cog.clip.callback(self.cog, interaction, 15)

        interaction.response.send_message.assert_awaited_once()
        call_kwargs = interaction.response.send_message.call_args.kwargs
        denial_text = interaction.response.send_message.call_args.args[0]
        self.assertTrue(call_kwargs.get("ephemeral"))
        self.assertNotIn(str(AUTHORIZED_CLIP_USER_ID), denial_text)
        interaction.response.defer.assert_not_called()
        buf.snapshot.assert_not_called()
        self.cog.service.exporter.export.assert_not_called()
        dm_channel.send.assert_not_called()
        self.assertEqual(delivered, [])

    async def test_internal_service_entry_points_also_enforce_authorization(self) -> None:
        intruder = SimpleNamespace(id=42424242, create_dm=AsyncMock())
        with self.assertRaises(ClipAuthorizationError):
            await self.cog.service.export_clip_for_guild(
                requester_user_id=42424242,
                guild=self.guild,
                duration=10,
            )
        with self.assertRaises(ClipAuthorizationError):
            await self.cog.service.create_and_deliver_clip(
                requester=intruder,  # type: ignore[arg-type]
                guild=self.guild,
                duration=10,
            )
        intruder.create_dm.assert_not_called()


# ===========================================================================
# 2. Rolling Buffer Correctness Tests
# ===========================================================================


class TestRollingAudioBuffer(unittest.TestCase):
    def test_never_retains_more_than_180_seconds_and_evicts_expired(self) -> None:
        clock = _FakeClock(initial=10_000.0)
        buf = RollingAudioBuffer(guild_id=1, max_seconds=180.0, clock=clock)
        buf.start_session(channel_id=10)
        sample_pkt = _encode_opus_tone(440.0, 0.02)[0]

        # Feed 420 seconds (7 minutes) of packets (1 packet per 0.5s for fast simulation)
        for step in range(840):
            clock.advance(0.5)
            buf.append_packet(
                user_id=101,
                opus_data=sample_pkt,
                rtp_timestamp=step * 24_000,
                sequence=step,
                ssrc=7,
            )
            stats = buf.stats()
            self.assertLessEqual(float(stats["oldest_packet_age_seconds"]), 180.0001)

        snap = buf.snapshot(180)
        self.assertAlmostEqual(snap.captured_seconds, 180.0, places=2)
        for spk in snap.speakers:
            for pkt in spk.packets:
                self.assertGreaterEqual(pkt.mono_time, clock() - 180.0)

        # Requesting 30s after 7 minutes returns ONLY the latest 30s
        snap_30 = buf.snapshot(30)
        self.assertAlmostEqual(snap_30.captured_seconds, 30.0, places=2)
        for spk in snap_30.speakers:
            for pkt in spk.packets:
                self.assertGreaterEqual(pkt.mono_time, clock() - 30.0)

    def test_short_recording_session_reports_actual_available_duration(self) -> None:
        clock = _FakeClock(initial=500.0)
        buf = RollingAudioBuffer(guild_id=1, clock=clock)
        buf.start_session(channel_id=10)
        pkt = _encode_opus_tone(440.0, 0.02)[0]

        # Only 12.5 seconds elapsed since recording started
        for i in range(50):
            clock.advance(0.25)
            buf.append_packet(
                user_id=101,
                opus_data=pkt,
                rtp_timestamp=i * 12_000,
                sequence=i,
                ssrc=1,
            )

        snap = buf.snapshot(60)
        self.assertEqual(snap.requested_seconds, 60)
        self.assertAlmostEqual(snap.captured_seconds, 12.5, places=2)

    def test_empty_or_silence_only_raises_clear_error(self) -> None:
        clock = _FakeClock(initial=100.0)
        buf = RollingAudioBuffer(guild_id=1, clock=clock)
        buf.start_session(channel_id=10)

        with self.assertRaises(NoUsableAudioError):
            buf.snapshot(10)

        # Append only Discord silence frames
        for i in range(10):
            clock.advance(0.02)
            buf.append_packet(
                user_id=101,
                opus_data=OPUS_SILENCE,
                rtp_timestamp=i * 960,
                sequence=i,
                ssrc=1,
            )
        with self.assertRaises(NoUsableAudioError):
            buf.snapshot(10)

    def test_memory_budget_and_max_speakers_stay_strictly_bounded(self) -> None:
        clock = _FakeClock(initial=100.0)
        max_bytes = 32 * 1024  # 32 KiB tight cap
        buf = RollingAudioBuffer(
            guild_id=1,
            max_seconds=180.0,
            max_active_speakers=3,
            max_buffer_bytes=max_bytes,
            clock=clock,
        )
        buf.start_session(channel_id=10)
        pkt = _encode_opus_tone(440.0, 0.02)[0]

        for step in range(600):
            clock.advance(0.02)
            buf.append_packet(
                user_id=1000 + (step % 8),
                opus_data=pkt,
                rtp_timestamp=step * 960,
                sequence=step,
                ssrc=step % 8,
            )
            stats = buf.stats()
            self.assertLessEqual(int(stats["buffered_bytes"]), max_bytes)
            self.assertLessEqual(int(stats["active_speakers"]), 3)


# ===========================================================================
# 3. Audio Quality, Overlapping Speakers, Gap Preservation & Anti-Clipping
# ===========================================================================


class TestAudioQualityAndMixing(unittest.IsolatedAsyncioTestCase):
    async def test_two_overlapping_tones_preserve_frequencies_timing_and_m4a_playability(self) -> None:
        clock = _FakeClock(initial=2000.0)
        buf = RollingAudioBuffer(guild_id=1, clock=clock)
        buf.start_session(channel_id=50)

        # Total 3.0s window:
        # Speaker A (440 Hz): speaks 0.2s .. 2.2s
        # Speaker B (880 Hz): joins later, speaks 1.2s .. 2.8s (overlaps 1.2s .. 2.2s)
        t0 = clock.now
        pkts_a = _encode_opus_tone(440.0, 2.0, amplitude=0.30)
        pkts_b = _encode_opus_tone(880.0, 1.6, amplitude=0.30)

        for i, pkt in enumerate(pkts_a):
            buf.append_packet(
                user_id=1,
                opus_data=pkt,
                rtp_timestamp=i * 960,
                sequence=i,
                ssrc=10,
                mono_time=t0 + 0.20 + i * 0.02,
            )
        for i, pkt in enumerate(pkts_b):
            buf.append_packet(
                user_id=2,
                opus_data=pkt,
                rtp_timestamp=50_000 + i * 960,
                sequence=200 + i,
                ssrc=20,
                mono_time=t0 + 1.20 + i * 0.02,
            )
        clock.now = t0 + 3.0

        snapshot = buf.snapshot(3)
        pipeline = ClipExportPipeline()
        try:
            with await pipeline.export(snapshot) as exported:
                self.assertTrue(os.path.isfile(exported.file_path))
                samples, rate, codec_name = _decode_m4a_to_mono_f32(exported.file_path)
                temp_path = exported.file_path

            # Temp file cleaned up on exit
            self.assertFalse(os.path.exists(temp_path))

            self.assertEqual(codec_name, "aac")
            self.assertEqual(rate, SAMPLE_RATE)
            self.assertAlmostEqual(samples.size / SAMPLE_RATE, 3.0, delta=0.10)

            # Segment 1 (0.35s .. 1.05s): ONLY 440 Hz should be strong
            seg_a_only = samples[int(0.35 * SAMPLE_RATE) : int(1.05 * SAMPLE_RATE)]
            p440_seg1 = _dominant_power_at_freq(seg_a_only, 440.0)
            p880_seg1 = _dominant_power_at_freq(seg_a_only, 880.0)
            self.assertGreater(p440_seg1, 0.04)
            self.assertLess(p880_seg1, p440_seg1 * 0.05)

            # Segment 2 (1.35s .. 2.05s): BOTH 440 Hz and 880 Hz overlap simultaneously
            seg_overlap = samples[int(1.35 * SAMPLE_RATE) : int(2.05 * SAMPLE_RATE)]
            p440_both = _dominant_power_at_freq(seg_overlap, 440.0)
            p880_both = _dominant_power_at_freq(seg_overlap, 880.0)
            self.assertGreater(p440_both, 0.04)
            self.assertGreater(p880_both, 0.04)

            # Segment 3 (2.35s .. 2.75s): ONLY 880 Hz remains after Speaker A stops
            seg_b_only = samples[int(2.35 * SAMPLE_RATE) : int(2.75 * SAMPLE_RATE)]
            p440_seg3 = _dominant_power_at_freq(seg_b_only, 440.0)
            p880_seg3 = _dominant_power_at_freq(seg_b_only, 880.0)
            self.assertGreater(p880_seg3, 0.04)
            self.assertLess(p440_seg3, p880_seg3 * 0.05)
        finally:
            await pipeline.shutdown()

    def test_silence_gap_does_not_shift_subsequent_speech_even_with_paused_rtp_clock(self) -> None:
        clock = _FakeClock(initial=3000.0)
        buf = RollingAudioBuffer(guild_id=1, clock=clock)
        buf.start_session(channel_id=50)
        t0 = clock.now

        burst1 = _encode_opus_tone(500.0, 0.5, amplitude=0.35)
        burst2 = _encode_opus_tone(500.0, 0.5, amplitude=0.35)

        # Burst 1 at 0.0s .. 0.5s
        for i, pkt in enumerate(burst1):
            buf.append_packet(
                user_id=1,
                opus_data=pkt,
                rtp_timestamp=i * 960,
                sequence=i,
                ssrc=10,
                mono_time=t0 + i * 0.02,
            )

        # Complete 1.5s silence (0.5s .. 2.0s), AND simulate Discord WebRTC pausing
        # RTP timestamp across silence (continues right at len(burst1)*960)!
        for i, pkt in enumerate(burst2):
            buf.append_packet(
                user_id=1,
                opus_data=pkt,
                rtp_timestamp=(len(burst1) + i) * 960,
                sequence=len(burst1) + i,
                ssrc=10,
                mono_time=t0 + 2.0 + i * 0.02,
            )
        clock.now = t0 + 3.0

        mix = mix_snapshot_to_pcm_f32(buf.snapshot(3))
        # Gap region (0.7s .. 1.8s) MUST be silent
        gap_rms = float(
            np.sqrt(np.mean(mix.samples[int(0.7 * SAMPLE_RATE) : int(1.8 * SAMPLE_RATE)] ** 2))
        )
        # Second burst region (2.05s .. 2.45s) MUST carry the 500 Hz tone
        burst2_rms = float(
            np.sqrt(np.mean(mix.samples[int(2.05 * SAMPLE_RATE) : int(2.45 * SAMPLE_RATE)] ** 2))
        )
        self.assertLess(gap_rms, 0.005)
        self.assertGreater(burst2_rms, 0.10)

    def test_limiter_prevents_clipping_under_heavy_overlap_without_corrupting_quiet_speech(self) -> None:
        clock = _FakeClock(initial=4000.0)
        buf = RollingAudioBuffer(guild_id=1, clock=clock)
        buf.start_session(channel_id=50)
        t0 = clock.now

        # 4 loud concurrent speakers at 0.0 .. 1.0s (each 0.70 amplitude -> sum ~2.8 > 1.0!)
        freqs = (330.0, 440.0, 554.37, 659.25)
        for idx, freq in enumerate(freqs, start=1):
            for i, pkt in enumerate(_encode_opus_tone(freq, 1.0, amplitude=0.70)):
                buf.append_packet(
                    user_id=idx,
                    opus_data=pkt,
                    rtp_timestamp=i * 960,
                    sequence=i,
                    ssrc=idx * 10,
                    mono_time=t0 + i * 0.02,
                )

        # Quiet single speaker at 1.4 .. 2.0s (amplitude 0.15)
        for i, pkt in enumerate(_encode_opus_tone(440.0, 0.6, amplitude=0.15)):
            buf.append_packet(
                user_id=9,
                opus_data=pkt,
                rtp_timestamp=i * 960,
                sequence=i,
                ssrc=90,
                mono_time=t0 + 1.40 + i * 0.02,
            )
        clock.now = t0 + 2.0

        mix = mix_snapshot_to_pcm_f32(buf.snapshot(2))
        self.assertGreater(mix.peak_before_limiter, 1.4)
        self.assertLessEqual(mix.peak_after_limiter, 0.925)
        # Quiet tail after limiter recovery retains its natural level (~0.15 peak)
        quiet_tail_peak = float(np.max(np.abs(mix.samples[int(1.55 * SAMPLE_RATE) : int(1.95 * SAMPLE_RATE)])))
        self.assertGreater(quiet_tail_peak, 0.10)

    def test_packet_loss_and_malformed_opus_packets_do_not_corrupt_remainder(self) -> None:
        clock = _FakeClock(initial=5000.0)
        buf = RollingAudioBuffer(guild_id=1, clock=clock)
        buf.start_session(channel_id=50)
        t0 = clock.now

        pkts = _encode_opus_tone(600.0, 2.0, amplitude=0.35)
        for i, pkt in enumerate(pkts):
            # Drop every 7th packet (simulate packet loss)
            if i % 7 == 3:
                continue
            # Corrupt packet 25 with malformed bytes
            data = b"\x00\xff\x00" if i == 25 else pkt
            buf.append_packet(
                user_id=1,
                opus_data=data,
                rtp_timestamp=i * 960,
                sequence=i,
                ssrc=11,
                mono_time=t0 + i * 0.02,
            )
        clock.now = t0 + 2.0

        mix = mix_snapshot_to_pcm_f32(buf.snapshot(2))
        self.assertAlmostEqual(mix.duration_seconds, 2.0, places=2)
        self.assertGreater(mix.concealed_frames, 5)
        # Second half of the clip (after all drops & malformed frame) still cleanly has 600 Hz!
        tail = mix.samples[int(1.0 * SAMPLE_RATE) :]
        self.assertGreater(_dominant_power_at_freq(tail, 600.0), 0.05)


# ===========================================================================
# 4. Lifecycle, Privacy, Consent, Concurrency & Failure Recovery
# ===========================================================================


class TestLifecyclePrivacyAndReliability(unittest.IsolatedAsyncioTestCase):
    def test_consent_notice_opt_out_and_opt_in_modes_purge_immediately(self) -> None:
        clock = _FakeClock(initial=100.0)
        bot = SimpleNamespace(user=SimpleNamespace(id=999))

        # 1. Notice mode: captures until user opts out, then blocks + purges immediately
        svc = VoiceClipService(bot, VoiceClipConfig(consent_mode="notice"), clock=clock)  # type: ignore[arg-type]
        guild, channel, vc = _make_guild_with_vc()
        svc.attach_voice_client(guild=guild, channel_id=channel.id, voice_client=vc)
        buf = svc.get_buffer(guild.id)
        assert buf is not None
        pkt = _encode_opus_tone(440.0, 0.02)[0]
        buf.append_packet(user_id=201, opus_data=pkt, rtp_timestamp=0, sequence=1, ssrc=1)
        buf.append_packet(user_id=202, opus_data=pkt, rtp_timestamp=0, sequence=1, ssrc=2)
        self.assertEqual(buf.stats()["active_speakers"], 2)

        svc.consent.revoke_user(guild.id, 201)
        self.assertFalse(svc.consent.can_record(guild.id, 201))
        self.assertTrue(svc.consent.can_record(guild.id, 202))
        self.assertEqual(buf.stats()["active_speakers"], 1)

        # 2. Opt-in mode: blocks until explicit allow_user
        opt_in_consent = ConsentManager(mode="opt_in")
        self.assertFalse(opt_in_consent.can_record(1, 301))
        opt_in_consent.allow_user(1, 301)
        self.assertTrue(opt_in_consent.can_record(1, 301))
        opt_in_consent.revoke_user(1, 301)
        self.assertFalse(opt_in_consent.can_record(1, 301))

    async def test_recording_continues_during_export_and_export_failures_preserve_buffer(self) -> None:
        clock = _FakeClock(initial=1000.0)
        bot = SimpleNamespace(user=SimpleNamespace(id=999))
        svc = VoiceClipService(bot, VoiceClipConfig(), clock=clock)  # type: ignore[arg-type]
        guild, channel, vc = _make_guild_with_vc()
        svc.attach_voice_client(guild=guild, channel_id=channel.id, voice_client=vc)
        buf = svc.get_buffer(guild.id)
        assert buf is not None

        for i, pkt in enumerate(_encode_opus_tone(440.0, 0.5)):
            buf.append_packet(
                user_id=10,
                opus_data=pkt,
                rtp_timestamp=i * 960,
                sequence=i,
                ssrc=1,
                mono_time=clock.advance(0.02),
            )

        # Simulate encoder failure on first attempt: temp dir must be cleaned & buffer intact
        bad_exporter = ClipExportPipeline(ffmpeg_executable="/nonexistent/ffmpeg-binary")
        with self.assertRaises(ClipExportError):
            await bad_exporter.export(buf.snapshot(1))
        self.assertEqual(bad_exporter.active_temp_bytes, 0)
        self.assertEqual(len(bad_exporter._live_temp_dirs), 0)

        # Start a real export and append live packets concurrently while export runs
        export_coro = svc.export_clip_for_guild(
            requester_user_id=AUTHORIZED_CLIP_USER_ID,
            guild=guild,
            duration=1,
        )
        export_task = asyncio.create_task(export_coro, name="test-concurrent-export")
        for i, pkt in enumerate(_encode_opus_tone(880.0, 0.4)):
            buf.append_packet(
                user_id=11,
                opus_data=pkt,
                rtp_timestamp=i * 960,
                sequence=i,
                ssrc=2,
                mono_time=clock.advance(0.02),
            )
        clip1, _ = await export_task
        clip1.cleanup()

        # Subsequent clip contains both speakers!
        clip2, _ = await svc.export_clip_for_guild(
            requester_user_id=AUTHORIZED_CLIP_USER_ID,
            guild=guild,
            duration=2,
        )
        try:
            self.assertEqual(clip2.speaker_count, 2)
        finally:
            clip2.cleanup()
            await svc.shutdown()

    async def test_dm_failure_cleans_temp_file_and_never_posts_publicly(self) -> None:
        clock = _FakeClock(initial=1000.0)
        bot = SimpleNamespace(user=SimpleNamespace(id=999), config=Config())
        cog = VoiceClip(bot)  # type: ignore[arg-type]
        cog.service = VoiceClipService(bot, VoiceClipConfig(), clock=clock)  # type: ignore[arg-type]
        guild, channel, vc = _make_guild_with_vc()
        cog.service.attach_voice_client(guild=guild, channel_id=channel.id, voice_client=vc)
        buf = cog.service.get_buffer(guild.id)
        assert buf is not None
        for i, pkt in enumerate(_encode_opus_tone(440.0, 0.4)):
            buf.append_packet(
                user_id=10,
                opus_data=pkt,
                rtp_timestamp=i * 960,
                sequence=i,
                ssrc=1,
                mono_time=clock.advance(0.02),
            )

        forbidden = discord.Forbidden(MagicMock(status=403), "Cannot send messages to this user")
        interaction, _ = _make_interaction(
            user_id=AUTHORIZED_CLIP_USER_ID,
            guild=guild,
            dm_exception=forbidden,
        )
        await cog.clip.callback(cog, interaction, 5)

        # Must never send to voice/public channel
        channel.send.assert_not_called()
        # Private ephemeral error reported
        interaction.followup.send.assert_awaited_once()
        followup_msg = interaction.followup.send.call_args.args[0]
        self.assertIn("DM", followup_msg)
        self.assertTrue(interaction.followup.send.call_args.kwargs.get("ephemeral"))
        # Zero leftover temp bytes or temp dirs
        self.assertEqual(cog.service.exporter.active_temp_bytes, 0)
        self.assertEqual(len(cog.service.exporter._live_temp_dirs), 0)
        await cog.service.shutdown()

    async def test_concurrent_export_limits_and_disconnect_channel_move_cleanup(self) -> None:
        clock = _FakeClock(initial=1000.0)
        pipeline = ClipExportPipeline(
            max_concurrent_exports=1,
            max_queued_exports=0,
            export_timeout_seconds=10.0,
        )
        buf = RollingAudioBuffer(guild_id=1, clock=clock)
        buf.start_session(channel_id=10)
        for i, pkt in enumerate(_encode_opus_tone(440.0, 0.2)):
            buf.append_packet(
                user_id=1,
                opus_data=pkt,
                rtp_timestamp=i * 960,
                sequence=i,
                ssrc=1,
                mono_time=clock.advance(0.02),
            )
        snap = buf.snapshot(1)

        gate = asyncio.Event()
        real_decode = pipeline._decode_mix_and_encode

        async def _slow_decode(s):
            await gate.wait()
            return await real_decode(s)

        pipeline._decode_mix_and_encode = _slow_decode  # type: ignore[method-assign]
        t1 = asyncio.create_task(pipeline.export(snap), name="test-export-1")
        await asyncio.sleep(0.02)

        # Capacity reached (1 running + 0 queued) -> immediate ClipBusyError
        with self.assertRaises(ClipBusyError):
            await pipeline.export(snap)

        gate.set()
        res = await t1
        res.cleanup()
        await pipeline.shutdown()

        # Channel switch clears old channel's audio; disconnect wipes everything
        buf.start_session(channel_id=999)
        self.assertEqual(buf.stats()["buffered_packets"], 0)
        buf.stop_session()
        self.assertFalse(buf.is_recording)


# ===========================================================================
# 5. Real UDP Loopback + ClipVoiceClient + RTP AEAD Integration Test
# ===========================================================================


class TestRealUDPClipVoiceClientIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_end_to_end_udp_socket_reader_rtp_aead_to_dm_m4a(self) -> None:
        """Full pipeline over a real OS UDP socket through SocketReader & ClipVoiceClient."""
        bot_config = Config()
        bot = ParadiseBot(bot_config)
        cog = VoiceClip(bot)
        guild, channel, _ = _make_guild_with_vc(guild_id=5555, channel_id=6666)
        channel._get_voice_client_key = lambda: (guild.id, "guild_id")
        channel.guild = guild

        # Instantiate the REAL ClipVoiceClient (creates real VoiceConnectionState + SocketReader thread)
        vc = ClipVoiceClient(bot, channel)
        guild.voice_client = vc
        vc._connection.state = discord.voice_state.ConnectionFlowState.connected

        recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        recv_sock.bind(("127.0.0.1", 0))
        recv_sock.setblocking(False)
        recv_addr = recv_sock.getsockname()
        vc._connection.socket = recv_sock
        vc._connection._socket_reader.resume()

        sender_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        secret_key = bytes(range(32))
        fake_ws = SimpleNamespace(secret_key=list(secret_key), speak=AsyncMock())

        try:
            # 1. Dispatch real Voice Gateway READY + SESSION_DESCRIPTION messages
            await vc._on_voice_ws_message(fake_ws, {"op": 2, "d": {"ssrc": 9999}})  # type: ignore[arg-type]
            await vc._on_voice_ws_message(
                fake_ws,  # type: ignore[arg-type]
                {
                    "op": 4,
                    "d": {
                        "mode": "aead_xchacha20_poly1305_rtpsize",
                        "dave_protocol_version": 0,
                    },
                },
            )

            pkts_alice = _encode_opus_tone(440.0, 0.6, amplitude=0.35)
            pkts_bob = _encode_opus_tone(880.0, 0.6, amplitude=0.35)
            nonce = 1

            # 2. Send first 3 UDP packets for Alice BEFORE op=5 SPEAKING arrives (tests staging!)
            for i in range(3):
                dgram = _build_rtpsize_aead_datagram(
                    secret_key=secret_key,
                    ssrc=1111,
                    sequence=i,
                    rtp_timestamp=i * 960,
                    payload=pkts_alice[i],
                    nonce_int=nonce,
                )
                nonce += 1
                sender_sock.sendto(dgram, recv_addr)
            await asyncio.sleep(0.03)

            # Now send SPEAKING for Alice (1111 -> user 7001) and VIDEO ssrc mapping for Bob (2222 -> user 7002)
            await vc._on_voice_ws_message(
                fake_ws,  # type: ignore[arg-type]
                {"op": 5, "d": {"user_id": "7001", "ssrc": 1111, "speaking": 1}},
            )
            await vc._on_voice_ws_message(
                fake_ws,  # type: ignore[arg-type]
                {"op": 12, "d": {"user_id": "7002", "audio_ssrc": 2222}},
            )

            # 3. Stream remaining overlapping UDP RTP packets over localhost socket
            for i in range(3, len(pkts_alice)):
                dgram_a = _build_rtpsize_aead_datagram(
                    secret_key=secret_key,
                    ssrc=1111,
                    sequence=i,
                    rtp_timestamp=i * 960,
                    payload=pkts_alice[i],
                    nonce_int=nonce,
                )
                nonce += 1
                dgram_b = _build_rtpsize_aead_datagram(
                    secret_key=secret_key,
                    ssrc=2222,
                    sequence=100 + i,
                    rtp_timestamp=5000 + i * 960,
                    payload=pkts_bob[i],
                    nonce_int=nonce,
                )
                nonce += 1
                sender_sock.sendto(dgram_a, recv_addr)
                sender_sock.sendto(dgram_b, recv_addr)
                await asyncio.sleep(0.005)

            await asyncio.sleep(0.05)

            # 4. Invoke /clip as authorized user 922359954025885737
            delivered: list[tuple[str, bytes, str]] = []
            interaction, _ = _make_interaction(
                user_id=AUTHORIZED_CLIP_USER_ID,
                guild=guild,
                captured_files=delivered,
            )
            await cog.clip.callback(cog, interaction, 5)

            self.assertEqual(len(delivered), 1)
            filename, m4a_bytes, dm_text = delivered[0]
            self.assertTrue(filename.endswith(".m4a"))
            self.assertIn("Captured speakers:** `2`", dm_text)

            samples, rate, codec_name = _decode_m4a_to_mono_f32(m4a_bytes)
            self.assertEqual(codec_name, "aac")
            self.assertEqual(rate, SAMPLE_RATE)
            self.assertGreater(_dominant_power_at_freq(samples, 440.0), 0.03)
            self.assertGreater(_dominant_power_at_freq(samples, 880.0), 0.03)
        finally:
            vc._teardown_recording()
            vc._connection._socket_reader.stop()
            recv_sock.close()
            sender_sock.close()
            await cog.service.shutdown()
            await bot.close()

    async def test_voice_presence_connects_undeafened_with_clip_voice_client_when_enabled(self) -> None:
        bot = ParadiseBot(Config())
        vp = VoicePresence(bot, settle_delay=0.0, speech_providers=[])
        perms = SimpleNamespace(view_channel=True, connect=True, speak=True, move_members=False)
        vc_chan = MagicMock()
        vc_chan.id = 321
        vc_chan.members = [SimpleNamespace(bot=False, id=1)]
        vc_chan.user_limit = 0
        vc_chan.permissions_for = MagicMock(return_value=perms)
        vc_chan.send = AsyncMock()
        vc_chan.connect = AsyncMock()

        guild = MagicMock()
        guild.id = 999
        guild.afk_channel = None
        guild.me = SimpleNamespace(id=12345, voice=SimpleNamespace(mute=False))
        guild.voice_channels = [vc_chan]
        guild.voice_client = None

        try:
            await vp.join_busiest(guild)
            vc_chan.connect.assert_awaited_once()
            kwargs = vc_chan.connect.call_args.kwargs
            self.assertIs(kwargs.get("cls"), ClipVoiceClient)
            self.assertIs(kwargs.get("self_deaf"), False)
        finally:
            await vp.cog_unload()
            await bot.close()


# ===========================================================================
# 6. Comprehensive Error Handling, Transport Modes & DAVE Edge Cases
# ===========================================================================


class TestErrorHandlingAndTransportEdgeCases(unittest.IsolatedAsyncioTestCase):
    def test_all_four_rtp_transport_modes_and_malformed_datagrams(self) -> None:
        secret = bytes(range(32))
        payload = _encode_opus_tone(440.0, 0.02)[0]
        header = struct.pack(">BBHII", 0x80, 0x78, 42, 9600, 123456)

        # 1. aead_xchacha20_poly1305_rtpsize
        dec_aead = TransportDecryptor("aead_xchacha20_poly1305_rtpsize", secret)
        dgram_aead = _build_rtpsize_aead_datagram(
            secret_key=secret,
            ssrc=123456,
            sequence=42,
            rtp_timestamp=9600,
            payload=payload,
            nonce_int=7,
            with_bede_extension=True,
        )
        p1 = dec_aead.decrypt_rtp(dgram_aead)
        assert p1 is not None
        self.assertEqual((p1.sequence, p1.rtp_timestamp, p1.ssrc, p1.payload), (42, 9600, 123456, payload))

        # 2. xsalsa20_poly1305_lite
        sbox = nacl.secret.SecretBox(secret)
        nonce_lite = bytearray(24)
        nonce_lite[:4] = struct.pack(">I", 99)
        dgram_lite = header + sbox.encrypt(payload, bytes(nonce_lite)).ciphertext + bytes(nonce_lite[:4])
        p2 = TransportDecryptor("xsalsa20_poly1305_lite", secret).decrypt_rtp(dgram_lite)
        assert p2 is not None
        self.assertEqual((p2.sequence, p2.rtp_timestamp, p2.ssrc, p2.payload), (42, 9600, 123456, payload))

        # 3. xsalsa20_poly1305_suffix
        nonce_sfx = bytes(range(24))
        dgram_sfx = header + sbox.encrypt(payload, nonce_sfx).ciphertext + nonce_sfx
        p3 = TransportDecryptor("xsalsa20_poly1305_suffix", secret).decrypt_rtp(dgram_sfx)
        assert p3 is not None
        self.assertEqual((p3.sequence, p3.rtp_timestamp, p3.ssrc, p3.payload), (42, 9600, 123456, payload))

        # 4. xsalsa20_poly1305
        nonce_std = bytearray(24)
        nonce_std[:12] = header
        dgram_std = header + sbox.encrypt(payload, bytes(nonce_std)).ciphertext
        p4 = TransportDecryptor("xsalsa20_poly1305", secret).decrypt_rtp(dgram_std)
        assert p4 is not None
        self.assertEqual((p4.sequence, p4.rtp_timestamp, p4.ssrc, p4.payload), (42, 9600, 123456, payload))

        # Non-Opus / RTCP / IP discovery / truncated frames are ignored cleanly
        self.assertIsNone(dec_aead.decrypt_rtp(b"\x80\xc9" + b"\x00" * 30))  # RTCP 201
        self.assertIsNone(dec_aead.decrypt_rtp(b"\x00\x02" + b"\x00" * 72))  # IP discovery
        self.assertIsNone(dec_aead.decrypt_rtp(b"\x80\x78\x00"))  # truncated

    def test_dave_e2ee_ready_unready_and_bot_filtering_in_router(self) -> None:
        clock = _FakeClock(initial=100.0)
        buf = RollingAudioBuffer(guild_id=1, clock=clock)
        buf.start_session(channel_id=10)
        consent = ConsentManager(mode="notice")

        fake_dave = MagicMock()
        fake_dave.ready = False
        fake_dave.can_passthrough = MagicMock(return_value=False)
        fake_dave.decrypt = MagicMock(side_effect=lambda uid, mt, data: data[4:])

        router = VoiceReceiveRouter(
            guild_id=1,
            buffer=buf,
            consent=consent,
            is_bot_user=lambda uid: uid == 9999,
            dave_state_getter=lambda: (fake_dave, 1),
            clock=clock,
        )
        secret = bytes(range(32))
        router.configure_transport("aead_xchacha20_poly1305_rtpsize", secret, own_ssrc=555)
        router.register_ssrc(user_id=1001, ssrc=111)
        router.register_ssrc(user_id=9999, ssrc=222)  # bot user

        opus_frame = _encode_opus_tone(440.0, 0.02)[0]
        dave_wrapped = b"DAVE" + opus_frame

        # Pre-epoch (dave.ready == False): ciphertext dropped cleanly, not fed to Opus
        router.handle_udp_packet(
            _build_rtpsize_aead_datagram(
                secret_key=secret,
                ssrc=111,
                sequence=1,
                rtp_timestamp=960,
                payload=dave_wrapped,
                nonce_int=1,
            )
        )
        self.assertEqual(router.packets_buffered, 0)

        # Once DAVE session is ready: decrypted cleanly & buffered; bot SSRC ignored
        fake_dave.ready = True
        router.handle_udp_packet(
            _build_rtpsize_aead_datagram(
                secret_key=secret,
                ssrc=111,
                sequence=2,
                rtp_timestamp=1920,
                payload=dave_wrapped,
                nonce_int=2,
            )
        )
        router.handle_udp_packet(
            _build_rtpsize_aead_datagram(
                secret_key=secret,
                ssrc=222,
                sequence=1,
                rtp_timestamp=960,
                payload=dave_wrapped,
                nonce_int=3,
            )
        )
        self.assertEqual(router.packets_buffered, 1)
        self.assertEqual(buf.stats()["active_speakers"], 1)

    async def test_disconnected_bot_oversized_output_export_timeout_and_expired_interaction(self) -> None:
        clock = _FakeClock(initial=1000.0)
        bot = SimpleNamespace(user=SimpleNamespace(id=999), config=Config())
        cog = VoiceClip(bot)  # type: ignore[arg-type]
        cog.service = VoiceClipService(bot, VoiceClipConfig(), clock=clock)  # type: ignore[arg-type]

        # 1. Bot not connected to any VC
        guild_disc, _, _ = _make_guild_with_vc(connected=False)
        interaction_disc, _ = _make_interaction(
            user_id=AUTHORIZED_CLIP_USER_ID, guild=guild_disc
        )
        await cog.clip.callback(cog, interaction_disc, 10)
        interaction_disc.response.send_message.assert_awaited_once()
        self.assertIn("not connected", interaction_disc.response.send_message.call_args.args[0])

        # 2. Connected VC, seed audio, test oversized file rejection
        guild, channel, vc = _make_guild_with_vc()
        cog.service.attach_voice_client(guild=guild, channel_id=channel.id, voice_client=vc)
        buf = cog.service.get_buffer(guild.id)
        assert buf is not None
        for i, pkt in enumerate(_encode_opus_tone(440.0, 1.0)):
            buf.append_packet(
                user_id=10,
                opus_data=pkt,
                rtp_timestamp=i * 960,
                sequence=i,
                ssrc=1,
                mono_time=clock.advance(0.02),
            )
        snap = buf.snapshot(1)

        tiny_limit_exporter = ClipExportPipeline(max_file_bytes=65_536)
        tiny_limit_exporter.max_file_bytes = 500  # smaller than valid M4A
        with self.assertRaises(ClipFileTooLargeError):
            await tiny_limit_exporter.export(snap)
        self.assertEqual(tiny_limit_exporter.active_temp_bytes, 0)
        await tiny_limit_exporter.shutdown()

        # 3. Export timeout kills/cleans cleanly
        timeout_exporter = ClipExportPipeline(export_timeout_seconds=1.0)

        async def _hang(_s):
            await asyncio.sleep(5.0)

        timeout_exporter._decode_mix_and_encode = _hang  # type: ignore[method-assign]
        timeout_exporter.export_timeout_seconds = 0.05
        with self.assertRaises(ClipExportTimeoutError):
            await timeout_exporter.export(snap)
        self.assertEqual(timeout_exporter.inflight_count, 0)
        await timeout_exporter.shutdown()

        # 4. Expired interaction during defer does not raise unhandled error
        expired_inter, dm_chan = _make_interaction(
            user_id=AUTHORIZED_CLIP_USER_ID, guild=guild
        )
        expired_inter.response.defer = AsyncMock(
            side_effect=discord.NotFound(MagicMock(status=404), "Unknown interaction")
        )
        await cog.clip.callback(cog, expired_inter, 5)
        dm_chan.send.assert_not_called()

        # 5. Bot disconnect event via on_voice_state_update purges buffer immediately
        bot_member = SimpleNamespace(id=999, bot=True, guild=guild)
        await cog.on_voice_state_update(
            bot_member,  # type: ignore[arg-type]
            SimpleNamespace(channel=channel, self_deaf=False, deaf=False),  # type: ignore[arg-type]
            SimpleNamespace(channel=None, self_deaf=False, deaf=False),  # type: ignore[arg-type]
        )
        self.assertIsNone(cog.service.get_buffer(guild.id))
        await cog.service.shutdown()


if __name__ == "__main__":
    unittest.main()
