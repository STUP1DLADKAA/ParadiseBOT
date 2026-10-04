"""Key-less translation helper (Google Translate's public gtx endpoint).

Shared by the ``/translate`` command and the mention router
("translate hello into Japanese"), so the HTTP details live in exactly
one place. Deterministic, no AI involved.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import aiohttp

log = logging.getLogger(__name__)

LANGUAGE_CODES = {
    "hindi": "hi", "english": "en", "spanish": "es", "french": "fr",
    "german": "de", "japanese": "ja", "korean": "ko", "chinese": "zh-CN",
    "arabic": "ar", "russian": "ru", "portuguese": "pt", "italian": "it",
    "turkish": "tr", "urdu": "ur", "punjabi": "pa", "marathi": "mr",
    "tamil": "ta", "telugu": "te", "bengali": "bn", "gujarati": "gu",
    "kannada": "kn", "malayalam": "ml", "dutch": "nl", "greek": "el",
    "hebrew": "he", "persian": "fa", "indonesian": "id", "thai": "th",
    "vietnamese": "vi", "polish": "pl", "ukrainian": "uk", "swedish": "sv",
    "nepali": "ne", "sinhala": "si",
}

# Common informal spellings people actually type at a bot.
_LANGUAGE_ALIASES = {
    "jp": "ja", "jap": "ja",
    "cn": "zh-CN", "zh": "zh-CN", "mandarin": "zh-CN",
    "kr": "ko",
    "pt-br": "pt", "brazilian": "pt",
    "espanol": "es",
    "deutsch": "de",
    "francais": "fr",
    "nihongo": "ja",
}


@dataclass(slots=True)
class Translation:
    text: str
    source: str
    target: str


def resolve_language(raw: str) -> str | None:
    raw = raw.strip().lower()
    if not raw:
        return None
    if raw in LANGUAGE_CODES:
        return LANGUAGE_CODES[raw]
    if raw in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[raw]
    if raw in LANGUAGE_CODES.values() or raw in ("zh-cn", "zh-tw", "zh"):
        return raw
    if len(raw) == 2 and raw.isalpha():
        return raw
    return None


async def translate_text(
    text: str, target_code: str, source_code: str = "auto"
) -> Translation | None:
    """Translate *text*, returning None on service failure (never raises)."""
    params = {
        "client": "gtx",
        "sl": source_code,
        "tl": target_code,
        "dt": "t",
        "q": text,
    }
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        ) as session:
            async with session.get(
                "https://translate.googleapis.com/translate_a/single", params=params
            ) as response:
                if response.status != 200:
                    log.debug("Translation HTTP %s", response.status)
                    return None
                data = await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        log.debug("Translation request failed", exc_info=True)
        return None

    try:
        translated = "".join(part[0] for part in data[0] if part and part[0])
        detected = data[2] if len(data) > 2 and isinstance(data[2], str) else source_code
    except (TypeError, IndexError):
        return None
    if not translated:
        return None
    return Translation(text=translated, source=detected, target=target_code)


__all__ = ["LANGUAGE_CODES", "Translation", "resolve_language", "translate_text"]
