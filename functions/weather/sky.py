"""Procedural, deterministic sky background.

No photographs, no single reused gradient: the sky is composed in cheap,
low-resolution layers (gradient -> horizon glow -> clouds -> haze) that are
then upsampled and softened, plus a handful of full-resolution accents
(stars, sun/moon disc, rain, snow, fog bands, restrained lightning) drawn
straight onto the final canvas. Every layer reads its colour and shape
parameters from :class:`~functions.weather.palette.Palette`, so the same
function produces a bright noon sky, a peach sunset, a deep-indigo storm
night, or a hazy fog morning without any special-cased "preset" branches —
only continuous parameters (day fraction, golden light, gloom, intensity).
"""

from __future__ import annotations

import math
import random

from PIL import Image, ImageDraw, ImageFilter

from .palette import Palette, mix


def _lowres_gradient(lw: int, lh: int, stops: tuple[tuple[int, int, int], ...]) -> Image.Image:
    """Vertical multi-stop gradient, built as a 1px column then stretched."""
    n = len(stops) - 1
    col = Image.new("RGB", (1, lh))
    px = col.load()
    for y in range(lh):
        t = y / max(1, lh - 1)
        seg = min(n - 1, int(t * n))
        local_t = t * n - seg
        px[0, y] = mix(stops[seg], stops[seg + 1], local_t)
    return col.resize((lw, lh), Image.BILINEAR)


def _radial_glow(size: tuple[int, int], cx: float, cy: float, radius: float, colour: tuple[int, int, int], peak_alpha: int) -> Image.Image:
    w, h = size
    layer = Image.new("L", (w, h), 0)
    px = layer.load()
    r2 = radius * radius
    x0, x1 = max(0, int(cx - radius)), min(w, int(cx + radius) + 1)
    y0, y1 = max(0, int(cy - radius)), min(h, int(cy + radius) + 1)
    for y in range(y0, y1):
        dy2 = (y - cy) ** 2
        for x in range(x0, x1):
            d2 = (x - cx) ** 2 + dy2
            if d2 >= r2:
                continue
            t = 1.0 - math.sqrt(d2 / r2)
            px[x, y] = int(peak_alpha * (t ** 1.6))
    rgba = Image.new("RGBA", (w, h), (*colour, 0))
    rgba.putalpha(layer)
    return rgba


def _cloud_puff(rng: random.Random, rw: int, rh: int, lit: tuple[int, int, int], shadow: tuple[int, int, int], alpha: int) -> Image.Image:
    """A single soft cumulus-ish clump made of overlapping ellipses.

    The working canvas must be padded enough on every side that the widest
    lump plus its blur radius never touches the layer edge — otherwise the
    shape gets clipped into a flat rectangular seam instead of fading out.
    """
    pad_x = int(rw * 0.55 + rh * 0.3) + 6
    pad_y = int(rh * 1.1) + 6
    w, h = rw + pad_x * 2, rh + pad_y * 2
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    lumps = rng.randint(4, 7)
    for i in range(lumps):
        lx = pad_x + (i / max(1, lumps - 1)) * rw + rng.uniform(-rw * 0.06, rw * 0.06)
        lw = rw * rng.uniform(0.28, 0.42)
        lh = rh * rng.uniform(0.55, 1.0)
        ly = pad_y + rh * 0.55 - lh * rng.uniform(0.35, 0.7)
        bias = i / max(1, lumps - 1)
        col = mix(lit, shadow, 0.15 + bias * 0.25)
        draw.ellipse((lx - lw, ly - lh, lx + lw, ly + lh), fill=(*col, alpha))
    # base shadow band to ground the puffs
    draw.ellipse((pad_x + rw * 0.05, pad_y + rh * 0.35, pad_x + rw * 0.95, pad_y + rh * 1.05), fill=(*shadow, int(alpha * 0.55)))
    return layer.filter(ImageFilter.GaussianBlur(max(2.0, rh * 0.16)))


