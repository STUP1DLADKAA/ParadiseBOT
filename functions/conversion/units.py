"""Unit conversion tables, factors, and formulas."""

from __future__ import annotations

import math
from typing import NamedTuple

from .models import ConversionCategory, ConversionResult


class UnitDef(NamedTuple):
    canonical: str
    category: ConversionCategory
    to_base: float  # Multiply by this to get base unit
    symbol: str


# Base units:
# Length: meter (m)
# Weight: gram (g)
# Speed: m/s
# Time: second (s)
# Volume: liter (L)
# Area: square meter (m²)
# Data: byte (B)

UNITS_DB: dict[str, UnitDef] = {
    # Length / Distance (Base: meter)
    "mm": UnitDef("mm", ConversionCategory.LENGTH, 0.001, "mm"),
    "cm": UnitDef("cm", ConversionCategory.LENGTH, 0.01, "cm"),
    "m": UnitDef("m", ConversionCategory.LENGTH, 1.0, "m"),
    "km": UnitDef("km", ConversionCategory.LENGTH, 1000.0, "km"),
    "in": UnitDef("in", ConversionCategory.LENGTH, 0.0254, "in"),
    "ft": UnitDef("ft", ConversionCategory.LENGTH, 0.3048, "ft"),
    "yd": UnitDef("yd", ConversionCategory.LENGTH, 0.9144, "yd"),
    "mi": UnitDef("mi", ConversionCategory.LENGTH, 1609.344, "mi"),
    "nm": UnitDef("nm", ConversionCategory.LENGTH, 1852.0, "nmi"),

    # Weight / Mass (Base: gram)
    "mg": UnitDef("mg", ConversionCategory.WEIGHT, 0.001, "mg"),
    "g": UnitDef("g", ConversionCategory.WEIGHT, 1.0, "g"),
    "kg": UnitDef("kg", ConversionCategory.WEIGHT, 1000.0, "kg"),
    "t": UnitDef("t", ConversionCategory.WEIGHT, 1_000_000.0, "t"),
    "oz": UnitDef("oz", ConversionCategory.WEIGHT, 28.349523125, "oz"),
    "lb": UnitDef("lb", ConversionCategory.WEIGHT, 453.59237, "lb"),
    "st": UnitDef("st", ConversionCategory.WEIGHT, 6350.29318, "st"),

    # Speed (Base: m/s)
    "m/s": UnitDef("m/s", ConversionCategory.SPEED, 1.0, "m/s"),
    "km/h": UnitDef("km/h", ConversionCategory.SPEED, 1.0 / 3.6, "km/h"),
    "mph": UnitDef("mph", ConversionCategory.SPEED, 0.44704, "mph"),
    "knots": UnitDef("knots", ConversionCategory.SPEED, 0.514444, "kn"),
    "ft/s": UnitDef("ft/s", ConversionCategory.SPEED, 0.3048, "ft/s"),

    # Time (Base: second)
    "ms": UnitDef("ms", ConversionCategory.TIME, 0.001, "ms"),
    "s": UnitDef("s", ConversionCategory.TIME, 1.0, "s"),
    "min": UnitDef("min", ConversionCategory.TIME, 60.0, "min"),
    "h": UnitDef("h", ConversionCategory.TIME, 3600.0, "h"),
    "d": UnitDef("d", ConversionCategory.TIME, 86400.0, "d"),
    "w": UnitDef("w", ConversionCategory.TIME, 604800.0, "w"),
    "mo": UnitDef("mo", ConversionCategory.TIME, 2629800.0, "mo"),  # avg month
    "y": UnitDef("y", ConversionCategory.TIME, 31557600.0, "y"),  # avg 365.25 days

    # Volume (Base: liter)
    "ml": UnitDef("ml", ConversionCategory.VOLUME, 0.001, "ml"),
    "l": UnitDef("l", ConversionCategory.VOLUME, 1.0, "L"),
    "tsp": UnitDef("tsp", ConversionCategory.VOLUME, 0.00492892, "tsp"),
    "tbsp": UnitDef("tbsp", ConversionCategory.VOLUME, 0.0147868, "tbsp"),
    "floz": UnitDef("floz", ConversionCategory.VOLUME, 0.0295735, "fl oz"),
    "cup": UnitDef("cup", ConversionCategory.VOLUME, 0.236588, "cup"),
    "pt": UnitDef("pt", ConversionCategory.VOLUME, 0.473176, "pt"),
    "qt": UnitDef("qt", ConversionCategory.VOLUME, 0.946353, "qt"),
    "gal": UnitDef("gal", ConversionCategory.VOLUME, 3.78541, "gal"),
    "m3": UnitDef("m3", ConversionCategory.VOLUME, 1000.0, "m³"),
    "cm3": UnitDef("cm3", ConversionCategory.VOLUME, 0.001, "cm³"),

    # Area (Base: square meter)
    "sqmm": UnitDef("sqmm", ConversionCategory.AREA, 0.000001, "mm²"),
    "sqcm": UnitDef("sqcm", ConversionCategory.AREA, 0.0001, "cm²"),
    "sqm": UnitDef("sqm", ConversionCategory.AREA, 1.0, "m²"),
    "sqkm": UnitDef("sqkm", ConversionCategory.AREA, 1_000_000.0, "km²"),
    "sqin": UnitDef("sqin", ConversionCategory.AREA, 0.00064516, "in²"),
    "sqft": UnitDef("sqft", ConversionCategory.AREA, 0.092903, "ft²"),
    "sqyd": UnitDef("sqyd", ConversionCategory.AREA, 0.836127, "yd²"),
    "sqmi": UnitDef("sqmi", ConversionCategory.AREA, 2589988.11, "mi²"),
    "acre": UnitDef("acre", ConversionCategory.AREA, 4046.86, "ac"),
    "ha": UnitDef("ha", ConversionCategory.AREA, 10000.0, "ha"),

    # Data / Storage (Base: Byte)
    "b": UnitDef("b", ConversionCategory.DATA, 1.0 / 8.0, "bit"),
    "byte": UnitDef("byte", ConversionCategory.DATA, 1.0, "B"),
    "kb": UnitDef("kb", ConversionCategory.DATA, 1000.0, "KB"),
    "mb": UnitDef("mb", ConversionCategory.DATA, 1_000_000.0, "MB"),
    "gb": UnitDef("gb", ConversionCategory.DATA, 1_000_000_000.0, "GB"),
    "tb": UnitDef("tb", ConversionCategory.DATA, 1_000_000_000_000.0, "TB"),
    "pb": UnitDef("pb", ConversionCategory.DATA, 1_000_000_000_000_000.0, "PB"),
    "kib": UnitDef("kib", ConversionCategory.DATA, 1024.0, "KiB"),
    "mib": UnitDef("mib", ConversionCategory.DATA, 1048576.0, "MiB"),
    "gib": UnitDef("gib", ConversionCategory.DATA, 1073741824.0, "GiB"),
    "tib": UnitDef("tib", ConversionCategory.DATA, 1099511627776.0, "TiB"),
    "kbit": UnitDef("kbit", ConversionCategory.DATA, 1000.0 / 8.0, "kbit"),
    "mbit": UnitDef("mbit", ConversionCategory.DATA, 1_000_000.0 / 8.0, "Mbit"),
    "gbit": UnitDef("gbit", ConversionCategory.DATA, 1_000_000_000.0 / 8.0, "Gbit"),
}

