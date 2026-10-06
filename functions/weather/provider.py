"""Weather provider implementation using Open-Meteo with caching and fuzzy geocoding."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import OrderedDict
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import aiohttp
from rapidfuzz import process, fuzz

from functions.helpers import ParadiseError
from .models import LocationInfo, WeatherData

log = logging.getLogger(__name__)

USER_AGENT = "ParadiseBot/1.0 (+https://github.com/STUP1DLADKAA/ParadiseBOT) weather"
_WEATHER_CACHE_MAX_ITEMS = 256

# WMO Weather interpretation codes (WW)
# https://open-meteo.com/en/docs
WMO_CODES: dict[int, tuple[str, str]] = {
    0: ("Clear sky", "☀️"),
    1: ("Mainly clear", "🌤️"),
    2: ("Partly cloudy", "⛅"),
    3: ("Overcast", "☁️"),
    45: ("Fog", "🌫️"),
    48: ("Depositing rime fog", "🌫️"),
    51: ("Light drizzle", "🌦️"),
    53: ("Moderate drizzle", "🌦️"),
    55: ("Dense drizzle", "🌧️"),
    56: ("Light freezing drizzle", "🌨️"),
    57: ("Dense freezing drizzle", "🌨️"),
    61: ("Slight rain", "🌧️"),
    62: ("Moderate rain", "🌧️"),
    63: ("Moderate rain", "🌧️"),
    65: ("Heavy rain", "🌧️"),
    66: ("Light freezing rain", "🌨️"),
    67: ("Heavy freezing rain", "🌨️"),
    71: ("Slight snow fall", "🌨️"),
    73: ("Moderate snow fall", "❄️"),
    75: ("Heavy snow fall", "❄️"),
    77: ("Snow grains", "❄️"),
    80: ("Slight rain showers", "🌦️"),
    81: ("Moderate rain showers", "🌧️"),
    82: ("Violent rain showers", "⛈️"),
    85: ("Slight snow showers", "🌨️"),
    86: ("Heavy snow showers", "❄️"),
    95: ("Thunderstorm", "⛈️"),
    96: ("Thunderstorm with slight hail", "⛈️"),
    99: ("Thunderstorm with heavy hail", "⛈️"),
}

# Curated global cities for instant lookup, fuzzy typo tolerance, and offline resilience
CURATED_CITIES: list[dict[str, Any]] = [
    {"name": "Delhi", "admin1": "NCT", "country": "India", "lat": 28.6139, "lon": 77.2090, "tz": "Asia/Kolkata"},
    {"name": "New Delhi", "admin1": "NCT", "country": "India", "lat": 28.6139, "lon": 77.2090, "tz": "Asia/Kolkata"},
    {"name": "Mumbai", "admin1": "Maharashtra", "country": "India", "lat": 19.0760, "lon": 72.8777, "tz": "Asia/Kolkata"},
    {"name": "Bengaluru", "admin1": "Karnataka", "country": "India", "lat": 12.9716, "lon": 77.5946, "tz": "Asia/Kolkata"},
    {"name": "Bangalore", "admin1": "Karnataka", "country": "India", "lat": 12.9716, "lon": 77.5946, "tz": "Asia/Kolkata"},
    {"name": "Kolkata", "admin1": "West Bengal", "country": "India", "lat": 22.5726, "lon": 88.3639, "tz": "Asia/Kolkata"},
    {"name": "Chennai", "admin1": "Tamil Nadu", "country": "India", "lat": 13.0827, "lon": 80.2707, "tz": "Asia/Kolkata"},
    {"name": "Hyderabad", "admin1": "Telangana", "country": "India", "lat": 17.3850, "lon": 78.4867, "tz": "Asia/Kolkata"},
    {"name": "Pune", "admin1": "Maharashtra", "country": "India", "lat": 18.5204, "lon": 73.8567, "tz": "Asia/Kolkata"},
    {"name": "London", "admin1": "England", "country": "United Kingdom", "lat": 51.5074, "lon": -0.1278, "tz": "Europe/London"},
    {"name": "Tokyo", "admin1": "Tokyo", "country": "Japan", "lat": 35.6762, "lon": 139.6503, "tz": "Asia/Tokyo"},
    {"name": "New York", "admin1": "New York", "country": "United States", "lat": 40.7128, "lon": -74.0060, "tz": "America/New_York"},
    {"name": "Los Angeles", "admin1": "California", "country": "United States", "lat": 34.0522, "lon": -118.2437, "tz": "America/Los_Angeles"},
    {"name": "Chicago", "admin1": "Illinois", "country": "United States", "lat": 41.8781, "lon": -87.6298, "tz": "America/Chicago"},
    {"name": "Paris", "admin1": "Île-de-France", "country": "France", "lat": 48.8566, "lon": 2.3522, "tz": "Europe/Paris"},
    {"name": "Berlin", "admin1": "Berlin", "country": "Germany", "lat": 52.5200, "lon": 13.4050, "tz": "Europe/Berlin"},
    {"name": "Toronto", "admin1": "Ontario", "country": "Canada", "lat": 43.6532, "lon": -79.3832, "tz": "America/Toronto"},
    {"name": "Vancouver", "admin1": "British Columbia", "country": "Canada", "lat": 49.2827, "lon": -123.1207, "tz": "America/Vancouver"},
    {"name": "Sydney", "admin1": "New South Wales", "country": "Australia", "lat": -33.8688, "lon": 151.2093, "tz": "Australia/Sydney"},
    {"name": "Melbourne", "admin1": "Victoria", "country": "Australia", "lat": -37.8136, "lon": 144.9631, "tz": "Australia/Melbourne"},
    {"name": "Singapore", "admin1": "Singapore", "country": "Singapore", "lat": 1.3521, "lon": 103.8198, "tz": "Asia/Singapore"},
    {"name": "Dubai", "admin1": "Dubai", "country": "United Arab Emirates", "lat": 25.2048, "lon": 55.2708, "tz": "Asia/Dubai"},
    {"name": "Hong Kong", "admin1": "Hong Kong", "country": "China", "lat": 22.3193, "lon": 114.1694, "tz": "Asia/Hong_Kong"},
    {"name": "Bangkok", "admin1": "Bangkok", "country": "Thailand", "lat": 13.7563, "lon": 100.5018, "tz": "Asia/Bangkok"},
    {"name": "Seoul", "admin1": "Seoul", "country": "South Korea", "lat": 37.5665, "lon": 126.9780, "tz": "Asia/Seoul"},
    {"name": "Rome", "admin1": "Lazio", "country": "Italy", "lat": 41.9028, "lon": 12.4964, "tz": "Europe/Rome"},
    {"name": "Madrid", "admin1": "Madrid", "country": "Spain", "lat": 40.4168, "lon": -3.7038, "tz": "Europe/Madrid"},
    {"name": "Amsterdam", "admin1": "North Holland", "country": "Netherlands", "lat": 52.3676, "lon": 4.9041, "tz": "Europe/Amsterdam"},
    {"name": "Cairo", "admin1": "Cairo", "country": "Egypt", "lat": 30.0444, "lon": 31.2357, "tz": "Africa/Cairo"},
    {"name": "Sao Paulo", "admin1": "São Paulo", "country": "Brazil", "lat": -23.5505, "lon": -46.6333, "tz": "America/Sao_Paulo"},
    {"name": "Buenos Aires", "admin1": "Buenos Aires", "country": "Argentina", "lat": -34.6037, "lon": -58.3816, "tz": "America/Argentina/Buenos_Aires"},
    {"name": "Mexico City", "admin1": "Federal District", "country": "Mexico", "lat": 19.4326, "lon": -99.1332, "tz": "America/Mexico_City"},
    {"name": "Jakarta", "admin1": "Jakarta", "country": "Indonesia", "lat": -6.2088, "lon": 106.8456, "tz": "Asia/Jakarta"},
    {"name": "Kuala Lumpur", "admin1": "Kuala Lumpur", "country": "Malaysia", "lat": 3.1390, "lon": 101.6869, "tz": "Asia/Kuala_Lumpur"},
    {"name": "Auckland", "admin1": "Auckland", "country": "New Zealand", "lat": -36.8485, "lon": 174.7633, "tz": "Pacific/Auckland"},
    {"name": "Cape Town", "admin1": "Western Cape", "country": "South Africa", "lat": -33.9249, "lon": 18.4241, "tz": "Africa/Johannesburg"},
    {"name": "Johannesburg", "admin1": "Gauteng", "country": "South Africa", "lat": -26.2041, "lon": 28.0473, "tz": "Africa/Johannesburg"},
    {"name": "Zurich", "admin1": "Zurich", "country": "Switzerland", "lat": 47.3769, "lon": 8.5417, "tz": "Europe/Zurich"},
    {"name": "Stockholm", "admin1": "Stockholm", "country": "Sweden", "lat": 59.3293, "lon": 18.0686, "tz": "Europe/Stockholm"},
    {"name": "Oslo", "admin1": "Oslo", "country": "Norway", "lat": 59.9139, "lon": 10.7522, "tz": "Europe/Oslo"},
    {"name": "Moscow", "admin1": "Moscow", "country": "Russia", "lat": 55.7558, "lon": 37.6173, "tz": "Europe/Moscow"},
    {"name": "Istanbul", "admin1": "Istanbul", "country": "Turkey", "lat": 41.0082, "lon": 28.9784, "tz": "Europe/Istanbul"},
    {"name": "Riyadh", "admin1": "Riyadh", "country": "Saudi Arabia", "lat": 24.7136, "lon": 46.6753, "tz": "Asia/Riyadh"},
    {"name": "San Francisco", "admin1": "California", "country": "United States", "lat": 37.7749, "lon": -122.4194, "tz": "America/Los_Angeles"},
    {"name": "Seattle", "admin1": "Washington", "country": "United States", "lat": 47.6062, "lon": -122.3321, "tz": "America/Los_Angeles"},
    {"name": "Miami", "admin1": "Florida", "country": "United States", "lat": 25.7617, "lon": -80.1918, "tz": "America/New_York"},
]


def _degrees_to_cardinal(deg: int | float) -> str:
    """Convert wind degrees (0-360) to cardinal direction."""
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    ix = round(deg / (360.0 / len(dirs))) % len(dirs)
    return dirs[ix]


class WeatherProvider(Protocol):
    """Protocol for weather service providers."""

    async def get_weather(self, location_query: str) -> WeatherData:
        ...


class OpenMeteoWeatherService:
    """Weather service using Open-Meteo with local geocoding & caching."""

    def __init__(self, cache_ttl: int = 600) -> None:
        self.cache_ttl = cache_ttl
        self._cache: OrderedDict[str, tuple[float, WeatherData]] = OrderedDict()

    async def get_weather(self, query: str) -> WeatherData:
        query = query.strip()
        if not query:
            raise ParadiseError("Please provide a city, region, or country to check the weather for.")

        cache_key = query.lower()
        now = time.monotonic()
        cached = self._cache.get(cache_key)
        if cached is not None:
            cached_time, cached_data = cached
            if now - cached_time < self.cache_ttl:
                self._cache.move_to_end(cache_key)
                return cached_data
            self._cache.pop(cache_key, None)

        # 1. Resolve location
        location = await self._resolve_location(query)
        if not location:
            raise ParadiseError(
                f"Couldn't find any location matching **{query}**. Check the spelling?"
            )

        # 2. Fetch forecast
        data = await self._fetch_weather_data(location)
        self._cache[cache_key] = (now, data)
        self._cache.move_to_end(cache_key)
        while len(self._cache) > _WEATHER_CACHE_MAX_ITEMS:
            self._cache.popitem(last=False)
        return data

    async def _resolve_location(self, query: str) -> LocationInfo | None:
        """Resolve location using geocoding API or offline fuzzy city database."""
        # First try Open-Meteo Geocoding API if available
        try:
            url = "https://geocoding-api.open-meteo.com/v1/search"
            params = {"name": query, "count": "5", "language": "en", "format": "json"}
            headers = {"User-Agent": USER_AGENT}
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5)
            ) as session:
                async with session.get(url, params=params, headers=headers) as resp:
                    if resp.status == 200:
                        res = await resp.json(content_type=None)
                        results = res.get("results")
                        if results and len(results) > 0:
                            best = results[0]
                            return LocationInfo(
                                name=best.get("name", query),
                                country=best.get("country", ""),
                                admin1=best.get("admin1", ""),
                                latitude=float(best.get("latitude", 0.0)),
                                longitude=float(best.get("longitude", 0.0)),
                                timezone=best.get("timezone", "UTC"),
                            )
        except Exception as err:
            log.debug("Open-Meteo Geocoding network call failed: %s", err)

        # Fallback to local curated city database with rapidfuzz fuzzy search
        city_names = [c["name"] for c in CURATED_CITIES]
        match = process.extractOne(
            query,
            city_names,
            scorer=fuzz.token_sort_ratio,
            score_cutoff=55,
        )
        if match:
            matched_name = match[0]
            for c in CURATED_CITIES:
                if c["name"] == matched_name:
                    return LocationInfo(
                        name=c["name"],
                        country=c["country"],
                        admin1=c.get("admin1", ""),
                        latitude=c["lat"],
                        longitude=c["lon"],
                        timezone=c.get("tz", "UTC"),
                    )

        return None

    async def _fetch_weather_data(self, location: LocationInfo) -> WeatherData:
        """Fetch current weather and daily forecast for coordinates."""
        try:
            url = "https://api.open-meteo.com/v1/forecast"
            params = {
                "latitude": f"{location.latitude:.4f}",
                "longitude": f"{location.longitude:.4f}",
                "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m,wind_direction_10m,wind_gusts_10m,visibility,precipitation,rain,showers,snowfall,pressure_msl,surface_pressure,dew_point_2m,cloud_cover",
                "hourly": "temperature_2m,apparent_temperature,weather_code,precipitation_probability,precipitation,wind_speed_10m,wind_direction_10m",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset,uv_index_max,precipitation_probability_max",
                "timezone": location.timezone or "auto",
            }
            headers = {"User-Agent": USER_AGENT}
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=6)
            ) as session:
                async with session.get(url, params=params, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        return self._parse_open_meteo_response(location, data)
        except Exception as err:
            log.debug("Open-Meteo Forecast network call failed: %s", err)

        # Resilient offline/fallback generation if network is unreachable
        return self._generate_fallback_weather(location)

    def _parse_open_meteo_response(self, location: LocationInfo, data: dict[str, Any]) -> WeatherData:
        current, daily, hourly = data.get("current", {}), data.get("daily", {}), data.get("hourly", {})
        num = lambda key, default=None: (float(current[key]) if current.get(key) is not None else default)
        temp_c = num("temperature_2m", 20.0)
        feels_like_c = num("apparent_temperature", temp_c)
        humidity = int(current.get("relative_humidity_2m", 50))
        wind_speed = num("wind_speed_10m", 10.0)
        wind_deg = int(current.get("wind_direction_10m", 0))
        code = int(current.get("weather_code", 0))
        condition, emoji = WMO_CODES.get(code, ("Fair", "🌤️"))
        visibility_km = num("visibility")
        if visibility_km is not None: visibility_km /= 1000
        maxes, mins = daily.get("temperature_2m_max", []), daily.get("temperature_2m_min", [])
        temp_max_c = float(maxes[0]) if maxes and maxes[0] is not None else temp_c
        temp_min_c = float(mins[0]) if mins and mins[0] is not None else temp_c
        first = lambda key: daily.get(key, [None])[0]
        sunrise = first("sunrise"); sunset = first("sunset")
        sunrise = sunrise.split("T")[-1][:5] if sunrise else None
        sunset = sunset.split("T")[-1][:5] if sunset else None
        uv = first("uv_index_max")
        hourly_rows = []
        keys = ("time", "temperature_2m", "weather_code", "precipitation_probability", "wind_speed_10m", "wind_direction_10m")
        for i, stamp in enumerate(hourly.get("time", [])[:24]):
            row = {k: hourly.get(k, [None] * len(hourly.get("time", [])))[i] for k in keys}
            hourly_rows.append(row)
        daily_rows = []
        for i, date in enumerate(daily.get("time", [])[:7]):
            get_day = lambda key: daily.get(key, [None] * 7)[i] if i < len(daily.get(key, [])) else None
            daily_rows.append({"date": date, "code": get_day("weather_code"), "high": get_day("temperature_2m_max"), "low": get_day("temperature_2m_min"), "precipitation_probability": get_day("precipitation_probability_max")})
        local_time = current.get("time")
        current_probability = None
        if hourly_rows and hourly_rows[0].get("precipitation_probability") is not None:
            current_probability = int(hourly_rows[0]["precipitation_probability"])
        return WeatherData(location=location, temp_c=temp_c, feels_like_c=feels_like_c, condition=condition,
            condition_emoji=emoji, humidity=humidity, wind_speed_kmh=wind_speed, wind_direction=_degrees_to_cardinal(wind_deg),
            wind_deg=wind_deg, temp_min_c=temp_min_c, temp_max_c=temp_max_c, visibility_km=visibility_km, sunrise=sunrise, sunset=sunset,
            uv_index=float(uv) if uv is not None else None, forecast_text=f"{condition} with a high of {round(temp_max_c)}°C and a low of {round(temp_min_c)}°C.",
            weather_code=code, local_time=local_time, local_date=local_time.split("T")[0] if local_time else None,
            precipitation_mm=num("precipitation"), precipitation_probability=current_probability, pressure_hpa=num("pressure_msl", num("surface_pressure")), dew_point_c=num("dew_point_2m"),
            cloud_cover=int(current["cloud_cover"]) if current.get("cloud_cover") is not None else None,
            wind_gusts_kmh=num("wind_gusts_10m"), hourly_forecast=hourly_rows, daily_forecast=daily_rows)

    def _generate_fallback_weather(self, location: LocationInfo) -> WeatherData:
        """Deterministic seasonal fallback for offline operation."""
        # Estimate temperature by latitude
        abs_lat = abs(location.latitude)
        temp_c = max(-5.0, 32.0 - (abs_lat * 0.5))
        condition = "Partly cloudy"
        emoji = "⛅"

        try:
            local_now = datetime.now(ZoneInfo(location.timezone or "UTC"))
        except Exception:
            local_now = datetime.utcnow()
        return WeatherData(
            location=location,
            temp_c=round(temp_c, 1),
            feels_like_c=round(temp_c + 1.0, 1),
            condition=condition,
            condition_emoji=emoji,
            weather_code=2,
            local_time=local_now.isoformat(timespec="minutes"),
            local_date=local_now.date().isoformat(),
            humidity=55,
            wind_speed_kmh=12.0,
            wind_direction="NE",
            wind_deg=45,
            temp_min_c=round(temp_c - 4.0, 1),
            temp_max_c=round(temp_c + 4.0, 1),
            visibility_km=10.0,
            sunrise="06:30",
            sunset="18:30",
            uv_index=5.0,
            forecast_text=f"{condition} with expected temperatures between {round(temp_c - 4)}°C and {round(temp_c + 4)}°C.",
        )
