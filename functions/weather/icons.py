"""Deterministic vector icon set (no emoji, no bitmaps).

Every icon is drawn procedurally with a consistent rounded-stroke language
— the same visual grammar Material Symbols uses — at 4x supersample and
downsampled for clean anti-aliased edges at any size. Weather icons take a
``family`` (see :mod:`functions.weather.palette`) plus ``is_day`` so the
same shape language covers every condition/time combination.
"""

from __future__ import annotations

import math

from PIL import Image, ImageDraw

from .palette import mix

Rgb = tuple[int, int, int]

_SS = 4  # supersample factor


def _canvas(size: int) -> tuple[Image.Image, ImageDraw.ImageDraw, float]:
    s = size * _SS
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    return im, ImageDraw.Draw(im), s / 2.0


def _finish(im: Image.Image, size: int) -> Image.Image:
    return im.resize((size, size), Image.LANCZOS)


def _stroke_line(draw: ImageDraw.ImageDraw, pts, fill, width) -> None:
    draw.line(pts, fill=fill, width=width, joint="curve")
    r = width / 2
    for x, y in (pts[0], pts[-1]):
        draw.ellipse((x - r, y - r, x + r, y + r), fill=fill)


def _sun_glyph(draw, cx, cy, r, colour, width, rays=8, ray_len=0.62, ray_gap=1.5):
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=colour)
    for i in range(rays):
        a = (2 * math.pi / rays) * i
        x0 = cx + math.cos(a) * (r + width * ray_gap)
        y0 = cy + math.sin(a) * (r + width * ray_gap)
        x1 = cx + math.cos(a) * (r * (1 + ray_len))
        y1 = cy + math.sin(a) * (r * (1 + ray_len))
        _stroke_line(draw, [(x0, y0), (x1, y1)], colour, width)


def _cloud_shape(draw, cx, cy, w, h, colour, outline=None, width=0):
    """A single friendly rounded-cloud silhouette centred at (cx, cy)."""
    body = (cx - w * 0.5, cy - h * 0.15, cx + w * 0.5, cy + h * 0.55)
    lumps = [
        (cx - w * 0.30, cy - h * 0.05, h * 0.42),
        (cx - w * 0.02, cy - h * 0.32, h * 0.58),
        (cx + w * 0.30, cy - h * 0.02, h * 0.46),
    ]
    if outline is not None:
        for lx, ly, lr in lumps:
            draw.ellipse((lx - lr, ly - lr, lx + lr, ly + lr), fill=colour, outline=outline, width=width)
        draw.rounded_rectangle(body, radius=h * 0.32, fill=colour, outline=outline, width=width)
    else:
        for lx, ly, lr in lumps:
            draw.ellipse((lx - lr, ly - lr, lx + lr, ly + lr), fill=colour)
        draw.rounded_rectangle(body, radius=h * 0.32, fill=colour)


def _raindrops(draw, cx, cy, spread, count, colour, width, length=0.24, size=1.0):
    for i in range(count):
        x = cx + (i - (count - 1) / 2) * spread
        y0 = cy
        _stroke_line(draw, [(x, y0), (x - spread * 0.18 * size, y0 + spread * length * size)], colour, width)


def _snowflake_dots(draw, cx, cy, spread, count, colour, r):
    for i in range(count):
        x = cx + (i - (count - 1) / 2) * spread
        draw.ellipse((x - r, cy - r, x + r, cy + r), fill=colour)


