"""DAVE E2EE transition bridge for davey 0.2+ running under discord.py 2.7.x.

The root cause this repairs
---------------------------

Discord enforces end-to-end encryption (the DAVE protocol) on voice.  discord.py
builds a :class:`davey.DaveSession` and drives the MLS handshake, but **davey
0.2.0 (released 2026-11-02) changed its API**: ``process_commit`` and
``process_welcome`` no longer install key ratchets.  The caller must now run the
transition state machine itself:

* voice gateway op 21 (``DAVE_PREPARE_TRANSITION``) → ``session.prepare_transition(id, version)``
  — this is the step that installs the per-user **decryption** key ratchets;
* voice gateway op 22 (``DAVE_EXECUTE_TRANSITION``) → ``session.execute_transition(id)``
  — this switches the **encryption** key to the prepared transition.

discord.py 2.7.1 (still the latest release) tracks the transition ids in
``VoiceConnectionState.dave_pending_transitions`` but **never calls either
method on the session** (upstream fix:
https://github.com/Rapptz/discord.py/pull/10548, still unmerged).  The result:
the DAVE session can decrypt nothing, so *every* incoming voice packet fails
E2EE decryption and is dropped — which is exactly how ``/clip`` ended up
reporting "No incoming voice audio has been received in this channel yet"
while everything else about the connection looked healthy.

This module applies the missing calls from ParadiseBOT's own voice-websocket
hook.  It is deliberately defensive:

* on davey < 0.2 the methods do not exist and the bridge is a no-op
  (0.1.x installs keys inside ``process_commit``/``process_welcome`` itself);
* if a future discord.py release starts calling ``prepare_transition`` /
  ``execute_transition`` itself, the duplicate calls remain harmless — davey
  0.2 ignores duplicate key-ratchet installs and returns ``False`` from
  ``execute_transition`` for an unknown pending transition;
* every call is wrapped so a bridge failure can never break the voice
  websocket.

The reference behaviour is the libdave sample client and the DAVE protocol
whitepaper (https://daveprotocol.com): prepare on opcode 21, execute on
opcode 22, and prepare/execute happen for *every* MLS group change — including
the very first epoch, which is why the session is unusable without it.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

#: Voice gateway opcodes handled by the bridge (discord-api-types voice/v8).
DAVE_PREPARE_TRANSITION_OP: int = 21
DAVE_EXECUTE_TRANSITION_OP: int = 22


def session_supports_transitions(session: Any) -> bool:
    """True when ``session`` is a davey >= 0.2 ``DaveSession``.

    davey 0.2 introduced ``prepare_transition`` / ``execute_transition``;
    0.1.x (which installs keys during commit/welcome processing) lacks both,
    and for those versions no bridge action is needed or possible.
    """
    return session is not None and hasattr(session, "prepare_transition") and hasattr(
        session, "execute_transition"
    )


def prepare_dave_transition(
    session: Any, transition_id: int, protocol_version: int
) -> bool:
    """Run ``session.prepare_transition(id, version)`` — installs decrypt keys.

    Returns True when the session accepted the transition.  Never raises.
    """
    if not session_supports_transitions(session):
        return False
    try:
        session.prepare_transition(int(transition_id), int(protocol_version))
        log.debug(
            "DAVE transition %s prepared (protocol v%s)",
            transition_id,
            protocol_version,
        )
        return True
    except Exception:  # noqa: BLE001 - the voice websocket must never break here
        log.warning(
            "DAVE prepare_transition(%s, v%s) failed; incoming audio may not "
            "decrypt until the connection is refreshed",
            transition_id,
            protocol_version,
            exc_info=True,
        )
        return False


def execute_dave_transition(session: Any, transition_id: int) -> bool:
    """Run ``session.execute_transition(id)`` — switches the encryption key.

    Returns True when a prepared transition was executed.  Never raises.
    """
    if not session_supports_transitions(session):
        return False
    try:
        executed = bool(session.execute_transition(int(transition_id)))
        if executed:
            log.debug("DAVE transition %s executed", transition_id)
        return executed
    except Exception:  # noqa: BLE001 - the voice websocket must never break here
        log.warning(
            "DAVE execute_transition(%s) failed; outgoing audio may not encrypt "
            "until the connection is refreshed",
            transition_id,
            exc_info=True,
        )
        return False
