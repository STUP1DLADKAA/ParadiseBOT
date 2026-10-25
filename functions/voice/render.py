"""Deterministic, programmatic Pillow renderer for Voice Leaderboard cards.

No AI generation: pure 2D graphics with Material Expressive styling,
high-DPI typography, circular avatar clipping, and font fallback.
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Sequence
from urllib.parse import urlparse

import aiohttp
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from functions.dictionary.fonts import FontLibrary, PILLOW_AVAILABLE
from .models import LeaderboardMember, VoicePeriod

log = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "avatar_cache"
_AVATAR_CACHE_TTL = 3600.0
_AVATAR_CACHE_MAX_ITEMS = 128
_DISK_CACHE_MAX_FILES = 512

# In-memory avatar cache: URL -> (timestamp, Image). Keep this LRU-bounded;
# a busy server can otherwise retain one Pillow image per avatar it has seen.
_AVATAR_CACHE: OrderedDict[str, tuple[float, Image.Image]] = OrderedDict()

# Theme Palette (Dark Paradise & Material Expressive)
BG_COLOR = (14, 12, 22)              # #0E0C16
SURFACE_COLOR = (24, 20, 38)         # #181426
SURFACE_LIGHT = (36, 31, 56)         # #241F38
CARD_BORDER = (55, 48, 86)           # #373056
ACCENT_PURPLE = (139, 92, 246)       # #8B5CF6 (Vibrant Violet)
ACCENT_TEAL = (46, 139, 116)         # #2E8B74 (Paradise Accent)
ACCENT_CYAN = (56, 189, 248)         # #38BDF8
TEXT_PRIMARY = (255, 255, 255)       # #FFFFFF
TEXT_SECONDARY = (161, 161, 170)     # #A1A1AA
TEXT_MUTED = (113, 113, 122)         # #71717A

# Podium Colors
GOLD = (245, 158, 11)                # #F59E0B
SILVER = (148, 163, 184)             # #94A3B8
BRONZE = (217, 119, 6)               # #D97706


def _create_circle_mask(size: int) -> Image.Image:
    """Supersampled circular alpha mask for smooth antialiased clipping."""
    factor = 4
    big_size = size * factor
    mask = Image.new("L", (big_size, big_size), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, big_size, big_size), fill=255)
    return mask.resize((size, size), Image.Resampling.LANCZOS)


def _render_fallback_avatar(name: str, size: int, color_seed: int = 0) -> Image.Image:
    """Generate a clean colored avatar with user's initial when avatar is missing."""
    palettes = [
        (124, 58, 237), (79, 70, 229), (37, 99, 235), (13, 148, 136),
        (217, 119, 6), (225, 29, 72), (147, 51, 234), (46, 139, 116),
    ]
    bg = palettes[abs(color_seed) % len(palettes)]
    img = Image.new("RGBA", (size, size), (*bg, 255))
    draw = ImageDraw.Draw(img)

    initial = (name[0] if name else "?").upper()
    font_lib = FontLibrary()
    font_set = font_lib.get("display", int(size * 0.45))
    font = font_set.faces[0].font if font_set.faces else ImageFont.load_default()

    bbox = draw.textbbox((0, 0), initial, font=font)
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    x = (size - w) // 2 - bbox[0]
    y = (size - h) // 2 - bbox[1]
    draw.text((x, y), initial, fill=(255, 255, 255, 255), font=font)

    # Circular mask
    mask = _create_circle_mask(size)
    output = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    output.paste(img, (0, 0), mask=mask)
    return output


def _remember_avatar(url: str, timestamp: float, image: Image.Image) -> None:
    """Insert an avatar into the bounded in-memory LRU cache."""
    _AVATAR_CACHE.pop(url, None)
    _AVATAR_CACHE[url] = (timestamp, image)
    while len(_AVATAR_CACHE) > _AVATAR_CACHE_MAX_ITEMS:
        _AVATAR_CACHE.popitem(last=False)


def _prune_disk_cache() -> None:
    """Keep generated avatar files bounded without touching persistent records."""
    try:
        files = list(CACHE_DIR.glob("*.png"))
        if len(files) <= _DISK_CACHE_MAX_FILES:
            return
        files.sort(key=lambda path: path.stat().st_mtime)
        for path in files[:-_DISK_CACHE_MAX_FILES]:
            try:
                path.unlink()
            except OSError:
                continue
    except OSError:
        log.debug("Could not prune avatar cache", exc_info=True)


