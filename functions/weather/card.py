"""Material Expressive weather card renderer.

The image is a real UI composed from primitives — no AI imagery, no stock
photography. Layout follows the two-pass discipline used by the dictionary
card: a measuring pass decides exactly how tall each optional section is
(so a location with no AQI or no hourly data doesn't leave a gap), then a
single painting pass draws the sky, the hero, and the Material "bottom
sheet" of tonal surfaces on top of it.

Composition mirrors first-party Android weather apps (Pixel Weather /
Samsung Weather): a realistic, time-and-condition-aware sky fills the top
of the frame with the location, clock, and huge temperature sitting
directly on it; a rounded Material sheet then rises up from the bottom
holding the metrics grid, hourly strip, daily list, and sunrise/sunset
card as tonal surfaces of the same dynamically generated palette.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from ..dictionary.fonts import FontLibrary
from .icons import draw_gauge_icon, draw_metric_icon, draw_weather_icon
from .models import WeatherData
from .palette import Palette, build_palette, mix
from .provider import WMO_CODES

W = 1080
PAD = 56

_FONTS = FontLibrary()


def _font(role: str, size: int) -> ImageFont.FreeTypeFont:
    fs = _FONTS.get(role, size)
    font = fs.primary
    if font is None:  # pragma: no cover - only on a font-less machine
        font = ImageFont.load_default()
    return font


_ICON_FAMILY_BY_KEY = {
    "clear": "clear",
    "partly_cloudy": "cloud",
    "cloudy": "overcast",
    "fog": "fog",
    "drizzle": "rain",
    "rain": "rain",
    "heavy_rain": "rain",
    "thunderstorm": "storm",
    "snow": "snow",
    "heavy_snow": "snow",
}


def _family_for(code: int) -> str:
    from .palette import category_for_code
    return _ICON_FAMILY_BY_KEY.get(category_for_code(code).key, "cloud")


def _is_day(hour: int, sunrise_min: int | None, sunset_min: int | None) -> bool:
    if sunrise_min is None or sunset_min is None:
        return 6 <= hour < 19
    minutes = hour * 60
    return sunrise_min <= minutes <= sunset_min


def _round1(value: float | None) -> str:
    return "—" if value is None else f"{round(value)}°"


def _uv_label(uv: float) -> str:
    if uv < 3:
        return "Low"
    if uv < 6:
        return "Moderate"
    if uv < 8:
        return "High"
    if uv < 11:
        return "Very high"
    return "Extreme"


def _aqi_label(aqi: float) -> str:
    if aqi <= 50:
        return "Good"
    if aqi <= 100:
        return "Moderate"
    if aqi <= 150:
        return "Sensitive"
    if aqi <= 200:
        return "Unhealthy"
    if aqi <= 300:
        return "Very unhealthy"
    return "Hazardous"


def _text_w(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> float:
    return draw.textlength(text, font=font)


def _ellipsize(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: float) -> str:
    """Truncate `text` with a trailing ellipsis so it fits within `max_w`,
    for the rare long place name / long condition string that would
    otherwise run off the card or collide with a neighbouring element."""
    if max_w <= 0 or _text_w(draw, text, font) <= max_w:
        return text
    ell = "\u2026"
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _text_w(draw, text[:mid].rstrip() + ell, font) <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return (text[:lo].rstrip() + ell) if lo > 0 else ell


def _draw_text(draw, xy, text, font, fill, anchor=None, tracking: float = 0.0):
    if tracking <= 0:
        draw.text(xy, text, font=font, fill=fill, anchor=anchor)
        return
    # Simple manual letter-spacing for the rare all-caps labels that need it.
    x, y = xy
    total = sum(draw.textlength(ch, font=font) + tracking for ch in text) - tracking
    if anchor and "m" in anchor[0:1]:
        x -= total / 2
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill, anchor=(None if not anchor else "l" + anchor[1]))
        x += draw.textlength(ch, font=font) + tracking


def _rrect(draw, box, radius, **kwargs):
    draw.rounded_rectangle(box, radius=radius, **kwargs)


@dataclass
class _Plan:
    """Everything the measuring pass decides before a pixel is drawn."""

    hero_h: int
    sheet_top: int
    metrics: list[tuple[str, str, str | None, str, float | None]]  # icon, label, secondary, value, gauge_fraction
    metrics_rows: int
    hourly: list[dict]
    daily: list[dict]
    show_sun_card: bool
    total_h: int


def _plan(weather: WeatherData, palette: Palette) -> _Plan:
    hero_h = 700

    metrics_all: list[tuple[str, str, str | None, str, float | None]] = []
    if weather.humidity is not None:
        metrics_all.append(("humidity", "Humidity", None, f"{weather.humidity}%", None))
    if weather.wind_speed_kmh is not None:
        metrics_all.append(("wind", "Wind", weather.wind_direction or None, f"{round(weather.wind_speed_kmh)} km/h", None))
    if weather.uv_index is not None:
        metrics_all.append(("uv", "UV index", _uv_label(weather.uv_index), f"{weather.uv_index:g}", min(1.0, weather.uv_index / 11.0)))
    if weather.pressure_hpa is not None:
        metrics_all.append(("pressure", "Pressure", None, f"{round(weather.pressure_hpa)} hPa", None))
    if weather.visibility_km is not None:
        metrics_all.append(("visibility", "Visibility", None, f"{weather.visibility_km:g} km", None))
    aqi = weather.air_quality.get("aqi") if weather.air_quality else None
    if aqi is not None:
        metrics_all.append(("aqi", "Air quality", _aqi_label(float(aqi)), str(int(aqi)), min(1.0, float(aqi) / 300.0)))
    if weather.dew_point_c is not None:
        metrics_all.append(("drop", "Dew point", None, f"{round(weather.dew_point_c)}°", None))
    if weather.wind_gusts_kmh is not None:
        metrics_all.append(("gust", "Wind gusts", None, f"{round(weather.wind_gusts_kmh)} km/h", None))
    metrics = metrics_all[:9]
    metrics_rows = math.ceil(len(metrics) / 3) if metrics else 0

    hourly = weather.hourly_forecast[:8]
    daily = weather.daily_forecast[:7]
    show_sun_card = bool(weather.sunrise and weather.sunset)

    sheet_h = 48  # top handle + breathing room
    if metrics:
        sheet_h += metrics_rows * 158 + 28
    if hourly:
        sheet_h += 56 + 190 + 28
    if daily:
        sheet_h += 56 + len(daily) * 78 + 20
    if show_sun_card:
        sheet_h += 236 + 28
    sheet_h += 44  # bottom padding

    total_h = hero_h + sheet_h
    return _Plan(hero_h, hero_h, metrics, metrics_rows, hourly, daily, show_sun_card, total_h)


# ---------------------------------------------------------------------------
# Painting
# ---------------------------------------------------------------------------


def _focus_scrim(canvas: Image.Image, palette: Palette, hero_h: int) -> None:
    """A soft, feathered darken/lighten behind the icon+temperature block.

    The sky behind it is deterministic but not layout-aware — an
    unusually bright patch of sky or a cloud edge can still land exactly
    behind the digits. This gives the numbers a guaranteed-legible field
    without ever reading as a hard panel, by being drawn on its own layer
    and properly alpha-composited (never as an opaque ``ImageDraw`` fill).
    """
    w, h = canvas.size
    layer = Image.new("L", (w, h), 0)
    ld = ImageDraw.Draw(layer)
    cx, cy = w / 2, hero_h * 0.46
    rx, ry = w * 0.62, hero_h * 0.50
    ld.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=118)
    layer = layer.filter(ImageFilter.GaussianBlur(hero_h * 0.14))
    tint = Image.new("RGBA", (w, h), (*palette.hero_scrim, 0))
    tint.putalpha(layer)
    canvas.alpha_composite(tint)


def _paint_hero(canvas: Image.Image, draw: ImageDraw.ImageDraw, weather: WeatherData, palette: Palette, hero_h: int) -> None:
    _focus_scrim(canvas, palette, hero_h)
    on = palette.hero_on
    on_muted = palette.hero_on_muted
    loc = weather.location

    # Status row: location (with a small pin) on the left, clock on the right.
    name_font = _font("semibold", 34)
    sub_font = _font("medium", 20)
    time_font = _font("semibold", 34)
    date_font = _font("medium", 20)

    pin_cx, pin_cy = PAD + 10, 78
    draw.ellipse((pin_cx - 6, pin_cy - 6, pin_cx + 6, pin_cy + 6), outline=on, width=4)
    draw.line((pin_cx, pin_cy + 6, pin_cx, pin_cy + 16), fill=on, width=4)

    now = _local_dt(weather)
    clock = now.strftime("%I:%M %p").lstrip("0")
    weekday = now.strftime("%A, %d %b")

    # The location block (left) and the clock block (right) share one row;
    # long place names or country names are ellipsized so the two never
    # collide, however extreme the source data is.
    name_x0 = PAD + 28
    gutter = 24
    time_w = _text_w(draw, clock, time_font)
    date_w = _text_w(draw, weekday, date_font)
    name_max_w = max(60.0, (W - PAD) - time_w - gutter - name_x0)
    sub_max_w = max(60.0, (W - PAD) - date_w - gutter - name_x0)

    name_shown = _ellipsize(draw, loc.name, name_font, name_max_w)
    _draw_text(draw, (name_x0, 60), name_shown, name_font, on)
    region = ", ".join(p for p in (loc.admin1, loc.country) if p and p != loc.name)
    if region:
        region_shown = _ellipsize(draw, region, sub_font, sub_max_w)
        _draw_text(draw, (name_x0, 102), region_shown, sub_font, on_muted)

    _draw_text(draw, (W - PAD, 60), clock, time_font, on, anchor="ra")
    _draw_text(draw, (W - PAD, 102), weekday, date_font, on_muted, anchor="ra")

    # Icon + temperature, stacked and centred.
    family = _family_for(weather.weather_code)
    minutes = now.hour * 60 + now.minute
    sunrise_min = _hhmm_to_min(weather.sunrise)
    sunset_min = _hhmm_to_min(weather.sunset)
    is_day = _is_day(now.hour, sunrise_min, sunset_min)

    icon_size = 184
    icon = draw_weather_icon(icon_size, family, is_day, on, accent=palette.tertiary)
    icon_y = 168
    canvas.alpha_composite(icon, (W // 2 - icon_size // 2, icon_y))

    temp_font = _font("display", 176)
    temp_text = f"{round(weather.temp_c)}°"
    ty = icon_y + icon_size + 4
    _draw_text(draw, (W // 2, ty), temp_text, temp_font, on, anchor="ma")

    max_cond_w = W - PAD * 2 - 40
    cond_size = 40
    cond_font = _font("semibold", cond_size)
    while _text_w(draw, weather.condition, cond_font) > max_cond_w and cond_size > 26:
        cond_size -= 2
        cond_font = _font("semibold", cond_size)
    cond_y = ty + 158
    _draw_text(draw, (W // 2, cond_y), weather.condition, cond_font, on, anchor="ma")

    feels_font = _font("medium", 24)
    feels_y = cond_y + 54
    _draw_text(draw, (W // 2, feels_y), f"Feels like {round(weather.feels_like_c)}°", feels_font, on_muted, anchor="ma")

    # High/low: a solid Material tonal chip (never a fake-translucent
    # overlay — PIL's ImageDraw does not alpha-blend onto existing pixels,
    # it replaces them, so any "subtle glass" fill here would just paint a
    # flat block of that colour once the image loses its alpha channel).
    pill_font = _font("semibold", 22)
    hi_lo = f"H:{round(weather.temp_max_c)}°   L:{round(weather.temp_min_c)}°"
    pw = _text_w(draw, hi_lo, pill_font)
    pill_w, pill_h = pw + 56, 52
    px0 = W // 2 - pill_w / 2
    py0 = feels_y + 46
    _rrect(draw, (px0, py0, px0 + pill_w, py0 + pill_h), pill_h / 2, fill=(*palette.primary_container, 255))
    _draw_text(draw, (W // 2, py0 + pill_h / 2 - 2), hi_lo, pill_font, palette.on_primary_container, anchor="mm")


def _local_dt(weather: WeatherData) -> datetime:
    if weather.local_time:
        try:
            return datetime.fromisoformat(weather.local_time)
        except ValueError:
            pass
    from zoneinfo import ZoneInfo
    try:
        return datetime.now(ZoneInfo(weather.location.timezone or "UTC"))
    except Exception:
        return datetime.utcnow()


def _hhmm_to_min(hhmm: str | None) -> int | None:
    if not hhmm:
        return None
    try:
        h, m = hhmm.split(":")[:2]
        return int(h) * 60 + int(m)
    except (ValueError, IndexError):
        return None


def _sheet_panel(draw: ImageDraw.ImageDraw, sheet_top: int, total_h: int, palette: Palette) -> None:
    radius = 40
    _rrect(draw, (0, sheet_top, W, total_h + radius), radius, fill=(*palette.surface, 255))
    handle_w, handle_h = 52, 5
    hx = W / 2 - handle_w / 2
    hy = sheet_top + 16
    _rrect(draw, (hx, hy, hx + handle_w, hy + handle_h), handle_h / 2, fill=(*palette.outline_variant, 255))


def _section_label(draw, x, y, text, palette: Palette) -> None:
    font = _font("semibold", 22)
    _draw_text(draw, (x, y), text.upper(), font, palette.primary, tracking=1.6)


def _metric_tile(canvas, draw, x, y, w, h, icon_name, label, secondary, value, palette: Palette, gauge=None, emphasize=False) -> None:
    fill = palette.surface_container_high if not emphasize else palette.primary_container
    on_col = palette.on_surface if not emphasize else palette.on_primary_container
    on_muted = palette.on_surface_variant if not emphasize else mix(palette.on_primary_container, fill, 0.25)
    _rrect(draw, (x, y, x + w, y + h), 28, fill=(*fill, 255))

    icon_col = palette.primary if not emphasize else palette.on_primary_container
    if gauge is not None:
        icon_sz = 46
        track = mix(fill, on_col, 0.16)
        gauge_col = palette.tertiary if not emphasize else palette.on_primary_container
        icon = draw_gauge_icon(icon_sz, icon_name, gauge, gauge_col, track)
    else:
        icon_sz = 40
        icon = draw_metric_icon(icon_sz, icon_name, icon_col)
    canvas.alpha_composite(icon, (int(x + 20), int(y + 16)))

    label_font = _font("medium", 17)
    value_font = _font("semibold", 30)
    _draw_text(draw, (x + 20, y + 70), label.upper(), label_font, on_muted, tracking=0.6)
    baseline = y + 118
    _draw_text(draw, (x + 20, baseline), value, value_font, on_col, anchor="lb")
    if secondary:
        sec_font = _font("medium", 16)
        value_w = _text_w(draw, value, value_font)
        _draw_text(draw, (x + 20 + value_w + 10, baseline - 3), secondary, sec_font, on_muted, anchor="lb")


def _paint_metrics(canvas, draw, plan: _Plan, cursor: int, palette: Palette) -> int:
    if not plan.metrics:
        return cursor
    cols = 3
    gutter = 16
    full_tile_w = (W - PAD * 2 - gutter * (cols - 1)) / cols
    tile_h = 142
    n = len(plan.metrics)
    for row in range(plan.metrics_rows):
        row_items = plan.metrics[row * cols:(row + 1) * cols]
        # A short trailing row stretches to fill the full width evenly,
        # rather than leaving a dangling gap on the right.
        row_w = (W - PAD * 2 - gutter * (len(row_items) - 1)) / len(row_items) if len(row_items) < cols else full_tile_w
        y = cursor + row * (tile_h + 16)
        for col, (icon_name, label, secondary, value, gauge) in enumerate(row_items):
            x = PAD + col * (row_w + gutter)
            _metric_tile(canvas, draw, x, y, row_w, tile_h, icon_name, label, secondary, value, palette, gauge=gauge)
    return cursor + plan.metrics_rows * (tile_h + 16) - 16 + 28


def _paint_hourly(canvas, draw, weather: WeatherData, plan: _Plan, cursor: int, palette: Palette, sunrise_min, sunset_min) -> int:
    if not plan.hourly:
        return cursor
    _section_label(draw, PAD, cursor, "Hourly forecast", palette)
    top = cursor + 44
    n = len(plan.hourly)
    gutter = 14
    tile_w = (W - PAD * 2 - gutter * (n - 1)) / n
    tile_h = 176
    for i, row in enumerate(plan.hourly):
        x = PAD + i * (tile_w + gutter)
        emphasize = i == 0
        fill = palette.primary_container if emphasize else palette.surface_container
        on_col = palette.on_primary_container if emphasize else palette.on_surface
        on_muted = mix(on_col, fill, 0.3) if emphasize else palette.on_surface_variant
        radius = 30 if emphasize else 26
        y0 = top - (10 if emphasize else 0)
        y1 = top + tile_h + (6 if emphasize else 0)
        _rrect(draw, (x, y0, x + tile_w, y1), radius, fill=(*fill, 255))

        stamp = str(row.get("time") or "")
        try:
            hour = int(stamp.split("T")[-1][:2])
        except ValueError:
            hour = 12
        label = "Now" if emphasize else datetime.strptime(f"{hour:02d}", "%H").strftime("%I %p").lstrip("0")
        label_font = _font("semibold" if emphasize else "medium", 19)
        _draw_text(draw, (x + tile_w / 2, y0 + 20), label, label_font, on_col, anchor="ma")

        code = int(row.get("weather_code") or 0)
        icon_day = _is_day(hour, sunrise_min, sunset_min)
        icon = draw_weather_icon(52, _family_for(code), icon_day, on_col, accent=on_muted)
        canvas.alpha_composite(icon, (int(x + tile_w / 2 - 26), int(y0 + 54)))

        temp = row.get("temperature_2m")
        temp_font = _font("semibold", 25)
        _draw_text(draw, (x + tile_w / 2, y0 + 116), _round1(temp), temp_font, on_col, anchor="ma")

        prob = row.get("precipitation_probability")
        if prob is not None:
            prob_font = _font("medium", 16)
            _draw_text(draw, (x + tile_w / 2, y0 + 150), f"{round(prob)}%", prob_font, on_muted, anchor="ma")
    return top + tile_h + 22 + 28


_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _paint_daily(canvas, draw, weather: WeatherData, plan: _Plan, cursor: int, palette: Palette, sunrise_min, sunset_min) -> int:
    if not plan.daily:
        return cursor
    n_days = len(plan.daily)
    label = "Today's outlook" if n_days <= 1 else f"{n_days}-day outlook"
    _section_label(draw, PAD, cursor, label, palette)
    y = cursor + 46
    row_h = 78
    day_font = _font("semibold", 22)
    hi_font = _font("semibold", 24)
    lo_font = _font("medium", 22)
    prob_font = _font("medium", 17)
    today = _local_dt(weather).date()
    for i, row in enumerate(plan.daily):
        try:
            d = date.fromisoformat(str(row.get("date")))
            label = "Today" if d == today else ("Tomorrow" if d == today + timedelta(days=1) else _WEEKDAYS[d.weekday()][:3])
        except ValueError:
            label = str(row.get("date") or "")[5:]

        line_y = y + i * row_h
        _draw_text(draw, (PAD, line_y + row_h / 2), label, day_font, palette.on_surface, anchor="lm")

        code = int(row.get("code") or 0)
        icon = draw_weather_icon(44, _family_for(code), True, palette.primary, accent=palette.tertiary)
        canvas.alpha_composite(icon, (PAD + 148, int(line_y + row_h / 2 - 22)))

        prob = row.get("precipitation_probability")
        if prob is not None and prob >= 5:
            drop = draw_metric_icon(20, "drop", palette.secondary)
            canvas.alpha_composite(drop, (PAD + 216, int(line_y + row_h / 2 - 10)))
            _draw_text(draw, (PAD + 240, line_y + row_h / 2), f"{round(prob)}%", prob_font, palette.on_surface_variant, anchor="lm")

        hi, lo = row.get("high"), row.get("low")
        hi_txt, lo_txt = _round1(hi), _round1(lo)
        lo_w = _text_w(draw, lo_txt, lo_font)
        _draw_text(draw, (W - PAD, line_y + row_h / 2), lo_txt, lo_font, palette.on_surface_variant, anchor="rm")
        _draw_text(draw, (W - PAD - lo_w - 20, line_y + row_h / 2), hi_txt, hi_font, palette.on_surface, anchor="rm")

        if i < len(plan.daily) - 1:
            divider = mix(palette.outline_variant, palette.surface, 0.35)
            draw.line((PAD, line_y + row_h - 1, W - PAD, line_y + row_h - 1), fill=divider, width=1)
    return y + len(plan.daily) * row_h + 20 + 28


def _paint_sun_card(canvas, draw, weather: WeatherData, cursor: int, palette: Palette) -> int:
    h = 236
    _rrect(draw, (PAD, cursor, W - PAD, cursor + h), 32, fill=(*palette.surface_container_high, 255))

    sunrise_min = _hhmm_to_min(weather.sunrise)
    sunset_min = _hhmm_to_min(weather.sunset)
    now = _local_dt(weather)
    minutes = now.hour * 60 + now.minute

    label_font = _font("medium", 18)
    time_font = _font("semibold", 32)

    icon = draw_metric_icon(34, "sunrise", palette.tertiary)
    canvas.alpha_composite(icon, (PAD + 32, cursor + 30))
    _draw_text(draw, (PAD + 80, cursor + 32), "SUNRISE", label_font, palette.on_surface_variant, tracking=1.2)
    _draw_text(draw, (PAD + 80, cursor + 56), weather.sunrise or "—", time_font, palette.on_surface)

    icon2 = draw_metric_icon(34, "sunset", palette.secondary)
    sx = W - PAD - 220
    canvas.alpha_composite(icon2, (sx, cursor + 30))
    _draw_text(draw, (sx + 48, cursor + 32), "SUNSET", label_font, palette.on_surface_variant, tracking=1.2)
    _draw_text(draw, (sx + 48, cursor + 56), weather.sunset or "—", time_font, palette.on_surface)

    # Minimal functional arc: sunrise -> sunset, with a marker for "now".
    arc_x0, arc_x1 = PAD + 40, W - PAD - 40
    arc_y = cursor + 168
    arc_w = arc_x1 - arc_x0
    draw.line((arc_x0, arc_y, arc_x1, arc_y), fill=(*palette.outline_variant, 255), width=4)
    if sunrise_min is not None and sunset_min is not None and sunset_min > sunrise_min:
        span = sunset_min - sunrise_min
        t = max(0.0, min(1.0, (minutes - sunrise_min) / span))
        if sunrise_min <= minutes <= sunset_min:
            arc_h = 46
            cx = arc_x0 + t * arc_w
            cy = arc_y - math.sin(t * math.pi) * arc_h
            steps = 28
            pts = []
            for s in range(steps + 1):
                st = s / steps
                px = arc_x0 + st * arc_w
                py = arc_y - math.sin(st * math.pi) * arc_h
                pts.append((px, py))
            draw.line(pts, fill=(*palette.primary, 255), width=4, joint="curve")
            r = 9
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(*palette.primary, 255))
    return cursor + h + 28


def render_weather_card(weather: WeatherData) -> bytes:
    """Render a Material Expressive weather card as PNG bytes."""
    palette = build_palette(weather)
    plan = _plan(weather, palette)

    from .sky import render_sky
    sky = render_sky(W, plan.total_h, plan.sheet_top, palette)
    canvas = sky.convert("RGBA")
    draw = ImageDraw.Draw(canvas, "RGBA")

    _paint_hero(canvas, draw, weather, palette, plan.hero_h)

    _sheet_panel(draw, plan.sheet_top, plan.total_h, palette)

    sunrise_min = _hhmm_to_min(weather.sunrise)
    sunset_min = _hhmm_to_min(weather.sunset)

    cursor = plan.sheet_top + 44
    cursor = _paint_metrics(canvas, draw, plan, cursor, palette)
    cursor = _paint_hourly(canvas, draw, weather, plan, cursor, palette, sunrise_min, sunset_min)
    cursor = _paint_daily(canvas, draw, weather, plan, cursor, palette, sunrise_min, sunset_min)
    if plan.show_sun_card:
        cursor = _paint_sun_card(canvas, draw, weather, cursor, palette)

    final = canvas.crop((0, 0, W, plan.total_h)).convert("RGB")
    out = io.BytesIO()
    final.save(out, "PNG", optimize=True)
    return out.getvalue()