def _paint_clouds(canvas: Image.Image, rng: random.Random, palette: Palette, band_top: float, band_bottom: float) -> None:
    """Low-res cloud deck; density/shape follow the weather category."""
    w, h = canvas.size
    cat = palette.category
    lit, shadow = palette.cloud_lit, palette.cloud_shadow

    if cat.family == "fog":
        return  # fog gets its own full-res haze bands

    if cat.key == "clear":
        wisp_n = 1 if cat.intensity > 0.05 else 0
        for _ in range(wisp_n):
            y = h * rng.uniform(band_top, band_top + 0.15)
            x = w * rng.uniform(0.15, 0.85)
            puff = _cloud_puff(rng, int(w * 0.30), int(h * 0.05), lit, shadow, 70)
            canvas.alpha_composite(puff, (int(x - puff.width / 2), int(y - puff.height / 2)))
        return

    if cat.family == "cloud":
        count = 3 + int(cat.intensity * 5)
        coverage_bias = cat.intensity
        for i in range(count):
            frac = (i + rng.uniform(-0.3, 0.3)) / count
            x = w * (0.06 + 0.88 * frac)
            y = h * rng.uniform(band_top, band_top + 0.30 + coverage_bias * 0.25)
            size_w = w * rng.uniform(0.30, 0.55) * (0.7 + coverage_bias * 0.6)
            size_h = size_w * rng.uniform(0.30, 0.45)
            alpha = int(palette.cloud_alpha * 255 * rng.uniform(0.75, 1.0))
            puff = _cloud_puff(rng, int(size_w), int(size_h), lit, shadow, alpha)
            canvas.alpha_composite(puff, (int(x - puff.width / 2), int(y - puff.height / 2)))
        return

    if cat.family in ("rain", "storm"):
        # a broad, layered, darker deck low in the sky
        layers = 3 + int(cat.intensity * 2)
        for layer_i in range(layers):
            y = h * (band_top + (layer_i / max(1, layers - 1)) * 0.42)
            tone = mix(lit, shadow, 0.35 + layer_i * 0.14 + cat.gloom * 0.25)
            band = Image.new("RGBA", (w, int(h * 0.28)), (0, 0, 0, 0))
            bd = ImageDraw.Draw(band)
            segs = rng.randint(4, 6)
            for s in range(segs):
                sx = band.width * (s / segs) + rng.uniform(-20, 20)
                sw = band.width / segs * rng.uniform(1.3, 1.9)
                sh = band.height * rng.uniform(0.45, 0.95)
                sy = band.height * rng.uniform(0.15, 0.5)
                a = int(255 * (0.55 + 0.35 * cat.intensity))
                bd.ellipse((sx - sw / 2, sy - sh / 2, sx + sw / 2, sy + sh / 2), fill=(*tone, a))
            band = band.filter(ImageFilter.GaussianBlur(h * 0.02))
            canvas.alpha_composite(band, (0, int(y)))
        if cat.family == "storm":
            # a couple of tall, blocky anvil-ish masses for structure
            for _ in range(2):
                x = w * rng.uniform(0.2, 0.8)
                y = h * rng.uniform(band_top - 0.05, band_top + 0.10)
                mass = _cloud_puff(rng, int(w * 0.34), int(h * 0.5), mix(lit, shadow, 0.4), mix(shadow, (10, 10, 16), 0.4), int(255 * 0.85))
                canvas.alpha_composite(mass, (int(x - mass.width / 2), int(y - mass.height / 2)))
        return

    if cat.family == "snow":
        count = 3 + int(cat.intensity * 3)
        for i in range(count):
            frac = (i + rng.uniform(-0.25, 0.25)) / count
            x = w * (0.05 + 0.9 * frac)
            y = h * rng.uniform(band_top + 0.05, band_top + 0.32)
            size_w = w * rng.uniform(0.28, 0.46)
            size_h = size_w * 0.32
            tone_lit = mix(lit, (255, 255, 255), 0.3)
            puff = _cloud_puff(rng, int(size_w), int(size_h), tone_lit, mix(shadow, (255, 255, 255), 0.25), int(210 * (0.6 + cat.intensity * 0.4)))
            canvas.alpha_composite(puff, (int(x - puff.width / 2), int(y - puff.height / 2)))
        return


def _stars(canvas: Image.Image, rng: random.Random, w: int, h: int, top_limit: int, palette: Palette) -> None:
    """Stars need real alpha blending (dimmer near dawn/dusk or under thin
    cloud), so they are drawn on their own layer and composited — drawing
    translucent fills straight onto the accumulated canvas with
    ``ImageDraw`` would just overwrite pixels instead of blending them.
    """
    visibility = palette.phase.night_depth * (1.0 - min(1.0, palette.category.intensity * 1.2))
    if palette.category.family == "fog":
        visibility *= 0.15
    if visibility <= 0.02:
        return
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    count = int(70 * visibility)
    for _ in range(count):
        x = rng.uniform(0, w)
        y = rng.uniform(0, top_limit * 0.94)
        r = rng.uniform(0.6, 1.8) * (0.6 + visibility * 0.6)
        a = int(rng.uniform(90, 230) * visibility)
        draw.ellipse((x - r, y - r, x + r, y + r), fill=(*palette.star_colour, a))
    canvas.alpha_composite(layer)


