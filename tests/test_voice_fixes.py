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
        self.assertEqual(router.dave_fail_since_success, 2)
        self.assertFalse(router.dave_ever_succeeded)

        dave.decrypt = MagicMock(side_effect=lambda uid, mt, data: data[4:])
        router.handle_udp_packet(datagram(b"XXXX" + b"opus", 4))
        self.assertEqual(router.dave_fail_streak, 0)
        self.assertEqual(router.dave_fail_since_success, 0)
        self.assertTrue(router.dave_ever_succeeded)
        self.assertIsNotNone(router.last_dave_success_mono)
        # The silence frame (skips the DAVE gate by design) + the recovered frame.
        self.assertEqual(router.packets_buffered, 2)
        self.assertEqual(router.non_silence_buffered, 1)

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
        service, _buf, router = self._service_with_router()
        router.packets_received = 0
        reason = service.diagnose_capture(3131)
        self.assertIn("no UDP packets", reason)
        self.assertIn("server-deafened", reason)

    def test_disarmed_receiver_reason(self) -> None:
        """Packets flow but no decryptor: the post-rebind dark state."""
        service, _buf, router = self._service_with_router()
        router.packets_received = 300  # counted, dropped pre-decrypt
        self.assertIn("decryptor", service.diagnose_capture(3131))
        self.assertIn("rebound", service.diagnose_capture(3131))

    def test_arming_receiver_clears_dark_reason(self) -> None:
        clock = _FakeClock(initial=100.0)
        service = VoiceClipService(MagicMock(), VoiceClipConfig(), clock=clock)
        gid = 3132
        buf = service.get_or_create_buffer(gid)
        buf.start_session(channel_id=9)
        clock.advance(30.0)  # session is 30s old
        router = VoiceReceiveRouter(guild_id=gid, buffer=buf, clock=clock)
        service._routers[gid] = router
        router.packets_received = 300
        # Disarmed: the post-rebind dark state
        self.assertIn("decryptor", service.diagnose_capture(gid))
        # Arming clears it (session still has no packets -> "waiting" text)
        router.configure_transport("xsalsa20_poly1305", bytes(range(32)))
        self.assertIn("none decrypted", service.diagnose_capture(gid))

    def test_only_silence_captured_reason(self) -> None:
        service, buf, router = self._service_with_router()
        secret = bytes(range(32))
        router.configure_transport("xsalsa20_poly1305", secret)
        router.packets_received = 100
        # Feed only silence frames into the buffer
        for i in range(6):
            buf.append_packet(
                user_id=10,
                opus_data=b"\xf8\xff\xfe",
                rtp_timestamp=i * 960,
                sequence=i,
                ssrc=1,
            )
        self.assertIn("only silence", service.diagnose_capture(3131))

    def test_session_with_speech_reports_ok(self) -> None:
        service, buf, router = self._service_with_router()
        secret = bytes(range(32))
        router.configure_transport("xsalsa20_poly1305", secret)
        router.packets_received = 100
        for i in range(6):
            buf.append_packet(
                user_id=10,
                opus_data=b"\x01\x02\x03\x04",
                rtp_timestamp=i * 960,
                sequence=i,
                ssrc=1,
            )
        self.assertEqual(service.diagnose_capture(3131), "ok")

    def test_session_just_started_reason(self) -> None:
        clock = _FakeClock(initial=100.0)
        service = VoiceClipService(MagicMock(), VoiceClipConfig(), clock=clock)
        buf = service.get_or_create_buffer(77)
        buf.start_session(channel_id=9)
        clock.advance(0.5)  # session is 0.5s old
        router = VoiceReceiveRouter(guild_id=77, buffer=buf, clock=clock)
        service._routers[77] = router
        router.packets_received = 40
        router.configure_transport("xsalsa20_poly1305", bytes(range(32)))
        self.assertEqual(service.diagnose_capture(77), "session_just_started")

    def test_cumulative_router_counters_cannot_mask_dead_session(self) -> None:
        """The regression this fixes: lifetime router counts ≠ session health."""
        clock = _FakeClock(initial=100.0)
        service = VoiceClipService(MagicMock(), VoiceClipConfig(), clock=clock)
        buf = service.get_or_create_buffer(88)
        buf.start_session(channel_id=9)
        router = VoiceReceiveRouter(guild_id=88, buffer=buf, clock=clock)
        service._routers[88] = router
        router.configure_transport("xsalsa20_poly1305", bytes(range(32)))
        # Simulate a past that had audio (lifetime counters) ...
        router.packets_received = 5_000
        router.packets_buffered = 4_000
        router.non_silence_buffered = 3_000
        clock.advance(300.0)  # ... but the session has been silent for 5 min.
        # Must NOT be "ok": the current session has captured nothing.
        self.assertNotEqual(service.diagnose_capture(88), "ok")
        self.assertIn("went dark after a rebind", service.diagnose_capture(88))

    def test_inactive_capture_reason(self) -> None:
        service = VoiceClipService(MagicMock(), VoiceClipConfig())
        self.assertEqual(service.diagnose_capture(999), "capture_inactive")


