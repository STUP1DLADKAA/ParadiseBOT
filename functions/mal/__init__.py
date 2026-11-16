"""MyAnimeList integration for ParadiseBOT.

Encrypted token storage, PKCE OAuth 2.0, MAL API v2 client, Discord commands,
and an OAuth callback server. Tokens are bound to a Discord user ID and
encrypted at rest in Supabase using a key from ``MAL_ENCRYPTION_KEY``.
"""

from .api import MalClient, MalApiError, MalRateLimited, MalUnauthorized
from .callback import MalCallbackServer
from .encryption import MalEncryption
from .oauth import MalOAuth, MalOAuthState, MalTokenPair
from .store import MalService, MalStore

__all__ = [
    "MalClient",
    "MalApiError",
    "MalRateLimited",
    "MalUnauthorized",
    "MalOAuth",
    "MalOAuthState",
    "MalTokenPair",
    "MalEncryption",
    "MalStore",
    "MalService",
    "MalCallbackServer",
]
