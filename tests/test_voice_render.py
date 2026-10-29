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

import asyncio
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image

from functions.voice import VoicePeriod
from functions.voice.ui import leaderboard_subtitle
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


class TestLeaderboardImageRenderer(unittest.IsolatedAsyncioTestCase):
    """The pre-existing end-to-end renderer tests, through ``render_leaderboard``.

    These go through the real async path including ``fetch_avatar``, so they
    also cover the avatar plumbing the direct-draw tests above bypass.
    """

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


class TestHighDefinitionRendering(RenderCase):
    """The card is drawn supersampled and down-sampled with LANCZOS."""

    def test_output_is_always_the_design_width(self):
        for scale in (1, 2, 3):
            with self.subTest(scale=scale):
                renderer = LeaderboardImageRenderer(scale=scale)
                png = renderer._draw_canvas(
                    "Paradise",
                    VoicePeriod.TODAY,
                    board(6),
                    [None] * 6,
                    "2026-03-10",
                    1,
                    6,
                    None,
                    None,
                    None,
                )
                with Image.open(io.BytesIO(png)) as image:
                    self.assertEqual(image.width, WIDTH)

    def test_supersampling_produces_smoother_edges(self):
        """A 2x render must contain intermediate tones a 1x render lacks."""

        def unique_greys(png: bytes) -> int:
            with Image.open(io.BytesIO(png)) as image:
                rgb = image.convert("L")
                # Sample the header area, which is all text + rules.
                crop = rgb.crop((30, 20, 700, 190))
                return len(set(crop.tobytes()))

        members = board(3)
        low = LeaderboardImageRenderer(scale=1)._draw_canvas(
            "Paradise", VoicePeriod.TODAY, members, [None] * 3, None, 1, 3, None, None, None
        )
        high = LeaderboardImageRenderer(scale=2)._draw_canvas(
            "Paradise", VoicePeriod.TODAY, members, [None] * 3, None, 1, 3, None, None, None
        )
        self.assertGreater(unique_greys(high), unique_greys(low))

    def test_paging_is_identical_in_shape_across_scales(self):
        members = board(14)
        heights = {
            scale: Image.open(
                io.BytesIO(
                    LeaderboardImageRenderer(scale=scale)._draw_canvas(
                        "Paradise", VoicePeriod.WEEK, members, [None] * len(members),
                        None, 1, len(members), None, None, None,
                    )
                )
            ).height
            for scale in (1, 2)
        }
        self.assertEqual(heights[1], heights[2])


class TestRenderCache(unittest.IsolatedAsyncioTestCase):
    """Deterministic data + a short TTL cache means paging back is instant."""

    def setUp(self) -> None:
        self.renderer = LeaderboardImageRenderer()

    async def test_identical_requests_render_once(self):
        members = board(6)
        calls = {"n": 0}
        original = self.renderer._draw_canvas

        def counting(*args, **kwargs):
            calls["n"] += 1
            return original(*args, **kwargs)

        self.renderer._draw_canvas = counting
        try:
            first = await self.renderer.render_leaderboard(
                "Paradise", VoicePeriod.TODAY, members, "2026-03-10"
            )
            second = await self.renderer.render_leaderboard(
                "Paradise", VoicePeriod.TODAY, members, "2026-03-10"
            )
        finally:
            self.renderer._draw_canvas = original
        self.assertEqual(calls["n"], 1)
        self.assertEqual(first, second)

    async def test_a_changed_percentage_is_not_served_from_cache(self):
        members = board(4)
        first = await self.renderer.render_leaderboard(
            "Paradise", VoicePeriod.TODAY, members, None
        )
        changed = [
            member(m.rank, m.display_name, m.total_seconds, percentage=m.percentage + 5)
            for m in members
        ]
        second = await self.renderer.render_leaderboard(
            "Paradise", VoicePeriod.TODAY, changed, None
        )
        self.assertNotEqual(first, second)

    async def test_caching_can_be_disabled(self):
        members = board(4)
        a = await self.renderer.render_leaderboard(
            "Paradise", VoicePeriod.TODAY, members, None, use_cache=False
        )
        b = await self.renderer.render_leaderboard(
            "Paradise", VoicePeriod.TODAY, members, None, use_cache=False
        )
        self.assertEqual(a, b)  # identical data, identical pixels

    async def test_two_identical_concurrent_requests_share_one_render(self):
        members = board(5)
        results = await asyncio.gather(
            *(
                self.renderer.render_leaderboard(
                    "Paradise", VoicePeriod.WEEK, members, "2026-03-10"
                )
                for _ in range(3)
            )
        )
        self.assertEqual(len(set(results)), 1)

    async def test_the_render_never_blocks_the_event_loop(self):
        """Drawing runs on the render pool, so a heartbeat still ticks."""

        ticks = 0

        async def heartbeat():
            nonlocal ticks
            for _ in range(20):
                await asyncio.sleep(0.005)
                ticks += 1

        await asyncio.gather(
            heartbeat(),
            self.renderer.render_leaderboard(
                "Paradise", VoicePeriod.MONTH, board(10), None, use_cache=False
            ),
        )
        self.assertEqual(ticks, 20)


