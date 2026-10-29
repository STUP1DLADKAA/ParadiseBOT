"""Programmatic, high-DPI renderer for the voice leaderboard card.

Everything here is deterministic 2D graphics — Pillow, gradients, LANCZOS
down-sampling. No generated imagery, no AI, no external template files: the
same data always produces the same pixels, which is what makes a leaderboard
trustworthy and a card cacheable.

Rendering strategy
------------------
* **Supersampling.** The card is drawn at ``scale``× (2 by default) and
  down-sampled with LANCZOS, so rings, rounded corners, gradients and text all
  come out smooth instead of stair-stepped.
* **Cached backdrops.** The gradient background only depends on the canvas
  size and the period accent colour, so it is built once and reused.
* **A dedicated render pool.** Drawing runs on the shared render threads
  (:mod:`functions.perf`), never on the event loop.
* **A short-lived result cache.** Paging back and forth, or two people asking
  for the same board at once, costs one render, not two.

The card scales its height with the number of rows, so a full page can never
collide with the footer, and every column is measured rather than assumed so
long, Unicode or RTL names cannot overrun a duration.

Design
------
A page, not a poster. White cards on a very light grey page, one accent colour
per period, and grey for everything except the numbers people came to read.
There are two reasons for the restraint: it reads instantly on a phone at 3 a.m.
in Discord's dark theme, and it leaves no room for decoration to compete with
the data. Specifically —

* **One type ladder.** A 44 px period title, a 15 px name, a 26 px time in the
  podium and a 32 px group total; nothing else is large.
* **One list, one sheet.** Ranks 4 and below live inside a single grouped card
  with hairline separators, like a settings list, instead of one boxed row per
  person. Fewer containers means less to parse.
* **Real shadows, drawn as pixels.** ``_lift`` pre-blends two faint offsets
  against the page colour, because ``ImageDraw`` replaces rather than blends
  RGBA fills (see the comment there — it is a two-line bug that turns a soft
  shadow into a black border).
* **No emoji in the card.** They render at different sizes on every platform and
  break the line rhythm; the medals are a word ("1st") and a colour.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Sequence
from urllib.parse import urlparse

import aiohttp
from PIL import Image, ImageDraw, ImageFont, ImageOps

from functions.cache import SingleFlight, TTLCache
from functions.dictionary.fonts import PILLOW_AVAILABLE, FontLibrary
from functions.perf import run_render, shared_session

from .models import LeaderboardMember, VoicePeriod

log = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "avatar_cache"
_AVATAR_CACHE_TTL = 3600.0
_AVATAR_CACHE_MAX_ITEMS = 256
_DISK_CACHE_MAX_FILES = 1024

# In-memory avatar cache: URL -> (timestamp, Image). LRU-bounded so a busy
# server cannot retain one Pillow image per avatar it has ever seen.
_AVATAR_CACHE: OrderedDict[str, tuple[float, Image.Image]] = OrderedDict()

# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

WIDTH = 1000
#: Page margin. Every column is measured from this, not assumed.
PAD = 44
ROW_HEIGHT = 64
ROW_PITCH = 78
HEADER_HEIGHT = 200
PODIUM_HEIGHT = 310
FOOTER_HEIGHT = 70
MIN_HEIGHT = 500

DEFAULT_SCALE = 2
_MAX_SCALE = 3

# ---------------------------------------------------------------------------
# Palette — light, quiet, and mostly grey.
# ---------------------------------------------------------------------------
# The card is a document, not a poster: white paper on a faint grey page, one
# accent colour per period, and grey for everything that is not the number the
# reader came for. Apple's system colours are used verbatim where they fit, and
# the text greys are their label / secondaryLabel / tertiaryLabel values.

BG_TOP = (255, 255, 255)             # #FFFFFF
BG_BOTTOM = (242, 242, 247)          # #F2F2F7 systemGroupedBackground
SURFACE_COLOR = (255, 255, 255)      # card
SURFACE_LIGHT = (247, 247, 249)      # chips / tracks
CARD_BORDER = (226, 226, 231)        # #E2E2E7 hairline
CARD_BORDER_SOFT = (238, 238, 242)   # inner separators
TRACK_COLOR = (237, 237, 241)        # progress-bar track
ACCENT_PURPLE = (94, 92, 230)        # #5E5CE6 systemIndigo
ACCENT_CYAN = (0, 122, 255)          # #007AFF systemBlue
ACCENT_BLUE = (0, 113, 227)          # #0071E3 (links, focus)
ACCENT_TEAL = (48, 176, 199)         # #30B0C7
ACCENT_PINK = (255, 55, 95)          # #FF375F systemPink
ACCENT_ORANGE = (255, 159, 10)       # #FF9F0A systemOrange
TEXT_PRIMARY = (29, 29, 31)          # #1D1D1F label
TEXT_SECONDARY = (110, 110, 115)     # #6E6E73 secondaryLabel
TEXT_MUTED = (142, 142, 147)         # #8E8E93 tertiaryLabel

GOLD = (255, 179, 0)                 # #FFB300
GOLD_DEEP = (255, 149, 0)            # #FF9500
SILVER = (174, 174, 178)             # #AEAEB2
SILVER_DEEP = (142, 142, 147)
BRONZE = (197, 139, 91)              # #C58B5B
BRONZE_DEEP = (163, 106, 61)

LIVE_GREEN = (52, 199, 89)           # #34C759

_PERIOD_ACCENT = {
    VoicePeriod.TODAY: ACCENT_CYAN,
    VoicePeriod.YESTERDAY: ACCENT_PURPLE,
    VoicePeriod.WEEK: ACCENT_CYAN,
    VoicePeriod.MONTH: ACCENT_TEAL,
    VoicePeriod.ALL_TIME: ACCENT_ORANGE,
}

_MEDAL_RAMPS = {
    1: (GOLD, GOLD_DEEP),
    2: (SILVER, SILVER_DEEP),
    3: (BRONZE, BRONZE_DEEP),
}


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    """Blend *a* toward *b* by ``amount`` (0..1)."""
    t = max(0.0, min(1.0, float(amount)))
    return (
        int(round(a[0] + (b[0] - a[0]) * t)),
        int(round(a[1] + (b[1] - a[1]) * t)),
        int(round(a[2] + (b[2] - a[2]) * t)),
    )


def _with_alpha(color: tuple[int, int, int], alpha: int) -> tuple[int, int, int, int]:
    return (color[0], color[1], color[2], max(0, min(255, int(alpha))))


# ---------------------------------------------------------------------------
# Cached graphic building blocks
# ---------------------------------------------------------------------------


@lru_cache(maxsize=16)
def _background(width: int, height: int, accent: tuple[int, int, int]) -> Image.Image:
    """White-to-grey page with one very faint accent wash at the top.

    Cached per canvas size, so the gradient is built once per shape of card
    rather than once per render.
    """
    width = max(1, int(width))
    height = max(1, int(height))
    ramp = Image.linear_gradient("L").resize((width, height), Image.Resampling.BILINEAR)
    canvas = ImageOps.colorize(ramp, black=BG_TOP, white=BG_BOTTOM).convert("RGBA")

    # A single soft wash behind the header only: enough to give the page a
    # temperature, far too little to compete with the type. A one-pixel
    # gradient stretched to the full width keeps it perfectly even.
    band = min(300, max(80, height // 3))
    fade = Image.linear_gradient("L").resize((1, band), Image.Resampling.BILINEAR)
    fade = fade.point(lambda v: int((1.0 - v / 255.0) ** 1.6 * 22))
    wash = Image.new("RGBA", (width, band), _with_alpha(accent, 255))
    wash.putalpha(fade.resize((width, band), Image.Resampling.BILINEAR))
    canvas.alpha_composite(wash, (0, 0))
    return canvas


@lru_cache(maxsize=32)
def _horizontal_ramp(width: int, height: int, left: tuple[int, int, int], right: tuple[int, int, int]) -> Image.Image:
    """A left→right colour ramp, used for bars and accents."""
    width = max(1, int(width))
    height = max(1, int(height))
    strip = Image.new("RGB", (256, 1))
    draw = ImageDraw.Draw(strip)
    for x in range(256):
        draw.point((x, 0), fill=_mix(left, right, x / 255.0))
    return strip.resize((width, height), Image.Resampling.BILINEAR)


@lru_cache(maxsize=64)
def _vertical_ramp(width: int, height: int, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    strip = Image.new("RGB", (1, 256))
    draw = ImageDraw.Draw(strip)
    for y in range(256):
        draw.point((0, y), fill=_mix(top, bottom, y / 255.0))
    return strip.resize((max(1, int(width)), max(1, int(height))), Image.Resampling.BILINEAR)


def _rounded_mask(size: tuple[int, int], radius: int, *, scale: int = 1) -> Image.Image:
    """Antialiased rounded-rectangle alpha mask (drawn at ``scale``× then shrunk)."""
    width, height = size
    big = Image.new(
        "L", (max(1, width * scale), max(1, height * scale)), 0
    )
    ImageDraw.Draw(big).rounded_rectangle(
        (0, 0, big.width - 1, big.height - 1),
        radius=max(0, int(radius * scale)),
        fill=255,
    )
    return big.resize((max(1, width), max(1, height)), Image.Resampling.LANCZOS)


@lru_cache(maxsize=64)
def _create_circle_mask(size: int) -> Image.Image:
    """Supersampled circular alpha mask for smooth antialiased clipping."""
    factor = 4
    big_size = size * factor
    mask = Image.new("L", (big_size, big_size), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, big_size, big_size), fill=255)
    return mask.resize((size, size), Image.Resampling.LANCZOS)


def _clamp_share(percentage: float) -> float:
    """A share of the server total, pinned to a drawable, truthful range."""
    try:
        value = float(percentage)
    except (TypeError, ValueError):
        return 0.0
    if value != value:  # NaN
        return 0.0
    return min(100.0, max(0.0, value))


_AVATAR_PALETTE = (
    (124, 58, 237),
    (79, 70, 229),
    (37, 99, 235),
    (13, 148, 136),
    (217, 119, 6),
    (225, 29, 72),
    (147, 51, 234),
    (46, 139, 116),
)


@lru_cache(maxsize=4)
def _radial_glow(size: int, color: tuple[int, int, int], strength: int) -> Image.Image:
    """A soft circular glow, built small and scaled up (cheap and smooth)."""
    size = max(8, int(size))
    ramp = Image.radial_gradient("L")  # 0 at the centre, 255 at the edge
    ramp = ImageOps.invert(ramp)
    # Square the falloff so the glow fades out gently instead of linearly.
    ramp = ramp.point(lambda v: int((v / 255.0) ** 2.2 * 255))
    alpha = ramp.point(lambda v: int(v * (strength / 255.0)))
    glow = Image.new("RGBA", ramp.size, _with_alpha(color, 255))
    glow.putalpha(alpha)
    return glow.resize((size, size), Image.Resampling.BICUBIC)


@lru_cache(maxsize=256)
def _fallback_avatar_cached(name: str, size: int, color_seed: int) -> Image.Image:
    return _render_fallback_avatar_uncached(name, size, color_seed)


def _render_fallback_avatar(name: str, size: int, color_seed: int = 0) -> Image.Image:
    """A clean initial-on-colour avatar for members without a picture.

    Cached: rebuilding a gradient + mask for every row on every page render
    was a measurable slice of the frame time, and the result is deterministic.
    The returned image must be treated as read-only (callers only resize it).
    """
    return _fallback_avatar_cached(name, int(size), int(color_seed))


def _render_fallback_avatar_uncached(name: str, size: int, color_seed: int = 0) -> Image.Image:
    size = max(8, int(size))
    top = _AVATAR_PALETTE[abs(color_seed) % len(_AVATAR_PALETTE)]
    bottom = _mix(top, (12, 10, 24), 0.45)

    body = _vertical_ramp(size, size, top, bottom).convert("RGBA")
    # A soft highlight in the upper-left keeps it from looking flat.
    highlight = _radial_glow(size, (255, 255, 255), 34)
    body.alpha_composite(highlight, (-int(size * 0.25), -int(size * 0.35)))

    draw = ImageDraw.Draw(body)
    initial = (name.strip()[:1] or "?").upper()
    font = _fallback_font(size)
    draw.text(
        (size / 2, size / 2 + size * 0.02),
        initial,
        fill=(255, 255, 255, 240),
        font=font,
        anchor="mm",
    )

    output = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    output.paste(body, (0, 0), mask=_create_circle_mask(size))
    return output


@lru_cache(maxsize=32)
def _fallback_font(size: int) -> ImageFont.ImageFont:
    try:
        font_set = FontLibrary().get("display", max(12, int(size * 0.46)))
        if font_set.faces:
            return font_set.faces[0].font
    except Exception:  # noqa: BLE001 - a bitmap font still renders an initial
        log.debug("Fallback avatar font unavailable", exc_info=True)
    return ImageFont.load_default()


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


def _is_safe_avatar_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


async def fetch_avatar(
    url: str | None,
    name: str,
    size: int = 128,
    *,
    session: aiohttp.ClientSession | None = None,
) -> Image.Image:
    """Fetch an avatar with bounded memory + disk caches and a pooled session.

    ``session`` is optional: callers that already have a page session can pass
    it, otherwise the process-wide pooled session is used so the TLS
    handshake is paid once for the whole process rather than once per render.
    """
    if not url or not _is_safe_avatar_url(url):
        return _render_fallback_avatar(name, size, hash(name))

    now = time.monotonic()
    cached = _AVATAR_CACHE.get(url)
    if cached is not None:
        cache_time, img = cached
        if now - cache_time < _AVATAR_CACHE_TTL:
            _AVATAR_CACHE.move_to_end(url)
            return img.resize((size, size), Image.Resampling.LANCZOS)
        _AVATAR_CACHE.pop(url, None)

    url_hash = sha256(url.encode("utf-8")).hexdigest()[:32]
    disk_path = CACHE_DIR / f"{url_hash}_{size}.png"
    if disk_path.exists():
        try:
            with Image.open(disk_path) as cached_img:
                avatar = cached_img.convert("RGBA").resize(
                    (size, size), Image.Resampling.LANCZOS
                )
            _remember_avatar(url, now, avatar)
            return avatar
        except Exception:  # noqa: BLE001 - a corrupt cache file is just a miss
            log.debug("Discarding unreadable avatar cache file %s", disk_path)

    owned = False
    request_session = session
    try:
        if request_session is None:
            request_session = shared_session("avatars", timeout=6.0)
            owned = True
        async with request_session.get(url) as resp:
            if resp.status == 200:
                data = await resp.read()
                raw = Image.open(io.BytesIO(data)).convert("RGBA")
                avatar = raw.resize((size, size), Image.Resampling.LANCZOS)
                circular = Image.new("RGBA", (size, size), (0, 0, 0, 0))
                circular.paste(avatar, (0, 0), mask=_create_circle_mask(size))

                _remember_avatar(url, now, circular)
                try:
                    CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    circular.save(disk_path, "PNG", optimize=False, compress_level=1)
                    _prune_disk_cache()
                except OSError:
                    pass
                return circular
    except Exception as err:  # noqa: BLE001 - avatar failure is cosmetic
        log.debug("Avatar fetch failed for %s: %s", url, err)
    finally:
        # Only close a session this function created itself.
        if owned and request_session is not None and not session:
            pass  # pooled sessions are long-lived; closed on shutdown
    return _render_fallback_avatar(name, size, hash(name))


# ---------------------------------------------------------------------------
# Painter — draws in 1× coordinates onto a supersampled canvas
# ---------------------------------------------------------------------------


class _Painter:
    """Small coordinate-scaling wrapper around ``ImageDraw``.

    Every method takes *design* coordinates (the 1000-px reference layout) and
    multiplies them by the supersampling factor, so layout code never has to
    think about the render scale.
    """

    __slots__ = ("image", "draw", "scale")

    def __init__(self, image: Image.Image, scale: int) -> None:
        self.image = image
        self.draw = ImageDraw.Draw(image)
        self.scale = max(1, int(scale))

    def px(self, value: float) -> int:
        return int(round(value * self.scale))

    def rect(self, box, *, radius: float = 0, fill=None, outline=None, width: float = 1):
        x0, y0, x1, y1 = (self.px(v) for v in box)
        self.draw.rounded_rectangle(
            (x0, y0, x1, y1),
            radius=self.px(radius),
            fill=fill,
            outline=outline,
            width=max(1, self.px(width)),
        )

    def ellipse(self, box, *, fill=None, outline=None, width: float = 1):
        x0, y0, x1, y1 = (self.px(v) for v in box)
        self.draw.ellipse(
            (x0, y0, x1, y1), fill=fill, outline=outline, width=max(1, self.px(width))
        )

    def line(self, points, *, fill, width: float = 1):
        scaled = [(self.px(x), self.px(y)) for x, y in points]
        self.draw.line(scaled, fill=fill, width=max(1, self.px(width)))

    def polygon(self, points, *, fill):
        self.draw.polygon([(self.px(x), self.px(y)) for x, y in points], fill=fill)

    def text(self, xy, text: str, *, font, fill, anchor: str = "la", spacing: int = 0):
        self.draw.text(
            (self.px(xy[0]), self.px(xy[1])),
            text,
            font=font,
            fill=fill,
            anchor=anchor,
            spacing=self.px(spacing),
        )

    def measure(self, text: str, font) -> tuple[float, float]:
        """Text width/height in *design* units, whatever the render scale."""
        left, top, right, bottom = self.draw.textbbox((0, 0), text, font=font)
        return ((right - left) / self.scale, (bottom - top) / self.scale)

    def paste(self, image: Image.Image, xy, *, mask: Image.Image | None = None):
        if mask is not None:
            self.image.paste(image, (self.px(xy[0]), self.px(xy[1])), mask)
        elif image.mode == "RGBA":
            self.image.paste(image, (self.px(xy[0]), self.px(xy[1])), image)
        else:
            self.image.paste(image, (self.px(xy[0]), self.px(xy[1])))

    def rounded_image(
        self,
        image: Image.Image,
        box,
        *,
        radius: float = 0,
        mask: Image.Image | None = None,
    ):
        """Place an image inside a rounded region of the design canvas."""
        x0, y0, x1, y1 = (self.px(v) for v in box)
        target = image.resize((max(1, x1 - x0), max(1, y1 - y0)), Image.Resampling.LANCZOS)
        if mask is not None:
            self.image.paste(target, (x0, y0), mask)
        elif target.mode == "RGBA":
            self.image.paste(target, (x0, y0), target)
        else:
            self.image.paste(target, (x0, y0))


@dataclass(frozen=True)
class _Layout:
    """Vertical layout for one page, in design coordinates."""

    height: int
    podium_y: int
    list_y: int
    footer_y: int

    @classmethod
    def build(cls, *, podium_count: int, row_count: int) -> "_Layout":
        podium = PODIUM_HEIGHT if podium_count else 0
        rows = row_count * ROW_PITCH + (30 if row_count else 0)
        height = max(MIN_HEIGHT, HEADER_HEIGHT + podium + rows + FOOTER_HEIGHT)
        content_y = HEADER_HEIGHT - 8
        return cls(
            height=height,
            podium_y=content_y,
            list_y=content_y + podium,
            footer_y=height - 45,
        )


class LeaderboardImageRenderer:
    """Renders the voice-activity leaderboard card."""

    def __init__(
        self,
        *,
        scale: int = DEFAULT_SCALE,
        cache_ttl: float = 45.0,
        cache_entries: int = 96,
    ) -> None:
        self.font_lib = FontLibrary()
        self.scale = max(1, min(_MAX_SCALE, int(scale or DEFAULT_SCALE)))
        self._cache: TTLCache[bytes] = TTLCache(
            ttl=max(0.0, float(cache_ttl)) or 1.0,
            max_entries=max(1, int(cache_entries)),
        )
        self._flight = SingleFlight()
        self._fonts: dict[tuple[str, int], ImageFont.ImageFont] = {}

    # -- fonts ---------------------------------------------------------

    def _font(self, role: str, size: int, scale: int | None = None) -> ImageFont.ImageFont:
        pixels = max(8, int(round(size * (scale or self.scale))))
        key = (role, pixels)
        cached = self._fonts.get(key)
        if cached is not None:
            return cached
        font: ImageFont.ImageFont | None = None
        if PILLOW_AVAILABLE:
            try:
                font_set = self.font_lib.get(role, pixels)
                if font_set.faces:
                    font = font_set.faces[0].font
            except Exception:  # noqa: BLE001 - fall back to a bitmap font
                log.debug("Font %s@%d unavailable", role, pixels, exc_info=True)
        if font is None:
            font = ImageFont.load_default()
        self._fonts[key] = font
        return font

    def clear_caches(self) -> None:
        self._cache.clear()
        self._fonts.clear()

    # -- public API ----------------------------------------------------

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
        subtitle_override: str | None = None,
        use_cache: bool = True,
    ) -> bytes:
        """Render one page of the leaderboard as PNG bytes."""
        start_idx = (page - 1) * page_size
        page_members = list(members[start_idx : start_idx + page_size])
        total_seconds_all = sum(m.total_seconds for m in members)
        key = self._cache_key(
            guild_name,
            period,
            page_members,
            date_str,
            page,
            page_size,
            title_override,
            subtitle_override,
        )
        if use_cache:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
        return await self._flight.run(
            key, lambda: self._render_uncached(
                guild_name,
                period,
                page_members,
                len(members),
                total_seconds_all,
                date_str,
                page,
                title_override,
                subtitle_override,
                key if use_cache else "",
            )
        )

    async def _render_uncached(
        self,
        guild_name: str,
        period: VoicePeriod,
        page_members: list[LeaderboardMember],
        total_count: int,
        total_seconds_all: int,
        date_str: str | None,
        page: int,
        title_override: str | None,
        subtitle_override: str | None,
        cache_key: str,
    ) -> bytes:
        avatar_pixels = 192 if self.scale > 1 else 128
        if any(member.avatar_url for member in page_members):
            avatars = await asyncio.gather(
                *(
                    fetch_avatar(
                        m.avatar_url, m.display_name, size=avatar_pixels
                    )
                    for m in page_members
                )
            )
        else:
            avatars = [None] * len(page_members)

        png = await run_render(
            self._draw_canvas,
            guild_name,
            period,
            page_members,
            avatars,
            date_str,
            page,
            total_count,
            total_seconds_all,
            title_override,
            subtitle_override,
        )
        if cache_key and png:
            self._cache.set(cache_key, png)
        return png

    def _cache_key(
        self,
        guild_name: str,
        period: VoicePeriod,
        members: Sequence[LeaderboardMember],
        date_str: str | None,
        page: int,
        page_size: int,
        title_override: str | None,
        subtitle_override: str | None,
    ) -> str:
        digest = sha256()
        digest.update(f"{guild_name}\x1f{period.value}\x1f{date_str}\x1f{page}".encode())
        digest.update(f"\x1f{page_size}\x1f{title_override}\x1f{subtitle_override}".encode())
        digest.update(f"\x1f{self.scale}".encode())
        for member in members:
            digest.update(
                (
                    f"\x1e{member.rank}|{member.user_id}|{member.display_name}|"
                    f"{member.total_seconds}|{member.percentage:.4f}|"
                    f"{int(member.is_active_now)}|{member.avatar_url}"
                ).encode("utf-8", "replace")
            )
        return digest.hexdigest()

    # -- drawing -------------------------------------------------------

    def _draw_canvas(
        self,
        guild_name: str,
        period: VoicePeriod,
        members: list[LeaderboardMember],
        avatars: Sequence[Image.Image | None],
        date_str: str | None,
        page: int,
        total_count: int,
        total_seconds_all: int | None = None,
        title_override: str | None = None,
        subtitle_override: str | None = None,
    ) -> bytes:
        num_podium = min(len(members), 3) if page == 1 else 0
        list_items = members[num_podium:] if page == 1 else list(members)
        list_avatars = list(avatars[num_podium:]) if page == 1 else list(avatars)
        layout = _Layout.build(podium_count=num_podium, row_count=len(list_items))

        accent = _PERIOD_ACCENT.get(period, ACCENT_PURPLE)
        scale = self.scale

        # The backdrop is pure gradient: build it at design resolution (cheap)
        # and scale it up for supersampled drawing. Nothing is lost visually.
        backdrop = _background(WIDTH, layout.height, accent)
        canvas = (
            backdrop.resize(
                (WIDTH * scale, layout.height * scale), Image.Resampling.BILINEAR
            ).copy()
            if scale > 1
            else backdrop.copy()
        )
        painter = _Painter(canvas, scale)

        self._draw_header(
            painter,
            period=period,
            guild_name=guild_name,
            date_str=date_str,
            total_count=total_count,
            total_seconds=(
                total_seconds_all
                if total_seconds_all is not None
                else sum(m.total_seconds for m in members)
            ),
            accent=accent,
            title_override=title_override,
            subtitle_override=subtitle_override,
            live_count=sum(1 for m in members if m.is_active_now),
        )

        if num_podium:
            self._draw_podium(
                painter,
                layout.podium_y,
                members[:num_podium],
                [av if av is not None else _render_fallback_avatar(m.display_name, 96, hash(m.display_name))
                 for m, av in zip(members[:num_podium], avatars[:num_podium])],
            )

        if list_items:
            # One grouped sheet for the whole list, not a box per person.
            self._draw_list_container(
                painter,
                layout.list_y,
                layout.list_y + len(list_items) * ROW_PITCH + 6,
                accent,
            )
            for index, member in enumerate(list_items):
                avatar = list_avatars[index] if index < len(list_avatars) else None
                if avatar is None:
                    avatar = _render_fallback_avatar(
                        member.display_name, 96, hash(member.display_name)
                    )
                self._draw_list_row(
                    painter,
                    layout.list_y + index * ROW_PITCH,
                    member,
                    avatar,
                    accent=accent,
                    last=index == len(list_items) - 1,
                )
        elif num_podium == 0:
            self._draw_empty_state(
                painter, layout.list_y, layout.footer_y - 20, accent
            )

        # Down-sample once: this is what makes every edge and glyph smooth.
        final = (
            canvas.convert("RGB").resize((WIDTH, layout.height), Image.Resampling.LANCZOS)
            if scale > 1
            else canvas.convert("RGB")
        )
        self._draw_chrome(final, layout, page=page, accent=accent)

        buffer = io.BytesIO()
        # ``optimize=True`` costs ~200ms on a 1000x1300 card for a few percent
        # of size; Discord re-encodes anyway. A middle compression level is
        # visually identical and roughly four times faster.
        final.save(buffer, format="PNG", compress_level=6)
        return buffer.getvalue()

    # -- sections ------------------------------------------------------

    def _draw_header(
        self,
        p: _Painter,
        *,
        period: VoicePeriod,
        guild_name: str,
        date_str: str | None,
        total_count: int,
        total_seconds: int,
        accent: tuple[int, int, int],
        title_override: str | None,
        subtitle_override: str | None,
        live_count: int = 0,
    ) -> None:
        """Eyebrow, title, one line of context, and the one number that matters.

        No pills, no boxed statistics, no shouting: the server is a quiet grey
        line, the period is the headline, and the group's total time sits on the
        right where a reader's eye lands after the title.
        """
        eyebrow_font = self._font("medium", 13)
        title_font = self._font("display", 44)
        sub_font = self._font("regular", 15)
        small_font = self._font("regular", 12)
        stat_label_font = self._font("regular", 13)
        stat_font = self._font("display", 32)

        title = title_override or period.label
        p.text(
            (PAD, 40),
            self._truncate(p, guild_name, 560, eyebrow_font),
            font=eyebrow_font,
            fill=TEXT_MUTED,
        )
        p.text(
            (PAD, 62),
            self._truncate(p, title, 560, title_font),
            font=title_font,
            fill=TEXT_PRIMARY,
        )

        if subtitle_override:
            subtitle = subtitle_override
        else:
            if total_count:
                parts = [f"{total_count} {'person' if total_count == 1 else 'people'}"]
            else:
                parts = ["nobody yet"]
            if live_count:
                parts.append(
                    f"{live_count} in a call right now" if live_count > 1 else "1 in a call now"
                )
            subtitle = "  ·  ".join(parts)
        p.text((PAD, 122), subtitle, font=sub_font, fill=TEXT_SECONDARY)
        if date_str:
            p.text((PAD, 146), date_str, font=small_font, fill=TEXT_MUTED)

        # The group's total, right-aligned, with no container around it.
        right = WIDTH - PAD
        p.text((right, 44), "Time together", font=stat_label_font, fill=TEXT_MUTED, anchor="ra")
        p.text(
            (right, 64),
            _compact_duration(total_seconds) if total_seconds else "—",
            font=stat_font,
            fill=TEXT_PRIMARY if total_seconds else TEXT_MUTED,
            anchor="ra",
        )

    def _lift(
        self,
        p: _Painter,
        box,
        *,
        radius: float,
        strength: int = 10,
        spread: float = 2.0,
    ) -> None:
        """A two-pixel shadow, drawn as a couple of very faint rounded rects.

        A real Gaussian blur at 2× supersampling costs more than the whole card
        is worth; two slightly offset rectangles a few percent darker than the
        page survive the LANCZOS down-sample as a soft edge and cost
        microseconds.
        """
        x0, y0, x1, y1 = box
        # Two passes only: a tight contact shadow and a wider, fainter one.
        #
        # ImageDraw does not alpha-blend a fill onto an RGBA canvas — it
        # *replaces* the pixel, alpha included — so a translucent grey would
        # survive the later RGB conversion as a solid dark line. The shadow is
        # therefore pre-blended against the page colour, which is all it ever
        # sits on.
        for offset, alpha in ((spread * 0.6, strength), (spread, max(2, strength // 2))):
            p.rect(
                (x0, y0 + offset, x1, y1 + offset),
                radius=radius + offset * 0.5,
                fill=_mix(BG_BOTTOM, (23, 23, 30), alpha / 255.0),
            )

    def _card(
        self,
        p: _Painter,
        box,
        accent: tuple[int, int, int],
        *,
        radius: float = 20,
        fill: tuple[int, int, int] | None = None,
        outline: tuple[int, int, int] | None = None,
        tint: float = 0.0,
        width: float = 1,
        shadow: int = 10,
    ) -> None:
        """White card, hairline border, the faintest lift off the page."""
        if shadow:
            self._lift(p, box, radius=radius, strength=shadow)
        base = fill or (_mix(SURFACE_COLOR, accent, tint) if tint else SURFACE_COLOR)
        p.rect(
            box,
            radius=radius,
            fill=(*base, 255),
            outline=outline or CARD_BORDER,
            width=width,
        )

    # Kept under its old name: the daily report and the leaderboard both call it.
    _glass_card = _card

    def _draw_podium(
        self,
        p: _Painter,
        top: int,
        members: list[LeaderboardMember],
        avatars: list[Image.Image],
    ) -> None:
        card_w, gap = 288, 20
        order = {3: [1, 0, 2], 2: [0, 1], 1: [0]}[len(members)]
        total_w = len(members) * card_w + (len(members) - 1) * gap
        start_x = (WIDTH - total_w) // 2
        # Shared baseline: the winner's card is simply taller, so the bars and
        # percentages line up right across the row.
        baseline = top + 292
        short_h, tall_h = 266, 292

        for slot, member_index in enumerate(order):
            member = members[member_index]
            avatar = avatars[member_index]
            x = start_x + slot * (card_w + gap)
            rank = member.rank if member.rank <= 3 else member_index + 1
            accent, deep = _MEDAL_RAMPS.get(rank, (ACCENT_CYAN, ACCENT_BLUE))
            card_h = tall_h if rank == 1 else short_h
            self._draw_podium_card(
                p, x, baseline - card_h, card_w, card_h, member, avatar, accent, deep, rank
            )

    def _draw_podium_card(
        self,
        p: _Painter,
        x: int,
        y: int,
        w: int,
        h: int,
        member: LeaderboardMember,
        avatar: Image.Image,
        accent: tuple[int, int, int],
        deep: tuple[int, int, int],
        rank: int,
    ) -> None:
        """One podium card: portrait, name, time, a bar, and how much of the room."""
        cx = x + w / 2
        winner = rank == 1
        self._card(
            p,
            (x, y, x + w, y + h),
            accent,
            radius=24,
            tint=0.05 if winner else 0.0,
            outline=_mix(CARD_BORDER, accent, 0.30 if winner else 0.0),
            width=1,
            shadow=14 if winner else 9,
        )

        # Position, as a word rather than a coin.
        place = {1: "1st", 2: "2nd", 3: "3rd"}.get(rank, f"{rank}th")
        place_font = self._font("semibold", 12)
        p.text((cx, y + 22), place, font=place_font, fill=_mix(accent, TEXT_SECONDARY, 0.35), anchor="ma")

        av_size = 84 if winner else 76
        pct_y = y + h - 20
        bar_y = y + h - 40
        time_y = y + h - 62
        name_y = y + h - 92
        av_centre = name_y - 30 - av_size / 2

        # A single quiet ring, not a gradient halo.
        ring_size = av_size + 6
        p.ellipse(
            (cx - ring_size / 2, av_centre - ring_size / 2, cx + ring_size / 2, av_centre + ring_size / 2),
            fill=_mix(accent, (255, 255, 255), 0.82),
        )
        p.paste(
            avatar.resize((p.px(av_size), p.px(av_size)), Image.Resampling.LANCZOS),
            (cx - av_size / 2, av_centre - av_size / 2),
            mask=_create_circle_mask(p.px(av_size)),
        )

        name_font = self._font("semibold", 17)
        p.text(
            (cx, name_y),
            self._truncate(p, member.display_name, w - 40, name_font),
            font=name_font,
            fill=TEXT_PRIMARY,
            anchor="ms",
        )
        p.text(
            (cx, time_y),
            member.formatted_time,
            font=self._font("display", 26),
            fill=TEXT_PRIMARY,
            anchor="ms",
        )

        share = _clamp_share(member.percentage)
        bar_w = w - 84
        bar_x = cx - bar_w / 2
        p.rect((bar_x, bar_y, bar_x + bar_w, bar_y + 4), radius=2, fill=TRACK_COLOR)
        if share > 0:
            filled = max(3.0, bar_w * share / 100.0)
            p.rect((bar_x, bar_y, bar_x + filled, bar_y + 4), radius=2, fill=accent)
        p.text(
            (cx, pct_y),
            f"{share:.0f}% of the time",
            font=self._font("regular", 12),
            fill=TEXT_MUTED,
            anchor="ma",
        )

    def _draw_list_container(
        self, p: _Painter, top: int, bottom: int, accent: tuple[int, int, int]
    ) -> None:
        """The grouped list: one white sheet, rows separated by hairlines."""
        self._card(p, (PAD, top, WIDTH - PAD, bottom), accent, radius=22, shadow=10)

    def _draw_list_row(
        self,
        p: _Painter,
        y: int,
        member: LeaderboardMember,
        avatar: Image.Image,
        *,
        accent: tuple[int, int, int],
        last: bool = False,
    ) -> None:
        h = ROW_HEIGHT
        left, right = PAD, WIDTH - PAD
        centre = y + h / 2

        # Hairline separator, inset to the text column like a grouped table.
        if not last:
            p.line(
                [(left + 44, y + h), (right - 18, y + h)],
                fill=_mix(CARD_BORDER_SOFT, (255, 255, 255), 0.45),
                width=1,
            )

        rank_font = self._font("semibold", 13)
        p.text(
            (left + 22, centre),
            str(member.rank),
            font=rank_font,
            fill=TEXT_MUTED,
            anchor="lm",
        )

        av_size = 40
        av_x = left + 44
        p.paste(
            avatar.resize((p.px(av_size), p.px(av_size)), Image.Resampling.LANCZOS),
            (av_x, centre - av_size / 2),
            mask=_create_circle_mask(p.px(av_size)),
        )

        time_font = self._font("semibold", 15)
        time_text = member.formatted_time
        time_w = p.measure(time_text, time_font)[0]
        time_x = right - 18 - time_w
        p.text((right - 18, centre - 7), time_text, font=time_font, fill=TEXT_PRIMARY, anchor="rm")

        share = _clamp_share(member.percentage)
        share_font = self._font("regular", 11)
        p.text(
            (right - 18, centre + 12),
            f"{share:.0f}%",
            font=share_font,
            fill=TEXT_MUTED,
            anchor="rm",
        )

        name_font = self._font("semibold", 15)
        name_x = av_x + av_size + 16
        if member.is_active_now:
            # A green dot, the way a presence dot works everywhere else.
            p.ellipse((name_x, centre - 12, name_x + 7, centre - 5), fill=LIVE_GREEN)
            name_x += 15
        name_max = max(60, time_x - 26 - name_x)
        p.text(
            (name_x, centre - 8),
            self._truncate(p, member.display_name, name_max, name_font),
            font=name_font,
            fill=TEXT_PRIMARY,
            anchor="lm",
        )

        bar_y = centre + 9
        bar_w = max(24.0, time_x - 40 - name_x)
        p.rect((name_x, bar_y, name_x + bar_w, bar_y + 3), radius=1.5, fill=TRACK_COLOR)
        if share > 0:
            filled = max(3.0, bar_w * share / 100.0)
            p.rect((name_x, bar_y, name_x + filled, bar_y + 3), radius=1.5, fill=accent)

    def _draw_empty_state(
        self, p: _Painter, top: int, bottom: int, accent: tuple[int, int, int]
    ) -> None:
        """A deliberate empty state: a quiet icon, a plain sentence, a hint."""
        centre = (top + bottom) / 2
        icon_cy = centre - 54

        p.ellipse(
            (WIDTH / 2 - 34, icon_cy - 34, WIDTH / 2 + 34, icon_cy + 34),
            fill=_mix(SURFACE_LIGHT, accent, 0.08),
        )
        p.draw.arc(
            (p.px(WIDTH / 2 - 21), p.px(icon_cy - 22), p.px(WIDTH / 2 + 21), p.px(icon_cy + 20)),
            start=180,
            end=360,
            fill=TEXT_MUTED,
            width=p.px(2.5),
        )
        for dx in (-21, 21):
            p.rect(
                (WIDTH / 2 + dx - 4, icon_cy - 3, WIDTH / 2 + dx + 4, icon_cy + 13),
                radius=4,
                fill=TEXT_MUTED,
            )

        p.text(
            (WIDTH / 2, centre + 20),
            "No one has been in a call yet",
            font=self._font("semibold", 19),
            fill=TEXT_PRIMARY,
            anchor="mm",
        )
        p.text(
            (WIDTH / 2, centre + 50),
            "Join a voice channel and this fills itself in.",
            font=self._font("regular", 14),
            fill=TEXT_SECONDARY,
            anchor="mm",
        )

    def _draw_chrome(
        self,
        canvas: Image.Image,
        layout: _Layout,
        *,
        page: int,
        accent: tuple[int, int, int],
    ) -> None:
        """Hairlines, footer and page number — drawn at native resolution.

        Supersampling softens a one-pixel rule into a grey smear, so the
        structural lines are laid down after the down-sample instead.
        """
        draw = ImageDraw.Draw(canvas)
        header_rule = HEADER_HEIGHT - 34
        draw.line([(PAD, header_rule), (WIDTH - PAD, header_rule)], fill=CARD_BORDER_SOFT, width=1)
        draw.line(
            [(PAD, layout.footer_y - 12), (WIDTH - PAD, layout.footer_y - 12)],
            fill=CARD_BORDER,
            width=1,
        )

        brand_font = self._font("semibold", 12, scale=1)
        meta_font = self._font("regular", 11, scale=1)

        draw.text((PAD, layout.footer_y + 1), "Paradise", font=brand_font, fill=TEXT_SECONDARY)
        brand_w = draw.textbbox((0, 0), "Paradise", font=brand_font)[2]
        draw.text(
            (PAD + brand_w + 8, layout.footer_y + 2),
            "· voice",
            font=meta_font,
            fill=TEXT_MUTED,
        )
        right = f"page {page}"
        right_w = draw.textbbox((0, 0), right, font=meta_font)[2]
        draw.text(
            (WIDTH - PAD - right_w, layout.footer_y + 2),
            right,
            font=meta_font,
            fill=TEXT_MUTED,
        )

    # -- helpers -------------------------------------------------------

    def _truncate(
        self, p: _Painter, text: str, max_w: float, font: ImageFont.ImageFont
    ) -> str:
        """Truncate with an ellipsis, measuring with the real font."""
        text = str(text or "")
        if p.measure(text, font)[0] <= max_w:
            return text
        low, high = 0, len(text)
        best = "…"
        while low < high:
            mid = (low + high) // 2
            candidate = text[:mid].rstrip() + "…"
            if p.measure(candidate, font)[0] <= max_w:
                best, low = candidate, mid + 1
            else:
                high = mid
        return best


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _format_hours_minutes(total_seconds: int) -> str:
    total_seconds = max(0, int(total_seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{total_seconds}s"


def _compact_duration(total_seconds: int) -> str:
    """A large, glanceable duration for the header stat card."""
    total_seconds = max(0, int(total_seconds))
    if total_seconds >= 360000:  # 100h+
        return f"{total_seconds / 3600:.0f}h"
    return _format_hours_minutes(total_seconds)


# ---------------------------------------------------------------------------
# Prewarm
# ---------------------------------------------------------------------------


def prewarm() -> None:
    """Build the cached backdrops and warm the font cache. Never raises."""
    try:
        renderer = LeaderboardImageRenderer(scale=DEFAULT_SCALE)
        for role, size in (
            ("display", 44),
            ("display", 32),
            ("display", 26),
            ("semibold", 19),
            ("semibold", 17),
            ("semibold", 15),
            ("semibold", 13),
            ("semibold", 12),
            ("medium", 13),
            ("regular", 15),
            ("regular", 14),
            ("regular", 13),
            ("regular", 12),
            ("regular", 11),
        ):
            renderer._font(role, size)
        for height in (MIN_HEIGHT, 620, 720, 900):
            _background(WIDTH * DEFAULT_SCALE, height * DEFAULT_SCALE, ACCENT_CYAN)
        _horizontal_ramp(160, 5, ACCENT_PURPLE, ACCENT_CYAN)
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            _prune_disk_cache()
        except OSError:
            pass
    except Exception:  # noqa: BLE001 - prewarm is a bonus, never a requirement
        log.debug("Leaderboard prewarm failed", exc_info=True)


__all__ = [
    "CARD_BORDER",
    "DEFAULT_SCALE",
    "LeaderboardImageRenderer",
    "ROW_HEIGHT",
    "ROW_PITCH",
    "WIDTH",
    "_render_fallback_avatar",
    "fetch_avatar",
    "prewarm",
]
