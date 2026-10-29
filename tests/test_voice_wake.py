"""Tests for the spoken wake word (no microphone, no network, no Discord).

The interesting failure modes are all local and all testable:

* the **segmenter** must keep a whole sentence and throw away a cough — the
  boundaries are asserted frame by frame;
* the **phrase matcher** must accept what a speech-to-text engine actually
  returns ("he paradise", "pair of dice", Devanagari) and reject sentences
  that merely contain the word;
* the **listener** must transcribe once per utterance, honour rate limits,
  survive a broken transcriber, and hand exactly one request to the bot;
* the **WAV encoder** must produce something a real decoder accepts.

The end-to-end test feeds Opus-shaped frames through a fake tap with a fake
transcriber, which is as close to "somebody said hey paradise" as a test can
get without a human and a sound card.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config, VoiceWakeConfig, WakeConfig  # noqa: E402
from functions.voice_clip.constants import FRAME_SAMPLES, SAMPLE_RATE  # noqa: E402
from functions.voice_wake.listener import SpokenWakeListener  # noqa: E402
from functions.voice_wake.phrases import find_wake, normalise  # noqa: E402
from functions.voice_wake.segmenter import (  # noqa: E402
    SpeechSegmenter,
    frame_rms,
    trim_silence,
)
from functions.voice_wake.stt import (  # noqa: E402
    ElevenLabsTranscriber,
    SpeechToTextChain,
    SpeechToTextUnavailable,
    decimate,
    downmix,
    to_wav,
    wav_from_bytes,
)

LOUD = (np.sin(np.linspace(0, 2 * np.pi * 8, FRAME_SAMPLES)) * 12000).astype(np.int16)
QUIET = np.zeros(FRAME_SAMPLES, dtype=np.int16)


def frames(count: int, frame=LOUD) -> list[np.ndarray]:
    return [frame.copy() for _ in range(count)]


# ---------------------------------------------------------------------------
# Segmenter
# ---------------------------------------------------------------------------


class TestSegmenter(unittest.TestCase):
    def make(self, **kwargs) -> SpeechSegmenter:
        params = dict(
            threshold=0.012,
            onset_frames=3,
            end_silence_frames=35,
            min_voiced_frames=12,
            max_frames=600,
            preroll_frames=5,
        )
        params.update(kwargs)
        return SpeechSegmenter(**params)

    def test_frame_rms_is_normalised(self):
        self.assertAlmostEqual(frame_rms(QUIET), 0.0, places=6)
        self.assertGreater(frame_rms(LOUD), 0.1)
        self.assertLess(frame_rms(LOUD), 1.0)

    def test_a_sentence_becomes_one_segment(self):
        segmenter = self.make()
        segment = None
        for _ in range(30):  # 600 ms of speech
            segment = segmenter.feed(7, LOUD, now=0.0) or segment
        for _ in range(40):  # 800 ms of silence: the sentence ends
            segment = segmenter.feed(7, QUIET, now=0.0) or segment
        self.assertIsNotNone(segment)
        self.assertEqual(segment.user_id, 7)
        # 600 ms of speech + pre-roll/keep context, not the trailing gap.
        self.assertGreater(segment.duration, 0.5)
        self.assertLess(segment.duration, 1.2)
        self.assertGreater(segment.voiced_seconds, 0.5)

    def test_a_cough_is_discarded(self):
        segmenter = self.make()
        for _ in range(5):  # 100 ms — a door, not a sentence
            segmenter.feed(7, LOUD, now=0.0)
        for _ in range(40):
            segmenter.feed(7, QUIET, now=0.0)
        self.assertEqual(segmenter.closed, 0)
        self.assertEqual(segmenter.discarded, 1)

    def test_noise_floor_never_starts_a_recording(self):
        segmenter = self.make()
        hiss = (np.random.RandomState(0).randn(FRAME_SAMPLES) * 200).astype(np.int16)
        for _ in range(300):
            segmenter.feed(7, hiss, now=0.0)
        self.assertEqual(segmenter.closed, 0)

    def test_a_long_monologue_is_capped(self):
        segmenter = self.make(max_frames=100)  # 2 s
        segment = None
        for _ in range(150):
            segment = segmenter.feed(7, LOUD, now=0.0) or segment
        self.assertIsNotNone(segment)
        self.assertLessEqual(segment.duration, 2.2)

    def test_speakers_do_not_mix(self):
        segmenter = self.make()
        segment = None
        for _ in range(30):
            segmenter.feed(1, LOUD, now=0.0)
            segment = segmenter.feed(2, QUIET, now=0.0) or segment
        for _ in range(40):
            segment = segmenter.feed(1, QUIET, now=0.0) or segment
        self.assertIsNotNone(segment)
        self.assertEqual(segment.user_id, 1)

    def test_flush_closes_a_half_finished_utterance(self):
        segmenter = self.make()
        for _ in range(30):
            segmenter.feed(3, LOUD, now=0.0)
        segment = segmenter.flush(3)
        self.assertIsNotNone(segment)
        self.assertEqual(segment.user_id, 3)
        self.assertIsNone(segmenter.flush(3))

    def test_trim_silence_keeps_the_middle(self):
        samples = np.concatenate([QUIET] * 20 + [LOUD] * 20 + [QUIET] * 20)
        trimmed = trim_silence(samples, threshold=0.012, keep_ms=100)
        self.assertLess(trimmed.size, samples.size)
        self.assertGreater(trimmed.size, 20 * FRAME_SAMPLES)


# ---------------------------------------------------------------------------
# Phrase matching
# ---------------------------------------------------------------------------


class TestPhrases(unittest.TestCase):
    def test_clean_transcripts(self):
        match = find_wake("Hey Paradise, what's the capital of Rajasthan?")
        self.assertTrue(match.matched)
        self.assertEqual(match.question, "what's the capital of rajasthan")
        self.assertFalse(match.bare)

    def test_stt_noise_is_tolerated(self):
        for text in (
            "he paradise what time is it",
            "hay paradise turn it up",
            "hey paradaise skip this",
            "hey pair of dice play something",
            "okay paradise stop",
        ):
            self.assertTrue(find_wake(text).matched, text)

    def test_hindi_and_hinglish(self):
        match = find_wake("अरे पैराडाइज़ आज मौसम कैसा है")
        self.assertTrue(match.matched)
        self.assertIn("मौसम", match.question)
        self.assertTrue(find_wake("हैलो पैराडाइस, खाना क्या बनाऊं").matched)

    def test_bare_wake_word(self):
        match = find_wake("hey paradise")
        self.assertTrue(match.matched)
        self.assertTrue(match.bare)
        self.assertEqual(match.question, "")

    def test_sentences_about_the_bot_do_not_trigger(self):
        for text in (
            "paradise is a nice word",
            "the paradise hotel is lovely",
            "I said hey to paradise yesterday",
            "hey what time is it",
            "",
        ):
            self.assertFalse(find_wake(text).matched, text)

    def test_question_survives_politeness_stripping(self):
        self.assertEqual(
            find_wake("hey paradise can you tell me a joke").question,
            "tell me a joke",
        )
        self.assertEqual(
            find_wake("so hey paradise please play billie jean").question,
            "play billie jean",
        )

    def test_normalise_strips_punctuation_but_keeps_devanagari(self):
        self.assertEqual(normalise("Hey,  Paradise!!"), "hey paradise")
        self.assertEqual(normalise("अरे, पैराडाइज़!"), "अरे पैराडाइज़")


# ---------------------------------------------------------------------------
# WAV encoding
# ---------------------------------------------------------------------------


class TestToWav(unittest.TestCase):
    def test_round_trip_is_a_valid_wav(self):
        pcm = (np.sin(np.linspace(0, 50, SAMPLE_RATE)) * 8000).astype(np.int16)
        wav = to_wav(pcm)
        self.assertEqual(wav[:4], b"RIFF")
        self.assertEqual(wav[8:12], b"WAVE")
        samples, rate = wav_from_bytes(wav)
        self.assertEqual(rate, 16_000)
        self.assertAlmostEqual(samples.size / rate, 1.0, delta=0.05)

    def test_stereo_is_downmixed(self):
        left = np.full(100, 1000, dtype=np.int16)
        right = np.full(100, 3000, dtype=np.int16)
        stereo = np.stack([left, right], axis=1)  # 100 frames × 2 channels
        mono = downmix(stereo)
        self.assertEqual(mono.size, 100)
        self.assertEqual(int(mono[0]), 2000)

    def test_decimation_keeps_the_shape_and_shrinks_the_data(self):
        source = (np.sin(np.linspace(0, 40, 4800)) * 6000).astype(np.int16)
        out = decimate(source, 48_000, 16_000)
        self.assertEqual(out.size, 1600)
        self.assertLessEqual(int(np.abs(out).max()), int(np.abs(source).max()))
        self.assertGreater(int(np.abs(out).max()), 1000)

    def test_empty_input_is_empty_output(self):
        self.assertEqual(to_wav(np.zeros(0, dtype=np.int16))[0:4], b"RIFF")
        self.assertEqual(wav_from_bytes(to_wav(np.zeros(0, dtype=np.int16)))[0].size, 0)


# ---------------------------------------------------------------------------
# Transcription chain
# ---------------------------------------------------------------------------


class FakeTranscriber:
    def __init__(self, name="fake", text="hey paradise what's up", error=None, available=True):
        self.name = name
        self.text = text
        self.error = error
        self.available = available
        self.calls: list[bytes] = []

    async def transcribe(self, wav: bytes, *, language: str = "") -> str:
        self.calls.append(wav)
        if self.error is not None:
            raise self.error
        return self.text


class TestTranscriptionChain(unittest.TestCase):
    def test_first_usable_provider_wins(self):
        first = FakeTranscriber("a", text="")
        second = FakeTranscriber("b", text="hello there")
        chain = SpeechToTextChain(first, second)
        text = asyncio.run(chain.transcribe(b"RIFF0000"))
        self.assertEqual(text, "hello there")
        self.assertEqual(len(first.calls), 1)
        self.assertEqual(len(second.calls), 1)

    def test_a_failing_provider_falls_through(self):
        first = FakeTranscriber("a", error=SpeechToTextUnavailable("down"))
        second = FakeTranscriber("b", text="ok")
        chain = SpeechToTextChain(first, second)
        self.assertEqual(asyncio.run(chain.transcribe(b"RIFF")), "ok")

    def test_everything_failing_returns_nothing(self):
        chain = SpeechToTextChain(
            FakeTranscriber("a", error=RuntimeError("boom")),
            FakeTranscriber("b", available=False),
        )
        self.assertEqual(asyncio.run(chain.transcribe(b"RIFF")), "")
        self.assertIn("a:", chain.last_error)

    def test_unavailable_chain_says_so(self):
        chain = SpeechToTextChain(ElevenLabsTranscriber(""))
        self.assertFalse(chain.available)
        self.assertIn("elevenlabs", chain.describe())
        self.assertIn("no key", chain.describe())


# ---------------------------------------------------------------------------
# Listener, end to end with a fake tap
# ---------------------------------------------------------------------------


class FakeDecoder:
    """Stands in for SpeakerOpusDecoder: turns a marker byte into audio."""

    def __init__(self) -> None:
        self.reset_count = 0

    def reset(self) -> None:
        self.reset_count += 1

    def decode_packet(self, opus: bytes):
        if opus == b"silence":
            return np.zeros(FRAME_SAMPLES, dtype=np.float32)
        return LOUD.astype(np.float32) / 32768.0


class Packet:
    def __init__(self, opus: bytes, epoch: int = 0, mono_time: float = 0.0) -> None:
        self.opus_data = opus
        self.stream_epoch = epoch
        self.mono_time = mono_time


class ListenerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import functions.voice_wake.listener as listener_module

        self._original_decoder = listener_module.SpeakerOpusDecoder
        listener_module.SpeakerOpusDecoder = FakeDecoder
        self.requests = []
        self.wake_only = []
        self.transcriber = FakeTranscriber(text="hey paradise what's the time")
        self.config = VoiceWakeConfig(
            enabled=True,
            stt="auto",
            energy_threshold=0.012,
            silence_ms=200,
            min_seconds=0.2,
            max_seconds=5.0,
            cooldown=0.0,
            max_per_minute=100,
            follow_up_seconds=5.0,
        )
        self.clock = 0.0
        self.listener = SpokenWakeListener(
            stt=SpeechToTextChain(self.transcriber),
            config=self.config,
            on_request=self._on_request,
            on_wake_only=self._on_wake_only,
            clock=lambda: self.clock,
        )
        await self.listener.start()

    async def asyncTearDown(self):
        import functions.voice_wake.listener as listener_module

        listener_module.SpeakerOpusDecoder = self._original_decoder
        await self.listener.stop()

    async def _on_request(self, request):
        self.requests.append(request)

    async def _on_wake_only(self, guild_id, user_id):
        self.wake_only.append((guild_id, user_id))

    def feed(self, opus: bytes, count: int, *, guild=1, user=5) -> None:
        for _ in range(count):
            self.listener.tap(guild, user, Packet(opus))

    async def settle(self, passes: int = 6):
        for _ in range(passes):
            await asyncio.sleep(0.05)

    async def test_saying_hey_paradise_reaches_the_bot(self):
        self.clock = 1000.0
        self.feed(b"voice", 25)      # 500 ms of speech
        self.feed(b"silence", 15)    # 300 ms gap: the sentence ends
        await self.settle()
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(request.guild_id, 1)
        self.assertEqual(request.user_id, 5)
        self.assertEqual(request.question, "what's the time")
        self.assertEqual(self.listener.stats.triggers, 1)
        self.assertEqual(len(self.transcriber.calls), 1)

    async def test_a_transcript_without_the_phrase_is_ignored(self):
        self.transcriber.text = "so anyway I was walking to the shop"
        self.feed(b"voice", 25)
        self.feed(b"silence", 15)
        await self.settle()
        self.assertEqual(self.requests, [])
        self.assertEqual(self.listener.stats.transcripts, 1)

    async def test_a_bare_wake_word_asks_for_the_question(self):
        self.transcriber.text = "hey paradise"
        self.feed(b"voice", 25)
        self.feed(b"silence", 15)
        await self.settle()
        self.assertEqual(self.wake_only, [(1, 5)])
        self.assertEqual(self.requests, [])

        # …and the next utterance is treated as the question.
        self.transcriber.text = "what's the capital of rajasthan"
        self.clock += 1.0
        self.feed(b"voice", 25)
        self.feed(b"silence", 15)
        await self.settle()
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(self.requests[0].followed_up)

    async def test_a_broken_transcriber_does_not_stop_the_listener(self):
        self.transcriber.error = RuntimeError("provider on fire")
        self.feed(b"voice", 25)
        self.feed(b"silence", 15)
        await self.settle()
        self.assertEqual(self.requests, [])

        self.transcriber.error = None
        self.feed(b"voice", 25)
        self.feed(b"silence", 15)
        await self.settle()
        self.assertEqual(len(self.requests), 1)

    async def test_rate_limits_protect_the_bill(self):
        self.listener.config = VoiceWakeConfig(
            enabled=True, silence_ms=200, min_seconds=0.2, cooldown=30.0,
            max_per_minute=100,
        )
        self.listener.segmenter.end_silence_frames = 10
        self.clock = 1000.0

        await self.say_something()          # accepted
        self.clock += 1.0
        await self.say_something()          # inside the 30 s cooldown
        self.clock += 60.0
        await self.say_something()          # accepted again

        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.listener.stats.dropped, 1)

    async def say_something(self, transcript: str | None = None) -> None:
        if transcript is not None:
            self.transcriber.text = transcript
        self.feed(b"voice", 25)
        self.feed(b"silence", 15)
        await self.settle()

    async def test_silence_alone_never_transcribes(self):
        self.feed(b"silence", 200)
        await self.settle()
        self.assertEqual(self.transcriber.calls, [])
        self.assertEqual(self.listener.stats.segments, 0)

    async def test_decoder_state_resets_across_stream_epochs(self):
        self.listener.tap(1, 5, Packet(b"voice", epoch=1))
        await self.settle(2)
        self.listener.tap(1, 5, Packet(b"voice", epoch=2))
        await self.settle(2)
        decoder = self.listener._decoders[(1, 5)]
        self.assertGreaterEqual(decoder.reset_count, 1)

    async def test_paused_guilds_are_skipped_by_the_cog_tap(self):
        # The cog drops packets before they reach the listener; this asserts
        # the drop happens *before* any transcription cost.
        self.listener.tap(9, 5, Packet(b"voice"))
        await self.settle(2)
        self.assertEqual(self.transcriber.calls, [])


# ---------------------------------------------------------------------------
# Real Opus packets (the fixture path a live channel would take)
# ---------------------------------------------------------------------------


def _opus_tone(seconds: float, *, amplitude: float = 0.35, freq: float = 300.0):
    """Encode a sine into real 20 ms Discord Opus packets, or return []."""
    from functions.voice_clip.opus_codec import ensure_opus_loaded

    if not ensure_opus_loaded():
        return []
    try:
        import discord

        encoder = discord.opus.Encoder()
    except Exception:  # noqa: BLE001 - libopus is optional on some hosts
        return []
    packets: list[bytes] = []
    cursor = 0
    for _ in range(max(1, int(round(seconds / 0.02)))):
        idx = np.arange(cursor, cursor + 960, dtype=np.float64)
        mono = (amplitude * 32767.0) * np.sin(
            2.0 * np.pi * freq * idx / SAMPLE_RATE
        )
        s16 = np.clip(mono, -32768, 32767).astype(np.int16)
        stereo = np.column_stack((s16, s16)).reshape(-1)
        packets.append(encoder.encode(stereo.tobytes(), 960))
        cursor += 960
    return packets


class TestRealOpusPath(unittest.TestCase):
    """decode → endpoint → WAV, using the same packets a channel delivers."""

    def test_a_spoken_sentence_becomes_a_sendable_wav(self):
        speech = _opus_tone(0.6)
        if not speech:
            self.skipTest("libopus is not available to encode a fixture")
        silence = _opus_tone(0.4, amplitude=0.0)

        from functions.voice_clip.opus_codec import SpeakerOpusDecoder

        decoder = SpeakerOpusDecoder()
        segmenter = SpeechSegmenter(
            threshold=0.012,
            onset_frames=3,
            end_silence_frames=15,
            min_voiced_frames=6,
            max_frames=600,
            preroll_frames=5,
        )
        segment = None
        for packet in speech + silence:
            samples = decoder.decode_packet(packet)
            if samples is None:
                continue
            pcm = np.clip(samples * 32767, -32768, 32767).astype(np.int16)
            segment = segmenter.feed(1, pcm, now=0.0) or segment

        self.assertIsNotNone(segment)
        self.assertAlmostEqual(segment.duration, 0.6, delta=0.25)
        wav = to_wav(segment.samples)
        samples, rate = wav_from_bytes(wav)
        self.assertEqual(rate, 16_000)
        self.assertGreater(samples.size, 0)
        # 600 ms of 16 kHz mono is ~19 KB; anything much larger means the
        # decimation silently stopped working.
        self.assertLess(len(wav), 40_000)


# ---------------------------------------------------------------------------
# The receiver tap: real decryption, real buffer, real observer
# ---------------------------------------------------------------------------


class TestReceiverTap(unittest.TestCase):
    """A packet that reaches the clip buffer must also reach the listener."""

    def _datagram(self, secret, ssrc, sequence, payload):
        import nacl.secret

        box = nacl.secret.Aead(secret)
        fixed = struct.pack(">BBHII", 0x80, 0x78, sequence & 0xFFFF, sequence * 960, ssrc)
        nonce = bytearray(24)
        nonce[:4] = struct.pack(">I", sequence & 0xFFFFFFFF)
        ciphertext = box.encrypt(payload, fixed, bytes(nonce)).ciphertext
        return fixed + ciphertext + bytes(nonce[:4])

    def test_tap_receives_the_buffered_packet(self):
        from functions.voice_clip.buffer import RollingAudioBuffer
        from functions.voice_clip.consent import ConsentManager
        from functions.voice_clip.receiver import VoiceReceiveRouter

        buffer = RollingAudioBuffer(guild_id=1)
        buffer.start_session(channel_id=2)
        seen = []
        router = VoiceReceiveRouter(
            guild_id=1,
            buffer=buffer,
            consent=ConsentManager(mode="notice"),
            taps=[lambda user_id, packet: seen.append((user_id, packet))],
        )
        secret = bytes(range(32))
        router.configure_transport("aead_xchacha20_poly1305_rtpsize", secret, own_ssrc=555)
        router.register_ssrc(user_id=1001, ssrc=111)

        payload = b"\xf8\xff\xfe"  # any opus-shaped blob
        router.handle_udp_packet(self._datagram(secret, 111, 5, payload))

        self.assertEqual(len(seen), 1)
        user_id, packet = seen[0]
        self.assertEqual(user_id, 1001)
        self.assertEqual(packet.opus_data, payload)
        self.assertEqual(packet.sequence, 5)

    def test_a_breaking_observer_does_not_break_audio(self):
        from functions.voice_clip.buffer import RollingAudioBuffer
        from functions.voice_clip.consent import ConsentManager
        from functions.voice_clip.receiver import VoiceReceiveRouter

        buffer = RollingAudioBuffer(guild_id=1)
        buffer.start_session(channel_id=2)
        called = []

        def explode(user_id, packet):
            raise RuntimeError("observer failed")

        router = VoiceReceiveRouter(
            guild_id=1,
            buffer=buffer,
            consent=ConsentManager(mode="notice"),
            taps=[explode, lambda user_id, packet: called.append(user_id)],
        )
        secret = bytes(range(32))
        router.configure_transport("aead_xchacha20_poly1305_rtpsize", secret, own_ssrc=555)
        router.register_ssrc(user_id=1001, ssrc=111)
        router.handle_udp_packet(self._datagram(secret, 111, 1, b"\xf8\xff\xfe"))
        # The second tap still ran, and the packet was still buffered.
        self.assertEqual(called, [1001])
        self.assertEqual(router.packets_buffered, 1)

    def test_a_muted_opt_out_user_never_reaches_the_tap(self):
        from functions.voice_clip.buffer import RollingAudioBuffer
        from functions.voice_clip.consent import ConsentManager
        from functions.voice_clip.receiver import VoiceReceiveRouter

        buffer = RollingAudioBuffer(guild_id=1)
        buffer.start_session(channel_id=2)
        consent = ConsentManager(mode="notice")
        consent.revoke_user(1, 1001)
        seen = []
        router = VoiceReceiveRouter(
            guild_id=1,
            buffer=buffer,
            consent=consent,
            taps=[lambda user_id, packet: seen.append(user_id)],
        )
        secret = bytes(range(32))
        router.configure_transport("aead_xchacha20_poly1305_rtpsize", secret, own_ssrc=555)
        router.register_ssrc(user_id=1001, ssrc=111)
        router.handle_udp_packet(self._datagram(secret, 111, 1, b"\xf8\xff\xfe"))
        self.assertEqual(seen, [])


# ---------------------------------------------------------------------------
# Config plumbing
# ---------------------------------------------------------------------------


class TestVoiceWakeConfig(unittest.TestCase):
    def test_defaults_listen(self):
        config = VoiceWakeConfig()
        self.assertTrue(config.enabled)
        self.assertTrue(config.listening)

    def test_off_switches_everything_off(self):
        self.assertFalse(VoiceWakeConfig.from_env({"VOICE_WAKE_ENABLED": "false"}).listening)
        self.assertFalse(VoiceWakeConfig.from_env({"VOICE_WAKE_STT": "off"}).listening)

    def test_values_are_clamped(self):
        config = VoiceWakeConfig.from_env(
            {
                "VOICE_WAKE_THRESHOLD": "9",
                "VOICE_WAKE_SILENCE_MS": "999999",
                "VOICE_WAKE_MIN_SECONDS": "0",
                "VOICE_WAKE_MAX_PER_MINUTE": "0",
            }
        )
        self.assertLessEqual(config.energy_threshold, 0.2)
        self.assertLessEqual(config.silence_ms, 3000)
        self.assertGreaterEqual(config.min_seconds, 0.2)
        self.assertGreaterEqual(config.max_per_minute, 1)

    def test_nonsense_falls_back(self):
        config = VoiceWakeConfig.from_env(
            {"VOICE_WAKE_THRESHOLD": "loud", "VOICE_WAKE_STT": "telepathy"}
        )
        self.assertAlmostEqual(config.energy_threshold, 0.012)
        self.assertEqual(config.stt, "auto")


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ---------------------------------------------------------------------------
# Cog integration: tap wiring, spoken request handling, announcements
# ---------------------------------------------------------------------------


class FakeClipService:
    """Just enough of VoiceClipService for the wake cog."""

    def __init__(self) -> None:
        self.taps: list = []

    def add_tap(self, tap) -> None:
        self.taps.append(tap)

    def remove_tap(self, tap) -> None:
        if tap in self.taps:
            self.taps.remove(tap)


class CogTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock

        from commands.wake import Wake

        self.service = FakeClipService()
        eleven = SimpleNamespace(api_key="xi-test-key")
        config = Config(
            elevenlabs=eleven,
            wake=WakeConfig(speak=False),
            voice_wake=VoiceWakeConfig(enabled=True, announce=True, post_text=True),
        )
        gemini = SimpleNamespace(available=False)
        self.ai = SimpleNamespace(
            answer_quick=AsyncMock(return_value="Jaipur is the capital."),
            gemini=gemini,
        )
        self.music_answers: list[str] = []
        music = SimpleNamespace(
            handle_spoken_request=self._music_answer,
        )
        import discord

        self.channel = MagicMock(spec=discord.VoiceChannel)
        self.channel.id = 6
        self.channel.send = AsyncMock()
        self.guild = SimpleNamespace(
            id=5,
            name="Test Server",
            voice_client=SimpleNamespace(channel=self.channel),
            get_member=lambda uid: SimpleNamespace(
                id=uid, display_name="Tester", voice=None, guild=self.guild
            ),
        )
        self.bot = SimpleNamespace(
            user=SimpleNamespace(id=1),
            ai=self.ai,
            guilds=[],
            config=config,
            voice_clip_service=self.service,
            get_cog=lambda name: music if name == "Music" else None,
            get_guild=lambda gid: self.guild if gid == 5 else None,
        )
        self.cog = Wake(self.bot)
        self.posted: list[str] = []
        self.spoken: list[str] = []
        self.cog._post_text = self._post_text
        self.cog._speak_text = self._speak_text
        await self.cog.cog_load()

    async def asyncTearDown(self):
        await self.cog.cog_unload()

    async def _music_answer(self, *, guild, member, channel, text):
        self.music_answers.append(text)
        return "playing **Billie Jean**." if text.startswith("play") else None

    async def _post_text(self, channel, text):
        self.posted.append(text)

    async def _speak_text(self, guild, member, channel, answer, *, failed):
        self.spoken.append(answer)

    async def test_the_receiver_tap_is_registered_and_removed(self):
        self.assertEqual(len(self.service.taps), 1)
        self.assertIsNotNone(self.cog.listener)

    async def test_a_spoken_question_is_answered_and_spoken(self):
        from functions.voice_wake import SpokenRequest, WakeMatch

        await self.cog._on_spoken_request(
            SpokenRequest(
                guild_id=5,
                user_id=7,
                question="what's the capital of rajasthan",
                transcript="hey paradise what's the capital of rajasthan",
                match=WakeMatch(True, "what's the capital of rajasthan"),
            )
        )
        self.ai.answer_quick.assert_awaited_once()
        self.assertEqual(self.posted, ["Jaipur is the capital."])
        self.assertEqual(self.spoken, ["Jaipur is the capital."])

    async def test_a_spoken_music_request_never_reaches_gemini(self):
        from functions.voice_wake import SpokenRequest, WakeMatch

        await self.cog._on_spoken_request(
            SpokenRequest(
                guild_id=5,
                user_id=7,
                question="play billie jean",
                transcript="hey paradise play billie jean",
                match=WakeMatch(True, "play billie jean"),
            )
        )
        self.assertEqual(self.music_answers, ["play billie jean"])
        self.ai.answer_quick.assert_not_awaited()
        self.assertEqual(self.spoken, ["playing **Billie Jean**."])

    async def test_paused_guilds_drop_packets_before_the_listener(self):
        self.cog._paused_guilds.add(5)
        before = len(self.cog.listener._queue)
        self.cog._on_voice_packet(5, 7, Packet(b"voice"))
        self.assertEqual(len(self.cog.listener._queue), before)
        self.cog._on_voice_packet(6, 7, Packet(b"voice"))
        self.assertEqual(len(self.cog.listener._queue), before + 1)

    async def test_the_announcement_is_posted_once_per_guild(self):
        from types import SimpleNamespace

        guild = SimpleNamespace(id=5)
        channel = SimpleNamespace(id=6)
        member = SimpleNamespace(id=1, guild=guild)
        after = SimpleNamespace(channel=channel)
        await self.cog.on_voice_state_update(member, None, after)
        await self.cog.on_voice_state_update(member, None, after)
        self.assertEqual(len(self.posted), 1)
        self.assertIn("hey paradise", self.posted[0].lower())

    async def test_voicewake_commands_are_registered(self):
        names = {command.name for command in self.cog.voicewake.commands}
        self.assertEqual(names, {"status", "stop", "start", "notice", "download"})


# ---------------------------------------------------------------------------
# On-device speech-to-text (faster-whisper): no key, no network, no upload
# ---------------------------------------------------------------------------


class FakeWhisperModel:
    """The two attributes of a WhisperModel the transcriber actually uses."""

    def __init__(self, text="hey paradise what's the time", delay=0.0, segments=None):
        self.text = text
        self.delay = delay
        self.segments = segments
        self.calls: list = []

    def transcribe(self, audio, **options):
        self.calls.append((np.asarray(audio), options, threading.current_thread().name))
        if self.delay:
            time.sleep(self.delay)
        pieces = self.segments if self.segments is not None else [self.text]
        info = SimpleNamespace(
            language=options.get("language") or "en", language_probability=0.98
        )
        return iter([SimpleNamespace(text=piece) for piece in pieces]), info


class LocalSTTTest(unittest.IsolatedAsyncioTestCase):
    def wav(self, seconds: float = 0.5, rate: int = 16000) -> bytes:
        count = int(rate * seconds)
        tone = (np.sin(np.linspace(0, 2 * np.pi * 8, count)) * 9000).astype(np.int16)
        return to_wav(tone, rate=rate, target_rate=rate)

    def build(self, model: FakeWhisperModel | None = None, **kwargs):
        from functions.voice_wake.local_stt import LocalWhisperTranscriber

        model = model or FakeWhisperModel()
        loads = []

        def loader():
            loads.append(model)
            return model

        transcriber = LocalWhisperTranscriber(loader=loader, **kwargs)
        return transcriber, model, loads

    async def test_a_sentence_comes_back_as_text(self):
        transcriber, model, loads = self.build()
        self.assertTrue(transcriber.available)
        text = await transcriber.transcribe(self.wav(), language="hi")
        self.assertEqual(text, "hey paradise what's the time")
        self.assertEqual(len(loads), 1)
        self.assertTrue(transcriber.loaded)
        self.assertGreater(transcriber.last_audio_seconds, 0.4)

    async def test_audio_reaches_the_model_as_16k_float32(self):
        transcriber, model, _ = self.build()
        await transcriber.transcribe(self.wav(seconds=0.5, rate=16000))
        audio, options, _thread = model.calls[0]
        self.assertEqual(audio.dtype, np.float32)
        self.assertAlmostEqual(audio.size, 8000, delta=16)
        self.assertLessEqual(float(np.abs(audio).max()), 1.0)
        self.assertGreater(float(np.abs(audio).max()), 0.1)

    async def test_the_decoder_is_steered_toward_the_wake_phrase(self):
        transcriber, model, _ = self.build()
        await transcriber.transcribe(self.wav(), language="hi")
        _audio, options, _thread = model.calls[0]
        self.assertIn("hey paradise", options["initial_prompt"].lower())
        self.assertEqual(options["language"], "hi")
        self.assertFalse(options["condition_on_previous_text"])
        self.assertFalse(options["vad_filter"])

    async def test_inference_runs_off_the_event_loop(self):
        transcriber, model, _ = self.build(FakeWhisperModel(delay=0.05))
        started = time.monotonic()
        await asyncio.gather(transcriber.transcribe(self.wav()), asyncio.sleep(0.01))
        self.assertGreaterEqual(time.monotonic() - started, 0.05)
        _audio, _options, thread = model.calls[0]
        self.assertNotEqual(thread, threading.current_thread().name)
        self.assertTrue(thread.startswith("paradise-whisper"))

    async def test_the_model_is_loaded_once(self):
        transcriber, _model, loads = self.build()
        await transcriber.transcribe(self.wav())
        await transcriber.transcribe(self.wav())
        self.assertEqual(len(loads), 1)

    async def test_segments_are_joined_into_one_line(self):
        transcriber, _model, _ = self.build(
            FakeWhisperModel(segments=["hey paradise,", " play", " kesariya\n"])
        )
        self.assertEqual(await transcriber.transcribe(self.wav()), "hey paradise, play kesariya")

    async def test_silence_is_never_transcribed(self):
        transcriber, _model, loads = self.build()
        self.assertEqual(await transcriber.transcribe(b""), "")
        self.assertEqual(await transcriber.transcribe(to_wav(np.zeros(0, dtype=np.int16))), "")
        self.assertEqual(loads, [])

    async def test_a_48k_utterance_is_decimated(self):
        transcriber, model, _ = self.build()
        await transcriber.transcribe(self.wav(seconds=1.0, rate=48000))
        audio, _options, _thread = model.calls[0]
        self.assertAlmostEqual(audio.size, 16000, delta=64)

    async def test_a_broken_model_is_explained_and_not_retried_every_time(self):
        from functions.voice_wake.local_stt import LOAD_RETRY_SECONDS

        now = [1000.0]
        attempts = []

        def loader():
            attempts.append(now[0])
            raise ConnectionError("no route to host")

        from functions.voice_wake.local_stt import LocalWhisperTranscriber

        transcriber = LocalWhisperTranscriber(loader=loader, clock=lambda: now[0])
        with self.assertRaises(SpeechToTextUnavailable) as caught:
            await transcriber.transcribe(self.wav())
        self.assertIn("VOICE_WAKE_STT_MODEL_DIR", str(caught.exception))
        self.assertIn("internet", str(caught.exception))
        with self.assertRaises(SpeechToTextUnavailable):
            await transcriber.transcribe(self.wav())
        self.assertEqual(len(attempts), 1, "a failed load must not retry immediately")
        now[0] += LOAD_RETRY_SECONDS + 1
        with self.assertRaises(SpeechToTextUnavailable):
            await transcriber.transcribe(self.wav())
        self.assertEqual(len(attempts), 2, "after the cooldown it may try again")

    async def test_the_chain_falls_back_to_the_cloud_when_local_fails(self):
        def broken():
            raise RuntimeError("ctranslate2 could not open the model")

        from functions.voice_wake.local_stt import LocalWhisperTranscriber

        local = LocalWhisperTranscriber(loader=broken)
        chain = SpeechToTextChain(
            local, FakeTranscriber(text="hey paradise skip this song")
        )
        self.assertEqual(
            await chain.transcribe(self.wav()), "hey paradise skip this song"
        )
        self.assertIn("VOICE_WAKE_STT_MODEL_DIR", local.load_error)

    async def test_idle_unload_frees_the_model(self):
        transcriber, _model, loads = self.build(unload_after=0.01)
        await transcriber.transcribe(self.wav())
        self.assertTrue(transcriber.loaded)
        await asyncio.sleep(0.08)
        self.assertFalse(transcriber.loaded, "the weights should be dropped when idle")
        await transcriber.transcribe(self.wav())
        self.assertEqual(len(loads), 2, "and loaded again on the next sentence")

    async def test_a_stuck_decode_times_out(self):
        transcriber, _model, _ = self.build(FakeWhisperModel(delay=0.4))
        transcriber.timeout = 0.05
        with self.assertRaises(SpeechToTextUnavailable):
            await transcriber.transcribe(self.wav())

    async def test_missing_dependency_is_reported_not_crashed(self):
        from unittest.mock import patch

        from functions.voice_wake.local_stt import (
            INSTALL_HINT,
            LocalWhisperTranscriber,
        )

        with patch("importlib.util.find_spec", return_value=None):
            transcriber = LocalWhisperTranscriber()
            self.assertFalse(transcriber.installed)
            self.assertFalse(transcriber.available)
            self.assertIn("not installed", transcriber.describe())
            with self.assertRaises(SpeechToTextUnavailable) as caught:
                await transcriber.transcribe(self.wav())
            self.assertIn(INSTALL_HINT, str(caught.exception))

    def test_describe_names_the_model_and_precision(self):
        from functions.voice_wake.local_stt import LocalWhisperTranscriber

        transcriber = LocalWhisperTranscriber(loader=lambda: None, model="small")
        self.assertEqual(transcriber.name, "local")
        self.assertIn("whisper small", transcriber.describe())
        self.assertIn("int8", transcriber.describe())

    async def test_close_releases_the_thread(self):
        transcriber, _model, _ = self.build()
        await transcriber.transcribe(self.wav())
        await transcriber.close()
        self.assertFalse(transcriber.available)
        self.assertFalse(transcriber.loaded)
        with self.assertRaises(SpeechToTextUnavailable):
            await transcriber.transcribe(self.wav())


class TestLocalSTTConfig(unittest.TestCase):
    def test_defaults_are_on_device_enough(self):
        config = VoiceWakeConfig.from_env({})
        self.assertEqual(config.stt_model, "small")
        self.assertEqual(config.stt_device, "cpu")
        self.assertEqual(config.stt_compute, "int8")
        self.assertFalse(config.stt_offline)
        self.assertEqual(config.stt_unload_after, 900.0)

    def test_a_local_only_chain_is_selectable(self):
        self.assertEqual(
            VoiceWakeConfig.from_env({"VOICE_WAKE_STT": "local"}).stt, "local"
        )

    def test_settings_are_sanitised(self):
        config = VoiceWakeConfig.from_env(
            {
                "VOICE_WAKE_STT_MODEL": "large-v3-turbo",
                "VOICE_WAKE_STT_THREADS": "999",
                "VOICE_WAKE_STT_UNLOAD_AFTER": "-5",
                "VOICE_WAKE_STT_OFFLINE": "1",
            }
        )
        self.assertEqual(config.stt_model, "large-v3-turbo")
        self.assertLessEqual(config.stt_threads, 64)
        self.assertEqual(config.stt_unload_after, 0.0)
        self.assertTrue(config.stt_offline)


@unittest.skipUnless(
    os.environ.get("VOICE_WAKE_STT_MODEL_DIR"),
    "set VOICE_WAKE_STT_MODEL_DIR to a real faster-whisper model to run Whisper itself",
)
class TestRealWhisper(unittest.IsolatedAsyncioTestCase):
    """Opt-in: real weights, real decode. `VOICE_WAKE_STT_MODEL_DIR=/models/small`."""

    async def test_a_synthesised_sentence_is_transcribed(self):
        from functions.voice_wake.local_stt import LocalWhisperTranscriber

        model_dir = os.environ["VOICE_WAKE_STT_MODEL_DIR"]
        transcriber = LocalWhisperTranscriber(
            model_dir=model_dir, offline=True, unload_after=0.0
        )
        text = await transcriber.transcribe(self.wav())
        self.assertIsInstance(text, str)

    def wav(self, seconds: float = 1.0) -> bytes:
        count = int(16000 * seconds)
        tone = (np.sin(np.linspace(0, 2 * np.pi * 220, count)) * 9000).astype(np.int16)
        return to_wav(tone, rate=16000, target_rate=16000)


class TestProviderOrder(unittest.IsolatedAsyncioTestCase):
    """`VOICE_WAKE_STT` decides who recognises speech, and local comes first."""

    async def asyncSetUp(self):
        from types import SimpleNamespace as NS

        from commands.wake import Wake

        self.bot = NS(
            user=NS(id=1),
            ai=NS(gemini=NS(available=True)),
            guilds=[],
            config=Config(
                elevenlabs=NS(api_key="xi-key"),
                wake=WakeConfig(speak=False),
                voice_wake=VoiceWakeConfig(enabled=False),
            ),
            voice_clip_service=NS(add_tap=lambda fn: None, remove_tap=lambda fn: None),
            get_cog=lambda name: None,
            get_guild=lambda gid: None,
        )
        self.cog = Wake(self.bot)

    def names(self, choice: str) -> list[str]:
        return [provider.name for provider in self.cog._stt_providers(choice)]

    def test_auto_tries_the_machine_before_the_internet(self):
        self.assertEqual(self.names("auto"), ["local", "elevenlabs", "gemini"])
        # Kept, so /voicewake can report on the model files after the fact.
        self.assertIsNotNone(self.cog.local_stt)
        self.assertEqual(self.cog.local_stt.name, "local")

    def test_local_only_means_local_only(self):
        self.assertEqual(self.names("local"), ["local"])

    def test_the_cloud_is_still_reachable_on_request(self):
        self.assertEqual(self.names("elevenlabs"), ["elevenlabs"])
        self.assertEqual(self.names("gemini"), ["gemini"])
        self.assertIsNone(self.cog.local_stt, "no local engine means no model to fetch")

    def test_local_settings_reach_the_transcriber(self):
        from types import SimpleNamespace as NS

        from commands.wake import Wake
        from functions.voice_wake.local_stt import LocalWhisperTranscriber

        bot = NS(
            user=self.bot.user,
            ai=self.bot.ai,
            guilds=[],
            config=Config(
                elevenlabs=NS(api_key="xi-key"),
                wake=WakeConfig(speak=False),
                voice_wake=VoiceWakeConfig(
                    enabled=True,
                    stt_model="base",
                    stt_compute="float32",
                    stt_offline=True,
                    stt_unload_after=0.0,
                ),
            ),
            voice_clip_service=self.bot.voice_clip_service,
        )
        cog = Wake(bot)
        (provider,) = cog._stt_providers("local")
        self.assertIsInstance(provider, LocalWhisperTranscriber)
        self.assertEqual(provider.model, "base")
        self.assertEqual(provider.compute_type, "float32")
        self.assertTrue(provider.offline)
        self.assertEqual(provider.unload_after, 0.0)


def _whisper_installed() -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec("faster_whisper") is not None
    except (ImportError, ValueError):  # pragma: no cover - broken install
        return False


@unittest.skipUnless(_whisper_installed(), "faster-whisper is not installed")
class TestRealWhisperAudioContract(unittest.IsolatedAsyncioTestCase):
    """The array we hand over must be what real Whisper code expects.

    ``FeatureExtractor`` is the first thing a real ``WhisperModel`` runs: it
    turns float32 mono audio into the 80-channel log-mel spectrogram the encoder
    consumes. Feeding it proves the dtype, the scale and the 16 kHz assumption
    without needing 75 MB of weights.
    """

    async def test_our_pcm_becomes_a_whisper_spectrogram(self):
        from faster_whisper.feature_extractor import FeatureExtractor

        from functions.voice_wake.local_stt import LocalWhisperTranscriber

        seen = {}

        class Model:
            def transcribe(self, audio, **options):
                features = FeatureExtractor()(audio)
                seen["shape"] = features.shape
                seen["dtype"] = features.dtype
                seen["level"] = float(np.sqrt(np.mean(features**2)))
                seen["options"] = options
                return iter([]), SimpleNamespace(language="en", language_probability=0.9)

        transcriber = LocalWhisperTranscriber(loader=Model)
        tone = (np.sin(np.linspace(0, 2 * np.pi * 220, 32000)) * 9000).astype(np.int16)
        await transcriber.transcribe(to_wav(tone, rate=16000, target_rate=16000), language="hi")

        rows, columns = seen["shape"]
        self.assertEqual(rows, 80, "whisper's mel filterbank has 80 channels")
        # 2 s of audio → 200 frames of 10 ms, plus the padded context window.
        self.assertGreaterEqual(columns, 199)
        self.assertLessEqual(columns, 3200)
        self.assertEqual(seen["dtype"], np.float32)
        self.assertGreater(seen["level"], 0.1)
        self.assertEqual(seen["options"]["language"], "hi")
        self.assertIn("hey paradise", seen["options"]["initial_prompt"].lower())


# ---------------------------------------------------------------------------
# The weights arrive by themselves
# ---------------------------------------------------------------------------


def _fake_snapshot(root, model="small", honour=True) -> str:
    """Create the on-disk layout faster-whisper's cache uses, and return it."""
    snapshot = os.path.join(
        root, f"models--Systran--faster-whisper-{model}", "snapshots", "abc123"
    )
    os.makedirs(snapshot, exist_ok=True)
    if honour:
        with open(os.path.join(snapshot, "model.bin"), "wb") as handle:
            handle.write(b"\x00" * 2048)
        with open(os.path.join(snapshot, "config.json"), "w", encoding="utf-8") as handle:
            handle.write("{}")
    return snapshot