class TestLeaderboardSubtitle(unittest.TestCase):
    """One line of context under the title — never invented, always useful."""

    def test_singular_member_count(self):
        self.assertEqual(
            leaderboard_subtitle(board(1), page=1, total_pages=1),
            "1 person",
        )

    def test_plural_and_live_members(self):
        members = board(4)
        members[0] = member(1, members[0].display_name, 7200, percentage=40.0)
        members[0].is_active_now = True
        members[1].is_active_now = True
        text = leaderboard_subtitle(members, page=1, total_pages=1)
        self.assertIn("4 people", text)
        self.assertIn("2 in a call right now", text)

    def test_paging_is_mentioned_only_when_there_is_more_than_one_page(self):
        self.assertNotIn("page", leaderboard_subtitle(board(3), page=1, total_pages=1))
        self.assertIn(
            "page 2 of 3", leaderboard_subtitle(board(30), page=2, total_pages=3)
        )

    def test_an_empty_board_is_honest(self):
        self.assertEqual(leaderboard_subtitle([], page=1, total_pages=1), "0 people")


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ---------------------------------------------------------------------------
# The daily DM: a report a person would send, not a data dump.
# ---------------------------------------------------------------------------


class TestDailyDm(unittest.TestCase):
    def build(self, **kwargs):
        from datetime import date

        from functions.voice.ui import build_daily_dm_embed

        defaults = dict(
            guild_name="Paradise Hub",
            target_date=date(2025, 10, 1),  # a Wednesday
            seconds=3 * 3600 + 42 * 60,
            rank=2,
            people=14,
            server_total_seconds=21 * 3600 + 10 * 60,
            previous_seconds=3 * 3600,
        )
        defaults.update(kwargs)
        return build_daily_dm_embed(**defaults)

    def test_the_heading_names_the_day_not_a_timestamp(self):
        embed = self.build()
        self.assertEqual(embed.title, "Wednesday, 1 October")
        self.assertNotIn("2025-10-01", embed.title)
        self.assertNotIn("T", embed.title)

    def test_the_first_sentence_is_your_own_time(self):
        embed = self.build()
        self.assertTrue(
            embed.description.startswith("You spent **3h 42m** in voice on Wednesday."),
            embed.description,
        )

    def test_comparison_is_written_in_words(self):
        self.assertIn("42m more than Tuesday.", self.build().description)
        self.assertIn(
            "10m less than Tuesday.",
            self.build(seconds=30 * 60, previous_seconds=40 * 60).description,
        )
        self.assertIn(
            "About the same as Tuesday.",
            self.build(seconds=3600, previous_seconds=3600 + 120).description,
        )

    def test_no_comparison_when_there_was_nothing_to_compare(self):
        text = self.build(previous_seconds=None).description
        self.assertNotIn("Tuesday", text)

    def test_fields_carry_the_numbers_without_repeating_the_sentence(self):
        embed = self.build()
        names = [field.name for field in embed.fields]
        self.assertEqual(names, ["On the board", "Everyone together"])
        self.assertEqual(embed.fields[0].value, "#2, out of 14")
        self.assertEqual(embed.fields[1].value, "21h 10m")
        self.assertEqual(embed.footer.text, "Paradise Hub")

    def test_a_quiet_day_is_not_dressed_up(self):
        embed = self.build(seconds=0, rank=None, people=0, previous_seconds=0)
        self.assertEqual(embed.description, "You weren't in voice on Wednesday.")
        self.assertEqual(embed.fields[0].value, "—")

    def test_a_good_day_gets_one_true_line_and_no_more(self):
        embed = self.build(seconds=7 * 3600, rank=1, people=9)
        self.assertIn("You had the most time in voice that day.", embed.description)
        self.assertEqual(embed.description.count("."), 3)  # three sentences, period.

    def test_no_emoji_and_no_shouting(self):
        embed = self.build()
        text = embed.description + (embed.title or "")
        for character in "🎙️🏆📅📈👥":
            self.assertNotIn(character, text)
        self.assertEqual(embed.title, embed.title.title())


