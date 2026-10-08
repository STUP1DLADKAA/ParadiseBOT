"""Offline tests for provider parsing, weather formatting, and image cards."""

from __future__ import annotations

import os
import sys
import unittest
from io import BytesIO
from unittest.mock import AsyncMock, patch

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from functions.helpers import ParadiseError
from functions.weather import (
    LocationInfo,
    OpenMeteoWeatherService,
    WeatherData,
    build_weather_embed,
    render_weather_card,
)
from functions.weather.provider import _degrees_to_cardinal


def sample_weather(**overrides) -> WeatherData:
    values = {
        "location": LocationInfo(
            name="Delhi", admin1="NCT", country="India", latitude=28.61,
            longitude=77.20, timezone="Asia/Kolkata",
        ),
        "temp_c": 28.0,
        "feels_like_c": 30.0,
        "condition": "Partly cloudy",
        "condition_emoji": "⛅",
        "humidity": 65,
        "wind_speed_kmh": 12.0,
        "wind_direction": "NE",
        "temp_min_c": 19.0,
        "temp_max_c": 31.0,
        "visibility_km": 10.0,
        "sunrise": "06:14",
        "sunset": "18:22",
        "uv_index": 6.5,
        "forecast_text": "Partly cloudy with pleasant temperatures.",
        "weather_code": 2,
        "local_time": "2026-10-08T14:05",
        "precipitation_probability": 15,
        "hourly_forecast": [
            {
                "time": f"2026-10-08T{hour:02d}:00",
                "temperature_2m": 26 + (18 - hour) / 10,
                "weather_code": 2,
                "precipitation_probability": 15,
            }
            for hour in range(15, 22)
        ],
    }
    values.update(overrides)
    return WeatherData(**values)


class _FailingRequest:
    async def __aenter__(self):
        raise RuntimeError("offline test: no network")

    async def __aexit__(self, *_args):
        return False


class _FailingSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def get(self, *_args, **_kwargs):
        return _FailingRequest()


class TestWeatherModels(unittest.TestCase):
    def test_location_info_display_name(self):
        loc1 = LocationInfo(name="Delhi", admin1="NCT", country="India", latitude=28.61, longitude=77.20)
        self.assertEqual(loc1.display_name, "Delhi, NCT, India")

        loc2 = LocationInfo(name="Singapore", admin1="Singapore", country="Singapore")
        self.assertEqual(loc2.display_name, "Singapore, Singapore")

    def test_temperature_and_wind_conversions(self):
        weather = sample_weather()
        self.assertAlmostEqual(weather.temp_f, 82.4, places=1)
        self.assertAlmostEqual(weather.feels_like_f, 86.0, places=1)
        self.assertAlmostEqual(weather.temp_min_f, 66.2, places=1)
        self.assertAlmostEqual(weather.temp_max_f, 87.8, places=1)
        self.assertAlmostEqual(weather.wind_speed_mph, 7.456, places=2)
        self.assertAlmostEqual(weather.visibility_miles, 6.21371, places=1)
        self.assertIsNone(WeatherData._to_fahrenheit(None))

    def test_degrees_to_cardinal(self):
        self.assertEqual(_degrees_to_cardinal(0), "N")
        self.assertEqual(_degrees_to_cardinal(90), "E")
        self.assertEqual(_degrees_to_cardinal(180), "S")
        self.assertEqual(_degrees_to_cardinal(270), "W")
        self.assertEqual(_degrees_to_cardinal(45), "NE")
        self.assertEqual(_degrees_to_cardinal(350), "N")


