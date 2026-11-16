"""Discord RTP + Transport AEAD + DAVE E2EE voice receiver for discord.py 2.7+."""

from __future__ import annotations

import asyncio
import logging
import struct
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    _HAS_AES_GCM = True
except ImportError:  # pragma: no cover - cryptography is a core runtime dep
    AESGCM = None  # type: ignore[assignment,misc]
    InvalidTag = CryptoError = Exception  # type: ignore[assignment,misc]
    _HAS_AES_GCM = False

import davey
import discord
import nacl.secret
from discord import opus
from discord import utils as discord_utils
from discord.enums import SpeakingState
from discord.gateway import DiscordVoiceWebSocket, VoiceKeepAliveHandler
from discord.voice_state import ConnectionFlowState, VoiceConnectionState
from nacl.exceptions import CryptoError as NaclCryptoError

from . import dave_compat
from .buffer import RollingAudioBuffer
from .constants import MAX_OPUS_PACKET_BYTES, OPUS_SILENCE

log = logging.getLogger(__name__)

DISCORD_OPUS_PAYLOAD_TYPE: int = 120  # 0x78
VOICE_OP_READY: int = 2
VOICE_OP_SESSION_DESCRIPTION: int = 4
VOICE_OP_SPEAKING: int = 5
VOICE_OP_RESUMED: int = 9
VOICE_OP_CLIENT_CONNECT: int = 11  # CLIENTS_CONNECT in Voice Gateway v8
VOICE_OP_VIDEO: int = 12
VOICE_OP_CLIENT_DISCONNECT: int = 13
VOICE_OP_DAVE_PREPARE_TRANSITION: int = dave_compat.DAVE_PREPARE_TRANSITION_OP
VOICE_OP_DAVE_EXECUTE_TRANSITION: int = dave_compat.DAVE_EXECUTE_TRANSITION_OP
VOICE_OP_DAVE_PREPARE_EPOCH: int = dave_compat.DAVE_PREPARE_EPOCH_OP

# Log DAVE decryption failure streaks at bounded milestones (~1s per 50 packets).
DAVE_FAILURE_LOG_STREAKS: tuple[int, ...] = (50, 500, 5_000)
# Consecutive post-ready DAVE decryption failures before requesting an in-place
# MLS session re-init (op 31 + op 26) without dropping the voice connection.
DAVE_INPLACE_RECOVERY_STREAK: int = 50
DAVE_INPLACE_RECOVERY_COOLDOWN_SECONDS: float = 15.0

MAX_SSRC_MAPPINGS: int = 256
MAX_STAGED_SSRCS: int = 16
# Up to 3 s of 20 ms packets per unmapped SSRC so speech already in progress
# when the bot joins/moves is recovered retroactively once mapped.
MAX_STAGED_PACKETS_PER_SSRC: int = 150
STAGED_TTL_SECONDS: float = 15.0
SSRC_DAVE_PROBE_INTERVAL_SECONDS: float = 0.25

# Short pre-MLS buffer for speakers whose first DAVE-encrypted frames arrive a
# few milliseconds before MLS_WELCOME (op 30) or MLS_ANNOUNCE_COMMIT_TRANSITION
# (op 29) installs their decryptor ratchet.
MAX_DAVE_WAIT_USERS: int = 16
MAX_DAVE_WAIT_PACKETS_PER_USER: int = 100
DAVE_WAIT_TTL_SECONDS: float = 4.0

UDP_KEEPALIVE_INTERVAL_SECONDS: float = 15.0

_RTP_FIXED_STRUCT = struct.Struct(">HII")

_TRANSPORT_CRYPTO_ERRORS: tuple[type[BaseException], ...] = (
    (NaclCryptoError, InvalidTag) if _HAS_AES_GCM else (NaclCryptoError,)
)


def _build_supported_modes() -> tuple[str, ...]:
    modes: list[str] = []
    if _HAS_AES_GCM:
        modes.append("aead_aes256_gcm_rtpsize")
    modes.extend(
        (
            "aead_xchacha20_poly1305_rtpsize",
            "xsalsa20_poly1305_lite",
            "xsalsa20_poly1305_suffix",
            "xsalsa20_poly1305",
        )
    )
    return tuple(modes)


SUPPORTED_TRANSPORT_MODES: tuple[str, ...] = _build_supported_modes()


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
    ssrc: int
    payload: bytes


def _strip_rtp_padding(plaintext: bytes, min_prefix_bytes: int = 0) -> bytes | None:
    """Strip RFC 3550 §5.1 padding without allowing underflow into headers."""
    total = len(plaintext)
    if total <= min_prefix_bytes:
        return None
    pad_len = plaintext[-1]
    if pad_len == 0 or pad_len > (total - min_prefix_bytes):
        return None
    return plaintext[: total - pad_len]


class TransportDecryptor:
    """Decrypts outer Discord RTP transport encryption and strips RTP extensions.

    Supports both current Discord Voice v8 AEAD transport modes
    (``aead_aes256_gcm_rtpsize`` and ``aead_xchacha20_poly1305_rtpsize``) as
    well as legacy XSalsa20 modes for backwards compatibility.
    """

    SUPPORTED_MODES: tuple[str, ...] = SUPPORTED_TRANSPORT_MODES

    def __init__(self, mode: str, secret_key: bytes) -> None:
        if mode not in self.SUPPORTED_MODES:
            raise ValueError(f"Unsupported Discord voice transport mode: {mode}")
        key_bytes = bytes(secret_key)
        if len(key_bytes) != 32:
            raise ValueError("Discord voice transport secret key must be 32 bytes")
        self.mode = mode
        self._aesgcm: Any = None
        self._aead: nacl.secret.Aead | None = None
        self._sbox: nacl.secret.SecretBox | None = None

        if mode == "aead_aes256_gcm_rtpsize":
            assert AESGCM is not None
            self._aesgcm = AESGCM(key_bytes)
        elif mode == "aead_xchacha20_poly1305_rtpsize":
            self._aead = nacl.secret.Aead(key_bytes)
        else:
            self._sbox = nacl.secret.SecretBox(key_bytes)

    def decrypt_rtp(self, datagram: bytes) -> ParsedRTPPacket | None:
        """Parse and decrypt one UDP datagram; returns None for non-Opus/RTCP frames."""
        length = len(datagram)
        if length < 16:
            return None
        # Ignore 74-byte Discord UDP IP discovery packets (0x0001 / 0x0002).
        if length == 74 and datagram[0] == 0x00 and datagram[1] in (0x01, 0x02):
            return None

        b0 = datagram[0]
        b1 = datagram[1]
        # RTP version 2 check.
        if (b0 >> 6) != 2:
            return None
        # Ignore multiplexed RTCP packets (RFC 5761: packet types 200..209).
        # Opus with marker bit M=1 has b1 == 0xF8 (248), safely above this range.
        if 200 <= b1 <= 209:
            return None
        # Filter strictly to Discord Opus audio (payload type 120), excluding
        # video/screenshare/RTX streams (payload types 101..110).
        if (b1 & 0x7F) != DISCORD_OPUS_PAYLOAD_TYPE:
            return None

        padding = bool(b0 & 0x20)
        extended = bool(b0 & 0x10)
        cc = b0 & 0x0F
        base_header_len = 12 + 4 * cc
        if length <= base_header_len:
            return None

        sequence, rtp_timestamp, ssrc = _RTP_FIXED_STRUCT.unpack_from(datagram, 2)

        if self.mode in ("aead_aes256_gcm_rtpsize", "aead_xchacha20_poly1305_rtpsize"):
            # In *_rtpsize modes the unencrypted AAD covers the fixed header,
            # CSRC list, and the 4-byte extension preamble (profile + length
            # in 32-bit words, both RFC 8285 0xBEDE one-byte and 0x100x
            # two-byte headers); the extension body itself is encrypted.
            aad_len = base_header_len + (4 if extended else 0)
            # Needs at least AAD + 16-byte AEAD auth tag + 4-byte nonce suffix.
            if length < aad_len + 20:
                return None
            aad = datagram[:aad_len]
            ciphertext = datagram[aad_len:-4]
            if self.mode == "aead_aes256_gcm_rtpsize":
                nonce12 = bytearray(12)
                nonce12[:4] = datagram[-4:]
                plaintext = self._aesgcm.decrypt(bytes(nonce12), ciphertext, aad)
            else:
                nonce24 = bytearray(24)
                nonce24[:4] = datagram[-4:]
                assert self._aead is not None
                plaintext = self._aead.decrypt(ciphertext, aad, bytes(nonce24))

            ext_bytes = 0
            if extended:
                ext_words = struct.unpack_from(">H", aad, base_header_len + 2)[0]
                ext_bytes = ext_words * 4
                if len(plaintext) < ext_bytes:
                    return None
            if padding:
                stripped = _strip_rtp_padding(plaintext, min_prefix_bytes=ext_bytes)
                if stripped is None:
                    return None
                plaintext = stripped
            if ext_bytes:
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

            ext_total = 0
            if extended:
                if len(plaintext) < 4:
                    return None
                ext_words = struct.unpack_from(">H", plaintext, 2)[0]
                ext_total = 4 + ext_words * 4
                if len(plaintext) < ext_total:
                    return None
            if padding:
                stripped = _strip_rtp_padding(plaintext, min_prefix_bytes=ext_total)
                if stripped is None:
                    return None
                plaintext = stripped
            if ext_total:
                plaintext = plaintext[ext_total:]

        if not plaintext or len(plaintext) > MAX_OPUS_PACKET_BYTES:
            return None

        return ParsedRTPPacket(
            sequence=sequence,
            rtp_timestamp=rtp_timestamp,
            ssrc=ssrc,
            payload=plaintext,
        )


