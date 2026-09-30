"""Discord UI embeds for weather reports."""

from __future__ import annotations

import discord

from functions.helpers import Colour
from .models import WeatherData


def build_weather_embed(weather: WeatherData) -> discord.Embed:
    """Build a rich, clean Discord embed for weather information."""
    loc = weather.location
    embed = discord.Embed(
        title=f"{weather.condition_emoji} Weather for {loc.display_name}",
        description=f"**{weather.condition}**\n{weather.forecast_text}",
        colour=Colour.ACCENT,
    )

    # Main temperature field
    embed.add_field(
        name="🌡️ Temperature",
        value=(
            f"**{weather.temp_c:.1f}°C** ({weather.temp_f:.1f}°F)\n"
            f"Feels like: {weather.feels_like_c:.1f}°C ({weather.feels_like_f:.1f}°F)"
        ),
        inline=True,
    )

    # Today's Range
    embed.add_field(
        name="📊 Today's Range",
        value=(
            f"**High:** {weather.temp_max_c:.1f}°C ({weather.temp_max_f:.1f}°F)\n"
            f"**Low:** {weather.temp_min_c:.1f}°C ({weather.temp_min_f:.1f}°F)"
        ),
        inline=True,
    )

    # Humidity & Wind
    embed.add_field(
        name="💨 Wind & Atmosphere",
        value=(
            f"**Wind:** {weather.wind_speed_kmh:.1f} km/h ({weather.wind_speed_mph:.1f} mph) {weather.wind_direction}\n"
            f"**Humidity:** {weather.humidity}%"
        ),
        inline=True,
    )

    # Visibility & Sun times
    extra_details = []
    if weather.visibility_km is not None:
        extra_details.append(f"**Visibility:** {weather.visibility_km:.1f} km ({weather.visibility_miles:.1f} mi)")
    if weather.sunrise and weather.sunset:
        extra_details.append(f"**Sun:** 🌅 {weather.sunrise} • 🌇 {weather.sunset}")
    if weather.uv_index is not None:
        extra_details.append(f"**UV Index:** {weather.uv_index:.1f}")

    if extra_details:
        embed.add_field(
            name="👁️ Details",
            value="\n".join(extra_details),
            inline=True,
        )

    embed.set_footer(
        text=f"Resolved to {loc.name}, {loc.country} • Timezone: {loc.timezone}"
    )
    return embed