async def fetch_avatar(
    url: str | None,
    name: str,
    size: int = 128,
    *,
    session: aiohttp.ClientSession | None = None,
) -> Image.Image:
    """Fetch avatar image asynchronously with bounded memory and disk caches.

    ``session`` is optional for compatibility with direct callers. The
    leaderboard renderer supplies one shared session for a page so ten
    avatars do not create ten connector pools.
    """
    if not url:
        return _render_fallback_avatar(name, size, hash(name))

    now = time.monotonic()
    cached = _AVATAR_CACHE.get(url)
    if cached is not None:
        cache_time, img = cached
        if now - cache_time < _AVATAR_CACHE_TTL:
            _AVATAR_CACHE.move_to_end(url)
            return img.resize((size, size), Image.Resampling.LANCZOS)
        _AVATAR_CACHE.pop(url, None)

    # Use a stable filename across restarts. The old process-salted filenames
    # remain harmless cache files and are intentionally not treated as data.
    url_hash = sha256(url.encode("utf-8")).hexdigest()[:32]
    disk_path = CACHE_DIR / f"{url_hash}_{size}.png"
    # Also accept the old process-salted filename for the lifetime of a
    # process, so a hot reload does not discard a cache that was just written.
    legacy_disk_path = CACHE_DIR / f"{abs(hash(url))}_{size}.png"
    for cached_path in (disk_path, legacy_disk_path):
        if not cached_path.exists():
            continue
        try:
            with Image.open(cached_path) as cached_img:
                avatar = cached_img.convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
                _remember_avatar(url, now, avatar)
                return avatar
        except Exception:
            continue

    # Fetch over the network. A supplied page session is reused; direct calls
    # still own and close their temporary session.
    owned_session = session is None
    request_session = session
    try:
        if request_session is None:
            request_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=4))
        assert request_session is not None
        async with request_session.get(url) as resp:
            if resp.status == 200:
                data = await resp.read()
                raw_img = Image.open(io.BytesIO(data)).convert("RGBA")
                avatar = raw_img.resize((size, size), Image.Resampling.LANCZOS)

                # Apply circular crop
                mask = _create_circle_mask(size)
                circular = Image.new("RGBA", (size, size), (0, 0, 0, 0))
                circular.paste(avatar, (0, 0), mask=mask)

                _remember_avatar(url, now, circular)
                try:
                    CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    circular.save(disk_path, "PNG")
                    _prune_disk_cache()
                except OSError:
                    pass
                return circular
    except Exception as err:
        log.debug("Avatar fetch failed for %s: %s", url, err)
    finally:
        if owned_session and request_session is not None:
            await request_session.close()

    return _render_fallback_avatar(name, size, hash(name))


