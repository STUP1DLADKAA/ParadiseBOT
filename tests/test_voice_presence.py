"""Unit tests for VoicePresence ("The GOAT has arrived 🐐" VC speech + chat)."""

from __future__ import annotations

import asyncio
import io
import math
import os
import struct
import sys
import unittest
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord

from commands.voice_presence import (
    ARRIVAL_SPEECH_TEXT,
    ARRIVAL_TEXT,
    VoicePresence,
)
from functions.dictionary.models import Clip
from functions.dictionary.playback import playback_support, resolve_ffmpeg_executable


def _make_wav_bytes(duration_sec: float = 0.15, sample_rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    n_samples = int(duration_sec * sample_rate)
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        frames = b"".join(
            struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / sample_rate)))
            for i in range(n_samples)
        )
        wf.writeframes(frames)
    return buf.getvalue()


class _FakeSpeechProvider:
    name = "fake"
    available = True

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.calls: list[str] = []

    async def speak(self, request):
        self.calls.append(request.word)
        return Clip(data=self.data, extension="wav", provider="fake")


def _make_voice_channel(
    channel_id: int,
    *,
    humans: int = 1,
    view: bool = True,
    connect: bool = True,
    speak: bool = True,
):
    perms = SimpleNamespace(
        view_channel=view,
        connect=connect,
        speak=speak,
        move_members=False,
    )
    members = [SimpleNamespace(bot=False, id=100 + i) for i in range(humans)]
    channel = MagicMock()
    channel.id = channel_id
    channel.members = members
    channel.user_limit = 0
    channel.permissions_for = MagicMock(return_value=perms)
    channel.send = AsyncMock()
    return channel


def _make_fake_voice_client(channel):
    client = MagicMock()
    client.channel = channel
    client._playing = False
    client.is_connected = MagicMock(return_value=True)
    client.is_playing = MagicMock(side_effect=lambda: client._playing)
    client.disconnect = AsyncMock()

    async def move_to(target):
        client.channel = target

    client.move_to = AsyncMock(side_effect=move_to)

    def play(source, *, after=None):
        client._playing = True
        if after is not None:
            client._playing = False
            after(None)

    client.play = MagicMock(side_effect=play)
    return client


