"""One object that owns the whole dictionary feature.

The cog asks this for three things — a lookup, a card, a pronunciation —
and everything underneath (HTTP session, caches, thread pool, providers,
voice support) is wired up and torn down here. Keeping the plumbing in
one place is what stops a failed render or a dead TTS provider from
reaching the rest of the bot.
"""

from __future__ import annotations

import asyncio
import logging

from .cache import SingleFlight, TTLCache
from .http import HttpClient
from .models import LookupResult, WordEntry
from .playback import PlaybackSupport, VoicePlayback, playback_support
from .pronounce import NeuralSpeechProvider, PronunciationOutcome, PronunciationService
from .render import (
    RenderUnavailable,
    Theme,
    card_signature,
    render_card,
    renderer_available,
    resolve_theme,
)
from .service import DictionaryService, WordDetails
from . import fonts

log = logging.getLogger(__name__)

CARD_CACHE_ITEMS = 64
CARD_CACHE_BYTES = 24 * 1024 * 1024
CARD_TTL = 3 * 3600.0
RENDER_CONCURRENCY = 2
RENDER_TIMEOUT = 15.0


class DictionaryFeature:
    """Data → card → audio, with every cache and deadline in one place."""

    def __init__(self, config=None) -> None:
        self.config = config
        timeout = float(getattr(config, "timeout", 8.0) or 8.0)
        self.client = HttpClient(timeout=timeout)
        self.service = DictionaryService(
            client=self.client,
            timeout=timeout,
            cache_size=int(getattr(config, "cache_size", 512) or 512),
            cache_ttl=float(getattr(config, "cache_ttl", 6 * 3600) or 6 * 3600),
        )
        self.pronunciation = PronunciationService(
            self.client,
            prefer_native=bool(getattr(config, "prefer_native_audio", True)),
            neural=NeuralSpeechProvider(
                self.client,
                base_url=getattr(config, "speech_base_url", "") or "",
                api_key=getattr(config, "speech_api_key", "") or "",
                model=getattr(config, "speech_model", "") or "",
                voice=getattr(config, "speech_voice", "") or "",
            ),
            timeout=timeout,
        )
        self.playback = VoicePlayback(
            enabled=bool(getattr(config, "voice_playback", False))
        )
        self.card_theme: Theme = resolve_theme(getattr(config, "card_theme", "dark"))
        self._cards: TTLCache[str, bytes] = TTLCache(
            max_items=CARD_CACHE_ITEMS,
            ttl=CARD_TTL,
            max_weight=CARD_CACHE_BYTES,
            weigher=len,
        )
        self._card_flight: SingleFlight = SingleFlight()
        self._render_gate = asyncio.Semaphore(RENDER_CONCURRENCY)
        self._render_broken = False

    # ------------------------------------------------------------------
    # Capability reporting (logged once at start-up)
    # ------------------------------------------------------------------

    @property
    def images_available(self) -> bool:
        return renderer_available() and not self._render_broken

    def voice_support(self) -> PlaybackSupport:
        return self.playback.support()

    def describe(self) -> str:
        bits = [
            "cards: " + (
                f"on ({self.card_theme.name} Material surface)"
                if self.images_available
                else "off (text fallback)"
            ),
            fonts.describe(),
            self.pronunciation.describe(),
        ]
        support = self.voice_support()
        bits.append(
            "voice playback: on" if support else f"voice playback: off ({support.reason})"
        )
        return " | ".join(bits)

    # ------------------------------------------------------------------
    # The three operations
    # ------------------------------------------------------------------

    async def lookup(
        self, query: str, *, language: str = "en", fuzzy: bool = True
    ) -> LookupResult:
        return await self.service.lookup(query, language=language, fuzzy=fuzzy)

    async def details(self, entry: WordEntry) -> WordDetails:
        return await self.service.details(entry)

    async def card(self, entry: WordEntry) -> bytes | None:
        """PNG bytes for an entry, or None when images aren't possible."""
        if not self.images_available:
            return None
        key = card_signature(entry, self.card_theme)
        cached = self._cards.get(key)
        if cached is not None:
            return cached

        async def run() -> bytes | None:
            try:
                async with self._render_gate:
                    data = await asyncio.wait_for(
                        asyncio.to_thread(render_card, entry, self.card_theme),
                        timeout=RENDER_TIMEOUT,
                    )
            except RenderUnavailable as error:
                log.warning("Card rendering unavailable: %s", error)
                self._render_broken = True
                return None
            except asyncio.TimeoutError:
                log.warning("Card rendering timed out for %r", entry.word)
                return None
            except Exception:  # noqa: BLE001 - a bad render must not kill a lookup
                log.exception("Card rendering failed for %r", entry.word)
                return None
            self._cards.put(key, data)
            return data

        return await self._card_flight.run(key, run)

    async def pronounce(
        self,
        word: str,
        *,
        entry: WordEntry | None = None,
        region: str = "",
        language: str = "en",
        with_alternates: bool = False,
    ) -> PronunciationOutcome:
        return await self.pronunciation.pronounce(
            word,
            entry=entry,
            region=region,
            language=language,
            with_alternates=with_alternates,
        )

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    @property
    def stats(self) -> str:
        return (
            f"{self.service.cache_stats} cards={len(self._cards)} "
            f"clips={self.pronunciation.cached_clips}"
        )

    def prune(self) -> None:
        self._cards.prune()

    async def close(self) -> None:
        self._cards.clear()
        self.pronunciation.clear()
        try:
            await self.playback.shutdown()
        except Exception:  # noqa: BLE001 - shutdown is best effort
            log.debug("Voice playback shutdown failed", exc_info=True)
        await self.service.close()
        await self.client.close()


__all__ = ["DictionaryFeature", "playback_support"]
