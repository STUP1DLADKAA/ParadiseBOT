"""Unit tests for the unit and currency conversion engine, parser, and models."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from functions.conversion import (
    ConversionCategory,
    ConversionEngine,
    build_conversion_embed,
    parse_conversion_query,
)
from functions.conversion.currency import CurrencyService, resolve_currency
from functions.conversion.parser import parse_fraction_or_float
from functions.conversion.units import convert_standard_unit, convert_temperature, resolve_unit
from functions.helpers import ParadiseError


class TestConversionParser(unittest.TestCase):
    def test_parse_fractions_and_floats(self):
        self.assertEqual(parse_fraction_or_float("1/2"), 0.5)
        self.assertEqual(parse_fraction_or_float("3/4"), 0.75)
        self.assertEqual(parse_fraction_or_float("10.5"), 10.5)
        self.assertEqual(parse_fraction_or_float("1,000"), 1000.0)

    def test_parse_natural_queries(self):
        # 10 km to miles
        v, f, t = parse_conversion_query("10 km to miles")
        self.assertEqual(v, 10.0)
        self.assertEqual(f, "km")
        self.assertEqual(t, "miles")

        # 100 USD to INR
        v, f, t = parse_conversion_query("100 USD to INR")
        self.assertEqual(v, 100.0)
        self.assertEqual(f, "USD")
        self.assertEqual(t, "INR")

        # 25 celsius to fahrenheit
        v, f, t = parse_conversion_query("25 celsius to fahrenheit")
        self.assertEqual(v, 25.0)
        self.assertEqual(f, "celsius")
        self.assertEqual(t, "fahrenheit")

        # 5 kg to lb
        v, f, t = parse_conversion_query("5 kg to lb")
        self.assertEqual(v, 5.0)
        self.assertEqual(f, "kg")
        self.assertEqual(t, "lb")

        # 10km in mi
        v, f, t = parse_conversion_query("10km in mi")
        self.assertEqual(v, 10.0)
        self.assertEqual(f, "km")
        self.assertEqual(t, "mi")

        # 50 mph -> km/h
        v, f, t = parse_conversion_query("50 mph -> km/h")
        self.assertEqual(v, 50.0)
        self.assertEqual(f, "mph")
        self.assertEqual(t, "km/h")

        # -40 f to c
        v, f, t = parse_conversion_query("-40 f to c")
        self.assertEqual(v, -40.0)
        self.assertEqual(f, "f")
        self.assertEqual(t, "c")

    def test_invalid_query_raises_error(self):
        with self.assertRaises(ParadiseError):
            parse_conversion_query("hello world")

        with self.assertRaises(ParadiseError):
            parse_conversion_query("")


class TestUnitConversions(unittest.TestCase):
    def test_length_distance(self):
        res = convert_standard_unit(10.0, "km", "mi")
        self.assertAlmostEqual(res.to_value, 6.21371, places=3)
        self.assertEqual(res.category, ConversionCategory.LENGTH)

        res2 = convert_standard_unit(1.0, "mi", "ft")
        self.assertAlmostEqual(res2.to_value, 5280.0, places=1)

    def test_weight_mass(self):
        res = convert_standard_unit(5.0, "kg", "lb")
        self.assertAlmostEqual(res.to_value, 11.0231, places=3)
        self.assertEqual(res.category, ConversionCategory.WEIGHT)

    def test_temperature(self):
        res = convert_temperature(25.0, "c", "f")
        self.assertEqual(res.to_value, 77.0)
        self.assertEqual(res.category, ConversionCategory.TEMPERATURE)

        res_neg = convert_temperature(-40.0, "f", "c")
        self.assertEqual(res_neg.to_value, -40.0)

        res_k = convert_temperature(0.0, "c", "k")
        self.assertEqual(res_k.to_value, 273.15)

    def test_speed(self):
        res = convert_standard_unit(60.0, "mph", "km/h")
        self.assertAlmostEqual(res.to_value, 96.5606, places=2)

    def test_time(self):
        res = convert_standard_unit(2.0, "h", "min")
        self.assertEqual(res.to_value, 120.0)

    def test_volume(self):
        res = convert_standard_unit(1.0, "gal", "l")
        self.assertAlmostEqual(res.to_value, 3.78541, places=3)

    def test_area(self):
        res = convert_standard_unit(1000.0, "sqft", "sqm")
        self.assertAlmostEqual(res.to_value, 92.903, places=2)

    def test_data_storage(self):
        res = convert_standard_unit(1.0, "gb", "mb")
        self.assertEqual(res.to_value, 1000.0)

        res2 = convert_standard_unit(1.0, "gib", "mib")
        self.assertEqual(res2.to_value, 1024.0)

    def test_incompatible_categories_raise_error(self):
        with self.assertRaises(ValueError):
            convert_standard_unit(10.0, "km", "kg")


class TestCurrencyConversions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = CurrencyService()

    async def test_currency_resolution(self):
        self.assertEqual(resolve_currency("USD"), "usd")
        self.assertEqual(resolve_currency("dollars"), "usd")
        self.assertEqual(resolve_currency("$"), "usd")
        self.assertEqual(resolve_currency("INR"), "inr")
        self.assertEqual(resolve_currency("rupees"), "inr")
        self.assertEqual(resolve_currency("₹"), "inr")
        self.assertEqual(resolve_currency("EUR"), "eur")
        self.assertEqual(resolve_currency("euros"), "eur")
        self.assertEqual(resolve_currency("€"), "eur")

    async def test_currency_conversion_fallback(self):
        res = await self.service.convert(10.0, "USD", "INR")
        self.assertEqual(res.from_unit, "USD")
        self.assertEqual(res.to_unit, "INR")
        self.assertGreater(res.to_value, 500.0)
        self.assertIn("USD →", res.display_text)
        self.assertIn("INR", res.display_text)

    async def test_same_currency(self):
        res = await self.service.convert(50.0, "USD", "USD")
        self.assertEqual(res.to_value, 50.0)


class TestConversionEngine(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = ConversionEngine()

    async def test_convert_query_units(self):
        res = await self.engine.convert_query("10 km to miles")
        self.assertEqual(res.category, ConversionCategory.LENGTH)
        self.assertAlmostEqual(res.to_value, 6.21371, places=3)

    async def test_convert_query_currency(self):
        res = await self.engine.convert_query("100 USD to INR")
        self.assertEqual(res.category, ConversionCategory.CURRENCY)
        self.assertEqual(res.from_unit, "USD")
        self.assertEqual(res.to_unit, "INR")

    async def test_convert_query_temp(self):
        res = await self.engine.convert_query("25 celsius to fahrenheit")
        self.assertEqual(res.category, ConversionCategory.TEMPERATURE)
        self.assertEqual(res.to_value, 77.0)

    def test_build_conversion_embed(self):
        res = convert_standard_unit(10.0, "km", "mi")
        embed = build_conversion_embed(res)
        self.assertIn("Length & Distance", embed.title)
        self.assertIn("10 km → 6.2137 mi", embed.description)


if __name__ == "__main__":
    unittest.main()