class TestWeatherProvider(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = OpenMeteoWeatherService(cache_ttl=60)

    async def test_empty_query_raises_error(self):
        with self.assertRaises(ParadiseError):
            await self.service.get_weather("")

    async def test_curated_city_lookup_falls_back_without_network(self):
        with patch("functions.weather.provider.aiohttp.ClientSession", return_value=_FailingSession()):
            loc = await self.service._resolve_location("Tokyo")
        self.assertIsNotNone(loc)
        self.assertEqual(loc.name, "Tokyo")
        self.assertEqual(loc.country, "Japan")

    async def test_fuzzy_city_resolution_is_offline(self):
        with patch("functions.weather.provider.aiohttp.ClientSession", return_value=_FailingSession()):
            loc_delhi = await self.service._resolve_location("Delhy")
            loc_tokyo = await self.service._resolve_location("Tokio")
            loc_mumbai = await self.service._resolve_location("Mumba")
            loc_london = await self.service._resolve_location("Londn")
        self.assertEqual(loc_delhi.name, "Delhi")
        self.assertEqual(loc_tokyo.name, "Tokyo")
        self.assertEqual(loc_mumbai.name, "Mumbai")
        self.assertEqual(loc_london.name, "London")

    async def test_weather_cache_keeps_successful_live_result(self):
        location = LocationInfo(name="Delhi", country="India", timezone="Asia/Kolkata")
        weather = sample_weather()
        self.service._resolve_location = AsyncMock(return_value=location)
        self.service._fetch_weather_data = AsyncMock(return_value=weather)

        first = await self.service.get_weather("Delhi")
        second = await self.service.get_weather("Delhi")
        self.assertIs(first, weather)
        self.assertIs(second, first)
        self.service._resolve_location.assert_awaited_once_with("Delhi")
        self.service._fetch_weather_data.assert_awaited_once_with(location)

    async def test_provider_outage_preserves_existing_fallback_behavior(self):
        location = LocationInfo(
            name="Delhi", latitude=28.61, country="India", timezone="Asia/Kolkata"
        )
        with patch("functions.weather.provider.aiohttp.ClientSession", return_value=_FailingSession()):
            weather = await self.service._fetch_weather_data(location)
        self.assertEqual(weather.condition, "Partly cloudy")
        self.assertEqual(weather.weather_code, 2)
        self.assertIsNotNone(weather.temp_c)

    async def test_partial_live_response_preserves_missing_values(self):
        location = LocationInfo(name="The Long City Name", country="Example", timezone="UTC")
        payload = {
            "current": {
                "time": "2026-10-08T15:00",
                "temperature_2m": 26.5,
                "weather_code": 999,
            },
            "hourly": {
                "time": ["2026-10-08T15:00", "2026-10-08T16:00"],
                "temperature_2m": [26.5, 27.0],
                "weather_code": [999, 2],
                "precipitation_probability": [None, 20],
            },
            "daily": {},
        }
        weather = self.service._parse_open_meteo_response(location, payload)
        self.assertEqual(weather.temp_c, 26.5)
        self.assertIsNone(weather.feels_like_c)
        self.assertIsNone(weather.humidity)
        self.assertIsNone(weather.wind_speed_kmh)
        self.assertIsNone(weather.temp_min_c)
        self.assertIsNone(weather.temp_max_c)
        self.assertIsNone(weather.sunrise)
        self.assertIsNone(weather.uv_index)
        self.assertEqual(weather.condition, "Conditions unavailable")
        self.assertEqual(weather.hourly_forecast[0]["temperature_2m"], 26.5)

    async def test_response_without_current_temperature_keeps_other_fields_optional(self):
        location = LocationInfo(name="Delhi", country="India", timezone="Asia/Kolkata")
        weather = self.service._parse_open_meteo_response(
            location, {"current": {"weather_code": 2}, "hourly": {}, "daily": {}}
        )
        self.assertIsNone(weather.temp_c)
        self.assertEqual(weather.condition, "Partly cloudy")
        self.assertIsNone(weather.humidity)
        self.assertIsNone(weather.wind_speed_kmh)


class TestWeatherUI(unittest.TestCase):
    def test_build_weather_embed_formats_available_measurements(self):
        embed = build_weather_embed(sample_weather())
        self.assertIn("Delhi", embed.title)
        self.assertIn("28.0°C", embed.fields[0].value)
        self.assertIn("82.4°F", embed.fields[0].value)
        self.assertIn("31.0°C", embed.fields[1].value)
        self.assertIn("12.0 km/h", embed.fields[2].value)
        self.assertIn("Asia/Kolkata", embed.footer.text)

    def test_missing_measurements_are_omitted_not_formatted_as_zero(self):
        weather = WeatherData(
            location=LocationInfo(name="A very long city name that should shrink", country="India"),
            temp_c=None,
            feels_like_c=None,
            condition="Conditions unavailable",
            weather_code=None,
        )
        embed = build_weather_embed(weather)
        self.assertEqual(embed.description, "Conditions unavailable")
        self.assertEqual(embed.fields, [])
        self.assertNotIn("0.0°C", str(embed.to_dict()))
        self.assertNotIn("None", str(embed.to_dict()))


class TestWeatherCard(unittest.TestCase):
    def assert_landscape_16_9(self, data: WeatherData):
        image = Image.open(BytesIO(render_weather_card(data)))
        self.assertEqual(image.size, (1200, 675))
        self.assertAlmostEqual(image.width / image.height, 16 / 9, places=6)

    def test_card_is_a_real_16_9_image(self):
        self.assert_landscape_16_9(sample_weather())

    def test_missing_values_and_unknown_condition_render_cleanly(self):
        weather = WeatherData(
            location=LocationInfo(name="Smalltown", country="Example", timezone="UTC"),
            temp_c=None,
            feels_like_c=None,
            condition="Conditions unavailable",
            condition_emoji="",
            weather_code=None,
            local_time="2026-10-08T14:05",
        )
        self.assert_landscape_16_9(weather)

    def test_long_city_name_does_not_break_card_dimensions(self):
        weather = sample_weather(
            location=LocationInfo(
                name="San Fernando del Valle de Catamarca in Argentina",
                admin1="Catamarca Province",
                country="Argentina",
                timezone="America/Argentina/Catamarca",
            )
        )
        self.assert_landscape_16_9(weather)


if __name__ == "__main__":
    unittest.main()
