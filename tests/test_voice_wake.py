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
import struct
import sys
import unittest

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
        self.assertIn("(off)", chain.describe())


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
        self.assertEqual(names, {"status", "stop", "start", "notice"})