def _sun_moon(canvas: Image.Image, palette: Palette, w: int, h: int, top_limit: int) -> None:
    cat = palette.category
    if cat.family == "cloud":
        obscured = min(1.0, cat.intensity * 1.3)
    elif cat.family in ("rain", "storm", "snow"):
        # Rain/snow/storm clouds are thick enough that the sun or moon should
        # read as barely-there at best, never a solid disc competing with
        # the hero icon.
        obscured = min(1.0, 0.55 + cat.intensity * 0.6)
    elif cat.family == "fog":
        obscured = 0.9
    else:
        obscured = 0.0
    visible_strength = max(0.0, 1.0 - obscured)
    if visible_strength <= 0.03:
        return
    ph = palette.phase
    cx = ph.sun_x * w
    cy = 0.08 * top_limit + ph.sun_y * top_limit * 0.72
    colour = palette.sun_colour if ph.show_sun else palette.moon_colour
    glow_r = min(w, h) * (0.20 if ph.show_sun else 0.11)
    glow = _radial_glow((w, h), cx, cy, glow_r, colour, int((95 if ph.show_sun else 75) * visible_strength))
    canvas.alpha_composite(glow)
    disc_r = h * (0.028 if ph.show_sun else 0.022)
    dim = int(disc_r * 4)
    a = int(255 * (0.08 + 0.92 * visible_strength))
    # Build the shape as an alpha *mask* (crescent = subtract, not "paint
    # black over it" — painting black would composite as a dark smudge
    # instead of a transparent bite once alpha_composite runs).
    mask = Image.new("L", (dim, dim), 0)
    md = ImageDraw.Draw(mask)
    md.ellipse((disc_r, disc_r, disc_r * 3, disc_r * 3), fill=255)
    if not ph.show_sun:
        md.ellipse((disc_r * 1.55, disc_r * 0.65, disc_r * 3.55, disc_r * 2.65), fill=0)
    solid = Image.new("RGBA", (dim, dim), (*colour, a))
    disc = Image.new("RGBA", (dim, dim), (0, 0, 0, 0))
    disc.paste(solid, (0, 0), mask)
    disc = disc.filter(ImageFilter.GaussianBlur(disc_r * 0.06))
    canvas.alpha_composite(disc, (int(cx - disc_r * 2), int(cy - disc_r * 2)))


def _precipitation(canvas: Image.Image, rng: random.Random, w: int, h: int, sheet_top: int, palette: Palette) -> None:
    """Rain/snow, drawn on their own alpha layer for the same reason as
    :func:`_stars` — so intensity can actually dim/thin them via alpha
    instead of every drop rendering at full, un-blended opacity."""
    cat = palette.category
    if not cat.precip:
        return
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    if cat.precip == "rain":
        n = int(60 + cat.intensity * 200)
        colour = palette.rain_colour
        for _ in range(n):
            x = rng.uniform(-40, w)
            y = rng.uniform(0, sheet_top)
            length = rng.uniform(14, 30) * (0.6 + cat.intensity)
            a = int(rng.uniform(50, 130) * (0.5 + cat.intensity * 0.6))
            draw.line((x, y, x - length * 0.22, y + length), fill=(*colour, a), width=1)
    elif cat.precip == "snow":
        n = int(40 + cat.intensity * 160)
        colour = palette.snow_colour
        for _ in range(n):
            x = rng.uniform(0, w)
            y = rng.uniform(0, sheet_top)
            r = rng.uniform(1.2, 3.4) * (0.7 + cat.intensity * 0.5)
            a = int(rng.uniform(90, 210))
            draw.ellipse((x - r, y - r, x + r, y + r), fill=(*colour, a))
    canvas.alpha_composite(layer)


def _fog_bands(canvas: Image.Image, rng: random.Random, w: int, h: int, sheet_top: int, palette: Palette) -> None:
    if palette.category.family != "fog":
        return
    layers = Image.new("RGBA", (w, sheet_top), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layers)
    bands = 5
    for i in range(bands):
        y = sheet_top * (0.25 + i * 0.16) + rng.uniform(-10, 10)
        band_h = sheet_top * rng.uniform(0.16, 0.30)
        a = int(255 * (0.20 + palette.category.intensity * 0.22))
        ld.ellipse((-w * 0.2, y - band_h / 2, w * 1.2, y + band_h / 2), fill=(*palette.fog_colour, a))
    layers = layers.filter(ImageFilter.GaussianBlur(sheet_top * 0.05))
    canvas.alpha_composite(layers, (0, 0))