class TestEmptyCaptureMessaging(unittest.TestCase):
    def test_user_message_matches_the_failing_stage(self) -> None:
        service = VoiceClipService(MagicMock(), VoiceClipConfig())
        gid = 55

        # No session at all
        msg = str(service._empty_capture_error(gid))
        self.assertIn("not currently active", msg)

        # Fresh session
        buf = service.get_or_create_buffer(gid)
        buf.start_session(channel_id=1)
        clock = _FakeClock(initial=0.0)
        service._clock = clock
        router = VoiceReceiveRouter(guild_id=gid, buffer=buf, clock=clock)
        router.configure_transport("xsalsa20_poly1305", bytes(range(32)))
        router.packets_received = 100
        service._routers[gid] = router
        clock.now = 500.0  # started ~long ago relative to fake session start
        buf._started_mono = 0.0
        msg = str(service._empty_capture_error(gid))
        self.assertIn("none decrypted", msg)

    def test_silent_window_message_says_speak_then_run(self) -> None:
        service = VoiceClipService(MagicMock(), VoiceClipConfig())
        gid = 56
        buf = service.get_or_create_buffer(gid)
        buf.start_session(channel_id=1)
        router = VoiceReceiveRouter(guild_id=gid, buffer=buf)
        router.configure_transport("xsalsa20_poly1305", bytes(range(32)))
        router.packets_received = 100
        service._routers[gid] = router
        for i in range(6):
            buf.append_packet(
                user_id=10,
                opus_data=b"\x01\x02\x03\x04",
                rtp_timestamp=i * 960,
                sequence=i,
                ssrc=1,
            )
        # Session HAS speech; the requested window was silent -> "ok" branch
        original = NoUsableAudioError("No audible speech was captured in the last 5s (only silence).")
        with self.assertLogs(level="WARNING") as logs:
            msg = str(service._empty_capture_error(gid, original))
        self.assertIn("speak", msg)
        self.assertIn("longer duration", msg)
        # The precise original error is preserved in the log, not swallowed.
        self.assertTrue(any("only silence" in line for line in logs.output))


class TestCaptureRecoveryV2(unittest.TestCase):
    def _service_router(self, gid=4242):
        service = VoiceClipService(MagicMock(), VoiceClipConfig())
        buf = service.get_or_create_buffer(gid)
        buf.start_session(channel_id=5)
        router = VoiceReceiveRouter(guild_id=gid, buffer=buf)
        service._routers[gid] = router
        return service, router

    def test_never_worked_is_flagged(self) -> None:
        service, router = self._service_router()
        router.packets_received = 300
        router.dave_fail_streak = 300
        self.assertIn(4242, service.capture_recovery_candidates())

    def test_worked_then_went_dark_is_flagged(self) -> None:
        """Speech decoded once, then everything fails — must be recoverable."""
        service, router = self._service_router()
        router.packets_received = 2_000
        router.dave_ever_succeeded = True
        router.last_dave_success_mono = 0.0
        router.dave_fail_since_success = 700
        service._clock = _FakeClock(initial=100.0)  # 100s after last success
        self.assertIn(4242, service.capture_recovery_candidates())

    def test_recent_success_not_flagged(self) -> None:
        service, router = self._service_router()
        router.packets_received = 2_000
        router.packets_buffered = 1_800
        router.non_silence_buffered = 1_500
        router.dave_ever_succeeded = True
        router.last_dave_success_mono = 95.0
        router.dave_fail_since_success = 700
        service._clock = _FakeClock(initial=100.0)  # only 5s since success
        self.assertNotIn(4242, service.capture_recovery_candidates())

    def test_fully_dark_pipeline_is_flagged(self) -> None:
        """Receiver gets packets but decodes nothing at all (never armed)."""
        service, router = self._service_router()
        router.packets_received = 600
        router.packets_buffered = 0
        self.assertIn(4242, service.capture_recovery_candidates())

    def test_healthy_receiver_not_flagged(self) -> None:
        service, router = self._service_router()
        router.packets_received = 2_000
        router.packets_buffered = 1_800
        router.non_silence_buffered = 1_500
        router.dave_ever_succeeded = True
        router.last_dave_success_mono = 99.0
        service._clock = _FakeClock(initial=100.0)
        self.assertEqual(service.capture_recovery_candidates(), [])


