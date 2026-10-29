"""Offline tests for Paradise's voice (functions/tts.py).

No network: every provider here is a local fake, and the ElevenLabs client is
exercised through its payload/fallback logic with ``_request`` stubbed out.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config, ElevenLabsConfig
from functions.tts import (
    ElevenLabsVoice,
    SpeechClip,
    SpeechService,
    SpeechUnavailable,
    build_speech_service,
    detect_language,
    looks_like_audio,
    speakable_text,
)

MP3 = b"ID3" + b"\x00" * 900


def _cfg(**overrides) -> ElevenLabsConfig:
    base = dict(
        api_key="sk-test-key-123456",
        voice_id="voiceABC123456",
        model="eleven_flash_v2_5",
        output_format="mp3_44100_128",
        stability=0.45,
        similarity_boost=0.85,
        style=0.15,
        speed=1.0,
        language_code="",
        pronunciation_first=True,
        timeout=10.0,
    )
    base.update(overrides)
    return ElevenLabsConfig(**base)


class TestTextCleaning(unittest.TestCase):
    def test_speakable_text_strips_markdown_links_and_newlines(self):
        text = speakable_text("**Bold** _idea_ https://example.com/a\nsecond line `code`")
        self.assertNotIn("*", text)
        self.assertNotIn("https://", text)
        self.assertNotIn("`", text)
        self.assertIn("second line", text)

    def test_long_text_is_cut_on_a_sentence_boundary(self):
        text = "First sentence here. " * 20
        cut = speakable_text(text, max_chars=60)
        self.assertLessEqual(len(cut), 60)
        self.assertTrue(cut.endswith("."))

    def test_detect_language(self):
        self.assertEqual(detect_language("namaste doston"), "en")
        self.assertEqual(detect_language("नमस्ते दोस्तों"), "hi")

    def test_looks_like_audio(self):
        self.assertEqual(looks_like_audio(MP3), "mp3")
        self.assertEqual(looks_like_audio(b"OggS" + b"\x00" * 900), "ogg")
        self.assertEqual(looks_like_audio(b"<html>nope</html>"), "")


class TestElevenLabsClient(unittest.IsolatedAsyncioTestCase):
    async def test_unconfigured_voice_is_unavailable(self):
        voice = ElevenLabsVoice(ElevenLabsConfig())
        self.assertFalse(voice.available)
        self.assertIsNone(await voice.speak("hi"))

    async def test_payload_uses_configured_voice_and_fast_model(self):
        voice = ElevenLabsVoice(_cfg())
        payload = voice._payload("namaste, hello", "hi")
        self.assertEqual(payload["model_id"], "eleven_flash_v2_5")
        self.assertEqual(payload["language_code"], "hi")
        self.assertEqual(payload["voice_settings"]["similarity_boost"], 0.85)
        self.assertNotIn("sk-test-key-123456", str(payload))

    async def test_headers_keep_the_key_out_of_the_url(self):
        voice = ElevenLabsVoice(_cfg())
        headers = voice._headers()
        self.assertEqual(headers["xi-api-key"], "sk-test-key-123456")

    async def test_speak_returns_a_clip(self):
        voice = ElevenLabsVoice(_cfg())
        calls: list[str] = []

        async def fake_request(text, *, language, model, minimal=False):
            calls.append(model)
            return MP3

        voice._request = fake_request
        clip = await voice.speak("hello there", language="en")
        self.assertIsNotNone(clip)
        self.assertEqual(clip.extension, "mp3")
        self.assertEqual(clip.provider, "elevenlabs")
        self.assertEqual(calls, ["eleven_flash_v2_5"])
        await voice.close()

    async def test_a_422_retries_the_same_model_with_safe_settings(self):
        voice = ElevenLabsVoice(_cfg())
        calls: list[tuple[str, bool]] = []

        async def fake_request(text, *, language, model, minimal=False):
            calls.append((model, minimal))
            if not minimal:
                raise SpeechUnavailable("ElevenLabs returned HTTP 422 (voice_settings)")
            return MP3

        voice._request = fake_request
        clip = await voice.speak("hello", language="en")
        self.assertIsNotNone(clip)
        self.assertEqual(
            calls,
            [("eleven_flash_v2_5", False), ("eleven_flash_v2_5", True)],
        )
        self.assertEqual(voice.model, "eleven_flash_v2_5")
        await voice.close()

    async def test_model_fallback_when_this_plan_lacks_the_model(self):
        voice = ElevenLabsVoice(_cfg())
        calls: list[str] = []

        async def fake_request(text, *, language, model, minimal=False):
            calls.append(model)
            if model == "eleven_flash_v2_5":
                raise SpeechUnavailable(
                    "ElevenLabs returned HTTP 400 (model eleven_flash_v2_5 not available)"
                )
            return MP3

        voice._request = fake_request
        clip = await voice.speak("hello", language="en")
        self.assertIsNotNone(clip)
        self.assertEqual(calls[0], "eleven_flash_v2_5")
        self.assertEqual(calls[1], "eleven_turbo_v2_5")
        self.assertEqual(voice.model, "eleven_turbo_v2_5")
        await voice.close()

    async def test_dead_model_is_remembered(self):
        voice = ElevenLabsVoice(_cfg())

        async def fake_request(text, *, language, model, minimal=False):
            if model == "eleven_flash_v2_5":
                raise SpeechUnavailable("HTTP 400 model not allowed")
            return MP3

        voice._request = fake_request
        await voice.speak("one", language="en")
        calls = voice._dead_models
        self.assertIn("eleven_flash_v2_5", calls)
        await voice.close()

    async def test_error_message_never_contains_the_key(self):
        voice = ElevenLabsVoice(_cfg())

        async def fake_request(text, *, language, model, minimal=False):
            raise SpeechUnavailable(f"ElevenLabs returned HTTP 401 (model {model})")

        voice._request = fake_request
        with self.assertLogs("functions.tts", level="WARNING") as logs:
            clip = await voice.speak("hi", language="en")
        self.assertIsNone(clip)  # every model failed -> no clip, no crash
        self.assertNotIn("sk-test-key-123456", "\n".join(logs.output))
        # A failed key must not be hammered again on the next request.
        calls_before = len(voice._dead_models)
        await voice.speak("hi again", language="en")
        self.assertEqual(len(voice._dead_models), calls_before)
        await voice.close()


class _FakeProvider:
    def __init__(self, name: str, clip: SpeechClip | None, *, fail: bool = False):
        self.name = name
        self._clip = clip
        self._fail = fail
        self.calls: list[tuple[str, str]] = []

    @property
    def available(self) -> bool:
        return True

    async def speak(self, text, *, language="en"):
        self.calls.append((text, language))
        if self._fail:
            raise RuntimeError("provider blew up")
        return self._clip


class TestSpeechService(unittest.IsolatedAsyncioTestCase):
    async def test_uses_the_first_provider_that_works(self):
        first = _FakeProvider("elevenlabs", None, fail=True)
        second = _FakeProvider("openai", SpeechClip(data=MP3, provider="openai"))
        service = SpeechService(providers=[first, second])
        clip = await service.synthesize("hello")
        self.assertEqual(clip.provider, "openai")
        self.assertEqual(len(first.calls), 1)
        self.assertEqual(len(second.calls), 1)

    async def test_all_providers_failing_raises_a_safe_error(self):
        service = SpeechService(
            providers=[
                _FakeProvider("a", None, fail=True),
                _FakeProvider("b", None),
            ]
        )
        with self.assertRaises(SpeechUnavailable):
            await service.synthesize("hello")

    async def test_cache_avoids_a_second_synthesis(self):
        provider = _FakeProvider("elevenlabs", SpeechClip(data=MP3))
        service = SpeechService(providers=[provider])
        await service.synthesize("same words")
        await service.synthesize("same words")
        self.assertEqual(len(provider.calls), 1)

    async def test_preload_writes_and_reuses_a_disk_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = _FakeProvider("elevenlabs", SpeechClip(data=MP3))
            service = SpeechService(providers=[provider], cache_dir=tmp)
            clip = await service.preload("refusal", "Nope. Not answering that.")
            self.assertIsNotNone(clip)
            self.assertEqual(len(os.listdir(tmp)), 1)

            # A brand-new service (i.e. after a restart) reads it from disk.
            fresh_provider = _FakeProvider("elevenlabs", SpeechClip(data=MP3))
            fresh = SpeechService(providers=[fresh_provider], cache_dir=tmp)
            again = await fresh.preload("refusal", "Nope. Not answering that.")
            self.assertIsNotNone(again)
            self.assertEqual(fresh_provider.calls, [])

    async def test_provider_names_skip_unconfigured_providers(self):
        service = build_speech_service(Config())
        self.assertNotIn("elevenlabs", service.provider_names)
        self.assertIn("web", service.provider_names)
        await service.close()

    async def test_elevenlabs_leads_when_configured(self):
        config = Config(
            elevenlabs=_cfg(),
            dictionary=Config().dictionary,
        )
        service = build_speech_service(config)
        self.assertEqual(service.provider_names[0], "elevenlabs")
        self.assertTrue(service.using_elevenlabs)
        await service.close()


class TestElevenLabsConfig(unittest.TestCase):
    def test_defaults_are_the_fast_multilingual_male_setup(self):
        config = Config.from_env(
            {
                "ELEVENLABS_API_KEY": " k ",
                "ELEVENLABS_VOICE_ID": " v ",
            }
        ).elevenlabs
        self.assertTrue(config.configured)
        self.assertEqual(config.model, "eleven_flash_v2_5")
        self.assertEqual(config.output_format, "mp3_44100_128")
        self.assertEqual(config.api_key, "k")
        self.assertEqual(config.voice_id, "v")

    def test_masked_key_never_shows_the_secret(self):
        config = _cfg()
        self.assertNotIn("sk-test-key-123456", config.masked_key)

    def test_speed_and_style_are_clamped(self):
        config = Config.from_env(
            {"ELEVENLABS_SPEED": "9", "ELEVENLABS_STABILITY": "-3"}
        ).elevenlabs
        self.assertLessEqual(config.speed, 1.2)
        self.assertGreaterEqual(config.stability, 0.0)


if __name__ == "__main__":
    unittest.main()