def draw_weather_icon(size: int, family: str, is_day: bool, colour: Rgb, accent: Rgb | None = None, night_stars: bool = False) -> Image.Image:
    """Return an RGBA glyph for a weather ``family`` at ``size`` px square."""
    im, d, c = _canvas(size)
    width = max(2, int(size * _SS * 0.052))
    accent = accent or colour

    if family == "clear":
        if is_day:
            _sun_glyph(d, c, c, size * _SS * 0.27, colour, width)
        else:
            r = size * _SS * 0.30
            d.ellipse((c - r, c - r, c + r, c + r), fill=colour)
            bite_r = r * 1.05
            d.ellipse((c - r * 0.15, c - r * 1.05, c + bite_r * 1.7, c + bite_r * 0.95), fill=(0, 0, 0, 0))
            # crescent via difference mask
            mask = Image.new("L", im.size, 0)
            md = ImageDraw.Draw(mask)
            md.ellipse((c - r, c - r, c + r, c + r), fill=255)
            md.ellipse((c - r * 0.25, c - r * 1.15, c + r * 1.75, c + r * 0.85), fill=0)
            solid = Image.new("RGBA", im.size, (*colour, 255))
            im.paste(solid, (0, 0), mask)
            for i in range(3):
                sx = c + r * (0.9 + i * 0.35)
                sy = c - r * (0.7 - i * 0.3)
                sr = size * _SS * 0.02
                d.ellipse((sx - sr, sy - sr, sx + sr, sy + sr), fill=(*accent, 210))
        return _finish(im, size)

    if family == "cloud":
        # Partly cloudy: a sun/moon peeking out from behind a single cloud.
        if is_day:
            sr = size * _SS * 0.17
            _sun_glyph(d, c - size * _SS * 0.16, c - size * _SS * 0.18, sr, accent, int(width * 0.8), rays=8, ray_len=0.55)
        else:
            r = size * _SS * 0.15
            mx, my = c - size * _SS * 0.17, c - size * _SS * 0.17
            mask = Image.new("L", im.size, 0)
            md = ImageDraw.Draw(mask)
            md.ellipse((mx - r, my - r, mx + r, my + r), fill=255)
            md.ellipse((mx - r * 0.2, my - r * 1.15, mx + r * 1.7, my + r * 0.75), fill=0)
            solid = Image.new("RGBA", im.size, (*accent, 255))
            im.paste(solid, (0, 0), mask)
        _cloud_shape(d, c + size * _SS * 0.03, c + size * _SS * 0.10, size * _SS * 0.62, size * _SS * 0.40, colour)
        return _finish(im, size)

    if family == "overcast":
        # Full cloud cover: two layered, denser cloud silhouettes, no sun
        # or moon peeking through — visually distinct from "partly cloudy".
        _cloud_shape(d, c - size * _SS * 0.10, c - size * _SS * 0.04, size * _SS * 0.56, size * _SS * 0.32, mix(colour, accent, 0.35) if accent != colour else colour)
        _cloud_shape(d, c + size * _SS * 0.06, c + size * _SS * 0.14, size * _SS * 0.66, size * _SS * 0.38, colour)
        return _finish(im, size)

    if family in ("rain", "storm"):
        _cloud_shape(d, c, c - size * _SS * 0.06, size * _SS * 0.66, size * _SS * 0.36, colour)
        if family == "storm":
            bolt_c = c
            bolt_y = c + size * _SS * 0.16
            pts = [
                (bolt_c + size * _SS * 0.03, bolt_y),
                (bolt_c - size * _SS * 0.08, bolt_y + size * _SS * 0.16),
                (bolt_c + size * _SS * 0.01, bolt_y + size * _SS * 0.16),
                (bolt_c - size * _SS * 0.06, bolt_y + size * _SS * 0.34),
            ]
            d.line(pts, fill=accent, width=width, joint="curve")
        else:
            _raindrops(d, c, c + size * _SS * 0.20, size * _SS * 0.17, 3, accent, width)
        return _finish(im, size)

    if family == "snow":
        _cloud_shape(d, c, c - size * _SS * 0.06, size * _SS * 0.66, size * _SS * 0.36, colour)
        _snowflake_dots(d, c, c + size * _SS * 0.24, size * _SS * 0.19, 3, accent, width * 0.65)
        return _finish(im, size)

    if family == "fog":
        for i, dy in enumerate((-0.16, -0.02, 0.12, 0.26)):
            y = c + size * _SS * dy
            w = size * _SS * (0.62 - abs(dy) * 0.3)
            col = colour if i % 2 == 0 else accent
            _stroke_line(d, [(c - w / 2, y), (c + w / 2, y)], col, width)
        return _finish(im, size)

    # fallback: a plain rounded cloud
    _cloud_shape(d, c, c, size * _SS * 0.66, size * _SS * 0.40, colour)
    return _finish(im, size)


def _icon(name):
    def deco(fn):
        _METRIC_ICONS[name] = fn
        return fn
    return deco


_METRIC_ICONS: dict[str, object] = {}


@_icon("humidity")
def _humidity(d, c, s, colour, width):
    r = s * 0.26
    d.polygon([(c, c - r * 1.45), (c - r * 0.95, c + r * 0.25), (c + r * 0.95, c + r * 0.25)], fill=colour)
    d.pieslice((c - r * 0.95, c - r * 0.25, c + r * 0.95, c + r * 1.45), 0, 360, fill=colour)


@_icon("wind")
def _wind(d, c, s, colour, width):
    for i, (dy, x1_frac, r_frac) in enumerate(((-0.15, 0.10, 0.11), (0.02, 0.22, 0.14), (0.19, -0.02, 0.10))):
        y = c + s * dy
        x0 = c - s * 0.32
        x1 = c + s * x1_frac
        d.line((x0, y, x1, y), fill=colour, width=width)
        hook_r = s * r_frac
        d.arc((x1 - hook_r, y - hook_r, x1 + hook_r, y + hook_r), -100, 100, fill=colour, width=width)
        cap = width / 2
        d.ellipse((x0 - cap, y - cap, x0 + cap, y + cap), fill=colour)


@_icon("pressure")
def _pressure(d, c, s, colour, width):
    r = s * 0.32
    d.ellipse((c - r, c - r, c + r, c + r), outline=colour, width=width)
    d.line((c, c, c + r * 0.55, c - r * 0.35), fill=colour, width=width)
    d.ellipse((c - width * 0.7, c - width * 0.7, c + width * 0.7, c + width * 0.7), fill=colour)


