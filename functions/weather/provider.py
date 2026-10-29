"""Weather provider implementation using Open-Meteo with caching and fuzzy geocoding."""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from rapidfuzz import process, fuzz

from functions.perf import shared_session as _shared_session

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


def _session(timeout: float):
    """The pooled session used for every weather request.

    One function so the offline tests can substitute an unreachable session
    and exercise the real fallback code path.
    """
    return _shared_session("weather", timeout=timeout)


class WeatherProvider(Protocol):
    """Protocol for weather service providers."""

    async def get_weather(self, location_query: str) -> WeatherData:
        ...


class OpenMeteoWeatherService:
    """Weather service using Open-Meteo with local geocoding & caching."""

    def __init__(self, cache_ttl: int = 600) -> None:
        self.cache_ttl = cache_ttl
        self._cache: OrderedDict[str, tuple[float, WeatherData]] = OrderedDict()
        # Geocoding results are stable for a session; remembering them makes
        # every repeat lookup ("weather in delhi" ten times a day) a cache hit
        # instead of another round trip.
        self._places: OrderedDict[str, tuple[float, LocationInfo]] = OrderedDict()
        self._places_ttl = max(3600.0, float(cache_ttl) * 6)

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
        now = time.monotonic()
        remembered = self._places.get(query)
        if remembered is not None:
            stamped, place = remembered
            if now - stamped < self._places_ttl:
                self._places.move_to_end(query)
                return place
            self._places.pop(query, None)

        place = await self._geocode(query)
        if place is not None:
            self._places[query] = (now, place)
            self._places.move_to_end(query)
            while len(self._places) > _WEATHER_CACHE_MAX_ITEMS:
                self._places.popitem(last=False)
        return place

    async def _geocode(self, query: str) -> LocationInfo | None:
        """One geocoding round trip, with the offline city table as fallback."""
        # First try Open-Meteo Geocoding API if available
        try:
            url = "https://geocoding-api.open-meteo.com/v1/search"
            params = {"name": query, "count": "5", "language": "en", "format": "json"}
            headers = {"User-Agent": USER_AGENT}
            session = _session(5.0)
            if True:  # keep the existing indentation/shape of the block
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
        """Fetch provider observations, preserving the existing offline fallback."""
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
        try:
            session = _session(6.0)
            if True:  # pooled: no TLS handshake per weather card
                async with session.get(url, params=params, headers=headers) as response:
                    if response.status != 200:
                        log.warning("Open-Meteo returned HTTP %s for a weather request", response.status)
                        return self._generate_fallback_weather(location)
                    payload = await response.json(content_type=None)
            return self._parse_open_meteo_response(location, payload)
        except Exception as error:  # retain the existing offline fallback behavior
            log.warning("Open-Meteo weather request failed (%s); using fallback", type(error).__name__)
            return self._generate_fallback_weather(location)

    def _parse_open_meteo_response(self, location: LocationInfo, data: dict[str, Any]) -> WeatherData:
        current = data.get("current") or {}
        daily = data.get("daily") or {}
        hourly = data.get("hourly") or {}

        def current_number(key: str) -> float | None:
            value = current.get(key)
            try:
                return float(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        temp_c = current_number("temperature_2m")
        feels_like_c = current_number("apparent_temperature")
        humidity_raw = current.get("relative_humidity_2m")
        try:
            humidity = int(humidity_raw) if humidity_raw is not None else None
        except (TypeError, ValueError):
            humidity = None
        wind_speed = current_number("wind_speed_10m")
        wind_degree_value = current_number("wind_direction_10m")
        wind_deg = int(wind_degree_value) % 360 if wind_degree_value is not None else None
        wind_direction = _degrees_to_cardinal(wind_deg) if wind_deg is not None else None
        code_raw = current.get("weather_code")
        try:
            code = int(code_raw) if code_raw is not None else None
        except (TypeError, ValueError):
            code = None
        condition, emoji = WMO_CODES.get(code, ("Conditions unavailable", "")) if code is not None else ("Conditions unavailable", "")
        visibility_km = current_number("visibility")
        if visibility_km is not None:
            visibility_km /= 1000.0

        maxes = daily.get("temperature_2m_max") or []
        mins = daily.get("temperature_2m_min") or []
        temp_max_c = self._first_number(maxes)
        temp_min_c = self._first_number(mins)

        def first_value(key: str):
            values = daily.get(key) or []
            return values[0] if values else None

        sunrise_raw = first_value("sunrise")
        sunset_raw = first_value("sunset")
        sunrise = str(sunrise_raw).split("T")[-1][:5] if sunrise_raw else None
        sunset = str(sunset_raw).split("T")[-1][:5] if sunset_raw else None
        uv_raw = first_value("uv_index_max")
        try:
            uv_index = float(uv_raw) if uv_raw is not None else None
        except (TypeError, ValueError):
            uv_index = None

        hourly_times = list(hourly.get("time") or [])
        hourly_keys = (
            "temperature_2m", "weather_code", "precipitation_probability",
            "precipitation", "wind_speed_10m", "wind_direction_10m",
        )
        hourly_rows: list[dict[str, Any]] = []
        for index, stamp in enumerate(hourly_times[:24]):
            row: dict[str, Any] = {"time": stamp}
            for key in hourly_keys:
                values = hourly.get(key) or []
                row[key] = values[index] if index < len(values) else None
            hourly_rows.append(row)

        current_time = current.get("time")
        current_probability = None
        if current_time and hourly_rows:
            try:
                current_dt = datetime.fromisoformat(str(current_time))
                timed_rows = []
                for row in hourly_rows:
                    if row.get("precipitation_probability") is None or not row.get("time"):
                        continue
                    row_dt = datetime.fromisoformat(str(row["time"]))
                    timed_rows.append((abs((row_dt - current_dt).total_seconds()), row))
                if timed_rows:
                    current_probability = int(min(timed_rows, key=lambda entry: entry[0])[1]["precipitation_probability"])
            except (TypeError, ValueError):
                current_probability = None

        daily_rows: list[dict[str, Any]] = []
        for index, day_stamp in enumerate((daily.get("time") or [])[:7]):
            def day_value(key: str):
                values = daily.get(key) or []
                return values[index] if index < len(values) else None
            daily_rows.append({
                "date": day_stamp,
                "code": day_value("weather_code"),
                "high": day_value("temperature_2m_max"),
                "low": day_value("temperature_2m_min"),
                "precipitation_probability": day_value("precipitation_probability_max"),
            })

        forecast_parts = []
        if temp_max_c is not None:
            forecast_parts.append(f"High {round(temp_max_c)}°C")
        if temp_min_c is not None:
            forecast_parts.append(f"low {round(temp_min_c)}°C")
        forecast_text = f"{condition} · " + " / ".join(forecast_parts) if forecast_parts else condition

        try:
            cloud_cover = int(current["cloud_cover"]) if current.get("cloud_cover") is not None else None
        except (TypeError, ValueError):
            cloud_cover = None

        return WeatherData(
            location=location,
            temp_c=temp_c,
            feels_like_c=feels_like_c,
            condition=condition,
            condition_emoji=emoji,
            humidity=humidity,
            wind_speed_kmh=wind_speed,
            wind_direction=wind_direction,
            wind_deg=wind_deg,
            temp_min_c=temp_min_c,
            temp_max_c=temp_max_c,
            visibility_km=visibility_km,
            sunrise=sunrise,
            sunset=sunset,
            uv_index=uv_index,
            forecast_text=forecast_text,
            weather_code=code,
            local_time=str(current_time) if current_time else None,
            local_date=str(current_time).split("T")[0] if current_time else None,
            precipitation_mm=current_number("precipitation"),
            precipitation_probability=current_probability,
            pressure_hpa=current_number("pressure_msl") or current_number("surface_pressure"),
            dew_point_c=current_number("dew_point_2m"),
            cloud_cover=cloud_cover,
            wind_gusts_kmh=current_number("wind_gusts_10m"),
            hourly_forecast=hourly_rows,
            daily_forecast=daily_rows,
        )

    @staticmethod
    def _first_number(values: list[Any]) -> float | None:
        if not values or values[0] is None:
            return None
        try:
            return float(values[0])
        except (TypeError, ValueError):
            return None

    def _generate_fallback_weather(self, location: LocationInfo) -> WeatherData:
        """Retain the original deterministic fallback for provider outages."""
        temperature = max(-5.0, 32.0 - (abs(location.latitude) * 0.5))
        try:
            local_now = datetime.now(ZoneInfo(location.timezone or "UTC"))
        except Exception:
            local_now = datetime.now(timezone.utc)
        condition = "Partly cloudy"
        return WeatherData(
            location=location,
            temp_c=round(temperature, 1),
            feels_like_c=round(temperature + 1.0, 1),
            condition=condition,
            condition_emoji="⛅",
            humidity=55,
            wind_speed_kmh=12.0,
            wind_direction="NE",
            wind_deg=45,
            temp_min_c=round(temperature - 4.0, 1),
            temp_max_c=round(temperature + 4.0, 1),
            visibility_km=10.0,
            sunrise="06:30",
            sunset="18:30",
            uv_index=5.0,
            forecast_text=(
                f"{condition} with expected temperatures between "
                f"{round(temperature - 4)}°C and {round(temperature + 4)}°C."
            ),
            weather_code=2,
            local_time=local_now.isoformat(timespec="minutes"),
            local_date=local_now.date().isoformat(),
        )
