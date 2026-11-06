"""Regression tests for the /clip voice-reception fix and voice-state enforcement.

Covers, in order:

* the DAVE E2EE transition bridge (the root-cause fix for "No incoming voice
  audio has been received in this channel yet"),
* the receiver's decryption-failure accounting,
* ``/clip``'s accurate empty-capture diagnosis,
* the permanent server-mute / server-undeafen enforcement cog,
* the shared voice-client join policy (never joins deafened, never swaps the
  receiver out for a plain client).

All offline: fakes only, no sockets, no gateway.
"""

from __future__ import annotations

import asyncio
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import nacl.secret
from discord.ext import commands

from bot import ParadiseBot
from config import Config, VoiceClipConfig
from functions.voice_clip import (
    ClipVoiceClient,
    NoUsableAudioError,
    RollingAudioBuffer,
    VoiceClipService,
    VoiceReceiveRouter,
    voice_client_class_for,
)
from functions.voice_clip import dave_compat
from functions.voice_clip.receiver import (
    VOICE_OP_DAVE_EXECUTE_TRANSITION,
    VOICE_OP_DAVE_PREPARE_TRANSITION,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeDaveSession:
    """A davey-0.2-style session that records transition calls."""

    def __init__(self) -> None:
        self.ready = True
        self.prepared: list[tuple[int, int]] = []
        self.executed: list[int] = []
        self.fail_prepare = False

    def prepare_transition(self, transition_id: int, protocol_version: int) -> None:
        if self.fail_prepare:
            raise RuntimeError("mls exploded")
        self.prepared.append((int(transition_id), int(protocol_version)))

    def execute_transition(self, transition_id: int) -> bool:
        self.executed.append(int(transition_id))
        return True


class _FakeClock:
    def __init__(self, initial: float = 1000.0) -> None:
        self.now = initial

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


async def _hook_client(with_service: bool):
    """A real ClipVoiceClient wired enough to dispatch voice-ws hook messages."""
    bot = ParadiseBot(Config())
    if with_service:
        bot.voice_clip_service = VoiceClipService(bot, VoiceClipConfig())
    channel = MagicMock(spec=discord.VoiceChannel)
    channel.id = 888
    guild = MagicMock(spec=discord.Guild)
    guild.id = 777
    channel.guild = guild
    vc = ClipVoiceClient(bot, channel)
    return vc, bot


# ---------------------------------------------------------------------------
# 1. The DAVE transition bridge
# ---------------------------------------------------------------------------


class TestDaveCompatBridge(unittest.IsolatedAsyncioTestCase):
    async def test_hook_applies_prepare_and_execute_transitions(self) -> None:
        """Ops 21/22 reach the davey session — the discord.py 2.7.1 gap."""
        vc, bot = await _hook_client(with_service=True)
        session = _FakeDaveSession()
        vc._connection.dave_session = session
        ws = SimpleNamespace()
        try:
            await vc._on_voice_ws_message(
                ws,
                {
                    "op": VOICE_OP_DAVE_PREPARE_TRANSITION,
                    "d": {"transition_id": 4, "protocol_version": 1},
                },
            )
            await vc._on_voice_ws_message(
                ws,
                {"op": VOICE_OP_DAVE_EXECUTE_TRANSITION, "d": {"transition_id": 4}},
            )
        finally:
            await bot.close()

        self.assertEqual(session.prepared, [(4, 1)])
        self.assertEqual(session.executed, [4])

    async def test_hook_applies_transitions_without_a_clip_receiver(self) -> None:
        """The bridge must run even when no clip receiver is bound."""
        vc, bot = await _hook_client(with_service=False)
        self.assertIsNone(vc._receive_router)
        session = _FakeDaveSession()
        vc._connection.dave_session = session
        try:
            await vc._on_voice_ws_message(
                SimpleNamespace(),
                {
                    "op": VOICE_OP_DAVE_PREPARE_TRANSITION,
                    "d": {"transition_id": 0, "protocol_version": 1},
                },
            )
        finally:
            await bot.close()
        self.assertEqual(session.prepared, [(0, 1)])

    def test_bridge_is_a_noop_for_davey_0_1_sessions(self) -> None:
        """A davey 0.1.x session (no transition methods) is left untouched."""
        old_style = SimpleNamespace()  # no prepare_transition/execute_transition
        self.assertFalse(dave_compat.session_supports_transitions(old_style))
        self.assertFalse(dave_compat.prepare_dave_transition(old_style, 1, 1))
        self.assertFalse(dave_compat.execute_dave_transition(old_style, 1))

    async def test_bridge_swallows_session_errors(self) -> None:
        session = _FakeDaveSession()
        session.fail_prepare = True
        self.assertFalse(dave_compat.prepare_dave_transition(session, 2, 1))
        # And a totally broken "session" object must not raise either.
        self.assertFalse(dave_compat.execute_dave_transition(object(), 2))

    async def test_malformed_transition_payloads_are_ignored(self) -> None:
        vc, bot = await _hook_client(with_service=False)
        session = _FakeDaveSession()
        vc._connection.dave_session = session
        ws = SimpleNamespace()
        try:
            for payload in (
                {"op": VOICE_OP_DAVE_PREPARE_TRANSITION, "d": {}},
                {
                    "op": VOICE_OP_DAVE_PREPARE_TRANSITION,
                    "d": {"transition_id": "x", "protocol_version": 1},
                },
                {"op": VOICE_OP_DAVE_EXECUTE_TRANSITION, "d": {}},
                {"op": VOICE_OP_DAVE_EXECUTE_TRANSITION, "d": {"transition_id": None}},
            ):
                await vc._on_voice_ws_message(ws, payload)
        finally:
            await bot.close()
        self.assertEqual(session.prepared, [])
        self.assertEqual(session.executed, [])


# ---------------------------------------------------------------------------
# 2. Receiver DAVE failure accounting
# ---------------------------------------------------------------------------


def _router_with_dave(dave) -> tuple[VoiceReceiveRouter, "callable[[bytes, int], bytes]"]:
    clock = _FakeClock(initial=100.0)
    buf = RollingAudioBuffer(guild_id=1, clock=clock)
    buf.start_session(channel_id=10)
    router = VoiceReceiveRouter(
        guild_id=1,
        buffer=buf,
        dave_state_getter=lambda: (dave, 1),
        clock=clock,
    )
    secret = bytes(range(32))
    router.configure_transport("aead_xchacha20_poly1305_rtpsize", secret, own_ssrc=555)
    router.register_ssrc(user_id=1001, ssrc=111)

    def datagram(payload: bytes, nonce_int: int) -> bytes:
        box = nacl.secret.Aead(secret)
        header = struct.pack(">BBHII", 0x80, 0x78, 1, 960, 111)
        nonce = bytearray(24)
        nonce[:4] = struct.pack(">I", nonce_int & 0xFFFFFFFF)
        ciphertext = box.encrypt(payload, header, bytes(nonce)).ciphertext
        return header + ciphertext + bytes(nonce[:4])

    return router, datagram


class TestReceiverDaveAccounting(unittest.TestCase):
    def test_failure_streak_grows_and_success_resets(self) -> None:
        dave = MagicMock()
        dave.ready = True
        dave.can_passthrough = MagicMock(return_value=False)
        dave.decrypt = MagicMock(side_effect=ValueError("no key"))
        router, datagram = _router_with_dave(dave)

        router.handle_udp_packet(datagram(b"\xf8\xff\xfe", 1))  # silence: skips DAVE
        router.handle_udp_packet(datagram(b"ENC1", 2))
        router.handle_udp_packet(datagram(b"ENC2", 3))
        self.assertEqual(router.dave_errors, 2)
        self.assertEqual(router.dave_fail_streak, 2)
        self.assertFalse(router.dave_ever_succeeded)

        dave.decrypt = MagicMock(side_effect=lambda uid, mt, data: data[4:])
        router.handle_udp_packet(datagram(b"XXXX" + b"opus", 4))
        self.assertEqual(router.dave_fail_streak, 0)
        self.assertTrue(router.dave_ever_succeeded)
        # The silence frame (skips the DAVE gate by design) + the recovered frame.
        self.assertEqual(router.packets_buffered, 2)

    def test_recovery_candidates_flags_never_decrypted_connections(self) -> None:
        dave = MagicMock()
        dave.ready = True
        dave.can_passthrough = MagicMock(return_value=False)
        dave.decrypt = MagicMock(side_effect=ValueError("no key"))
        service = VoiceClipService(MagicMock(), VoiceClipConfig())
        gid = 4242
        buf = service.get_or_create_buffer(gid)
        buf.start_session(channel_id=5)
        router = VoiceReceiveRouter(
            guild_id=gid,
            buffer=buf,
            dave_state_getter=lambda: (dave, 1),
        )
        service._routers[gid] = router
        router.packets_received = 300
        router.dave_fail_streak = 300
        self.assertEqual(service.capture_recovery_candidates(), [gid])

        # A healthy receiver is never flagged.
        router.dave_ever_succeeded = True
        self.assertEqual(service.capture_recovery_candidates(), [])


# ---------------------------------------------------------------------------
# 3. Accurate empty-capture diagnosis for /clip
# ---------------------------------------------------------------------------


class TestCaptureDiagnosis(unittest.TestCase):
    def _service_with_router(self, gid: int = 3131):
        service = VoiceClipService(MagicMock(), VoiceClipConfig())
        buf = service.get_or_create_buffer(gid)
        buf.start_session(channel_id=9)
        router = VoiceReceiveRouter(guild_id=gid, buffer=buf)
        service._routers[gid] = router
        return service, buf, router

    def test_no_udp_packets_reason(self) -> None:
        service, _buf, _router = self._service_with_router()
        reason = service.diagnose_capture(3131)
        self.assertIn("no UDP packets", reason)
        self.assertIn("server-deafened", reason)

    def test_dave_failure_reason(self) -> None:
        service, _buf, router = self._service_with_router()
        router.packets_received = 120
        router.dave_errors = 120
        reason = service.diagnose_capture(3131)
        self.assertIn("DAVE", reason)
        self.assertIn("key exchange", reason)

    def test_transport_failure_reason(self) -> None:
        service, _buf, router = self._service_with_router()
        router.packets_received = 50
        router.transport_errors = 50
        self.assertIn("transport decryption", service.diagnose_capture(3131))

    def test_healthy_receiver_reports_ok(self) -> None:
        service, _buf, router = self._service_with_router()
        router.packets_received = 10
        router.packets_buffered = 8
        self.assertEqual(service.diagnose_capture(3131), "ok")
        with self.assertRaises(NoUsableAudioError) as ctx:
            raise service._empty_capture_error(3131)
        self.assertIn("someone needs to talk", str(ctx.exception))

    def test_inactive_capture_reason(self) -> None:
        service = VoiceClipService(MagicMock(), VoiceClipConfig())
        self.assertEqual(service.diagnose_capture(999), "capture_inactive")


# ---------------------------------------------------------------------------
# 4. Permanent server mute / server undeafen enforcement
# ---------------------------------------------------------------------------


def _member(mute: bool, deaf: bool, *, can_manage: bool = True, member_id: int = 999000):
    voice = SimpleNamespace(channel=SimpleNamespace(id=5), mute=mute, deaf=deaf)
    member = MagicMock(spec=discord.Member)
    member.id = member_id
    member.voice = voice
    member.edit = AsyncMock()
    member.guild_permissions = SimpleNamespace(mute_members=can_manage)
    return member


def _guild_with_member(member):
    guild = MagicMock(spec=discord.Guild)
    guild.id = 321
    guild.me = member
    return guild


class TestVoiceEnforcement(unittest.IsolatedAsyncioTestCase):
    def _cog(self):
        from commands.voice_enforcement import VoiceEnforcement

        bot = MagicMock(spec=commands.Bot)
        bot.user = SimpleNamespace(id=999000)
        cog = VoiceEnforcement(bot)
        cog._sweep.cancel()
        return cog

    async def _dispatch(self, cog, member, *, before_m, before_d, after_m, after_d):
        await cog.on_voice_state_update(
            member,
            SimpleNamespace(mute=before_m, deaf=before_d),
            SimpleNamespace(mute=after_m, deaf=after_d),
        )
        state = cog._states.get(member.guild.id)
        task = state.retry_task if state else None
        if task is not None:
            await asyncio.wait_for(task, timeout=3)

    async def test_unmuted_bot_is_server_muted_again(self) -> None:
        cog = self._cog()
        member = _member(mute=False, deaf=False)
        guild = _guild_with_member(member)
        member.guild = guild
        await self._dispatch(cog, member, before_m=True, before_d=False, after_m=False, after_d=False)
        member.edit.assert_awaited_once()
        kwargs = member.edit.await_args.kwargs
        self.assertTrue(kwargs.get("mute"))
        self.assertIs(kwargs.get("deafen"), False)

    async def test_deafened_bot_is_undeafened_again(self) -> None:
        cog = self._cog()
        member = _member(mute=True, deaf=True)
        guild = _guild_with_member(member)
        member.guild = guild
        await self._dispatch(cog, member, before_m=True, before_d=False, after_m=True, after_d=True)
        member.edit.assert_awaited_once()
        kwargs = member.edit.await_args.kwargs
        self.assertIs(kwargs.get("deafen"), False)
        self.assertTrue(kwargs.get("mute"))

    async def test_desired_state_makes_no_api_call(self) -> None:
        cog = self._cog()
        member = _member(mute=True, deaf=False)
        guild = _guild_with_member(member)
        member.guild = guild
        await self._dispatch(cog, member, before_m=False, before_d=True, after_m=True, after_d=False)
        member.edit.assert_not_awaited()

    async def test_identical_voice_events_are_deduplicated(self) -> None:
        cog = self._cog()
        member = _member(mute=False, deaf=False)
        guild = _guild_with_member(member)
        member.guild = guild
        for _ in range(5):
            await cog.on_voice_state_update(
                member,
                SimpleNamespace(mute=True, deaf=False),
                SimpleNamespace(mute=False, deaf=False),
            )
        state = cog._states.get(guild.id)
        if state is not None and state.retry_task is not None:
            await asyncio.wait_for(state.retry_task, timeout=3)
        self.assertEqual(member.edit.await_count, 1)

    async def test_missing_permission_never_calls_the_api(self) -> None:
        cog = self._cog()
        member = _member(mute=False, deaf=False, can_manage=False)
        guild = _guild_with_member(member)
        member.guild = guild
        await self._dispatch(cog, member, before_m=True, before_d=False, after_m=False, after_d=False)
        member.edit.assert_not_awaited()

    async def test_other_members_are_ignored(self) -> None:
        cog = self._cog()
        member = _member(mute=False, deaf=False, member_id=123456789)
        guild = _guild_with_member(member)
        member.guild = guild
        await cog.on_voice_state_update(
            member,
            SimpleNamespace(mute=False, deaf=False),
            SimpleNamespace(mute=False, deaf=False),
        )
        member.edit.assert_not_awaited()
        self.assertEqual(cog._states, {})

    async def test_wait_for_idle_resolves_after_enforcement(self) -> None:
        cog = self._cog()
        member = _member(mute=False, deaf=False)
        guild = _guild_with_member(member)
        member.guild = guild
        await self._dispatch(cog, member, before_m=True, before_d=False, after_m=False, after_d=False)
        await asyncio.wait_for(cog.wait_for_idle(guild.id), timeout=2)
        self.assertTrue(member.edit.await_count >= 1)


# ---------------------------------------------------------------------------
# 5. Shared join policy
# ---------------------------------------------------------------------------


class TestJoinPolicy(unittest.TestCase):
    def test_clip_enabled_uses_receive_capable_client(self) -> None:
        bot = SimpleNamespace(config=SimpleNamespace(voice_clip=VoiceClipConfig(enabled=True)))
        self.assertIs(voice_client_class_for(bot), ClipVoiceClient)

    def test_clip_disabled_uses_plain_client(self) -> None:
        bot = SimpleNamespace(config=SimpleNamespace(voice_clip=VoiceClipConfig(enabled=False)))
        self.assertIs(voice_client_class_for(bot), discord.VoiceClient)

    def test_real_bot_config_defaults_to_receive_capable(self) -> None:
        self.assertIs(voice_client_class_for(ParadiseBot(Config())), ClipVoiceClient)


# ---------------------------------------------------------------------------
# 6. Pronunciation playback never joins deafened / plain
# ---------------------------------------------------------------------------


class TestPlaybackJoinPolicy(unittest.IsolatedAsyncioTestCase):
    async def test_playback_connects_undeafened_with_receive_capable_class(self) -> None:
        from functions.dictionary.playback import VoicePlayback

        playback = VoicePlayback(enabled=True)
        guild = MagicMock(spec=discord.Guild)
        guild.id = 7
        bot = MagicMock(spec=commands.Bot)
        bot.user = SimpleNamespace(id=1)
        bot.config = Config()
        guild._state = SimpleNamespace(client=bot)

        captured: dict[str, object] = {}

        async def fake_connect(*, reconnect, self_deaf, cls):
            captured["self_deaf"] = self_deaf
            captured["cls"] = cls
            client = MagicMock()
            client.is_connected = MagicMock(return_value=True)
            return client

        channel = MagicMock(spec=discord.VoiceChannel)
        channel.id = 42
        channel.connect = fake_connect

        client, created = await playback._join_channel(guild, channel)

        self.assertTrue(created)
        self.assertIs(captured["self_deaf"], False)
        self.assertIs(captured["cls"], ClipVoiceClient)
        self.assertIs(client.is_connected(), True)


if __name__ == "__main__":
    unittest.main()
