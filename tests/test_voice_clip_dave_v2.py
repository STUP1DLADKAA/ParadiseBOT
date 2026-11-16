"""End-to-end regression suite for Voice Gateway v8, DAVE 0.2 MLS, RTP AEAD, and M4A conversion."""

from __future__ import annotations

import asyncio
import os
import socket
import struct
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import av
import davey
import nacl.secret
import numpy as np
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from discord.gateway import DiscordVoiceWebSocket
from discord.voice_state import ConnectionFlowState, SocketReader

from config import VoiceClipConfig
from functions.dictionary.playback import resolve_ffmpeg_executable
from functions.voice_clip import (
    OPUS_SILENCE,
    SAMPLE_RATE,
    SUPPORTED_TRANSPORT_MODES,
    ClipExportError,
    ClipVoiceClient,
    ClipVoiceConnectionState,
    ClipVoiceWebSocket,
    RollingAudioBuffer,
    TransportDecryptor,
    VoiceClipService,
    VoiceReceiveRouter,
)
from functions.voice_clip.constants import AUTHORIZED_CLIP_USER_IDS
from functions.voice_clip.dave_compat import (
    is_dave_encrypted_frame,
    session_can_encrypt,
    session_supports_transitions,
    try_identify_dave_sender,
)
from functions.voice_clip.mixer import (
    LIMITER_CEILING,
    MAX_STATIC_GAIN,
    apply_static_gain,
    mix_snapshot_to_pcm_f32,
)
from functions.voice_clip.opus_codec import (
    SpeakerOpusDecoder,
    _av_frame_to_mono_f32,
    ensure_opus_loaded,
)

AUTHORIZED_ID = next(iter(AUTHORIZED_CLIP_USER_IDS))
GUILD_ID = 777000111222333444
CHANNEL_ID = 888000111222333444
BOT_ID = 999000111222333444
ALICE_ID = 111000111222333444
BOB_ID = 222000111222333444
OTHER_BOT_ID = 333000111222333444


# ---------------------------------------------------------------------------
# Real RFC 9420 MLS 1.0 ExternalSender simulator (DAVE P256/AES128GCM/SHA256)
# ---------------------------------------------------------------------------


def _mls_varint(n: int) -> bytes:
    if n < 0x40:
        return bytes([n])
    if n < 0x4000:
        return struct.pack(">H", 0x4000 | n)
    if n < 0x40000000:
        return struct.pack(">I", 0x80000000 | n)
    raise ValueError(f"MLS varint overflow: {n}")


def _mls_vl(payload: bytes) -> bytes:
    return _mls_varint(len(payload)) + payload


class FakeDiscordMLSSFU:
    """Signs genuine RFC 9420 MLS ExternalSender Add/Remove proposals for davey."""

    def __init__(self, channel_id: int) -> None:
        self.channel_id = channel_id
        self.group_id = channel_id.to_bytes(8, "big")
        self._sk = ec.generate_private_key(ec.SECP256R1())
        self._pk = self._sk.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )

    def external_sender_bytes(self) -> bytes:
        # ExternalSender { signature_key<V>, Credential::basic(0x0001, "\x00") }
        return _mls_vl(self._pk) + struct.pack(">H", 1) + _mls_vl(b"\x00")

    def _sign_public_proposal(self, epoch: int, proposal: bytes) -> bytes:
        framed = (
            _mls_vl(self.group_id)
            + struct.pack(">QBI", epoch, 2, 0)  # SenderType::external = 2, index = 0
            + _mls_vl(b"")  # authenticated_data
            + b"\x02"  # ContentType::proposal = 2
            + proposal
        )
        tbs = struct.pack(">HH", 1, 1) + framed  # mls10, mls_public_message
        sign_content = _mls_vl(b"MLS 1.0 FramedContentTBS") + _mls_vl(tbs)
        sig = self._sk.sign(sign_content, ec.ECDSA(hashes.SHA256()))
        return struct.pack(">HH", 1, 1) + framed + _mls_vl(sig)

    def add_proposals(self, epoch: int, key_packages: list[bytes]) -> bytes:
        body = b"".join(
            self._sign_public_proposal(epoch, struct.pack(">H", 1) + kp)
            for kp in key_packages
        )
        return _mls_vl(body)

    @staticmethod
    def bin_frame(seq: int, opcode: int, payload: bytes) -> bytes:
        return struct.pack(">HB", seq, opcode) + payload

    @classmethod
    def external_sender_frame(cls, seq: int, ext_bytes: bytes) -> bytes:
        return cls.bin_frame(seq, DiscordVoiceWebSocket.MLS_EXTERNAL_SENDER, ext_bytes)

    @classmethod
    def proposals_frame(cls, seq: int, op_type: int, proposals: bytes) -> bytes:
        return cls.bin_frame(
            seq,
            DiscordVoiceWebSocket.MLS_PROPOSALS,
            bytes([op_type]) + proposals,
        )

    @classmethod
    def commit_frame(cls, seq: int, transition_id: int, commit: bytes) -> bytes:
        return cls.bin_frame(
            seq,
            DiscordVoiceWebSocket.MLS_ANNOUNCE_COMMIT_TRANSITION,
            struct.pack(">H", transition_id) + commit,
        )

    @classmethod
    def welcome_frame(cls, seq: int, transition_id: int, welcome: bytes) -> bytes:
        return cls.bin_frame(
            seq,
            DiscordVoiceWebSocket.MLS_WELCOME,
            struct.pack(">H", transition_id) + welcome,
        )


def _encode_opus_tone(
    freq_hz: float,
    duration_s: float = 0.6,
    *,
    amplitude: float = 0.35,
    layout: str = "mono",
    frame_ms: int = 20,
) -> list[bytes]:
    enc = av.CodecContext.create("libopus", "w")
    enc.sample_rate = SAMPLE_RATE
    enc.layout = layout
    enc.format = "s16"
    enc.bit_rate = 64_000
    enc.options = {"frame_duration": str(frame_ms), "fec": "1", "packet_loss": "15"}
    enc.open()

    total = int(SAMPLE_RATE * duration_s)
    frame_samples = SAMPLE_RATE * frame_ms // 1000
    t = np.arange(total, dtype=np.float32) / float(SAMPLE_RATE)
    pcm = (amplitude * np.sin(2.0 * np.pi * freq_hz * t) * 32767.0).astype(np.int16)

    packets: list[bytes] = []
    for off in range(0, total, frame_samples):
        chunk = pcm[off : off + frame_samples]
        if chunk.size < frame_samples:
            chunk = np.pad(chunk, (0, frame_samples - chunk.size))
        arr = (
            np.column_stack([chunk, chunk]).reshape(1, -1)
            if layout == "stereo"
            else chunk.reshape(1, -1)
        )
        frame = av.AudioFrame.from_ndarray(arr, format="s16", layout=layout)
        frame.sample_rate = SAMPLE_RATE
        frame.pts = off
        packets.extend(bytes(p) for p in enc.encode(frame))
    packets.extend(bytes(p) for p in enc.encode(None))
    return [p for p in packets if p]