# Unit alias mappings
UNIT_ALIASES: dict[str, str] = {
    # Length
    "millimeter": "mm", "millimeters": "mm",
    "centimeter": "cm", "centimeters": "cm",
    "meter": "m", "meters": "m", "metre": "m", "metres": "m",
    "kilometer": "km", "kilometers": "km", "kilometre": "km", "kilometres": "km",
    "inch": "in", "inches": "in", '"': "in",
    "foot": "ft", "feet": "ft", "'": "ft",
    "yard": "yd", "yards": "yd",
    "mile": "mi", "miles": "mi",
    "nauticalmile": "nm", "nauticalmiles": "nm", "nmi": "nm",

    # Weight
    "milligram": "mg", "milligrams": "mg",
    "gram": "g", "grams": "g",
    "kilogram": "kg", "kilograms": "kg", "kilo": "kg", "kilos": "kg",
    "ton": "t", "tons": "t", "tonne": "t", "tonnes": "t", "metricton": "t",
    "ounce": "oz", "ounces": "oz",
    "pound": "lb", "pounds": "lb", "lbs": "lb",
    "stone": "st", "stones": "st",

    # Speed
    "kph": "km/h", "kmph": "km/h", "km/hr": "km/h", "kilometerperhour": "km/h", "kilometersperhour": "km/h",
    "milesperhour": "mph", "mi/h": "mph", "mi/hr": "mph",
    "mps": "m/s", "meterpersecond": "m/s", "meterspersecond": "m/s",
    "knot": "knots", "kt": "knots", "kts": "knots",
    "fps": "ft/s", "feetpersecond": "ft/s",

    # Time
    "millisecond": "ms", "milliseconds": "ms",
    "second": "s", "seconds": "s", "sec": "s", "secs": "s",
    "minute": "min", "minutes": "min", "mins": "min",
    "hour": "h", "hours": "h", "hr": "h", "hrs": "h",
    "day": "d", "days": "d",
    "week": "w", "weeks": "w", "wk": "w", "wks": "w",
    "month": "mo", "months": "mo",
    "year": "y", "years": "y", "yr": "y", "yrs": "y",

    # Volume
    "milliliter": "ml", "milliliters": "ml", "millilitre": "ml", "millilitres": "ml",
    "liter": "l", "liters": "l", "litre": "l", "litres": "l",
    "teaspoon": "tsp", "teaspoons": "tsp",
    "tablespoon": "tbsp", "tablespoons": "tbsp",
    "fluidounce": "floz", "fluidounces": "floz", "fl oz": "floz", "floz": "floz",
    "cups": "cup",
    "pint": "pt", "pints": "pt",
    "quart": "qt", "quarts": "qt",
    "gallon": "gal", "gallons": "gal",
    "m^3": "m3", "cubicmeter": "m3", "cubicmeters": "m3",
    "cm^3": "cm3", "cubiccentimeter": "cm3", "cubiccentimeters": "cm3", "cc": "cm3",

    # Area
    "sqmm": "sqmm", "squaremillimeter": "sqmm", "squaremillimeters": "sqmm", "mm2": "sqmm", "mm^2": "sqmm",
    "sqcm": "sqcm", "squarecentimeter": "sqcm", "squarecentimeters": "sqcm", "cm2": "sqcm", "cm^2": "sqcm",
    "sqm": "sqm", "squaremeter": "sqm", "squaremeters": "sqm", "squaremetre": "sqm", "m2": "sqm", "m^2": "sqm",
    "sqkm": "sqkm", "squarekilometer": "sqkm", "squarekilometers": "sqkm", "km2": "sqkm", "km^2": "sqkm",
    "sqin": "sqin", "squareinch": "sqin", "squareinches": "sqin", "in2": "sqin", "in^2": "sqin",
    "sqft": "sqft", "squarefoot": "sqft", "squarefeet": "sqft", "ft2": "sqft", "ft^2": "sqft",
    "sqyd": "sqyd", "squareyard": "sqyd", "squareyards": "sqyd", "yd2": "sqyd", "yd^2": "sqyd",
    "sqmi": "sqmi", "squaremile": "sqmi", "squaremiles": "sqmi", "mi2": "sqmi", "mi^2": "sqmi",
    "acres": "acre",
    "hectare": "ha", "hectares": "ha",

    # Data
    "bit": "b", "bits": "b",
    "bytes": "byte", "b": "byte",  # context default B=Byte
    "kilobyte": "kb", "kilobytes": "kb",
    "megabyte": "mb", "megabytes": "mb",
    "gigabyte": "gb", "gigabytes": "gb",
    "terabyte": "tb", "terabytes": "tb",
    "petabyte": "pb", "petabytes": "pb",
    "kibibyte": "kib", "kibibytes": "kib",
    "mebibyte": "mib", "mebibytes": "mib",
    "gibibyte": "gib", "gibibytes": "gib",
    "tebibyte": "tib", "tebibytes": "tib",
    "kilobit": "kbit", "kilobits": "kbit",
    "megabit": "mbit", "megabits": "mbit",
    "gigabit": "gbit", "gigabits": "gbit",
}

