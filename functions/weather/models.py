"""Data models for weather observations and forecasts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class LocationInfo:
    """Geographic information for a resolved location."""

    name: str
    country: str = ""
    admin1: str = ""  # State/Region/Province
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
    """Full weather report for a location."""

    location: LocationInfo
    temp_c: float
    feels_like_c: float
    condition: str
    condition_emoji: str
    humidity: int
    wind_speed_kmh: float
    wind_direction: str
    wind_deg: int = 0
    temp_min_c: float = 0.0
    temp_max_c: float = 0.0
    visibility_km: float | None = None
    sunrise: str | None = None
    sunset: str | None = None
    uv_index: float | None = None
    forecast_text: str = ""
    updated_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def temp_f(self) -> float:
        return (self.temp_c * 9 / 5) + 32

    @property
    def feels_like_f(self) -> float:
        return (self.feels_like_c * 9 / 5) + 32

    @property
    def temp_min_f(self) -> float:
        return (self.temp_min_c * 9 / 5) + 32

    @property
    def temp_max_f(self) -> float:
        return (self.temp_max_c * 9 / 5) + 32

    @property
    def wind_speed_mph(self) -> float:
        return self.wind_speed_kmh * 0.621371

    @property
    def visibility_miles(self) -> float | None:
        if self.visibility_km is None:
            return None
        return self.visibility_km * 0.621371
