"""Discord RTP + Transport AEAD + DAVE E2EE voice receiver for discord.py 2.7+."""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
import logging
import struct
import threading
import time
from typing import Any

import davey
import discord
from discord.enums import SpeakingState
from discord.gateway import DiscordVoiceWebSocket
from discord.voice_state import VoiceConnectionState
import nacl.secret
from nacl.exceptions import CryptoError

from .buffer import RollingAudioBuffer
from .consent import ConsentManager
from .constants import MAX_OPUS_PACKET_BYTES, OPUS_SILENCE

log = logging.getLogger(__name__)

DISCORD_OPUS_PAYLOAD_TYPE: int = 120  # 0x78
VOICE_OP_READY: int = 2
VOICE_OP_SESSION_DESCRIPTION: int = 4
VOICE_OP_SPEAKING: int = 5
VOICE_OP_CLIENT_CONNECT: int = 11
VOICE_OP_VIDEO: int = 12
VOICE_OP_CLIENT_DISCONNECT: int = 13

MAX_SSRC_MAPPINGS: int = 256
MAX_STAGED_SSRCS: int = 16
MAX_STAGED_PACKETS_PER_SSRC: int = 25
STAGED_TTL_SECONDS: float = 2.0
UDP_KEEPALIVE_INTERVAL_SECONDS: float = 15.0

_RTP_FIXED_STRUCT = struct.Struct(">HII")


@dataclass(frozen=True, slots=True)
class ParsedRTPPacket:
    """Transport-decrypted RTP voice frame ready for speaker lookup + DAVE E2EE."""

    sequence: int
    rtp_timestamp: int
    ssrc: int
    payload: bytes


@dataclass(frozen=True, slots=True)
class _StagedFrame:
    mono_time: float
    sequence: int
    rtp_timestamp: int
    payload: bytes


class TransportDecryptor:
    """Decrypts outer Discord RTP transport encryption and strips RTP extensions."""

    SUPPORTED_MODES: tuple[str, ...] = (
        "aead_xchacha20_poly1305_rtpsize",
        "xsalsa20_poly1305_lite",
        "xsalsa20_poly1305_suffix",
        "xsalsa20_poly1305",
    )

    def __init__(self, mode: str, secret_key: bytes) -> None:
        if mode not in self.SUPPORTED_MODES:
            raise ValueError(f"Unsupported Discord voice transport mode: {mode}")
        if len(secret_key) != 32:
            raise ValueError("Discord voice transport secret key must be 32 bytes")
        self.mode = mode
        if mode.startswith("aead_"):
            self._aead: nacl.secret.Aead | None = nacl.secret.Aead(bytes(secret_key))
            self._sbox: nacl.secret.SecretBox | None = None
        else:
            self._aead = None
            self._sbox = nacl.secret.SecretBox(bytes(secret_key))

    def decrypt_rtp(self, datagram: bytes) -> ParsedRTPPacket | None:
        """Parse and decrypt one UDP datagram; returns None for non-Opus/RTCP frames."""
        length = len(datagram)
        if length < 16:
            return None
        # Ignore 74-byte Discord UDP IP discovery responses.
        if length == 74 and datagram[0] == 0x00 and datagram[1] == 0x02:
            return None

        b0 = datagram[0]
        b1 = datagram[1]
        # RTP v2 check
        if (b0 >> 6) != 2:
            return None
        # Ignore RTCP packets (payload types 200..204)
        if 200 <= b1 <= 204:
            return None
        # Only process Discord Opus audio frames (payload type 120)
        if (b1 & 0x7F) != DISCORD_OPUS_PAYLOAD_TYPE:
            return None

        padding = bool(b0 & 0x20)
        extended = bool(b0 & 0x10)
        cc = b0 & 0x0F
        base_header_len = 12 + 4 * cc
        if length <= base_header_len:
            return None

        sequence, rtp_timestamp, ssrc = _RTP_FIXED_STRUCT.unpack_from(datagram, 2)

        if self.mode == "aead_xchacha20_poly1305_rtpsize":
            aad_len = base_header_len + (4 if extended else 0)
            # Needs at least AAD + 16-byte AEAD tag + 4-byte nonce suffix.
            if length < aad_len + 20:
                return None
            aad = datagram[:aad_len]
            ciphertext = datagram[aad_len:-4]
            nonce = bytearray(24)
            nonce[:4] = datagram[-4:]
            assert self._aead is not None
            plaintext = self._aead.decrypt(ciphertext, aad, bytes(nonce))
            if padding and plaintext:
                pad_len = plaintext[-1]
                if 0 < pad_len <= len(plaintext):
                    plaintext = plaintext[:-pad_len]
            if extended:
                ext_words = struct.unpack_from(">H", aad, base_header_len + 2)[0]
                ext_bytes = ext_words * 4
                if len(plaintext) < ext_bytes:
                    return None
                plaintext = plaintext[ext_bytes:]
        else:
            assert self._sbox is not None
            if self.mode == "xsalsa20_poly1305_lite":
                if length < base_header_len + 20:
                    return None
                ciphertext = datagram[base_header_len:-4]
                nonce = bytearray(24)
                nonce[:4] = datagram[-4:]
                plaintext = self._sbox.decrypt(ciphertext, bytes(nonce))
            elif self.mode == "xsalsa20_poly1305_suffix":
                if length < base_header_len + 40:
                    return None
                ciphertext = datagram[base_header_len:-24]
                nonce_bytes = datagram[-24:]
                plaintext = self._sbox.decrypt(ciphertext, nonce_bytes)
            else:  # xsalsa20_poly1305
                if length < base_header_len + 16:
                    return None
                ciphertext = datagram[base_header_len:]
                nonce = bytearray(24)
                nonce[:12] = datagram[:12]
                plaintext = self._sbox.decrypt(ciphertext, bytes(nonce))

            if padding and plaintext:
                pad_len = plaintext[-1]
                if 0 < pad_len <= len(plaintext):
                    plaintext = plaintext[:-pad_len]
            if extended:
                if len(plaintext) < 4:
                    return None
                ext_words = struct.unpack_from(">H", plaintext, 2)[0]
                ext_total = 4 + ext_words * 4
                if len(plaintext) < ext_total:
                    return None
                plaintext = plaintext[ext_total:]

        if not plaintext or len(plaintext) > MAX_OPUS_PACKET_BYTES:
            return None

        return ParsedRTPPacket(
            sequence=sequence,
            rtp_timestamp=rtp_timestamp,
            ssrc=ssrc,
            payload=plaintext,
        )


