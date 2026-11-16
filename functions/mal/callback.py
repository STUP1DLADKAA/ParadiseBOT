"""aiohttp OAuth callback server for MyAnimeList authorization.

MAL redirects the user here after they authorize. The callback:
1. Validates the ``state`` parameter against the in-memory PKCE flow.
2. Exchanges the authorization code for access + refresh tokens.
3. Stores encrypted tokens in Supabase, bound to the Discord user ID.
4. Returns a simple HTML success page telling the user to return to Discord.

The server runs alongside the Discord bot (started in ``setup_hook``,
stopped in ``close``) and shares the :class:`MalService` instance.
"""

from __future__ import annotations

import logging

from aiohttp import web

from .store import MalService

log = logging.getLogger("paradise.mal.callback")

# HTML returned after a successful callback.
_SUCCESS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MyAnimeList connected — ParadiseBOT</title>
<style>
  body { font-family: system-ui, sans-serif; background: #2f3136; color: #fff;
         display: flex; flex-direction: column; align-items: center;
         justify-content: center; min-height: 100vh; margin: 0; padding: 20px; }
  .card { background: #36393f; border: 1px solid #4f545c; border-radius: 8px;
          padding: 32px 48px; text-align: center; box-shadow: 0 8px 24px rgba(0,0,0,.4); }
  h1 { margin: 0 0 12px; font-size: 1.5rem; color: #00bfff; }
  p { margin: 0 0 20px; color: #b9bbbe; font-size: 1rem; line-height: 1.5; }
  code { background: #202225; padding: 2px 6px; border-radius: 4px; color: #f04747; }
</style>
</head>
<body>
<div class="card">
  <h1>✅ MyAnimeList connected</h1>
  <p>Your MAL account has been linked to your Discord user. Return to Discord and run <code>/mal profile</code> to verify everything is working.</p>
</div>
</body>
</html>"""

# HTML returned when the callback is invalid or missing.
_ERROR_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MyAnimeList connection failed — ParadiseBOT</title>
<style>
  body { font-family: system-ui, sans-serif; background: #2f3136; color: #fff;
         display: flex; flex-direction: column; align-items: center;
         justify-content: center; min-height: 100vh; margin: 0; padding: 20px; }
  .card { background: #36393f; border: 1px solid #4f545c; border-radius: 8px;
          padding: 32px 48px; text-align: center; box-shadow: 0 8px 24px rgba(0,0,0,.4); }
  h1 { margin: 0 0 12px; font-size: 1.5rem; color: #f04747; }
  p { margin: 0 0 20px; color: #b9bbbe; font-size: 1rem; line-height: 1.5; }
</style>
</head>
<body>
<div class="card">
  <h1>❌ Connection failed</h1>
  <p>Could not complete the MyAnimeList connection. Please try <code>/mal connect</code> again in Discord.</p>
</div>
</body>
</html>"""


class MalCallbackServer:
    """aiohttp web server handling the MAL OAuth redirect."""

    def __init__(
        self,
        service: MalService,
        host: str = "0.0.0.0",
        port: int = 8765,
    ) -> None:
        self._service = service
        self._host = host
        self._port = port
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the callback HTTP server."""
        if self._runner is not None:
            return
        self._app = web.Application()
        self._app.router.add_get("/mal/oauth/callback", self._handle_callback)
        self._app.router.add_get("/mal/oauth/health", self._handle_health)
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()
        log.info("MAL OAuth callback server listening on %s:%d", self._host, self._port)

    async def stop(self) -> None:
        """Stop the callback HTTP server."""
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        self._app = None
        log.info("MAL OAuth callback server stopped")

    @property
    def callback_url(self) -> str:
        """Public callback URL (for configuring in the MAL developer portal)."""
        return f"http://{self._host}:{self._port}/mal/oauth/callback"

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "service": "mal-oauth-callback"})

    async def _handle_callback(self, request: web.Request) -> web.Response:
        """Handle MAL's OAuth redirect after user authorization."""
        code = request.query.get("code")
        state = request.query.get("state")
        error = request.query.get("error")
        error_description = request.query.get("error_description")

        # MAL reported an error (user cancelled, etc.).
        if error:
            log.warning("MAL OAuth error: %s %s", error, error_description)
            return web.Response(text=_ERROR_HTML, content_type="text/html")

        if not code or not state:
            log.warning("MAL callback missing code or state")
            return web.Response(text=_ERROR_HTML, content_type="text/html")

        # The state encodes the Discord user ID. Look up the pending flow.
        # We need the discord_user_id to consume the flow. The state value
        # itself is random; we stored discord_user_id -> flow in memory.
        # To find the right flow, we iterate — there should be very few.
        discord_user_id = self._find_user_by_state(state)
        if discord_user_id is None:
            log.warning("No pending OAuth flow for state %s", state)
            return web.Response(text=_ERROR_HTML, content_type="text/html")

        code_verifier = self._service.consume_connect_flow(discord_user_id, state)
        if code_verifier is None:
            log.warning("Failed to consume OAuth flow for user %s", discord_user_id)
            return web.Response(text=_ERROR_HTML, content_type="text/html")

        mal_user_id = await self._service.exchange_and_store(
            discord_user_id, code, code_verifier
        )
        if mal_user_id is None:
            log.warning("Token exchange failed for user %s", discord_user_id)
            return web.Response(text=_ERROR_HTML, content_type="text/html")

        log.info(
            "MAL account linked: discord_user=%s mal_user=%s",
            discord_user_id,
            mal_user_id,
        )
        return web.Response(text=_SUCCESS_HTML, content_type="text/html")

    def _find_user_by_state(self, state: str) -> int | None:
        """Find the Discord user ID associated with an OAuth state.

        Iterates pending flows. This is O(n) but flows are short-lived and
        few.
        """
        for discord_user_id, flow in self._service.oauth._flows.items():  # noqa: SLF001
            if flow.state == state and not flow.is_expired:
                return discord_user_id
        return None
