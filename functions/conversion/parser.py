"""Natural language parser for conversion queries."""

from __future__ import annotations

import re

from functions.helpers import ParadiseError

_MAGNITUDES: dict[str, float] = {
    "k": 1_000.0,
    "m": 1_000_000.0,
    "b": 1_000_000_000.0,
}

# Numeric token accepting thousand separators ("1,000", "1,250,000.50"),
# plain decimals ("10.5"), or simple fractions ("3/4").
_NUM = r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:/\d+)?"
_SEP = r"(?:to|in|into|as|->|=>|:)"
_UNIT = r"[a-zA-Z°^/²³$€£¥₹₩%]+(?:\s+[a-zA-Z°^/²³]+)?"
_CURR_PREFIX = r"(?:CA\$|A\$|S\$|NZ\$|HK\$|R\$|US\$|Rs\.?|[$€£¥₹₩])"

# Prefix currency queries: "$100 to INR", "₹5,000 in USD", "-€12.50 to GBP", "$2.5k to INR".
_PREFIX_CURRENCY_RE = re.compile(
    rf"^(?P<sign>[-+]?)\s*(?P<from>{_CURR_PREFIX})\s*"
    rf"(?P<val>(?:\d{{1,3}}(?:,\d{{3}})+|\d+)(?:\.\d+)?(?:/\d+)?)"
    rf"(?P<mag>[kmb])?\s+{_SEP}\s+(?P<to>{_UNIT})$",
    re.IGNORECASE,
)

# Magnitude-suffixed currency queries: "2.5k USD to INR", "1.2m inr in usd".
_MAG_CURRENCY_RE = re.compile(
    rf"^(?P<val>{_NUM})(?P<mag>[kmb])\s+(?P<from>{_UNIT})\s+{_SEP}\s+(?P<to>{_UNIT})$",
    re.IGNORECASE,
)

# Compound imperial height/length: "5'11 to cm", "5'11\" in meters", "5 ft 11 in to cm".
_FEET_INCHES_RE = re.compile(
    rf"^(?P<ft>\d{{1,2}})\s*(?:'|′|ft|feet|foot)\s*"
    rf"(?P<inch>\d{{1,2}}(?:\.\d+)?)\s*(?:\"|″|in|inch|inches)?\s+"
    rf"{_SEP}\s+(?P<to>{_UNIT})$",
    re.IGNORECASE,
)

# Standard "<number> <from> to <to>" and compact "<number><from> to <to>".
_STANDARD_RE = re.compile(
    rf"^(?P<val>{_NUM})\s*(?P<from>{_UNIT})\s+{_SEP}\s+(?P<to>{_UNIT})$",
    re.IGNORECASE,
)
_COMPACT_RE = re.compile(
    rf"^(?P<val>{_NUM})(?P<from>[a-zA-Z°$€£¥₹₩%]+)\s+{_SEP}\s+(?P<to>{_UNIT})$",
    re.IGNORECASE,
)
# European decimal comma ("2,5 km to m") where digits after comma != 3.
_EURO_DECIMAL_RE = re.compile(
    rf"^(?P<val>[-+]?\d+,\d{{1,2}})\s*(?P<from>{_UNIT})\s+{_SEP}\s+(?P<to>{_UNIT})$",
    re.IGNORECASE,
)


def parse_fraction_or_float(val_str: str, *, magnitude: str = "") -> float:
    """Parse string as float or fraction, e.g. '1/2', '3.5', '1,000', '2.5' + 'k'."""
    cleaned = val_str.strip()
    if re.fullmatch(r"[-+]?\d+,\d{1,2}", cleaned):
        cleaned = cleaned.replace(",", ".")
    else:
        cleaned = cleaned.replace(",", "")
    if "/" in cleaned:
        parts = cleaned.split("/", 1)
        num = float(parts[0].strip())
        den = float(parts[1].strip())
        if den == 0:
            raise ParadiseError("Division by zero in conversion value.")
        base = num / den
    else:
        base = float(cleaned)
    mult = _MAGNITUDES.get((magnitude or "").lower(), 1.0)
    return base * mult