@_icon("visibility")
def _visibility(d, c, s, colour, width):
    r = s * 0.30
    d.arc((c - r, c - r * 0.75, c + r, c + r * 0.75), 200, 340, fill=colour, width=width)
    d.arc((c - r, c - r * 0.75, c + r, c + r * 0.75), 20, 160, fill=colour, width=width)
    pr = s * 0.13
    d.ellipse((c - pr, c - pr, c + pr, c + pr), fill=colour)


@_icon("uv")
def _uv(d, c, s, colour, width):
    _sun_glyph(d, c, c, s * 0.22, colour, width, rays=8, ray_len=0.5)


@_icon("aqi")
def _aqi(d, c, s, colour, width):
    r = s * 0.30
    d.ellipse((c - r, c - r, c + r, c + r), outline=colour, width=max(2, int(width * 0.8)))
    for a in (0, 120, 240):
        rad = math.radians(a)
        pr = s * 0.045
        x, y = c + math.cos(rad) * r * 0.5, c + math.sin(rad) * r * 0.5
        d.ellipse((x - pr, y - pr, x + pr, y + pr), fill=colour)
    d.ellipse((c - s * 0.05, c - s * 0.05, c + s * 0.05, c + s * 0.05), fill=colour)


def _horizon_sun(d, c, s, colour, width, rising: bool):
    r = s * 0.20
    hy = c + s * 0.10
    d.pieslice((c - r, hy - r, c + r, hy + r), 180, 360, fill=colour)
    d.line((c - s * 0.34, hy, c + s * 0.34, hy), fill=colour, width=width)
    ah = s * 0.045
    for dx in (-0.15, 0.0, 0.15):
        x = c + s * dx
        top_y, bot_y = hy - r * 2.0, hy - r * 1.15
        if rising:
            y0, y1 = bot_y, top_y  # tail near horizon, head up
        else:
            y0, y1 = top_y, bot_y  # tail up high, head near horizon
        _stroke_line(d, [(x, y0), (x, y1)], colour, max(2, int(width * 0.72)))
        if rising:
            d.polygon([(x - ah, y1 + ah * 1.3), (x + ah, y1 + ah * 1.3), (x, y1 - ah * 0.8)], fill=colour)
        else:
            d.polygon([(x - ah, y1 - ah * 1.3), (x + ah, y1 - ah * 1.3), (x, y1 + ah * 0.8)], fill=colour)


@_icon("sunrise")
def _sunrise(d, c, s, colour, width):
    _horizon_sun(d, c, s, colour, width, rising=True)


@_icon("sunset")
def _sunset(d, c, s, colour, width):
    _horizon_sun(d, c, s, colour, width, rising=False)


@_icon("drop")
def _drop(d, c, s, colour, width):
    r = s * 0.24
    d.pieslice((c - r, c - r * 0.2, c + r, c + r * 1.8), 0, 360, fill=colour)
    d.polygon([(c, c - r * 1.5), (c - r * 0.85, c + r * 0.2), (c + r * 0.85, c + r * 0.2)], fill=colour)


@_icon("gust")
def _gust(d, c, s, colour, width):
    _wind(d, c, s, colour, width)


@_icon("compass")
def _compass(d, c, s, colour, width):
    r = s * 0.32
    d.ellipse((c - r, c - r, c + r, c + r), outline=colour, width=width)
    d.polygon([(c, c - r * 0.7), (c - r * 0.22, c), (c, c + r * 0.7), (c + r * 0.22, c)], fill=colour)


def draw_metric_icon(size: int, name: str, colour: Rgb) -> Image.Image:
    im, d, c = _canvas(size)
    width = max(2, int(size * _SS * 0.075))
    fn = _METRIC_ICONS.get(name)
    if fn is None:
        r = size * _SS * 0.28
        d.ellipse((c - r, c - r, c + r, c + r), outline=colour, width=width)
    else:
        fn(d, c, size * _SS, colour, width)
    return _finish(im, size)


def draw_gauge_icon(size: int, name: str, fraction: float, colour: Rgb, track: Rgb) -> Image.Image:
    """A small radial-progress ring (0..1) with the metric glyph centred
    inside — the one deliberately circular, "instrument" shape among the
    otherwise rounded-rectangle metric tiles, used for at-a-glance scales
    like UV index or air quality rather than plain values.
    """
    im, d, c = _canvas(size)
    ring_w = max(3, int(size * _SS * 0.09))
    r = size * _SS * 0.46 - ring_w / 2
    box = (c - r, c - r, c + r, c + r)
    d.arc(box, 0, 360, fill=track, width=ring_w)
    fraction = max(0.0, min(1.0, fraction))
    if fraction > 0.003:
        start, end = -90, -90 + 360 * fraction
        d.arc(box, start, end, fill=colour, width=ring_w)
    fn = _METRIC_ICONS.get(name)
    if fn is not None:
        fn(d, c, size * _SS * 0.56, colour, max(2, int(ring_w * 0.8)))
    return _finish(im, size)