class LeaderboardImageRenderer:
    """Renders high-quality leaderboard statistics cards using Pillow."""

    def __init__(self) -> None:
        self.font_lib = FontLibrary()

    async def render_leaderboard(
        self,
        guild_name: str,
        period: VoicePeriod,
        members: Sequence[LeaderboardMember],
        date_str: str | None = None,
        page: int = 1,
        page_size: int = 10,
        *,
        title_override: str | None = None,
    ) -> bytes:
        """Render leaderboard image in an asynchronous executor."""
        total_seconds_all = sum(m.total_seconds for m in members)
        start_idx = (page - 1) * page_size
        page_members = list(members[start_idx : start_idx + page_size])

        if any(member.avatar_url for member in page_members):
            # One connector pool per rendered page instead of one ClientSession
            # per avatar. URL-less members still use local fallback avatars.
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=4)) as session:
                avatars = await asyncio.gather(
                    *(
                        fetch_avatar(m.avatar_url, m.display_name, size=160, session=session)
                        for m in page_members
                    )
                )
        else:
            avatars = await asyncio.gather(
                *(fetch_avatar(m.avatar_url, m.display_name, size=160) for m in page_members)
            )

        # Offload Pillow drawing to worker thread
        return await asyncio.to_thread(
            self._draw_canvas,
            guild_name,
            period,
            page_members,
            avatars,
            date_str,
            page,
            len(members),
            total_seconds_all,
            title_override,
        )

    def _draw_canvas(
        self,
        guild_name: str,
        period: VoicePeriod,
        members: list[LeaderboardMember],
        avatars: list[Image.Image],
        date_str: str | None,
        page: int,
        total_count: int,
        total_seconds_all: int | None = None,
        title_override: str | None = None,
    ) -> bytes:
        WIDTH = 1000
        # Calculate dynamic height
        num_podium = min(len(members), 3) if page == 1 else 0
        list_items = members[num_podium:] if page == 1 else members

        header_height = 200
        podium_height = 310 if num_podium > 0 else 0
        list_height = len(list_items) * 78 + (30 if list_items else 0)
        footer_height = 70

        HEIGHT = header_height + podium_height + list_height + footer_height
        HEIGHT = max(HEIGHT, 500)

        # Create canvas
        canvas = Image.new("RGBA", (WIDTH, HEIGHT), BG_COLOR)
        draw = ImageDraw.Draw(canvas)

        # Draw subtle background glow / accents
        self._draw_ambient_glow(canvas, WIDTH, HEIGHT)

        # Load fonts
        title_font = self._get_font("display", 34)
        subtitle_font = self._get_font("semibold", 17)
        body_font = self._get_font("medium", 16)
        bold_font = self._get_font("semibold", 16)
        pill_font = self._get_font("semibold", 13)
        small_font = self._get_font("regular", 13)
        mini_font = self._get_font("semibold", 11)

        # --------------------------------------------------------------
        # 1. HEADER
        # --------------------------------------------------------------
        header_y = 35

        # Pill badge: PARADISE VOICE ACTIVITY
        pill_text = "PARADISE VOICE ACTIVITY"
        draw.rounded_rectangle(
            (40, header_y, 40 + 210, header_y + 26),
            radius=13,
            fill=SURFACE_LIGHT,
            outline=CARD_BORDER,
            width=1,
        )
        draw.text((54, header_y + 5), pill_text, fill=ACCENT_CYAN, font=pill_font)

        # Main Title (Period & Date)
        if title_override:
            main_title = title_override
        elif date_str:
            main_title = f"{period.label.upper()} • {date_str.upper()}"
        else:
            main_title = f"{period.label.upper()} LEADERBOARD"
        draw.text((40, header_y + 36), main_title, fill=TEXT_PRIMARY, font=title_font)

        # Subtitle: Server name & total members
        sub_text = f"Server: {guild_name}  •  {total_count} active voice members tracked"
        draw.text((40, header_y + 82), sub_text, fill=TEXT_SECONDARY, font=subtitle_font)

        # Top total voice time badge
        total_time_sec = (
            total_seconds_all
            if total_seconds_all is not None
            else sum(m.total_seconds for m in members)
        )
        hours = total_time_sec // 3600
        mins = (total_time_sec % 3600) // 60
        total_time_str = f"{hours}h {mins}m tracked" if hours > 0 else f"{mins}m tracked"

        badge_w = 180
        badge_h = 56
        badge_x = WIDTH - 40 - badge_w
        badge_y = header_y + 30
        draw.rounded_rectangle(
            (badge_x, badge_y, badge_x + badge_w, badge_y + badge_h),
            radius=12,
            fill=SURFACE_COLOR,
            outline=CARD_BORDER,
            width=1,
        )
        draw.text((badge_x + 16, badge_y + 10), "SERVER TOTAL", fill=TEXT_MUTED, font=mini_font)
        draw.text((badge_x + 16, badge_y + 26), total_time_str, fill=ACCENT_PURPLE, font=bold_font)

        current_y = header_y + 130

        # Separator line
        draw.line([(40, current_y), (WIDTH - 40, current_y)], fill=CARD_BORDER, width=1)
        current_y += 25

        # --------------------------------------------------------------
        # 2. PODIUM SECTION (Page 1 Top 3)
        # --------------------------------------------------------------
        if num_podium > 0:
            podium_members = members[:num_podium]
            podium_avatars = avatars[:num_podium]

            card_w = 280
            gap = 25
            total_w = len(podium_members) * card_w + (len(podium_members) - 1) * gap
            start_x = (WIDTH - total_w) // 2

            if len(podium_members) == 3:
                display_order = [1, 0, 2]  # #2, #1, #3
            elif len(podium_members) == 2:
                display_order = [0, 1]
            else:
                display_order = [0]

            for slot_i, member_idx in enumerate(display_order):
                m = podium_members[member_idx]
                av = podium_avatars[member_idx]
                cx = start_x + slot_i * (card_w + gap)
                cy = current_y

                rank = m.rank
                if rank == 1:
                    border_color = GOLD
                    medal = "#1"
                    card_h = 270
                    cy_offset = 0
                elif rank == 2:
                    border_color = SILVER
                    medal = "#2"
                    card_h = 255
                    cy_offset = 15
                else:
                    border_color = BRONZE
                    medal = "#3"
                    card_h = 255
                    cy_offset = 15

                self._draw_podium_card(
                    canvas,
                    draw,
                    cx,
                    cy + cy_offset,
                    card_w,
                    card_h,
                    m,
                    av,
                    border_color,
                    medal,
                )

            current_y += podium_height + 15

        # --------------------------------------------------------------
        # 3. RANKING LIST SECTION (Ranks #4+)
        # --------------------------------------------------------------
        if list_items:
            list_avatars = avatars[num_podium:] if page == 1 else avatars
            for i, (m, av) in enumerate(zip(list_items, list_avatars)):
                row_y = current_y + i * 76
                self._draw_list_row(canvas, draw, 40, row_y, WIDTH - 80, 64, m, av)

            current_y += len(list_items) * 76 + 15
        elif num_podium == 0:
            empty_msg = "No voice activity recorded for this period."
            eb = draw.textbbox((0, 0), empty_msg, font=subtitle_font)
            ew = eb[2] - eb[0]
            draw.text(
                ((WIDTH - ew) // 2, current_y + 95),
                empty_msg,
                fill=TEXT_SECONDARY,
                font=subtitle_font,
            )

        # --------------------------------------------------------------
        # 4. FOOTER
        # --------------------------------------------------------------
        footer_y = HEIGHT - 45
        draw.line([(40, footer_y - 12), (WIDTH - 40, footer_y - 12)], fill=CARD_BORDER, width=1)
        draw.text(
            (40, footer_y),
            "ParadiseBOT • Voice Statistics",
            fill=TEXT_MUTED,
            font=small_font,
        )

        page_str = f"Page {page}"
        draw.text(
            (WIDTH - 40 - 55, footer_y),
            page_str,
            fill=TEXT_MUTED,
            font=small_font,
        )

        # Output PNG buffer
        buffer = io.BytesIO()
        canvas.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()

    def _draw_ambient_glow(self, canvas: Image.Image, w: int, h: int) -> None:
        """Draw subtle top purple gradient glow."""
        glow = Image.new("RGBA", (w, 200), (0, 0, 0, 0))
        glow_draw = ImageDraw.Draw(glow)
        for y in range(200):
            alpha = int(25 * (1.0 - y / 200.0))
            glow_draw.line([(0, y), (w, y)], fill=(124, 58, 237, alpha))
        canvas.alpha_composite(glow, (0, 0))

    def _draw_podium_card(
        self,
        canvas: Image.Image,
        draw: ImageDraw.ImageDraw,
        x: int,
        y: int,
        w: int,
        h: int,
        member: LeaderboardMember,
        avatar: Image.Image,
        accent: tuple[int, int, int],
        medal: str,
    ) -> None:
        # Card Background
        draw.rounded_rectangle(
            (x, y, x + w, y + h),
            radius=16,
            fill=SURFACE_COLOR,
            outline=accent,
            width=2 if member.rank == 1 else 1,
        )

        # Medal Badge
        badge_w = 64
        badge_h = 24
        badge_x = x + (w - badge_w) // 2
        badge_y = y + 14
        draw.rounded_rectangle(
            (badge_x, badge_y, badge_x + badge_w, badge_y + badge_h),
            radius=12,
            fill=(*accent, 40),
            outline=accent,
            width=1,
        )
        badge_font = self._get_font("semibold", 13)
        bb = draw.textbbox((0, 0), medal, font=badge_font)
        bw = bb[2] - bb[0]
        draw.text((badge_x + (badge_w - bw) // 2, badge_y + 3), medal, fill=accent, font=badge_font)

        # Avatar with ring
        av_size = 72 if member.rank == 1 else 64
        av_x = x + (w - av_size) // 2
        av_y = y + 46
        # Avatar ring
        draw.ellipse(
            (av_x - 3, av_y - 3, av_x + av_size + 3, av_y + av_size + 3),
            outline=accent,
            width=2,
        )
        resized_av = avatar.resize((av_size, av_size), Image.Resampling.LANCZOS)
        canvas.paste(resized_av, (av_x, av_y), mask=resized_av)

        # Display Name (Truncated if long)
        name_font = self._get_font("semibold", 16)
        name = self._truncate_text(draw, member.display_name, w - 24, name_font)
        name_bbox = draw.textbbox((0, 0), name, font=name_font)
        nw = name_bbox[2] - name_bbox[0]
        draw.text((x + (w - nw) // 2, av_y + av_size + 12), name, fill=TEXT_PRIMARY, font=name_font)

        # Time Pill
        time_y = av_y + av_size + 38
        draw.rounded_rectangle(
            (x + 20, time_y, x + w - 20, time_y + 30),
            radius=8,
            fill=SURFACE_LIGHT,
            outline=CARD_BORDER,
            width=1,
        )
        time_font = self._get_font("semibold", 15)
        time_str = member.formatted_time
        tb = draw.textbbox((0, 0), time_str, font=time_font)
        tw = tb[2] - tb[0]
        draw.text((x + (w - tw) // 2, time_y + 5), time_str, fill=TEXT_PRIMARY, font=time_font)

        # Percentage
        pct_font = self._get_font("regular", 12)
        pct_str = f"{member.percentage}% of server VC"
        pb = draw.textbbox((0, 0), pct_str, font=pct_font)
        pw = pb[2] - pb[0]
        draw.text((x + (w - pw) // 2, time_y + 36), pct_str, fill=TEXT_MUTED, font=pct_font)

    def _draw_list_row(
        self,
        canvas: Image.Image,
        draw: ImageDraw.ImageDraw,
        x: int,
        y: int,
        w: int,
        h: int,
        member: LeaderboardMember,
        avatar: Image.Image,
    ) -> None:
        # Row card background
        draw.rounded_rectangle(
            (x, y, x + w, y + h),
            radius=12,
            fill=SURFACE_COLOR,
            outline=CARD_BORDER,
            width=1,
        )

        # Rank badge
        rank_font = self._get_font("semibold", 15)
        rank_str = f"#{member.rank}"
        draw.text((x + 20, y + (h - 18) // 2), rank_str, fill=TEXT_MUTED, font=rank_font)

        # Avatar
        av_size = 40
        av_x = x + 65
        av_y = y + (h - av_size) // 2
        resized_av = avatar.resize((av_size, av_size), Image.Resampling.LANCZOS)
        canvas.paste(resized_av, (av_x, av_y), mask=resized_av)

        # Name
        name_font = self._get_font("semibold", 15)
        name = self._truncate_text(draw, member.display_name, 350, name_font)
        draw.text((av_x + av_size + 16, y + (h - 20) // 2), name, fill=TEXT_PRIMARY, font=name_font)

        # Progress bar (percentage share)
        bar_w = 160
        bar_h = 6
        bar_x = x + w - 340
        bar_y = y + (h - bar_h) // 2
        draw.rounded_rectangle(
            (bar_x, bar_y, bar_x + bar_w, bar_y + bar_h),
            radius=3,
            fill=SURFACE_LIGHT,
        )
        fill_w = max(4, int(bar_w * (member.percentage / 100.0)))
        draw.rounded_rectangle(
            (bar_x, bar_y, bar_x + fill_w, bar_y + bar_h),
            radius=3,
            fill=ACCENT_PURPLE,
        )

        # Time Pill
        time_font = self._get_font("semibold", 15)
        time_str = member.formatted_time
        tb = draw.textbbox((0, 0), time_str, font=time_font)
        tw = tb[2] - tb[0]
        draw.text((x + w - tw - 24, y + (h - 20) // 2), time_str, fill=TEXT_PRIMARY, font=time_font)

    def _truncate_text(
        self, draw: ImageDraw.ImageDraw, text: str, max_w: int, font: ImageFont.ImageFont
    ) -> str:
        """Truncate text with ellipsis if exceeding max_w."""
        bbox = draw.textbbox((0, 0), text, font=font)
        if bbox[2] - bbox[0] <= max_w:
            return text
        for i in range(len(text) - 1, 0, -1):
            cand = text[:i] + "…"
            bbox = draw.textbbox((0, 0), cand, font=font)
            if bbox[2] - bbox[0] <= max_w:
                return cand
        return text[:1] + "…"

    def _get_font(self, role: str, size: int) -> ImageFont.ImageFont:
        """Resolve font with fallback."""
        if not PILLOW_AVAILABLE:
            return ImageFont.load_default()
        try:
            font_set = self.font_lib.get(role, size)
            if font_set.faces:
                return font_set.faces[0].font
        except Exception:
            pass
        return ImageFont.load_default()