TEMP_UNITS = {"c", "celsius", "centigrade", "f", "fahrenheit", "k", "kelvin"}


def resolve_unit(name: str) -> str | None:
    """Normalize unit string to canonical key."""
    clean = name.strip().lower().replace(" ", "").replace("_", "").replace("-", "")
    # Degree signs can arrive on either side ("°C", "25C°") and no unit key
    # contains one, so drop them before matching.
    clean = clean.replace("°", "").replace("º", "").replace("∘", "")
    if clean in UNITS_DB:
        return clean
    if clean in UNIT_ALIASES:
        return UNIT_ALIASES[clean]
    if clean in ("celsius", "c", "centigrade"):
        return "celsius"
    if clean in ("fahrenheit", "f"):
        return "fahrenheit"
    if clean in ("kelvin", "k"):
        return "kelvin"
    return None


def convert_temperature(val: float, from_u: str, to_u: str) -> ConversionResult:
    """Handle non-linear temperature conversions."""
    # Convert from_u to Celsius
    if from_u in ("c", "celsius", "centigrade"):
        c_val = val
        from_canon = "Celsius"
        from_sym = "°C"
    elif from_u in ("f", "fahrenheit"):
        c_val = (val - 32) * 5 / 9
        from_canon = "Fahrenheit"
        from_sym = "°F"
    elif from_u in ("k", "kelvin"):
        c_val = val - 273.15
        from_canon = "Kelvin"
        from_sym = "K"
    else:
        raise ValueError(f"Unknown temperature unit {from_u}")

    # Convert Celsius to to_u
    if to_u in ("c", "celsius", "centigrade"):
        result_val = c_val
        to_canon = "Celsius"
        to_sym = "°C"
    elif to_u in ("f", "fahrenheit"):
        result_val = (c_val * 9 / 5) + 32
        to_canon = "Fahrenheit"
        to_sym = "°F"
    elif to_u in ("k", "kelvin"):
        result_val = c_val + 273.15
        to_canon = "Kelvin"
        to_sym = "K"
    else:
        raise ValueError(f"Unknown temperature unit {to_u}")

    return ConversionResult(
        from_value=val,
        from_unit=from_canon,
        from_symbol=from_sym,
        to_value=result_val,
        to_unit=to_canon,
        to_symbol=to_sym,
        category=ConversionCategory.TEMPERATURE,
    )


def convert_standard_unit(val: float, from_key: str, to_key: str) -> ConversionResult:
    """Convert value between two standard linear units in the same category."""
    unit_a = UNITS_DB[from_key]
    unit_b = UNITS_DB[to_key]

    if unit_a.category != unit_b.category:
        raise ValueError(
            f"Cannot convert between {unit_a.category.value} and {unit_b.category.value}."
        )

    base_val = val * unit_a.to_base
    result_val = base_val / unit_b.to_base

    return ConversionResult(
        from_value=val,
        from_unit=unit_a.canonical,
        from_symbol=unit_a.symbol,
        to_value=result_val,
        to_unit=unit_b.canonical,
        to_symbol=unit_b.symbol,
        category=unit_a.category,
    )
