"""Weather-and-time state -> a Material Expressive tonal colour system.

Two independent axes decide everything the renderer draws:

``Phase``
    Where the sun currently sits relative to sunrise/sunset — a continuous
    ``0..1`` daylight signal plus a "golden" (dawn/dusk) signal, rather than
    a hard switch between "day" and "night" presets. Interpolating these
    two numbers is what makes a sky slide smoothly from noon into sunset
    into night instead of snapping between unrelated images.

``Category``
    What the sky is doing — clear, cloudy, rain, snow, fog, storm — with a
    continuous intensity/gloom so "light rain" and "violent thunderstorm"
    read differently even though both are "rain-family".

:func:`build_palette` combines both into a single :class:`Palette` — a
small Material-style token set (primary/secondary/tertiary, surface tiers,
outlines) plus sky-only tokens (gradient stops, cloud tone, star/rain/snow
colour) so the background and the UI are always drawn from the same
tonal family.
"""

from __future__ import annotations

import colorsys
import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from .models import WeatherData

Rgb = tuple[int, int, int]


# ---------------------------------------------------------------------------
# Weather -> category
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Category:
    key: str            # clear | partly_cloudy | cloudy | fog | drizzle | rain | heavy_rain | thunderstorm | snow | heavy_snow
    intensity: float     # 0..1 how strongly the effect reads (rain density, cloud thickness...)
    gloom: float         # 0..1 how much it darkens/desaturates the sky & surfaces
    precip: str          # "" | "rain" | "snow"
    family: str          # coarse bucket used for icon selection


_CODE_TABLE: dict[int, Category] = {
    0: Category("clear", 0.0, 0.0, "", "clear"),
    1: Category("clear", 0.12, 0.04, "", "clear"),
    2: Category("partly_cloudy", 0.42, 0.16, "", "cloud"),
    3: Category("cloudy", 0.78, 0.36, "", "cloud"),
    45: Category("fog", 0.55, 0.38, "", "fog"),
    48: Category("fog", 0.65, 0.42, "", "fog"),
    51: Category("drizzle", 0.28, 0.32, "rain", "rain"),
    53: Category("drizzle", 0.46, 0.38, "rain", "rain"),
    55: Category("drizzle", 0.64, 0.44, "rain", "rain"),
    56: Category("drizzle", 0.5, 0.44, "rain", "rain"),
    57: Category("drizzle", 0.68, 0.48, "rain", "rain"),
    61: Category("rain", 0.35, 0.4, "rain", "rain"),
    62: Category("rain", 0.5, 0.44, "rain", "rain"),
    63: Category("rain", 0.6, 0.48, "rain", "rain"),
    65: Category("heavy_rain", 0.88, 0.62, "rain", "rain"),
    66: Category("rain", 0.55, 0.5, "rain", "rain"),
    67: Category("heavy_rain", 0.8, 0.58, "rain", "rain"),
    71: Category("snow", 0.3, 0.24, "snow", "snow"),
    73: Category("snow", 0.55, 0.3, "snow", "snow"),
    75: Category("heavy_snow", 0.82, 0.36, "snow", "snow"),
    77: Category("snow", 0.35, 0.26, "snow", "snow"),
    80: Category("rain", 0.4, 0.38, "rain", "rain"),
    81: Category("rain", 0.62, 0.46, "rain", "rain"),
    82: Category("heavy_rain", 0.92, 0.58, "rain", "rain"),
    85: Category("snow", 0.45, 0.28, "snow", "snow"),
    86: Category("heavy_snow", 0.78, 0.34, "snow", "snow"),
    95: Category("thunderstorm", 0.72, 0.7, "rain", "storm"),
    96: Category("thunderstorm", 0.86, 0.78, "rain", "storm"),
    99: Category("thunderstorm", 1.0, 0.88, "rain", "storm"),
}


def category_for_code(code: int) -> Category:
    """The static, code-only category — handy for one-off icon lookups
    (hourly/daily rows) where there's no per-row cloud/precip detail to
    refine it with."""
    return _CODE_TABLE.get(code, Category("partly_cloudy", 0.4, 0.2, "", "cloud"))


