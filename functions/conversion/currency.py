"""Currency conversion module with live exchange rates and robust fallback."""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from datetime import datetime, timezone

import aiohttp

from functions.helpers import ParadiseError
from .models import ConversionCategory, ConversionResult

log = logging.getLogger(__name__)

_RATES_CACHE_MAX_SOURCES = 64
_RATES_CACHE_MAX_DESTINATIONS = 64

CURRENCY_SYMBOLS: dict[str, str] = {
    "usd": "$",
    "eur": "€",
    "gbp": "£",
    "inr": "₹",
    "jpy": "¥",
    "cny": "¥",
    "cad": "CA$",
    "aud": "A$",
    "chf": "CHF ",
    "brl": "R$",
    "mxn": "Mex$",
    "rub": "₽",
    "krw": "₩",
    "sgd": "S$",
    "nzd": "NZ$",
    "hkd": "HK$",
    "sek": "kr ",
    "nok": "kr ",
    "zar": "R ",
    "try": "₺",
    "aed": "AED ",
    "sar": "SAR ",
    "thb": "฿",
    "idr": "Rp ",
    "myr": "RM ",
    "php": "₱",
    "vnd": "₫",
    "pln": "zł ",
    "ils": "₪",
    "btc": "₿",
    "eth": "Ξ",
}

# Reliable baseline exchange rates relative to USD (USD = 1.0) for offline/fallback resilience
BASE_USD_RATES: dict[str, float] = {
    "usd": 1.0,
    "inr": 83.50,
    "eur": 0.92,
    "gbp": 0.78,
    "jpy": 155.20,
    "cny": 7.24,
    "cad": 1.37,
    "aud": 1.52,
    "chf": 0.90,
    "brl": 5.45,
    "mxn": 18.20,
    "rub": 88.50,
    "krw": 1380.0,
    "sgd": 1.35,
    "nzd": 1.64,
    "hkd": 7.81,
    "sek": 10.50,
    "nok": 10.60,
    "zar": 18.10,
    "try": 32.80,
    "aed": 3.67,
    "sar": 3.75,
    "thb": 36.70,
    "idr": 16350.0,
    "myr": 4.71,
    "php": 58.60,
    "vnd": 25450.0,
    "pln": 3.98,
    "ils": 3.72,
}

CURRENCY_ALIASES: dict[str, str] = {
    "dollar": "usd", "dollars": "usd", "buck": "usd", "bucks": "usd", "$": "usd",
    "rupee": "inr", "rupees": "inr", "rs": "inr", "₹": "inr",
    "euro": "eur", "euros": "eur", "€": "eur",
    "pound": "gbp", "pounds": "gbp", "quid": "gbp", "£": "gbp",
    "yen": "jpy", "¥": "jpy",
    "yuan": "cny", "renminbi": "cny", "rmb": "cny",
    "cad": "cad", "aud": "aud", "chf": "chf", "franc": "chf", "francs": "chf",
    "won": "krw", "₩": "krw", "real": "brl", "reais": "brl", "peso": "mxn", "pesos": "mxn",
    "dirham": "aed", "dirhams": "aed", "riyal": "sar", "riyals": "sar",
    "bitcoin": "btc", "ethereum": "eth",
}


def resolve_currency(raw: str) -> str | None:
    """Resolve a raw string to a 3-letter currency code."""
    clean = raw.strip().lower().replace(" ", "")
    if clean in CURRENCY_ALIASES:
        return CURRENCY_ALIASES[clean]
    if clean in BASE_USD_RATES:
        return clean
    if len(clean) == 3 and clean.isalpha():
        return clean
    return None


