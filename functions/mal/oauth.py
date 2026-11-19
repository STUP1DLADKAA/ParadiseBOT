"""PKCE-based OAuth 2.0 Authorization Code flow for MyAnimeList.

Never handles MAL passwords. The user is sent to MAL's authorization page;
MAL redirects back with an authorization code which we exchange for tokens.
PKCE (RFC 7636) protects the code exchange even though the callback URL is a
public endpoint.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass, field

import aiohttp

from functions.perf import shared_session

log = logging.getLogger("paradise.mal.oauth")

MAL_AUTHORIZE_URL = "https://myanimelist.net/v1/oauth2/authorize"
MAL_TOKEN_URL = "https://api.myanimelist.net/v2/oauth2/token"
MAL_REVOKE_URL = "https://api.myanimelist.net/v2/oauth2/revoke"

# OAuth state / PKCE flow lifetime: 10 minutes.
FLOW_TTL_SECONDS = 600


@dataclass
class MalTokenPair:
    """OAuth token response from MAL."""

    access_token: str
    refresh_token: str
    expires_in: int
    token_type: str = "Bearer"

    @property
    def expires_at(self) -> float:
        return time.time() + self.expires_in

    @property
    def is_expired(self) -> bool:
        # 60-second safety margin so we never send a token that's about to die.
        return time.time() >= (self.expires_at - 60)


@dataclass
class MalOAuthState:
    """In-flight OAuth transaction, keyed by Discord user ID."""

    code_verifier: str
    state: str
    discord_user_id: int
    created_at: float = field(default_factory=time.time)

    @property
    def is_expired(self) -> bool:
        return time.time() - self.created_at > FLOW_TTL_SECONDS


def generate_code_verifier() -> str:
    """Generate a PKCE code verifier (43-128 base64url chars)."""
    return secrets.token_urlsafe(96)[:128]


def generate_code_challenge(verifier: str) -> str:
    """SHA-256 of the verifier, base64url-encoded without padding (S256)."""
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def generate_state() -> str:
    return secrets.token_urlsafe(32)


class MalOAuth:
    """Manages PKCE OAuth flow state and token exchange/refresh/revocation."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        callback_url: str,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.callback_url = callback_url
        self._session = session
        # discord_user_id -> MalOAuthState. In-memory only, single process.
        self._flows: dict[int, MalOAuthState] = {}

    @property
    def session(self) -> aiohttp.ClientSession:
        """Lazily fall back to the shared aiohttp session when none was given."""
        if self._session is None:
            self._session = shared_session("mal")
        return self._session

    # ------------------------------------------------------------------
    # Authorization URL (step 1 of the flow)
    # ------------------------------------------------------------------

    def start_flow(self, discord_user_id: int) -> tuple[str, str]:
        """Begin a PKCE flow for ``discord_user_id``.

        Returns ``(authorize_url, state)``. The verifier and state are stored
        in memory and consumed when the callback arrives.
        """
        # Purge any stale flow for this user before starting a new one.
        self._flows.pop(discord_user_id, None)

        verifier = generate_code_verifier()
        challenge = generate_code_challenge(verifier)
        state = generate_state()

        self._flows[discord_user_id] = MalOAuthState(
            code_verifier=verifier,
            state=state,
            discord_user_id=discord_user_id,
        )

        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.callback_url,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        query = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{MAL_AUTHORIZE_URL}?{query}"
        log.debug("Started MAL OAuth flow for discord user %s", discord_user_id)
        return url, state

    def consume_flow(self, discord_user_id: int, state: str) -> str | None:
        """Return the stored code_verifier if ``state`` matches, else None.

        Consumes (removes) the flow regardless of outcome so a code can only
        be exchanged once.
        """
        flow = self._flows.get(discord_user_id)
        if flow is None or flow.is_expired:
            self._flows.pop(discord_user_id, None)
            return None
        if flow.state != state:
            log.warning("OAuth state mismatch for discord user %s", discord_user_id)
            self._flows.pop(discord_user_id, None)
            return None
        verifier = flow.code_verifier
        self._flows.pop(discord_user_id, None)
        log.debug("Consumed MAL OAuth flow for discord user %s", discord_user_id)
        return verifier

    # ------------------------------------------------------------------
    # Token exchange (step 3 of the flow)
    # ------------------------------------------------------------------

    async def exchange_code(
        self, code: str, code_verifier: str
    ) -> MalTokenPair | None:
        """Exchange an authorization code for access + refresh tokens."""
        data = {
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": self.callback_url,
        }
        try:
            async with self.session.post(MAL_TOKEN_URL, data=data) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.warning(
                        "MAL token exchange failed: HTTP %s %s", resp.status, body
                    )
                    return None
                payload = await resp.json()
                return MalTokenPair(
                    access_token=payload["access_token"],
                    refresh_token=payload["refresh_token"],
                    expires_in=int(payload["expires_in"]),
                    token_type=payload.get("token_type", "Bearer"),
                )
        except Exception:  # noqa: BLE001 - network failures must not crash
            log.exception("MAL token exchange raised an exception")
            return None

    async def refresh_tokens(self, refresh_token: str) -> MalTokenPair | None:
        """Refresh an access token using a refresh token."""
        data = {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": refresh_token,
        }
        try:
            async with self.session.post(MAL_TOKEN_URL, data=data) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.warning(
                        "MAL token refresh failed: HTTP %s %s", resp.status, body
                    )
                    return None
                payload = await resp.json()
                return MalTokenPair(
                    access_token=payload["access_token"],
                    refresh_token=payload.get("refresh_token", refresh_token),
                    expires_in=int(payload["expires_in"]),
                    token_type=payload.get("token_type", "Bearer"),
                )
        except Exception:  # noqa: BLE001
            log.exception("MAL token refresh raised an exception")
            return None

    async def revoke_token(self, token: str) -> bool:
        """Revoke an access or refresh token. Returns True on success."""
        data = {
            "token": token,
            "token_type_hint": "access_token",
        }
        try:
            async with self.session.post(MAL_REVOKE_URL, data=data) as resp:
                return resp.status == 200
        except Exception:  # noqa: BLE001
            log.exception("MAL token revocation raised an exception")
            return False
