"""Key-less translation helper (Google Translate's public gtx endpoint).

Shared by the ``/translate`` command and the mention router
("translate hello into Japanese"), so HTTP pooling, caching, transient retry
and language normalization live in one place. Deterministic, no AI required.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import aiohttp

from functions.cache import TTLCache
from functions.perf import shared_session

log = logging.getLogger(__name__)

LANGUAGE_CODES: dict[str, str] = {
    "hindi": "hi", "english": "en", "spanish": "es", "french": "fr",
    "german": "de", "japanese": "ja", "korean": "ko", "chinese": "zh-CN",
    "arabic": "ar", "russian": "ru", "portuguese": "pt", "italian": "it",
    "turkish": "tr", "urdu": "ur", "punjabi": "pa", "marathi": "mr",
    "tamil": "ta", "telugu": "te", "bengali": "bn", "gujarati": "gu",
    "kannada": "kn", "malayalam": "ml", "odia": "or", "assamese": "as",
    "sanskrit": "sa", "dutch": "nl", "greek": "el", "hebrew": "he",
    "persian": "fa", "indonesian": "id", "malay": "ms", "tagalog": "tl",
    "filipino": "tl", "thai": "th", "vietnamese": "vi", "polish": "pl",
    "ukrainian": "uk", "czech": "cs", "romanian": "ro", "hungarian": "hu",
    "swedish": "sv", "norwegian": "no", "danish": "da", "finnish": "fi",
    "swahili": "sw", "latin": "la", "nepali": "ne", "sinhala": "si",
}

# Common informal spellings & multi-word variants people type in chat.
_LANGUAGE_ALIASES: dict[str, str] = {
    "jp": "ja", "jap": "ja", "nihongo": "ja",
    "cn": "zh-CN", "zh": "zh-CN", "zh-cn": "zh-CN", "mandarin": "zh-CN",
    "simplified chinese": "zh-CN", "chinese simplified": "zh-CN",
    "zh-tw": "zh-TW", "traditional chinese": "zh-TW", "chinese traditional": "zh-TW",
    "kr": "ko", "hangul": "ko",
    "pt-br": "pt", "brazilian": "pt", "brazilian portuguese": "pt",
    "espanol": "es", "español": "es", "mexican spanish": "es",
    "deutsch": "de", "ger": "de", "deu": "de",
    "francais": "fr", "français": "fr", "fra": "fr",
    "eng": "en", "hin": "hi", "esp": "es",
    "farsi": "fa", "bangla": "bn", "oriya": "or",
}

_CACHE: TTLCache["Translation"] = TTLCache(ttl=1800.0, max_entries=256)
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"


@dataclass(slots=True)
class Translation:
    text: str
    source: str
    target: str


def resolve_language(raw: str) -> str | None:
    cleaned = " ".join(str(raw or "").strip().strip(".,?!:;'\"").lower().split())
    if not cleaned:
        return None
    if cleaned in LANGUAGE_CODES:
        return LANGUAGE_CODES[cleaned]
    if cleaned in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[cleaned]
    if cleaned in LANGUAGE_CODES.values():
        return cleaned
    if len(cleaned) == 2 and cleaned.isalpha():
        return cleaned
    return None


def _parse_gtx_payload(
    data: Any, *, source_code: str, target_code: str
) -> Translation | None:
    if not isinstance(data, list) or not data or not isinstance(data[0], list):
        return None
    parts: list[str] = []
    for part in data[0]:
        if isinstance(part, list) and part and isinstance(part[0], str):
            parts.append(part[0])
    translated = "".join(parts).strip()
    if not translated:
        return None
    detected = (
        data[2]
        if len(data) > 2 and isinstance(data[2], str) and data[2].strip()
        else source_code
    )
    return Translation(text=translated, source=detected, target=target_code)


async def translate_text(
    text: str,
    target_code: str,
    source_code: str = "auto",
    *,
    fetch_json: Callable[[str, dict[str, str]], Awaitable[Any]] | None = None,
) -> Translation | None:
    """Translate *text*, returning ``None`` on service failure (never raises)."""
    clean_text = str(text or "").strip()
    if not clean_text or not target_code:
        return None

    cache_key = f"{source_code.lower()}:{target_code.lower()}:{clean_text}"
    if fetch_json is None:
        cached = _CACHE.get(cache_key)
        if cached is not None:
            return cached

    params = {
        "client": "gtx",
        "sl": source_code,
        "tl": target_code,
        "dt": "t",
        "q": clean_text,
    }

    data: Any = None
    for attempt in range(2):
        try:
            if fetch_json is not None:
                data = await fetch_json(_TRANSLATE_URL, params)
                break
            session = shared_session("translate", timeout=10.0)
            async with session.get(_TRANSLATE_URL, params=params) as response:
                if response.status == 200:
                    data = await response.json(content_type=None)
                    break
                log.debug("Translation HTTP %s (attempt %d)", response.status, attempt + 1)
                if response.status not in _RETRYABLE_STATUS:
                    return None
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            log.debug("Translation request failed (attempt %d)", attempt + 1, exc_info=True)
        if attempt == 0:
            await asyncio.sleep(0.2)

    result = _parse_gtx_payload(
        data, source_code=source_code, target_code=target_code
    )
    if result is not None and fetch_json is None:
        _CACHE.set(cache_key, result)
    return result


__all__ = ["LANGUAGE_CODES", "Translation", "resolve_language", "translate_text"]
