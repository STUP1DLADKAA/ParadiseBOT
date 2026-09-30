"""Unit tests for the Pillow voice activity leaderboard image renderer."""

from __future__ import annotations

import io
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image

from functions.voice import (
    LeaderboardImageRenderer,
    LeaderboardMember,
    VoicePeriod,
)
from functions.voice.render import _render_fallback_avatar, fetch_avatar


class TestLeaderboardImageRenderer(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.renderer = LeaderboardImageRenderer()

    def test_fallback_avatar_generation(self):
        img = _render_fallback_avatar("Alex", 128, 42)
        self.assertIsInstance(img, Image.Image)
        self.assertEqual(img.size, (128, 128))
        self.assertEqual(img.mode, "RGBA")

    async def test_render_podium_top3(self):
        members = [
            LeaderboardMember(
                rank=1,
                user_id=1,
                display_name="Alex (Gold)",
                username="alex",
                avatar_url=None,
                total_seconds=16320,  # 4h 32m
                percentage=45.0,
            ),
            LeaderboardMember(
                rank=2,
                user_id=2,
                display_name="Sam (Silver)",
                username="sam",
                avatar_url=None,
                total_seconds=13680,  # 3h 48m
                percentage=35.0,
            ),
            LeaderboardMember(
                rank=3,
                user_id=3,
                display_name="John (Bronze)",
                username="john",
                avatar_url=None,
                total_seconds=7200,   # 2h
                percentage=20.0,
            ),
        ]

        png_bytes = await self.renderer.render_leaderboard(
            guild_name="Paradise Server",
            period=VoicePeriod.TODAY,
            members=members,
            date_str="30 SEPTEMBER 2026",
            page=1,
            page_size=10,
        )

        self.assertIsInstance(png_bytes, bytes)
        # Check PNG magic bytes
        self.assertTrue(png_bytes.startswith(b"\x89PNG\r\n\x1a\n"))

        # Verify image opens with PIL
        with Image.open(io.BytesIO(png_bytes)) as img:
            self.assertEqual(img.size[0], 1000)
            self.assertGreater(img.size[1], 400)

    async def test_render_full_leaderboard_with_list_items(self):
        members = [
            LeaderboardMember(rank=1, user_id=1, display_name="User 1", username="u1", avatar_url=None, total_seconds=18000, percentage=30.0),
            LeaderboardMember(rank=2, user_id=2, display_name="User 2", username="u2", avatar_url=None, total_seconds=14000, percentage=25.0),
            LeaderboardMember(rank=3, user_id=3, display_name="User 3", username="u3", avatar_url=None, total_seconds=10000, percentage=18.0),
            LeaderboardMember(rank=4, user_id=4, display_name="User 4", username="u4", avatar_url=None, total_seconds=6000, percentage=12.0),
            LeaderboardMember(rank=5, user_id=5, display_name="User 5", username="u5", avatar_url=None, total_seconds=4000, percentage=8.0),
            LeaderboardMember(rank=6, user_id=6, display_name="User 6 With a Very Long Name That Exceeds Normal Limits", username="u6", avatar_url=None, total_seconds=2000, percentage=4.0),
        ]

        png_bytes = await self.renderer.render_leaderboard(
            guild_name="Paradise Server",
            period=VoicePeriod.TODAY,
            members=members,
            date_str="30 SEPTEMBER 2026",
            page=1,
            page_size=10,
        )

        self.assertIsInstance(png_bytes, bytes)
        self.assertTrue(png_bytes.startswith(b"\x89PNG\r\n\x1a\n"))

        with Image.open(io.BytesIO(png_bytes)) as img:
            self.assertEqual(img.size[0], 1000)
            self.assertGreater(img.size[1], 600)

    async def test_render_single_member_server(self):
        members = [
            LeaderboardMember(rank=1, user_id=1, display_name="Solo Member", username="solo", avatar_url=None, total_seconds=3600, percentage=100.0),
        ]

        png_bytes = await self.renderer.render_leaderboard(
            guild_name="Small Server",
            period=VoicePeriod.TODAY,
            members=members,
            page=1,
            page_size=10,
        )

        self.assertIsInstance(png_bytes, bytes)
        self.assertTrue(png_bytes.startswith(b"\x89PNG\r\n\x1a\n"))


if __name__ == "__main__":
    unittest.main()