class VoiceReceiveRouter:
    """Thread-safe packet decryptor, SSRC resolver and buffer feeder."""

    def __init__(
        self,
        *,
        guild_id: int,
        buffer: RollingAudioBuffer,
        consent: ConsentManager,
        is_bot_user: Callable[[int], bool] | None = None,
        dave_state_getter: Callable[[], tuple[davey.DaveSession | None, int]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.guild_id = int(guild_id)
        self.buffer = buffer
        self.consent = consent
        self._is_bot_user = is_bot_user or (lambda _uid: False)
        self._dave_state_getter = dave_state_getter or (lambda: (None, 0))
        self._clock = clock

        self._lock = threading.Lock()
        self._decryptor: TransportDecryptor | None = None
        self._own_ssrc: int | None = None
        self._ssrc_to_user: OrderedDict[int, int] = OrderedDict()
        self._user_to_ssrc: dict[int, int] = {}
        self._staged_by_ssrc: OrderedDict[int, deque[_StagedFrame]] = OrderedDict()

        # Privacy-safe counters (never store packet payloads or keys)
        self.packets_received: int = 0
        self.packets_buffered: int = 0
        self.transport_errors: int = 0
        self.dave_errors: int = 0

    def configure_transport(
        self,
        mode: str,
        secret_key: bytes,
        *,
        own_ssrc: int | None = None,
    ) -> None:
        decryptor = TransportDecryptor(mode, secret_key)
        with self._lock:
            self._decryptor = decryptor
            if own_ssrc is not None:
                self._own_ssrc = int(own_ssrc)

    def set_own_ssrc(self, ssrc: int) -> None:
        with self._lock:
            self._own_ssrc = int(ssrc)

    def register_ssrc(self, user_id: int, ssrc: int) -> None:
        """Map ``ssrc -> user_id`` and flush any recently staged UDP frames."""
        uid = int(user_id)
        ssrc_int = int(ssrc)
        now = self._clock()
        staged_frames: list[_StagedFrame] = []

        with self._lock:
            if self._own_ssrc is not None and ssrc_int == self._own_ssrc:
                return
            old_ssrc = self._user_to_ssrc.get(uid)
            if old_ssrc is not None and old_ssrc != ssrc_int:
                self._ssrc_to_user.pop(old_ssrc, None)
            self._user_to_ssrc[uid] = ssrc_int
            self._ssrc_to_user[ssrc_int] = uid
            self._ssrc_to_user.move_to_end(ssrc_int)
            while len(self._ssrc_to_user) > MAX_SSRC_MAPPINGS:
                evicted_ssrc, evicted_uid = self._ssrc_to_user.popitem(last=False)
                if self._user_to_ssrc.get(evicted_uid) == evicted_ssrc:
                    self._user_to_ssrc.pop(evicted_uid, None)

            staged_q = self._staged_by_ssrc.pop(ssrc_int, None)
            if staged_q:
                cutoff = now - STAGED_TTL_SECONDS
                staged_frames = [f for f in staged_q if f.mono_time >= cutoff]

        if not staged_frames:
            return
        if self._is_bot_user(uid) or not self.consent.can_record(self.guild_id, uid):
            return

        for frame in staged_frames:
            opus_bytes = self._unwrap_dave(uid, frame.payload)
            if opus_bytes is not None:
                if self.buffer.append_packet(
                    user_id=uid,
                    opus_data=opus_bytes,
                    rtp_timestamp=frame.rtp_timestamp,
                    sequence=frame.sequence,
                    ssrc=ssrc_int,
                    mono_time=frame.mono_time,
                ):
                    self.packets_buffered += 1

    def remove_user(self, user_id: int) -> None:
        uid = int(user_id)
        with self._lock:
            ssrc = self._user_to_ssrc.pop(uid, None)
            if ssrc is not None:
                self._ssrc_to_user.pop(ssrc, None)
                self._staged_by_ssrc.pop(ssrc, None)
        self.buffer.mark_speaker_discontinuity(uid)

    def reset(self) -> None:
        with self._lock:
            self._decryptor = None
            self._own_ssrc = None
            self._ssrc_to_user.clear()
            self._user_to_ssrc.clear()
            self._staged_by_ssrc.clear()

    def handle_udp_packet(self, datagram: bytes) -> None:
        """Non-blocking callback invoked on ``SocketReader`` thread per UDP packet."""
        arrival_mono = self._clock()
        self.packets_received += 1

        with self._lock:
            decryptor = self._decryptor
            own_ssrc = self._own_ssrc

        if decryptor is None:
            return

        try:
            parsed = decryptor.decrypt_rtp(datagram)
        except CryptoError:
            self.transport_errors += 1
            return
        except Exception:  # noqa: BLE001 - malformed network input must never crash reader
            self.transport_errors += 1
            return

        if parsed is None:
            return
        if own_ssrc is not None and parsed.ssrc == own_ssrc:
            return

        with self._lock:
            user_id = self._ssrc_to_user.get(parsed.ssrc)
            if user_id is None:
                # Ignore pre-SPEAKING silence bursts; stage speech frames briefly.
                if parsed.payload != OPUS_SILENCE:
                    self._stage_unknown_ssrc_unlocked(parsed, arrival_mono)
                return

        if self._is_bot_user(user_id):
            return
        if not self.consent.can_record(self.guild_id, user_id):
            return

        opus_bytes = self._unwrap_dave(user_id, parsed.payload)
        if opus_bytes is None:
            return

        if self.buffer.append_packet(
            user_id=user_id,
            opus_data=opus_bytes,
            rtp_timestamp=parsed.rtp_timestamp,
            sequence=parsed.sequence,
            ssrc=parsed.ssrc,
            mono_time=arrival_mono,
        ):
            self.packets_buffered += 1

    def _stage_unknown_ssrc_unlocked(
        self,
        parsed: ParsedRTPPacket,
        arrival_mono: float,
    ) -> None:
        cutoff = arrival_mono - STAGED_TTL_SECONDS
        stale_keys = [
            k
            for k, q in self._staged_by_ssrc.items()
            if not q or q[-1].mono_time < cutoff
        ]
        for k in stale_keys:
            self._staged_by_ssrc.pop(k, None)

        q = self._staged_by_ssrc.get(parsed.ssrc)
        if q is None:
            while len(self._staged_by_ssrc) >= MAX_STAGED_SSRCS:
                self._staged_by_ssrc.popitem(last=False)
            q = deque(maxlen=MAX_STAGED_PACKETS_PER_SSRC)
            self._staged_by_ssrc[parsed.ssrc] = q
        else:
            self._staged_by_ssrc.move_to_end(parsed.ssrc)

        q.append(
            _StagedFrame(
                mono_time=arrival_mono,
                sequence=parsed.sequence,
                rtp_timestamp=parsed.rtp_timestamp,
                payload=parsed.payload,
            )
        )

    def _unwrap_dave(self, user_id: int, transport_payload: bytes) -> bytes | None:
        """Unwrap Discord DAVE E2EE audio payload when active on the voice session."""
        if transport_payload == OPUS_SILENCE:
            return OPUS_SILENCE

        dave_session, dave_version = self._dave_state_getter()
        if dave_session is None:
            return transport_payload

        if dave_version > 0:
            if not dave_session.ready:
                try:
                    if dave_session.can_passthrough(user_id):
                        return dave_session.decrypt(
                            user_id, davey.MediaType.audio, transport_payload
                        )
                except Exception:  # noqa: BLE001
                    self.dave_errors += 1
                return None
            try:
                decrypted = dave_session.decrypt(
                    user_id, davey.MediaType.audio, transport_payload
                )
                return decrypted if decrypted else None
            except Exception:  # noqa: BLE001
                self.dave_errors += 1
                return None

        # Protocol version 0 (downgraded / passthrough): if a session still has
        # active passthrough decryptors, run through it, else pass raw Opus.
        if dave_session.ready:
            try:
                if dave_session.can_passthrough(user_id):
                    return dave_session.decrypt(
                        user_id, davey.MediaType.audio, transport_payload
                    )
            except Exception:  # noqa: BLE001
                log.debug("Passthrough DAVE decrypt fallback to raw Opus")
        return transport_payload


class ClipVoiceClient(discord.VoiceClient):
    """``discord.VoiceClient`` subclass adding non-blocking RTP/DAVE voice reception."""

    def __init__(self, client: discord.Client, channel: discord.abc.Connectable) -> None:
        self._receive_router: VoiceReceiveRouter | None = None
        self._listener_registered: bool = False
        self._keepalive_task: asyncio.Task[None] | None = None
        self._silence_task: asyncio.Task[None] | None = None
        self._keepalive_counter: int = 0
        super().__init__(client, channel)
        self._bind_clip_service(getattr(channel, "id", None))

    def create_connection_state(self) -> VoiceConnectionState:
        return VoiceConnectionState(self, hook=self._on_voice_ws_message)

    def _bind_clip_service(self, channel_id: int | None) -> None:
        service = getattr(self.client, "voice_clip_service", None)
        guild = getattr(self.channel, "guild", None)
        if service is None or guild is None or channel_id is None:
            return
        if not service.enabled:
            return
        self._receive_router = service.attach_voice_client(
            guild=guild,
            channel_id=int(channel_id),
            voice_client=self,
        )
        self._ensure_socket_listener()

    def _ensure_socket_listener(self) -> None:
        if self._receive_router is None:
            return
        if not self._listener_registered:
            self._connection.add_socket_listener(self._on_udp_packet)
            self._listener_registered = True
        if self._connection.socket is not discord.utils.MISSING:
            reader = getattr(self._connection, "_socket_reader", None)
            if reader is not None:
                reader.resume()

    def _remove_socket_listener(self) -> None:
        if self._listener_registered:
            try:
                self._connection.remove_socket_listener(self._on_udp_packet)
            except Exception:  # noqa: BLE001
                log.debug("Socket listener already removed", exc_info=True)
            self._listener_registered = False

    def _on_udp_packet(self, data: bytes) -> None:
        router = self._receive_router
        if router is not None:
            router.handle_udp_packet(data)

    async def on_voice_state_update(self, data: Any) -> None:
        old_channel_id = getattr(self.channel, "id", None)
        await super().on_voice_state_update(data)
        raw_cid = data.get("channel_id") if isinstance(data, dict) else None
        if raw_cid is None:
            self._teardown_recording()
            return
        new_channel_id = int(raw_cid)
        if new_channel_id != old_channel_id:
            if self._receive_router is not None:
                self._receive_router.reset()
            self._bind_clip_service(new_channel_id)

    async def _on_voice_ws_message(
        self,
        ws: DiscordVoiceWebSocket,
        msg: dict[str, Any],
    ) -> None:
        op = msg.get("op")
        data = msg.get("d")
        if not isinstance(data, dict):
            return

        if self._receive_router is None:
            self._bind_clip_service(getattr(self.channel, "id", None))
        router = self._receive_router
        if router is None:
            return

        if op == VOICE_OP_READY:
            ssrc = data.get("ssrc")
            if isinstance(ssrc, int):
                router.set_own_ssrc(ssrc)

        elif op == VOICE_OP_SESSION_DESCRIPTION:
            mode = data.get("mode") or self._connection.mode
            secret_key = ws.secret_key or self._connection.secret_key
            if isinstance(mode, str) and secret_key:
                try:
                    router.configure_transport(
                        mode,
                        bytes(secret_key),
                        own_ssrc= (
                            self._connection.ssrc
                            if isinstance(self._connection.ssrc, int)
                            else None
                        ),
                    )
                    self._ensure_socket_listener()
                    self._start_keepalive()
                    self._schedule_sfu_prime()
                except ValueError:
                    log.warning("Unsupported voice transport mode for clip capture: %s", mode)

        elif op == VOICE_OP_SPEAKING:
            raw_uid = data.get("user_id")
            raw_ssrc = data.get("ssrc")
            if raw_uid is not None and isinstance(raw_ssrc, int):
                try:
                    router.register_ssrc(int(raw_uid), raw_ssrc)
                except (TypeError, ValueError):
                    log.debug("Invalid SPEAKING user_id payload")

        elif op == VOICE_OP_VIDEO:
            raw_uid = data.get("user_id")
            raw_ssrc = data.get("audio_ssrc")
            if raw_uid is not None and isinstance(raw_ssrc, int) and raw_ssrc > 0:
                try:
                    router.register_ssrc(int(raw_uid), raw_ssrc)
                except (TypeError, ValueError):
                    log.debug("Invalid VIDEO user_id payload")

        elif op == VOICE_OP_CLIENT_CONNECT:
            raw_uid = data.get("user_id")
            raw_ssrc = data.get("audio_ssrc")
            if raw_uid is not None and isinstance(raw_ssrc, int) and raw_ssrc > 0:
                try:
                    router.register_ssrc(int(raw_uid), raw_ssrc)
                except (TypeError, ValueError):
                    log.debug("Invalid CLIENT_CONNECT payload")

        elif op == VOICE_OP_CLIENT_DISCONNECT:
            raw_uid = data.get("user_id")
            if raw_uid is not None:
                try:
                    router.remove_user(int(raw_uid))
                except (TypeError, ValueError):
                    log.debug("Invalid CLIENT_DISCONNECT payload")

    def _active_loop(self) -> asyncio.AbstractEventLoop:
        if isinstance(self.loop, asyncio.AbstractEventLoop):
            return self.loop
        return asyncio.get_running_loop()

    def _schedule_sfu_prime(self) -> None:
        if self._silence_task is not None and not self._silence_task.done():
            self._silence_task.cancel()
        guild_id = getattr(getattr(self.channel, "guild", None), "id", 0)
        self._silence_task = self._active_loop().create_task(
            self._send_initial_silence(),
            name=f"paradise-vc-prime-{guild_id}",
        )

    async def _send_initial_silence(self) -> None:
        """Send 5 Opus silence frames so Discord's SFU opens incoming RTP streams."""
        try:
            await asyncio.sleep(0.15)
            if not self.is_connected() or self.is_playing():
                return
            ws = self._connection.ws
            if ws is not discord.utils.MISSING and ws is not None:
                await ws.speak(SpeakingState.voice)
            for _ in range(5):
                if not self.is_connected() or self.is_playing():
                    return
                self.send_audio_packet(OPUS_SILENCE, encode=False)
                await asyncio.sleep(0.02)
            if ws is not discord.utils.MISSING and ws is not None and not self.is_playing():
                await ws.speak(SpeakingState.none)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            log.debug("Initial SFU silence prime skipped", exc_info=True)

    def _start_keepalive(self) -> None:
        if self._keepalive_task is not None and not self._keepalive_task.done():
            return
        guild_id = getattr(getattr(self.channel, "guild", None), "id", 0)
        self._keepalive_task = self._active_loop().create_task(
            self._udp_keepalive_loop(),
            name=f"paradise-vc-keepalive-{guild_id}",
        )

    async def _udp_keepalive_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(UDP_KEEPALIVE_INTERVAL_SECONDS)
                if not self.is_connected():
                    continue
                sock = self._connection.socket
                if not sock or sock is discord.utils.MISSING:
                    continue
                try:
                    self._keepalive_counter = (self._keepalive_counter + 1) & 0xFFFFFFFFFFFFFFFF
                    self._connection.send_packet(
                        self._keepalive_counter.to_bytes(8, "big")
                    )
                except OSError:
                    log.debug("Voice UDP keepalive send skipped", exc_info=True)
        except asyncio.CancelledError:
            return

    def _teardown_recording(self) -> None:
        self._remove_socket_listener()
        for task in (self._silence_task, self._keepalive_task):
            if task is not None and not task.done():
                task.cancel()
        self._silence_task = None
        self._keepalive_task = None
        if self._receive_router is not None:
            self._receive_router.reset()
            self._receive_router = None
        service = getattr(self.client, "voice_clip_service", None)
        guild = getattr(self.channel, "guild", None)
        if service is not None and guild is not None:
            service.detach_guild(guild.id)

    async def disconnect(self, *, force: bool = False) -> None:
        self._teardown_recording()
        await super().disconnect(force=force)

    def cleanup(self) -> None:
        self._teardown_recording()
        super().cleanup()
