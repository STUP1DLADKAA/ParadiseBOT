"""Landscape 16:9 weather card rendered with the existing Pillow stack.

The card is a native image composition (not a stretched portrait layout):
current conditions occupy the upper two-thirds, with a compact hourly strip
at the bottom. The existing time/condition-aware sky palette and custom
weather glyphs are reused; every displayed measurement comes from the
provider-shaped ``WeatherData`` model.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PIL import Image, ImageDraw, ImageFont

from functions.dictionary.fonts import FontLibrary

from .icons import draw_metric_icon, draw_weather_icon
from .models import WeatherData
from .palette import Palette, build_palette, category_for_code

WIDTH = 1200
HEIGHT = 675
PAD = 56
BOTTOM_TOP = 515

_FONTS = FontLibrary()


def _font(role: str, size: int) -> ImageFont.FreeTypeFont:
    font_set = _FONTS.get(role, size)
    return font_set.primary or ImageFont.load_default()


def _draw_text(draw: ImageDraw.ImageDraw, xy, text: str, font, fill, *, anchor: str = "la") -> None:
    draw.text(xy, text, font=font, fill=fill, anchor=anchor)


def _fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    role: str,
    size: int,
    max_width: float,
    *,
    minimum: int = 16,
) -> tuple[str, ImageFont.FreeTypeFont]:
    font_size = size
    font = _font(role, font_size)
    while font_size > minimum and draw.textlength(text, font=font) > max_width:
        font_size -= 2
        font = _font(role, font_size)
    if draw.textlength(text, font=font) <= max_width:
        return text, font
    ellipsis = "…"
    candidate = text
    while candidate and draw.textlength(candidate + ellipsis, font=font) > max_width:
        candidate = candidate[:-1]
    return (candidate.rstrip() + ellipsis if candidate else ellipsis), font


def _location_zone(weather: WeatherData):
    try:
        return ZoneInfo(weather.location.timezone or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def _local_datetime(weather: WeatherData) -> datetime:
    zone = _location_zone(weather)
    if weather.local_time:
        try:
            value = datetime.fromisoformat(weather.local_time)
            if value.tzinfo is None:
                return value.replace(tzinfo=zone)
            return value.astimezone(zone)
        except (TypeError, ValueError):
            pass
    return datetime.now(zone)


def _as_local_datetime(value, zone) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone)


def _upcoming_hours(weather: WeatherData, *, count: int = 6) -> list[tuple[datetime, dict]]:
    zone = _location_zone(weather)
    local_now = _local_datetime(weather)
    now_utc = local_now.astimezone(timezone.utc)
    rows = []
    for row in weather.hourly_forecast or []:
        if not isinstance(row, dict):
            continue
        forecast_at = _as_local_datetime(row.get("time"), zone)
        if forecast_at is None or forecast_at.astimezone(timezone.utc) <= now_utc:
            continue
        # Without an actual temperature there is no useful hourly item to show.
        if row.get("temperature_2m") is None:
            continue
        rows.append((forecast_at, row))
    rows.sort(key=lambda entry: entry[0].astimezone(timezone.utc))
    return rows[:count]


def _weather_family(code: int | None) -> str:
    key = category_for_code(code if code is not None else -1).key
    return {
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
    }.get(key, "cloud")


def _is_day(moment: datetime, weather: WeatherData) -> bool:
    def parse_minutes(value: str | None):
        if not value:
            return None
        try:
            hour, minute = str(value).split(":")[:2]
            return int(hour) * 60 + int(minute)
        except (ValueError, TypeError):
            return None

    current = moment.hour * 60 + moment.minute
    sunrise = parse_minutes(weather.sunrise)
    sunset = parse_minutes(weather.sunset)
    if sunrise is None or sunset is None:
        return 6 <= moment.hour < 19
    return sunrise <= current <= sunset


def _metric_rows(weather: WeatherData) -> list[tuple[str, str, str]]:
    metrics: list[tuple[str, str, str]] = []
    if weather.humidity is not None:
        metrics.append(("humidity", "Humidity", f"{weather.humidity}%"))
    if weather.wind_speed_kmh is not None:
        wind_value = f"{weather.wind_speed_kmh:.0f} km/h"
        if weather.wind_direction:
            wind_value += f" {weather.wind_direction}"
        metrics.append(("wind", "Wind", wind_value))
    if weather.precipitation_probability is not None:
        metrics.append(("drop", "Precipitation", f"{weather.precipitation_probability}%"))
    elif weather.precipitation_mm is not None:
        metrics.append(("drop", "Precipitation", f"{weather.precipitation_mm:g} mm"))
    if weather.uv_index is not None:
        metrics.append(("uv", "UV index", f"{weather.uv_index:g}"))
    if not metrics and weather.pressure_hpa is not None:
        metrics.append(("pressure", "Pressure", f"{weather.pressure_hpa:.0f} hPa"))
    return metrics[:4]


def _paint_current_metrics(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    metrics: list[tuple[str, str, str]],
    palette: Palette,
) -> None:
    if not metrics:
        return
    col_x = (690, 925)
    row_y = (354, 432)
    tile_w, tile_h = 215, 66
    for index, (icon_name, label, value) in enumerate(metrics):
        x = col_x[index % 2]
        y = row_y[index // 2]
        draw.rounded_rectangle(
            (x, y, x + tile_w, y + tile_h),
            radius=18,
            fill=(*palette.surface_container, 255),
            outline=(*palette.outline_variant, 255),
            width=1,
        )
        icon = draw_metric_icon(30, icon_name, palette.primary)
        canvas.alpha_composite(icon, (x + 15, y + 18))
        _draw_text(draw, (x + 56, y + 13), label.upper(), _font("medium", 14), palette.on_surface_variant)
        _draw_text(draw, (x + 56, y + 34), value, _font("semibold", 23), palette.on_surface)


def _format_hour(moment: datetime) -> str:
    hour = moment.hour % 12 or 12
    return f"{hour} {'AM' if moment.hour < 12 else 'PM'}"


def _paint_hourly_strip(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    weather: WeatherData,
    palette: Palette,
) -> None:
    top = BOTTOM_TOP
    draw.rounded_rectangle(
        (28, top, WIDTH - 28, HEIGHT - 12),
        radius=28,
        fill=(*palette.surface, 255),
        outline=(*palette.outline_variant, 255),
        width=1,
    )
    _draw_text(
        draw,
        (PAD, top + 19),
        "NEXT HOURS",
        _font("semibold", 16),
        palette.on_surface_variant,
    )
    hours = _upcoming_hours(weather)
    if not hours:
        _draw_text(
            draw,
            (PAD, top + 67),
            "Hourly forecast unavailable",
            _font("medium", 22),
            palette.on_surface_variant,
        )
        return

    count = len(hours)
    strip_left, strip_right = PAD, WIDTH - PAD
    item_width = (strip_right - strip_left) / count
    for index, (moment, row) in enumerate(hours):
        x_center = strip_left + item_width * (index + 0.5)
        if index:
            x_line = int(strip_left + item_width * index)
            draw.line(
                (x_line, top + 52, x_line, HEIGHT - 40),
                fill=(*palette.outline_variant, 170),
                width=1,
            )
        _draw_text(
            draw,
            (x_center, top + 57),
            _format_hour(moment),
            _font("medium", 16),
            palette.on_surface_variant,
            anchor="ma",
        )
        try:
            code = int(row["weather_code"]) if row.get("weather_code") is not None else weather.weather_code
        except (TypeError, ValueError):
            code = weather.weather_code
        icon = draw_weather_icon(
            34,
            _weather_family(code),
            _is_day(moment, weather),
            palette.primary,
            accent=palette.tertiary,
            night_stars=not _is_day(moment, weather),
        )
        canvas.alpha_composite(icon, (int(x_center - 17), top + 72))
        try:
            temperature = f"{float(row['temperature_2m']):.0f}°C"
        except (TypeError, ValueError, KeyError):
            temperature = ""
        if temperature:
            _draw_text(
                draw,
                (x_center, top + 105),
                temperature,
                _font("semibold", 19),
                palette.on_surface,
                anchor="ma",
            )


def render_weather_card(weather: WeatherData) -> bytes:
    """Render the deliverable PNG at exactly 1200×675 (16:9)."""
    palette = build_palette(weather)
    from .sky import render_sky

    canvas = render_sky(WIDTH, HEIGHT, BOTTOM_TOP, palette).convert("RGBA")
    draw = ImageDraw.Draw(canvas, "RGBA")
    local_now = _local_datetime(weather)
    hero_text = (*palette.hero_on, 255)
    hero_muted = (*palette.hero_on_muted, 255)

    # A compact locator/date row anchors the card without competing with the temperature.
    draw.rounded_rectangle((PAD, 38, PAD + 13, 51), radius=6, fill=(*palette.primary, 255))
    _draw_text(draw, (PAD + 25, 34), "PARADISE WEATHER", _font("semibold", 16), hero_muted)
    location_title, location_font = _fit_text(
        draw, weather.location.name, "display", 38, 555, minimum=24
    )
    _draw_text(draw, (PAD, 67), location_title, location_font, hero_text)
    region = ", ".join(
        part for part in (weather.location.admin1, weather.location.country)
        if part and part.casefold() != weather.location.name.casefold()
    )
    if region:
        region_text, region_font = _fit_text(draw, region, "medium", 18, 555, minimum=13)
        _draw_text(draw, (PAD, 112), region_text, region_font, hero_muted)

    date_text = f"{local_now.strftime('%A')} · {local_now.day} {local_now.strftime('%b')}"
    time_text = local_now.strftime("%I:%M %p").lstrip("0")
    _draw_text(draw, (WIDTH - PAD, 39), time_text, _font("semibold", 22), hero_text, anchor="ra")
    _draw_text(draw, (WIDTH - PAD, 70), date_text, _font("medium", 16), hero_muted, anchor="ra")

    if weather.temp_c is None:
        temperature = "—"
    else:
        temperature = f"{weather.temp_c:.0f}°C"
    temperature, temperature_font = _fit_text(
        draw, temperature, "display", 132, 565, minimum=76
    )
    _draw_text(draw, (PAD - 5, 154), temperature, temperature_font, hero_text)

    condition = (weather.condition or "Conditions unavailable").strip()
    condition, condition_font = _fit_text(
        draw, condition, "semibold", 29, 560, minimum=19
    )
    _draw_text(draw, (PAD, 314), condition, condition_font, hero_text)

    detail_y = 363
    if weather.feels_like_c is not None:
        _draw_text(
            draw,
            (PAD, detail_y),
            f"Feels like {weather.feels_like_c:.0f}°C",
            _font("medium", 21),
            hero_muted,
        )

    range_parts = []
    if weather.temp_max_c is not None:
        range_parts.append(f"HIGH {weather.temp_max_c:.0f}°C")
    if weather.temp_min_c is not None:
        range_parts.append(f"LOW {weather.temp_min_c:.0f}°C")
    if range_parts:
        chip_text = "   ·   ".join(range_parts)
        chip_font = _font("semibold", 17)
        chip_w = min(555, draw.textlength(chip_text, font=chip_font) + 36)
        draw.rounded_rectangle(
            (PAD, 410, PAD + chip_w, 458),
            radius=22,
            fill=(*palette.primary_container, 255),
        )
        _draw_text(
            draw,
            (PAD + 18, 424),
            _fit_text(draw, chip_text, "semibold", 17, chip_w - 36, minimum=14)[0],
            chip_font,
            palette.on_primary_container,
        )

    # Large, condition-specific vector weather glyph as the visual hero.
    family = _weather_family(weather.weather_code)
    icon = draw_weather_icon(
        255,
        family,
        _is_day(local_now, weather),
        palette.hero_on,
        accent=palette.tertiary,
        night_stars=palette.phase.night_depth > 0.5,
    )
    canvas.alpha_composite(icon, (810, 91))
    _paint_current_metrics(canvas, draw, _metric_rows(weather), palette)
    _paint_hourly_strip(canvas, draw, weather, palette)

    final = canvas.convert("RGB")
    if final.size != (1200, 675):  # defensive invariant for Discord delivery
        final = final.resize((1200, 675), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    final.save(output, format="PNG", optimize=True)
    return output.getvalue()


__all__ = ["HEIGHT", "WIDTH", "render_weather_card"]