# ---------------------------------------------------------------------------
# 4. Router state inheritance across rebinds (channel moves / recovery)
# ---------------------------------------------------------------------------


class TestRouterStateInheritance(unittest.TestCase):
    def _armed_router(self, gid: int = 1, channel_id: int = 10):
        clock = _FakeClock(initial=100.0)
        buf = RollingAudioBuffer(guild_id=gid, clock=clock)
        buf.start_session(channel_id=channel_id)
        router = VoiceReceiveRouter(guild_id=gid, buffer=buf, clock=clock)
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

    def test_inherit_carries_transport_and_mappings(self) -> None:
        old, datagram = self._armed_router()
        old.handle_udp_packet(datagram(b"\x01\x02\x03\x04", 1))
        self.assertEqual(old.packets_buffered, 1)
        old_received = old.packets_received

        new_buf = RollingAudioBuffer(guild_id=1)
        new_buf.start_session(channel_id=10)
        fresh = VoiceReceiveRouter(guild_id=1, buffer=new_buf)
        self.assertFalse(fresh.has_transport())
        fresh.inherit_from(old)

        # Transport armed + SSRC mapping carried: the SAME encrypted stream
        # continues to buffer without any new SESSION_DESCRIPTION.
        self.assertTrue(fresh.has_transport())
        fresh.handle_udp_packet(datagram(b"\x05\x06\x07\x08", 2))
        # inherited count (1) + this packet
        self.assertEqual(fresh.packets_buffered, 2)
        self.assertEqual(new_buf.stats()["session_packets"], 1)
        # Diagnostic continuity: lifetime counters carried over.
        self.assertEqual(fresh.packets_received, old_received + 1)

    def test_inherit_without_old_is_a_noop(self) -> None:
        fresh = VoiceReceiveRouter(guild_id=1, buffer=RollingAudioBuffer(guild_id=1))
        fresh.inherit_from(None)
        self.assertFalse(fresh.has_transport())

    def test_move_rebind_keeps_capture_alive_end_to_end(self) -> None:
        """The production killer: channel move without op 4 must not die."""
        import asyncio as _asyncio

        async def scenario() -> None:
            bot = ParadiseBot(Config())
            bot.voice_clip_service = VoiceClipService(bot, VoiceClipConfig())
            channel = MagicMock(spec=discord.VoiceChannel)
            channel.id = 888
            guild = MagicMock(spec=discord.Guild)
            guild.id = 777
            channel.guild = guild
            vc = ClipVoiceClient(bot, channel)
            try:
                # First bind via the hook (like READY would)
                ws = SimpleNamespace(secret_key=list(range(32)))
                await vc._on_voice_ws_message(ws, {"op": 2, "d": {"ssrc": 9999}})
                await vc._on_voice_ws_message(
                    ws,
                    {
                        "op": 4,
                        "d": {
                            "mode": "aead_xchacha20_poly1305_rtpsize",
                            "dave_protocol_version": 0,
                        },
                    },
                )
                self.assertTrue(vc._receive_router.has_transport())
                router_before = vc._receive_router
                router_before.register_ssrc(user_id=1001, ssrc=111)

                # A channel move: the hook sees the raw voice-state payload
                # (self.channel still points at the old channel in this fake,
                # which is what the move-detection compares against).
                await vc.on_voice_state_update(
                    {"channel_id": 999, "session_id": "abc"}
                )
                self.assertIsNot(vc._receive_router, router_before)
                # ... and the new router is STILL ARMED — the fix.
                self.assertTrue(vc._receive_router.has_transport())
                # The recording session switched to the new channel.
                self.assertEqual(
                    bot.voice_clip_service.get_buffer(777).channel_id, 999
                )
            finally:
                vc._teardown_recording()
                await bot.close()

        _asyncio.run(scenario())


