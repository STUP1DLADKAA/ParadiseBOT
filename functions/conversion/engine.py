"""Conversion engine coordinating units, temperatures, and currency conversions."""

from __future__ import annotations

import logging

from functions.helpers import ParadiseError
from .currency import BASE_USD_RATES, CURRENCY_ALIASES, CURRENCY_SYMBOLS, CurrencyService, resolve_currency
from .models import ConversionResult
from .parser import parse_conversion_query
from .units import TEMP_UNITS, convert_standard_unit, convert_temperature, resolve_unit

log = logging.getLogger(__name__)


class ConversionEngine:
    """Master conversion service."""

    def __init__(self) -> None:
        self.currency_service = CurrencyService()

    async def convert_query(self, query: str) -> ConversionResult:
        """Parse natural language query and perform conversion."""
        val, from_raw, to_raw = parse_conversion_query(query)
        return await self.convert(val, from_raw, to_raw)

    async def convert(self, val: float, from_unit_raw: str, to_unit_raw: str) -> ConversionResult:
        """Perform conversion given value, from_unit, and to_unit."""
        # 1. Check temperature ("°C", "25C°" and bare "c" all normalize).
        from_clean = from_unit_raw.strip().lower().strip("°º∘")
        to_clean = to_unit_raw.strip().lower().strip("°º∘")
        if from_clean in TEMP_UNITS and to_clean in TEMP_UNITS:
            return convert_temperature(val, from_clean, to_clean)

        # 2. Check standard units before speculative 3-letter ISO currency codes
        # so queries like "10 min to sec" or "2 gal to cup" never get mistaken
        # for unknown currencies ("MIN" -> "SEC").
        known_currencies = (
            set(BASE_USD_RATES) | set(CURRENCY_SYMBOLS) | set(CURRENCY_ALIASES)
        )
        from_key = from_unit_raw.strip().rstrip(".").lower().replace(" ", "")
        to_key = to_unit_raw.strip().rstrip(".").lower().replace(" ", "")
        explicit_currency = from_key in known_currencies or to_key in known_currencies

        if not explicit_currency:
            from_u = resolve_unit(from_unit_raw)
            to_u = resolve_unit(to_unit_raw)
            if from_u and to_u:
                if from_u in ("celsius", "fahrenheit", "kelvin") and to_u in (
                    "celsius", "fahrenheit", "kelvin"
                ):
                    return convert_temperature(val, from_u, to_u)
                try:
                    return convert_standard_unit(val, from_u, to_u)
                except ValueError as err:
                    raise ParadiseError(str(err)) from err

        # 3. Currency conversion.
        from_curr = resolve_currency(from_unit_raw)
        to_curr = resolve_currency(to_unit_raw)
        if from_curr and to_curr:
            return await self.currency_service.convert(val, from_curr, to_curr)

        # 4. Remaining standard-unit lookups (also produces clear error messages).
        from_u = resolve_unit(from_unit_raw)
        to_u = resolve_unit(to_unit_raw)

        if not from_u:
            raise ParadiseError(f"Unknown or unsupported unit: `{from_unit_raw}`")
        if not to_u:
            raise ParadiseError(f"Unknown or unsupported unit: `{to_unit_raw}`")

        # Handle temperature via resolve_unit if matched
        if from_u in ("celsius", "fahrenheit", "kelvin") and to_u in ("celsius", "fahrenheit", "kelvin"):
            return convert_temperature(val, from_u, to_u)

        try:
            return convert_standard_unit(val, from_u, to_u)
        except ValueError as err:
            raise ParadiseError(str(err))
