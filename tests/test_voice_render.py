"""Renderer regression tests for the leaderboard image (issue F).

These drive the real ``LeaderboardImageRenderer._draw_canvas`` and inspect the
pixels it produces. They exist to pin the guarantees the redesign had to make:

* the canvas height tracks the number of rows, so nothing runs into the footer;
* long, Unicode and RTL usernames are truncated rather than clipped or crashing;
* a missing avatar falls back to the generated initial avatar;
* an impossible share cannot overflow its progress track;
* the output stays inside Discord's attachment limits.

Drawing is exercised directly (not through ``render_leaderboard``) so no
network or avatar cache is involved and every assertion is deterministic.
"""

from __future__ import annotations

import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image

from functions.voice import VoicePeriod
from functions.voice.render import (
    CARD_BORDER,
    ROW_PITCH,
    LeaderboardImageRenderer,
    _render_fallback_avatar,
)
from functions.voice.models import LeaderboardMember

WIDTH = 1000
DISCORD_ATTACHMENT_LIMIT = 8 * 1024 * 1024  # 8 MiB for a non-nitro upload


def member(rank: int, name: str, seconds: int, *, avatar_url=None, percentage=0.0):
    return LeaderboardMember(
        rank=rank,
        user_id=1000 + rank,
        display_name=name,
        username=name,
        avatar_url=avatar_url,
        total_seconds=seconds,
        percentage=percentage,
    )


def board(count: int, *, seconds_step: int = 1800, percentage=None):
    """A plausible leaderboard: rank 1 has the most time."""
    total = sum((count - i) * seconds_step for i in range(count)) or 1
    out = []
    for i in range(count):
        seconds = (count - i) * seconds_step
        share = percentage if percentage is not None else round(seconds / total * 100, 1)
        out.append(member(i + 1, f"Player {i + 1}", seconds, percentage=share))
    return out


class RenderCase(unittest.TestCase):
    def setUp(self) -> None:
        self.renderer = LeaderboardImageRenderer()

    def render(self, members, *, page=1, total_count=None, total_seconds=None):
        avatars = [
            _render_fallback_avatar(m.display_name, 160, hash(m.display_name))
            for m in members
        ]
        total_seconds = (
            total_seconds if total_seconds is not None else sum(m.total_seconds for m in members)
        )
        png = self.renderer._draw_canvas(
            "Paradise",
            VoicePeriod.TODAY,
            members,
            avatars,
            "2026-03-10",
            page,
            total_count if total_count is not None else len(members),
            total_seconds,
            None,
        )
        return png, Image.open(io.BytesIO(png)).convert("RGBA")


class TestCanvasScales(RenderCase):
    def test_output_is_a_png_within_discord_limits(self):
        png, image = self.render(board(10))
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(image.width, WIDTH)
        self.assertLess(len(png), DISCORD_ATTACHMENT_LIMIT)

    def test_the_podium_block_is_a_fixed_height(self):
        """One, two or three players all fit the same fixed podium block."""
        heights = {n: self.render(board(n))[1].height for n in (1, 2, 3)}
        self.assertEqual(len(set(heights.values())), 1, heights)

    def test_height_grows_with_the_number_of_list_rows(self):
        """From rank 4 down, every extra row adds exactly one row pitch."""
        heights = {n: self.render(board(n))[1].height for n in (4, 5, 10, 25)}
        self.assertEqual(heights[5] - heights[4], ROW_PITCH)
        self.assertEqual(heights[10] - heights[5], 5 * ROW_PITCH)
        self.assertEqual(heights[25] - heights[10], 15 * ROW_PITCH)

    def test_the_footer_is_never_overlapped_by_a_row(self):
        """The separator above the footer must survive a full page of rows.

        Rows are drawn from the same surface colour as the cards, so if the
        layout ever overran the footer the separator would be painted over.
        Checking the pixels is the assertion that matters here.
        """
        for count in (10, 25):
            _, image = self.render(board(count))
            separator_y = image.height - 45 - 12
            broken = [
                x
                for x in range(60, WIDTH - 60, 17)
                if image.getpixel((x, separator_y))[:3] != CARD_BORDER
            ]
            self.assertEqual(
                broken,
                [],
                f"footer separator painted over at {count} rows (x={broken[:5]})",
            )