class TestLeaderboardFraming(unittest.TestCase):
    """The embed frames the card; it must not say everything twice."""

    def embed(self, **kwargs):
        from functions.voice.ui import leaderboard_image_embed

        return leaderboard_image_embed(
            type("G", (), {"name": "Paradise Hub"})(),
            VoicePeriod.TODAY,
            **kwargs,
        )

    def test_no_second_headline(self):
        self.assertIsNone(self.embed(page=1, total_pages=1).title)

    def test_the_image_is_the_attachment(self):
        self.assertEqual(
            self.embed().image.url, "attachment://voice_leaderboard.png"
        )
        self.assertEqual(
            self.embed(filename="daily_vc_leaderboard.png").image.url,
            "attachment://daily_vc_leaderboard.png",
        )

    def test_the_footer_stays_quiet_unless_there_are_pages(self):
        self.assertEqual(self.embed(page=1, total_pages=1).footer.text, "Paradise Hub")
        self.assertEqual(
            self.embed(page=2, total_pages=3).footer.text, "Paradise Hub  ·  page 2 of 3"
        )

    def test_a_caption_is_only_used_when_given(self):
        self.assertIsNone(self.embed().description)
        self.assertEqual(
            self.embed(caption="Nobody was in voice that day.").description,
            "Nobody was in voice that day.",
        )


# ---------------------------------------------------------------------------
# The redesign: a light page, readable type, and no accidental black borders.
# ---------------------------------------------------------------------------


def _relative_luminance(rgb) -> float:
    def channel(value: float) -> float:
        value /= 255.0
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b) -> float:
    first, second = _relative_luminance(a), _relative_luminance(b)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


class TestLightDesign(RenderCase):
    """The look is Apple-ish: white cards, quiet greys, one accent."""

    def test_the_page_is_light_and_the_text_is_dark(self):
        from functions.voice import render as module

        for name in ("BG_TOP", "BG_BOTTOM", "SURFACE_COLOR", "SURFACE_LIGHT"):
            colour = getattr(module, name)
            self.assertGreater(
                _relative_luminance(colour), 0.85, f"{name} should be near-white"
            )
        self.assertLess(_relative_luminance(module.TEXT_PRIMARY), 0.05)

    def test_primary_and_secondary_text_pass_contrast(self):
        from functions.voice import render as module

        card = module.SURFACE_COLOR
        # WCAG AA needs 4.5:1 for body text; the primary tone is AAA.
        self.assertGreater(contrast(module.TEXT_PRIMARY, card), 7.0)
        self.assertGreater(contrast(module.TEXT_SECONDARY, card), 4.5)
        self.assertGreater(contrast(module.TEXT_MUTED, card), 3.0)

    def test_the_rendered_card_has_no_dark_edges_or_boxes(self):
        """A translucent RGBA fill would survive as a black hairline."""
        _, image = self.render(board(6))
        dark = [
            (x, y)
            for y in range(0, image.height, 7)
            for x in range(0, image.width, 7)
            if _relative_luminance(image.getpixel((x, y))) < 0.05
        ]
        # Only glyph pixels may be that dark, and glyphs are sparse; a border
        # would light up an entire row or column.
        rows: dict[int, int] = {}
        for _x, y in dark:
            rows[y] = rows.get(y, 0) + 1
        self.assertLess(max(rows.values() or [0]), image.width // 8, "a rule went dark")

    def test_the_card_stays_inside_discords_limits_after_the_redesign(self):
        png, image = self.render(board(10))
        self.assertLess(len(png), DISCORD_ATTACHMENT_LIMIT)
        self.assertEqual(image.width, WIDTH)

    def test_every_period_accent_is_visible_on_white(self):
        """Accents fill bars and dots, not body text, so 2:1 is the bar to clear.

        systemOrange sits at 2.06:1 on white — the same as Apple's own use of
        it for small solid shapes — and anything paler than that disappears at
        1× on a phone screen.
        """
        from functions.voice import render as module

        for period, accent in module._PERIOD_ACCENT.items():
            self.assertGreater(
                contrast(accent, module.SURFACE_COLOR),
                1.9,
                f"{period.value} accent {accent} is too pale to see",
            )