class TestVoicePresenceArrival(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.wav_bytes = _make_wav_bytes()
        self.provider = _FakeSpeechProvider(self.wav_bytes)
        self.bot = MagicMock()
        self.bot.user = SimpleNamespace(id=999000)
        self.bot.guilds = []
        self.cog = VoicePresence(
            self.bot,
            settle_delay=0.0,
            speech_providers=[self.provider],
        )
        # Stub out external ffmpeg spawn during fast unit tests
        self.cog._create_audio_source = MagicMock(
            side_effect=lambda clip, exe: MagicMock(cleanup=MagicMock())
        )

    async def asyncTearDown(self):
        await self.cog.cog_unload()

    async def test_join_and_move_both_text_and_speak_arrival_once(self):
        vc1 = _make_voice_channel(101, humans=1)
        vc2 = _make_voice_channel(102, humans=3)
        guild = MagicMock()
        guild.id = 555
        guild.afk_channel = None
        guild.me = SimpleNamespace(id=999000, voice=SimpleNamespace(mute=False))
        guild.voice_channels = [vc1]
        guild.voice_client = None

        async def connect_vc1(**kwargs):
            guild.voice_client = _make_fake_voice_client(vc1)
            # Simulate Discord firing VOICE_STATE_UPDATE for the bot mid-connect
            bot_member = SimpleNamespace(id=999000, bot=True, guild=guild)
            await self.cog.on_voice_state_update(
                bot_member,
                SimpleNamespace(channel=None),
                SimpleNamespace(channel=vc1),
            )
            return guild.voice_client

        vc1.connect = AsyncMock(side_effect=connect_vc1)

        with patch("commands.voice_presence.playback_support", return_value=True):
            await self.cog.join_busiest(guild)
            # Let any spawned listener tasks settle
            await asyncio.sleep(0.02)

            vc1.connect.assert_awaited_once()
            vc1.send.assert_awaited_once()
            self.assertEqual(vc1.send.call_args.args[0], ARRIVAL_TEXT)
            guild.voice_client.play.assert_called_once()
            self.assertEqual(self.provider.calls, [ARRIVAL_SPEECH_TEXT])

            # Staying in the same busiest VC does NOT re-announce
            await self.cog.join_busiest(guild)
            vc1.send.assert_awaited_once()
            guild.voice_client.play.assert_called_once()

            # Busier VC appears -> bot moves, texts & speaks in vc2 using cached clip
            guild.voice_channels = [vc1, vc2]
            await self.cog.join_busiest(guild)
            await asyncio.sleep(0.02)

            guild.voice_client.move_to.assert_awaited_once_with(vc2)
            vc2.send.assert_awaited_once()
            self.assertEqual(vc2.send.call_args.args[0], ARRIVAL_TEXT)
            self.assertEqual(guild.voice_client.play.call_count, 2)
            # Clip cached after first synthesis -> still only 1 TTS provider call!
            self.assertEqual(self.provider.calls, [ARRIVAL_SPEECH_TEXT])

    async def test_manual_admin_move_triggers_text_and_voice_arrival(self):
        vc1 = _make_voice_channel(101, humans=2)
        vc2 = _make_voice_channel(102, humans=1)
        guild = MagicMock()
        guild.id = 555
        guild.afk_channel = None
        guild.me = SimpleNamespace(id=999000, voice=SimpleNamespace(mute=False))
        guild.voice_channels = [vc1, vc2]
        guild.voice_client = _make_fake_voice_client(vc2)
        self.cog._last_announced_channel[guild.id] = vc1.id

        bot_member = SimpleNamespace(id=999000, bot=True, guild=guild)
        with patch("commands.voice_presence.playback_support", return_value=True):
            await self.cog.on_voice_state_update(
                bot_member,
                SimpleNamespace(channel=vc1),
                SimpleNamespace(channel=vc2),
            )
            await asyncio.sleep(0.05)

        vc2.send.assert_awaited_once()
        self.assertEqual(vc2.send.call_args.args[0], ARRIVAL_TEXT)
        guild.voice_client.play.assert_called_once()

    async def test_disconnect_when_empty_resets_announcement_for_next_join(self):
        vc1 = _make_voice_channel(101, humans=1)
        guild = MagicMock()
        guild.id = 555
        guild.afk_channel = None
        guild.me = SimpleNamespace(id=999000, voice=SimpleNamespace(mute=False))
        guild.voice_channels = [vc1]
        guild.voice_client = None

        async def connect_vc1(**kwargs):
            guild.voice_client = _make_fake_voice_client(vc1)
            return guild.voice_client

        vc1.connect = AsyncMock(side_effect=connect_vc1)

        with patch("commands.voice_presence.playback_support", return_value=True):
            await self.cog.join_busiest(guild)
            self.assertEqual(vc1.send.await_count, 1)

            # Everyone leaves -> periodic sweep disconnects cleanly
            vc1.members = []
            old_client = guild.voice_client
            await self.cog.join_busiest(guild, disconnect_when_empty=True)
            old_client.disconnect.assert_awaited_once()
            guild.voice_client = None

            # Someone joins again -> announces fresh arrival
            vc1.members = [SimpleNamespace(bot=False, id=101)]
            await self.cog.join_busiest(guild)
            self.assertEqual(vc1.send.await_count, 2)

    async def test_text_failure_does_not_block_voice_speech(self):
        vc1 = _make_voice_channel(101, humans=1)
        vc1.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "denied"))
        guild = MagicMock()
        guild.id = 555
        guild.afk_channel = None
        guild.me = SimpleNamespace(id=999000, voice=SimpleNamespace(mute=False))
        guild.voice_channels = [vc1]
        guild.voice_client = None

        async def connect_vc1(**kwargs):
            guild.voice_client = _make_fake_voice_client(vc1)
            return guild.voice_client

        vc1.connect = AsyncMock(side_effect=connect_vc1)

        with patch("commands.voice_presence.playback_support", return_value=True):
            await self.cog.join_busiest(guild)
            guild.voice_client.play.assert_called_once()

    def test_bundled_ffmpeg_encodes_opus_packets(self):
        support = playback_support()
        self.assertTrue(bool(support), f"Expected voice playback support, got: {support.reason}")
        exe = resolve_ffmpeg_executable()
        self.assertIsNotNone(exe)
        wav_data = _make_wav_bytes(duration_sec=0.1)
        source = discord.FFmpegOpusAudio(
            io.BytesIO(wav_data),
            pipe=True,
            executable=exe,
            options="-loglevel error",
        )
        try:
            packets = []
            while True:
                pkt = source.read()
                if not pkt:
                    break
                packets.append(pkt)
            self.assertGreater(len(packets), 0)
            self.assertTrue(source.is_opus())
        finally:
            stdout = getattr(source, "_stdout", None)
            if stdout is not None and hasattr(stdout, "close"):
                stdout.close()
            source.cleanup()


if __name__ == "__main__":
    unittest.main()
