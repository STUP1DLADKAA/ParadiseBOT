"""Encryption at rest for MAL OAuth tokens.

Uses Fernet (AES-128-CBC + HMAC-SHA256) from the ``cryptography`` package,
which is already a project dependency. The key is a base64-encoded 32-byte
value stored in ``MAL_ENCRYPTION_KEY``. Nothing here touches the network or
Discord — it is pure symmetric encryption/decryption.
"""

from __future__ import annotations

import base64
import logging

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger("paradise.mal.encryption")


class MalEncryption:
    """Symmetric encryption for sensitive MAL token fields."""

    def __init__(self, key_b64: str) -> None:
        try:
            raw = base64.urlsafe_b64decode(key_b64 + "==")
            if len(raw) != 32:
                raise ValueError("encryption key must decode to exactly 32 bytes")
            # Fernet validates the key format itself; this also confirms it.
            self._fernet = Fernet(key_b64)
        except Exception as exc:  # noqa: BLE001 - invalid config must surface
            log.error("Invalid MAL_ENCRYPTION_KEY: %s", exc)
            raise

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a string, returning a base64-encoded ciphertext token."""
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt a Fernet token, returning the original plaintext.

        Raises ``InvalidToken`` if the key has rotated or the ciphertext is
        corrupted. Callers should treat that as "no usable token" and require
        re-authorization.
        """
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except InvalidToken:
            log.warning("Failed to decrypt MAL token — key may have rotated")
            raise
