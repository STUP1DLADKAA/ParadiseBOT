"""Unit tests for the weather system, provider, geocoding, and UI."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from functions.helpers import ParadiseError
from functions.weather import (
    LocationInfo,
    OpenMeteoWeatherService,
    WeatherData,
    build_weather_embed,
)
from functions.weather.provider import _degrees_to_cardinal


class TestWeatherModels(unittest.TestCase):
    def test_location_info_display_name(self):
        loc1 = LocationInfo(name="Delhi", admin1="NCT", country="India", latitude=28.61, longitude=77.20)
        self.assertEqual(loc1.display_name, "Delhi, NCT, India")

        loc2 = LocationInfo(name="Singapore", admin1="Singapore", country="Singapore")
        self.assertEqual(loc2.display_name, "Singapore, Singapore")

    def test_weather_data_temperature_conversions(self):
        loc = LocationInfo(name="London", country="United Kingdom")
        w = WeatherData(
            location=loc,
            temp_c=20.0,
            feels_like_c=22.0,
            condition="Partly cloudy",
            condition_emoji="⛅",
            humidity=60,
            wind_speed_kmh=10.0,
            wind_direction="NE",
            temp_min_c=15.0,
            temp_max_c=25.0,
            visibility_km=10.0,
        )

        self.assertAlmostEqual(w.temp_f, 68.0, places=1)
        self.assertAlmostEqual(w.feels_like_f, 71.6, places=1)
        self.assertAlmostEqual(w.temp_min_f, 59.0, places=1)
        self.assertAlmostEqual(w.temp_max_f, 77.0, places=1)
        self.assertAlmostEqual(w.wind_speed_mph, 6.21371, places=1)
        self.assertAlmostEqual(w.visibility_miles, 6.21371, places=1)

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

    async def test_resolve_curated_city(self):
        loc = await self.service._resolve_location("Tokyo")
        self.assertIsNotNone(loc)
        self.assertEqual(loc.name, "Tokyo")
        self.assertEqual(loc.country, "Japan")

    async def test_fuzzy_city_resolution(self):
        loc_delhi = await self.service._resolve_location("Delhy")
        self.assertIsNotNone(loc_delhi)
        self.assertEqual(loc_delhi.name, "Delhi")

        loc_tokyo = await self.service._resolve_location("Tokio")
        self.assertIsNotNone(loc_tokyo)
        self.assertEqual(loc_tokyo.name, "Tokyo")

        loc_mumbai = await self.service._resolve_location("Mumba")
        self.assertIsNotNone(loc_mumbai)
        self.assertEqual(loc_mumbai.name, "Mumbai")

        loc_london = await self.service._resolve_location("Londn")
        self.assertIsNotNone(loc_london)
        self.assertEqual(loc_london.name, "London")

    async def test_fallback_weather_generation(self):
        loc = LocationInfo(name="Delhi", country="India", latitude=28.61, longitude=77.20, timezone="Asia/Kolkata")
        w = self.service._generate_fallback_weather(loc)
        self.assertIsNotNone(w)
        self.assertEqual(w.location.name, "Delhi")
        self.assertGreater(w.temp_c, -50)
        self.assertGreater(w.humidity, 0)
        self.assertIn("Delhi", w.location.display_name)

    async def test_get_weather_caching(self):
        w1 = await self.service.get_weather("Delhi")
        self.assertIsNotNone(w1)

        w2 = await self.service.get_weather("Delhi")
        self.assertIs(w1, w2)


class TestWeatherUI(unittest.TestCase):
    def test_build_weather_embed(self):
        loc = LocationInfo(name="Delhi", admin1="NCT", country="India", latitude=28.61, longitude=77.20, timezone="Asia/Kolkata")
        w = WeatherData(
            location=loc,
            temp_c=28.0,
            feels_like_c=30.0,
            condition="Partly cloudy",
            condition_emoji="⛅",
            humidity=65,
            wind_speed_kmh=12.0,
            wind_direction="NE",
            temp_min_c=19.0,
            temp_max_c=31.0,
            visibility_km=10.0,
            sunrise="06:14",
            sunset="18:22",
            uv_index=6.5,
            forecast_text="Partly cloudy with pleasant temperatures.",
        )

        embed = build_weather_embed(w)
        self.assertIn("Delhi", embed.title)
        self.assertIn("28.0°C", embed.fields[0].value)
        self.assertIn("82.4°F", embed.fields[0].value)
        self.assertIn("31.0°C", embed.fields[1].value)
        self.assertIn("12.0 km/h", embed.fields[2].value)
        self.assertIn("Asia/Kolkata", embed.footer.text)


if __name__ == "__main__":
    unittest.main()