def classify(weather: WeatherData) -> Category:
    base = category_for_code(weather.weather_code if weather.weather_code is not None else -1)
    # Nudge intensity/gloom toward what the API actually observed, so two
    # cities reporting the same WMO code but different cloud cover/rain
    # rate don't render identically.
    intensity, gloom = base.intensity, base.gloom
    if weather.cloud_cover is not None:
        cc = max(0.0, min(1.0, weather.cloud_cover / 100.0))
        if base.family in ("cloud", "rain", "storm", "snow"):
            intensity = (intensity * 0.7) + (cc * 0.3)
            gloom = (gloom * 0.75) + (cc * 0.25)
    if weather.precipitation_mm is not None and base.precip:
        pm = max(0.0, min(1.0, weather.precipitation_mm / 8.0))
        intensity = min(1.0, (intensity * 0.75) + (pm * 0.25))
    return Category(base.key, max(0.0, min(1.0, intensity)), max(0.0, min(1.0, gloom)), base.precip, base.family)


# ---------------------------------------------------------------------------
# Time-of-day -> continuous phase
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Phase:
    day_frac: float       # 0 (night) .. 1 (full daylight)
    golden: float         # 0..1 intensity of warm dawn/dusk light regardless of day/night
    night_depth: float    # 0..1 how deep into the night we are (star visibility)
    is_pm_side: bool      # sun past solar noon / moon past midnight -> arc direction
    sun_x: float          # 0..1 horizontal position for the sun/moon glyph
    sun_y: float          # 0..1 vertical position (0 top .. 1 horizon)
    show_sun: bool
    show_moon: bool
    minutes: int          # local minutes-of-day, for seeding


def _parse_minutes(hhmm: str | None) -> int | None:
    if not hhmm:
        return None
    try:
        h, m = hhmm.split(":")[:2]
        return int(h) * 60 + int(m)
    except (ValueError, IndexError):
        return None


def _local_now(weather: WeatherData) -> datetime:
    if weather.local_time:
        try:
            return datetime.fromisoformat(weather.local_time)
        except ValueError:
            pass
    try:
        return datetime.now(ZoneInfo(weather.location.timezone or "UTC"))
    except Exception:
        return datetime.utcnow()


def _smoothstep(a: float, b: float, x: float) -> float:
    if a == b:
        return 0.0 if x < a else 1.0
    t = max(0.0, min(1.0, (x - a) / (b - a)))
    return t * t * (3 - 2 * t)


def compute_phase(weather: WeatherData) -> Phase:
    now = _local_now(weather)
    minutes = now.hour * 60 + now.minute
    sunrise = _parse_minutes(weather.sunrise)
    sunset = _parse_minutes(weather.sunset)
    if sunrise is None or sunset is None or sunset <= sunrise:
        sunrise, sunset = 6 * 60 + 15, 18 * 60 + 15

    # Twilight windows either side of sunrise/sunset (civil-ish, stylised).
    # Only the outer edges shape the palette ramp; the inner ones are the
    # geometric midpoints of those ramps and need no separate constant.
    dawn_start = sunrise - 70
    dusk_end = sunset + 70
    solar_noon = (sunrise + sunset) / 2
    # Midnight is the midpoint of the night span (sunset -> next sunrise).
    night_span = (sunrise + 1440) - sunset
    solar_midnight = sunset + night_span / 2
    if solar_midnight >= 1440:
        solar_midnight -= 1440

    t = float(minutes)

    if sunrise <= t <= sunset:
        day_frac = 1.0
    elif dawn_start <= t < sunrise:
        day_frac = _smoothstep(dawn_start, sunrise, t)
    elif sunset < t <= dusk_end:
        day_frac = 1.0 - _smoothstep(sunset, dusk_end, t)
    else:
        day_frac = 0.0

    # Golden light: a bump centred on sunrise and one centred on sunset.
    def bump(centre: float, width: float, value: float) -> float:
        d = abs(value - centre)
        if d > width:
            return 0.0
        return math.cos((d / width) * (math.pi / 2)) ** 1.4

    golden = max(bump(sunrise, 75.0, t), bump(sunset, 75.0, t))

    # Night depth peaks at solar midnight, fades out through both twilights.
    if dusk_end <= t or t <= dawn_start:
        # distance (in minutes, wrapped) from solar midnight
        raw = t - solar_midnight
        raw = (raw + 720) % 1440 - 720
        half_night = max(1.0, night_span / 2)
        night_depth = 1.0 - min(1.0, abs(raw) / half_night)
        night_depth = 0.35 + 0.65 * _smoothstep(0.0, 1.0, night_depth)
    else:
        night_depth = 1.0 - day_frac
        night_depth = min(night_depth, 0.35)

    is_pm_side = t >= solar_noon

    # Sun arcs sunrise(x=0)->sunset(x=1) with height peaking at noon.
    if sunrise <= t <= sunset and sunset > sunrise:
        progress = (t - sunrise) / (sunset - sunrise)
        show_sun, show_moon = True, False
    elif dawn_start <= t < sunrise:
        progress = 0.0
        show_sun, show_moon = day_frac > 0.05, day_frac <= 0.6
    elif sunset < t <= dusk_end:
        progress = 1.0
        show_sun, show_moon = day_frac > 0.05, day_frac <= 0.6
    else:
        # Night: arc the moon across the night span for a bit of life.
        span = night_span if night_span > 0 else 1.0
        raw = (t - sunset) % 1440
        progress = max(0.0, min(1.0, raw / span))
        show_sun, show_moon = False, True

    sun_x = 0.12 + 0.76 * progress
    sun_y = 1.0 - math.sin(progress * math.pi) ** 0.85  # 0 (top) at noon/midnight, ~1 near horizon

    return Phase(
        day_frac=day_frac,
        golden=golden,
        night_depth=max(0.0, min(1.0, night_depth)),
        is_pm_side=is_pm_side,
        sun_x=sun_x,
        sun_y=max(0.05, min(0.95, sun_y)),
        show_sun=show_sun,
        show_moon=show_moon,
        minutes=minutes,
    )


# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------


def hsl(h: float, s: float, l: float) -> Rgb:
    """Hue in degrees, saturation/lightness 0..1 -> 8-bit RGB."""
    r, g, b = colorsys.hls_to_rgb((h % 360) / 360.0, max(0.0, min(1.0, l)), max(0.0, min(1.0, s)))
    return (round(r * 255), round(g * 255), round(b * 255))


def mix(a: Rgb, b: Rgb, t: float) -> Rgb:
    t = max(0.0, min(1.0, t))
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))  # type: ignore[return-value]


def luminance(c: Rgb) -> float:
    r, g, b = (v / 255.0 for v in c)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * max(0.0, min(1.0, t))


def hue_lerp(h0: float, h1: float, t: float) -> float:
    """Interpolate hues the short way around the wheel.

    A naive numeric lerp between a blue zenith hue (~230) and a warm
    horizon hue (~30) sweeps straight through green/yellow on the way
    down. Going the short way instead passes through violet/magenta,
    which is what a real sunset/sunrise sky does.
    """
    diff = ((h1 - h0 + 180) % 360) - 180
    return (h0 + diff * max(0.0, min(1.0, t))) % 360


# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Palette:
    # Material roles
    primary: Rgb
    on_primary: Rgb
    primary_container: Rgb
    on_primary_container: Rgb
    secondary: Rgb
    on_secondary_container: Rgb
    secondary_container: Rgb
    tertiary: Rgb
    tertiary_container: Rgb
    on_tertiary_container: Rgb
    surface: Rgb
    surface_dim: Rgb
    surface_container: Rgb
    surface_container_high: Rgb
    surface_container_highest: Rgb
    on_surface: Rgb
    on_surface_variant: Rgb
    outline: Rgb
    outline_variant: Rgb
    error_container: Rgb
    on_error_container: Rgb

    # Sky-only tokens
    sky_stops: tuple[Rgb, ...]
    horizon_glow: Rgb
    cloud_lit: Rgb
    cloud_shadow: Rgb
    cloud_alpha: float
    star_colour: Rgb
    sun_colour: Rgb
    moon_colour: Rgb
    rain_colour: Rgb
    snow_colour: Rgb
    fog_colour: Rgb
    lightning_colour: Rgb
    hero_on: Rgb
    hero_on_muted: Rgb
    hero_scrim: Rgb
    dark_ui: bool

    phase: Phase = field(repr=False)
    category: Category = field(repr=False)
    seed: int = 0


def _seed_for(weather: WeatherData, phase: Phase) -> int:
    key = f"{weather.location.name}|{weather.location.latitude:.2f}|{weather.location.longitude:.2f}|{phase.minutes // 20}"
    return int(hashlib.sha1(key.encode()).hexdigest()[:8], 16)


