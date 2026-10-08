"""Data models for provider observations and hourly/daily forecasts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(slots=True)
class LocationInfo:
    """Geographic information returned by the existing geocoder."""

    name: str
    country: str = ""
    admin1: str = ""
    latitude: float = 0.0
    longitude: float = 0.0
    timezone: str = "UTC"

    @property
    def display_name(self) -> str:
        parts = [self.name]
        if self.admin1 and self.admin1 != self.name:
            parts.append(self.admin1)
        if self.country:
            parts.append(self.country)
        return ", ".join(parts)


@dataclass(slots=True)
class WeatherData:
    """Provider-shaped report; unavailable measurements remain ``None``."""

    location: LocationInfo
    temp_c: float | None
    feels_like_c: float | None
    condition: str
    condition_emoji: str = ""
    humidity: int | None = None
    wind_speed_kmh: float | None = None
    wind_direction: str | None = None
    wind_deg: int | None = None
    temp_min_c: float | None = None
    temp_max_c: float | None = None
    visibility_km: float | None = None
    sunrise: str | None = None
    sunset: str | None = None
    uv_index: float | None = None
    forecast_text: str = ""
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    weather_code: int | None = None
    local_time: str | None = None
    local_date: str | None = None
    precipitation_mm: float | None = None
    precipitation_probability: int | None = None
    pressure_hpa: float | None = None
    dew_point_c: float | None = None
    cloud_cover: int | None = None
    wind_gusts_kmh: float | None = None
    hourly_forecast: list[dict[str, Any]] = field(default_factory=list)
    daily_forecast: list[dict[str, Any]] = field(default_factory=list)
    air_quality: dict[str, Any] | None = None

    @staticmethod
    def _to_fahrenheit(value: float | None) -> float | None:
        return None if value is None else (value * 9 / 5) + 32

    @property
    def temp_f(self) -> float | None:
        return self._to_fahrenheit(self.temp_c)

    @property
    def feels_like_f(self) -> float | None:
        return self._to_fahrenheit(self.feels_like_c)

    @property
    def temp_min_f(self) -> float | None:
        return self._to_fahrenheit(self.temp_min_c)

    @property
    def temp_max_f(self) -> float | None:
        return self._to_fahrenheit(self.temp_max_c)

    @property
    def wind_speed_mph(self) -> float | None:
        return None if self.wind_speed_kmh is None else self.wind_speed_kmh * 0.621371

    @property
    def visibility_miles(self) -> float | None:
        return None if self.visibility_km is None else self.visibility_km * 0.621371