class TestTransportRearm(unittest.IsolatedAsyncioTestCase):
    async def test_arm_from_connection_state_without_op4(self) -> None:
        bot = ParadiseBot(Config())
        channel = MagicMock(spec=discord.VoiceChannel)
        channel.id = 888
        guild = MagicMock(spec=discord.Guild)
        guild.id = 777
        channel.guild = guild
        vc = ClipVoiceClient(bot, channel)
        try:
            vc._receive_router = VoiceReceiveRouter(
                guild_id=777, buffer=RollingAudioBuffer(guild_id=777)
            )
            self.assertFalse(vc._receive_router.has_transport())
            vc._connection.secret_key = list(range(32))
            vc._connection.mode = "xsalsa20_poly1305"
            vc._connection.ssrc = 4242
            vc._arm_receiver()
            self.assertTrue(vc._receive_router.has_transport())
        finally:
            await bot.close()

    async def test_arm_is_a_noop_before_handshake(self) -> None:
        bot = ParadiseBot(Config())
        channel = MagicMock(spec=discord.VoiceChannel)
        channel.id = 888
        guild = MagicMock(spec=discord.Guild)
        guild.id = 777
        channel.guild = guild
        vc = ClipVoiceClient(bot, channel)
        try:
            vc._receive_router = VoiceReceiveRouter(
                guild_id=777, buffer=RollingAudioBuffer(guild_id=777)
            )
            vc._connection.secret_key = discord.utils.MISSING
            vc._connection.mode = discord.utils.MISSING
            vc._arm_receiver()  # must not raise
            self.assertFalse(vc._receive_router.has_transport())
        finally:
            await bot.close()


class TestStagedFrameTTL(unittest.TestCase):
    def test_frames_staged_over_2s_still_flush_after_mapping(self) -> None:
        """A user already speaking at join time maps only at their next pause."""
        clock = _FakeClock(initial=100.0)
        buf = RollingAudioBuffer(guild_id=1, clock=clock)
        buf.start_session(channel_id=10)
        router = VoiceReceiveRouter(guild_id=1, buffer=buf, clock=clock)
        secret = bytes(range(32))
        router.configure_transport("aead_xchacha20_poly1305_rtpsize", secret)
        router.register_ssrc(user_id=0, ssrc=0)  # no-op warmup

        def datagram(nonce_int: int) -> bytes:
            box = nacl.secret.Aead(secret)
            header = struct.pack(">BBHII", 0x80, 0x78, 1, 960, 321)
            nonce = bytearray(24)
            nonce[:4] = struct.pack(">I", nonce_int & 0xFFFFFFFF)
            ciphertext = box.encrypt(b"\x01\x02\x03\x04", header, bytes(nonce)).ciphertext
            return header + ciphertext + bytes(nonce[:4])

        # Packet arrives from an unknown SSRC while the user is mid-sentence
        router.handle_udp_packet(datagram(1))
        self.assertEqual(buf.stats()["session_packets"], 0)  # staged, not dropped

        clock.advance(5.0)  # 5s later they pause; Discord sends SPEAKING
        router.register_ssrc(user_id=1001, ssrc=321)
        # The staged frame flushed into the buffer with the now-known user.
        self.assertEqual(buf.stats()["session_packets"], 1)
        self.assertEqual(router.non_silence_buffered, 1)


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


# ---------------------------------------------------------------------------
# 7. Real-socket end-to-end: encrypted RTP across a channel move
# ---------------------------------------------------------------------------


