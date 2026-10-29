"""Natural language parser for conversion queries."""

from __future__ import annotations

import re

from functions.helpers import ParadiseError


def parse_fraction_or_float(val_str: str) -> float:
    """Parse string as float or fraction, e.g. '1/2', '3.5', '1,000'."""
    val_str = val_str.replace(",", "").strip()
    if "/" in val_str:
        parts = val_str.split("/", 1)
        num = float(parts[0].strip())
        den = float(parts[1].strip())
        if den == 0:
            raise ParadiseError("Division by zero in conversion value.")
        return num / den
    return float(val_str)


def parse_conversion_query(raw: str) -> tuple[float, str, str]:
    """
    Parse a freeform natural language query into (value, from_unit, to_unit).
    
    Examples:
        '10 km to miles' -> (10.0, 'km', 'miles')
        '100 USD to INR' -> (100.0, 'USD', 'INR')
        '25 celsius to fahrenheit' -> (25.0, 'celsius', 'fahrenheit')
        '5 kg to lb' -> (5.0, 'kg', 'lb')
        '10km in mi' -> (10.0, 'km', 'mi')
        '50 mph -> km/h' -> (50.0, 'mph', 'km/h')
        '25c to f' -> (25.0, 'c', 'f')
        '-40 f to c' -> (-40.0, 'f', 'c')
    """
    text = raw.strip()
    if not text:
        raise ParadiseError("Please specify what you want to convert, e.g. `/convert 10 km to miles`.")

    # Match separator keywords: 'to', 'in', 'into', 'as', '->', '=>', ':'
    # E.g.: "10 km to miles", "100 usd in inr", "25 celsius -> fahrenheit"
    pattern = r"^(?P<val>[-+]?\d+(?:[.,]\d+)?(?:\/\d+)?)\s*(?P<from>[a-zA-Z°^/²³$€£¥₹%]+(?:\s+[a-zA-Z°^/²³]+)?)\s+(?:to|in|into|as|->|=>|:)\s+(?P<to>[a-zA-Z°^/²³$€£¥₹%]+(?:\s+[a-zA-Z°^/²³]+)?)$"
    m = re.match(pattern, text, re.IGNORECASE)
    
    if not m:
        # Check without space between number and unit, e.g. "10km to miles", "25C to F"
        alt_pattern = r"^(?P<val>[-+]?\d+(?:[.,]\d+)?(?:\/\d+)?)(?P<from>[a-zA-Z°$€£¥₹%]+)\s+(?:to|in|into|as|->|=>|:)\s+(?P<to>[a-zA-Z°^/²³$€£¥₹%]+(?:\s+[a-zA-Z°^/²³]+)?)$"
        m = re.match(alt_pattern, text, re.IGNORECASE)

    if not m:
        # Check 3-part split: "10 km miles"
        parts = text.split()
        if len(parts) == 3:
            try:
                val = parse_fraction_or_float(parts[0])
                return val, parts[1], parts[2]
            except ValueError:
                pass
        raise ParadiseError(
            f"Couldn't understand conversion: `{text}`\n"
            "Try formats like:\n"
            "• `/convert 10 km to miles`\n"
            "• `/convert 100 USD to INR`\n"
            "• `/convert 25 celsius to fahrenheit`\n"
            "• `/convert 5 kg to lb`"
        )

    val_str = m.group("val")
    from_raw = m.group("from").strip()
    to_raw = m.group("to").strip()

    try:
        val = parse_fraction_or_float(val_str)
    except ValueError:
        raise ParadiseError(f"Invalid number: `{val_str}`")

    return val, from_raw, to_raw
