"""Where the words come from.

Two free, key-less providers with a clear division of labour:

``FreeDictionaryProvider`` (api.dictionaryapi.dev, Wiktionary-derived)
    The primary source. It is the only one that has IPA and real human
    audio recordings, so it also feeds the pronounce button.

``DatamuseProvider`` (api.datamuse.com, WordNet + Wiktionary)
    Always used for synonyms, antonyms and related words, used as a
    *fallback* definition source when the primary is down or has no entry,
    and used for "did you mean" and for confirming inflected forms.

Both are wrapped by a provider protocol, so a third source can be added
without the service layer changing. Parsing is done by pure functions so
every quirk below is covered by fixtures in the test-suite rather than by
hitting the network.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote

from .http import HttpClient, HttpError, NotFoundError
from .models import (
    PART_OF_SPEECH_LABELS,
    Pronunciation,
    Sense,
    WordEntry,
    WordForm,
    dedupe,
)
from .normalize import common_prefix_probe, normalize_query

log = logging.getLogger(__name__)

FREE_DICTIONARY_URL = "https://api.dictionaryapi.dev/api/v2/entries"
DATAMUSE_URL = "https://api.datamuse.com/words"

MAX_SENSES = 12
MAX_DEFINITION = 600
MAX_EXAMPLE = 240

# hello-uk.mp3 / promise-us.mp3 …and Google's older hello--_gb_1.mp3 style.
_REGION_SUFFIX = re.compile(r"[-_](us|uk|gb|au|ca|ie|nz|in|za)(?:[-_.]|$)", re.IGNORECASE)
_MISSPELLING = re.compile(r"^\s*(?:common\s+)?misspelling of ([\w'-]+)", re.IGNORECASE)
_DATAMUSE_DEF = re.compile(r"^(?P<pos>[a-z]+)\t(?P<text>.+)$", re.DOTALL)
_TRAILING_BRACKET = re.compile(r"\s*\[[^\]]*\]\s*$")


# ---------------------------------------------------------------------------
# Shared text tidying (presentation only — never changes meaning)
# ---------------------------------------------------------------------------


def tidy_definition(raw: str) -> str:
    text = " ".join(str(raw or "").split())
    if not text:
        return ""
    text = _TRAILING_BRACKET.sub("", text).strip()
    text = _capitalise(text)
    if text and text[-1] not in ".!?:;":
        text += "."
    return text[:MAX_DEFINITION]


def tidy_example(raw: str) -> str:
    text = " ".join(str(raw or "").split())
    if not text:
        return ""
    if len(text) >= 2 and text[0] in "\"'\u201c\u2018" and text[-1] in "\"'\u201d\u2019":
        text = text[1:-1].strip()
    text = _capitalise(text)
    if text and text[-1] not in ".!?\u2026":
        text += "."
    return text[:MAX_EXAMPLE]


def _capitalise(text: str) -> str:
    """Upper-case the first real letter, skipping a leading "(label)"."""
    if not text:
        return text
    start = 0
    if text.startswith("("):
        close = text.find(")")
        if 0 < close < 40:
            start = close + 1
            while start < len(text) and text[start] == " ":
                start += 1
    if start >= len(text) or not text[start].isalpha():
        return text
    return text[:start] + text[start].upper() + text[start + 1 :]


def normalise_part_of_speech(raw: str) -> str:
    part = " ".join(str(raw or "").split()).lower()
    if not part:
        return ""
    if part in PART_OF_SPEECH_LABELS:
        return PART_OF_SPEECH_LABELS[part]
    # "exclamation" is Oxford's name for what Wiktionary calls interjection.
    return {"exclamation": "interjection"}.get(part, part)


def absolute_audio_url(raw: str) -> str:
    url = str(raw or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    if not url.lower().startswith(("http://", "https://")):
        return ""
    return url


def region_from_audio_url(url: str) -> str:
    match = _REGION_SUFFIX.search(url.rsplit("/", 1)[-1])
    if not match:
        return ""
    region = match.group(1).lower()
    return "uk" if region == "gb" else region


# ---------------------------------------------------------------------------
# Provider protocol
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Enrichment:
    """Secondary lexical data — never definitions, never invented."""

    synonyms: tuple[str, ...] = ()
    antonyms: tuple[str, ...] = ()
    related: tuple[str, ...] = ()
    provider: str = ""

    @property
    def is_empty(self) -> bool:
        return not (self.synonyms or self.antonyms or self.related)


class DefinitionProvider(Protocol):
    """Anything that can turn a word into a :class:`WordEntry`."""

    name: str

    async def lookup(self, term: str, language: str = "en") -> WordEntry | None:
        ...


# ---------------------------------------------------------------------------
# api.dictionaryapi.dev
# ---------------------------------------------------------------------------


def parse_free_dictionary(payload: Any, term: str) -> WordEntry | None:
    """Turn a Free Dictionary API response into an entry. Tolerates anything."""
    if not isinstance(payload, list) or not payload:
        return None

    word = ""
    senses: list[Sense] = []
    pronunciations: list[Pronunciation] = []
    synonyms: list[str] = []
    antonyms: list[str] = []
    origin = ""
    sources: list[str] = []
    licence = ""
    seen_audio: set[str] = set()
    seen_text: set[str] = set()

    for block in payload:
        if not isinstance(block, dict):
            continue
        word = word or str(block.get("word") or "").strip()
        origin = origin or " ".join(str(block.get("origin") or "").split())

        licence_block = block.get("license")
        if isinstance(licence_block, dict) and not licence:
            licence = str(licence_block.get("name") or "").strip()

        for url in block.get("sourceUrls") or []:
            if isinstance(url, str) and url.strip():
                sources.append(url.strip())

        # -- pronunciations ------------------------------------------------
        top_phonetic = " ".join(str(block.get("phonetic") or "").split())
        raw_phonetics = block.get("phonetics")
        if isinstance(raw_phonetics, list):
            for item in raw_phonetics:
                if not isinstance(item, dict):
                    continue
                text = " ".join(str(item.get("text") or "").split())
                audio = absolute_audio_url(item.get("audio") or "")
                if not text and not audio:
                    continue
                if audio and audio in seen_audio:
                    continue
                if audio:
                    seen_audio.add(audio)
                elif text and text in seen_text:
                    continue
                if text:
                    seen_text.add(text)
                item_licence = item.get("license")
                pronunciations.append(
                    Pronunciation(
                        text=text,
                        audio_url=audio,
                        region=region_from_audio_url(audio) if audio else "",
                        source_url=str(item.get("sourceUrl") or "").strip(),
                        licence=(
                            str(item_licence.get("name") or "").strip()
                            if isinstance(item_licence, dict)
                            else ""
                        ),
                    )
                )
        if top_phonetic and top_phonetic not in seen_text:
            seen_text.add(top_phonetic)
            pronunciations.append(Pronunciation(text=top_phonetic))

        # -- meanings ------------------------------------------------------
        meanings = block.get("meanings")
        if not isinstance(meanings, list):
            continue
        for meaning in meanings:
            if not isinstance(meaning, dict):
                continue
            part = normalise_part_of_speech(meaning.get("partOfSpeech") or "")
            synonyms.extend(_string_list(meaning.get("synonyms")))
            antonyms.extend(_string_list(meaning.get("antonyms")))
            definitions = meaning.get("definitions")
            if not isinstance(definitions, list):
                continue
            for definition in definitions:
                if not isinstance(definition, dict):
                    continue
                text = tidy_definition(definition.get("definition") or "")
                if not text:
                    continue
                senses.append(
                    Sense(
                        part_of_speech=part,
                        definition=text,
                        example=tidy_example(definition.get("example") or ""),
                        synonyms=dedupe(_string_list(definition.get("synonyms")), limit=12),
                        antonyms=dedupe(_string_list(definition.get("antonyms")), limit=12),
                    )
                )

    word = word or normalize_query(term)
    if not word or not senses:
        return None

    return WordEntry(
        word=word,
        senses=tuple(senses[:MAX_SENSES]),
        pronunciations=tuple(_order_pronunciations(pronunciations)),
        synonyms=dedupe(synonyms, limit=24, skip={word.casefold()}),
        antonyms=dedupe(antonyms, limit=16, skip={word.casefold()}),
        origin=origin,
        sources=dedupe(sources, limit=3),
        licence=licence,
    )


def _order_pronunciations(items: list[Pronunciation]) -> list[Pronunciation]:
    """Ones with both IPA and audio first — they make the nicest card."""

    def rank(item: Pronunciation) -> tuple[int, int]:
        return (0 if (item.text and item.audio_url) else 1 if item.text else 2, 0)

    return sorted(items, key=rank)[:8]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


class FreeDictionaryProvider:
    """api.dictionaryapi.dev — definitions, IPA and human audio. No key."""

    name = "Free Dictionary"

    def __init__(self, client: HttpClient, *, base_url: str = FREE_DICTIONARY_URL) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")

    async def lookup(self, term: str, language: str = "en") -> WordEntry | None:
        url = f"{self._base_url}/{language}/{quote(term, safe='')}"
        payload = await self._client.get_json(url)
        return parse_free_dictionary(payload, term)


# ---------------------------------------------------------------------------
# api.datamuse.com
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DatamuseWord:
    word: str
    score: float = 0.0
    tags: tuple[str, ...] = ()
    definitions: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    @property
    def parts_of_speech(self) -> tuple[str, ...]:
        return tuple(
            normalise_part_of_speech(tag)
            for tag in self.tags
            if tag in PART_OF_SPEECH_LABELS and PART_OF_SPEECH_LABELS[tag]
        )

    @property
    def misspelling_of(self) -> str:
        for _part, text in self.definitions:
            match = _MISSPELLING.match(text)
            if match:
                return match.group(1)
        return ""


def parse_datamuse(payload: Any) -> list[DatamuseWord]:
    """Datamuse rows → typed words. Unknown shapes are skipped, never guessed."""
    if not isinstance(payload, list):
        return []
    out: list[DatamuseWord] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        word = " ".join(str(row.get("word") or "").split())
        if not word:
            continue
        try:
            score = float(row.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        tags = tuple(str(tag) for tag in row.get("tags") or [] if isinstance(tag, str))
        definitions: list[tuple[str, str]] = []
        for raw in row.get("defs") or []:
            if not isinstance(raw, str):
                continue
            match = _DATAMUSE_DEF.match(raw.strip())
            if match:
                definitions.append(
                    (normalise_part_of_speech(match.group("pos")), match.group("text").strip())
                )
            elif raw.strip():
                definitions.append(("", raw.strip()))
        out.append(DatamuseWord(word, score, tags, tuple(definitions)))
    return out


def entry_from_datamuse(word: DatamuseWord, term: str) -> WordEntry | None:
    """Build a (definitions-only) entry from a Datamuse row."""
    senses: list[Sense] = []
    for part, text in word.definitions[:MAX_SENSES]:
        definition = tidy_definition(text)
        if definition:
            senses.append(Sense(part_of_speech=part, definition=definition))
    if not senses:
        return None
    return WordEntry(
        word=word.word or normalize_query(term),
        senses=tuple(senses),
        sources=("https://www.datamuse.com/api/",),
        licence="Datamuse (WordNet / Wiktionary)",
    )


class DatamuseProvider:
    """api.datamuse.com — synonyms, antonyms, related words, spelling help."""

    name = "Datamuse"

    def __init__(self, client: HttpClient, *, base_url: str = DATAMUSE_URL) -> None:
        self._client = client
        self._base_url = base_url

    async def _words(self, params: dict[str, Any], timeout: float | None = None) -> list[DatamuseWord]:
        payload = await self._client.get_json(self._base_url, params=params, timeout=timeout)
        return parse_datamuse(payload)

    # -- definitions (fallback) -------------------------------------------

    async def lookup(self, term: str, language: str = "en") -> WordEntry | None:
        if language.lower() not in ("en", "en-us", "en-gb"):
            return None  # Datamuse is English-only; say so by returning nothing.
        rows = await self._words({"sp": term, "md": "dp", "max": 4})
        target = normalize_query(term).casefold()
        for row in rows:
            if row.word.casefold() == target:
                return entry_from_datamuse(row, term)
        return None

    # -- enrichment --------------------------------------------------------

    async def enrich(self, term: str, *, include_related: bool = False) -> Enrichment:
        """Synonyms / antonyms (/ related), best effort and never fatal."""
        jobs = [
            self._words({"rel_syn": term, "max": 14}),
            self._words({"rel_ant": term, "max": 10}),
        ]
        if include_related:
            jobs.append(self._words({"ml": term, "max": 14}))
        results = await asyncio.gather(*jobs, return_exceptions=True)

        def words(index: int) -> list[str]:
            if index >= len(results):
                return []
            value = results[index]
            if isinstance(value, BaseException):
                log.debug("Datamuse enrichment part %s failed: %s", index, value)
                return []
            return [row.word for row in value]

        synonyms = dedupe(words(0), limit=14)
        antonyms = dedupe(words(1), limit=10)
        known = {w.casefold() for w in (*synonyms, *antonyms)}
        related = dedupe(words(2), limit=12, skip=known | {term.casefold()})
        return Enrichment(synonyms, antonyms, related, provider=self.name)

    # -- did you mean ------------------------------------------------------

    async def suggest(self, term: str) -> list[tuple[str, float]]:
        """Close spellings, then close sounds. Returns (word, score) pairs."""
        spelled, sounds = await asyncio.gather(
            self._words({"sp": term, "md": "d", "max": 8}),
            self._words({"sl": term, "max": 6}),
            return_exceptions=True,
        )
        candidates: list[tuple[str, float]] = []
        target = normalize_query(term).casefold()

        rows = spelled if isinstance(spelled, list) else []
        for row in rows:
            # Wiktionary knows many misspellings and names the right word.
            correction = row.misspelling_of
            if correction and correction.casefold() != target:
                candidates.append((correction, row.score + 1_000_000))
            if row.word.casefold() != target:
                candidates.append((row.word, row.score))

        if isinstance(sounds, list):
            candidates.extend(
                (row.word, row.score * 0.75) for row in sounds if row.word.casefold() != target
            )
        return candidates

    # -- inflected forms ---------------------------------------------------

    async def verify_forms(self, word: str, candidates: tuple[WordForm, ...]) -> tuple[WordForm, ...]:
        """Keep only the generated forms that really exist, in one request."""
        if not candidates:
            return ()
        rows = await self._words({"sp": common_prefix_probe(word), "md": "p", "max": 60})
        known = {row.word.casefold() for row in rows}
        return tuple(form for form in candidates if form.value.casefold() in known)


__all__ = [
    "DatamuseProvider",
    "DatamuseWord",
    "DefinitionProvider",
    "Enrichment",
    "FreeDictionaryProvider",
    "HttpError",
    "NotFoundError",
    "absolute_audio_url",
    "entry_from_datamuse",
    "normalise_part_of_speech",
    "parse_datamuse",
    "parse_free_dictionary",
    "region_from_audio_url",
    "tidy_definition",
    "tidy_example",
]