def build_palette(weather: WeatherData) -> Palette:
    category = classify(weather)
    phase = compute_phase(weather)
    day, golden, gloom = phase.day_frac, phase.golden, category.gloom
    seed = _seed_for(weather, phase)

    # --- Base hue story -----------------------------------------------
    # Zenith hue drifts blue(day) -> indigo(night); golden light injects a
    # warm hue band near the horizon regardless of the zenith hue.
    zenith_hue = lerp(233.0, 206.0, day)          # night indigo -> day azure
    if category.family == "storm":
        zenith_hue = lerp(248.0, 226.0, day)
    elif category.family == "snow":
        zenith_hue = lerp(224.0, 202.0, day)
    elif category.family == "fog":
        zenith_hue = lerp(222.0, 205.0, day)
    elif category.key in ("cloudy", "rain", "heavy_rain", "drizzle"):
        zenith_hue = lerp(226.0, 210.0, day)

    horizon_warm_hue = lerp(28.0, 350.0, min(1.0, golden * 1.3))  # orange -> pink as golden peaks/fades

    zenith_sat = lerp(0.5, 0.62, day) * (1.0 - gloom * 0.55)
    zenith_light = lerp(0.10, 0.62, day) * (1.0 - gloom * 0.38) + gloom * 0.04
    zenith_light = max(0.06, min(0.86, zenith_light))

    # Horizon hue only leans warm when there is actual golden light; at a
    # plain clear noon or a starry midnight it should stay a paler/darker
    # shade of the *same* hue family, never drift toward an unrelated hue.
    warm = golden
    horizon_hue = hue_lerp(zenith_hue, horizon_warm_hue, warm)
    mid_hue = hue_lerp(zenith_hue, horizon_warm_hue, warm * 0.55)

    # Atmospheric whitening near the horizon (Rayleigh scattering look):
    # more saturated/warm during golden hour, paler/hazier the rest of the
    # time, and darker (not whiter) at night.
    horizon_sat = lerp(0.30, 0.62, warm) * (1.0 - gloom * 0.5)
    horizon_light_base = lerp(0.16, 0.82, day)
    horizon_light = lerp(horizon_light_base, max(horizon_light_base, 0.80), warm) * (1.0 - gloom * 0.35)
    horizon_light = max(0.10, min(0.92, horizon_light))

    mid_sat = lerp(zenith_sat, horizon_sat, 0.5)
    mid_light = lerp(zenith_light, horizon_light, 0.55)

    zenith_rgb = hsl(zenith_hue, zenith_sat, zenith_light)
    mid_rgb = hsl(mid_hue, mid_sat, mid_light)
    horizon_rgb = hsl(horizon_hue, horizon_sat, horizon_light)

    # extra stop just above the horizon for haze/fog washing everything out
    haze_amt = category.intensity if category.family == "fog" else 0.0
    grey = hsl(horizon_hue, 0.06, min(0.9, horizon_light + 0.12))
    horizon_rgb = mix(horizon_rgb, grey, haze_amt * 0.8)
    mid_rgb = mix(mid_rgb, grey, haze_amt * 0.5)

    sky_stops = (zenith_rgb, mix(zenith_rgb, mid_rgb, 0.5), mid_rgb, mix(mid_rgb, horizon_rgb, 0.6), horizon_rgb)

    horizon_glow = hsl(horizon_warm_hue, min(0.85, 0.55 + golden * 0.3), lerp(0.55, 0.82, golden))

    # --- Sky-effect colours --------------------------------------------
    cloud_base_l = lerp(0.22, 0.92, day) * (1.0 - gloom * 0.55)
    cloud_lit = hsl(hue_lerp(zenith_hue, horizon_warm_hue, golden * 0.7), 0.16 + golden * 0.12, max(0.35, min(0.96, cloud_base_l + 0.10)))
    cloud_shadow = hsl(zenith_hue, 0.22, max(0.06, cloud_base_l - 0.30 - gloom * 0.12))
    cloud_alpha = 0.24 + category.intensity * 0.46

    star_colour = hsl(210, 0.15, 0.95)
    sun_colour = hsl(42, 0.85, 0.68)
    moon_colour = hsl(224, 0.18, 0.90)
    rain_colour = hsl(206, 0.35, lerp(0.55, 0.78, day))
    snow_colour = hsl(210, 0.15, 0.97)
    fog_colour = hsl(horizon_hue, 0.08, lerp(0.55, 0.88, day))
    lightning_colour = hsl(225, 0.25, 0.92)

    # --- Material roles ---------------------------------------------------
    # UI leans dark when the sky is dark or gloomy, light when bright & clear.
    surface_l_day = lerp(0.965, 0.62, gloom)
    surface_l_night = lerp(0.155, 0.085, gloom)
    dark_ui = day < 0.55
    surface_l = lerp(surface_l_night, surface_l_day, day)

    primary_hue = hue_lerp(zenith_hue, horizon_warm_hue, golden * 0.85)
    secondary_hue = primary_hue - 34
    tertiary_hue = primary_hue + 54

    surf_sat = 0.10 + gloom * 0.04
    surface = hsl(primary_hue, surf_sat, surface_l)
    if dark_ui:
        surface_dim = hsl(primary_hue, surf_sat + 0.02, max(0.05, surface_l - 0.03))
        surface_container = hsl(primary_hue, surf_sat + 0.03, surface_l + 0.035)
        surface_container_high = hsl(primary_hue, surf_sat + 0.04, surface_l + 0.07)
        surface_container_highest = hsl(primary_hue, surf_sat + 0.05, surface_l + 0.105)
        on_surface = hsl(primary_hue, 0.10, 0.95)
        on_surface_variant = hsl(primary_hue, 0.10, 0.78)
        outline = hsl(primary_hue, 0.10, 0.46)
        outline_variant = hsl(primary_hue, 0.10, 0.28)
    else:
        surface_dim = hsl(primary_hue, surf_sat + 0.02, max(0.55, surface_l - 0.10))
        surface_container = hsl(primary_hue, surf_sat + 0.02, min(0.99, surface_l - 0.045))
        surface_container_high = hsl(primary_hue, surf_sat + 0.03, min(0.99, surface_l - 0.09))
        surface_container_highest = hsl(primary_hue, surf_sat + 0.04, min(0.99, surface_l - 0.135))
        on_surface = hsl(primary_hue, 0.16, 0.14)
        on_surface_variant = hsl(primary_hue, 0.12, 0.34)
        outline = hsl(primary_hue, 0.10, 0.55)
        outline_variant = hsl(primary_hue, 0.10, 0.80)

    primary = hsl(primary_hue, 0.62, 0.55 if dark_ui else 0.44)
    on_primary = hsl(primary_hue, 0.3, 0.99) if dark_ui else hsl(primary_hue, 0.3, 0.99)
    primary_container = hsl(primary_hue, 0.42, 0.28 if dark_ui else 0.90)
    on_primary_container = hsl(primary_hue, 0.35, 0.92 if dark_ui else 0.20)

    secondary = hsl(secondary_hue, 0.30, 0.62 if dark_ui else 0.40)
    secondary_container = hsl(secondary_hue, 0.24, 0.26 if dark_ui else 0.88)
    on_secondary_container = hsl(secondary_hue, 0.2, 0.90 if dark_ui else 0.22)

    tertiary = hsl(tertiary_hue, 0.40, 0.68 if dark_ui else 0.42)
    tertiary_container = hsl(tertiary_hue, 0.36, 0.26 if dark_ui else 0.89)
    on_tertiary_container = hsl(tertiary_hue, 0.3, 0.92 if dark_ui else 0.22)

    error_container = hsl(18, 0.55, 0.26 if dark_ui else 0.90)
    on_error_container = hsl(18, 0.5, 0.90 if dark_ui else 0.28)

    # --- Hero text (drawn straight over the sky, not a surface) --------
    sky_luma = luminance(mix(mid_rgb, horizon_rgb, 0.4))
    if sky_luma > 0.62:
        hero_on = hsl(primary_hue, 0.32, 0.12)
        hero_on_muted = hsl(primary_hue, 0.20, 0.30)
        hero_scrim = (255, 255, 255)
    else:
        hero_on = hsl(primary_hue, 0.12, 0.98)
        hero_on_muted = hsl(primary_hue, 0.14, 0.84)
        hero_scrim = (0, 0, 0)

    return Palette(
        primary=primary, on_primary=on_primary, primary_container=primary_container, on_primary_container=on_primary_container,
        secondary=secondary, on_secondary_container=on_secondary_container, secondary_container=secondary_container,
        tertiary=tertiary, tertiary_container=tertiary_container, on_tertiary_container=on_tertiary_container,
        surface=surface, surface_dim=surface_dim, surface_container=surface_container,
        surface_container_high=surface_container_high, surface_container_highest=surface_container_highest,
        on_surface=on_surface, on_surface_variant=on_surface_variant, outline=outline, outline_variant=outline_variant,
        error_container=error_container, on_error_container=on_error_container,
        sky_stops=sky_stops, horizon_glow=horizon_glow, cloud_lit=cloud_lit, cloud_shadow=cloud_shadow, cloud_alpha=cloud_alpha,
        star_colour=star_colour, sun_colour=sun_colour, moon_colour=moon_colour, rain_colour=rain_colour,
        snow_colour=snow_colour, fog_colour=fog_colour, lightning_colour=lightning_colour,
        hero_on=hero_on, hero_on_muted=hero_on_muted, hero_scrim=hero_scrim, dark_ui=dark_ui,
        phase=phase, category=category, seed=seed,
    )