class TestMoveWithRealUDPCapture(unittest.IsolatedAsyncioTestCase):
    async def test_encrypted_speech_flows_across_a_channel_move(self) -> None:
        """The production killer, replayed with a real OS UDP socket.

        Bind → armed via the real hook → real AEAD RTP packets buffered →
        channel move (new router, no new handshake) → the SAME encrypted
        stream must keep buffering.  Before the fix the post-move router had
        no decryptor and every packet was dropped forever.
        """
        import socket as socket_mod

        bot = ParadiseBot(Config())
        bot.voice_clip_service = VoiceClipService(bot, VoiceClipConfig())
        channel = MagicMock(spec=discord.VoiceChannel)
        channel.id = 888
        guild = MagicMock(spec=discord.Guild)
        guild.id = 777
        guild.get_member = MagicMock(return_value=SimpleNamespace(bot=False))
        # discord.py's move handler does self.voice_client.channel = guild.get_channel(new_id)
        guild.get_channel = MagicMock(return_value=channel)
        channel.guild = guild
        vc = ClipVoiceClient(bot, channel)
        guild.voice_client = vc
        vc._connection.state = discord.voice_state.ConnectionFlowState.connected

        recv_sock = socket_mod.socket(socket_mod.AF_INET, socket_mod.SOCK_DGRAM)
        recv_sock.bind(("127.0.0.1", 0))
        recv_sock.setblocking(False)
        recv_addr = recv_sock.getsockname()
        vc._connection.socket = recv_sock
        vc._connection._socket_reader.resume()

        sender = socket_mod.socket(socket_mod.AF_INET, socket_mod.SOCK_DGRAM)
        secret = bytes(range(32))
        ws = SimpleNamespace(secret_key=list(secret))

        def datagram(ssrc: int, seq: int, payload: bytes, nonce_int: int) -> bytes:
            box = nacl.secret.Aead(secret)
            header = struct.pack(">BBHII", 0x90, 0x78, seq, seq * 960, ssrc)
            ext_preamble = struct.pack(">2sH", b"\xbe\xde", 1)
            aad = header + ext_preamble
            plaintext = b"\x10\x2a\x00\x00" + payload
            nonce = bytearray(24)
            nonce[:4] = struct.pack(">I", nonce_int & 0xFFFFFFFF)
            ct = box.encrypt(plaintext, aad, bytes(nonce)).ciphertext
            return aad + ct + bytes(nonce[:4])

        try:
            await vc._on_voice_ws_message(ws, {"op": 2, "d": {"ssrc": 9999}})
            await vc._on_voice_ws_message(
                ws,
                {
                    "op": 4,
                    "d": {
                        "mode": "aead_xchacha20_poly1305_rtpsize",
                        "dave_protocol_version": 0,
                    },
                },
            )
            await vc._on_voice_ws_message(
                ws, {"op": 5, "d": {"user_id": "7001", "ssrc": 1111, "speaking": 1}}
            )

            def buffered() -> int:
                buf = bot.voice_clip_service.get_buffer(777)
                return int(buf.stats()["session_packets"] or 0) if buf else 0

            # Speech before the move
            for i in range(4):
                sender.sendto(datagram(1111, i, b"\x01\x02\x03\x04", i + 1), recv_addr)
                await asyncio.sleep(0.005)
            for _ in range(200):
                if buffered() >= 2:
                    break
                await asyncio.sleep(0.01)
            self.assertGreaterEqual(buffered(), 2)

            # The move: raw voice-state payload with the new channel id
            await vc.on_voice_state_update(
                {"channel_id": 999, "session_id": "sess-1"}
            )
            self.assertTrue(vc._receive_router.has_transport())

            # Speech after the move — same encrypted stream, no new op 4
            base = buffered()  # 0 if the move restarted the session, else 4
            for i in range(4, 10):
                sender.sendto(datagram(1111, i, b"\x01\x02\x03\x04", i + 1), recv_addr)
                await asyncio.sleep(0.005)
            deadline = asyncio.get_running_loop().time() + 5.0
            target = base + 2
            while buffered() < target and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
            self.assertGreaterEqual(buffered(), target)

            health = bot.voice_clip_service.capture_health(777)
            self.assertTrue(health["receiver_armed"])
            self.assertGreater(health["session_non_silence_packets"], 0)
        finally:
            vc._teardown_recording()
            vc._connection._socket_reader.stop()
            recv_sock.close()
            sender.close()
            await bot.close()


if __name__ == "__main__":
    unittest.main()
