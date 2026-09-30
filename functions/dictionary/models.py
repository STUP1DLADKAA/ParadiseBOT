"""The shape of a dictionary answer.

These types are the contract between the three layers that never import
each other: providers fill them in, the renderer draws them, and the cog
turns them into Discord. Everything is immutable and free of Discord and
HTTP types, which is what makes the whole feature unit-testable offline.

Nothing here invents data. A field that a provider did not supply stays
empty, and every consumer is expected to cope with that.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace

# Regional labels the Free Dictionary API encodes in its audio file names.
REGION_LABELS = {
    "us": "US",
    "uk": "UK",
    "au": "AU",
    "ca": "CA",
    "ie": "IE",
    "nz": "NZ",
    "in": "IN",
    "za": "ZA",
}

# Long form part-of-speech names, used for the card's pill.
PART_OF_SPEECH_LABELS = {
    "n": "noun",
    "v": "verb",
    "adj": "adjective",
    "adv": "adverb",
    "u": "",  # datamuse "unknown"
    "prop": "proper noun",
    "pron": "pronoun",
    "prep": "preposition",
    "conj": "conjunction",
    "det": "determiner",
    "interj": "interjection",
}


def dedupe(values: Iterable[str], *, limit: int | None = None, skip: set[str] | None = None) -> tuple[str, ...]:
    """Case-insensitive de-duplication that keeps the first spelling seen."""
    seen: set[str] = set(skip or ())
    out: list[str] = []
    for raw in values:
        if not isinstance(raw, str):
            continue
        value = " ".join(raw.split())
        if not value:
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
        if limit is not None and len(out) >= limit:
            break
    return tuple(out)


@dataclass(frozen=True, slots=True)
class Pronunciation:
    """One accepted way of saying the word."""

    text: str = ""           # IPA, e.g. /ˈprɒmɪs/
    audio_url: str = ""      # a real recording, when the provider has one
    region: str = ""         # us / uk / au / … ('' when unlabelled)
    source_url: str = ""
    licence: str = ""

    @property
    def label(self) -> str:
        return REGION_LABELS.get(self.region, self.region.upper())

    @property
    def has_audio(self) -> bool:
        return bool(self.audio_url)


@dataclass(frozen=True, slots=True)
class Sense:
    """A single meaning: one part of speech, one definition."""

    part_of_speech: str
    definition: str
    example: str = ""
    synonyms: tuple[str, ...] = ()
    antonyms: tuple[str, ...] = ()

    @property
    def is_usable(self) -> bool:
        return bool(self.definition.strip())


@dataclass(frozen=True, slots=True)
class WordForm:
    """A related inflection, e.g. ('plural', 'promises')."""

    label: str
    value: str


@dataclass(frozen=True, slots=True)
class WordEntry:
    """Everything Paradise knows about one word, merged from all providers."""

    word: str
    senses: tuple[Sense, ...] = ()
    pronunciations: tuple[Pronunciation, ...] = ()
    synonyms: tuple[str, ...] = ()
    antonyms: tuple[str, ...] = ()
    related: tuple[str, ...] = ()
    origin: str = ""
    forms: tuple[WordForm, ...] = ()
    language: str = "en"
    sources: tuple[str, ...] = ()
    licence: str = ""

    # -- convenience for the renderer / UI --------------------------------

    @property
    def primary(self) -> Sense | None:
        return self.senses[0] if self.senses else None

    @property
    def part_of_speech(self) -> str:
        return self.senses[0].part_of_speech if self.senses else ""

    @property
    def parts_of_speech(self) -> tuple[str, ...]:
        return dedupe(sense.part_of_speech for sense in self.senses if sense.part_of_speech)

    @property
    def ipa(self) -> str:
        for pronunciation in self.pronunciations:
            if pronunciation.text:
                return pronunciation.text
        return ""

    @property
    def phonetic_variants(self) -> tuple[Pronunciation, ...]:
        """Distinct written pronunciations (one per IPA spelling)."""
        seen: set[str] = set()
        out: list[Pronunciation] = []
        for pronunciation in self.pronunciations:
            if not pronunciation.text or pronunciation.text in seen:
                continue
            seen.add(pronunciation.text)
            out.append(pronunciation)
        return tuple(out)

    @property
    def audio_options(self) -> tuple[Pronunciation, ...]:
        return tuple(p for p in self.pronunciations if p.has_audio)

    @property
    def examples(self) -> tuple[str, ...]:
        return dedupe(sense.example for sense in self.senses if sense.example)

    def all_synonyms(self, limit: int | None = None) -> tuple[str, ...]:
        pool: list[str] = list(self.synonyms)
        for sense in self.senses:
            pool.extend(sense.synonyms)
        return dedupe(pool, limit=limit, skip={self.word.casefold()})

    def all_antonyms(self, limit: int | None = None) -> tuple[str, ...]:
        pool: list[str] = list(self.antonyms)
        for sense in self.senses:
            pool.extend(sense.antonyms)
        return dedupe(pool, limit=limit, skip={self.word.casefold()})

    def senses_by_part(self) -> tuple[tuple[str, tuple[Sense, ...]], ...]:
        """Senses grouped by part of speech, in first-seen order."""
        order: list[str] = []
        buckets: dict[str, list[Sense]] = {}
        for sense in self.senses:
            key = sense.part_of_speech or "—"
            if key not in buckets:
                buckets[key] = []
                order.append(key)
            buckets[key].append(sense)
        return tuple((key, tuple(buckets[key])) for key in order)

    @property
    def is_empty(self) -> bool:
        return not any(sense.is_usable for sense in self.senses)

    def with_extras(
        self,
        *,
        synonyms: Iterable[str] = (),
        antonyms: Iterable[str] = (),
        related: Iterable[str] = (),
        forms: Iterable[WordForm] = (),
    ) -> WordEntry:
        """Return a copy enriched with secondary-provider data."""
        skip = {self.word.casefold()}
        merged_syn = dedupe([*self.synonyms, *synonyms], limit=24, skip=skip)
        merged_ant = dedupe([*self.antonyms, *antonyms], limit=16, skip=skip)
        known = {value.casefold() for value in (*merged_syn, *merged_ant)} | skip
        merged_rel = dedupe([*self.related, *related], limit=16, skip=known)
        seen_forms = {(f.label, f.value.casefold()) for f in self.forms}
        extra_forms = [
            f
            for f in forms
            if (f.label, f.value.casefold()) not in seen_forms
            and f.value.casefold() != self.word.casefold()
        ]
        return replace(
            self,
            synonyms=merged_syn,
            antonyms=merged_ant,
            related=merged_rel,
            forms=(*self.forms, *extra_forms),
        )


@dataclass(frozen=True, slots=True)
class LookupResult:
    """The outcome of a lookup: an entry, or the closest words to it."""

    query: str
    entry: WordEntry | None = None
    suggestions: tuple[str, ...] = ()
    # Set when we answered a different word than asked, e.g. running → run.
    resolved_from: str = ""
    # True when a provider failed and the answer is thinner than usual.
    partial: bool = False
    # Which providers actually contributed.
    providers: tuple[str, ...] = ()

    @property
    def found(self) -> bool:
        return self.entry is not None and not self.entry.is_empty


@dataclass(frozen=True, slots=True)
class SpeechRequest:
    """A request to say one word out loud. Never a whole definition."""

    word: str
    language: str = "en"
    region: str = ""
    audio_url: str = ""

    @property
    def cache_key(self) -> tuple[str, str, str]:
        return (self.word.casefold(), self.language.lower(), self.region.lower())


@dataclass(frozen=True, slots=True)
class Clip:
    """Synthesised or downloaded pronunciation audio, held in memory."""

    data: bytes
    extension: str = "mp3"
    provider: str = ""
    region: str = ""
    # Extra detail for logs / the ephemeral note under the player.
    detail: str = field(default="")

    @property
    def size(self) -> int:
        return len(self.data)

    def filename(self, word: str) -> str:
        safe = "".join(ch for ch in word if ch.isalnum() or ch in "-_") or "word"
        suffix = f"-{self.region}" if self.region else ""
        return f"{safe[:40]}{suffix}.{self.extension}"