def _lightning(canvas: Image.Image, rng: random.Random, w: int, h: int, sheet_top: int, palette: Palette) -> None:
    if palette.category.family != "storm":
        return
    # Deterministic per-render coin flip so a storm image occasionally shows
    # a restrained flash instead of one appearing on every single render.
    if rng.random() > 0.42:
        return
    cx = rng.uniform(w * 0.2, w * 0.8)
    cy = sheet_top * rng.uniform(0.25, 0.55)
    glow = _radial_glow((w, h), cx, cy, min(w, h) * 0.32, palette.lightning_colour, 70)
    canvas.alpha_composite(glow)
    # one restrained jagged bolt, thin and soft rather than a neon streak
    bolt = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bolt)
    x, y = cx, cy - h * 0.03
    pts = [(x, y)]
    for _ in range(4):
        x += rng.uniform(-18, 18)
        y += rng.uniform(18, 34)
        pts.append((x, y))
    bd.line(pts, fill=(*palette.lightning_colour, 130), width=2)
    bolt = bolt.filter(ImageFilter.GaussianBlur(1.2))
    canvas.alpha_composite(bolt)


def render_sky(w: int, h: int, sheet_top: int, palette: Palette) -> Image.Image:
    """Compose the full sky background for a card of size (w, h).

    ``sheet_top`` is the y-coordinate where the opaque Material bottom
    sheet begins; the sky above it is what stays visible, so cloud decks,
    the horizon glow, and precipitation concentrate in that band.
    """
    rng = random.Random(palette.seed)
    scale = 6
    lw, lh = max(60, w // scale), max(90, h // scale)
    low_sheet_top = max(4, int(sheet_top / scale))

    # The gradient's colour stops (zenith -> horizon) are meant to be fully
    # travelled within the *visible* sky, i.e. by the time we reach where
    # the opaque bottom sheet begins — not by the bottom of the whole
    # canvas (most of which the sheet hides anyway).
    visible_gradient = _lowres_gradient(lw, low_sheet_top, palette.sky_stops)
    low = Image.new("RGB", (lw, lh), palette.sky_stops[-1])
    low.paste(visible_gradient, (0, 0))
    low = low.convert("RGBA")
    glow_radius = max(lw, lh) * 0.9
    glow = _radial_glow(
        (lw, lh), lw * palette.phase.sun_x, low_sheet_top * 0.98,
        glow_radius, palette.horizon_glow,
        int(150 * (0.25 + palette.phase.golden)),
    )
    low.alpha_composite(glow)

    _paint_clouds(low, rng, palette, band_top=0.18, band_bottom=0.9)

    big = low.resize((w, h), Image.BICUBIC)
    big = big.filter(ImageFilter.GaussianBlur(max(3, w // 95)))

    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 255))
    canvas.alpha_composite(big)

    _stars(canvas, rng, w, h, sheet_top, palette)
    _sun_moon(canvas, palette, w, h, sheet_top)
    _fog_bands(canvas, rng, w, h, sheet_top, palette)
    _precipitation(canvas, rng, w, h, sheet_top, palette)
    _lightning(canvas, rng, w, h, sheet_top, palette)

    # A gentle scrim across the whole hero column so the location/clock and
    # the huge temperature always have something to grip, independent of
    # how bright a cloud or how busy a starfield happens to land behind
    # them — without this, a large cloud (or a bright patch of sky) can
    # land directly behind the digits and blow out the contrast.
    scrim = Image.new("L", (1, h), 0)
    spx = scrim.load()
    peak = 58
    for y in range(h):
        if y > sheet_top:
            spx[0, y] = 0
            continue
        t = y / max(1, sheet_top)
        ramp_in = min(1.0, t / 0.18)
        ramp_out = min(1.0, max(0.0, (1.0 - t) / 0.10))
        spx[0, y] = int(peak * ramp_in * (0.35 + 0.65 * ramp_out))
    scrim_rgba = Image.new("RGBA", (1, h), (*palette.hero_scrim, 0))
    scrim_rgba.putalpha(scrim)
    canvas.alpha_composite(scrim_rgba.resize((w, h)))

    return canvas