def _build_encrypted_rtp(
    mode: str,
    key: bytes,
    *,
    seq: int,
    ts: int,
    ssrc: int,
    payload: bytes,
    nonce_int: int = 1,
    ext_profile: int | None = None,
    ext_body: bytes = b"",
    csrcs: tuple[int, ...] = (),
    pad_bytes: int = 0,
    payload_type: int = 120,
    marker: bool = False,
) -> bytes:
    cc = len(csrcs)
    b0 = 0x80 | (0x20 if pad_bytes > 0 else 0) | (0x10 if ext_profile is not None else 0) | cc
    b1 = (0x80 if marker else 0) | (payload_type & 0x7F)
    header = struct.pack(">BBHII", b0, b1, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc & 0xFFFFFFFF)
    for csrc in csrcs:
        header += struct.pack(">I", csrc & 0xFFFFFFFF)

    ext_hdr = b""
    if ext_profile is not None:
        assert len(ext_body) % 4 == 0
        ext_hdr = struct.pack(">HH", ext_profile, len(ext_body) // 4)

    padding = (b"\x00" * (pad_bytes - 1) + bytes([pad_bytes])) if pad_bytes > 0 else b""
    plain = ext_body + payload + padding

    if mode == "aead_aes256_gcm_rtpsize":
        aad = header + ext_hdr
        nonce = struct.pack(">I", nonce_int) + b"\x00" * 8
        return aad + AESGCM(key).encrypt(nonce, plain, aad) + nonce[:4]
    if mode == "aead_xchacha20_poly1305_rtpsize":
        aad = header + ext_hdr
        nonce = struct.pack(">I", nonce_int) + b"\x00" * 20
        return aad + nacl.secret.Aead(key).encrypt(plain, aad, nonce).ciphertext + nonce[:4]
    if mode == "xsalsa20_poly1305_lite":
        nonce = struct.pack(">I", nonce_int) + b"\x00" * 20
        box = nacl.secret.SecretBox(key)
        return header + box.encrypt(ext_hdr + plain, nonce).ciphertext + nonce[:4]
    if mode == "xsalsa20_poly1305_suffix":
        nonce = struct.pack(">I", nonce_int) + b"\x00" * 20
        box = nacl.secret.SecretBox(key)
        return header + box.encrypt(ext_hdr + plain, nonce).ciphertext + nonce
    if mode == "xsalsa20_poly1305":
        nonce = header[:12] + b"\x00" * 12
        box = nacl.secret.SecretBox(key)
        return header + box.encrypt(ext_hdr + plain, nonce).ciphertext
    raise ValueError(f"Unknown mode {mode}")


def _make_harness(loop: asyncio.AbstractEventLoop):
    """Build a standalone ClipVoiceConnectionState + ClipVoiceWebSocket test rig."""
    sent_json: list[dict] = []
    sent_binary: list[tuple[int, bytes]] = []
    hook_calls: list[dict] = []

    guild = SimpleNamespace(
        id=GUILD_ID,
        members=[],
        get_member=lambda _uid: None,
    )
    channel = SimpleNamespace(id=CHANNEL_ID, guild=guild)
    vc = SimpleNamespace(
        channel=channel,
        guild=guild,
        loop=loop,
        user=SimpleNamespace(id=BOT_ID),
        _state=SimpleNamespace(
            user=SimpleNamespace(id=BOT_ID),
            loop=loop,
        ),
        _connection=None,
    )

    async def _hook(_ws, msg):
        hook_calls.append(msg)

    conn = ClipVoiceConnectionState(vc, hook=_hook)
    conn.state = ConnectionFlowState.connected
    vc._connection = conn

    ws = object.__new__(ClipVoiceWebSocket)
    ws._connection = conn
    ws._hook = _hook
    ws._max_heartbeat_timeout = 60.0
    ws.thread_id = threading.get_ident()
    ws.gateway = "wss://voice.discord.test"
    ws._keep_alive = None
    ws._close_code = None
    ws.secret_key = None
    ws.ssrc = 77777
    ws.seq_ack = -1

    async def _send_as_json(payload: dict) -> None:
        sent_json.append(payload)

    async def _send_binary(opcode: int, data: bytes) -> None:
        sent_binary.append((opcode, bytes(data)))

    async def _send_transition_ready(tid: int) -> None:
        sent_json.append(
            {
                "op": DiscordVoiceWebSocket.DAVE_TRANSITION_READY,
                "d": {"transition_id": tid},
            }
        )

    ws.send_as_json = _send_as_json
    ws.send_binary = _send_binary
    ws.send_transition_ready = _send_transition_ready
    ws.load_secret_key = AsyncMock()
    ws.initial_connection = AsyncMock()
    conn.ws = ws
    return vc, conn, ws, sent_json, sent_binary


def _read_mls_varint(buf: bytes, pos: int) -> tuple[int, int]:
    first = buf[pos]
    tag = first >> 6
    if tag == 0:
        return first & 0x3F, pos + 1
    if tag == 1:
        return struct.unpack_from(">H", buf, pos)[0] & 0x3FFF, pos + 2
    if tag == 2:
        return struct.unpack_from(">I", buf, pos)[0] & 0x3FFFFFFF, pos + 4
    raise ValueError("Unsupported 8-byte MLS varint")


def _skip_vl(buf: bytes, pos: int) -> int:
    length, pos = _read_mls_varint(buf, pos)
    return pos + length


def _split_commit_welcome(blob: bytes) -> tuple[bytes, bytes]:
    """Deterministically split Op 28's ``MLSMessage(Commit) || Welcome`` payload."""
    # MLSMessage header: version=0x0001, wire_format=0x0001 (mls_public_message)
    assert blob[:4] == b"\x00\x01\x00\x01"
    pos = 4
    pos = _skip_vl(blob, pos)  # group_id<V>
    pos += 8  # uint64 epoch
    sender_type = blob[pos]
    pos += 1
    if sender_type in (1, 2):  # member or external: uint32 index
        pos += 4
    pos = _skip_vl(blob, pos)  # authenticated_data<V>
    content_type = blob[pos]
    pos += 1
    assert content_type == 3  # ContentType::commit
    # Commit { proposals<V>; optional<UpdatePath> path; }
    pos = _skip_vl(blob, pos)
    has_path = blob[pos]
    pos += 1
    if has_path == 1:
        # UpdatePath { LeafNode leaf_node; UpdatePathNode nodes<V>; }
        pos = _skip_vl(blob, pos)  # encryption_key<V>
        pos = _skip_vl(blob, pos)  # signature_key<V>
        pos += 2  # credential_type
        pos = _skip_vl(blob, pos)  # identity<V>
        # Capabilities: versions<V>, ciphersuites<V>, extensions<V>, proposals<V>, credentials<V>
        for _ in range(5):
            pos = _skip_vl(blob, pos)
        leaf_node_source = blob[pos]
        pos += 1
        if leaf_node_source == 1:  # key_package -> Lifetime (2 * uint64)
            pos += 16
        elif leaf_node_source == 3:  # commit -> parent_hash<V>
            pos = _skip_vl(blob, pos)
        pos = _skip_vl(blob, pos)  # extensions<V>
        pos = _skip_vl(blob, pos)  # signature<V>
        pos = _skip_vl(blob, pos)  # nodes<V>
    # FramedContentAuthData for commit: signature<V> + confirmation_tag<V>
    pos = _skip_vl(blob, pos)
    pos = _skip_vl(blob, pos)
    # PublicMessage membership_tag<V> (present for SenderType::member == 1)
    if sender_type == 1:
        pos = _skip_vl(blob, pos)
    commit, welcome = blob[:pos], blob[pos:]
    assert len(welcome) > 4
    return commit, welcome


# ---------------------------------------------------------------------------
# 1. Voice Gateway v8 + davey 0.2.0 MLS State Machine Tests
# ---------------------------------------------------------------------------


class TestDaveV2GatewayStateMachine(unittest.IsolatedAsyncioTestCase):
    async def test_full_welcome_and_commit_epochs_install_decryptors_and_guard_encrypt(
        self,
    ) -> None:
        loop = asyncio.get_running_loop()
        vc, conn, ws, sent_json, sent_binary = _make_harness(loop)
        sfu = FakeDiscordMLSSFU(CHANNEL_ID)
        ext = sfu.external_sender_bytes()

        alice = davey.DaveSession(1, ALICE_ID, CHANNEL_ID)
        alice.set_external_sender(ext)
        _ = alice.get_serialized_key_package()

        # Op 11: CLIENTS_CONNECT reports Alice and Bot in the channel.
        await ws.received_message(
            {"op": 11, "d": {"user_ids": [str(ALICE_ID), str(BOT_ID)]}}
        )
        self.assertEqual(conn.connected_user_ids, {ALICE_ID, BOT_ID})

        # Op 4: SESSION_DESCRIPTION negotiates aead_aes256_gcm_rtpsize + DAVE v1.
        key = os.urandom(32)
        await ws.received_message(
            {
                "op": DiscordVoiceWebSocket.SESSION_DESCRIPTION,
                "d": {
                    "mode": "aead_aes256_gcm_rtpsize",
                    "secret_key": list(key),
                    "dave_protocol_version": 1,
                },
            }
        )
        self.assertIsNotNone(conn.dave_session)
        self.assertTrue(session_supports_transitions(conn.dave_session))
        self.assertEqual(len(sent_binary), 1)
        self.assertEqual(sent_binary[0][0], DiscordVoiceWebSocket.MLS_KEY_PACKAGE)
        bot_kp = sent_binary[0][1]

        # Op 25: MLS_EXTERNAL_SENDER.
        await ws.received_binary_message(sfu.external_sender_frame(1, ext))

        # Alice creates Commit + Welcome adding Paradise to Epoch 1.
        props1 = sfu.add_proposals(0, [bot_kp])
        cw1 = alice.process_proposals(davey.ProposalsOperationType.append, props1)
        assert cw1 is not None and cw1.welcome is not None
        self.assertTrue(alice.process_commit(cw1.commit))
        alice.prepare_transition(3, 1)
        self.assertTrue(alice.execute_transition(3))

        # Op 30: Paradise receives MLS_WELCOME (transition_id=3).
        await ws.received_binary_message(sfu.welcome_frame(2, 3, cw1.welcome))
        self.assertTrue(conn.dave_session.ready)
        # Right after Welcome, davey 0.2 has ready=True (decryptors live) but
        # can_encrypt=False until DAVE_EXECUTE_TRANSITION arrives!
        self.assertFalse(session_can_encrypt(conn.dave_session))
        self.assertIn(
            {"op": DiscordVoiceWebSocket.DAVE_TRANSITION_READY, "d": {"transition_id": 3}},
            sent_json,
        )

        # Alice's DAVE-encrypted speech MUST decrypt immediately after Op 30!
        alice_ct = alice.encrypt_opus(b"\x78ALICE_SPEAKING_EPOCH_1")
        self.assertTrue(is_dave_encrypted_frame(alice_ct))
        pt = conn.dave_session.decrypt(ALICE_ID, davey.MediaType.audio, alice_ct)
        self.assertEqual(pt, b"\x78ALICE_SPEAKING_EPOCH_1")

        # Op 22: DAVE_EXECUTE_TRANSITION(3) flips can_encrypt=True and syncs privacy codes.
        await ws.received_message(
            {"op": DiscordVoiceWebSocket.DAVE_EXECUTE_TRANSITION, "d": {"transition_id": 3}}
        )
        self.assertTrue(session_can_encrypt(conn.dave_session))
        self.assertEqual(conn.dave_session.voice_privacy_code, alice.voice_privacy_code)

        # Epoch 2: Bob joins — Paradise itself processes proposals (Op 27), emits Op 28,
        # applies commit transition (Op 29), emits Op 23, and executes Op 22!
        bob = davey.DaveSession(1, BOB_ID, CHANNEL_ID)
        bob.set_external_sender(ext)
        bob_kp = bob.get_serialized_key_package()

        await ws.received_message({"op": 11, "d": {"user_ids": [str(BOB_ID)]}})
        props2 = sfu.add_proposals(1, [bob_kp])
        _ = alice.process_proposals(davey.ProposalsOperationType.append, props2)

        sent_binary.clear()
        sent_json.clear()
        await ws.received_binary_message(sfu.proposals_frame(3, 0, props2))
        self.assertEqual(len(sent_binary), 1)
        self.assertEqual(sent_binary[0][0], DiscordVoiceWebSocket.MLS_COMMIT_WELCOME)
        commit2, welcome2 = _split_commit_welcome(sent_binary[0][1])

        # Apply commit2 across Paradise (Op 29), Alice, and Welcome on Bob.
        await ws.received_binary_message(sfu.commit_frame(4, 4, commit2))
        self.assertIn(
            {"op": DiscordVoiceWebSocket.DAVE_TRANSITION_READY, "d": {"transition_id": 4}},
            sent_json,
        )
        self.assertTrue(alice.process_commit(commit2))
        alice.prepare_transition(4, 1)
        bob.process_welcome(welcome2)
        bob.prepare_transition(4, 1)

        await ws.received_message(
            {"op": DiscordVoiceWebSocket.DAVE_EXECUTE_TRANSITION, "d": {"transition_id": 4}}
        )
        self.assertTrue(alice.execute_transition(4))
        self.assertTrue(bob.execute_transition(4))

        # Both Alice AND Bob encrypt speech in Epoch 2; Paradise decrypts both!
        self.assertEqual(
            conn.dave_session.decrypt(
                ALICE_ID, davey.MediaType.audio, alice.encrypt_opus(b"\x78ALICE_E2")
            ),
            b"\x78ALICE_E2",
        )
        self.assertEqual(
            conn.dave_session.decrypt(
                BOB_ID, davey.MediaType.audio, bob.encrypt_opus(b"\x78BOB_E2")
            ),
            b"\x78BOB_E2",
        )

        # Op 13: CLIENT_DISCONNECT removes Bob from connected_user_ids cleanly.
        await ws.received_message({"op": 13, "d": {"user_id": str(BOB_ID)}})
        self.assertEqual(conn.connected_user_ids, {ALICE_ID, BOT_ID})

    async def test_send_audio_packet_guards_post_welcome_not_ready_and_encrypts_post_execute(
        self,
    ) -> None:
        loop = asyncio.get_running_loop()
        sfu = FakeDiscordMLSSFU(CHANNEL_ID)
        ext = sfu.external_sender_bytes()
        key = os.urandom(32)

        guild = SimpleNamespace(id=GUILD_ID, members=[], get_member=lambda _uid: None)
        channel = SimpleNamespace(
            id=CHANNEL_ID,
            guild=guild,
            _get_voice_client_key=lambda: (GUILD_ID, "guild_id"),
        )
        bot = SimpleNamespace(
            user=SimpleNamespace(id=BOT_ID),
            voice_clip_service=None,
            _connection=SimpleNamespace(
                user=SimpleNamespace(id=BOT_ID),
                loop=loop,
                _remove_voice_client=lambda _gid: None,
            ),
        )
        vc = ClipVoiceClient(bot, channel)  # type: ignore[arg-type]
        conn = vc._connection
        sent_udp: list[bytes] = []
        conn.socket = SimpleNamespace(sendall=lambda data: sent_udp.append(bytes(data)))
        conn.ip = "127.0.0.1"
        conn.port = 50000
        conn.ssrc = 54321
        conn.mode = "aead_aes256_gcm_rtpsize"
        conn.secret_key = list(key)
        conn.state = ConnectionFlowState.connected

        ws = object.__new__(ClipVoiceWebSocket)
        ws._connection = conn
        ws._hook = AsyncMock()
        ws.seq_ack = -1
        sent_bin: list[tuple[int, bytes]] = []
        ws.send_binary = AsyncMock(side_effect=lambda op, b: sent_bin.append((op, bytes(b))))
        ws.send_as_json = AsyncMock()
        ws.send_transition_ready = AsyncMock()
        ws.load_secret_key = AsyncMock()
        conn.ws = ws

        try:
            await ws.received_message(
                {
                    "op": DiscordVoiceWebSocket.SESSION_DESCRIPTION,
                    "d": {
                        "mode": "aead_aes256_gcm_rtpsize",
                        "secret_key": list(key),
                        "dave_protocol_version": 1,
                    },
                }
            )
            bot_kp = sent_bin[-1][1]
            await ws.received_binary_message(sfu.external_sender_frame(1, ext))

            alice = davey.DaveSession(1, ALICE_ID, CHANNEL_ID)
            alice.set_external_sender(ext)
            _ = alice.get_serialized_key_package()
            cw = alice.process_proposals(
                davey.ProposalsOperationType.append, sfu.add_proposals(0, [bot_kp])
            )
            assert cw is not None and cw.welcome is not None
            alice.process_commit(cw.commit)
            alice.prepare_transition(6, 1)
            alice.execute_transition(6)

            # Right after Op 30 Welcome (before Op 22 Execute): ready=True, can_encrypt=False.
            # Outbound silence prime MUST NOT raise ValueError: Failed to encrypt: NotReady!
            await ws.received_binary_message(sfu.welcome_frame(2, 6, cw.welcome))
            self.assertTrue(conn.dave_session.ready)
            self.assertFalse(conn.can_encrypt)
            vc.send_audio_packet(OPUS_SILENCE, encode=False)
            self.assertEqual(len(sent_udp), 1)

            # After Op 22 Execute: outbound non-silence frame is encrypted with BOTH
            # DAVE E2EE AND aead_aes256_gcm_rtpsize, and Alice decrypts it bit-for-bit!
            await ws.received_message(
                {"op": DiscordVoiceWebSocket.DAVE_EXECUTE_TRANSITION, "d": {"transition_id": 6}}
            )
            self.assertTrue(conn.can_encrypt)
            sample_opus = b"\x78OUTBOUND_TEST_FRAME"
            vc.send_audio_packet(sample_opus, encode=False)
            self.assertEqual(len(sent_udp), 2)

            rtp_parsed = TransportDecryptor("aead_aes256_gcm_rtpsize", key).decrypt_rtp(
                sent_udp[1]
            )
            assert rtp_parsed is not None
            self.assertTrue(is_dave_encrypted_frame(rtp_parsed.payload))
            self.assertEqual(
                alice.decrypt(BOT_ID, davey.MediaType.audio, rtp_parsed.payload),
                sample_opus,
            )
        finally:
            vc.cleanup()

    async def test_upgrade_from_initial_v0_to_v1_and_downgrade_back_to_v0(self) -> None:
        loop = asyncio.get_running_loop()
        _, conn, ws, sent_json, sent_binary = _make_harness(loop)
        sfu = FakeDiscordMLSSFU(CHANNEL_ID)
        ext = sfu.external_sender_bytes()

        buf = RollingAudioBuffer(guild_id=GUILD_ID)
        buf.start_session(CHANNEL_ID)
        key = os.urandom(32)
        router = VoiceReceiveRouter(
            guild_id=GUILD_ID,
            buffer=buf,
            dave_state_getter=lambda: (conn.dave_session, conn.dave_protocol_version),
            dave_downgraded_getter=lambda: conn.dave_downgraded,
            dave_lock=conn.dave_lock,
            own_user_id_getter=lambda: BOT_ID,
        )
        router.configure_transport("aead_aes256_gcm_rtpsize", key)
        router.register_ssrc(user_id=ALICE_ID, ssrc=101)

        # Call starts at protocol v0 (a legacy non-DAVE client is in the channel).
        await ws.received_message(
            {
                "op": DiscordVoiceWebSocket.SESSION_DESCRIPTION,
                "d": {
                    "mode": "aead_aes256_gcm_rtpsize",
                    "secret_key": list(key),
                    "dave_protocol_version": 0,
                },
            }
        )
        self.assertIsNone(conn.dave_session)
        self.assertTrue(conn.dave_downgraded)

        # Plain unencrypted Opus buffers cleanly at v0.
        router.handle_udp_packet(
            _build_encrypted_rtp(
                "aead_aes256_gcm_rtpsize",
                key,
                seq=1,
                ts=960,
                ssrc=101,
                payload=b"\x78PLAIN_V0",
                nonce_int=1,
            )
        )
        self.assertEqual(router.packets_buffered, 1)

        # Legacy user leaves -> Discord sends Op 24 (DAVE_PREPARE_EPOCH, epoch=1, ver=1)
        # while conn.dave_session is STILL None!
        await ws.received_message(
            {
                "op": DiscordVoiceWebSocket.DAVE_PREPARE_EPOCH,
                "d": {"epoch": 1, "protocol_version": 1},
            }
        )
        self.assertIsNotNone(conn.dave_session)
        self.assertEqual(conn.dave_protocol_version, 1)
        self.assertTrue(conn.dave_downgraded)  # still upgrading until Op 22!
        self.assertEqual(len(sent_binary), 1)
        self.assertEqual(sent_binary[0][0], DiscordVoiceWebSocket.MLS_KEY_PACKAGE)
        bot_kp = sent_binary[0][1]

        # Unencrypted packets arriving mid-upgrade STILL buffer without drop!
        router.handle_udp_packet(
            _build_encrypted_rtp(
                "aead_aes256_gcm_rtpsize",
                key,
                seq=2,
                ts=1920,
                ssrc=101,
                payload=b"\x78PLAIN_UPGRADING",
                nonce_int=2,
            )
        )
        self.assertEqual(router.packets_buffered, 2)

        # Complete MLS Welcome (tid=9) + Execute (tid=9) -> full E2EE v1 active!
        alice = davey.DaveSession(1, ALICE_ID, CHANNEL_ID)
        alice.set_external_sender(ext)
        _ = alice.get_serialized_key_package()
        cw = alice.process_proposals(
            davey.ProposalsOperationType.append, sfu.add_proposals(0, [bot_kp])
        )
        assert cw is not None and cw.welcome is not None
        alice.process_commit(cw.commit)
        alice.prepare_transition(9, 1)
        alice.execute_transition(9)

        await ws.received_binary_message(sfu.external_sender_frame(1, ext))
        await ws.received_binary_message(sfu.welcome_frame(2, 9, cw.welcome))
        await ws.received_message(
            {"op": DiscordVoiceWebSocket.DAVE_EXECUTE_TRANSITION, "d": {"transition_id": 9}}
        )
        self.assertFalse(conn.dave_downgraded)
        self.assertTrue(conn.dave_session.can_encrypt)
        self.assertTrue(conn.can_encrypt)

        # E2EE packet decrypts cleanly!
        router.handle_udp_packet(
            _build_encrypted_rtp(
                "aead_aes256_gcm_rtpsize",
                key,
                seq=3,
                ts=2880,
                ssrc=101,
                payload=alice.encrypt_opus(b"\x78ENCRYPTED_V1"),
                nonce_int=3,
            )
        )
        self.assertEqual(router.packets_buffered, 3)

        # Now downgrade v1 -> v0 via Op 21 + Op 22!
        sent_json.clear()
        await ws.received_message(
            {
                "op": DiscordVoiceWebSocket.DAVE_PREPARE_TRANSITION,
                "d": {"transition_id": 10, "protocol_version": 0},
            }
        )
        self.assertIn(
            {"op": DiscordVoiceWebSocket.DAVE_TRANSITION_READY, "d": {"transition_id": 10}},
            sent_json,
        )
        self.assertTrue(conn.dave_session.can_passthrough(ALICE_ID))
        await ws.received_message(
            {"op": DiscordVoiceWebSocket.DAVE_EXECUTE_TRANSITION, "d": {"transition_id": 10}}
        )
        self.assertEqual(conn.dave_protocol_version, 0)
        self.assertTrue(conn.dave_downgraded)

        router.handle_udp_packet(
            _build_encrypted_rtp(
                "aead_aes256_gcm_rtpsize",
                key,
                seq=4,
                ts=3840,
                ssrc=101,
                payload=b"\x78PLAIN_DOWNGRADED",
                nonce_int=4,
            )
        )
        self.assertEqual(router.packets_buffered, 4)
        self.assertEqual(router.dave_errors, 0)

    async def test_external_sender_already_in_group_and_invalid_welcome_recover_via_op31(
        self,
    ) -> None:
        loop = asyncio.get_running_loop()
        _, conn, ws, sent_json, sent_binary = _make_harness(loop)
        sfu1 = FakeDiscordMLSSFU(CHANNEL_ID)
        ext1 = sfu1.external_sender_bytes()

        await ws.received_message(
            {
                "op": DiscordVoiceWebSocket.SESSION_DESCRIPTION,
                "d": {
                    "mode": "aead_xchacha20_poly1305_rtpsize",
                    "secret_key": list(os.urandom(32)),
                    "dave_protocol_version": 1,
                },
            }
        )
        bot_kp = sent_binary[-1][1]
        await ws.received_binary_message(sfu1.external_sender_frame(1, ext1))

        alice = davey.DaveSession(1, ALICE_ID, CHANNEL_ID)
        alice.set_external_sender(ext1)
        _ = alice.get_serialized_key_package()
        cw = alice.process_proposals(
            davey.ProposalsOperationType.append, sfu1.add_proposals(0, [bot_kp])
        )
        assert cw is not None and cw.welcome is not None
        await ws.received_binary_message(sfu1.welcome_frame(2, 5, cw.welcome))
        self.assertTrue(conn.dave_session.ready)

        # Mid-call SFU rotation pushes a brand-new Op 25 while already in group.
        sfu2 = FakeDiscordMLSSFU(CHANNEL_ID)
        sent_binary.clear()
        await ws.received_binary_message(
            sfu2.external_sender_frame(3, sfu2.external_sender_bytes())
        )
        self.assertEqual(len(sent_binary), 1)
        self.assertEqual(sent_binary[0][0], DiscordVoiceWebSocket.MLS_KEY_PACKAGE)

        # Corrupted Welcome (tid=12) sends Op 31 + fresh Op 26 KeyPackage.
        sent_json.clear()
        sent_binary.clear()
        await ws.received_binary_message(sfu2.welcome_frame(4, 12, b"\xde\xad\xbe\xef"))
        self.assertIn(
            {
                "op": DiscordVoiceWebSocket.MLS_INVALID_COMMIT_WELCOME,
                "d": {"transition_id": 12},
            },
            sent_json,
        )
        self.assertEqual(len(sent_binary), 1)
        self.assertEqual(sent_binary[0][0], DiscordVoiceWebSocket.MLS_KEY_PACKAGE)

        # In-place recovery respects cooldown and sends Op 31 + Op 26.
        sent_json.clear()
        sent_binary.clear()
        conn._last_dave_recovery_mono = -999.0
        conn.request_inplace_dave_recovery()
        await asyncio.sleep(0.05)
        self.assertEqual(len(sent_json), 1)
        self.assertEqual(sent_json[0]["op"], DiscordVoiceWebSocket.MLS_INVALID_COMMIT_WELCOME)
        self.assertEqual(len(sent_binary), 1)
        self.assertEqual(sent_binary[0][0], DiscordVoiceWebSocket.MLS_KEY_PACKAGE)

        # Second call inside cooldown window is a no-op.
        conn.request_inplace_dave_recovery()
        await asyncio.sleep(0.05)
        self.assertEqual(len(sent_json), 1)


# ---------------------------------------------------------------------------
# 2. Pre-Welcome Packet Staging + DAVE AEAD SSRC Auto-Identification
# ---------------------------------------------------------------------------


class TestDaveStagingAndSSRCIdentification(unittest.IsolatedAsyncioTestCase):
    async def test_pre_welcome_packets_flush_on_welcome_and_unknown_ssrcs_auto_resolve(
        self,
    ) -> None:
        loop = asyncio.get_running_loop()
        vc, conn, ws, _, sent_binary = _make_harness(loop)
        sfu = FakeDiscordMLSSFU(CHANNEL_ID)
        ext = sfu.external_sender_bytes()
        key = os.urandom(32)

        now = 500.0
        buf = RollingAudioBuffer(guild_id=GUILD_ID, clock=lambda: now)
        buf.start_session(CHANNEL_ID)
        router = VoiceReceiveRouter(
            guild_id=GUILD_ID,
            buffer=buf,
            is_bot_user=lambda uid: uid in {BOT_ID, OTHER_BOT_ID},
            dave_state_getter=lambda: (conn.dave_session, conn.dave_protocol_version),
            dave_downgraded_getter=lambda: conn.dave_downgraded,
            dave_lock=conn.dave_lock,
            own_user_id_getter=lambda: BOT_ID,
            clock=lambda: now,
        )
        vc.on_dave_keys_updated = router.flush
        router.configure_transport("aead_aes256_gcm_rtpsize", key)

        # Alice is mapped via SPEAKING (ssrc=501); Bob (ssrc=602) NEVER sends SPEAKING;
        # OTHER_BOT (ssrc=703) also never sends SPEAKING and MUST be dropped as a bot!
        router.register_ssrc(user_id=ALICE_ID, ssrc=501)

        await ws.received_message(
            {
                "op": DiscordVoiceWebSocket.SESSION_DESCRIPTION,
                "d": {
                    "mode": "aead_aes256_gcm_rtpsize",
                    "secret_key": list(key),
                    "dave_protocol_version": 1,
                },
            }
        )
        bot_kp = sent_binary[-1][1]
        await ws.received_binary_message(sfu.external_sender_frame(1, ext))

        # Build an MLS group containing Alice, Bob, OtherBot, and Paradise.
        alice = davey.DaveSession(1, ALICE_ID, CHANNEL_ID)
        bob = davey.DaveSession(1, BOB_ID, CHANNEL_ID)
        other_bot = davey.DaveSession(1, OTHER_BOT_ID, CHANNEL_ID)
        for peer in (alice, bob, other_bot):
            peer.set_external_sender(ext)
        _ = alice.get_serialized_key_package()
        bob_kp = bob.get_serialized_key_package()
        obot_kp = other_bot.get_serialized_key_package()

        cw = alice.process_proposals(
            davey.ProposalsOperationType.append,
            sfu.add_proposals(0, [bot_kp, bob_kp, obot_kp]),
        )
        assert cw is not None and cw.welcome is not None
        alice.process_commit(cw.commit)
        alice.prepare_transition(1, 1)
        alice.execute_transition(1)
        for peer in (bob, other_bot):
            peer.process_welcome(cw.welcome)
            peer.prepare_transition(1, 1)
            peer.execute_transition(1)

        # BEFORE Op 30 MLS_WELCOME arrives at Paradise, Alice and Bob are ALREADY talking!
        for i in range(4):
            now += 0.02
            router.handle_udp_packet(
                _build_encrypted_rtp(
                    "aead_aes256_gcm_rtpsize",
                    key,
                    seq=10 + i,
                    ts=960 * (i + 1),
                    ssrc=501,
                    payload=alice.encrypt_opus(f"\x78ALICE_EARLY_{i}".encode()),
                    nonce_int=10 + i,
                )
            )
            router.handle_udp_packet(
                _build_encrypted_rtp(
                    "aead_aes256_gcm_rtpsize",
                    key,
                    seq=50 + i,
                    ts=960 * (i + 1),
                    ssrc=602,
                    payload=bob.encrypt_opus(f"\x78BOB_NO_OP5_{i}".encode()),
                    nonce_int=50 + i,
                )
            )
        self.assertEqual(router.packets_buffered, 0)

        # Now Op 30 MLS_WELCOME arrives -> BOTH Alice's pre-key packets AND Bob's
        # unmapped-SSRC packets flush, decrypt, and land in the buffer with ZERO loss!
        await ws.received_binary_message(sfu.welcome_frame(2, 1, cw.welcome))
        self.assertEqual(router.packets_buffered, 8)
        self.assertEqual(router.dave_ssrc_resolved, 1)
        self.assertEqual(router.dave_errors, 0)

        # OtherBot transmits on unmapped SSRC 703 -> identified as bot and filtered out!
        now += 0.02
        router.handle_udp_packet(
            _build_encrypted_rtp(
                "aead_aes256_gcm_rtpsize",
                key,
                seq=90,
                ts=960,
                ssrc=703,
                payload=other_bot.encrypt_opus(b"\x78BOT_NOISE"),
                nonce_int=90,
            )
        )
        self.assertEqual(router.packets_buffered, 8)

        # Forged 0xFAFA packet on unmapped SSRC 999 never falsely binds a user,
        # even when passthrough mode is temporarily enabled.
        conn.dave_session.set_passthrough_mode(True)
        forged = b"\x78FORGED" + b"\x00" * 8 + b"\x01\x0c\xfa\xfa"
        self.assertIsNone(
            try_identify_dave_sender(conn.dave_session, forged, own_user_id=BOT_ID)
        )

        snap = buf.snapshot(10)
        by_user = {s.user_id: [p.opus_data for p in s.packets] for s in snap.speakers}
        self.assertEqual(set(by_user.keys()), {ALICE_ID, BOB_ID})
        self.assertEqual(
            by_user[ALICE_ID],
            [f"\x78ALICE_EARLY_{i}".encode() for i in range(4)],
        )
        self.assertEqual(
            by_user[BOB_ID],
            [f"\x78BOB_NO_OP5_{i}".encode() for i in range(4)],
        )

        router.reset()
        self.assertEqual(len(router._staged_by_ssrc), 0)
        self.assertEqual(len(router._dave_wait_by_user), 0)


# ---------------------------------------------------------------------------
# 3. 2026 RTP Transport AEAD Modes, Padding/Extensions, RTCP, and Reconnect Guard
# ---------------------------------------------------------------------------


class TestTransportModesAndPacketFiltering(unittest.IsolatedAsyncioTestCase):
    def test_all_transport_modes_including_aes256_gcm_extensions_and_padding(
        self,
    ) -> None:
        self.assertIn("aead_aes256_gcm_rtpsize", SUPPORTED_TRANSPORT_MODES)
        self.assertIn("aead_xchacha20_poly1305_rtpsize", SUPPORTED_TRANSPORT_MODES)

        # Simulate a DAVE-encrypted payload ending with [0x01, 0x0c, 0xfa, 0xfa]
        # to prove RFC 3550 padding + RFC 5285 extensions never corrupt its tail!
        dave_like_payload = b"\x78VOICE_DATA" + b"\xaa" * 8 + b"\x01\x0c\xfa\xfa"
        self.assertTrue(is_dave_encrypted_frame(dave_like_payload))

        configs = [
            ("plain", None, b"", (), 0),
            ("one_byte_ext", 0xBEDE, b"\x10\x7f\x00\x00", (), 0),
            ("two_byte_ext", 0x1000, b"\x01\x02\xab\xcd\x02\x01\xef\x00", (), 0),
            ("ext_plus_csrc_plus_padding", 0xBEDE, b"\x10\x55\x21\xaa\xbb\x00\x00\x00", (11, 22), 8),
            ("padding_only", None, b"", (), 4),
        ]

        for mode in SUPPORTED_TRANSPORT_MODES:
            key = os.urandom(32)
            dec = TransportDecryptor(mode, key)
            for idx, (label, prof, ext_body, csrcs, pad) in enumerate(configs, start=1):
                with self.subTest(mode=mode, variant=label):
                    raw = _build_encrypted_rtp(
                        mode,
                        key,
                        seq=100 + idx,
                        ts=960 * idx,
                        ssrc=424242,
                        payload=dave_like_payload,
                        nonce_int=idx,
                        ext_profile=prof,
                        ext_body=ext_body,
                        csrcs=csrcs,
                        pad_bytes=pad,
                    )
                    pkt = dec.decrypt_rtp(raw)
                    self.assertIsNotNone(pkt)
                    assert pkt is not None
                    self.assertEqual(pkt.payload, dave_like_payload)
                    self.assertTrue(is_dave_encrypted_frame(pkt.payload))

        # Invalid RFC 3550 padding (pad_len == 0 or pad_len > len) is rejected cleanly.
        key = os.urandom(32)
        dec = TransportDecryptor("aead_aes256_gcm_rtpsize", key)
        hdr = struct.pack(">BBHII", 0xA0, 120, 1, 960, 424242)  # P=1
        nonce = struct.pack(">I", 99) + b"\x00" * 8
        for bad_plain in (b"\x78\x00\x00", b"\x78\x09"):
            bad_raw = hdr + AESGCM(key).encrypt(nonce, bad_plain, hdr) + nonce[:4]
            self.assertIsNone(dec.decrypt_rtp(bad_raw))

    def test_clip_voice_client_outbound_aes256_gcm_roundtrips_into_decryptor(
        self,
    ) -> None:
        key = os.urandom(32)
        vc = object.__new__(ClipVoiceClient)
        vc._incr_nonce = 0
        vc._connection = SimpleNamespace(secret_key=list(key))
        header = struct.pack(">BBHII", 0x80, 0x78, 7, 6720, 123456)
        datagram = vc._encrypt_aead_aes256_gcm_rtpsize(header, OPUS_SILENCE)
        pkt = TransportDecryptor("aead_aes256_gcm_rtpsize", key).decrypt_rtp(datagram)
        self.assertIsNotNone(pkt)
        assert pkt is not None
        self.assertEqual(pkt.payload, OPUS_SILENCE)

    def test_rtcp_video_and_keepalives_never_trigger_false_dark_pipe_reconnect(
        self,
    ) -> None:
        key = os.urandom(32)
        now = 100.0
        guild = SimpleNamespace(id=GUILD_ID, get_member=lambda _uid: None)
        bot = SimpleNamespace(user=SimpleNamespace(id=BOT_ID), get_guild=lambda _gid: guild)
        service = VoiceClipService(bot, VoiceClipConfig(enabled=True), clock=lambda: now)
        vc = SimpleNamespace(
            _connection=SimpleNamespace(
                dave_session=None,
                dave_protocol_version=0,
                dave_downgraded=True,
                dave_lock=threading.RLock(),
            )
        )
        router = service.attach_voice_client(
            guild=guild, channel_id=CHANNEL_ID, voice_client=vc
        )
        router.configure_transport("aead_aes256_gcm_rtpsize", key)

        # Flood 800 mixed RTCP SR/RR/SDES, keepalive, and video RTP datagrams.
        for i in range(200):
            # RTCP Sender Report (PT=200) and Receiver Report (PT=201)
            router.handle_udp_packet(struct.pack(">BBHI", 0x80, 200, 6, 12345) + b"\x00" * 20)
            router.handle_udp_packet(struct.pack(">BBHI", 0x81, 201, 7, 12345) + b"\x00" * 24)
            # 8-byte UDP keepalive echo
            router.handle_udp_packet(struct.pack(">Q", i))
            # Video RTP packet (PT=101 H264 / PT=103 AV1)
            router.handle_udp_packet(
                _build_encrypted_rtp(
                    "aead_aes256_gcm_rtpsize",
                    key,
                    seq=i,
                    ts=9000 * i,
                    ssrc=8888,
                    payload=b"\x99VIDEO_NAL",
                    nonce_int=i + 1,
                    payload_type=101,
                )
            )

        self.assertEqual(router.packets_received, 800)
        self.assertEqual(router.audio_packets_received, 0)
        self.assertEqual(router.transport_errors, 0)
        self.assertEqual(router.dave_errors, 0)
        # Quiet channel receiving RTCP/video MUST NOT be flagged broken!
        self.assertEqual(service.capture_recovery_candidates(), [])

        # Conversely, 500 genuinely unbuffered audio packets DO flag recovery!
        for i in range(500):
            router.handle_udp_packet(
                _build_encrypted_rtp(
                    "aead_aes256_gcm_rtpsize",
                    key,
                    seq=1000 + i,
                    ts=960 * i,
                    ssrc=99999,  # never mapped
                    payload=b"\x78UNMAPPED_AUDIO",
                    nonce_int=1000 + i,
                    payload_type=120,
                )
            )
        self.assertEqual(router.audio_packets_received, 500)
        self.assertEqual(service.capture_recovery_candidates(), [GUILD_ID])

    async def test_transient_null_channel_voice_state_update_preserves_active_buffer(
        self,
    ) -> None:
        now = 200.0
        guild = SimpleNamespace(
            id=GUILD_ID,
            get_channel=lambda cid: SimpleNamespace(id=cid, guild=guild),
            get_member=lambda _uid: None,
        )
        bot = SimpleNamespace(
            user=SimpleNamespace(id=BOT_ID),
            get_guild=lambda gid: guild if gid == GUILD_ID else None,
            voice_clip_service=None,
        )
        service = VoiceClipService(bot, VoiceClipConfig(enabled=True), clock=lambda: now)
        bot.voice_clip_service = service

        vc = object.__new__(ClipVoiceClient)
        vc.channel = SimpleNamespace(id=CHANNEL_ID, guild=guild)
        vc.client = bot
        vc._receive_router = None
        vc._listener_registered = False
        vc._keepalive_task = None
        vc._silence_task = None
        vc._keepalive_counter = 0
        vc._connection = SimpleNamespace(
            state=ConnectionFlowState.connected,
            dave_session=None,
            dave_protocol_version=0,
            dave_downgraded=True,
            dave_lock=threading.RLock(),
            connected_user_ids={ALICE_ID},
            voice_state_update=AsyncMock(),
            is_connected=lambda: True,
            add_socket_listener=lambda _cb: None,
            remove_socket_listener=lambda _cb: None,
        )
        vc._bind_clip_service(CHANNEL_ID)
        buf = service.get_buffer(GUILD_ID)
        assert buf is not None
        buf.append_packet(
            user_id=ALICE_ID,
            opus_data=b"\x78KEEP_ME",
            rtp_timestamp=960,
            sequence=1,
            ssrc=111,
            mono_time=now,
        )

        # Transient channel_id=None during gateway reconnect must NOT drop the buffer!
        await vc.on_voice_state_update({"channel_id": None})
        self.assertEqual(vc.channel.id, CHANNEL_ID)
        still_there = service.get_buffer(GUILD_ID)
        self.assertIs(still_there, buf)
        self.assertEqual(still_there.stats()["session_packets"], 1)

        # True disconnect state DOES tear down capture and wipe buffered packets.
        vc._connection.state = ConnectionFlowState.disconnected
        await vc.on_voice_state_update({"channel_id": None})
        self.assertIsNone(service.get_buffer(GUILD_ID))


# ---------------------------------------------------------------------------
# 4. Opus Codec (libopus + PyAV), Mixer, and Live UDP + DAVE 0.2 + AES-GCM -> M4A
# ---------------------------------------------------------------------------


class TestOpusMixerAndLiveDaveV2Export(unittest.IsolatedAsyncioTestCase):
    def test_libopus_and_pyav_decoders_match_exact_level_without_aliasing_or_spikes(
        self,
    ) -> None:
        self.assertTrue(ensure_opus_loaded())
        d0 = SpeakerOpusDecoder()
        self.assertIsNone(d0.decode_packet(b""))
        self.assertIsNotNone(d0.decode_packet(OPUS_SILENCE))

        for layout in ("mono", "stereo"):
            for frame_ms in (10, 20, 40, 60):
                pkts = _encode_opus_tone(440.0, 0.36, layout=layout, frame_ms=frame_ms)
                d_nat = SpeakerOpusDecoder(prefer_pyav=False)
                d_av = SpeakerOpusDecoder(prefer_pyav=True)
                self.assertEqual(d_nat.backend, "libopus")
                self.assertEqual(d_av.backend, "pyav")

                nat_frames = [d_nat.decode_packet(p) for p in pkts]
                av_frames = [d_av.decode_packet(p) for p in pkts]
                assert all(f is not None for f in nat_frames)
                assert all(f is not None for f in av_frames)

                # Verify returned arrays never alias subsequent decode calls.
                first_copy = nat_frames[0].copy()
                _ = d_nat.decode_plc()
                _ = d_nat.decode_fec(pkts[-1])
                _ = d_av.decode_plc()
                _ = d_av.decode_fec(pkts[-1])
                np.testing.assert_array_equal(nat_frames[0], first_copy)

                nat_pcm = np.concatenate(nat_frames)  # type: ignore[arg-type]
                av_pcm = np.concatenate(av_frames)  # type: ignore[arg-type]
                self.assertEqual(nat_pcm.shape, av_pcm.shape)
                self.assertLess(float(np.max(np.abs(nat_pcm - av_pcm))), 1e-4)

        # Verify built-in FFmpeg fltp "opus" mono<->stereo switches never exceed [-1, 1].
        mono_pkts = _encode_opus_tone(440.0, 0.1, amplitude=0.85, layout="mono")
        stereo_pkts = _encode_opus_tone(440.0, 0.1, amplitude=0.85, layout="stereo")
        fltp_ctx = av.CodecContext.create("opus", "r")
        resampler = None
        for pkt in (mono_pkts[0], mono_pkts[1], stereo_pkts[0], stereo_pkts[1], mono_pkts[2]):
            for frame in fltp_ctx.decode(av.Packet(pkt)):
                mono, resampler = _av_frame_to_mono_f32(frame, resampler)
                self.assertTrue(bool(np.all(np.isfinite(mono))))
                self.assertLessEqual(float(np.max(np.abs(mono))), 1.0)

    def test_mixer_caps_noise_floor_gain_and_reports_total_decode_failure(self) -> None:
        # Near-zero comfort noise is capped by MAX_STATIC_GAIN rather than blown up 500x.
        whisper = np.full(SAMPLE_RATE, 2e-4, dtype=np.float32)
        gain = apply_static_gain(whisper)
        self.assertAlmostEqual(gain, MAX_STATIC_GAIN, places=4)
        self.assertLess(float(np.max(np.abs(whisper))), 0.01)

        # NaN / Inf samples are sanitized and clamped below LIMITER_CEILING.
        dirty = np.array([0.1, float("nan"), float("inf"), float("-inf"), -0.1], dtype=np.float32)
        apply_static_gain(dirty)
        self.assertTrue(bool(np.all(np.isfinite(dirty))))
        self.assertLessEqual(float(np.max(np.abs(dirty))), LIMITER_CEILING + 1e-6)

        # Buffer containing ONLY corrupt/undecrypted non-silent payloads raises ClipExportError.
        now = 300.0
        buf = RollingAudioBuffer(guild_id=GUILD_ID, clock=lambda: now)
        buf.start_session(CHANNEL_ID)
        for i in range(5):
            now += 0.02
            # TOC byte with c=3 (arbitrary frame count) and missing count byte -> invalid Opus.
            buf.append_packet(
                user_id=ALICE_ID,
                opus_data=b"\x03",
                rtp_timestamp=960 * i,
                sequence=i,
                ssrc=111,
                mono_time=now,
            )
        snap = buf.snapshot(5)
        with self.assertRaises(ClipExportError):
            mix_snapshot_to_pcm_f32(snap)

    async def test_live_udp_aes256_gcm_plus_dave_v2_mls_multi_speaker_exports_valid_m4a(
        self,
    ) -> None:
        ffmpeg_exe = resolve_ffmpeg_executable()
        self.assertIsNotNone(ffmpeg_exe)
        assert ffmpeg_exe is not None
        self.assertTrue(os.access(ffmpeg_exe, os.X_OK))

        loop = asyncio.get_running_loop()
        sfu = FakeDiscordMLSSFU(CHANNEL_ID)
        ext = sfu.external_sender_bytes()
        transport_key = os.urandom(32)

        with tempfile.TemporaryDirectory():
            cfg = VoiceClipConfig(enabled=True)
            members = {
                ALICE_ID: SimpleNamespace(id=ALICE_ID, bot=False),
                BOB_ID: SimpleNamespace(id=BOB_ID, bot=False),
                OTHER_BOT_ID: SimpleNamespace(id=OTHER_BOT_ID, bot=True),
                BOT_ID: SimpleNamespace(id=BOT_ID, bot=True),
            }
            guild = SimpleNamespace(
                id=GUILD_ID,
                members=list(members.values()),
                get_member=lambda uid: members.get(uid),
                get_channel=lambda cid: SimpleNamespace(id=cid, guild=guild),
                voice_client=None,
            )
            bot = SimpleNamespace(
                user=SimpleNamespace(id=BOT_ID),
                get_guild=lambda gid: guild if gid == GUILD_ID else None,
                voice_clip_service=None,
                _connection=SimpleNamespace(
                    user=SimpleNamespace(id=BOT_ID),
                    loop=loop,
                ),
            )
            service = VoiceClipService(bot, cfg)
            bot.voice_clip_service = service

            # Real loopback UDP socket pair + real discord.py SocketReader thread!
            recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            recv_sock.bind(("127.0.0.1", 0))
            recv_sock.setblocking(False)
            recv_addr = recv_sock.getsockname()
            send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

            channel_obj = SimpleNamespace(
                id=CHANNEL_ID,
                guild=guild,
                _get_voice_client_key=lambda: (GUILD_ID, "guild_id"),
            )
            guild.get_channel = lambda cid: channel_obj if cid == CHANNEL_ID else None
            bot._connection._remove_voice_client = lambda _gid: None

            vc = ClipVoiceClient(bot, channel_obj)  # type: ignore[arg-type]
            guild.voice_client = vc
            conn = vc._connection
            self.assertIsInstance(conn, ClipVoiceConnectionState)
            conn.socket = recv_sock
            conn.ip = "127.0.0.1"
            conn.port = send_sock.getsockname()[1] or 9
            conn.state = ConnectionFlowState.connected

            reader = SocketReader(conn)
            conn._socket_reader = reader
            reader.start()

            sent_binary: list[tuple[int, bytes]] = []
            sent_json: list[dict] = []
            ws = object.__new__(ClipVoiceWebSocket)
            ws._connection = conn
            ws._hook = vc._on_voice_ws_message
            ws._max_heartbeat_timeout = 60.0
            ws.thread_id = threading.get_ident()
            ws.gateway = "wss://voice.discord.test"
            ws._keep_alive = None
            ws._close_code = None
            ws.secret_key = list(transport_key)
            ws.ssrc = 99999
            ws.seq_ack = -1

            async def _send_bin(opcode: int, data: bytes) -> None:
                sent_binary.append((opcode, bytes(data)))

            async def _send_json(payload: dict) -> None:
                sent_json.append(payload)

            async def _send_ready(tid: int) -> None:
                sent_json.append({"op": 23, "d": {"transition_id": tid}})

            async def _load_key(d: dict) -> None:
                conn.secret_key = ws.secret_key = d["secret_key"]

            async def _init_conn(d: dict) -> None:
                conn.ssrc = d["ssrc"]

            ws.send_binary = _send_bin
            ws.send_as_json = _send_json
            ws.send_transition_ready = _send_ready
            ws.load_secret_key = _load_key
            ws.initial_connection = _init_conn
            ws.speak = AsyncMock()
            conn.ws = ws

            try:
                # 1. Complete Voice Gateway v8 + DAVE v1 MLS handshake!
                await ws.received_message(
                    {"op": DiscordVoiceWebSocket.READY, "d": {"ssrc": 99999, "modes": ["aead_aes256_gcm_rtpsize"]}}
                )
                await ws.received_message(
                    {
                        "op": 11,
                        "d": {
                            "user_ids": [
                                str(ALICE_ID),
                                str(BOB_ID),
                                str(OTHER_BOT_ID),
                                str(BOT_ID),
                            ]
                        },
                    }
                )
                await ws.received_message(
                    {
                        "op": DiscordVoiceWebSocket.SESSION_DESCRIPTION,
                        "d": {
                            "mode": "aead_aes256_gcm_rtpsize",
                            "secret_key": list(transport_key),
                            "dave_protocol_version": 1,
                        },
                    }
                )
                bot_kp = sent_binary[-1][1]
                await ws.received_binary_message(sfu.external_sender_frame(1, ext))

                alice_dave = davey.DaveSession(1, ALICE_ID, CHANNEL_ID)
                bob_dave = davey.DaveSession(1, BOB_ID, CHANNEL_ID)
                obot_dave = davey.DaveSession(1, OTHER_BOT_ID, CHANNEL_ID)
                for s in (alice_dave, bob_dave, obot_dave):
                    s.set_external_sender(ext)
                _ = alice_dave.get_serialized_key_package()
                cw = alice_dave.process_proposals(
                    davey.ProposalsOperationType.append,
                    sfu.add_proposals(
                        0,
                        [
                            bot_kp,
                            bob_dave.get_serialized_key_package(),
                            obot_dave.get_serialized_key_package(),
                        ],
                    ),
                )
                assert cw is not None and cw.welcome is not None
                alice_dave.process_commit(cw.commit)
                alice_dave.prepare_transition(2, 1)
                alice_dave.execute_transition(2)
                for s in (bob_dave, obot_dave):
                    s.process_welcome(cw.welcome)
                    s.prepare_transition(2, 1)
                    s.execute_transition(2)

                await ws.received_binary_message(sfu.welcome_frame(2, 2, cw.welcome))
                await ws.received_message(
                    {"op": DiscordVoiceWebSocket.DAVE_EXECUTE_TRANSITION, "d": {"transition_id": 2}}
                )

                # Only Alice sends SPEAKING (Op 5); Bob is resolved purely via DAVE AEAD!
                await ws.received_message(
                    {
                        "op": DiscordVoiceWebSocket.SPEAKING,
                        "d": {"user_id": str(ALICE_ID), "ssrc": 1111, "speaking": 1},
                    }
                )
                await ws.received_message(
                    {
                        "op": DiscordVoiceWebSocket.SPEAKING,
                        "d": {"user_id": str(OTHER_BOT_ID), "ssrc": 3333, "speaking": 1},
                    }
                )

                # 2. Transmit real stereo 440 Hz (Alice) + mono 660 Hz (Bob) + bot 1200 Hz
                # + RTCP SR packets over the live UDP socket!
                alice_opus = _encode_opus_tone(440.0, 0.8, layout="stereo")
                bob_opus = _encode_opus_tone(660.0, 0.8, layout="mono")
                bot_opus = _encode_opus_tone(1200.0, 0.8, layout="mono")

                nonce_counter = 1
                for i in range(min(len(alice_opus), len(bob_opus), len(bot_opus))):
                    send_sock.sendto(
                        _build_encrypted_rtp(
                            "aead_aes256_gcm_rtpsize",
                            transport_key,
                            seq=i + 1,
                            ts=960 * (i + 1),
                            ssrc=1111,
                            payload=alice_dave.encrypt_opus(alice_opus[i]),
                            nonce_int=nonce_counter,
                            ext_profile=0xBEDE,
                            ext_body=b"\x10\x64\x00\x00",
                            pad_bytes=4 if i % 3 == 0 else 0,
                        ),
                        recv_addr,
                    )
                    nonce_counter += 1
                    send_sock.sendto(
                        _build_encrypted_rtp(
                            "aead_aes256_gcm_rtpsize",
                            transport_key,
                            seq=i + 1,
                            ts=960 * (i + 1),
                            ssrc=2222,  # never sent Op 5!
                            payload=bob_dave.encrypt_opus(bob_opus[i]),
                            nonce_int=nonce_counter,
                        ),
                        recv_addr,
                    )
                    nonce_counter += 1
                    send_sock.sendto(
                        _build_encrypted_rtp(
                            "aead_aes256_gcm_rtpsize",
                            transport_key,
                            seq=i + 1,
                            ts=960 * (i + 1),
                            ssrc=3333,
                            payload=obot_dave.encrypt_opus(bot_opus[i]),
                            nonce_int=nonce_counter,
                        ),
                        recv_addr,
                    )
                    nonce_counter += 1
                    if i % 5 == 0:
                        send_sock.sendto(
                            struct.pack(">BBHI", 0x80, 200, 6, 1111) + b"\x00" * 20,
                            recv_addr,
                        )
                    await asyncio.sleep(0.015)

                # Wait briefly for SocketReader thread to drain all datagrams.
                deadline = time.monotonic() + 2.0
                expected = 2 * min(len(alice_opus), len(bob_opus), len(bot_opus))
                while time.monotonic() < deadline:
                    if service.receiver_stats(GUILD_ID)["buffered"] >= expected:
                        break
                    await asyncio.sleep(0.01)

                health = service.capture_health(GUILD_ID)
                self.assertEqual(health["buffered"], expected)
                self.assertEqual(health["dave_errors"], 0)
                self.assertEqual(health["transport_errors"], 0)
                self.assertEqual(health["dave_protocol_version"], 1)
                self.assertTrue(health["dave_ready"])
                self.assertEqual(health["dave_ssrc_resolved"], 1)

                # 3. Export the live buffer to a real M4A file and verify its spectrum!
                clip, _chan = await service.export_clip_for_guild(
                    requester_user_id=AUTHORIZED_ID,
                    guild=guild,  # type: ignore[arg-type]
                    duration=5,
                )
                try:
                    self.assertTrue(os.path.isfile(clip.file_path))
                    self.assertGreater(clip.file_size_bytes, 2048)
                    self.assertGreater(clip.captured_seconds, 0.5)
                    self.assertEqual(clip.speaker_count, 2)

                    with av.open(clip.file_path) as container:
                        self.assertEqual(len(container.streams.audio), 1)
                        astream = container.streams.audio[0]
                        self.assertEqual(astream.codec_context.name, "aac")
                        self.assertEqual(astream.rate, SAMPLE_RATE)
                        self.assertEqual(astream.channels, 1)
                        decoded_chunks = [
                            f.to_ndarray().reshape(-1).astype(np.float32)
                            for f in container.decode(audio=0)
                        ]

                    pcm = np.concatenate(decoded_chunks)
                    self.assertTrue(bool(np.all(np.isfinite(pcm))))
                    self.assertGreater(float(np.max(np.abs(pcm))), 0.05)
                    self.assertLessEqual(float(np.max(np.abs(pcm))), 1.05)

                    spectrum = np.abs(np.fft.rfft(pcm))
                    freqs = np.fft.rfftfreq(pcm.size, d=1.0 / SAMPLE_RATE)

                    def _band_peak(center: float) -> float:
                        mask = (freqs >= center - 12.0) & (freqs <= center + 12.0)
                        return float(np.max(spectrum[mask]))

                    p440 = _band_peak(440.0)
                    p660 = _band_peak(660.0)
                    p1200 = _band_peak(1200.0)
                    noise = float(np.median(spectrum))
                    self.assertGreater(p440, noise * 50.0)
                    self.assertGreater(p660, noise * 50.0)
                    # Bot's 1200 Hz tone must be completely absent (< 5% of human tones).
                    self.assertLess(p1200, min(p440, p660) * 0.05)
                finally:
                    clip.cleanup()
            finally:
                reader.stop()
                vc.cleanup()
                recv_sock.close()
                send_sock.close()


if __name__ == "__main__":
    unittest.main()