def parse_conversion_query(raw: str) -> tuple[float, str, str]:
    """Parse a freeform natural language query into ``(value, from_unit, to_unit)``.

    Examples:
        '10 km to miles'           -> (10.0, 'km', 'miles')
        '100 USD to INR'           -> (100.0, 'USD', 'INR')
        '1,250,000 INR to USD'     -> (1250000.0, 'INR', 'USD')
        '$100 to INR'              -> (100.0, '$', 'INR')
        '€50 in GBP'               -> (50.0, '€', 'GBP')
        '$2.5k to INR'             -> (2500.0, '$', 'INR')
        '5\\'11 to cm'              -> (71.0, 'in', 'cm')
        '25 celsius to fahrenheit' -> (25.0, 'celsius', 'fahrenheit')
        '-40 f to c'               -> (-40.0, 'f', 'c')
    """
    text = " ".join(str(raw or "").split()).strip(" ?!")
    if not text:
        raise ParadiseError(
            "Please specify what you want to convert, e.g. `@Paradise 10 km to miles`."
        )

    prefix = _PREFIX_CURRENCY_RE.match(text)
    if prefix is not None:
        sign = prefix.group("sign") or ""
        from_raw = prefix.group("from").strip().rstrip(".")
        to_raw = prefix.group("to").strip().rstrip(".")
        try:
            val = parse_fraction_or_float(
                f"{sign}{prefix.group('val')}",
                magnitude=prefix.group("mag") or "",
            )
        except ValueError as err:
            raise ParadiseError(f"Invalid number: `{prefix.group('val')}`") from err
        return val, from_raw, to_raw

    feet_inches = _FEET_INCHES_RE.match(text)
    if feet_inches is not None:
        feet = int(feet_inches.group("ft"))
        inches = float(feet_inches.group("inch"))
        return feet * 12.0 + inches, "in", feet_inches.group("to").strip()

    mag_match = _MAG_CURRENCY_RE.match(text)
    if mag_match is not None:
        try:
            val = parse_fraction_or_float(
                mag_match.group("val"),
                magnitude=mag_match.group("mag"),
            )
        except ValueError as err:
            raise ParadiseError(f"Invalid number: `{mag_match.group('val')}`") from err
        return val, mag_match.group("from").strip(), mag_match.group("to").strip()

    m = (
        _STANDARD_RE.match(text)
        or _COMPACT_RE.match(text)
        or _EURO_DECIMAL_RE.match(text)
    )
    if not m:
        # 3-part shorthand: "10 km miles", "$100 inr"
        short_prefix = re.match(
            rf"^(?P<sign>[-+]?)(?P<from>{_CURR_PREFIX})\s*"
            rf"(?P<val>(?:\d{{1,3}}(?:,\d{{3}})+|\d+)(?:\.\d+)?)(?P<mag>[kmb])?\s+"
            rf"(?P<to>{_UNIT})$",
            text,
            re.IGNORECASE,
        )
        if short_prefix is not None:
            val = parse_fraction_or_float(
                f"{short_prefix.group('sign')}{short_prefix.group('val')}",
                magnitude=short_prefix.group("mag") or "",
            )
            return (
                val,
                short_prefix.group("from").strip().rstrip("."),
                short_prefix.group("to").strip().rstrip("."),
            )
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
            "• `@Paradise 10 km to miles`\n"
            "• `@Paradise $100 to INR`\n"
            "• `@Paradise 25 celsius to fahrenheit`\n"
            "• `@Paradise 5'11 in cm`"
        )

    val_str = m.group("val")
    from_raw = m.group("from").strip().rstrip(".")
    to_raw = m.group("to").strip().rstrip(".")

    try:
        val = parse_fraction_or_float(val_str)
    except ValueError as err:
        raise ParadiseError(f"Invalid number: `{val_str}`") from err

    return val, from_raw, to_raw