class TestModelFiles(unittest.TestCase):
    """`prepare_model_files` — deciding, fetching, and never raising."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="whisper-cache-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_an_existing_snapshot_is_found_without_the_network(self):
        from functions.voice_wake.local_stt import find_cached_model

        snapshot = _fake_snapshot(self.tmp)
        self.assertEqual(find_cached_model("small", self.tmp), snapshot)

    def test_a_half_written_snapshot_is_not_mistaken_for_a_model(self):
        from functions.voice_wake.local_stt import find_cached_model

        _fake_snapshot(self.tmp, honour=False)
        self.assertEqual(find_cached_model("small", self.tmp), "")

    def test_a_custom_model_id_maps_to_its_own_cache_folder(self):
        from functions.voice_wake.local_stt import _repo_dir_name

        self.assertEqual(
            _repo_dir_name("deepdml/faster-whisper-large-v3-turbo-ct2"),
            "models--deepdml--faster-whisper-large-v3-turbo-ct2",
        )
        self.assertEqual(_repo_dir_name("small"), "models--Systran--faster-whisper-small")

    def test_a_direct_model_directory_is_recognised(self):
        from functions.voice_wake.local_stt import STATUS_READY, download_model_files

        model_dir = os.path.join(self.tmp, "converted-small")
        os.makedirs(model_dir)
        with open(os.path.join(model_dir, "model.bin"), "wb") as handle:
            handle.write(b"\x00" * 16)

        def must_not_download(*args, **kwargs):  # pragma: no cover - asserted below
            raise AssertionError("a directory that holds the model needs no download")

        status, path, _message = download_model_files(
            model_dir, downloader=must_not_download
        )
        self.assertEqual(status, STATUS_READY)
        self.assertEqual(path, model_dir)

    def test_a_cached_model_is_reported_ready_and_not_downloaded_again(self):
        from functions.voice_wake.local_stt import STATUS_READY, download_model_files

        snapshot = _fake_snapshot(self.tmp)
        calls = []

        def downloader(model, **options):  # pragma: no cover - asserted below
            calls.append(model)
            return ""

        status, path, _message = download_model_files(
            "small", model_dir=self.tmp, downloader=downloader
        )
        self.assertEqual(status, STATUS_READY)
        self.assertEqual(path, snapshot)
        self.assertEqual(calls, [])

    def test_a_missing_model_is_downloaded_into_the_configured_directory(self):
        from functions.voice_wake.local_stt import (
            STATUS_DOWNLOADED,
            STATUS_READY,
            download_model_files,
        )

        calls = []

        def downloader(model, **options):
            calls.append((model, options))
            return _fake_snapshot(options["cache_dir"], model)

        status, path, _message = download_model_files(
            "small", model_dir=self.tmp, downloader=downloader
        )
        self.assertEqual(status, STATUS_DOWNLOADED)
        self.assertEqual(calls[0][0], "small")
        self.assertEqual(calls[0][1]["cache_dir"], self.tmp)
        self.assertTrue(path.startswith(self.tmp))
        # …and the second start-up is a no-op, because the files are there now.
        self.assertEqual(
            download_model_files("small", model_dir=self.tmp)[0], STATUS_READY
        )

    def test_offline_never_touches_the_network(self):
        from functions.voice_wake.local_stt import STATUS_OFFLINE, download_model_files

        def must_not_download(*args, **kwargs):  # pragma: no cover - asserted below
            raise AssertionError("offline mode must not fetch anything")

        status, path, message = download_model_files(
            "small", model_dir=self.tmp, offline=True, downloader=must_not_download
        )
        self.assertEqual(status, STATUS_OFFLINE)
        self.assertEqual(path, "")
        self.assertIn("VOICE_WAKE_STT_OFFLINE", message)
        self.assertIn("VOICE_WAKE_STT_MODEL_DIR", message)

    def test_a_dead_network_is_a_sentence_not_an_exception(self):
        from functions.voice_wake.local_stt import STATUS_FAILED, download_model_files

        def downloader(*args, **kwargs):
            raise ConnectionError("getaddrinfo failed")

        status, path, message = download_model_files(
            "small", model_dir=self.tmp, downloader=downloader
        )
        self.assertEqual(status, STATUS_FAILED)
        self.assertEqual(path, "")
        self.assertIn("could not be downloaded", message)
        self.assertIn("VOICE_WAKE_STT_MODEL_DIR", message)

    def test_the_tool_reports_the_installed_dependency(self):
        """`python tools/fetch_whisper_model.py --check` is honest about state."""
        import subprocess

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        result = subprocess.run(
            [
                sys.executable,
                os.path.join(root, "tools", "fetch_whisper_model.py"),
                "small",
                "--dir",
                self.tmp,
                "--check",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        # Nothing cached: 1, "not on this machine". Never a traceback.
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("not on this machine", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

        _fake_snapshot(self.tmp)
        result = subprocess.run(
            [
                sys.executable,
                os.path.join(root, "tools", "fetch_whisper_model.py"),
                "small",
                "--dir",
                self.tmp,
                "--check",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("on disk", result.stdout)


class TestPrepareAtStartup(unittest.IsolatedAsyncioTestCase):
    """`LocalWhisperTranscriber.prepare()` — the start-up fetch."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="whisper-cache-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def transcriber(self, **kwargs):
        from functions.voice_wake.local_stt import LocalWhisperTranscriber

        options = {
            "model": "small",
            "model_dir": self.tmp,
            "unload_after": 0.0,
        }
        options.update(kwargs)
        return LocalWhisperTranscriber(**options)

    async def test_an_injected_model_needs_no_files(self):
        from functions.voice_wake.local_stt import STATUS_READY

        transcriber = self.transcriber(loader=SimpleNamespace(transcribe=None))
        self.assertEqual(await transcriber.prepare(), STATUS_READY)
        self.assertTrue(transcriber.prepared)

    async def test_the_files_are_fetched_on_the_executor_not_the_loop(self):
        from functions.voice_wake.local_stt import STATUS_DOWNLOADED

        seen = {}

        def downloader(model, **options):
            seen["thread"] = threading.current_thread().name
            return _fake_snapshot(options["cache_dir"], model)

        transcriber = self.transcriber(downloader=downloader)
        status = await transcriber.prepare()
        self.assertEqual(status, STATUS_DOWNLOADED)
        self.assertTrue(transcriber.prepared)
        self.assertIn("paradise-whisper", seen["thread"])
        self.assertIn("on disk", transcriber.describe_model())

    async def test_preparing_twice_downloads_once(self):
        from functions.voice_wake.local_stt import STATUS_DOWNLOADED, STATUS_READY

        calls = []

        def downloader(model, **options):
            calls.append(model)
            return _fake_snapshot(options["cache_dir"], model)

        transcriber = self.transcriber(downloader=downloader)
        self.assertEqual(await transcriber.prepare(), STATUS_DOWNLOADED)
        self.assertEqual(await transcriber.prepare(), STATUS_DOWNLOADED)
        self.assertEqual(calls, ["small"])
        # A fresh process finds the files already there and fetches nothing.
        def must_not_download(*args, **kwargs):  # pragma: no cover - asserted below
            raise AssertionError("the files are already on disk")

        self.assertEqual(
            await self.transcriber(downloader=must_not_download).prepare(), STATUS_READY
        )

    async def test_a_missing_dependency_is_reported_not_raised(self):
        from functions.voice_wake.local_stt import STATUS_NOT_INSTALLED

        transcriber = self.transcriber()
        transcriber._installed = False
        self.assertEqual(await transcriber.prepare(), STATUS_NOT_INSTALLED)
        self.assertFalse(transcriber.prepared)
        self.assertIn("not installed", transcriber.describe_model())

    async def test_offline_without_files_says_so(self):
        from functions.voice_wake.local_stt import STATUS_OFFLINE

        def must_not_download(*args, **kwargs):  # pragma: no cover - asserted below
            raise AssertionError("offline mode must not fetch anything")

        transcriber = self.transcriber(offline=True, downloader=must_not_download)
        self.assertEqual(await transcriber.prepare(), STATUS_OFFLINE)
        self.assertIn("OFFLINE=true", transcriber.describe_model())

    async def test_a_failed_download_can_succeed_later(self):
        from functions.voice_wake.local_stt import STATUS_DOWNLOADED, STATUS_FAILED

        attempts = []

        def downloader(model, **options):
            attempts.append(model)
            if len(attempts) == 1:
                raise OSError("network is unreachable")
            return _fake_snapshot(options["cache_dir"], model)

        transcriber = self.transcriber(downloader=downloader)
        self.assertEqual(await transcriber.prepare(), STATUS_FAILED)
        self.assertIn("download failed", transcriber.describe_model())
        # The wake word still works: a later attempt gets the files.
        self.assertEqual(await transcriber.prepare(), STATUS_DOWNLOADED)
        self.assertTrue(transcriber.prepared)

    async def test_a_download_that_hangs_gives_up(self):
        from functions.voice_wake.local_stt import STATUS_FAILED

        def downloader(*args, **kwargs):
            time.sleep(3)
            return ""

        transcriber = self.transcriber(downloader=downloader)
        status = await transcriber.prepare(timeout=1.0)
        self.assertIn(status, {STATUS_FAILED})
        self.assertIn("took too long", transcriber.prepare_message)

    async def test_offline_hands_ctranslate2_a_real_model_directory(self):
        """The download root holds snapshots, not `model.bin`; offline must resolve."""
        from functions.voice_wake import local_stt

        snapshot = _fake_snapshot(self.tmp)
        seen = {}

        class FakeModel:
            def __init__(self, source, **options):
                seen["source"] = source
                seen["options"] = options

        original = local_stt._import_backend
        local_stt._import_backend = lambda: FakeModel
        self.addCleanup(setattr, local_stt, "_import_backend", original)

        model = self.transcriber(offline=True)._build_model()
        self.assertIsInstance(model, FakeModel)
        self.assertEqual(seen["source"], snapshot)
        self.assertTrue(seen["options"]["local_files_only"])
        self.assertEqual(seen["options"]["download_root"], self.tmp)


