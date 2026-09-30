"""Unit tests for time, timezone resolution, formatting, and UI."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from functions.helpers import ParadiseError
from functions.time import (
    CITY_TIMEZONES,
    TZ_ABBREVIATIONS,
    build_time_embed,
    get_current_time_for_zone,
    resolve_timezone,
)


class TestTimezones(unittest.TestCase):
    def test_resolve_standard_iana(self):
        self.assertEqual(resolve_timezone("Asia/Kolkata"), "Asia/Kolkata")
        self.assertEqual(resolve_timezone("Europe/London"), "Europe/London")
        self.assertEqual(resolve_timezone("America/New_York"), "America/New_York")

    def test_resolve_abbreviations(self):
        self.assertEqual(resolve_timezone("IST"), "Asia/Kolkata")
        self.assertEqual(resolve_timezone("EST"), "America/New_York")
        self.assertEqual(resolve_timezone("PST"), "America/Los_Angeles")
        self.assertEqual(resolve_timezone("GMT"), "UTC")
        self.assertEqual(resolve_timezone("UTC"), "UTC")
        self.assertEqual(resolve_timezone("CET"), "Europe/Paris")
        self.assertEqual(resolve_timezone("JST"), "Asia/Tokyo")

    def test_resolve_cities(self):
        self.assertEqual(resolve_timezone("Delhi"), "Asia/Kolkata")
        self.assertEqual(resolve_timezone("Mumbai"), "Asia/Kolkata")
        self.assertEqual(resolve_timezone("London"), "Europe/London")
        self.assertEqual(resolve_timezone("Tokyo"), "Asia/Tokyo")
        self.assertEqual(resolve_timezone("New York"), "America/New_York")
        self.assertEqual(resolve_timezone("Paris"), "Europe/Paris")
        self.assertEqual(resolve_timezone("Sydney"), "Australia/Sydney")

    def test_fuzzy_city_resolution(self):
        self.assertEqual(resolve_timezone("delhy"), "Asia/Kolkata")
        self.assertEqual(resolve_timezone("tokio"), "Asia/Tokyo")
        self.assertEqual(resolve_timezone("londn"), "Europe/London")

    def test_get_current_time_for_zone(self):
        info = get_current_time_for_zone("Asia/Kolkata", label="Delhi")
        self.assertEqual(info.tz_name, "Asia/Kolkata")
        self.assertEqual(info.location_or_target, "Delhi")
        self.assertEqual(info.utc_offset, "UTC+05:30")
        self.assertIn(":", info.formatted_12h)
        self.assertIn(":", info.formatted_24h)
        self.assertGreater(info.epoch_timestamp, 0)

    def test_invalid_timezone_raises_error(self):
        with self.assertRaises(ParadiseError):
            get_current_time_for_zone("Invalid/Unknown_Zone_123")

    def test_build_time_embed(self):
        info = get_current_time_for_zone("Europe/London", label="London")
        embed = build_time_embed(info)
        self.assertIn("London", embed.title)
        self.assertIn(info.formatted_12h, embed.fields[0].value)
        self.assertIn("Europe/London", embed.fields[2].value)

    def test_build_time_embed_for_member(self):
        mock_user = MagicMock()
        mock_user.display_name = "Alex"
        mock_user.display_avatar.url = "https://example.com/avatar.png"

        info = get_current_time_for_zone("Asia/Kolkata", label="Alex")
        embed = build_time_embed(info, user_target=mock_user)
        self.assertIn("Alex", embed.title)
        self.assertIn("Asia/Kolkata", embed.fields[2].value)


if __name__ == "__main__":
    unittest.main()
