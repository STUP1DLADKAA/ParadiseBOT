"""Tests for AI Build Mode: router detection, configuration, sessions, generator, preview server, and orchestrator."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
from functions.build.config import BuildConfig
from functions.build.generator import BuildGenerator, GeneratedProject
from functions.build.orchestrator import BuildOrchestrator
from functions.build.server import PreviewServer
from functions.build.session import ProjectSession, ProjectStore
from functions.router import BUILD, Route, route_query


class TestBuildRouter(unittest.TestCase):
    """Verify natural-language build requests route to BUILD correctly."""

    def test_build_new_website_triggers(self):
        cases = [
            "build me a modern calculator website",
            "make me a Discord bot dashboard",
            "create a modern landing page for my Minecraft server",
            "build me a calculator",
            "make a beautiful animated music player UI",
            "build me a working todo app",
            "please build me a portfolio website",
            "spin up a simple counter app",
        ]
        for query in cases:
            r = route_query(query)
            self.assertEqual(r.kind, BUILD, f"Failed for {query}")
            self.assertEqual(r.payload2, "new")

    def test_build_iterative_edits(self):
        cases = [
            "make the background black",
            "add a login page",
            "improve the animations",
            "make the entire UI more premium",
            "add a mobile navigation menu",
            "change the button color to red",
            "style the header with neon colors",
        ]
        for query in cases:
            r = route_query(query)
            self.assertEqual(r.kind, BUILD, f"Failed for {query}")
            self.assertEqual(r.payload2, "edit")

    def test_non_build_requests_are_not_stolen(self):
        cases = [
            ("weather in Delhi", "weather"),
            ("what's 2 + 2", "calculate"),
            ("100 USD to INR", "convert"),
            ("define algorithm", "define"),
            ("roast me", "roast"),
        ]
        for query, expected_kind in cases:
            r = route_query(query)
            self.assertEqual(r.kind, expected_kind, f"Wrong route for {query}")


class TestBuildConfig(unittest.TestCase):
    def test_default_config(self):
        cfg = BuildConfig()
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.preview_port, 8080)
        self.assertEqual(cfg.max_concurrent_builds, 3)

    def test_env_parsing(self):
        env = {
            "BUILD_MODE_ENABLED": "false",
            "BUILD_PREVIEW_PORT": "9000",
            "BUILD_PREVIEW_DOMAIN": "https://preview.domain.com",
            "BUILD_MAX_CONCURRENT": "5",
        }
        cfg = BuildConfig.from_env(env)
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.preview_port, 9000)
        self.assertEqual(cfg.preview_domain, "https://preview.domain.com")
        self.assertEqual(cfg.max_concurrent_builds, 5)
        self.assertEqual(cfg.get_preview_url("test-1"), "https://preview.domain.com/preview/test-1/")


class TestProjectStore(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, "test_builds.db")
        self.projects_dir = os.path.join(self.tmp_dir, "projects")
        self.store = ProjectStore(self.db_path, self.projects_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_save_and_retrieve_project(self):
        session = ProjectSession(
            project_id="proj-123",
            owner_id=1001,
            channel_id=2001,
            title="Calculator App",
            description="A clean neon calculator",
            framework="html",
            preview_url="http://localhost:8080/preview/proj-123/",
            created_at=100.0,
            updated_at=100.0,
            files={
                "index.html": "<!DOCTYPE html><html><body><h1>Calc</h1></body></html>",
                "styles.css": "body { background: #000; }",
            },
        )
        self.store.save_project(session)

        loaded = self.store.get_project("proj-123")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.title, "Calculator App")
        self.assertEqual(loaded.owner_id, 1001)
        self.assertIn("index.html", loaded.files)
        self.assertIn("styles.css", loaded.files)
        self.assertEqual(loaded.files["styles.css"], "body { background: #000; }")

    def test_active_user_project_lookup(self):
        s1 = ProjectSession(
            project_id="p1",
            owner_id=555,
            channel_id=10,
            title="Old Project",
            description="",
            framework="html",
            preview_url="",
            created_at=10.0,
            updated_at=10.0,
            files={"index.html": "old"},
        )
        s2 = ProjectSession(
            project_id="p2",
            owner_id=555,
            channel_id=10,
            title="Recent Project",
            description="",
            framework="html",
            preview_url="",
            created_at=20.0,
            updated_at=20.0,
            files={"index.html": "recent"},
        )
        self.store.save_project(s1)
        self.store.save_project(s2)

        active = self.store.get_active_user_project(555, 10)
        self.assertIsNotNone(active)
        self.assertEqual(active.project_id, "p2")

    def test_path_traversal_protection(self):
        session = ProjectSession(
            project_id="safe-id",
            owner_id=999,
            channel_id=10,
            title="Hacker",
            description="",
            framework="html",
            preview_url="",
            created_at=10.0,
            updated_at=10.0,
            files={
                "../../etc/passwd": "malicious content",
                "normal.html": "safe content",
            },
        )
        self.store.save_project(session)
        target_dir = self.store._project_dir("safe-id")
        self.assertFalse(os.path.exists(os.path.join(self.tmp_dir, "etc/passwd")))
        self.assertTrue((target_dir / "normal.html").exists())

    def test_cleanup_expired(self):
        s1 = ProjectSession(
            project_id="exp-1",
            owner_id=555,
            channel_id=10,
            title="Expired Project",
            description="",
            framework="html",
            preview_url="",
            created_at=10.0,
            updated_at=10.0,
            files={"index.html": "old"},
        )
        self.store.save_project(s1)
        cleaned = self.store.cleanup_expired(max_age_seconds=5)
        self.assertEqual(cleaned, 1)
        loaded = self.store.get_project("exp-1")
        self.assertTrue(loaded.is_expired)


class TestBuildGenerator(unittest.IsolatedAsyncioTestCase):
    async def test_generate_project_happy_path(self):
        mock_ai = MagicMock()
        mock_response = json.dumps({
            "title": "Minimal Todo App",
            "framework": "html",
            "description": "A sleek dark-mode todo list application.",
            "files": {
                "index.html": "<!DOCTYPE html><html><body><h1>Todo</h1></body></html>",
                "app.js": "console.log('todo ready');",
            },
        })
        mock_ai.chat = AsyncMock(return_value=mock_response)
        cfg = BuildConfig()
        generator = BuildGenerator(mock_ai, cfg)

        project = await generator.generate_project("build me a todo app", "Alice")
        self.assertEqual(project.title, "Minimal Todo App")
        self.assertIn("index.html", project.files)
        self.assertIn("app.js", project.files)
        mock_ai.chat.assert_called_once()

    async def test_modify_project_merges_files(self):
        mock_ai = MagicMock()
        mock_response = json.dumps({
            "title": "Minimal Todo App Dark",
            "description": "Changed background to dark mode.",
            "files": {
                "styles.css": "body { background: #111; color: #fff; }",
            },
        })
        mock_ai.chat = AsyncMock(return_value=mock_response)
        cfg = BuildConfig()
        generator = BuildGenerator(mock_ai, cfg)

        current_files = {
            "index.html": "<!DOCTYPE html><html><body><h1>Todo</h1></body></html>",
        }
        modified = await generator.modify_project(
            "Minimal Todo App",
            "A todo app",
            current_files,
            "make the background black",
        )
        self.assertIn("index.html", modified.files)
        self.assertIn("styles.css", modified.files)
        self.assertEqual(modified.files["styles.css"], "body { background: #111; color: #fff; }")


class TestPreviewServer(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, "test_server.db")
        self.projects_dir = os.path.join(self.tmp_dir, "projects")
        self.store = ProjectStore(self.db_path, self.projects_dir)
        self.config = BuildConfig(preview_host="127.0.0.1", preview_port=18999)
        self.server = PreviewServer(self.config, self.store)
        await self.server.start()

    async def asyncTearDown(self):
        await self.server.stop()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    async def test_health_endpoint(self):
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{self.config.preview_port}/healthz") as resp:
                self.assertEqual(resp.status, 200)
                data = await resp.json()
                self.assertEqual(data["status"], "ok")

    async def test_serve_project_html_and_css(self):
        # Save a test project
        project = ProjectSession(
            project_id="test-srv",
            owner_id=1234,
            channel_id=5678,
            title="Preview Test",
            description="Testing preview serving",
            framework="html",
            preview_url="",
            created_at=time.time(),
            updated_at=time.time(),
            files={
                "index.html": "<!DOCTYPE html><html><body><h1 id='test'>Hello Preview</h1></body></html>",
                "style.css": "body { margin: 0; }",
            },
        )
        self.store.save_project(project)

        async with aiohttp.ClientSession() as session:
            # Test index.html
            async with session.get(f"http://127.0.0.1:{self.config.preview_port}/preview/test-srv/") as resp:
                self.assertEqual(resp.status, 200)
                text = await resp.text()
                self.assertIn("Hello Preview", text)
                self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")

            # Test style.css
            async with session.get(f"http://127.0.0.1:{self.config.preview_port}/preview/test-srv/style.css") as resp:
                self.assertEqual(resp.status, 200)
                text = await resp.text()
                self.assertEqual(text, "body { margin: 0; }")

    async def test_project_not_found_returns_404(self):
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{self.config.preview_port}/preview/unknown-id/") as resp:
                self.assertEqual(resp.status, 404)


class TestBuildOrchestrator(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.config = BuildConfig(
            db_path=os.path.join(self.tmp_dir, "orch.db"),
            projects_dir=os.path.join(self.tmp_dir, "projects"),
            preview_port=18998,
            preview_host="127.0.0.1",
        )
        self.mock_ai = MagicMock()
        self.mock_ai.available = True
        self.mock_bot = MagicMock()

    async def asyncTearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    async def test_handle_new_build_pipeline(self):
        orchestrator = BuildOrchestrator(self.mock_bot, self.mock_ai, self.config)
        await orchestrator.start()
        try:
            # Mock generator response
            mock_project = GeneratedProject(
                title="Calculator Pro",
                description="A modern calculator app",
                framework="html",
                files={"index.html": "<!DOCTYPE html><html><body>Calculator</body></html>"},
            )
            orchestrator.generator.generate_project = AsyncMock(return_value=mock_project)

            # Mock discord message
            mock_message = MagicMock()
            mock_message.author = MagicMock(id=101, display_name="TestUser")
            mock_message.channel = MagicMock(id=202)
            mock_status = MagicMock()
            mock_status.edit = AsyncMock()
            mock_message.reply = AsyncMock(return_value=mock_status)

            success = await orchestrator.handle_new_build(
                mock_message, "build me a modern calculator website"
            )
            self.assertTrue(success)
            self.assertTrue(mock_status.edit.called)

            # Verify saved in project store
            active = orchestrator.store.get_active_user_project(101, 202)
            self.assertIsNotNone(active)
            self.assertEqual(active.title, "Calculator Pro")
            self.assertIn("index.html", active.files)
        finally:
            await orchestrator.stop()


if __name__ == "__main__":
    unittest.main()