def _is_missing_decryptor_error(err: BaseException) -> bool:
    text = str(err)
    return "NoDecryptorForUser" in text or "MissingKeyRatchet" in text


class VoiceReceiveRouter:
    """Thread-safe packet decryptor, SSRC resolver and rolling-buffer feeder."""

    def __init__(
        self,
        *,
        guild_id: int,
        buffer: RollingAudioBuffer,
        is_bot_user: Callable[[int], bool] | None = None,
        dave_state_getter: Callable[[], tuple[davey.DaveSession | None, int]] | None = None,
        dave_downgraded_getter: Callable[[], bool] | None = None,
        dave_lock: threading.RLock | None = None,
        dave_recovery_callback: Callable[[], None] | None = None,
        own_user_id_getter: Callable[[], int | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.guild_id = int(guild_id)
        self.buffer = buffer
        self._is_bot_user = is_bot_user or (lambda _uid: False)
        self._dave_state_getter = dave_state_getter or (lambda: (None, 0))
        self._dave_downgraded_getter = dave_downgraded_getter or (lambda: False)
        self._dave_lock = dave_lock or threading.RLock()
        self._dave_recovery_callback = dave_recovery_callback
        self._own_user_id_getter = own_user_id_getter or (lambda: None)
        self._clock = clock

        self._lock = threading.Lock()
        self._decryptor: TransportDecryptor | None = None
        self._own_ssrc: int | None = None
        self._ssrc_to_user: OrderedDict[int, int] = OrderedDict()
        self._user_to_ssrc: dict[int, int] = {}
        self._staged_by_ssrc: OrderedDict[int, deque[_StagedFrame]] = OrderedDict()
        self._ssrc_last_probe_mono: OrderedDict[int, float] = OrderedDict()
        self._dave_wait_by_user: OrderedDict[int, deque[_StagedFrame]] = OrderedDict()

        # Privacy-safe counters (never store packet payloads or keys).
        self.packets_received: int = 0
        self.audio_packets_received: int = 0
        self.packets_buffered: int = 0
        self.transport_errors: int = 0
        self.dave_errors: int = 0
        self.dave_not_ready_drops: int = 0
        self.dave_ssrc_resolved: int = 0
        self.non_silence_buffered: int = 0
        self.dave_fail_streak: int = 0
        self.dave_fail_since_success: int = 0
        self.last_dave_success_mono: float | None = None
        self.dave_ever_succeeded: bool = False
        self._first_packet_logged: bool = False

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
        log.info(
            "Voice receiver transport ready for guild %s (mode=%s)",
            self.guild_id,
            mode,
        )

    def has_transport(self) -> bool:
        with self._lock:
            return self._decryptor is not None

    def dave_status(self) -> tuple[int, bool]:
        """Return ``(protocol_version, session_ready)`` for health reporting."""
        session, version = self._dave_state_getter()
        if session is None:
            return int(version or 0), int(version or 0) == 0
        with self._dave_lock:
            ready = bool(getattr(session, "ready", False))
        return int(version or 0), ready

    def inherit_from(self, old: VoiceReceiveRouter | None) -> None:
        """Carry live capture state over from a previous router across rebinds."""
        if old is None or old is self:
            return
        with self._lock:
            with old._lock:
                self._decryptor = old._decryptor
                self._own_ssrc = old._own_ssrc
                self._ssrc_to_user = OrderedDict(old._ssrc_to_user)
                self._user_to_ssrc = dict(old._user_to_ssrc)
                self._staged_by_ssrc = OrderedDict(
                    (ssrc, deque(q, maxlen=MAX_STAGED_PACKETS_PER_SSRC))
                    for ssrc, q in old._staged_by_ssrc.items()
                )
                self._dave_wait_by_user = OrderedDict(
                    (uid, deque(q, maxlen=MAX_DAVE_WAIT_PACKETS_PER_USER))
                    for uid, q in old._dave_wait_by_user.items()
                )
                self.packets_received = old.packets_received
                self.audio_packets_received = old.audio_packets_received
                self.packets_buffered = old.packets_buffered
                self.transport_errors = old.transport_errors
                self.dave_errors = old.dave_errors
                self.dave_not_ready_drops = old.dave_not_ready_drops
                self.dave_ssrc_resolved = old.dave_ssrc_resolved
                self.non_silence_buffered = old.non_silence_buffered
                self.dave_fail_streak = old.dave_fail_streak
                self.dave_fail_since_success = old.dave_fail_since_success
                self.last_dave_success_mono = old.last_dave_success_mono
                self.dave_ever_succeeded = old.dave_ever_succeeded
                self._first_packet_logged = old._first_packet_logged
        log.info(
            "Voice receiver state inherited for guild %s (%d SSRC mappings)",
            self.guild_id,
            len(self._ssrc_to_user),
        )

    def set_own_ssrc(self, ssrc: int) -> None:
        with self._lock:
            self._own_ssrc = int(ssrc)

    def _bind_ssrc_unlocked(
        self, uid: int, ssrc_int: int, now: float
    ) -> list[_StagedFrame]:
        if self._own_ssrc is not None and ssrc_int == self._own_ssrc:
            return []
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

        self._ssrc_last_probe_mono.pop(ssrc_int, None)
        staged_q = self._staged_by_ssrc.pop(ssrc_int, None)
        if not staged_q:
            return []
        cutoff = now - STAGED_TTL_SECONDS
        return [f for f in staged_q if f.mono_time >= cutoff]

    def _record_buffered_packet(self, opus_bytes: bytes, mono_time: float) -> None:
        self.packets_buffered += 1
        if opus_bytes != OPUS_SILENCE:
            self.non_silence_buffered += 1
            if self.dave_fail_since_success > 0:
                log.info(
                    "Voice capture recovered in guild %s after %d failed "
                    "packet(s) since the last decoded speech frame",
                    self.guild_id,
                    self.dave_fail_since_success,
                )
            self.dave_fail_since_success = 0
            self.last_dave_success_mono = mono_time
            self.dave_ever_succeeded = True

    def _append_decoded_frame(
        self,
        *,
        user_id: int,
        opus_bytes: bytes,
        rtp_timestamp: int,
        sequence: int,
        ssrc: int,
        mono_time: float,
    ) -> None:
        if self.buffer.append_packet(
            user_id=user_id,
            opus_data=opus_bytes,
            rtp_timestamp=rtp_timestamp,
            sequence=sequence,
            ssrc=ssrc,
            mono_time=mono_time,
        ):
            self._record_buffered_packet(opus_bytes, mono_time)

    def register_ssrc(self, user_id: int, ssrc: int) -> None:
        """Map ``ssrc -> user_id`` and flush any recently staged UDP frames."""
        uid = int(user_id)
        ssrc_int = int(ssrc)
        if uid <= 0 or ssrc_int <= 0:
            return
        now = self._clock()

        with self._lock:
            staged_frames = self._bind_ssrc_unlocked(uid, ssrc_int, now)

        if not staged_frames or self._is_bot_user(uid):
            return

        for frame in staged_frames:
            opus_bytes = self._unwrap_dave(uid, frame)
            if opus_bytes is not None:
                self._append_decoded_frame(
                    user_id=uid,
                    opus_bytes=opus_bytes,
                    rtp_timestamp=frame.rtp_timestamp,
                    sequence=frame.sequence,
                    ssrc=ssrc_int,
                    mono_time=frame.mono_time,
                )

    def flush(self) -> int:
        """Retry frames queued while MLS ratchets or SSRC bindings completed."""
        now = self._clock()
        flushed = 0

        # 1. Drain known-speaker frames that arrived just before MLS welcome/commit.
        with self._lock:
            wait_items = list(self._dave_wait_by_user.items())
            self._dave_wait_by_user.clear()
            staged_ssrcs = [
                (ssrc, deque(q))
                for ssrc, q in self._staged_by_ssrc.items()
                if q
            ]
            self._ssrc_last_probe_mono.clear()

        cutoff = now - DAVE_WAIT_TTL_SECONDS
        for uid, frames in wait_items:
            if self._is_bot_user(uid):
                continue
            for frame in frames:
                if frame.mono_time < cutoff:
                    continue
                opus_bytes = self._unwrap_dave(uid, frame, allow_requeue=False)
                if opus_bytes is not None:
                    self._append_decoded_frame(
                        user_id=uid,
                        opus_bytes=opus_bytes,
                        rtp_timestamp=frame.rtp_timestamp,
                        sequence=frame.sequence,
                        ssrc=frame.ssrc,
                        mono_time=frame.mono_time,
                    )
                    flushed += 1

        # 2. Try DAVE AEAD sender identification on any unmapped SSRC bursts
        #    that arrived before the group welcome completed.
        staged_cutoff = now - STAGED_TTL_SECONDS
        for ssrc, frames in staged_ssrcs:
            fresh = [f for f in frames if f.mono_time >= staged_cutoff]
            if not fresh:
                continue
            # Probe the earliest frame first so DAVE's per-sender nonce window
            # advances monotonically (1 -> 2 -> 3 -> ...) when flushing the rest.
            probe_frame = fresh[0]
            resolved = self._try_resolve_ssrc_via_dave(probe_frame.payload)
            if resolved is None:
                continue
            uid, first_opus = resolved
            with self._lock:
                # Re-pop live queue in case more frames arrived concurrently.
                current = self._bind_ssrc_unlocked(uid, ssrc, now)
            self.dave_ssrc_resolved += 1
            if self._is_bot_user(uid):
                continue
            for frame in current:
                if (
                    frame.sequence == probe_frame.sequence
                    and frame.rtp_timestamp == probe_frame.rtp_timestamp
                ):
                    opus_bytes: bytes | None = first_opus
                else:
                    opus_bytes = self._unwrap_dave(uid, frame, allow_requeue=False)
                if opus_bytes is not None:
                    self._append_decoded_frame(
                        user_id=uid,
                        opus_bytes=opus_bytes,
                        rtp_timestamp=frame.rtp_timestamp,
                        sequence=frame.sequence,
                        ssrc=ssrc,
                        mono_time=frame.mono_time,
                    )
                    flushed += 1

        return flushed

    def remove_user(self, user_id: int) -> None:
        uid = int(user_id)
        with self._lock:
            ssrc = self._user_to_ssrc.pop(uid, None)
            if ssrc is not None:
                self._ssrc_to_user.pop(ssrc, None)
                self._staged_by_ssrc.pop(ssrc, None)
                self._ssrc_last_probe_mono.pop(ssrc, None)
            self._dave_wait_by_user.pop(uid, None)
        self.buffer.mark_speaker_discontinuity(uid)

    def reset(self) -> None:
        with self._lock:
            self._decryptor = None
            self._own_ssrc = None
            self._ssrc_to_user.clear()
            self._user_to_ssrc.clear()
            self._staged_by_ssrc.clear()
            self._ssrc_last_probe_mono.clear()
            self._dave_wait_by_user.clear()

    def _try_resolve_ssrc_via_dave(self, payload: bytes) -> tuple[int, bytes] | None:
        dave_session, dave_version = self._dave_state_getter()
        if dave_session is None or dave_version <= 0:
            return None
        with self._lock:
            mapped_users = tuple(self._user_to_ssrc.keys())
        own_uid = self._own_user_id_getter()
        with self._dave_lock:
            return dave_compat.try_identify_dave_sender(
                dave_session,
                payload,
                own_user_id=own_uid,
                already_mapped_users=mapped_users,
            )

    def handle_udp_packet(self, datagram: bytes) -> None:
        """Non-blocking callback invoked on ``SocketReader`` thread per UDP packet."""
        arrival_mono = self._clock()
        self.packets_received += 1
        if not self._first_packet_logged:
            self._first_packet_logged = True
            log.info(
                "Voice UDP packets are arriving for guild %s (receiver is live)",
                self.guild_id,
            )

        with self._lock:
            decryptor = self._decryptor
            own_ssrc = self._own_ssrc

        if decryptor is None:
            return

        try:
            parsed = decryptor.decrypt_rtp(datagram)
        except _TRANSPORT_CRYPTO_ERRORS:
            self.transport_errors += 1
            return
        except Exception:  # noqa: BLE001 - malformed network input must never crash reader
            self.transport_errors += 1
            return

        if parsed is None:
            return
        if own_ssrc is not None and parsed.ssrc == own_ssrc:
            return

        self.audio_packets_received += 1
        frame = _StagedFrame(
            mono_time=arrival_mono,
            sequence=parsed.sequence,
            rtp_timestamp=parsed.rtp_timestamp,
            ssrc=parsed.ssrc,
            payload=parsed.payload,
        )

        should_probe_dave = False
        with self._lock:
            user_id = self._ssrc_to_user.get(parsed.ssrc)
            if user_id is None:
                if parsed.payload == OPUS_SILENCE:
                    return
                last_probe = self._ssrc_last_probe_mono.get(parsed.ssrc)
                if (
                    dave_compat.is_dave_encrypted_frame(parsed.payload)
                    and (
                        last_probe is None
                        or (arrival_mono - last_probe) >= SSRC_DAVE_PROBE_INTERVAL_SECONDS
                    )
                ):
                    self._ssrc_last_probe_mono[parsed.ssrc] = arrival_mono
                    while len(self._ssrc_last_probe_mono) > MAX_STAGED_SSRCS * 2:
                        self._ssrc_last_probe_mono.popitem(last=False)
                    should_probe_dave = True
                else:
                    self._stage_unknown_ssrc_unlocked(frame)
                    return

        if user_id is None:
            resolved = self._try_resolve_ssrc_via_dave(parsed.payload) if should_probe_dave else None
            if resolved is None:
                with self._lock:
                    # Another event may have mapped the SSRC while we probed.
                    user_id = self._ssrc_to_user.get(parsed.ssrc)
                    if user_id is None:
                        self._stage_unknown_ssrc_unlocked(frame)
                        return
            else:
                uid, current_opus = resolved
                self.dave_ssrc_resolved += 1
                with self._lock:
                    earlier_frames = self._bind_ssrc_unlocked(uid, parsed.ssrc, arrival_mono)
                log.debug(
                    "Resolved unmapped audio SSRC %s in guild %s via DAVE AEAD",
                    parsed.ssrc,
                    self.guild_id,
                )
                if self._is_bot_user(uid):
                    return
                self.dave_fail_streak = 0
                self.dave_fail_since_success = 0
                self.last_dave_success_mono = arrival_mono
                self.dave_ever_succeeded = True
                for staged in earlier_frames:
                    opus_prev = self._unwrap_dave(uid, staged, allow_requeue=False)
                    if opus_prev is not None:
                        self._append_decoded_frame(
                            user_id=uid,
                            opus_bytes=opus_prev,
                            rtp_timestamp=staged.rtp_timestamp,
                            sequence=staged.sequence,
                            ssrc=parsed.ssrc,
                            mono_time=staged.mono_time,
                        )
                self._append_decoded_frame(
                    user_id=uid,
                    opus_bytes=current_opus,
                    rtp_timestamp=parsed.rtp_timestamp,
                    sequence=parsed.sequence,
                    ssrc=parsed.ssrc,
                    mono_time=arrival_mono,
                )
                return

        if self._is_bot_user(user_id):
            return

        opus_bytes = self._unwrap_dave(user_id, frame)
        if opus_bytes is None:
            return

        self._append_decoded_frame(
            user_id=user_id,
            opus_bytes=opus_bytes,
            rtp_timestamp=parsed.rtp_timestamp,
            sequence=parsed.sequence,
            ssrc=parsed.ssrc,
            mono_time=arrival_mono,
        )

    def _stage_unknown_ssrc_unlocked(self, frame: _StagedFrame) -> None:
        cutoff = frame.mono_time - STAGED_TTL_SECONDS
        stale_keys = [
            k for k, q in self._staged_by_ssrc.items() if not q or q[-1].mono_time < cutoff
        ]
        for k in stale_keys:
            self._staged_by_ssrc.pop(k, None)

        q = self._staged_by_ssrc.get(frame.ssrc)
        if q is None:
            while len(self._staged_by_ssrc) >= MAX_STAGED_SSRCS:
                self._staged_by_ssrc.popitem(last=False)
            q = deque(maxlen=MAX_STAGED_PACKETS_PER_SSRC)
            self._staged_by_ssrc[frame.ssrc] = q
        else:
            self._staged_by_ssrc.move_to_end(frame.ssrc)
        q.append(frame)

    def _queue_for_dave_ready(self, user_id: int, frame: _StagedFrame) -> None:
        cutoff = frame.mono_time - DAVE_WAIT_TTL_SECONDS
        with self._lock:
            stale = [
                uid
                for uid, q in self._dave_wait_by_user.items()
                if not q or q[-1].mono_time < cutoff
            ]
            for uid in stale:
                self._dave_wait_by_user.pop(uid, None)
            q = self._dave_wait_by_user.get(user_id)
            if q is None:
                while len(self._dave_wait_by_user) >= MAX_DAVE_WAIT_USERS:
                    self._dave_wait_by_user.popitem(last=False)
                q = deque(maxlen=MAX_DAVE_WAIT_PACKETS_PER_USER)
                self._dave_wait_by_user[user_id] = q
            else:
                self._dave_wait_by_user.move_to_end(user_id)
            q.append(frame)

    def _unwrap_dave(
        self,
        user_id: int,
        frame: _StagedFrame,
        *,
        allow_requeue: bool = True,
    ) -> bytes | None:
        """Unwrap Discord DAVE E2EE audio payload when active on the session."""
        transport_payload = frame.payload
        if transport_payload == OPUS_SILENCE:
            return OPUS_SILENCE

        dave_session, dave_version = self._dave_state_getter()
        if dave_session is None:
            return transport_payload

        downgraded = bool(self._dave_downgraded_getter())

        with self._dave_lock:
            is_ready = bool(getattr(dave_session, "ready", False))

            if dave_version > 0 and not downgraded:
                if not is_ready:
                    # MLS group is still completing its initial welcome/commit.
                    try:
                        if dave_session.can_passthrough(user_id):
                            out = dave_session.decrypt(
                                user_id, davey.MediaType.audio, transport_payload
                            )
                            return bytes(out) if out else None
                    except Exception:  # noqa: BLE001
                        self.dave_errors += 1
                    self.dave_not_ready_drops += 1
                    if allow_requeue and dave_compat.is_dave_encrypted_frame(transport_payload):
                        self._queue_for_dave_ready(user_id, frame)
                    return None

                try:
                    decrypted = dave_session.decrypt(
                        user_id, davey.MediaType.audio, transport_payload
                    )
                except Exception as err:  # noqa: BLE001
                    # A brand-new joiner's first RTP frames can arrive a few ms
                    # before their MLS Add commit completes; stage briefly so
                    # their first word is decoded once op 29 arrives.
                    if (
                        allow_requeue
                        and _is_missing_decryptor_error(err)
                        and dave_compat.is_dave_encrypted_frame(transport_payload)
                    ):
                        self._queue_for_dave_ready(user_id, frame)
                    self.dave_errors += 1
                    self.dave_fail_streak += 1
                    self.dave_fail_since_success += 1
                    streak = self.dave_fail_streak
                    if streak in DAVE_FAILURE_LOG_STREAKS:
                        log.warning(
                            "DAVE E2EE decryption has failed for %d packets in a row "
                            "in guild %s (last error: %s); requesting MLS session refresh.",
                            streak,
                            self.guild_id,
                            err.__class__.__name__,
                        )
                    if (
                        self._dave_recovery_callback is not None
                        and streak >= DAVE_INPLACE_RECOVERY_STREAK
                        and streak % DAVE_INPLACE_RECOVERY_STREAK == 0
                    ):
                        try:
                            self._dave_recovery_callback()
                        except Exception:  # noqa: BLE001
                            log.debug("DAVE in-place recovery trigger failed", exc_info=True)
                    return None

                if self.dave_fail_streak > 0:
                    log.info(
                        "DAVE E2EE decryption recovered in guild %s after %d "
                        "consecutive failure(s)",
                        self.guild_id,
                        self.dave_fail_streak,
                    )
                self.dave_fail_streak = 0
                self.dave_fail_since_success = 0
                self.last_dave_success_mono = frame.mono_time
                self.dave_ever_succeeded = True
                return bytes(decrypted) if decrypted else None

            # Protocol version 0 / downgraded call: decrypt any in-flight E2EE
            # tail packets during the 10s grace window, pass plaintext Opus through.
            if is_ready:
                try:
                    if dave_session.can_passthrough(user_id) or dave_compat.is_dave_encrypted_frame(
                        transport_payload
                    ):
                        out = dave_session.decrypt(
                            user_id, davey.MediaType.audio, transport_payload
                        )
                        if out:
                            return bytes(out)
                except Exception:  # noqa: BLE001
                    if dave_compat.is_dave_encrypted_frame(transport_payload):
                        # Never feed raw MLS ciphertext into the Opus decoder.
                        self.dave_errors += 1
                        return None
                    log.debug("Passthrough DAVE decrypt fell back to raw Opus")

        if dave_compat.is_dave_encrypted_frame(transport_payload):
            self.dave_errors += 1
            return None
        return transport_payload


class ClipVoiceWebSocket(DiscordVoiceWebSocket):
    """Voice Gateway v8 websocket driving the full ``davey`` 0.2 state machine.

    Implements the ``Rapptz/discord.py#10548`` / ``libdave`` updates missing
    from ``discord.py`` 2.7.1 — most critically ``prepare_transition`` after
    binary opcodes 29 (``MLS_ANNOUNCE_COMMIT_TRANSITION``) and 30
    (``MLS_WELCOME``), roster tracking across opcodes 11/13, and thread-safe
    session locking shared with the UDP receive thread.
    """

    _paradise_dave_bridge: bool = True

    async def received_message(self, msg: dict[str, Any]) -> None:
        op = msg.get("op")
        data = msg.get("d")
        self.seq_ack = msg.get("seq", self.seq_ack)
        state: ClipVoiceConnectionState = self._connection  # type: ignore[assignment]

        if op == self.READY and isinstance(data, dict):
            await self.initial_connection(data)
        elif op == self.HEARTBEAT_ACK:
            if self._keep_alive:
                self._keep_alive.ack()
        elif op == self.RESUMED:
            log.debug("Voice RESUME succeeded for guild %s", getattr(state.guild, "id", "?"))
        elif op == self.SESSION_DESCRIPTION and isinstance(data, dict):
            state.mode = data["mode"]
            await self.load_secret_key(data)
            dave_ver = int(data.get("dave_protocol_version", 0) or 0)
            state.dave_protocol_version = dave_ver
            state.dave_downgraded = dave_ver == 0
            if dave_ver > 0 or state.dave_session is not None:
                await state.reinit_dave_session(ws=self)
        elif op == self.HELLO and isinstance(data, dict):
            interval = data["heartbeat_interval"] / 1000.0
            self._keep_alive = VoiceKeepAliveHandler(ws=self, interval=min(interval, 5.0))
            self._keep_alive.start()
        elif op == self.CLIENTS_CONNECT and isinstance(data, dict):
            raw_ids = data.get("user_ids")
            if isinstance(raw_ids, list):
                for raw_uid in raw_ids:
                    try:
                        state.connected_user_ids.add(int(raw_uid))
                    except (TypeError, ValueError):
                        continue
            single_uid = data.get("user_id")
            if single_uid is not None:
                try:
                    state.connected_user_ids.add(int(single_uid))
                except (TypeError, ValueError):
                    pass
        elif op == self.CLIENT_DISCONNECT and isinstance(data, dict):
            raw_uid = data.get("user_id")
            if raw_uid is not None:
                try:
                    state.connected_user_ids.discard(int(raw_uid))
                except (TypeError, ValueError):
                    pass
        elif op == self.DAVE_PREPARE_EPOCH and isinstance(data, dict):
            # Handled even when state.dave_session is still None so a call that
            # began at protocol v0 (non-E2EE) can upgrade cleanly to DAVE v1+.
            epoch = data.get("epoch")
            ver = data.get("protocol_version")
            if epoch == 1 and isinstance(ver, int):
                state.dave_protocol_version = ver
                await state.reinit_dave_session(ws=self)
        elif state.dave_session is not None and isinstance(data, dict):
            if op == self.DAVE_PREPARE_TRANSITION:
                tid = data.get("transition_id")
                ver = data.get("protocol_version")
                if isinstance(tid, int) and isinstance(ver, int):
                    state.dave_pending_transitions[tid] = ver
                    with state.dave_lock:
                        dave_compat.prepare_dave_transition(state.dave_session, tid, ver)
                    if tid == 0:
                        await state._execute_transition(0)
                    else:
                        await self.send_transition_ready(tid)
                    state.notify_dave_keys_updated()
            elif op == self.DAVE_EXECUTE_TRANSITION:
                tid = data.get("transition_id")
                if isinstance(tid, int):
                    await state._execute_transition(tid)

        await self._hook(self, msg)

    async def received_binary_message(self, msg: bytes) -> None:
        if len(msg) < 3:
            return
        self.seq_ack = struct.unpack_from(">H", msg, 0)[0]
        op = msg[2]
        state: ClipVoiceConnectionState = self._connection  # type: ignore[assignment]
        session = state.dave_session
        if session is None:
            return

        if op == self.MLS_EXTERNAL_SENDER:
            channel_id = getattr(getattr(state.voice_client, "channel", None), "id", None)
            user_id = getattr(getattr(state, "user", None), "id", None)
            with state.dave_lock:
                applied, reinitialized = dave_compat.set_dave_external_sender(
                    session,
                    msg[3:],
                    protocol_version=state.dave_protocol_version,
                    user_id=user_id,
                    channel_id=channel_id,
                )
                key_pkg = (
                    session.get_serialized_key_package()
                    if applied and reinitialized
                    else None
                )
            if key_pkg:
                await self.send_binary(self.MLS_KEY_PACKAGE, key_pkg)

        elif op == self.MLS_PROPOSALS:
            if len(msg) < 4:
                return
            optype = msg[3]
            expected_ids = state.expected_dave_user_ids()
            try:
                with state.dave_lock:
                    commit_welcome = dave_compat.process_dave_proposals(
                        session,
                        optype,
                        msg[4:],
                        expected_user_ids=expected_ids,
                    )
            except Exception:  # noqa: BLE001
                log.warning(
                    "Failed to process MLS proposals in guild %s; re-initialising DAVE session",
                    getattr(state.guild, "id", "?"),
                    exc_info=True,
                )
                await state.reinit_dave_session(ws=self)
                return
            if commit_welcome:
                await self.send_binary(self.MLS_COMMIT_WELCOME, commit_welcome)

        elif op == self.MLS_ANNOUNCE_COMMIT_TRANSITION:
            if len(msg) < 5:
                return
            transition_id = struct.unpack_from(">H", msg, 3)[0]
            try:
                with state.dave_lock:
                    prepared = dave_compat.process_dave_commit_transition(
                        session,
                        transition_id,
                        msg[5:],
                        fallback_version=state.dave_protocol_version or 1,
                    )
            except Exception:  # noqa: BLE001
                log.warning(
                    "Invalid MLS commit (transition %d) in guild %s; recovering",
                    transition_id,
                    getattr(state.guild, "id", "?"),
                )
                await state._recover_from_invalid_commit(transition_id, ws=self)
                return

            if not prepared:
                return

            state.on_mls_transition_prepared(transition_id)
            if transition_id != 0:
                state.dave_pending_transitions[transition_id] = state.dave_protocol_version
                await self.send_transition_ready(transition_id)
            log.info(
                "DAVE MLS commit transition %d ready for guild %s (epoch=%s)",
                transition_id,
                getattr(state.guild, "id", "?"),
                getattr(session, "epoch", "?"),
            )
            state.notify_dave_keys_updated()

        elif op == self.MLS_WELCOME:
            if len(msg) < 5:
                return
            transition_id = struct.unpack_from(">H", msg, 3)[0]
            recognized_ids = state.expected_dave_user_ids()
            try:
                with state.dave_lock:
                    dave_compat.process_dave_welcome_transition(
                        session,
                        transition_id,
                        msg[5:],
                        recognized_user_ids=recognized_ids,
                        fallback_version=state.dave_protocol_version or 1,
                    )
            except Exception:  # noqa: BLE001
                log.warning(
                    "Invalid MLS welcome (transition %d) in guild %s; recovering",
                    transition_id,
                    getattr(state.guild, "id", "?"),
                )
                await state._recover_from_invalid_commit(transition_id, ws=self)
                return

            state.on_mls_transition_prepared(transition_id)
            if transition_id != 0:
                state.dave_pending_transitions[transition_id] = state.dave_protocol_version
                await self.send_transition_ready(transition_id)
            log.info(
                "DAVE MLS welcome transition %d ready for guild %s (epoch=%s)",
                transition_id,
                getattr(state.guild, "id", "?"),
                getattr(session, "epoch", "?"),
            )
            state.notify_dave_keys_updated()


class ClipVoiceConnectionState(VoiceConnectionState):
    """Thread-safe voice connection state paired with :class:`ClipVoiceWebSocket`."""

    def __init__(self, voice_client: ClipVoiceClient, *, hook: Any = None) -> None:
        super().__init__(voice_client, hook=hook)
        self.dave_lock: threading.RLock = threading.RLock()
        self.connected_user_ids: set[int] = set()
        self.last_dave_transition_id: int | None = None
        self._dave_reinitializing: bool = False
        self._last_dave_recovery_mono: float = 0.0

    @property
    def can_encrypt(self) -> bool:
        session = self.dave_session
        return (
            self.dave_protocol_version != 0
            and not self.dave_downgraded
            and session is not None
            and dave_compat.session_can_encrypt(session)
        )

    def expected_dave_user_ids(self) -> list[int] | None:
        """Build the recognised user-ID roster for MLS proposals & welcomes."""
        ids: set[int] = set(self.connected_user_ids)
        self_uid = getattr(getattr(self, "user", None), "id", None)
        if isinstance(self_uid, int) and self_uid > 0:
            ids.add(self_uid)
        channel = getattr(self.voice_client, "channel", None)
        voice_states = getattr(channel, "voice_states", None)
        if isinstance(voice_states, dict):
            for uid in voice_states.keys():
                if isinstance(uid, int) and uid > 0:
                    ids.add(uid)
        # Only pass an explicit allow-list when we have actually seen remote
        # members on the gateway; otherwise let davey validate credentials alone.
        remote_count = len(ids - ({self_uid} if isinstance(self_uid, int) else set()))
        return list(ids) if remote_count > 0 else None

    async def reinit_dave_session(
        self, *, ws: DiscordVoiceWebSocket | None = None
    ) -> None:
        self.dave_pending_transitions.clear()
        target_ws = ws if ws is not None else self.ws
        if self.dave_protocol_version > 0:
            channel_id = int(self.voice_client.channel.id)
            user_id = int(self.user.id)
            with self.dave_lock:
                session = self.dave_session
                if session is not None:
                    session.reinit(self.dave_protocol_version, user_id, channel_id)
                else:
                    session = davey.DaveSession(
                        self.dave_protocol_version, user_id, channel_id
                    )
                    self.dave_session = session
                # While upgrading from a v0 session, peers still transmit
                # unencrypted Opus until DAVE_EXECUTE_TRANSITION completes.
                if self.dave_downgraded:
                    dave_compat.set_dave_passthrough(session, True)
                key_package = session.get_serialized_key_package()
            if target_ws is not discord_utils.MISSING and target_ws is not None:
                await target_ws.send_binary(
                    DiscordVoiceWebSocket.MLS_KEY_PACKAGE, key_package
                )
        elif self.dave_session is not None:
            with self.dave_lock:
                self.dave_session.reset()
                dave_compat.set_dave_passthrough(self.dave_session, True)

    async def _recover_from_invalid_commit(
        self,
        transition_id: int,
        *,
        ws: DiscordVoiceWebSocket | None = None,
    ) -> None:
        target_ws = ws if ws is not None else self.ws
        if target_ws is not discord_utils.MISSING and target_ws is not None:
            await target_ws.send_as_json(
                {
                    "op": DiscordVoiceWebSocket.MLS_INVALID_COMMIT_WELCOME,
                    "d": {"transition_id": int(transition_id)},
                }
            )
        await self.reinit_dave_session(ws=target_ws)

    def on_mls_transition_prepared(self, transition_id: int) -> None:
        """Update connection transition bookkeeping after op 29 or op 30."""
        self._dave_reinitializing = False
        self.last_dave_transition_id = int(transition_id)
        if transition_id == 0:
            # Transition 0 is applied immediately inside prepare_transition().
            if self.dave_downgraded and self.dave_protocol_version > 0:
                self.dave_downgraded = False
                if self.dave_session is not None:
                    with self.dave_lock:
                        dave_compat.set_dave_passthrough(
                            self.dave_session,
                            False,
                            dave_compat.DAVE_PASSTHROUGH_TRANSITION_EXPIRY_SECONDS,
                        )

    async def _execute_transition(self, transition_id: int) -> None:
        if transition_id not in self.dave_pending_transitions:
            log.debug(
                "Ignoring execute_transition for unknown transition id %d",
                transition_id,
            )
            return

        target_version = self.dave_pending_transitions.pop(transition_id)
        session = self.dave_session
        # Transition 0 was already applied by prepare_transition(0, version).
        if session is not None and transition_id != 0:
            with self.dave_lock:
                if dave_compat.session_supports_transitions(session):
                    if not dave_compat.execute_dave_transition(session, transition_id):
                        log.warning(
                            "DAVE transition %d was not pending inside davey session",
                            transition_id,
                        )
                        return

        old_version = self.dave_protocol_version
        self.dave_protocol_version = target_version
        self.last_dave_transition_id = transition_id
        self._dave_reinitializing = False

        if old_version != self.dave_protocol_version and self.dave_protocol_version == 0:
            self.dave_downgraded = True
            if session is not None:
                with self.dave_lock:
                    dave_compat.set_dave_passthrough(session, True)
            log.debug("DAVE session downgraded to passthrough (v0)")
        elif transition_id > 0 and self.dave_downgraded and self.dave_protocol_version > 0:
            self.dave_downgraded = False
            if session is not None:
                with self.dave_lock:
                    # Disable passthrough after the 10 s transition expiry
                    # (fixes discord.py 2.7.1 bug that passed True here).
                    dave_compat.set_dave_passthrough(
                        session,
                        False,
                        dave_compat.DAVE_PASSTHROUGH_TRANSITION_EXPIRY_SECONDS,
                    )
            log.debug("DAVE session upgraded to protocol v%d", self.dave_protocol_version)

        self.notify_dave_keys_updated()

    def notify_dave_keys_updated(self) -> None:
        cb = getattr(self.voice_client, "on_dave_keys_updated", None)
        if callable(cb):
            cb()

    def request_inplace_dave_recovery(self) -> None:
        """Thread-safe trigger: refresh MLS keys in-place without disconnecting."""
        if self._dave_reinitializing or self.dave_pending_transitions:
            return
        if self.dave_protocol_version <= 0 or self.dave_downgraded:
            return
        now = time.monotonic()
        if (now - self._last_dave_recovery_mono) < DAVE_INPLACE_RECOVERY_COOLDOWN_SECONDS:
            return
        loop = getattr(self.voice_client, "loop", None)
        if not isinstance(loop, asyncio.AbstractEventLoop) or not loop.is_running():
            return
        self._last_dave_recovery_mono = now
        self._dave_reinitializing = True
        loop.call_soon_threadsafe(self._spawn_inplace_recovery, loop)

    def _spawn_inplace_recovery(self, loop: asyncio.AbstractEventLoop) -> None:
        loop.create_task(
            self._run_inplace_dave_recovery(),
            name=f"paradise-dave-reinit-{getattr(self.guild, 'id', 0)}",
        )

    async def _run_inplace_dave_recovery(self) -> None:
        try:
            if not self.is_connected() or self.dave_protocol_version <= 0:
                return
            ws = self.ws
            if ws is discord_utils.MISSING or ws is None:
                return
            if self.last_dave_transition_id is not None:
                await self._recover_from_invalid_commit(self.last_dave_transition_id)
            else:
                await self.reinit_dave_session()
            log.info(
                "Requested in-place DAVE MLS key refresh for guild %s",
                getattr(self.guild, "id", "?"),
            )
        except Exception:  # noqa: BLE001
            log.debug("In-place DAVE MLS key refresh failed", exc_info=True)
        finally:
            self._dave_reinitializing = False

    async def _connect_websocket(self, resume: bool) -> DiscordVoiceWebSocket:
        seq_ack = -1
        if self.ws is not discord_utils.MISSING:
            seq_ack = self.ws.seq_ack
        if not resume:
            self.connected_user_ids.clear()
            self.dave_pending_transitions.clear()
        ws = await ClipVoiceWebSocket.from_connection_state(
            self, resume=resume, hook=self.hook, seq_ack=seq_ack
        )
        self.state = ConnectionFlowState.websocket_connected
        return ws


class ClipVoiceClient(discord.VoiceClient):
    """``discord.VoiceClient`` subclass adding non-blocking RTP/DAVE voice reception."""

    supported_modes: tuple[str, ...] = SUPPORTED_TRANSPORT_MODES  # type: ignore[assignment]

    def __init__(self, client: discord.Client, channel: discord.abc.Connectable) -> None:
        self._receive_router: VoiceReceiveRouter | None = None
        self._listener_registered: bool = False
        self._keepalive_task: asyncio.Task[None] | None = None
        self._silence_task: asyncio.Task[None] | None = None
        self._keepalive_counter: int = 0
        super().__init__(client, channel)

    def create_connection_state(self) -> VoiceConnectionState:
        return ClipVoiceConnectionState(self, hook=self._on_voice_ws_message)

    def _encrypt_aead_aes256_gcm_rtpsize(self, header: bytes, data: bytes) -> bytes:
        """Encrypt an outgoing RTP payload with Discord's AES-256-GCM rtpsize mode."""
        assert AESGCM is not None
        aes = AESGCM(bytes(self.secret_key))
        nonce = bytearray(12)
        nonce[:4] = struct.pack(">I", self._incr_nonce)
        self.checked_add("_incr_nonce", 1, 4294967295)
        return header + aes.encrypt(bytes(nonce), bytes(data), bytes(header)) + bytes(nonce[:4])

    def _get_voice_packet(self, data: bytes) -> bytes | None:  # type: ignore[override]
        """Formulate an outgoing RTP packet with thread-safe DAVE 0.2 encryption."""
        packet = bytes(data)
        conn = self._connection
        session = getattr(conn, "dave_session", None)
        if session is not None and conn.can_encrypt and packet != OPUS_SILENCE:
            dave_lock = getattr(conn, "dave_lock", None)
            if dave_lock is not None:
                with dave_lock:
                    if not dave_compat.session_can_encrypt(session):
                        return None
                    packet = session.encrypt_opus(packet)
            else:
                if not dave_compat.session_can_encrypt(session):
                    return None
                packet = session.encrypt_opus(packet)

        header = bytearray(12)
        header[0] = 0x80
        header[1] = 0x78
        struct.pack_into(">H", header, 2, self.sequence)
        struct.pack_into(">I", header, 4, self.timestamp)
        struct.pack_into(">I", header, 8, self.ssrc)

        encrypt_packet = getattr(self, "_encrypt_" + self.mode)
        return encrypt_packet(bytes(header), packet)

    def send_audio_packet(self, data: bytes, *, encode: bool = True) -> None:
        """Send one 20 ms Opus/PCM frame, safely skipping pre-key DAVE windows."""
        self.checked_add("sequence", 1, 65535)
        if encode:
            encoded_data = self.encoder.encode(data, self.encoder.SAMPLES_PER_FRAME)
        else:
            encoded_data = data
        packet = self._get_voice_packet(encoded_data)
        if packet is not None:
            try:
                self._connection.send_packet(packet)
            except OSError:
                log.debug(
                    "Outgoing voice packet dropped (seq=%s, ts=%s)",
                    self.sequence,
                    self.timestamp,
                )
        self.checked_add("timestamp", opus.Encoder.SAMPLES_PER_FRAME, 4294967295)

    def on_dave_keys_updated(self) -> None:
        """Flush any voice frames queued while MLS ratchets were completing."""
        router = self._receive_router
        if router is not None:
            try:
                router.flush()
            except Exception:  # noqa: BLE001
                log.debug("Post-transition DAVE frame flush error", exc_info=True)

    def _bind_clip_service(self, channel_id: int | None) -> None:
        service = getattr(self.client, "voice_clip_service", None)
        guild = getattr(self.channel, "guild", None)
        if service is None or guild is None or channel_id is None:
            return
        if not getattr(service, "receiving_wanted", service.enabled):
            return
        already_bound = self._receive_router is not None
        old_router = self._receive_router
        self._receive_router = service.attach_voice_client(
            guild=guild,
            channel_id=int(channel_id),
            voice_client=self,
            previous_router=None if old_router is None else old_router,
        )
        self._ensure_socket_listener()
        self._arm_receiver()
        if not already_bound:
            log.info(
                "Voice receiver attached for guild %s in channel %s",
                guild.id,
                channel_id,
            )

    def _arm_receiver(self) -> None:
        """Configure the router's transport decryptor from live connection state."""
        router = self._receive_router
        if router is None or router.has_transport():
            return
        conn = self._connection
        mode = getattr(conn, "mode", None)
        secret_key = getattr(conn, "secret_key", None)
        if mode is discord.utils.MISSING or secret_key is discord.utils.MISSING:
            return
        if not isinstance(mode, str) or not isinstance(secret_key, (list, bytes, bytearray)):
            return
        if len(secret_key) != 32:
            return
        try:
            ssrc = getattr(conn, "ssrc", None)
            router.configure_transport(
                mode,
                bytes(secret_key) if not isinstance(secret_key, bytes) else secret_key,
                own_ssrc=ssrc if isinstance(ssrc, int) else None,
            )
            log.info(
                "Voice receiver transport re-armed from connection state for guild %s",
                getattr(getattr(self.channel, "guild", None), "id", "?"),
            )
        except (ValueError, TypeError):
            log.debug("Could not re-arm the voice receiver transport", exc_info=True)

    def rebind_recording(self) -> None:
        """Re-attach rolling capture after a service-side detach (idempotent)."""
        self._bind_clip_service(getattr(self.channel, "id", None))

    def _ensure_socket_listener(self) -> None:
        if self._receive_router is None:
            return
        reader = getattr(self._connection, "_socket_reader", None)
        callbacks = getattr(reader, "_callbacks", None)
        if isinstance(callbacks, list):
            if self._on_udp_packet not in callbacks:
                self._connection.add_socket_listener(self._on_udp_packet)
            self._listener_registered = True
        elif not self._listener_registered:
            self._connection.add_socket_listener(self._on_udp_packet)
            self._listener_registered = True

        sock = getattr(self._connection, "socket", discord.utils.MISSING)
        if sock is not discord.utils.MISSING and sock is not None and reader is not None:
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
            # Ignore transient channel_id=None frames observed while discord.py
            # is actively executing an internal reconnect flow; tear down only
            # when the connection state actually transitioned to disconnected.
            conn_state = getattr(self._connection, "state", ConnectionFlowState.disconnected)
            if conn_state is ConnectionFlowState.disconnected:
                self._teardown_recording()
            return
        new_channel_id = int(raw_cid)
        if new_channel_id != old_channel_id:
            connected_ids = getattr(self._connection, "connected_user_ids", None)
            if isinstance(connected_ids, set):
                connected_ids.clear()
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

        if op == VOICE_OP_RESUMED:
            self._ensure_socket_listener()
            self._arm_receiver()
            return

        # ClipVoiceWebSocket already applies ops 21/22 in received_message
        # before invoking this hook.  Keep the fallback active whenever a plain
        # DiscordVoiceWebSocket or test stub calls the hook directly.
        handled_by_ws = bool(getattr(ws, "_paradise_dave_bridge", False))
        dave_lock = getattr(self._connection, "dave_lock", None)

        if op == VOICE_OP_DAVE_PREPARE_TRANSITION:
            if not handled_by_ws:
                session = getattr(self._connection, "dave_session", None)
                tid = data.get("transition_id")
                version = data.get("protocol_version")
                if session is not None and isinstance(tid, int) and isinstance(version, int):
                    if dave_lock is not None:
                        with dave_lock:
                            ok = dave_compat.prepare_dave_transition(session, tid, version)
                    else:
                        ok = dave_compat.prepare_dave_transition(session, tid, version)
                    if ok:
                        self.on_dave_keys_updated()
            return

        if op == VOICE_OP_DAVE_EXECUTE_TRANSITION:
            if not handled_by_ws:
                session = getattr(self._connection, "dave_session", None)
                tid = data.get("transition_id")
                if session is not None and isinstance(tid, int):
                    if dave_lock is not None:
                        with dave_lock:
                            ok = dave_compat.execute_dave_transition(session, tid)
                    else:
                        ok = dave_compat.execute_dave_transition(session, tid)
                    if ok:
                        self.on_dave_keys_updated()
            return

        if router is None:
            return

        if op == VOICE_OP_READY:
            ssrc = data.get("ssrc")
            if isinstance(ssrc, int):
                router.set_own_ssrc(ssrc)
            self._ensure_socket_listener()

        elif op == VOICE_OP_SESSION_DESCRIPTION:
            mode = data.get("mode") or self._connection.mode
            secret_key = getattr(ws, "secret_key", None) or self._connection.secret_key
            if (
                isinstance(mode, str)
                and isinstance(secret_key, (list, bytes, bytearray))
                and len(secret_key) == 32
            ):
                try:
                    router.configure_transport(
                        mode,
                        bytes(secret_key),
                        own_ssrc=(
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
            if raw_uid is not None and isinstance(raw_ssrc, int) and raw_ssrc > 0:
                try:
                    uid = int(raw_uid)
                    connected_ids = getattr(self._connection, "connected_user_ids", None)
                    if isinstance(connected_ids, set):
                        connected_ids.add(uid)
                    router.register_ssrc(uid, raw_ssrc)
                except (TypeError, ValueError):
                    log.debug("Invalid SPEAKING user_id payload")

        elif op == VOICE_OP_VIDEO:
            raw_uid = data.get("user_id")
            raw_ssrc = data.get("audio_ssrc")
            if raw_uid is not None and isinstance(raw_ssrc, int) and raw_ssrc > 0:
                try:
                    uid = int(raw_uid)
                    connected_ids = getattr(self._connection, "connected_user_ids", None)
                    if isinstance(connected_ids, set):
                        connected_ids.add(uid)
                    router.register_ssrc(uid, raw_ssrc)
                except (TypeError, ValueError):
                    log.debug("Invalid VIDEO user_id payload")

        elif op == VOICE_OP_CLIENT_CONNECT:
            connected_ids = getattr(self._connection, "connected_user_ids", None)
            raw_ids = data.get("user_ids")
            if isinstance(raw_ids, list) and isinstance(connected_ids, set):
                for item in raw_ids:
                    try:
                        connected_ids.add(int(item))
                    except (TypeError, ValueError):
                        continue
            raw_uid = data.get("user_id")
            raw_ssrc = data.get("audio_ssrc")
            if raw_uid is not None:
                try:
                    uid = int(raw_uid)
                    if isinstance(connected_ids, set):
                        connected_ids.add(uid)
                    if isinstance(raw_ssrc, int) and raw_ssrc > 0:
                        router.register_ssrc(uid, raw_ssrc)
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
                self._ensure_socket_listener()
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
