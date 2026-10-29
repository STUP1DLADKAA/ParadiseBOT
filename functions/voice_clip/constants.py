"""Constants, strict authorization, input validation and errors for /clip."""

from __future__ import annotations

import os

from functions.helpers import ParadiseError

#: Default operator ID (the deployment this bot grew up in). Override with
#: ``CLIP_ALLOWED_USER_IDS`` — a comma-separated list of Discord user IDs.
AUTHORIZED_CLIP_USER_ID: int = 922359954025885737

CLIP_USERS_ENV = "CLIP_ALLOWED_USER_IDS"

MIN_CLIP_DURATION: int = 1
MAX_CLIP_DURATION: int = 180

SAMPLE_RATE: int = 48_000
FRAME_SAMPLES: int = 960  # 20 ms @ 48 kHz
FRAME_DURATION_SECONDS: float = FRAME_SAMPLES / SAMPLE_RATE  # 0.02s
OPUS_SILENCE: bytes = b"\xf8\xff\xfe"
MAX_OPUS_PACKET_BYTES: int = 1500
PACKET_OVERHEAD_BYTES: int = 64

UNAUTHORIZED_CLIP_MESSAGE: str = "something went wrong trying to process that clip. try again in a moment."


class ClipError(ParadiseError):
    """Base user-safe error for the rolling voice clip system."""


class ClipAuthorizationError(ClipError):
    """Raised when a user outside the operator allow-list invokes clip logic."""


class ClipValidationError(ClipError):
    """Raised when clip parameters (such as duration) are invalid."""


class ClipUnavailableError(ClipError):
    """Raised when voice recording is inactive or unavailable in the server."""


class NoUsableAudioError(ClipError):
    """Raised when the requested interval has no non-silent captured speech."""


class ClipBusyError(ClipError):
    """Raised when export concurrency/queue capacity is full."""


class ClipExportError(ClipError):
    """Raised when audio decoding, mixing or encoding fails."""


class ClipExportTimeoutError(ClipExportError):
    """Raised when clip generation exceeds its configured deadline."""


class ClipFileTooLargeError(ClipExportError):
    """Raised when the encoded clip exceeds the configured upload limit."""


class ClipDeliveryError(ClipError):
    """Raised when sending the generated clip via Discord DM fails."""


def authorized_clip_user_ids() -> tuple[int, ...]:
    """The IDs allowed to request/receive clips, from ``CLIP_ALLOWED_USER_IDS``.

    Falls back to the historical single ID so an existing deployment keeps
    working unchanged; an empty value disables ``/clip`` entirely.
    """
    raw = (os.environ.get(CLIP_USERS_ENV) or "").strip()
    if not raw:
        return (AUTHORIZED_CLIP_USER_ID,)
    ids: list[int] = []
    for chunk in raw.replace(";", ",").replace(" ", ",").split(","):
        value = chunk.strip().strip("<>@!")
        if value.isdigit() and int(value) > 0 and int(value) not in ids:
            ids.append(int(value))
    return tuple(ids)


def authorize_clip_user(user_id: object) -> bool:
    """Return True only for an authorized operator ID (never for ``True``/``1``)."""
    if type(user_id) is not int:
        return False
    return user_id in authorized_clip_user_ids()


def require_clip_authorization(user_id: object) -> int:
    """Enforce server-side immutable Discord ID authorization."""
    if not authorize_clip_user(user_id):
        raise ClipAuthorizationError(UNAUTHORIZED_CLIP_MESSAGE)
    assert isinstance(user_id, int)
    return user_id


def validate_clip_duration(duration: object) -> int:
    """Validate that ``duration`` is a whole number of seconds in [1, 180]."""
    if isinstance(duration, bool) or not isinstance(duration, int):
        raise ClipValidationError(
            f"Duration must be a whole number of seconds from {MIN_CLIP_DURATION} to {MAX_CLIP_DURATION}."
        )
    if duration < MIN_CLIP_DURATION or duration > MAX_CLIP_DURATION:
        raise ClipValidationError(
            f"Duration must be between {MIN_CLIP_DURATION} and {MAX_CLIP_DURATION} seconds."
        )
    return duration
