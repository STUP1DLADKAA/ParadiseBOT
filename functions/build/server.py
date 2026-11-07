"""Live preview server and deployment engine for AI Build Mode."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
from pathlib import Path
from typing import Any

from aiohttp import web
from functions.build.config import BuildConfig
from functions.build.session import ProjectStore

log = logging.getLogger("paradise.build.server")


class PreviewServer:
    """Isolated, multi-tenant HTTP server for serving project live previews."""

    def __init__(self, config: BuildConfig, store: ProjectStore) -> None:
        self.config = config
        self.store = store
        self.app: web.Application | None = None
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self._running = False
        self._cleanup_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the aiohttp preview server."""
        if self._running:
            return

        self.app = web.Application(middlewares=[self._security_headers_middleware])
        self.app.router.add_get("/", self._handle_index)
        self.app.router.add_get("/healthz", self._handle_health)
        self.app.router.add_get("/preview/{project_id}", self._handle_preview_redirect)
        self.app.router.add_get("/preview/{project_id}/", self._handle_preview_root)
        self.app.router.add_get("/preview/{project_id}/{path:.*}", self._handle_preview_file)

        self.runner = web.AppRunner(self.app)
        await self.runner.setup()

        self.site = web.TCPSite(
            self.runner,
            self.config.preview_host,
            self.config.preview_port,
        )
        await self.site.start()
        self._running = True
        log.info(
            "Live Preview Server started on %s:%d (external base: %s)",
            self.config.preview_host,
            self.config.preview_port,
            self.config.get_preview_url("<project_id>"),
        )

        # Background cleanup loop for expired projects
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop(self) -> None:
        """Stop the preview server and cleanup."""
        if not self._running:
            return

        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        if self.site:
            await self.site.stop()
        if self.runner:
            await self.runner.cleanup()

        self._running = False
        log.info("Live Preview Server stopped")

    @web.middleware
    async def _security_headers_middleware(self, request: web.Request, handler) -> web.Response:
        """Apply security headers, sandbox isolation, and CORS."""
        response = await handler(request)
        # Allow embedding in iframes / previews while isolating origin
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
        # X-Content-Type-Options prevent MIME sniffing
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Prevent caching of live previews so edits are seen instantly
        if request.path.startswith("/preview/"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    async def _handle_index(self, request: web.Request) -> web.Response:
        """Root landing page explaining Build Mode preview service."""
        html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ParadiseBOT Live Preview Hub</title>
    <style>
        :root { --bg: #0f172a; --card: #1e293b; --text: #f8fafc; --accent: #38bdf8; }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; padding: 20px; box-sizing: border-box; }
        .card { background: var(--card); border: 1px solid #334155; border-radius: 16px; padding: 32px; max-width: 480px; text-align: center; box-shadow: 0 20px 25px -5px rgba(0,0,0,0.5); }
        h1 { margin-top: 0; color: var(--accent); font-size: 1.8rem; }
        p { color: #94a3b8; line-height: 1.6; }
        .badge { display: inline-block; padding: 6px 12px; background: rgba(56, 189, 248, 0.1); color: var(--accent); border-radius: 9999px; font-size: 0.85rem; font-weight: 600; margin-bottom: 12px; }
    </style>
</head>
<body>
    <div class="card">
        <div class="badge">Live Sandbox</div>
        <h1>ParadiseBOT AI Build Hub</h1>
        <p>Interactive preview server is operational. Mention ParadiseBOT in Discord with <code>@ParadiseBOT build ...</code> to create and preview apps instantly.</p>
    </div>
</body>
</html>"""
        return web.Response(text=html, content_type="text/html")

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "service": "paradise-build-preview"})

    async def _handle_preview_redirect(self, request: web.Request) -> web.Response:
        project_id = request.match_info.get("project_id", "")
        raise web.HTTPFound(f"/preview/{project_id}/")

    async def _handle_preview_root(self, request: web.Request) -> web.Response:
        return await self._serve_project_file(request, "index.html")

    async def _handle_preview_file(self, request: web.Request) -> web.Response:
        rel_path = request.match_info.get("path", "")
        if not rel_path:
            rel_path = "index.html"
        return await self._serve_project_file(request, rel_path)

    async def _serve_project_file(self, request: web.Request, rel_path: str) -> web.Response:
        project_id = request.match_info.get("project_id", "")
        project = self.store.get_project(project_id)
        if not project:
            return self._not_found_response("Project not found or expired.")

        if project.is_expired:
            return self._expired_response(project)

        # Sanitize requested path to avoid directory traversal
        clean_path = rel_path.replace("\\", "/").strip("/ ")
        if ".." in clean_path.split("/"):
            return web.HTTPForbidden(text="Access denied")

        target_dir = self.store._project_dir(project_id)
        file_path = (target_dir / clean_path).resolve()

        # Strict boundary check
        if not str(file_path).startswith(str(target_dir)):
            return web.HTTPForbidden(text="Access denied")

        if not file_path.exists():
            # If requesting a directory without trailing slash, try index.html
            if file_path.is_dir():
                file_path = file_path / "index.html"
            # If requesting without extension, try .html
            elif not file_path.suffix and (target_dir / f"{clean_path}.html").exists():
                file_path = target_dir / f"{clean_path}.html"

        if not file_path.exists() or not file_path.is_file():
            # If index.html exists, return it for single page app (SPA) fallback
            index_path = target_dir / "index.html"
            if index_path.exists():
                file_path = index_path
            else:
                return self._not_found_response("Requested file not found in project.")

        # Determine mime type
        mime_type, _ = mimetypes.guess_type(str(file_path))
        if mime_type is None:
            if file_path.suffix in (".jsx", ".tsx", ".ts"):
                mime_type = "application/javascript"
            elif file_path.suffix == ".json":
                mime_type = "application/json"
            else:
                mime_type = "text/plain"

        try:
            with open(file_path, "rb") as f:
                content = f.read()
            return web.Response(body=content, content_type=mime_type)
        except Exception as e:
            log.warning("Error reading project file %s: %s", file_path, e)
            return web.HTTPInternalServerError(text="Failed to read file")

    def _not_found_response(self, message: str) -> web.Response:
        html = f"""<!DOCTYPE html>
<html>
<head><title>Preview Not Found</title>
<style>
body {{ font-family: system-ui, sans-serif; background: #0f172a; color: #f8fafc; text-align: center; padding: 50px; }}
h1 {{ color: #f43f5e; }}
p {{ color: #94a3b8; }}
</style>
</head>
<body>
<h1>404 - Project Preview Not Found</h1>
<p>{message}</p>
</body>
</html>"""
        return web.Response(text=html, status=404, content_type="text/html")

    def _expired_response(self, project: Any) -> web.Response:
        html = f"""<!DOCTYPE html>
<html>
<head><title>Preview Expired</title>
<style>
body {{ font-family: system-ui, sans-serif; background: #0f172a; color: #f8fafc; text-align: center; padding: 50px; }}
h1 {{ color: #eab308; }}
p {{ color: #94a3b8; }}
</style>
</head>
<body>
<h1>Preview Expired</h1>
<p>The preview for <strong>{project.title}</strong> has expired due to inactivity.</p>
<p>Mention ParadiseBOT in Discord with an update request to re-deploy your project.</p>
</body>
</html>"""
        return web.Response(text=html, status=410, content_type="text/html")

    async def _cleanup_loop(self) -> None:
        """Periodically mark old inactive projects expired."""
        while self._running:
            try:
                await asyncio.sleep(self.config.preview_cleanup_interval)
                cleaned = self.store.cleanup_expired(self.config.preview_expiration_seconds)
                if cleaned > 0:
                    log.info("Cleaned up %d expired project previews", cleaned)
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("Error in preview cleanup loop")