class CurrencyService:
    """Fetches and caches currency exchange rates."""

    def __init__(self, cache_ttl: int = 3600) -> None:
        self.cache_ttl = cache_ttl
        self._rates_cache: OrderedDict[str, tuple[float, dict[str, float]]] = OrderedDict()
        self._last_timestamp: dict[str, str] = {}

    async def convert(self, val: float, from_curr: str, to_curr: str) -> ConversionResult:
        from_code = resolve_currency(from_curr)
        to_code = resolve_currency(to_curr)

        if not from_code or not to_code:
            raise ParadiseError(
                f"Unknown currency: `{from_curr}` or `{to_curr}`. "
                "Supported currencies include USD, INR, EUR, GBP, JPY, CAD, AUD, etc."
            )

        from_code = from_code.lower()
        to_code = to_code.lower()

        if from_code == to_code:
            return ConversionResult(
                from_value=val,
                from_unit=from_code.upper(),
                from_symbol=CURRENCY_SYMBOLS.get(from_code, ""),
                to_value=val,
                to_unit=to_code.upper(),
                to_symbol=CURRENCY_SYMBOLS.get(to_code, ""),
                category=ConversionCategory.CURRENCY,
                rate_timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            )

        rate, ts = await self._get_rate(from_code, to_code)
        to_val = val * rate

        from_sym = CURRENCY_SYMBOLS.get(from_code, "")
        to_sym = CURRENCY_SYMBOLS.get(to_code, "")

        return ConversionResult(
            from_value=val,
            from_unit=from_code.upper(),
            from_symbol=from_sym,
            to_value=to_val,
            to_unit=to_code.upper(),
            to_symbol=to_sym,
            category=ConversionCategory.CURRENCY,
            rate_info=f"1 {from_code.upper()} = {rate:.4f} {to_code.upper()}",
            rate_timestamp=ts,
        )

    async def _get_rate(self, from_c: str, to_c: str) -> tuple[float, str]:
        """Fetch exchange rate from_c -> to_c with caching and fallback."""
        cache_key = from_c
        now = time.monotonic()
        cached = self._rates_cache.get(cache_key)
        if cached is not None:
            c_time, rates = cached
            if now - c_time < self.cache_ttl and to_c in rates:
                self._rates_cache.move_to_end(cache_key)
                return rates[to_c], self._last_timestamp.get(cache_key, "Cached rate")
            if now - c_time >= self.cache_ttl:
                self._rates_cache.pop(cache_key, None)
                self._last_timestamp.pop(cache_key, None)

        # Try live Frankfurter API
        try:
            url = "https://api.frankfurter.app/latest"
            params = {"from": from_c.upper(), "to": to_c.upper()}
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5)
            ) as session:
                async with session.get(url, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        rates = data.get("rates", {})
                        rate_date = data.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
                        if to_c.upper() in rates:
                            rate = float(rates[to_c.upper()])
                            cached_rates = self._rates_cache.get(cache_key)
                            if cached_rates is None:
                                self._rates_cache[cache_key] = (now, {})
                                cached_rates = self._rates_cache[cache_key]
                            cached_rates[1][to_c] = rate
                            while len(cached_rates[1]) > _RATES_CACHE_MAX_DESTINATIONS:
                                cached_rates[1].pop(next(iter(cached_rates[1])))
                            self._rates_cache.move_to_end(cache_key)
                            while len(self._rates_cache) > _RATES_CACHE_MAX_SOURCES:
                                old_key, _ = self._rates_cache.popitem(last=False)
                                self._last_timestamp.pop(old_key, None)
                            self._last_timestamp[cache_key] = f"Live rate as of {rate_date}"
                            return rate, f"Live rate as of {rate_date}"
        except Exception as err:
            log.debug("Frankfurter API call failed: %s", err)

        # Try open.er-api.com fallback
        try:
            url = f"https://open.er-api.com/v6/latest/{from_c.upper()}"
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5)
            ) as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        rates = data.get("rates", {})
                        time_last = data.get("time_last_update_utc", "")[:16]
                        if to_c.upper() in rates:
                            rate = float(rates[to_c.upper()])
                            return rate, f"Live rate ({time_last})"
        except Exception as err:
            log.debug("open.er-api.com call failed: %s", err)

        # Fallback to local baseline USD rates table
        if from_c in BASE_USD_RATES and to_c in BASE_USD_RATES:
            usd_to_from = BASE_USD_RATES[from_c]
            usd_to_to = BASE_USD_RATES[to_c]
            rate = usd_to_to / usd_to_from
            ts = "Reference exchange rate"
            return rate, ts

        raise ParadiseError(f"Could not retrieve exchange rate for {from_c.upper()} to {to_c.upper()}.")
