"""Currency conversion module with live exchange rates and robust fallback."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import aiohttp

from functions.helpers import ParadiseError
from functions.perf import shared_session
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
    "dollar": "usd", "dollars": "usd", "usdollar": "usd", "usdollars": "usd",
    "us$": "usd", "buck": "usd", "bucks": "usd", "$": "usd",
    "rupee": "inr", "rupees": "inr", "indianrupee": "inr", "indianrupees": "inr",
    "rs": "inr", "rs.": "inr", "₹": "inr",
    "euro": "eur", "euros": "eur", "€": "eur",
    "pound": "gbp", "pounds": "gbp", "britishpound": "gbp", "britishpounds": "gbp",
    "poundsterling": "gbp", "sterling": "gbp", "quid": "gbp", "£": "gbp",
    "yen": "jpy", "japaneseyen": "jpy", "¥": "jpy",
    "yuan": "cny", "chineseyuan": "cny", "renminbi": "cny", "rmb": "cny",
    "cad": "cad", "ca$": "cad", "canadiandollar": "cad", "canadiandollars": "cad",
    "aud": "aud", "a$": "aud", "australiandollar": "aud", "australiandollars": "aud",
    "sgd": "sgd", "s$": "sgd", "singaporedollar": "sgd", "singaporedollars": "sgd",
    "nzd": "nzd", "nz$": "nzd", "newzealanddollar": "nzd", "newzealanddollars": "nzd",
    "hkd": "hkd", "hk$": "hkd", "hongkongdollar": "hkd", "hongkongdollars": "hkd",
    "chf": "chf", "franc": "chf", "francs": "chf", "swissfranc": "chf", "swissfrancs": "chf",
    "won": "krw", "koreanwon": "krw", "southkoreanwon": "krw", "₩": "krw",
    "real": "brl", "reais": "brl", "r$": "brl",
    "peso": "mxn", "pesos": "mxn", "mexicanpeso": "mxn", "mexicanpesos": "mxn",
    "dirham": "aed", "dirhams": "aed", "uaedirham": "aed", "uaedirhams": "aed",
    "riyal": "sar", "riyals": "sar", "saudiriyal": "sar", "saudiriyals": "sar",
    "baht": "thb", "ringgit": "myr", "rupiah": "idr", "lira": "try",
    "ruble": "rub", "rubles": "rub", "rouble": "rub", "shekel": "ils", "shekels": "ils",
    "zloty": "pln", "rand": "zar", "dong": "vnd",
    "bitcoin": "btc", "ethereum": "eth",
}


def resolve_currency(raw: str) -> str | None:
    """Resolve a raw string to a 3-letter currency code."""
    clean = raw.strip().rstrip(".").lower().replace(" ", "")
    if clean in CURRENCY_ALIASES:
        return CURRENCY_ALIASES[clean]
    if clean in BASE_USD_RATES:
        return clean
    if len(clean) == 3 and clean.isalpha():
        return clean
    return None


class CurrencyService:
    """Fetches and caches currency exchange rates with pooled HTTP + fallback."""

    def __init__(
        self,
        cache_ttl: int = 3600,
        *,
        fetch_json: Callable[[str, dict[str, str] | None], Awaitable[dict[str, Any] | None]]
        | None = None,
    ) -> None:
        self.cache_ttl = cache_ttl
        self._fetch_json = fetch_json
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

    def _store_rate(self, cache_key: str, to_c: str, rate: float, label: str, now: float) -> None:
        entry = self._rates_cache.get(cache_key)
        destinations = dict(entry[1]) if entry is not None else {}
        destinations[to_c] = rate
        while len(destinations) > _RATES_CACHE_MAX_DESTINATIONS:
            destinations.pop(next(iter(destinations)))
        self._rates_cache[cache_key] = (now, destinations)
        self._rates_cache.move_to_end(cache_key)
        while len(self._rates_cache) > _RATES_CACHE_MAX_SOURCES:
            old_key, _ = self._rates_cache.popitem(last=False)
            self._last_timestamp.pop(old_key, None)
        self._last_timestamp[cache_key] = label

    async def _request_json(
        self, url: str, params: dict[str, str] | None = None
    ) -> dict[str, Any] | None:
        for attempt in range(2):
            try:
                if self._fetch_json is not None:
                    data = await self._fetch_json(url, params)
                    return data if isinstance(data, dict) else None
                session = shared_session("currency", timeout=5.0)
                async with session.get(url, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        return data if isinstance(data, dict) else None
                    if resp.status not in (429, 500, 502, 503, 504):
                        return None
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
                log.debug("Currency rate fetch attempt %d failed (%s): %s", attempt + 1, url, err)
            except Exception as err:  # noqa: BLE001
                log.debug("Currency rate fetch error (%s): %s", url, err)
                return None
            if attempt == 0:
                await asyncio.sleep(0.15)
        return None

    async def _get_rate(self, from_c: str, to_c: str) -> tuple[float, str]:
        """Fetch exchange rate from_c -> to_c with pooled HTTP, retry, stale & baseline fallback."""
        cache_key = from_c
        now = time.monotonic()
        stale_rate: tuple[float, str] | None = None
        cached = self._rates_cache.get(cache_key)
        if cached is not None:
            c_time, rates = cached
            if to_c in rates:
                ts = self._last_timestamp.get(cache_key, "Cached rate")
                if now - c_time < self.cache_ttl:
                    self._rates_cache.move_to_end(cache_key)
                    return rates[to_c], ts
                stale_rate = (rates[to_c], f"{ts} (cached)")
            if now - c_time >= self.cache_ttl:
                self._rates_cache.pop(cache_key, None)
                self._last_timestamp.pop(cache_key, None)

        # Primary: Frankfurter API
        data = await self._request_json(
            "https://api.frankfurter.app/latest",
            {"from": from_c.upper(), "to": to_c.upper()},
        )
        if data is not None:
            rates = data.get("rates") or {}
            if isinstance(rates, dict) and to_c.upper() in rates:
                rate = float(rates[to_c.upper()])
                if rate > 0:
                    rate_date = data.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
                    label = f"Live rate as of {rate_date}"
                    self._store_rate(cache_key, to_c, rate, label, now)
                    return rate, label

        # Secondary: open.er-api.com
        fallback_data = await self._request_json(
            f"https://open.er-api.com/v6/latest/{from_c.upper()}"
        )
        if fallback_data is not None:
            rates = fallback_data.get("rates") or {}
            if isinstance(rates, dict) and to_c.upper() in rates:
                rate = float(rates[to_c.upper()])
                if rate > 0:
                    time_last = str(fallback_data.get("time_last_update_utc", ""))[:16]
                    label = f"Live rate ({time_last})" if time_last else "Live rate"
                    self._store_rate(cache_key, to_c, rate, label, now)
                    return rate, label

        # Prefer a recently fetched live rate over the static reference table.
        if stale_rate is not None:
            return stale_rate

        # Offline fallback to baseline USD rates table.
        if from_c in BASE_USD_RATES and to_c in BASE_USD_RATES:
            usd_to_from = BASE_USD_RATES[from_c]
            usd_to_to = BASE_USD_RATES[to_c]
            rate = usd_to_to / usd_to_from
            return rate, "Reference exchange rate"

        raise ParadiseError(
            f"Could not retrieve exchange rate for {from_c.upper()} to {to_c.upper()}."
        )
