"""Paradise's dictionary: lookup, card rendering and pronunciation.

Layered on purpose, so each piece can be tested (and replaced) alone:

``normalize``   messy input → a clean search term; natural-language triggers
``models``      the immutable shapes every layer agrees on
``http``        one careful HTTP client: timeouts, retries, caps, back-off
``sources``     the providers (Free Dictionary, Datamuse) behind one protocol
``service``     the lookup pipeline: cache, coalescing, fallbacks, suggestions
``fonts``       font resolution and per-glyph fallback (IPA renders properly)
``render``      the card: deterministic layout, then paint
``pronounce``   one word → one audio clip, provider abstraction, cached
``playback``    optional voice-channel output (off unless the box supports it)
``feature``     the facade the cog actually talks to

Nothing below ``feature``/``playback`` imports Discord.
"""

from __future__ import annotations

from .cache import RateLimiter, SingleFlight, TTLCache
from .feature import DictionaryFeature
from .http import HttpClient, HttpError, NotFoundError, ThrottledError
from .models import (
    Clip,
    LookupResult,
    Pronunciation,
    Sense,
    SpeechRequest,
    WordEntry,
    WordForm,
)
from .normalize import (
    FuzzyMatch,
    base_forms,
    candidate_forms,
    edit_distance,
    fuzzy_confidence,
    is_lookupable,
    match_text_request,
    normalize_query,
    rank_fuzzy_matches,
    rank_suggestions,
    should_auto_correct,
)
from .playback import PlaybackSupport, PlaybackUnavailable, VoicePlayback, playback_support
from .pronounce import (
    ElevenLabsSpeechProvider,
    NativeRecordingProvider,
    NeuralSpeechProvider,
    PronunciationError,
    PronunciationOutcome,
    PronunciationService,
    WebVoiceProvider,
)
from .render import (
    DEFAULT_THEME,
    LIGHT_THEME,
    RenderUnavailable,
    Theme,
    card_signature,
    plan_card,
    render_card,
    resolve_theme,
    renderer_available,
)
from .service import DictionaryService, WordDetails
from .sources import (
    DatamuseProvider,
    Enrichment,
    FreeDictionaryProvider,
    parse_datamuse,
    parse_free_dictionary,
)

__all__ = [
    "DEFAULT_THEME",
    "LIGHT_THEME",
    "Clip",
    "DatamuseProvider",
    "DictionaryFeature",
    "DictionaryService",
    "ElevenLabsSpeechProvider",
    "Enrichment",
    "FreeDictionaryProvider",
    "FuzzyMatch",
    "HttpClient",
    "HttpError",
    "LookupResult",
    "NativeRecordingProvider",
    "NeuralSpeechProvider",
    "NotFoundError",
    "PlaybackSupport",
    "PlaybackUnavailable",
    "Pronunciation",
    "PronunciationError",
    "PronunciationOutcome",
    "PronunciationService",
    "RateLimiter",
    "RenderUnavailable",
    "Sense",
    "SingleFlight",
    "SpeechRequest",
    "TTLCache",
    "Theme",
    "ThrottledError",
    "VoicePlayback",
    "WebVoiceProvider",
    "WordDetails",
    "WordEntry",
    "WordForm",
    "base_forms",
    "candidate_forms",
    "card_signature",
    "edit_distance",
    "fuzzy_confidence",
    "is_lookupable",
    "match_text_request",
    "normalize_query",
    "parse_datamuse",
    "parse_free_dictionary",
    "plan_card",
    "playback_support",
    "rank_fuzzy_matches",
    "rank_suggestions",
    "render_card",
    "resolve_theme",
    "should_auto_correct",
    "renderer_available",
]
