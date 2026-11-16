"""DAVE E2EE protocol bridge for ``davey`` 0.2+ on Discord Voice Gateway v8.

Why this module exists
----------------------

Discord's DAVE (Audio & Video End-to-End Encryption) protocol wraps every Opus
voice frame in an inner MLS-derived AES-128-GCM layer on top of outer RTP
transport AEAD.  In ``davey`` 0.2.0+:

1. **Binary opcodes 29 (``MLS_ANNOUNCE_COMMIT_TRANSITION``) and 30
   (``MLS_WELCOME``)** — which drive *every* initial group join and *every*
   member-change epoch — do **not** install decryption key ratchets inside
   ``process_commit`` / ``process_welcome``.  The caller must check the boolean
   return of ``process_commit`` and immediately call
   ``session.prepare_transition(transition_id, session.protocol_version)``.
   Only ``prepare_transition`` updates per-user decryptors and sets
   ``session.ready = True`` (and for ``transition_id == 0`` also activates the
   outgoing encryption key immediately).
2. **JSON opcode 21 (``DAVE_PREPARE_TRANSITION``)** — sent on protocol
   downgrades / re-initialisations — must call
   ``session.prepare_transition(transition_id, protocol_version)`` and enable
   passthrough mode when transitioning to protocol version ``0``.
3. **JSON opcode 22 (``DAVE_EXECUTE_TRANSITION``)** — sent once clients signal
   readiness via opcode 23 — must call
   ``session.execute_transition(transition_id)`` (when ``transition_id != 0``)
   to switch the outgoing encryption key, and disable passthrough (with a 10 s
   transition window) when finishing an upgrade from version ``0``.
4. **Outgoing encryption readiness** requires ``session.can_encrypt`` (not just
   ``session.ready``), because between an ``MLS_WELCOME`` and its matching
   ``DAVE_EXECUTE_TRANSITION`` decryptors are live while the encryptor still
   has no key ratchet.

``discord.py`` 2.7.1 implements the pre-0.2 ``davey`` contract (upstream update:
``Rapptz/discord.py#10548``), omitting ``prepare_transition`` after binary
opcodes 29/30 and ``execute_transition`` on opcode 22, and never forwarding
binary voice-gateway frames to connection hooks.  :class:`ClipVoiceWebSocket`
and :class:`ClipVoiceConnectionState` use the helpers here to run the complete
``davey`` 0.2 state machine while staying backwards-compatible with ``davey``
0.1.x and forward-compatible with future ``discord.py`` releases.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

import davey

log = logging.getLogger(__name__)

# Discord Voice Gateway v8 DAVE & MLS opcodes.
DAVE_PREPARE_TRANSITION_OP: int = 21
DAVE_EXECUTE_TRANSITION_OP: int = 22
DAVE_TRANSITION_READY_OP: int = 23
DAVE_PREPARE_EPOCH_OP: int = 24
MLS_EXTERNAL_SENDER_OP: int = 25
MLS_KEY_PACKAGE_OP: int = 26
MLS_PROPOSALS_OP: int = 27
MLS_COMMIT_WELCOME_OP: int = 28
MLS_ANNOUNCE_COMMIT_TRANSITION_OP: int = 29
MLS_WELCOME_OP: int = 30
MLS_INVALID_COMMIT_WELCOME_OP: int = 31

# DAVE encrypted media frame footer layout (daveprotocol.com):
#   [ciphertext][8B truncated GCM tag][LEB128 nonce][LEB128 ranges...][u8 size][0xFA 0xFA]
DAVE_MAGIC_MARKER: bytes = b"\xfa\xfa"
DAVE_TAG_BYTES: int = 8
DAVE_MIN_SUPPLEMENTAL_BYTES: int = DAVE_TAG_BYTES + 1 + 1 + len(DAVE_MAGIC_MARKER)  # 12
DAVE_PASSTHROUGH_TRANSITION_EXPIRY_SECONDS: int = 10


def _has_valid_leb128_prefix(buf: bytes) -> bool:
    """Return True if ``buf`` begins with a terminated unsigned LEB128 varint."""
    if not buf:
        return False
    # Truncated DAVE nonce is u32 -> at most 5 LEB128 bytes.
    limit = min(len(buf), 5)
    for idx in range(limit):
        if (buf[idx] & 0x80) == 0:
            return True
    return False


def is_dave_encrypted_frame(payload: bytes) -> bool:
    """Return True when ``payload`` carries a structurally valid DAVE E2EE footer.

    Matches ``InboundFrameProcessor::parse_frame`` in ``libdave`` / ``davey``:
    checks the trailing ``0xFAFA`` marker, the supplemental-data byte count,
    minimum size for the 8-byte truncated AES-128-GCM tag + LEB128 nonce + size
    + marker, and LEB128 termination of the nonce field.
    """
    length = len(payload)
    if length < DAVE_MIN_SUPPLEMENTAL_BYTES:
        return False
    if payload[-2:] != DAVE_MAGIC_MARKER:
        return False
    supp_size = payload[-3]
    if supp_size < DAVE_MIN_SUPPLEMENTAL_BYTES or supp_size > length:
        return False
    supp_start = length - supp_size
    nonce_slice = payload[supp_start + DAVE_TAG_BYTES : length - 3]
    return _has_valid_leb128_prefix(nonce_slice)


def session_supports_transitions(session: Any) -> bool:
    """True when ``session`` is a ``davey`` >= 0.2 ``DaveSession``."""
    return (
        session is not None
        and hasattr(session, "prepare_transition")
        and hasattr(session, "execute_transition")
    )


def session_can_encrypt(session: Any) -> bool:
    """True when ``session`` is ready *and* holds an active encryption ratchet."""
    if session is None or not bool(getattr(session, "ready", False)):
        return False
    can_enc = getattr(session, "can_encrypt", None)
    if isinstance(can_enc, bool):
        return can_enc
    return True


def session_protocol_version(session: Any, fallback: int = 1) -> int:
    """Return ``session.protocol_version`` as a positive int, or ``fallback``."""
    raw = getattr(session, "protocol_version", None)
    if isinstance(raw, int) and raw > 0:
        return raw
    return max(1, int(fallback))


def set_dave_passthrough(
    session: Any,
    enabled: bool,
    transition_expiry: int | None = None,
) -> bool:
    """Configure passthrough mode across both ``davey`` 0.1.x and 0.2.x."""
    fn = getattr(session, "set_passthrough_mode", None)
    if not callable(fn):
        return False
    try:
        if transition_expiry is None:
            try:
                fn(bool(enabled))
            except TypeError:
                # davey 0.1.x required an explicit integer expiry.
                fn(bool(enabled), 120 if enabled else DAVE_PASSTHROUGH_TRANSITION_EXPIRY_SECONDS)
        else:
            fn(bool(enabled), int(transition_expiry))
        return True
    except Exception:  # noqa: BLE001
        log.debug("Could not update DAVE passthrough mode", exc_info=True)
        return False


def set_dave_external_sender(
    session: Any,
    external_sender_data: bytes,
    *,
    protocol_version: int | None = None,
    user_id: int | None = None,
    channel_id: int | None = None,
) -> tuple[bool, bool]:
    """Apply binary opcode 25 (``MLS_EXTERNAL_SENDER``).

    Returns ``(applied, reinitialized)``.  ``davey`` 0.2 rejects
    ``set_external_sender`` with ``AlreadyInGroup`` when an established group
    already exists (e.g. if a voice server rotates external sender state before
    emitting ``DAVE_PREPARE_EPOCH``).  When caller IDs are supplied we reinit
    and retry once so the caller can send a fresh key package cleanly instead
    of crashing the voice websocket loop.
    """
    if session is None or not external_sender_data:
        return False, False
    try:
        session.set_external_sender(external_sender_data)
        return True, False
    except Exception as err:  # noqa: BLE001
        msg = str(err)
        can_reinit = (
            "AlreadyInGroup" in msg
            and user_id is not None
            and channel_id is not None
            and hasattr(session, "reinit")
        )
        if can_reinit:
            try:
                ver = session_protocol_version(session, protocol_version or 1)
                session.reinit(int(ver), int(user_id), int(channel_id))
                session.set_external_sender(external_sender_data)
                return True, True
            except Exception:  # noqa: BLE001
                log.warning(
                    "DAVE set_external_sender failed even after session reinit",
                    exc_info=True,
                )
                return False, False
        log.warning("DAVE set_external_sender rejected payload: %s", err.__class__.__name__)
        return False, False


def _normalise_user_ids(user_ids: Iterable[int] | None) -> list[int] | None:
    if not user_ids:
        return None
    out: list[int] = []
    seen: set[int] = set()
    for uid in user_ids:
        if isinstance(uid, int) and not isinstance(uid, bool) and uid > 0 and uid not in seen:
            seen.add(uid)
            out.append(uid)
    return out or None


def process_dave_proposals(
    session: Any,
    optype_byte: int,
    proposals: bytes,
    *,
    expected_user_ids: Iterable[int] | None = None,
) -> bytes | None:
    """Process binary opcode 27 (``MLS_PROPOSALS``) and return op 28 bytes if any.

    Passes ``expected_user_ids`` when available (``Rapptz/discord.py#10548``),
    but falls back cleanly to unfiltered processing if gateway roster delivery
    raced ahead of ``CLIENTS_CONNECT`` (e.g. on session resume or channel move).
    """
    if session is None or not proposals:
        return None
    op_enum = (
        davey.ProposalsOperationType.append
        if int(optype_byte) == 0
        else davey.ProposalsOperationType.revoke
    )
    roster = _normalise_user_ids(expected_user_ids)
    result = None
    if roster is not None:
        try:
            result = session.process_proposals(op_enum, proposals, roster)
        except TypeError:
            result = session.process_proposals(op_enum, proposals)
        except Exception as err:  # noqa: BLE001
            if "UnrecognizedUser" in str(err) or "UnexpectedUser" in str(err):
                log.debug(
                    "DAVE proposals contained a user ahead of CLIENTS_CONNECT; "
                    "retrying without local roster filter"
                )
                result = session.process_proposals(op_enum, proposals)
            else:
                raise
    else:
        result = session.process_proposals(op_enum, proposals)

    commit = getattr(result, "commit", None)
    if not isinstance(commit, (bytes, bytearray)) or not commit:
        return None
    welcome = getattr(result, "welcome", None)
    if isinstance(welcome, (bytes, bytearray)) and welcome:
        return bytes(commit) + bytes(welcome)
    return bytes(commit)


def process_dave_commit_transition(
    session: Any,
    transition_id: int,
    commit: bytes,
    *,
    fallback_version: int = 1,
) -> bool:
    """Apply binary opcode 29 (``MLS_ANNOUNCE_COMMIT_TRANSITION``).

    Runs ``process_commit(commit)`` and — on ``davey`` 0.2+ — immediately calls
    ``prepare_transition(transition_id, protocol_version)`` so per-speaker
    decryptor key ratchets are installed and ``session.ready`` becomes ``True``
    (and for ``transition_id == 0`` the encryption ratchet also switches).

    Returns ``True`` when the transition was prepared (caller should record and
    send opcode 23 if ``transition_id != 0``), or ``False`` when ``davey`` 0.2
    explicitly ignored the commit (no group / no queued proposals).  Raises on
    invalid commits so the caller can run opcode 31 recovery.
    """
    if session is None:
        return False
    commit_result = session.process_commit(commit)
    # davey 0.2 returns bool (False => ignored commit); davey 0.1 returned None.
    if commit_result is False:
        log.debug("MLS commit ignored by DAVE session for transition %s", transition_id)
        return False
    if session_supports_transitions(session):
        ver = session_protocol_version(session, fallback_version)
        session.prepare_transition(int(transition_id), ver)
        log.debug(
            "DAVE MLS commit transition %s prepared (protocol v%s, epoch=%s, ready=%s)",
            transition_id,
            ver,
            getattr(session, "epoch", None),
            getattr(session, "ready", None),
        )
    return True


def process_dave_welcome_transition(
    session: Any,
    transition_id: int,
    welcome: bytes,
    *,
    recognized_user_ids: Iterable[int] | None = None,
    fallback_version: int = 1,
) -> bool:
    """Apply binary opcode 30 (``MLS_WELCOME``) and install decryptor ratchets.

    Runs ``process_welcome`` (with roster validation when available, falling
    back if ``CLIENTS_CONNECT`` arrived late) followed immediately by
    ``prepare_transition(transition_id, protocol_version)`` on ``davey`` 0.2+.
    Raises on invalid welcomes so the caller can send opcode 31 and re-init.
    """
    if session is None:
        return False
    roster = _normalise_user_ids(recognized_user_ids)
    if roster is not None:
        try:
            session.process_welcome(welcome, roster)
        except TypeError:
            # davey 0.1.x accepted only (welcome,)
            session.process_welcome(welcome)
        except Exception as err:  # noqa: BLE001
            if "UnrecognizedUser" in str(err) or "UnexpectedUser" in str(err):
                log.debug(
                    "DAVE welcome roster contained a member ahead of CLIENTS_CONNECT; "
                    "retrying without local roster filter"
                )
                session.process_welcome(welcome)
            else:
                raise
    else:
        session.process_welcome(welcome)

    if session_supports_transitions(session):
        ver = session_protocol_version(session, fallback_version)
        session.prepare_transition(int(transition_id), ver)
        log.debug(
            "DAVE MLS welcome transition %s prepared (protocol v%s, epoch=%s, ready=%s)",
            transition_id,
            ver,
            getattr(session, "epoch", None),
            getattr(session, "ready", None),
        )
    return True


def prepare_dave_transition(
    session: Any, transition_id: int, protocol_version: int
) -> bool:
    """Run ``session.prepare_transition(id, version)`` for JSON opcode 21.

    Installs decryptor ratchets (or expires them with a 10 s grace window when
    ``protocol_version == 0``) and immediately applies ``transition_id == 0``.
    Returns True when the session accepted the transition; never raises.
    """
    if not session_supports_transitions(session):
        return False
    tid = int(transition_id)
    ver = int(protocol_version)
    try:
        session.prepare_transition(tid, ver)
        if ver == 0:
            set_dave_passthrough(session, True)
        log.debug("DAVE transition %s prepared (protocol v%s)", tid, ver)
        return True
    except Exception:  # noqa: BLE001 - the voice websocket must never break here
        log.warning(
            "DAVE prepare_transition(%s, v%s) failed; incoming audio may not "
            "decrypt until the connection is refreshed",
            tid,
            ver,
            exc_info=True,
        )
        return False


def execute_dave_transition(session: Any, transition_id: int) -> bool:
    """Run ``session.execute_transition(id)`` for JSON opcode 22.

    Switches the outgoing encryption key to the previously prepared epoch.
    Note that ``davey`` 0.2 already applies ``transition_id == 0`` inside
    ``prepare_transition(0, version)``.  Returns True when executed; never
    raises.
    """
    if not session_supports_transitions(session):
        return False
    tid = int(transition_id)
    try:
        executed = bool(session.execute_transition(tid))
        if executed:
            log.debug("DAVE transition %s executed", tid)
        return executed
    except Exception:  # noqa: BLE001 - the voice websocket must never break here
        log.warning(
            "DAVE execute_transition(%s) failed; outgoing audio may not encrypt "
            "until the connection is refreshed",
            tid,
            exc_info=True,
        )
        return False


def session_group_user_ids(session: Any) -> list[int]:
    """Return remote/local user IDs currently in ``session``'s MLS group."""
    getter = getattr(session, "get_user_ids", None)
    if not callable(getter):
        return []
    try:
        raw_ids = getter() or ()
    except Exception:  # noqa: BLE001
        return []
    out: list[int] = []
    for item in raw_ids:
        try:
            # davey 0.2 returns List[int]; davey 0.1 returned List[str].
            uid = int(item)
        except (TypeError, ValueError):
            continue
        if uid > 0:
            out.append(uid)
    return out


def _decrypt_success_count(session: Any, user_id: int) -> int | None:
    getter = getattr(session, "get_decryption_stats", None)
    if not callable(getter):
        return None
    try:
        stats = getter(user_id, davey.MediaType.audio)
    except TypeError:
        try:
            stats = getter(user_id)
        except Exception:  # noqa: BLE001
            return None
    except Exception:  # noqa: BLE001
        return None
    succ = getattr(stats, "successes", None)
    return int(succ) if isinstance(succ, int) else None


def try_identify_dave_sender(
    session: Any,
    payload: bytes,
    *,
    own_user_id: int | None = None,
    already_mapped_users: Iterable[int] = (),
    max_candidates: int = 16,
) -> tuple[int, bytes] | None:
    """Cryptographically identify and decrypt an unmapped SSRC's DAVE frame.

    When a bot joins or moves into a channel where someone is *already*
    speaking continuously (or using open-mic), Discord does not emit opcode 5
    (``SPEAKING``) until that user's next silence-to-speech edge.  In an active
    DAVE group each member encrypts with a unique MLS-derived AES-128-GCM key
    ratchet, so authenticating the DAVE AEAD tag against the group's unmapped
    member ratchets both identifies ``ssrc -> user_id`` on the very first frame
    and recovers its Opus audio immediately.

    Safety invariants:
    * Only runs on structurally valid ``0xFAFA`` DAVE-encrypted frames.
    * Verifies ``DecryptionStats.successes`` strictly increased (when stats are
      exposed) so a passthrough frame can never produce a false binding.
    * Failed tag checks in ``davey`` are side-effect-free (nonce & ratchet
      generation state advances only inside ``report_cipher_success``).
    """
    if session is None or not bool(getattr(session, "ready", False)):
        return None
    if not is_dave_encrypted_frame(payload):
        return None

    group_uids = session_group_user_ids(session)
    if not group_uids:
        return None

    self_uid = own_user_id
    if self_uid is None:
        raw_self = getattr(session, "user_id", None)
        if isinstance(raw_self, int) and raw_self > 0:
            self_uid = raw_self

    mapped_set = set(already_mapped_users)
    unmapped: list[int] = []
    mapped: list[int] = []
    for uid in group_uids:
        if self_uid is not None and uid == self_uid:
            continue
        if uid in mapped_set:
            mapped.append(uid)
        else:
            unmapped.append(uid)

    # Probe unmapped group members first; fall back to mapped ones in case a
    # speaker rotated their audio SSRC without a preceding disconnect event.
    candidates = (unmapped + mapped)[: max(1, int(max_candidates))]
    for uid in candidates:
        before = _decrypt_success_count(session, uid)
        try:
            plaintext = session.decrypt(uid, davey.MediaType.audio, payload)
        except Exception:  # noqa: BLE001
            continue
        if not plaintext:
            continue
        after = _decrypt_success_count(session, uid)
        if before is not None and after is not None and after <= before:
            # Passed through unauthenticated rather than matching uid's key.
            continue
        return uid, bytes(plaintext)
    return None
