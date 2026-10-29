"""Tests for the music engine: parsing, queueing, mixing, ducking, controls.

Audio in Discord cannot be tested here (no token, no voice socket, no CDN),
so the tests attack the parts that decide whether the audio will be *right*:

* the intent parser, in English and Hinglish, including the false positives
  that would otherwise start a song in the middle of a sentence;
* the queue's off-by-one behaviour when a song ends;
* the ducking arithmetic — the song really is multiplied by 0.15 while an
  answer is playing and really returns to 1.0 afterwards;
* the player state machine, driven through a fake voice client, so "skip"
  advances exactly one track and the loop modes keep their promises;
* the wake-word bridge: "Hey Paradise play …" never reaches Gemini.

The one real FFmpeg test decodes a generated tone, which proves the frame
size and the sample rate the mixer depends on.
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config, MusicConfig  # noqa: E402
from functions.music import (  # noqa: E402
    LoopMode,
    MusicError,
    MusicQueue,
    MusicService,
    Track,
    format_duration,
    parse_music_intent,
)
from functions.music.decoder import (  # noqa: E402
    FRAME_BYTES,
    PcmStream,
    decode_to_pcm,
    resolve_ffmpeg,
    silence_frame,
)
from functions.music.mixer import (  # noqa: E402
    FADE_FRAMES,
    GainRamp,
    MusicMixer,
    apply_gain,
    mix_frames,
)
from functions.music.player import NotConnected  # noqa: E402
from functions.music.search import NoResults, YtDlpResolver  # noqa: E402
from functions.music.ui import (  # noqa: E402
    now_playing_embed,
    panel_embed,
    progress_bar,
    queue_embed,
)

LOUD = 16000
QUIET = 8000


def constant_frame(value: int) -> bytes:
    return np.full(FRAME_BYTES // 2, value, dtype="<i2").tobytes()


def peak(frame: bytes) -> int:
    return int(np.abs(np.frombuffer(frame, dtype="<i2")).max()) if frame else 0


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class TestIntentParsing(unittest.TestCase):
    def assertAction(self, text: str, action: str, query: str | None = None) -> None:
        intent = parse_music_intent(text)
        self.assertIsNotNone(intent, text)
        self.assertEqual(intent.action, action, f"{text!r} → {intent}")
        if query is not None:
            self.assertEqual(intent.query, query, text)

    def test_english_play_requests(self):
        self.assertAction("play billie jean", "play", "billie jean")
        self.assertAction("play billy jeans by michael jackson", "play",
                          "billy jeans by michael jackson")
        self.assertAction("put on some nirvana", "play", "nirvana")
        self.assertAction("start arijit singh songs", "play", "arijit singh")

    def test_hinglish_play_requests(self):
        self.assertAction("billie jean baja do", "play", "billie jean")
        self.assertAction("billie jean bajao", "play", "billie jean")
        # A trailing "ke gaane" is grammar, not part of the search.
        self.assertAction("kishore kumar ke gaane bajao", "play", "kishore kumar")

    def test_wake_prefix_is_tolerated(self):
        self.assertAction("Hey Paradise play billie jean", "play", "billie jean")
        self.assertAction("hey paradise, skip", "skip")

    def test_a_link_is_a_play_request(self):
        self.assertAction("play https://youtu.be/dQw4w9WgXcQ", "play",
                          "https://youtu.be/dQw4w9WgXcQ")
        self.assertAction("https://youtu.be/dQw4w9WgXcQ", "play")

    def test_transport_controls(self):
        for text in ("skip", "next", "next song", "agla", "skip this song please",
                     "अगला गाना लगाओ"):
            self.assertAction(text, "skip")
        for text in ("pause", "pause karo", "hold on"):
            self.assertAction(text, "pause")
        for text in ("resume", "chalu karo", "play again", "continue"):
            self.assertAction(text, "resume")
        for text in ("stop", "band karo", "ruko", "stop the music", "bas karo"):
            self.assertAction(text, "stop")
        for text in ("leave vc", "nikal jao", "disconnect"):
            self.assertAction(text, "leave")

    def test_questions_and_queue_readouts(self):
        for text in ("what's playing", "kaun sa song chal raha hai",
                     "kya chal raha hai", "np"):
            self.assertAction(text, "nowplaying")
        for text in ("queue", "what is the queue", "playlist"):
            self.assertAction(text, "queue")

    def test_volume(self):
        intent = parse_music_intent("volume 30")
        self.assertEqual(intent.action, "volume")
        self.assertAlmostEqual(intent.value, 0.30)
        self.assertTrue(intent.absolute)

        intent = parse_music_intent("awaaz thodi kam karo")
        self.assertEqual(intent.action, "volume")
        self.assertLess(intent.value, 0)
        self.assertFalse(intent.absolute)

        intent = parse_music_intent("volume up")
        self.assertGreater(intent.value, 0)

    def test_loop_modes(self):
        self.assertEqual(parse_music_intent("loop").query, "cycle")
        self.assertEqual(parse_music_intent("loop off").query, "off")
        self.assertEqual(parse_music_intent("loop this song").query, "track")

    def test_ordinary_sentences_are_not_music(self):
        for text in (
            "what's the capital of rajasthan",
            "tell me a joke",
            "how do i stop my laptop from overheating",
            "playback is broken on my pc",
            "what does paradise mean",
            "",
        ):
            self.assertIsNone(parse_music_intent(text), text)

    def test_a_song_title_containing_stop_is_still_a_song(self):
        intent = parse_music_intent("play don't stop me now")
        self.assertEqual(intent.action, "play")
        self.assertIn("stop", intent.query)

    def test_play_with_no_song_asks_for_one(self):
        intent = parse_music_intent("play some music")
        self.assertEqual(intent.action, "play")
        self.assertEqual(intent.query, "")


# ---------------------------------------------------------------------------
# Resolver (no network: yt-dlp payloads are passed in as data)
# ---------------------------------------------------------------------------


class TestResolver(unittest.TestCase):
    def setUp(self):
        self.resolver = YtDlpResolver()

    def test_search_entry_becomes_a_track(self):
        entry = {
            "id": "dQw4w9WgXcQ",
            "title": "Billie Jean",
            "duration": 293,
            "webpage_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "thumbnail": "https://i.ytimg.com/vi/x/hq.jpg",
            "extractor_key": "Youtube",
        }
        track = self.resolver._to_track(entry)
        self.assertEqual(track.title, "Billie Jean")
        self.assertEqual(track.duration, 293)
        self.assertEqual(track.source, "Youtube")
        self.assertEqual(track.duration_text, "4:53")

    def test_entries_of_a_playlist_are_unwrapped(self):
        info = {"entries": [{"title": "a", "url": "https://x/1"},
                            {"title": "b", "url": "https://x/2"}]}
        self.assertEqual(len(self.resolver._entries(info)), 2)
        self.assertEqual(len(self.resolver._entries({"title": "solo"})), 1)

    def test_stream_prefers_audio_only_and_the_better_bitrate(self):
        info = {
            "formats": [
                {"url": "video", "acodec": "opus", "vcodec": "vp9", "abr": 160},
                {"url": "low-audio", "acodec": "opus", "vcodec": "none",
                 "ext": "webm", "abr": 48},
                {"url": "high-audio", "acodec": "mp4a", "vcodec": "none",
                 "ext": "m4a", "abr": 129},
                {"url": "silent", "acodec": "none", "vcodec": "av1"},
            ]
        }
        self.assertEqual(self.resolver._pick_stream(info), "high-audio")

    def test_a_live_stream_with_one_url_is_used_as_is(self):
        self.assertEqual(
            self.resolver._pick_stream({"url": "https://live/x.m3u8"}),
            "https://live/x.m3u8",
        )

    def test_missing_title_falls_back_instead_of_crashing(self):
        track = self.resolver._to_track({"id": "abc"})
        self.assertEqual(track.title, "untitled")
        self.assertIn("abc", track.url)


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


def make_track(title: str, duration: int = 200) -> Track:
    return Track(title=title, url=f"https://example.invalid/{title}", duration=duration)


class TestQueue(unittest.TestCase):
    def test_fifo_and_pop(self):
        queue = MusicQueue(10)
        queue.add_many([make_track("a"), make_track("b")])
        self.assertEqual(queue.pop().title, "a")
        self.assertEqual(queue.pop().title, "b")
        self.assertIsNone(queue.pop())

    def test_limit_is_enforced(self):
        queue = MusicQueue(2)
        self.assertEqual(queue.add_many([make_track(str(i)) for i in range(5)]), 2)
        with self.assertRaises(Exception):
            queue.add(make_track("overflow"))

    def test_remove_and_move_use_display_numbers(self):
        queue = MusicQueue(10, [make_track("a"), make_track("b"), make_track("c")])
        self.assertEqual(queue.remove(2).title, "b")
        self.assertEqual([t.title for t in queue], ["a", "c"])
        self.assertTrue(queue.move(1, 2))
        self.assertEqual([t.title for t in queue], ["c", "a"])
        self.assertIsNone(queue.remove(9))

    def test_shuffle_keeps_every_track(self):
        tracks = [make_track(str(i)) for i in range(8)]
        queue = MusicQueue(10, tracks)
        queue.shuffle()
        self.assertEqual(sorted(t.title for t in queue), [t.title for t in tracks])

    def test_total_duration(self):
        queue = MusicQueue(10, [make_track("a", 60), make_track("b", 90)])
        self.assertEqual(queue.total_duration(), 150)


# ---------------------------------------------------------------------------
# Mixing and ducking
# ---------------------------------------------------------------------------


class FakeStream:
    """A deterministic stand-in for an FFmpeg PCM stream."""

    def __init__(self, value: int = LOUD, frames: int = 1000) -> None:
        self.value = value
        self.remaining = frames
        self.closed = False
        self.position = 0.0

    def open(self):
        return self

    def read_frame(self, timeout: float = 1.0) -> bytes:
        if self.remaining <= 0:
            return b""
        self.remaining -= 1
        self.position += 0.02
        return constant_frame(self.value)

    def close(self) -> None:
        self.closed = True


class TestMixing(unittest.TestCase):
    def test_full_gain_is_unchanged(self):
        frame = constant_frame(LOUD)
        self.assertEqual(peak(mix_frames(frame, b"", 1.0)), LOUD)

    def test_duck_applies_exactly_the_configured_gain(self):
        music = constant_frame(LOUD)
        mixed = mix_frames(music, b"", 0.15)
        self.assertAlmostEqual(peak(mixed), int(LOUD * 0.15), delta=2)

    def test_speech_is_added_on_top_of_the_ducked_song(self):
        music = apply_gain(constant_frame(LOUD), 0.15)
        mixed = mix_frames(music, constant_frame(QUIET), 1.0)
        self.assertAlmostEqual(peak(mixed), int(LOUD * 0.15) + QUIET, delta=2)

    def test_loud_speech_and_loud_music_clip_instead_of_wrapping(self):
        mixed = mix_frames(constant_frame(32000), constant_frame(32000), 1.0)
        samples = np.frombuffer(mixed, dtype="<i2")
        self.assertEqual(int(samples.max()), 32767)
        self.assertGreaterEqual(int(samples.min()), -32768)

    def test_ramp_reaches_the_target_in_the_fade_window(self):
        ramp = GainRamp(start=1.0, fade_frames=12)
        ramp.set_target(0.15)
        values = [ramp.step() for _ in range(12)]
        self.assertEqual(values[-1], 0.15)
        self.assertTrue(ramp.settled)
        # It walks down, it does not jump.
        self.assertTrue(0.15 < values[0] < 1.0)


class TestMusicMixer(unittest.TestCase):
    def make_mixer(self, **kwargs) -> MusicMixer:
        return MusicMixer(
            FakeStream(),
            volume=0.5,
            duck_volume=0.15,
            fade_frames=FADE_FRAMES,
            **kwargs,
        )

    def test_every_frame_is_exactly_one_discord_frame(self):
        mixer = self.make_mixer()
        for _ in range(5):
            self.assertEqual(len(mixer.read()), FRAME_BYTES)

    def test_the_song_ducks_while_speaking_and_returns(self):
        mixer = self.make_mixer()
        base = peak(mixer.read())  # 16000 * 0.5 master volume
        self.assertAlmostEqual(base, int(LOUD * 0.5), delta=2)

        # Plenty of speech, so the duck holds for the whole window.
        mixer.push_speech(constant_frame(QUIET) * (FADE_FRAMES + 6))
        ducked = [peak(mixer.read()) for _ in range(FADE_FRAMES + 2)]
        # Last frame of the fade: song at 15 % + speech at full level.
        self.assertAlmostEqual(
            ducked[-1], int(LOUD * 0.15 * 0.5) + QUIET, delta=3
        )
        self.assertLess(ducked[-1], base + QUIET)

        restored = [peak(mixer.read()) for _ in range(FADE_FRAMES + 4)]
        self.assertAlmostEqual(restored[-1], base, delta=2)

    def test_speech_is_consumed_then_the_gain_comes_back(self):
        mixer = self.make_mixer()
        mixer.push_speech(constant_frame(QUIET) * 3)  # three frames worth
        for _ in range(3):
            mixer.read()
        self.assertFalse(mixer.speech_pending)
        for _ in range(FADE_FRAMES + 1):
            mixer.read()
        self.assertFalse(mixer.ducked)
        self.assertTrue(mixer._ramp.settled)

    def test_a_song_that_ends_still_finishes_the_sentence(self):
        mixer = MusicMixer(FakeStream(frames=1), volume=1.0, duck_volume=0.15)
        self.assertNotEqual(mixer.read(), b"")
        mixer.push_speech(constant_frame(QUIET))
        self.assertNotEqual(mixer.read(), b"")  # speech still plays
        self.assertEqual(mixer.read(), b"")  # then silence ends the source

    def test_a_stalled_stream_is_silence_not_the_end(self):
        class Stalling(FakeStream):
            def read_frame(self, timeout: float = 1.0):
                return None

        mixer = MusicMixer(Stalling(), volume=1.0)
        frame = mixer.read()
        self.assertEqual(len(frame), FRAME_BYTES)
        self.assertEqual(peak(frame), 0)

    def test_speech_only_mixer_plays_and_then_stops(self):
        mixer = MusicMixer(None, volume=1.0)
        mixer.push_speech(constant_frame(QUIET) * 2)
        self.assertAlmostEqual(peak(mixer.read()), QUIET, delta=2)
        self.assertGreaterEqual(peak(mixer.read()), 0)
        self.assertEqual(mixer.read(), b"")


# ---------------------------------------------------------------------------
# Decoder (real FFmpeg)
# ---------------------------------------------------------------------------


@unittest.skipIf(resolve_ffmpeg() is None, "ffmpeg is not available")
class TestDecoder(unittest.TestCase):
    def test_decodes_to_48k_stereo_frames(self):
        import subprocess
        import tempfile

        ffmpeg = resolve_ffmpeg()
        with tempfile.TemporaryDirectory() as tmp:
            wav = os.path.join(tmp, "tone.wav")
            subprocess.run(
                [
                    ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
                    "-ac", "2", "-ar", "48000", wav,
                ],
                check=True,
                timeout=30,
            )
            with open(wav, "rb") as handle:
                pcm = decode_to_pcm(handle.read(), executable=ffmpeg)
        self.assertGreater(len(pcm), 0)
        self.assertEqual(len(pcm) % FRAME_BYTES, 0)
        seconds = len(pcm) / (48000 * 2 * 2)
        self.assertAlmostEqual(seconds, 0.5, delta=0.1)

    def test_ducking_on_a_real_stream(self):
        """The whole point, measured: 0.15 while speaking, 1.0 afterwards."""
        import subprocess
        import tempfile

        ffmpeg = resolve_ffmpeg()
        with tempfile.TemporaryDirectory() as tmp:
            song = os.path.join(tmp, "song.wav")
            subprocess.run(
                [
                    ffmpeg, "-nostdin", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=2", "-ac", "2",
                    "-ar", "48000", song,
                ],
                check=True,
                timeout=30,
            )
            mixer = MusicMixer(
                PcmStream(song, executable=ffmpeg).open(),
                volume=0.5,
                duck_volume=0.15,
                fade_frames=FADE_FRAMES,
            )
            try:
                before = peak(mixer.read())
                # Silent speech isolates what the duck does to the *song*.
                mixer.push_speech(silence_frame() * (FADE_FRAMES + 4))
                during = [peak(mixer.read()) for _ in range(FADE_FRAMES + 4)]
                after = [peak(mixer.read()) for _ in range(FADE_FRAMES + 4)]
            finally:
                mixer.cleanup()

        self.assertGreater(before, 0)
        self.assertAlmostEqual(during[-1] / before, 0.15, delta=0.02)
        self.assertAlmostEqual(after[-1] / before, 1.0, delta=0.05)

    def test_stream_reads_frames_then_ends(self):
        import subprocess
        import tempfile

        ffmpeg = resolve_ffmpeg()
        with tempfile.TemporaryDirectory() as tmp:
            wav = os.path.join(tmp, "tone.wav")
            subprocess.run(
                [
                    ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "sine=frequency=220:duration=0.2",
                    "-ac", "2", "-ar", "48000", wav,
                ],
                check=True,
                timeout=30,
            )
            stream = PcmStream(wav, executable=ffmpeg).open()
            try:
                frames = []
                while True:
                    frame = stream.read_frame(timeout=5.0)
                    if frame == b"":
                        break
                    frames.append(frame)
            finally:
                stream.close()
        self.assertGreaterEqual(len(frames), 5)
        self.assertTrue(all(len(frame) == FRAME_BYTES for frame in frames))


# ---------------------------------------------------------------------------
# Player state machine
# ---------------------------------------------------------------------------


class FakeVoiceClient:
    """Mimics the bits of ``discord.VoiceClient`` the player actually uses."""

    def __init__(self, channel=None) -> None:
        self.channel = channel or SimpleNamespace(id=555)
        self.source = None
        self._after = None
        self._paused = False
        self.played: list = []

    def is_connected(self) -> bool:
        return True

    def is_playing(self) -> bool:
        return self.source is not None and not self._paused

    def is_paused(self) -> bool:
        return self.source is not None and self._paused

    def play(self, source, *, after=None) -> None:
        self.source = source
        self._after = after
        self._paused = False
        self.played.append(source)

    def stop(self) -> None:
        after = self._after
        self.source, self._after = None, None
        if after is not None:
            after(None)  # discord does call ``after`` on a manual stop

    def pause(self, value: bool) -> None:
        self._paused = bool(value)

    async def move_to(self, channel) -> None:
        self.channel = channel

    async def disconnect(self, *, force: bool = False) -> None:
        self.source = None

    def finish(self) -> None:
        """Pretend the audio ended on its own."""
        after = self._after
        self.source, self._after = None, None
        if after is not None:
            after(None)


class FakeResolver:
    def __init__(self, tracks=None, fail: Exception | None = None) -> None:
        self.tracks = tracks or [make_track("Billie Jean", 293)]
        self.fail = fail
        self.searches: list[str] = []
        self.available = True

    async def search(self, query, *, requester_id=0, requester_name="", limit=1):
        self.searches.append(query)
        if self.fail is not None:
            raise self.fail
        return [t.with_requester(requester_id, requester_name) for t in self.tracks[:limit]]

    async def stream_url(self, track) -> str:
        return f"https://stream.invalid/{track.title}"

    def describe(self) -> str:
        return "fake resolver"


class PlayerTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = SimpleNamespace(
            config=Config(),
            get_cog=lambda name: None,
            user=SimpleNamespace(id=1),
        )
        self.resolver = FakeResolver()
        self.service = MusicService(
            self.bot,
            config=MusicConfig(idle_disconnect=0),
            resolver=self.resolver,
            stream_factory=lambda url: FakeStream(),
        )
        self.player = self.service.player(99)
        self.voice = FakeVoiceClient()
        self.player._voice = self.voice
        self.player._loop = asyncio.get_running_loop()

    async def settle(self):
        # ``start`` opens the stream in the default executor, so give the
        # worker thread a moment before asserting on the new state.
        for _ in range(10):
            await asyncio.sleep(0.01)

    async def test_play_starts_a_track(self):
        tracks = await self.service.play(
            SimpleNamespace(id=99), SimpleNamespace(id=555), "billie jean",
            requester_id=7, requester_name="Tester",
        )
        self.assertEqual(tracks[0].title, "Billie Jean")
        self.assertEqual(self.player.current.title, "Billie Jean")
        self.assertIsNotNone(self.voice.source)
        self.assertFalse(self.voice.source.is_opus())

    async def test_second_request_is_queued(self):
        await self.service.play(SimpleNamespace(id=99), None, "one")
        self.resolver.tracks = [make_track("Second")]
        await self.service.play(SimpleNamespace(id=99), None, "two")
        self.assertEqual([t.title for t in self.player.queue], ["Second"])

    async def test_finishing_a_track_advances_once(self):
        self.resolver.tracks = [make_track("a"), make_track("b")]
        await self.service.play(SimpleNamespace(id=99), None, "a")
        self.resolver.tracks = [make_track("b")]
        await self.service.play(SimpleNamespace(id=99), None, "b")
        self.voice.finish()
        await self.settle()
        self.assertIsNotNone(self.player.current)
        self.assertEqual(self.player.current.title, "b")
        self.assertEqual(len(self.player.queue), 0)

    async def test_skip_advances_exactly_one(self):
        for name in ("a", "b", "c"):
            self.resolver.tracks = [make_track(name)]
            await self.service.play(SimpleNamespace(id=99), None, name)
        self.assertEqual(self.player.current.title, "a")
        upcoming = await self.player.skip(1)
        self.assertEqual(self.player.current.title, "b")
        self.assertEqual(upcoming.title, "b")
        self.assertEqual([t.title for t in self.player.queue], ["c"])

    async def test_skip_two(self):
        for name in ("a", "b", "c"):
            self.resolver.tracks = [make_track(name)]
            await self.service.play(SimpleNamespace(id=99), None, name)
        await self.player.skip(2)
        self.assertEqual(self.player.current.title, "c")

    async def test_pause_and_resume(self):
        await self.service.play(SimpleNamespace(id=99), None, "a")
        self.assertTrue(self.player.pause())
        self.assertTrue(self.player.paused)
        self.assertFalse(self.player.pause())
        self.assertTrue(self.player.resume())
        self.assertFalse(self.player.paused)

    async def test_stop_clears_the_queue(self):
        for name in ("a", "b"):
            self.resolver.tracks = [make_track(name)]
            await self.service.play(SimpleNamespace(id=99), None, name)
        await self.player.stop()
        self.assertIsNone(self.player.current)
        self.assertEqual(len(self.player.queue), 0)
        self.assertIsNone(self.voice.source)

    async def test_volume_is_clamped(self):
        await self.service.play(SimpleNamespace(id=99), None, "a")
        self.assertAlmostEqual(self.player.set_volume(0.4), 0.4)
        self.assertAlmostEqual(self.player.set_volume(9.0), 2.0)
        self.assertAlmostEqual(self.player.set_volume(-1.0), 0.0)
        self.assertAlmostEqual(self.voice.source.master_volume, 0.0)

    async def test_loop_track_replays_the_same_song(self):
        self.resolver.tracks = [make_track("a")]
        await self.service.play(SimpleNamespace(id=99), None, "a")
        self.player.set_loop(LoopMode.TRACK)
        self.voice.finish()
        await self.settle()
        self.assertEqual(self.player.current.title, "a")

    async def test_loop_queue_recycles_the_finished_track(self):
        self.resolver.tracks = [make_track("a")]
        await self.service.play(SimpleNamespace(id=99), None, "a")
        self.player.set_loop(LoopMode.QUEUE)
        self.voice.finish()
        await self.settle()
        # One song on loop-queue: it goes back on the queue and plays again.
        self.assertEqual(self.player.current.title, "a")
        self.assertEqual(len(self.player.queue), 0)

    async def test_speaking_mixes_into_the_music(self):
        await self.service.play(SimpleNamespace(id=99), None, "a")
        mixer = self.voice.source
        self.assertFalse(mixer.ducked)
        spoken = await self.player.speak(constant_frame(QUIET) * 40)
        self.assertTrue(spoken)
        self.assertTrue(mixer.speech_pending)
        for _ in range(FADE_FRAMES + 2):
            mixer.read()
        self.assertTrue(mixer.ducked)
        self.assertLess(mixer._ramp.value, 0.5)

    async def test_speaking_while_paused_lets_the_answer_through(self):
        await self.service.play(SimpleNamespace(id=99), None, "a")
        self.player.pause()
        self.assertTrue(await self.player.speak(constant_frame(QUIET)))
        self.assertFalse(self.voice._paused)  # temporarily resumed for speech
        mixer = self.voice.source
        while mixer.speech_pending:
            mixer.read()
        await asyncio.sleep(0.25)
        self.assertTrue(self.player.paused)  # and paused again afterwards

    async def test_speaking_without_music_plays_on_its_own(self):
        spoken = await self.player.speak(constant_frame(QUIET) * 2)
        self.assertTrue(spoken)
        self.assertIsNotNone(self.voice.source)
        self.assertIsNone(self.player.current)

    async def test_search_failures_are_friendly(self):
        self.resolver.fail = NoResults("nothing found for that.")
        with self.assertRaises(NoResults):
            await self.service.play(SimpleNamespace(id=99), None, "asdfgh")

    async def test_play_without_a_voice_client(self):
        self.player._voice = None
        with self.assertRaises(NotConnected):
            await self.player.start(make_track("a"))

    async def test_state_snapshot(self):
        self.resolver.tracks = [make_track("a")]
        await self.service.play(SimpleNamespace(id=99), None, "a")
        state = self.player.state
        self.assertEqual(state.current.title, "a")
        self.assertTrue(state.playing)
        self.assertEqual(state.channel_id, 555)
        self.assertEqual(state.volume, 0.5)


# ---------------------------------------------------------------------------
# Embeds
# ---------------------------------------------------------------------------


class TestEmbeds(unittest.TestCase):
    def make_state(self, **kwargs):
        from functions.music import PlayerState

        params = dict(
            guild_id=1,
            channel_id=2,
            current=make_track("Billie Jean", 293),
            position=60.0,
            volume=0.5,
            upcoming=(make_track("b"), make_track("c")),
        )
        params.update(kwargs)
        return PlayerState(**params)

    def test_progress_bar_width_is_stable(self):
        for position in (0, 1, 100, 292, 293, 300):
            self.assertEqual(len(progress_bar(position, 293)), BAR_LEN)
        self.assertIn("live", progress_bar(0, 0))

    def test_now_playing_has_the_essentials(self):
        embed = now_playing_embed(self.make_state())
        self.assertEqual(embed.title, "Billie Jean")
        self.assertIn("1:00", embed.description)
        self.assertIn("50%", embed.fields[0].value)

    def test_empty_states_are_designed_not_blank(self):
        empty = self.make_state(current=None, upcoming=())
        self.assertIn("Nothing playing", now_playing_embed(empty).title)
        self.assertIn("nothing queued", queue_embed(empty).fields[-1].value)
        self.assertIn("nothing is playing", panel_embed(empty).description)

    def test_queue_embed_pages(self):
        tracks = tuple(make_track(f"s{i}") for i in range(25))
        embed = queue_embed(self.make_state(upcoming=tracks), per_page=10)
        self.assertIn("Up next (25)", embed.fields[1].name)
        self.assertIn("page 1/3", embed.footer.text)

    def test_panel_lists_text_commands(self):
        embed = panel_embed(self.make_state(), prefix="!")
        blob = " ".join(field.value for field in embed.fields)
        self.assertIn("!p <song>", blob)
        self.assertIn("Hey Paradise", blob)

    def test_duration_formatting(self):
        self.assertEqual(format_duration(65), "1:05")
        self.assertEqual(format_duration(3725), "1:02:05")
        self.assertEqual(format_duration(0), "live")


BAR_LEN = len("▬" * 14 + "🔘")


# ---------------------------------------------------------------------------
# Cog: text commands, voice bridge, panel
# ---------------------------------------------------------------------------


def make_message(content: str, *, channel=None, in_voice: bool = True):
    if channel is None:
        if in_voice:
            import discord

            channel = MagicMock(spec=discord.VoiceChannel)
            channel.id = 42
            channel.send = AsyncMock()
            channel.permissions_for = MagicMock(
                return_value=SimpleNamespace(
                    send_messages=True, connect=True, speak=True, view_channel=True
                )
            )
        else:
            channel = SimpleNamespace(id=42, send=AsyncMock(), permissions_for=None)
    message = MagicMock()
    message.content = content
    message.channel = channel
    message.author = SimpleNamespace(id=7, bot=False, display_name="Tester", voice=None)
    message.guild = SimpleNamespace(id=99, name="Test", me=SimpleNamespace(id=1))
    message.reply = AsyncMock()
    return message


class TestCog(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from commands.music import Music

        self.bot = SimpleNamespace(
            config=Config(music=MusicConfig(prefix="!", idle_disconnect=0)),
            get_cog=lambda name: None,
            add_view=lambda view: None,
            user=SimpleNamespace(id=1),
        )
        self.cog = Music(self.bot)
        self.cog.service = MusicService(
            self.bot,
            config=self.bot.config.music,
            resolver=FakeResolver(),
            stream_factory=lambda url: FakeStream(),
        )
        self.voice = FakeVoiceClient()
        player = self.cog.service.player(99)
        player._voice = self.voice
        player._loop = asyncio.get_running_loop()

    async def test_unknown_prefixes_are_ignored(self):
        self.assertFalse(await self.cog.handle_message(make_message("hello there")))
        self.assertFalse(await self.cog.handle_message(make_message("!unknown")))

    async def test_prefix_play_starts_music(self):
        message = make_message("!p billie jean")
        self.assertTrue(await self.cog.handle_message(message))
        message.reply.assert_awaited_once()
        self.assertIn("playing", message.reply.await_args.kwargs["content"].lower())

    async def test_prefix_aliases(self):
        for text, action in (("!s", "skip"), ("!np", "nowplaying"), ("!stop", "stop")):
            intent = self.cog._intent_for(self.cog._split_command(text)[0], "")
            self.assertEqual(intent.action, action, text)

    async def test_play_without_a_song_explains_itself(self):
        message = make_message("!p")
        self.assertTrue(await self.cog.handle_message(message))
        self.assertIn("what should i play", message.reply.await_args.kwargs["content"])

    async def test_voice_bridge_returns_a_spoken_sentence(self):
        text = await self.cog.handle_voice_request(
            make_message("hey paradise play billie jean"), "play billie jean"
        )
        self.assertIsNotNone(text)
        self.assertIn("Billie Jean", text)

    async def test_voice_bridge_ignores_questions(self):
        text = await self.cog.handle_voice_request(
            make_message("hey paradise what's up"), "what's the capital of rajasthan"
        )
        self.assertIsNone(text)

    async def test_voice_bridge_reports_errors_as_text(self):
        text = await self.cog.handle_voice_request(
            make_message("hey paradise pause"), "pause"
        )
        self.assertIn("nothing is playing", text)

    async def test_play_needs_a_voice_channel(self):
        # A guild the bot has never joined: nothing to play into.
        with self.assertRaises(MusicError):
            await self.cog.execute(
                parse_music_intent("play x"),
                guild=SimpleNamespace(id=1234),
                member=SimpleNamespace(id=7, display_name="T", voice=None),
            )

    async def test_panel_button_pauses(self):
        await self.cog.service.play(
            SimpleNamespace(id=99), SimpleNamespace(id=555), "a"
        )
        note = await self.cog._panel_action(self.cog.service.player(99), "toggle")
        self.assertIn("paused", note)
        note = await self.cog._panel_action(self.cog.service.player(99), "toggle")
        self.assertIn("resumed", note)

    async def test_panel_volume_buttons(self):
        player = self.cog.service.player(99)
        self.assertIn("40%", await self.cog._panel_action(player, "vol_down"))
        self.assertIn("50%", await self.cog._panel_action(player, "vol_up"))

    async def test_loop_button_cycles(self):
        player = self.cog.service.player(99)
        self.assertIn("this song", await self.cog._panel_action(player, "loop"))
        self.assertIn("the queue", await self.cog._panel_action(player, "loop"))
        self.assertIn("off", await self.cog._panel_action(player, "loop"))


# ---------------------------------------------------------------------------
# Wake-word bridge
# ---------------------------------------------------------------------------


class TestWakeBridge(unittest.IsolatedAsyncioTestCase):
    """“Hey Paradise play …” must reach music, not Gemini."""

    async def asyncSetUp(self):
        from commands.wake import Wake

        self.ai = SimpleNamespace(
            answer_quick=AsyncMock(return_value="an AI answer")
        )
        self.calls: list[str] = []

        async def handle_voice_request(message, text):
            # Mirrors the real cog: only music requests get an answer.
            self.calls.append(text)
            if parse_music_intent(text) is None:
                return None
            return "playing **Billie Jean** (`4:53`)."

        self.music = SimpleNamespace(handle_voice_request=handle_voice_request)
        self.bot = SimpleNamespace(
            user=SimpleNamespace(id=1),
            ai=self.ai,
            guilds=[],
            config=Config.from_env({"WAKE_WORD_SPEAK": "false"}),
            get_cog=lambda name: self.music if name == "Music" else None,
            get_guild=lambda gid: None,
        )
        self.cog = Wake(self.bot)
        await self.cog.cog_load()

    async def asyncTearDown(self):
        await self.cog.cog_unload()

    async def test_music_request_is_not_sent_to_gemini(self):
        message = make_message("hey paradise play billie jean")
        self.assertTrue(await self.cog.handle_message(message))
        self.assertEqual(self.calls, ["play billie jean"])
        self.ai.answer_quick.assert_not_awaited()
        self.assertIn("Billie Jean", message.reply.await_args.args[0])

    async def test_questions_still_reach_gemini(self):
        message = make_message("hey paradise what's the capital of rajasthan")
        self.assertTrue(await self.cog.handle_message(message))
        self.assertEqual(self.calls, ["what's the capital of rajasthan"])
        self.ai.answer_quick.assert_awaited_once()
        self.assertIn("an AI answer", message.reply.await_args.args[0])

    async def test_sitting_in_a_voice_channel_enables_the_wake_word_elsewhere(self):
        from commands.wake import match_wake

        self.assertIsNotNone(match_wake("hey paradise hi"))
        message = make_message("hey paradise hi")
        import discord

        message.channel = SimpleNamespace(id=1, send=AsyncMock(), permissions_for=None)
        message.author.voice = SimpleNamespace(
            channel=MagicMock(spec=discord.VoiceChannel)
        )
        # Not a voice channel's text chat, but the author is in one: answer.
        self.assertTrue(await self.cog.handle_message(message))


if __name__ == "__main__":
    unittest.main(verbosity=2)
