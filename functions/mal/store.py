"""Supabase-backed token storage and the high-level MAL service.

``MalStore`` performs CRUD on the ``mal_tokens`` Supabase table. ``MalService``
combines the store, the OAuth client, the API client, and encryption into
higher-level operations: connect, disconnect, get a valid access token (with
auto-refresh), and make authenticated MAL API calls.

User isolation: every row is keyed by Discord user ID. The Supabase table has
RLS enabled with no policies — only the service-role key (held server-side)
can read or write. A Discord user can only ever touch their own row because
the bot always filters by the calling user's ID.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from cryptography.fernet import InvalidToken

from .api import MalClient, MalUnauthorized
from .encryption import MalEncryption
from .oauth import MalOAuth

log = logging.getLogger("paradise.mal.store")


class MalStore:
    """Supabase CRUD for MAL OAuth tokens, encrypted at rest."""

    def __init__(
        self,
        url: str,
        key: str,
        encryption: MalEncryption,
    ) -> None:
        self._url = url
        self._key = key
        self._encryption = encryption
        self._supabase_client = None  # lazy, created on first use

    async def _client(self) -> Any:
        if self._supabase_client is None:
            if not self._url or not self._key:
                raise RuntimeError(
                    "MAL store is not configured (SUPABASE_URL / SUPABASE_SECRET_KEY)"
                )
            from supabase import AsyncClientOptions, acreate_client

            self._supabase_client = await acreate_client(
                self._url,
                self._key,
                options=AsyncClientOptions(
                    auto_refresh_token=False,
                    persist_session=False,
                ),
            )
        return self._supabase_client

    async def configured(self) -> bool:
        """True if Supabase credentials are present."""
        return bool(self._url and self._key)

    async def get_tokens(self, discord_user_id: int) -> dict[str, Any] | None:
        """Return decrypted token fields for a Discord user, or None."""
        if not await self.configured():
            return None
        client = await self._client()
        try:
            resp = await client.table("mal_tokens").select("*").eq(
                "discord_user_id", discord_user_id
            ).execute()
        except Exception:  # noqa: BLE001
            log.exception("Failed to read MAL tokens for user %s", discord_user_id)
            return None
        rows = resp.data or []
        if not rows:
            return None
        row = rows[0]
        try:
            return {
                "discord_user_id": row["discord_user_id"],
                "mal_user_id": row["mal_user_id"],
                "access_token": self._encryption.decrypt(row["access_token_encrypted"]),
                "refresh_token": self._encryption.decrypt(
                    row["refresh_token_encrypted"]
                ),
                "token_expires_at": row["token_expires_at"],
            }
        except InvalidToken:
            log.warning(
                "Stored MAL tokens for user %s cannot be decrypted — key rotated",
                discord_user_id,
            )
            return None

    async def store_tokens(
        self,
        discord_user_id: int,
        mal_user_id: int,
        access_token: str,
        refresh_token: str,
        expires_in: int,
    ) -> bool:
        """Encrypt and upsert MAL tokens for a Discord user."""
        if not await self.configured():
            return False
        client = await self._client()
        expires_at = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + expires_in)
        )
        updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            await client.table("mal_tokens").upsert(
                {
                    "discord_user_id": discord_user_id,
                    "mal_user_id": mal_user_id,
                    "access_token_encrypted": self._encryption.encrypt(access_token),
                    "refresh_token_encrypted": self._encryption.encrypt(refresh_token),
                    "token_expires_at": expires_at,
                    "updated_at": updated_at,
                },
                on_conflict="discord_user_id",
            ).execute()
            return True
        except Exception:  # noqa: BLE001
            log.exception("Failed to store MAL tokens for user %s", discord_user_id)
            return False

    async def delete_tokens(self, discord_user_id: int) -> bool:
        """Remove all MAL tokens for a Discord user."""
        if not await self.configured():
            return False
        client = await self._client()
        try:
            await client.table("mal_tokens").delete().eq(
                "discord_user_id", discord_user_id
            ).execute()
            return True
        except Exception:  # noqa: BLE001
            log.exception("Failed to delete MAL tokens for user %s", discord_user_id)
            return False

    async def has_tokens(self, discord_user_id: int) -> bool:
        """True if the Discord user has linked MAL tokens."""
        row = await self.get_tokens(discord_user_id)
        return row is not None


class MalService:
    """High-level MAL operations: token lifecycle + authenticated API calls."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        callback_url: str,
        encryption: MalEncryption,
        store: MalStore,
        session=None,
    ) -> None:
        self._oauth = MalOAuth(client_id, client_secret, callback_url, session)
        self._client = MalClient(client_id, session)
        self._store = store
        self._encryption = encryption

    # ------------------------------------------------------------------
    # OAuth flow
    # ------------------------------------------------------------------

    def start_connect_flow(self, discord_user_id: int) -> tuple[str, str]:
        """Begin PKCE OAuth flow. Returns (authorize_url, state)."""
        return self._oauth.start_flow(discord_user_id)

    def consume_connect_flow(
        self, discord_user_id: int, state: str
    ) -> str | None:
        """Consume the stored PKCE flow, returning the code_verifier or None."""
        return self._oauth.consume_flow(discord_user_id, state)

    async def exchange_and_store(
        self,
        discord_user_id: int,
        code: str,
        code_verifier: str,
    ) -> int | None:
        """Exchange a code for tokens, store them, return the MAL user ID."""
        pair = await self._oauth.exchange_code(code, code_verifier)
        if pair is None:
            return None
        # Fetch the MAL user profile to get the MAL user ID.
        profile = await self._client.get_user(pair.access_token)
        if profile is None:
            log.warning("Could not fetch MAL profile after token exchange")
            mal_user_id = 0
        else:
            mal_user_id = int(profile.get("id", 0))
        ok = await self._store.store_tokens(
            discord_user_id,
            mal_user_id,
            pair.access_token,
            pair.refresh_token,
            pair.expires_in,
        )
        if not ok:
            return None
        return mal_user_id

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    async def get_valid_access_token(self, discord_user_id: int) -> str | None:
        """Return a usable access token, refreshing if needed.

        Returns None if the user has no linked account or tokens are unusable.
        """
        tokens = await self._store.get_tokens(discord_user_id)
        if tokens is None:
            return None

        access_token = tokens["access_token"]

        # Check expiry.
        expires_at = tokens.get("token_expires_at")
        if expires_at:
            try:
                exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if exp.timestamp() > time.time() + 60:
                    return access_token
            except (ValueError, AttributeError):
                pass

        # Expired or unknown — try refresh.
        refresh_token = tokens["refresh_token"]
        refreshed = await self._oauth.refresh_tokens(refresh_token)
        if refreshed is None:
            log.warning(
                "Token refresh failed for user %s — re-auth required", discord_user_id
            )
            await self._store.delete_tokens(discord_user_id)
            return None

        # Store the refreshed tokens.
        await self._store.store_tokens(
            discord_user_id,
            tokens["mal_user_id"],
            refreshed.access_token,
            refreshed.refresh_token,
            refreshed.expires_in,
        )
        return refreshed.access_token

    async def disconnect(
        self, discord_user_id: int, revoke: bool = True
    ) -> bool:
        """Revoke tokens (optionally) and delete from storage."""
        tokens = await self._store.get_tokens(discord_user_id)
        if revoke and tokens:
            await self._oauth.revoke_token(tokens["refresh_token"])
            await self._oauth.revoke_token(tokens["access_token"])
        return await self._store.delete_tokens(discord_user_id)

    # ------------------------------------------------------------------
    # Authenticated API calls with auto-refresh
    # ------------------------------------------------------------------

    async def _with_valid_token(
        self, discord_user_id: int, fn, *args, **kwargs
    ) -> Any:
        """Run ``fn(access_token, *args, **kwargs)`` with a valid token.

        On ``MalUnauthorized``, refreshes the token and retries once.
        """
        token = await self.get_valid_access_token(discord_user_id)
        if token is None:
            return None
        try:
            return await fn(token, *args, **kwargs)
        except MalUnauthorized:
            # Token was invalid — force refresh and retry once.
            tokens = await self._store.get_tokens(discord_user_id)
            if tokens is None:
                return None
            refreshed = await self._oauth.refresh_tokens(tokens["refresh_token"])
            if refreshed is None:
                await self._store.delete_tokens(discord_user_id)
                return None
            await self._store.store_tokens(
                discord_user_id,
                tokens["mal_user_id"],
                refreshed.access_token,
                refreshed.refresh_token,
                refreshed.expires_in,
            )
            try:
                return await fn(refreshed.access_token, *args, **kwargs)
            except MalUnauthorized:
                log.warning(
                    "Token still invalid after refresh for user %s", discord_user_id
                )
                await self._store.delete_tokens(discord_user_id)
                return None

    # Convenience wrappers around MalClient with auto-refresh.

    async def get_profile(self, discord_user_id: int) -> dict[str, Any] | None:
        return await self._with_valid_token(
            discord_user_id, self._client.get_user
        )

    async def get_list(
        self,
        discord_user_id: int,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any] | None:
        return await self._with_valid_token(
            discord_user_id,
            self._client.get_animelist,
            status=status,
            limit=limit,
            offset=offset,
        )

    async def update_entry(
        self,
        discord_user_id: int,
        anime_id: int,
        **fields: Any,
    ) -> dict[str, Any] | None:
        return await self._with_valid_token(
            discord_user_id,
            self._client.update_animelist,
            anime_id,
            **fields,
        )

    async def remove_entry(self, discord_user_id: int, anime_id: int) -> bool:
        return await self._with_valid_token(
            discord_user_id,
            self._client.delete_animelist,
            anime_id,
        )

    # Public (unauthenticated) endpoints — no token needed.

    async def search_anime(
        self, query: str, limit: int = 10, offset: int = 0
    ) -> dict[str, Any] | None:
        return await self._client.search_anime(query, limit=limit, offset=offset)

    async def get_anime(self, anime_id: int) -> dict[str, Any] | None:
        return await self._client.get_anime(anime_id)

    @property
    def oauth(self) -> MalOAuth:
        return self._oauth

    @property
    def store(self) -> MalStore:
        return self._store