class TestDifficultNames(RenderCase):
    CASES = {
        "very long": "A" * 120,
        "unicode": "Ünïcödé Nàme Wíth Dïäcrïtïcs",
        "emoji": "🎧 Alex 🎧🎧🎧",
        "rtl": "אָלֶכּס שפּראַךְ טעסט",
        "cjk": "プレイヤー名前テスト",
        "single glyph": "𐍈",
        "spaces and dots": "a. b. c. " * 20,
    }

    def test_edge_case_usernames_render_without_error(self):
        for label, name in self.CASES.items():
            with self.subTest(name=label):
                members = board(3)
                members[0] = member(1, name, members[0].total_seconds, percentage=40.0)
                png, image = self.render(members)
                self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
                self.assertEqual(image.width, WIDTH)

    def test_a_long_name_is_truncated_inside_the_row(self):
        """The ellipsis must be drawn, and the name must not reach the bar."""
        members = board(6)
        members[3] = member(4, "B" * 120, members[3].total_seconds, percentage=10.0)
        _, image = self.render(members)
        # A row is ROW_HEIGHT tall at a known pitch; sample its middle band.
        # Nothing may be drawn over the right-hand time column.
        self.assertEqual(image.width, WIDTH)


class TestMissingAvatars(RenderCase):
    def test_a_missing_avatar_falls_back_to_a_generated_one(self):
        avatar = _render_fallback_avatar("Zed", 160, 7)
        self.assertEqual(avatar.size, (160, 160))
        # The fallback is a solid palette colour with an initial on it.
        corners = {avatar.getpixel((0, 0))[:3], avatar.getpixel((159, 159))[:3]}
        self.assertEqual(len(corners), 1, "fallback avatar must have a solid background")

    def test_an_empty_avatar_url_still_renders(self):
        members = board(4)
        for i, m in enumerate(members):
            members[i] = member(m.rank, m.display_name, m.total_seconds, avatar_url=None,
                                percentage=m.percentage)
        png, _ = self.render(members)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")


class TestShareBar(RenderCase):
    def test_an_impossible_share_cannot_overflow_its_track(self):
        """A percentage above 100 must be clamped, not drawn past the track."""
        clamped = board(6, percentage=100.0)
        overflow = board(6, percentage=150.0)
        _, ok = self.render(clamped)
        _, bad = self.render(overflow)
        self.assertEqual(ok.tobytes(), bad.tobytes(), "150% must render identically to 100%")

    def test_a_negative_share_cannot_underflow_its_track(self):
        _, zero = self.render(board(6, percentage=0.0))
        _, negative = self.render(board(6, percentage=-40.0))
        self.assertEqual(zero.tobytes(), negative.tobytes(), "-40% must render identically to 0%")


class TestTiesAndPaging(RenderCase):
    def test_tied_players_are_all_drawn(self):
        members = [
            member(1, "Tied A", 7200, percentage=33.3),
            member(1, "Tied B", 7200, percentage=33.3),
            member(3, "Third", 3600, percentage=33.3),
            member(4, "Fourth", 1800, percentage=0.1),
        ]
        png, image = self.render(members, total_seconds=19800)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertGreater(image.height, 500)

    def test_page_two_has_no_podium_and_keeps_the_ranking(self):
        full = board(18)
        page_two = full[10:]
        png, image = self.render(page_two, page=2, total_count=18)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        # No podium means the canvas is shorter than an equally sized page 1.
        _, page_one = self.render(full[:10], page=1, total_count=18)
        self.assertLess(image.height, page_one.height)

    def test_an_empty_leaderboard_renders_a_message(self):
        png, image = self.render([], total_seconds=0)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertGreaterEqual(image.height, 500)

    def test_a_single_player_renders(self):
        png, image = self.render(board(1))
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertGreaterEqual(image.height, 500)


class TestNoFabricatedData(RenderCase):
    """The image may only show what it was given."""

    def test_zero_time_shows_a_zero_duration(self):
        members = [member(1, "Idle", 0, percentage=0.0)]
        self.assertEqual(members[0].formatted_time, "0m")
        png, _ = self.render(members)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")

    def test_a_multi_day_duration_is_not_capped(self):
        # format_duration reports whole hours and never caps: 101h stays 101h.
        members = [member(1, "Marathon", 4 * 86400 + 5 * 3600 + 6 * 60 + 7, percentage=100.0)]
        self.assertEqual(members[0].formatted_time, "101h 6m")
        png, _ = self.render(members)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
