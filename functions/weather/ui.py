"""Text fallback and Discord embed for weather responses."""

from __future__ import annotations

import discord

from functions.helpers import Colour
from .models import WeatherData


def _temperature(celsius: float | None, fahrenheit: float | None) -> str | None:
    if celsius is None:
        return None
    if fahrenheit is None:
        return f"{celsius:.1f}°C"
    return f"{celsius:.1f}°C ({fahrenheit:.1f}°F)"


def build_weather_embed(weather: WeatherData) -> discord.Embed:
    """Build a useful no-image fallback without filling absent data with zeroes."""
    location = weather.location
    title_prefix = f"{weather.condition_emoji} " if weather.condition_emoji else ""
    description = weather.condition
    if weather.forecast_text and weather.forecast_text != weather.condition:
        description += f"\n{weather.forecast_text}"
    embed = discord.Embed(
        title=f"{title_prefix}Weather for {location.display_name}"[:256],
        description=description[:4096],
        colour=Colour.ACCENT,
    )

    temperature_lines = []
    current = _temperature(weather.temp_c, weather.temp_f)
    feels = _temperature(weather.feels_like_c, weather.feels_like_f)
    if current:
        temperature_lines.append(f"**Now:** {current}")
    if feels:
        temperature_lines.append(f"**Feels like:** {feels}")
    if temperature_lines:
        embed.add_field(name="Temperature", value="\n".join(temperature_lines), inline=True)

    range_lines = []
    high = _temperature(weather.temp_max_c, weather.temp_max_f)
    low = _temperature(weather.temp_min_c, weather.temp_min_f)
    if high:
        range_lines.append(f"**High:** {high}")
    if low:
        range_lines.append(f"**Low:** {low}")
    if range_lines:
        embed.add_field(name="Today's range", value="\n".join(range_lines), inline=True)

    atmosphere = []
    if weather.humidity is not None:
        atmosphere.append(f"**Humidity:** {weather.humidity}%")
    if weather.wind_speed_kmh is not None:
        wind = f"{weather.wind_speed_kmh:.1f} km/h"
        if weather.wind_speed_mph is not None:
            wind += f" ({weather.wind_speed_mph:.1f} mph)"
        if weather.wind_direction:
            wind += f" {weather.wind_direction}"
        atmosphere.append(f"**Wind:** {wind}")
    if weather.precipitation_probability is not None:
        atmosphere.append(f"**Precipitation:** {weather.precipitation_probability}%")
    if atmosphere:
        embed.add_field(name="Conditions", value="\n".join(atmosphere), inline=True)

    details = []
    if weather.visibility_km is not None:
        details.append(f"**Visibility:** {weather.visibility_km:.1f} km")
    if weather.sunrise:
        details.append(f"**Sunrise:** {weather.sunrise}")
    if weather.sunset:
        details.append(f"**Sunset:** {weather.sunset}")
    if weather.uv_index is not None:
        details.append(f"**UV index:** {weather.uv_index:.1f}")
    if details:
        embed.add_field(name="More", value="\n".join(details), inline=True)

    embed.set_footer(text=f"Local time · {location.timezone}")
    return embed