class TestWakeFetchesTheModelAtStartup(unittest.IsolatedAsyncioTestCase):
    """The cog decides *whether* to fetch, and says what happened."""

    def cog(self, **voice_wake):
        from commands.wake import Wake

        bot = SimpleNamespace(
            user=SimpleNamespace(id=1),
            ai=SimpleNamespace(gemini=SimpleNamespace(available=False)),
            guilds=[],
            config=Config(
                elevenlabs=SimpleNamespace(api_key=""),
                wake=WakeConfig(speak=False),
                voice_wake=VoiceWakeConfig(**{"enabled": True, "stt": "local", **voice_wake}),
            ),
            voice_clip_service=SimpleNamespace(add_tap=lambda fn: None, remove_tap=lambda fn: None),
            get_cog=lambda name: None,
            get_guild=lambda gid: None,
        )
        return Wake(bot)

    def local(self, status="ready"):
        calls = []

        class Local:
            prepare_status = ""
            model_path = ""
            prepare_message = ""

            async def prepare(self, **kwargs):
                calls.append(kwargs)
                self.prepare_status = status
                return status

            def describe_model(self):
                return f"whisper small: {status}"

        return Local(), calls

    async def test_startup_fetches_the_model_in_the_background(self):
        cog = self.cog()
        cog.local_stt, _calls = self.local()
        cog._prepare_local_model()
        self.assertIsNotNone(cog._prepare_task)
        await cog._prepare_task
        self.assertEqual(cog.local_stt.prepare_status, "ready")

    async def test_preload_can_be_switched_off(self):
        cog = self.cog(stt_preload=False)
        cog.local_stt, _calls = self.local()
        cog._prepare_local_model()
        self.assertIsNone(cog._prepare_task)

    async def test_a_disabled_wake_word_fetches_nothing(self):
        cog = self.cog()
        cog.voice_wake = VoiceWakeConfig(enabled=False)
        cog.local_stt, _calls = self.local()
        cog._prepare_local_model()
        self.assertIsNone(cog._prepare_task)

    async def test_the_cloud_only_chain_has_nothing_to_fetch(self):
        cog = self.cog(stt="gemini")
        self.assertEqual([p.name for p in cog._stt_providers("gemini")], ["gemini"])
        self.assertIsNone(cog.local_stt)

    def test_the_config_default_is_on(self):
        self.assertTrue(VoiceWakeConfig.from_env({}).stt_preload)
        self.assertFalse(VoiceWakeConfig.from_env({"VOICE_WAKE_STT_PRELOAD": "0"}).stt_preload)

    async def test_the_command_fetches_on_demand(self):
        cog = self.cog()
        local, calls = self.local(status="downloaded")
        cog.local_stt = local
        interaction = _FakeInteraction()
        await cog.voicewake_download.callback(cog, interaction)
        self.assertTrue(interaction.deferred)
        self.assertEqual(len(calls), 1)
        self.assertIn("downloaded", interaction.last_message)

    async def test_the_command_explains_offline_mode(self):
        cog = self.cog(stt_offline=True)
        local, calls = self.local()
        cog.local_stt = local
        interaction = _FakeInteraction()
        await cog.voicewake_download.callback(cog, interaction)
        self.assertEqual(calls, [])
        self.assertIn("OFFLINE", interaction.last_message)


class _FakeInteraction:
    """Just enough of `discord.Interaction` for the reply paths we use."""

    def __init__(self):
        self.deferred = False
        self.last_message = ""

    @property
    def response(self):
        outer = self

        class Response:
            async def defer(inner, **kwargs):
                outer.deferred = True

            async def send_message(inner, content="", **kwargs):
                outer.last_message = content

        return Response()

    @property
    def followup(self):
        outer = self

        class Followup:
            async def send(inner, content="", **kwargs):
                outer.last_message = content

        return Followup()
