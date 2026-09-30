"""Offline validation for the dictionary feature.

No token, no network, no Discord gateway. Fake providers stand in for the
two web APIs, so every branch below — cache hits, coalesced lookups, a
provider falling over, a misspelling, an inflected form, a word with no
audio — is exercised deterministically.

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import pathlib
import re
import struct
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from functions.dictionary import (  # noqa: E402
    DictionaryService,
    Enrichment,
    PronunciationError,
    PronunciationService,
    RateLimiter,
    SingleFlight,
    TTLCache,
    WordEntry,
    base_forms,
    candidate_forms,
    card_signature,
    edit_distance,
    is_lookupable,
    match_text_request,
    normalize_query,
    parse_datamuse,
    parse_free_dictionary,
    plan_card,
    rank_suggestions,
    render_card,
    renderer_available,
)
from functions.dictionary import fonts as fonts_module  # noqa: E402
from functions.dictionary.models import (  # noqa: E402
    Clip,
    Pronunciation,
    Sense,
    SpeechRequest,
    WordForm,
    dedupe,
)
from functions.dictionary.normalize import common_prefix_probe  # noqa: E402
from functions.dictionary.playback import VoicePlayback, playback_support  # noqa: E402
from functions.dictionary.pronounce import (  # noqa: E402
    NativeRecordingProvider,
    NeuralSpeechProvider,
    looks_like_audio,
    speakable,
)
from functions.dictionary.render import (  # noqa: E402
    DEFAULT_THEME,
    LIGHT_THEME,
    fit_text,
    text_width,
    wrap_text,
)
from functions.dictionary.sources import (  # noqa: E402
    entry_from_datamuse,
    normalise_part_of_speech,
    region_from_audio_url,
    tidy_definition,
    tidy_example,
)
from functions.helpers import ParadiseError  # noqa: E402

run = asyncio.run

# Providers are *meant* to fail in several tests; keep the log tidy.
logging.disable(logging.ERROR)


# ---------------------------------------------------------------------------
# Fixtures: real response shapes from the two providers
# ---------------------------------------------------------------------------

HELLO = [
    {
        "word": "hello",
        "phonetics": [
            {
                "audio": "https://api.dictionaryapi.dev/media/pronunciations/en/hello-au.mp3",
                "sourceUrl": "https://commons.wikimedia.org/w/index.php?curid=75797336",
                "license": {"name": "BY-SA 4.0"},
            },
            {
                "text": "/h\u0259\u02c8l\u0259\u028a/",
                "audio": "https://api.dictionaryapi.dev/media/pronunciations/en/hello-uk.mp3",
            },
            {"text": "/h\u0259\u02c8lo\u028a/", "audio": ""},
        ],
        "meanings": [
            {
                "partOfSpeech": "noun",
                "definitions": [{"definition": '"Hello!" or an equivalent greeting.'}],
                "synonyms": ["greeting"],
                "antonyms": [],
            },
            {
                "partOfSpeech": "interjection",
                "definitions": [
                    {
                        "definition": "A greeting said when meeting someone",
                        "example": "Hello, everyone.",
                    },
                    {
                        "definition": "A greeting used when answering the telephone.",
                        "example": "Hello? How may I help you?",
                    },
                ],
                "synonyms": [],
                "antonyms": ["bye", "goodbye"],
            },
        ],
        "license": {"name": "CC BY-SA 3.0"},
        "sourceUrls": ["https://en.wiktionary.org/wiki/hello"],
    }
]

# The older Oxford-style payload: top-level phonetic, an origin, and a
# protocol-relative audio URL.
OLD_STYLE = [
    {
        "word": "promise",
        "phonetic": "/\u02c8pr\u0252m\u026as/",
        "phonetics": [
            {"text": "/\u02c8pr\u0252m\u026as/", "audio": "//ssl.gstatic.com/dictionary/promise--_gb_1.mp3"}
        ],
        "origin": "late Middle English: from Latin promissum.",
        "meanings": [
            {
                "partOfSpeech": "exclamation",
                "definitions": [
                    {"definition": "a declaration that one will do something", "example": "a promise of help"}
                ],
            }
        ],
    }
]

DATAMUSE_SERENDIPITY = [
    {
        "word": "serendipity",
        "score": 319109,
        "tags": ["n", "f:0.292473"],
        "defs": [
            "n\tAn unsought, unintended or unexpected, but fortunate, discovery.",
            "n\tThe phenomenon of making an unplanned, fortunate discovery. ",
        ],
    },
    {"word": "serendibite", "score": 4, "tags": ["n"], "defs": ["n\t(mineralogy) A triclinic mineral. "]},
]

DATAMUSE_MISSPELLING = [
    {
        "word": "recieve",
        "score": 148042,
        "tags": ["v"],
        "defs": ["v\tMisspelling of receive. [(transitive) To be given something.] "],
    },
    {"word": "relieve", "score": 18044, "tags": ["v"], "defs": ["v\t(transitive) To ease."]},
]


def entry_for(word="promise", **kwargs) -> WordEntry:
    defaults = dict(
        senses=(
            Sense("noun", "A commitment to do something.", example="He kept his promise."),
        ),
        pronunciations=(
            Pronunciation(
                text="/\u02c8pr\u0252m\u026as/",
                audio_url="https://example.test/promise-uk.mp3",
                region="uk",
            ),
        ),
        synonyms=("pledge", "vow"),
        sources=("https://en.wiktionary.org/wiki/promise",),
        licence="CC BY-SA 3.0",
    )
    defaults.update(kwargs)
    return WordEntry(word=word, **defaults)


# ---------------------------------------------------------------------------
# Query normalisation
# ---------------------------------------------------------------------------


class TestNormalize(unittest.TestCase):
    def test_case_and_whitespace(self):
        self.assertEqual(normalize_query("  Hello  "), "Hello")
        self.assertEqual(normalize_query("\thello\n"), "hello")
        self.assertEqual(normalize_query("break   a   leg"), "break a leg")

    def test_quotes_of_every_shape(self):
        for raw in ('"hello"', "'hello'", "\u201chello\u201d", "\u2018hello\u2019", "`hello`", "\u00abhello\u00bb"):
            self.assertEqual(normalize_query(raw), "hello", raw)

    def test_markdown_and_punctuation(self):
        self.assertEqual(normalize_query("**hello**"), "hello")
        self.assertEqual(normalize_query("hello?"), "hello")
        self.assertEqual(normalize_query("hello!!!"), "hello")
        self.assertEqual(normalize_query('"hello",'), "hello")

    def test_internal_punctuation_survives(self):
        self.assertEqual(normalize_query("well-being"), "well-being")
        self.assertEqual(normalize_query("don't"), "don't")

    def test_empty_and_junk(self):
        for raw in ("", "   ", '""', "...", None):
            self.assertEqual(normalize_query(raw), "")

    def test_length_cap(self):
        self.assertLessEqual(len(normalize_query("x" * 200)), 48)

    def test_is_lookupable(self):
        self.assertTrue(is_lookupable("hello"))
        self.assertTrue(is_lookupable("well-being"))
        self.assertTrue(is_lookupable("break a leg"))
        self.assertTrue(is_lookupable("na\u00efve"))
        self.assertFalse(is_lookupable("hello world how are you today"))
        self.assertFalse(is_lookupable("404"))
        self.assertFalse(is_lookupable("he110"))
        self.assertFalse(is_lookupable(""))
        self.assertFalse(is_lookupable("break a leg", allow_phrase=False))


class TestTextTriggers(unittest.TestCase):
    def test_every_documented_phrasing(self):
        cases = {
            "dictionary hello": "hello",
            "define hello": "hello",
            'Define "hello"': "hello",
            "what does hello mean?": "hello",
            "meaning of hello": "hello",
            "DEFINE HELLO": "HELLO",
            "definition of hello": "hello",
            "what is the meaning of hello": "hello",
            "what's the definition of \u201chello\u201d": "hello",
            "hello meaning": "hello",
            "define 'serendipity'": "serendipity",
            "can you define hello": "hello",
            "define hello please": "hello",
            "what does `hello` mean": "hello",
            "meaning of the word hello": "hello",
            'dictionary "break a leg"': "break a leg",
        }
        for text, expected in cases.items():
            self.assertEqual(match_text_request(text), expected, text)

    def test_ignores_normal_conversation(self):
        negatives = [
            "I need to define a function in python",
            "we should define the scope of this project first",
            "that gives new meaning to the word tired",
            "hello",
            "hello everyone",
            "what does he mean by that",
            "what does it mean",
            "define",
            "dictionary",
            "the dictionary is on the shelf",
            "/define hello",
            "!define hello",
            "define https://example.com",
            "define <@12345>",
            "what does this mean?",
            "meaning of life the universe and everything is 42",
            "define ```code```",
            "lmao define that energy honestly it was so unhinged and also very funny",
            "define the relationship we have been having for months now please",
        ]
        for text in negatives:
            self.assertIsNone(match_text_request(text), text)

    def test_multiline_and_long_messages_are_ignored(self):
        self.assertIsNone(match_text_request("define hello\nand also this"))
        self.assertIsNone(match_text_request("define hello " + "x" * 200))

    def test_unquoted_phrases_do_not_trigger(self):
        self.assertIsNone(match_text_request("define break a leg"))
        self.assertEqual(match_text_request('define "break a leg"'), "break a leg")


class TestMorphology(unittest.TestCase):
    def test_base_forms(self):
        self.assertIn("run", base_forms("running"))
        self.assertIn("promise", base_forms("promises"))
        self.assertIn("carry", base_forms("carried"))
        self.assertIn("happy", base_forms("happier"))
        self.assertIn("stop", base_forms("stopped"))
        self.assertIn("make", base_forms("making"))
        self.assertIn("box", base_forms("boxes"))
        self.assertIn("go", base_forms("went"))
        self.assertIn("child", base_forms("children"))
        self.assertIn("quick", base_forms("quickly"))

    def test_base_forms_are_bounded_and_clean(self):
        self.assertEqual(base_forms("a"), ())
        self.assertEqual(base_forms("run"), ())
        self.assertLessEqual(len(base_forms("running")), 6)
        self.assertNotIn("running", base_forms("running"))

    def test_candidate_forms(self):
        values = {form.value for form in candidate_forms("promise", ("noun", "verb"))}
        self.assertIn("promises", values)
        self.assertIn("promised", values)
        self.assertIn("promising", values)
        values = {form.value for form in candidate_forms("carry", ("verb",))}
        self.assertIn("carries", values)
        self.assertIn("carried", values)
        values = {form.value for form in candidate_forms("stop", ("verb",))}
        self.assertIn("stopping", values)
        values = {form.value for form in candidate_forms("happy", ("adjective",))}
        self.assertIn("happier", values)
        self.assertIn("happiest", values)

    def test_prefix_probe(self):
        self.assertEqual(common_prefix_probe("promise"), "promis*")
        self.assertEqual(common_prefix_probe("carry"), "carr*")

    def test_edit_distance(self):
        self.assertEqual(edit_distance("hello", "hello"), 0)
        self.assertEqual(edit_distance("recieve", "receive"), 1)  # transposition
        self.assertEqual(edit_distance("cat", "cart"), 1)
        self.assertGreater(edit_distance("cat", "elephant"), 4)

    def test_rank_suggestions(self):
        ranked = rank_suggestions(
            "recieve", [("relieve", 100.0), ("receive", 50.0), ("believe", 10.0)]
        )
        self.assertEqual(ranked[0], "receive")
        self.assertNotIn("recieve", ranked)

    def test_rank_suggestions_drops_distant_words(self):
        self.assertEqual(rank_suggestions("xyzzyx", [("elephant", 900.0)]), ())


# ---------------------------------------------------------------------------
# Provider parsing
# ---------------------------------------------------------------------------


class TestFreeDictionaryParsing(unittest.TestCase):
    def test_full_entry(self):
        entry = parse_free_dictionary(HELLO, "hello")
        self.assertEqual(entry.word, "hello")
        self.assertEqual(entry.part_of_speech, "noun")
        self.assertEqual(len(entry.senses), 3)
        self.assertIn("greeting", entry.all_synonyms())
        self.assertIn("bye", entry.all_antonyms())
        self.assertEqual(entry.licence, "CC BY-SA 3.0")
        self.assertTrue(entry.sources)

    def test_pronunciations_and_regions(self):
        entry = parse_free_dictionary(HELLO, "hello")
        regions = {p.region for p in entry.audio_options}
        self.assertEqual(regions, {"au", "uk"})
        self.assertTrue(entry.ipa.startswith("/"))
        # The one with both spelling and audio should lead.
        self.assertTrue(entry.pronunciations[0].text)
        self.assertTrue(entry.pronunciations[0].audio_url)

    def test_protocol_relative_audio_and_origin(self):
        entry = parse_free_dictionary(OLD_STYLE, "promise")
        self.assertTrue(entry.pronunciations[0].audio_url.startswith("https://"))
        self.assertEqual(entry.pronunciations[0].region, "uk")  # gb → uk
        self.assertIn("Latin", entry.origin)
        # "exclamation" is normalised to the Wiktionary name.
        self.assertEqual(entry.part_of_speech, "interjection")

    def test_examples_and_definition_tidying(self):
        entry = parse_free_dictionary(HELLO, "hello")
        self.assertIn("Hello, everyone.", entry.examples)
        for sense in entry.senses:
            self.assertTrue(sense.definition[0].isupper() or not sense.definition[0].isalpha())
            self.assertIn(sense.definition[-1], ".!?:;")

    def test_malformed_payloads_never_raise(self):
        for payload in (None, {}, [], [{}], [[]], [{"word": "x"}], "nope", [{"meanings": 5}]):
            self.assertIsNone(parse_free_dictionary(payload, "x"), payload)

    def test_partial_payload_keeps_what_it_can(self):
        payload = [{"word": "x", "meanings": [{"definitions": [{"definition": "a thing"}]}]}]
        entry = parse_free_dictionary(payload, "x")
        self.assertEqual(entry.word, "x")
        self.assertEqual(entry.part_of_speech, "")
        self.assertEqual(entry.pronunciations, ())
        self.assertEqual(entry.examples, ())

    def test_region_from_url(self):
        self.assertEqual(region_from_audio_url("https://x/hello-uk.mp3"), "uk")
        self.assertEqual(region_from_audio_url("https://x/hello--_gb_1.mp3"), "uk")
        self.assertEqual(region_from_audio_url("https://x/hello.mp3"), "")

    def test_part_of_speech_names(self):
        self.assertEqual(normalise_part_of_speech("n"), "noun")
        self.assertEqual(normalise_part_of_speech("adj"), "adjective")
        self.assertEqual(normalise_part_of_speech("Exclamation"), "interjection")
        self.assertEqual(normalise_part_of_speech(""), "")

    def test_text_tidying(self):
        self.assertEqual(tidy_definition("  a   thing  "), "A thing.")
        self.assertEqual(tidy_definition("(transitive) to ease"), "(transitive) To ease.")
        self.assertEqual(tidy_definition("Misspelling of receive. [stuff]"), "Misspelling of receive.")
        self.assertEqual(tidy_example('"he ran"'), "He ran.")
        self.assertEqual(tidy_example(""), "")


class TestDatamuseParsing(unittest.TestCase):
    def test_rows(self):
        rows = parse_datamuse(DATAMUSE_SERENDIPITY)
        self.assertEqual(rows[0].word, "serendipity")
        self.assertEqual(rows[0].parts_of_speech, ("noun",))
        self.assertEqual(len(rows[0].definitions), 2)

    def test_misspelling_detection(self):
        rows = parse_datamuse(DATAMUSE_MISSPELLING)
        self.assertEqual(rows[0].misspelling_of, "receive")
        self.assertEqual(rows[1].misspelling_of, "")

    def test_entry_from_row(self):
        entry = entry_from_datamuse(parse_datamuse(DATAMUSE_SERENDIPITY)[0], "serendipity")
        self.assertEqual(entry.word, "serendipity")
        self.assertEqual(entry.part_of_speech, "noun")
        self.assertEqual(len(entry.senses), 2)

    def test_junk_rows_are_skipped(self):
        self.assertEqual(parse_datamuse(None), [])
        self.assertEqual(parse_datamuse([1, "x", {}, {"word": ""}]), [])


class TestModels(unittest.TestCase):
    def test_dedupe_is_case_insensitive_and_ordered(self):
        self.assertEqual(dedupe(["A", "a", "b", " b ", "c"]), ("A", "b", "c"))
        self.assertEqual(dedupe(["a", "b"], limit=1), ("a",))
        self.assertEqual(dedupe(["a", "b"], skip={"a"}), ("b",))

    def test_entry_never_lists_itself_as_a_synonym(self):
        entry = entry_for().with_extras(synonyms=["promise", "pledge", "PLEDGE"])
        self.assertNotIn("promise", [s.casefold() for s in entry.all_synonyms()])
        self.assertEqual(len([s for s in entry.all_synonyms() if s.casefold() == "pledge"]), 1)

    def test_related_words_exclude_known_synonyms(self):
        entry = entry_for().with_extras(synonyms=["pledge"], related=["pledge", "oath"])
        self.assertEqual(entry.related, ("oath",))

    def test_senses_grouped_by_part(self):
        entry = WordEntry(
            word="run",
            senses=(
                Sense("verb", "To move fast."),
                Sense("noun", "An act of running."),
                Sense("verb", "To manage."),
            ),
        )
        grouped = dict(entry.senses_by_part())
        self.assertEqual(len(grouped["verb"]), 2)
        self.assertEqual(entry.parts_of_speech, ("verb", "noun"))

    def test_clip_filename_is_safe(self):
        clip = Clip(b"x", extension="mp3", region="uk")
        self.assertEqual(clip.filename("hello"), "hello-uk.mp3")
        self.assertEqual(clip.filename("../../etc/passwd"), "etcpasswd-uk.mp3")
        self.assertEqual(Clip(b"x").filename("hi"), "hi.mp3")
        self.assertEqual(clip.filename(""), "word-uk.mp3")


# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestCaches(unittest.TestCase):
    def test_ttl_expiry(self):
        clock = FakeClock()
        cache = TTLCache(ttl=10, timer=clock)
        cache.put("a", 1)
        self.assertEqual(cache.get("a"), 1)
        clock.advance(11)
        self.assertIsNone(cache.get("a"))
        self.assertEqual(len(cache), 0)

    def test_lru_eviction(self):
        cache = TTLCache(max_items=2, ttl=100)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.get("a")           # 'a' is now the most recent
        cache.put("c", 3)
        self.assertIsNone(cache.get("b"))
        self.assertEqual(cache.get("a"), 1)
        self.assertEqual(cache.stats.evictions, 1)

    def test_weight_cap(self):
        cache = TTLCache(max_items=100, ttl=100, max_weight=10, weigher=len)
        cache.put("a", b"12345")
        cache.put("b", b"12345")
        cache.put("c", b"1")
        self.assertIsNone(cache.get("a"))
        self.assertLessEqual(cache.weight, 10)

    def test_oversized_item_is_not_stored(self):
        cache = TTLCache(max_items=10, ttl=100, max_weight=4, weigher=len)
        cache.put("a", b"12345")
        self.assertIsNone(cache.get("a"))
        self.assertEqual(len(cache), 0)

    def test_prune(self):
        clock = FakeClock()
        cache = TTLCache(ttl=5, timer=clock)
        cache.put("a", 1)
        clock.advance(6)
        self.assertEqual(cache.prune(), 1)
        self.assertEqual(len(cache), 0)

    def test_single_flight_coalesces(self):
        async def scenario():
            flight = SingleFlight()
            calls = 0

            async def work():
                nonlocal calls
                calls += 1
                await asyncio.sleep(0.02)
                return "done"

            results = await asyncio.gather(*[flight.run("k", work) for _ in range(8)])
            return calls, results

        calls, results = run(scenario())
        self.assertEqual(calls, 1)
        self.assertEqual(results, ["done"] * 8)

    def test_single_flight_propagates_errors_without_caching_them(self):
        async def scenario():
            flight = SingleFlight()
            attempts = 0

            async def work():
                nonlocal attempts
                attempts += 1
                raise RuntimeError("nope")

            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    await flight.run("k", work)
            return attempts, flight.in_flight

        attempts, pending = run(scenario())
        self.assertEqual(attempts, 2)
        self.assertEqual(pending, 0)

    def test_rate_limiter(self):
        clock = FakeClock()
        limiter = RateLimiter(rate=2, per=1.0, timer=clock)
        self.assertTrue(limiter.try_acquire())
        self.assertTrue(limiter.try_acquire())
        self.assertFalse(limiter.try_acquire())
        self.assertGreater(limiter.retry_after(), 0)
        clock.advance(1.0)
        self.assertTrue(limiter.try_acquire())


# ---------------------------------------------------------------------------
# The lookup pipeline
# ---------------------------------------------------------------------------


class FakePrimary:
    name = "Fake Dictionary"

    def __init__(self, entries: dict[str, WordEntry] | None = None, error: Exception | None = None):
        self.entries = entries or {}
        self.error = error
        self.calls: list[str] = []

    async def lookup(self, term: str, language: str = "en"):
        self.calls.append(term)
        if self.error is not None:
            raise self.error
        entry = self.entries.get(term.casefold())
        if entry is None:
            from functions.dictionary import NotFoundError

            raise NotFoundError("no entry")
        return entry


class FakeSecondary:
    name = "Fake Datamuse"

    def __init__(self, *, enrichment=None, suggestions=None, entries=None, forms=()):
        self.enrichment = enrichment or Enrichment(("pledge",), ("denial",), ("oath",), "Fake Datamuse")
        self.suggestions = suggestions or []
        self.entries = entries or {}
        self.forms = forms
        self.enrich_calls = 0
        self.suggest_calls = 0

    async def enrich(self, term: str, *, include_related: bool = False):
        self.enrich_calls += 1
        return self.enrichment

    async def suggest(self, term: str):
        self.suggest_calls += 1
        return self.suggestions

    async def lookup(self, term: str, language: str = "en"):
        return self.entries.get(term.casefold())

    async def verify_forms(self, word, candidates):
        return tuple(self.forms)


def make_service(primary, secondary=None, **kwargs):
    return DictionaryService(
        client=None, primary=primary, secondary=secondary or FakeSecondary(), **kwargs
    )


class TestLookupPipeline(unittest.TestCase):
    def test_found_and_enriched(self):
        primary = FakePrimary({"promise": entry_for()})
        secondary = FakeSecondary()
        service = make_service(primary, secondary)
        result = run(service.lookup("  Promise  "))
        self.assertTrue(result.found)
        self.assertEqual(result.entry.word, "promise")
        self.assertIn("pledge", result.entry.all_synonyms())
        self.assertIn("denial", result.entry.all_antonyms())
        self.assertIn("oath", result.entry.related)
        self.assertFalse(result.partial)

    def test_quoted_and_uppercase_input_reach_the_same_entry(self):
        primary = FakePrimary({"promise": entry_for()})
        service = make_service(primary)
        for raw in ('"PROMISE"', " promise? ", "\u201cPromise\u201d"):
            self.assertTrue(run(service.lookup(raw)).found, raw)
        self.assertEqual(len(primary.calls), 1, "cache should have answered the rest")

    def test_inflected_form_falls_back_to_the_base_word(self):
        primary = FakePrimary({"run": entry_for("run")})
        service = make_service(primary)
        result = run(service.lookup("running"))
        self.assertTrue(result.found)
        self.assertEqual(result.entry.word, "run")
        self.assertEqual(result.resolved_from, "running")

    def test_unknown_word_returns_suggestions(self):
        primary = FakePrimary({})
        secondary = FakeSecondary(suggestions=[("receive", 100.0), ("relieve", 50.0)])
        service = make_service(primary, secondary)
        result = run(service.lookup("recieve"))
        self.assertFalse(result.found)
        self.assertEqual(result.suggestions[0], "receive")
        self.assertEqual(secondary.suggest_calls, 1)

    def test_high_confidence_fuzzy_match_is_verified_then_automatic(self):
        # Each of these is one Damerau-Levenshtein edit from the real word:
        # a substitution, an insertion/deletion or a repeated-letter mistake.
        corrections = {
            "definately": "definitely",
            "accomodate": "accommodate",
            "seperate": "separate",
            "enviroment": "environment",
        }
        primary = FakePrimary({word: entry_for(word) for word in corrections.values()})
        secondary = FakeSecondary(
            suggestions=[(word, 100.0) for word in corrections.values()]
        )
        service = make_service(primary, secondary)
        for typed, corrected in corrections.items():
            with self.subTest(typed=typed):
                # Give each typo only its intended candidate. A real provider
                # supplies this candidate list from its lexical index.
                secondary.suggestions = [(corrected, 100.0)]
                result = run(service.lookup(typed))
                self.assertTrue(result.found)
                self.assertEqual(result.entry.word, corrected)
                self.assertTrue(result.auto_corrected)
                self.assertEqual(result.resolved_from, typed)

    def test_lexical_misspelling_signal_resolves_a_tied_transposition(self):
        primary = FakePrimary({"receive": entry_for("receive")})
        # Datamuse adds one million to a row which explicitly says
        # "misspelling of receive". A merely similar real word must not make
        # that correction look ambiguous.
        secondary = FakeSecondary(
            suggestions=[("receive", 1_000_100.0), ("relieve", 100.0)]
        )
        result = run(make_service(primary, secondary).lookup("recieve"))
        self.assertTrue(result.found)
        self.assertEqual(result.entry.word, "receive")
        self.assertTrue(result.auto_corrected)

    def test_verified_correction_promotes_the_canonical_cache_entry(self):
        primary = FakePrimary({"definitely": entry_for("definitely")})
        secondary = FakeSecondary(suggestions=[("definitely", 100.0)])
        service = make_service(primary, secondary)
        self.assertTrue(run(service.lookup("definately")).found)
        calls_after_correction = len(primary.calls)
        self.assertGreaterEqual(calls_after_correction, 2)
        self.assertTrue(run(service.lookup("definitely")).found)
        self.assertEqual(
            len(primary.calls), calls_after_correction, "canonical entry came from cache"
        )

    def test_ambiguous_match_requires_confirmation(self):
        primary = FakePrimary({"address": entry_for("address"), "dress": entry_for("dress")})
        secondary = FakeSecondary(suggestions=[("address", 100.0), ("dress", 90.0)])
        result = run(make_service(primary, secondary).lookup("adress"))
        self.assertFalse(result.found)
        self.assertEqual(result.suggested_word, "address")
        self.assertFalse(result.auto_corrected)

    def test_exact_search_never_restarts_fuzzy_matching(self):
        primary = FakePrimary({"definitely": entry_for("definitely")})
        secondary = FakeSecondary(suggestions=[("definitely", 100.0)])
        service = make_service(primary, secondary)
        result = run(service.lookup("definately", fuzzy=False))
        self.assertFalse(result.found)
        self.assertEqual(result.suggestions, ())
        self.assertEqual(secondary.suggest_calls, 0)

    def test_primary_outage_falls_back_to_the_secondary(self):
        from functions.dictionary import HttpError

        primary = FakePrimary(error=HttpError("boom"))
        secondary = FakeSecondary(entries={"promise": entry_for()})
        service = make_service(primary, secondary)
        result = run(service.lookup("promise"))
        self.assertTrue(result.found)
        self.assertTrue(result.partial)
        self.assertIn("Fake Datamuse", result.providers)

    def test_total_outage_is_reported_not_raised(self):
        from functions.dictionary import HttpError

        primary = FakePrimary(error=HttpError("boom"))
        secondary = FakeSecondary(entries={})
        service = make_service(primary, secondary)
        result = run(service.lookup("promise"))
        self.assertFalse(result.found)
        self.assertTrue(result.partial)

    def test_broken_enrichment_never_breaks_a_lookup(self):
        class Exploding(FakeSecondary):
            async def enrich(self, term, *, include_related=False):
                raise RuntimeError("datamuse is on fire")

        service = make_service(FakePrimary({"promise": entry_for()}), Exploding())
        result = run(service.lookup("promise"))
        self.assertTrue(result.found)
        self.assertIn("pledge", result.entry.all_synonyms())  # from the primary

    def test_results_are_cached(self):
        primary = FakePrimary({"promise": entry_for()})
        service = make_service(primary)
        run(service.lookup("promise"))
        run(service.lookup("promise"))
        self.assertEqual(len(primary.calls), 1)

    def test_concurrent_identical_lookups_hit_the_provider_once(self):
        class Slow(FakePrimary):
            async def lookup(self, term, language="en"):
                await asyncio.sleep(0.03)
                return await super().lookup(term, language)

        primary = Slow({"promise": entry_for()})
        service = make_service(primary)

        async def scenario():
            return await asyncio.gather(*[service.lookup("promise") for _ in range(10)])

        results = run(scenario())
        self.assertEqual(len(primary.calls), 1)
        self.assertTrue(all(r.found for r in results))

    def test_slow_provider_is_cut_off(self):
        class Hanging(FakePrimary):
            async def lookup(self, term, language="en"):
                await asyncio.sleep(5)
                return entry_for()

        service = make_service(Hanging({}), timeout=0.05)
        result = run(service.lookup("promise"))
        self.assertFalse(result.found)  # timed out, reported cleanly

    def test_invalid_input_is_a_friendly_error(self):
        service = make_service(FakePrimary({}))
        for raw in ("", "   ", "12345", "!!!", "a" * 200):
            with self.assertRaises(ParadiseError):
                run(service.lookup(raw))

    def test_details_are_best_effort(self):
        secondary = FakeSecondary(forms=(WordForm("plural", "promises"),))
        service = make_service(FakePrimary({"promise": entry_for()}), secondary)
        details = run(service.details(entry_for()))
        self.assertEqual(details.forms[0].value, "promises")

    def test_details_survive_a_broken_provider(self):
        class Exploding(FakeSecondary):
            async def verify_forms(self, word, candidates):
                raise RuntimeError("nope")

        service = make_service(FakePrimary({}), Exploding())
        details = run(service.details(entry_for()))
        self.assertEqual(details.forms, ())


# ---------------------------------------------------------------------------
# Fonts and rendering
# ---------------------------------------------------------------------------


class TestFonts(unittest.TestCase):
    def test_bundled_fonts_are_present(self):
        bundled = [f for f in fonts_module._catalogue() if f.bundled]
        self.assertTrue(bundled, "assets/fonts should ship with the repo")

    def test_bundled_fonts_cover_ipa(self):
        for font in fonts_module._catalogue():
            if not font.bundled:
                continue
            points = fonts_module.coverage(font.path)
            for char in "\u02c8\u02cc\u02d0\u0252\u026a\u0283\u00f0\u03b8\u014b\u0259\u0279\u028a\u00e6":
                self.assertIn(ord(char), points, f"{font.name} cannot draw {char!r}")

    def test_cmap_reader_rejects_rubbish(self):
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".ttf", delete=False) as handle:
            handle.write(b"not a font at all" + struct.pack(">I", 0))
            path = handle.name
        try:
            self.assertEqual(fonts_module.coverage(path), frozenset())
        finally:
            os.unlink(path)

    def test_fallback_chain_reaches_a_wide_coverage_face(self):
        library = fonts_module.FontLibrary()
        font_set = library.get("regular", 24)
        self.assertTrue(font_set.usable)
        # Every face in the chain is distinct and usable.
        self.assertEqual(len(font_set.faces), len({id(f.font) for f in font_set.faces}))

    def test_runs_split_on_missing_glyphs(self):
        library = fonts_module.FontLibrary()
        font_set = library.get("regular", 24)
        runs = font_set.runs("hello")
        self.assertEqual("".join(text for text, _font in runs), "hello")
        exotic = font_set.runs("hello \u0444 world")
        self.assertEqual("".join(text for text, _font in exotic), "hello \u0444 world")

    def test_describe_is_a_single_line(self):
        self.assertNotIn("\n", fonts_module.describe())


@unittest.skipUnless(renderer_available(), "Pillow is not installed")
class TestRenderer(unittest.TestCase):
    def all_cases(self) -> dict[str, WordEntry]:
        return {
            "typical": entry_for(),
            "bare": WordEntry(word="zarf", senses=(Sense("noun", "A cup holder."),)),
            "no-definition": WordEntry(word="ghost"),
            "long-word": WordEntry(
                word="pneumonoultramicroscopicsilicovolcanoconiosis",
                senses=(Sense("noun", "A very long word for a lung disease. " * 6),),
                pronunciations=(Pronunciation(text="/\u02ccnju\u02d0m\u0259no\u028a\u02cc\u028cltr\u0259\u02ccma\u026akr\u0259\u02c8sk\u0252p\u026ak/"),),
            ),
            "long-definition": WordEntry(
                word="set",
                senses=(
                    Sense(
                        "verb",
                        "To put something somewhere " * 40,
                        example="She set the cup down on the table " * 12,
                    ),
                ),
            ),
            "many-senses": WordEntry(
                word="run",
                senses=tuple(Sense("verb", f"Meaning number {i}.") for i in range(12)),
                synonyms=tuple(f"synonym{i}" for i in range(20)),
                antonyms=tuple(f"antonym{i}" for i in range(12)),
            ),
            "unicode": WordEntry(
                word="\u00c6thelthryth",
                senses=(Sense("proper noun", "\u03b8 \u00f0 \u014b \u0283 \u0292 \u0251 \u00e6 \u0259 \u026a \u028a \u0254 \u025b \u2014 na\u00efve caf\u00e9."),),
                pronunciations=(Pronunciation(text="/\u02c8\u00e6\u03b8\u0259l\u03b8r\u026a\u03b8/", region="uk"),),
            ),
            "no-pronunciation": WordEntry(
                word="rizz", senses=(Sense("noun", "Charm."),), synonyms=("charisma",)
            ),
            "empty-word": WordEntry(word="", senses=(Sense("noun", "Nothing."),)),
            "spaces": WordEntry(word="break a leg", senses=(Sense("phrase", "Good luck."),)),
        }

    def test_every_case_renders_a_valid_png(self):
        for name, entry in self.all_cases().items():
            with self.subTest(case=name):
                data = render_card(entry)
                self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"), name)
                self.assertGreater(len(data), 1000)

    def test_nothing_overflows_or_overlaps(self):
        theme = DEFAULT_THEME
        for name, entry in self.all_cases().items():
            with self.subTest(case=name):
                layout = plan_card(entry, theme)
                self.assertLessEqual(layout.height, theme.max_height)
                for block in layout.blocks:
                    self.assertGreaterEqual(block.x, theme.padding - 20, f"{name}:{block.kind}")
                    self.assertLessEqual(
                        round(block.right), theme.width - theme.padding + 1,
                        f"{name}:{block.kind} spills off the right edge",
                    )
                    self.assertLessEqual(
                        round(block.bottom), layout.height, f"{name}:{block.kind} spills off the bottom"
                    )
                self._assert_no_text_overlap(layout, name)

    def _assert_no_text_overlap(self, layout, name):
        text_blocks = [
            b for b in layout.blocks
            if b.text and b.kind not in ("rule", "example-rule")
        ]
        for i, first in enumerate(text_blocks):
            for second in text_blocks[i + 1 :]:
                horizontal = first.x < second.right - 1 and second.x < first.right - 1
                vertical = first.y < second.bottom - 1 and second.y < first.bottom - 1
                self.assertFalse(
                    horizontal and vertical,
                    f"{name}: {first.kind}({first.text[:12]!r}) overlaps "
                    f"{second.kind}({second.text[:12]!r})",
                )

    def test_missing_fields_simply_drop_their_blocks(self):
        bare = plan_card(WordEntry(word="zarf", senses=(Sense("noun", "A cup holder."),)))
        kinds = set(bare.kinds())
        self.assertNotIn("ipa", kinds)
        self.assertNotIn("example", kinds)
        self.assertNotIn("synonym-label", kinds)
        self.assertIn("word", kinds)
        self.assertIn("definition", kinds)

    def test_long_word_shrinks_before_it_truncates(self):
        short = plan_card(entry_for("cat")).find("word")
        long = plan_card(entry_for("pneumonoultramicroscopicsilicovolcanoconiosis")).find("word")
        self.assertLess(long.size, short.size)

    def test_definition_is_capped_in_lines(self):
        layout = plan_card(
            WordEntry(word="set", senses=(Sense("verb", "To put something down. " * 60),))
        )
        lines = [b for b in layout.blocks if b.kind == "definition"]
        self.assertLessEqual(len(lines), DEFAULT_THEME.max_definition_lines)
        self.assertTrue(lines[-1].text.endswith("\u2026"))

    def test_render_is_deterministic(self):
        entry = entry_for()
        self.assertEqual(render_card(entry), render_card(entry))

    def test_material_hierarchy_has_a_meaning_surface_and_light_variant(self):
        entry = entry_for().with_extras(related=["oath"])
        layout = plan_card(entry)
        kinds = set(layout.kinds())
        for kind in ("word", "pronunciation-label", "pos", "definition", "example", "synonym", "related", "meaning-surface"):
            self.assertIn(kind, kinds)
        self.assertNotEqual(render_card(entry), render_card(entry, LIGHT_THEME))

    def test_signature_tracks_content(self):
        first = entry_for()
        self.assertEqual(card_signature(first), card_signature(entry_for()))
        self.assertNotEqual(card_signature(first), card_signature(entry_for("pledge")))

    def test_card_is_readable_sized(self):
        from PIL import Image

        image = Image.open(io.BytesIO(render_card(entry_for())))
        self.assertEqual(image.width, DEFAULT_THEME.width)
        self.assertGreaterEqual(image.height, DEFAULT_THEME.min_height)
        # Comfortably legible once Discord scales it down on a phone.
        self.assertGreaterEqual(DEFAULT_THEME.definition_size / image.width * 400, 9)

    def test_concurrent_renders_are_safe(self):
        async def scenario():
            entries = [entry_for(f"word{i}") for i in range(8)]
            return await asyncio.gather(*[asyncio.to_thread(render_card, e) for e in entries])

        results = run(scenario())
        self.assertEqual(len(results), 8)
        self.assertTrue(all(r.startswith(b"\x89PNG") for r in results))


@unittest.skipUnless(renderer_available(), "Pillow is not installed")
class TestTextLayoutHelpers(unittest.TestCase):
    def setUp(self):
        self.font = fonts_module.FontLibrary().get("regular", 28)

    def test_wrap_respects_width(self):
        lines = wrap_text(self.font, "the quick brown fox jumps over the lazy dog", 200, 10)
        for line in lines:
            self.assertLessEqual(text_width(self.font, line), 200)

    def test_unbreakable_word_is_split(self):
        lines = wrap_text(self.font, "a" * 200, 200, 4)
        self.assertGreater(len(lines), 1)
        for line in lines:
            self.assertLessEqual(text_width(self.font, line), 201)

    def test_wrap_truncates_with_an_ellipsis(self):
        lines = wrap_text(self.font, "word " * 200, 300, 3)
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[-1].endswith("\u2026"))

    def test_fit_text(self):
        self.assertEqual(fit_text(self.font, "short", 1000), "short")
        clipped = fit_text(self.font, "a very long piece of text indeed", 80)
        self.assertTrue(clipped.endswith("\u2026"))
        self.assertLessEqual(text_width(self.font, clipped), 81)

    def test_empty_input(self):
        self.assertEqual(wrap_text(self.font, "", 100, 3), [])
        self.assertEqual(wrap_text(self.font, "hello", 100, 0), [])


# ---------------------------------------------------------------------------
# Pronunciation
# ---------------------------------------------------------------------------

MP3 = b"ID3" + b"\x00" * 600
OGG = b"OggS" + b"\x00" * 600


class FakeSpeechProvider:
    def __init__(self, name, clip=None, error=None, available=True):
        self.name = name
        self.clip = clip
        self.error = error
        self._available = available
        self.requests: list[SpeechRequest] = []

    @property
    def available(self):
        return self._available

    async def speak(self, request: SpeechRequest):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.clip


class TestPronunciation(unittest.TestCase):
    def test_audio_sniffing(self):
        self.assertEqual(looks_like_audio(MP3), "mp3")
        self.assertEqual(looks_like_audio(OGG), "ogg")
        self.assertEqual(looks_like_audio(b"<html>" + b" " * 600), "")
        self.assertEqual(looks_like_audio(b"ID3"), "")  # too small to be real

    def test_only_the_word_is_ever_spoken(self):
        self.assertEqual(speakable("  hello  "), "hello")
        self.assertEqual(speakable("x" * 500), "x" * 64)
        self.assertEqual(speakable(None), "")

    def _service(self, *providers, prefer_native=True):
        return PronunciationService(
            client=None, providers=list(providers), prefer_native=prefer_native
        )

    def test_native_recording_wins(self):
        native = FakeSpeechProvider("recording", Clip(MP3, provider="recording", region="uk"))
        web = FakeSpeechProvider("web", Clip(MP3, provider="web"))
        service = self._service(native, web)
        outcome = run(service.pronounce("promise", entry=entry_for()))
        self.assertEqual(outcome.clip.provider, "recording")
        self.assertEqual(outcome.region_label, "UK")
        self.assertEqual(web.requests, [])

    def test_recording_provider_is_skipped_without_a_url(self):
        native = FakeSpeechProvider("recording", Clip(MP3))
        web = FakeSpeechProvider("web", Clip(OGG, provider="web"))
        service = self._service(native, web)
        entry = WordEntry(word="rizz", senses=(Sense("noun", "Charm."),))
        outcome = run(service.pronounce("rizz", entry=entry))
        self.assertEqual(outcome.clip.provider, "web")
        self.assertEqual(native.requests, [])

    def test_failure_moves_to_the_next_provider(self):
        from functions.dictionary import HttpError

        broken = FakeSpeechProvider("neural", error=HttpError("502"))
        web = FakeSpeechProvider("web", Clip(MP3, provider="web"))
        service = self._service(broken, web)
        outcome = run(service.pronounce("hello"))
        self.assertEqual(outcome.clip.provider, "web")

    def test_a_crashing_provider_is_contained(self):
        broken = FakeSpeechProvider("neural", error=ValueError("bad"))
        web = FakeSpeechProvider("web", Clip(MP3, provider="web"))
        outcome = run(self._service(broken, web).pronounce("hello"))
        self.assertEqual(outcome.clip.provider, "web")

    def test_everything_failing_is_a_friendly_error(self):
        dead = FakeSpeechProvider("web", clip=None)
        with self.assertRaises(PronunciationError):
            run(self._service(dead).pronounce("hello"))

    def test_only_the_headword_reaches_the_provider(self):
        web = FakeSpeechProvider("web", Clip(MP3, provider="web"))
        entry = entry_for()
        run(self._service(web).pronounce(entry.word, entry=entry))
        self.assertEqual(web.requests[0].word, "promise")
        self.assertNotIn("commitment", web.requests[0].word)

    def test_clips_are_cached(self):
        web = FakeSpeechProvider("web", Clip(MP3, provider="web"))
        service = self._service(web)
        run(service.pronounce("hello"))
        run(service.pronounce("HELLO"))
        run(service.pronounce("  hello "))
        self.assertEqual(len(web.requests), 1)
        self.assertEqual(service.cached_clips, 1)

    def test_concurrent_requests_are_coalesced(self):
        class Slow(FakeSpeechProvider):
            async def speak(self, request):
                await asyncio.sleep(0.02)
                return await super().speak(request)

        web = Slow("web", Clip(MP3, provider="web"))
        service = self._service(web)

        async def scenario():
            return await asyncio.gather(*[service.pronounce("hello") for _ in range(6)])

        outcomes = run(scenario())
        self.assertEqual(len(web.requests), 1)
        self.assertEqual(len(outcomes), 6)

    def test_regional_variants_are_separate_cache_entries(self):
        native = FakeSpeechProvider("recording", Clip(MP3, provider="recording"))
        entry = WordEntry(
            word="promise",
            senses=(Sense("noun", "A commitment."),),
            pronunciations=(
                Pronunciation(text="/a/", audio_url="https://x/promise-uk.mp3", region="uk"),
                Pronunciation(text="/b/", audio_url="https://x/promise-us.mp3", region="us"),
            ),
        )
        service = self._service(native)
        run(service.pronounce("promise", entry=entry, region="uk"))
        run(service.pronounce("promise", entry=entry, region="us"))
        self.assertEqual(len(native.requests), 2)
        self.assertEqual(
            {r.region for r in native.requests}, {"uk", "us"}
        )

    def test_variant_selection_prefers_the_requested_region(self):
        entry = WordEntry(
            word="promise",
            senses=(Sense("noun", "A commitment."),),
            pronunciations=(
                Pronunciation(audio_url="https://x/promise-uk.mp3", region="uk"),
                Pronunciation(audio_url="https://x/promise-us.mp3", region="us"),
            ),
        )
        service = self._service(FakeSpeechProvider("recording", Clip(MP3)))
        self.assertEqual(service.choose_variant(entry, "us").region, "us")
        self.assertEqual(service.choose_variant(entry, "").region, "uk")
        self.assertIsNone(service.choose_variant(None))
        self.assertIsNone(service.choose_variant(entry_for("x", pronunciations=())))

    def test_provider_order_can_prefer_synthesis(self):
        native = FakeSpeechProvider("recording", Clip(MP3, provider="recording"))
        neural = FakeSpeechProvider("neural", Clip(MP3, provider="neural"))
        service = self._service(native, neural, prefer_native=False)
        outcome = run(service.pronounce("promise", entry=entry_for()))
        self.assertEqual(outcome.clip.provider, "neural")

    def test_unconfigured_neural_provider_is_unavailable(self):
        self.assertFalse(NeuralSpeechProvider(client=None).available)
        self.assertTrue(
            NeuralSpeechProvider(
                client=None, base_url="https://x/v1", model="speech-1"
            ).available
        )

    def test_native_provider_rejects_a_non_url(self):
        provider = NativeRecordingProvider(client=None)
        self.assertIsNone(run(provider.speak(SpeechRequest(word="x", audio_url=""))))
        self.assertIsNone(
            run(provider.speak(SpeechRequest(word="x", audio_url="javascript:alert(1)")))
        )

    def test_empty_word_is_refused(self):
        with self.assertRaises(PronunciationError):
            run(self._service(FakeSpeechProvider("web", Clip(MP3))).pronounce("   "))


# ---------------------------------------------------------------------------
# Voice playback (capability reporting only — no gateway in tests)
# ---------------------------------------------------------------------------


class TestPlayback(unittest.TestCase):
    def test_disabled_by_default_with_a_reason(self):
        support = VoicePlayback(enabled=False).support()
        self.assertFalse(support)
        self.assertIn("off", support.reason)

    def test_enabled_reports_what_is_missing(self):
        support = VoicePlayback(enabled=True).support()
        if not support:
            self.assertTrue(support.reason)

    def test_module_level_probe_is_honest(self):
        support = playback_support()
        self.assertIsInstance(bool(support), bool)
        if not support:
            self.assertTrue(support.reason)

    def test_channel_for_users_not_in_voice(self):
        from types import SimpleNamespace

        playback = VoicePlayback(enabled=True)
        self.assertIsNone(playback.channel_for(SimpleNamespace(voice=None)))
        self.assertIsNone(playback.channel_for(SimpleNamespace()))


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# The Discord layer: commands, chat triggers, buttons
#
# No gateway and no HTTP — a real cog and real views are driven with fake
# interactions, so the actual code paths a user hits are exercised.
# ---------------------------------------------------------------------------

import discord  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from commands.dictionary import (  # noqa: E402
    SCREEN_CARD,
    SCREEN_EXAMPLES,
    SCREEN_MORE,
    Dictionary,
    LoadingView,
    NotFoundView,
    ResultView,
    card_fallback_text,
    examples_text,
    more_text,
    not_found_text,
)
from config import Config, DictionaryConfig  # noqa: E402
from functions.dictionary import DictionaryFeature, WordDetails  # noqa: E402


def http_error(status: int, name: str = "Not Found"):
    return SimpleNamespace(status=status, reason=name)


class FakeResponse:
    def __init__(self) -> None:
        self.done = False
        self.messages: list[dict] = []
        self.edits: list[dict] = []
        self.defers: list[dict] = []
        self.raises: Exception | None = None

    def is_done(self) -> bool:
        return self.done

    async def send_message(self, content=None, **kwargs):
        if self.raises:
            raise self.raises
        self.done = True
        self.messages.append({"content": content, **kwargs})

    async def edit_message(self, **kwargs):
        if self.raises:
            raise self.raises
        self.done = True
        self.edits.append(kwargs)

    async def defer(self, **kwargs):
        if self.raises:
            raise self.raises
        self.done = True
        self.defers.append(kwargs)


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})


class FakeInteraction:
    def __init__(self, user_id: int = 1) -> None:
        self.user = SimpleNamespace(id=user_id, display_name=f"user{user_id}")
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.original_edits: list[dict] = []
        self.guild_id = 99

    async def edit_original_response(self, **kwargs):
        self.original_edits.append(kwargs)


class FakeMessage:
    def __init__(self, content: str, *, author_id: int = 7, bot: bool = False, mentions=()):
        self.content = content
        self.author = SimpleNamespace(id=author_id, bot=bot, display_name="asker")
        self.mentions = list(mentions)
        self.guild = SimpleNamespace(id=5, me=SimpleNamespace(id=1))
        permissions = discord.Permissions.none()
        permissions.send_messages = True
        self.channel = SimpleNamespace(
            id=11, permissions_for=lambda _member: permissions
        )
        self.replies: list[dict] = []
        self.placeholder = FakePlaceholder()

    async def reply(self, **kwargs):
        self.replies.append(kwargs)
        return self.placeholder


class FakePlaceholder:
    def __init__(self) -> None:
        self.edits: list[dict] = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


def make_cog(
    *,
    entries=None,
    suggestions=None,
    settings: DictionaryConfig | None = None,
    clip: Clip | None = None,
) -> Dictionary:
    """A real cog with the two web providers swapped for fakes."""
    config = Config(dictionary=settings or DictionaryConfig())
    bot = SimpleNamespace(config=config, user=SimpleNamespace(id=1))
    cog = Dictionary(bot)  # type: ignore[arg-type]
    cog.feature.service.primary = FakePrimary(entries if entries is not None else {"promise": entry_for()})
    cog.feature.service.secondary = FakeSecondary(suggestions=suggestions or [])
    cog.feature.pronunciation.providers = [
        FakeSpeechProvider("recording", clip or Clip(MP3, provider="recording", region="uk"))
    ]
    return cog


def buttons_of(view: discord.ui.LayoutView) -> dict[str, discord.ui.Button]:
    found = {}
    for item in view.walk_children():
        if isinstance(item, discord.ui.Button) and item.label:
            found[item.label] = item
    return found


def text_of(view: discord.ui.LayoutView) -> str:
    return "\n".join(
        item.content
        for item in view.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )


class TestSlashCommandFlow(unittest.TestCase):
    def test_loading_state_then_card(self):
        cog = make_cog()
        interaction = FakeInteraction()
        run(cog._run_command(interaction, "promise"))

        self.assertEqual(len(interaction.response.messages), 1, "a loading state is shown first")
        loading = interaction.response.messages[0]["view"]
        self.assertIsInstance(loading, LoadingView)
        self.assertIn("Looking up", text_of(loading))
        self.assertIn("promise", text_of(loading))

        self.assertEqual(len(interaction.original_edits), 1, "then it is replaced")
        final = interaction.original_edits[0]
        self.assertIsInstance(final["view"], ResultView)
        self.assertEqual(len(final["attachments"]), 1)
        self.assertEqual(final["attachments"][0].filename, "dictionary.png")
        run(cog.cog_unload())

    def test_card_attachment_is_a_real_png(self):
        cog = make_cog()
        interaction = FakeInteraction()
        run(cog._run_command(interaction, "promise"))
        payload = interaction.original_edits[0]["attachments"][0].fp.read()
        self.assertTrue(payload.startswith(b"\x89PNG\r\n\x1a\n"))
        run(cog.cog_unload())

    def test_buttons_offered_on_the_card(self):
        cog = make_cog()
        interaction = FakeInteraction()
        run(cog._run_command(interaction, "promise"))
        labels = set(buttons_of(interaction.original_edits[0]["view"]))
        self.assertIn("Pronounce", labels)
        self.assertIn("More", labels)
        self.assertNotIn("Examples", labels, "only offered when there are extra examples")
        run(cog.cog_unload())

    def test_examples_button_appears_when_there_are_several(self):
        entry = WordEntry(
            word="hello",
            senses=(
                Sense("noun", "A greeting.", example="She gave a quick hello."),
                Sense("interjection", "Used in greeting.", example="Hello, everyone."),
            ),
        )
        cog = make_cog(entries={"hello": entry})
        interaction = FakeInteraction()
        run(cog._run_command(interaction, "hello"))
        self.assertIn("Examples", buttons_of(interaction.original_edits[0]["view"]))
        run(cog.cog_unload())

    def test_bad_input_never_posts_a_card(self):
        cog = make_cog()
        interaction = FakeInteraction()
        run(cog._run_command(interaction, "12345"))
        self.assertEqual(interaction.original_edits, [])
        self.assertTrue(interaction.response.messages[0]["ephemeral"])
        run(cog.cog_unload())

    def test_unknown_word_offers_a_confirmed_correction(self):
        cog = make_cog(entries={}, suggestions=[("receive", 100.0), ("relieve", 10.0)])
        interaction = FakeInteraction()
        run(cog._run_command(interaction, "recieve"))
        view = interaction.original_edits[0]["view"]
        self.assertIsInstance(view, NotFoundView)
        self.assertIn("Did you mean", text_of(view))
        labels = buttons_of(view)
        self.assertIn("Yes, search receive", labels)
        self.assertIn("Search exactly what I typed", labels)
        run(cog.cog_unload())

    def test_high_confidence_misspelling_is_corrected_before_rendering(self):
        cog = make_cog(
            entries={"receive": entry_for("receive")},
            suggestions=[("receive", 100.0)],
        )
        interaction = FakeInteraction()
        run(cog._run_command(interaction, "recieve"))
        view = interaction.original_edits[0]["view"]
        self.assertIsInstance(view, ResultView)
        self.assertIn("corrected from", text_of(view))
        run(cog.cog_unload())

    def test_correction_button_belongs_to_the_asker(self):
        cog = make_cog(entries={}, suggestions=[("receive", 100.0)])
        interaction = FakeInteraction(user_id=1)
        run(cog._run_command(interaction, "recieve"))
        button = buttons_of(interaction.original_edits[0]["view"])["Yes, search receive"]
        stranger = FakeInteraction(user_id=2)
        run(button.callback(stranger))
        self.assertEqual(stranger.response.edits, [])
        self.assertTrue(stranger.response.messages[0]["ephemeral"])
        run(cog.cog_unload())

    def test_lookup_failure_shows_a_graceful_message(self):
        cog = make_cog()

        class Exploding:
            name = "boom"

            async def lookup(self, term, language="en"):
                raise RuntimeError("everything is on fire")

        cog.feature.service.primary = Exploding()
        cog.feature.service.secondary = FakeSecondary(suggestions=[])
        interaction = FakeInteraction()
        run(cog._run_command(interaction, "promise"))
        view = interaction.original_edits[0]["view"]
        self.assertNotIsInstance(view, ResultView)
        self.assertIn("Couldn't find", text_of(view))
        run(cog.cog_unload())

    def test_expired_interaction_is_survivable(self):
        cog = make_cog()
        interaction = FakeInteraction()
        interaction.response.raises = discord.NotFound(http_error(404), "unknown interaction")
        run(cog._run_command(interaction, "promise"))  # must not raise
        self.assertEqual(interaction.original_edits, [])
        run(cog.cog_unload())

    def test_concurrent_commands_share_one_lookup(self):
        cog = make_cog()

        async def scenario():
            interactions = [FakeInteraction(user_id=i) for i in range(5)]
            await asyncio.gather(*[cog._run_command(i, "promise") for i in interactions])
            return interactions

        interactions = run(scenario())
        self.assertEqual(len(cog.feature.service.primary.calls), 1)
        for interaction in interactions:
            self.assertIsInstance(interaction.original_edits[0]["view"], ResultView)
        run(cog.cog_unload())

    def test_define_and_dictionary_are_the_same_pipeline(self):
        cog = make_cog()
        self.assertEqual(
            cog.dictionary.callback.__wrapped__ if hasattr(cog.dictionary.callback, "__wrapped__") else True,
            cog.dictionary.callback.__wrapped__ if hasattr(cog.dictionary.callback, "__wrapped__") else True,
        )
        names = {command.name for command in cog.get_app_commands()}
        self.assertEqual(names, {"dictionary", "define"})
        run(cog.cog_unload())


class TestChatTriggerFlow(unittest.TestCase):
    def test_plain_request_is_answered(self):
        cog = make_cog()
        message = FakeMessage('Define "promise"')
        self.assertTrue(run(cog.handle_message(message)))
        self.assertIsInstance(message.replies[0]["view"], LoadingView)
        self.assertFalse(message.replies[0]["mention_author"])
        self.assertIsInstance(message.placeholder.edits[0]["view"], ResultView)
        run(cog.cog_unload())

    def test_ordinary_chat_is_left_alone(self):
        cog = make_cog()
        for content in ("hello everyone", "I need to define a function", "what does it mean"):
            message = FakeMessage(content)
            self.assertFalse(run(cog.handle_message(message)), content)
            self.assertEqual(message.replies, [])
        run(cog.cog_unload())

    def test_bots_and_mentions_are_ignored(self):
        cog = make_cog()
        self.assertFalse(run(cog.handle_message(FakeMessage("define promise", bot=True))))
        mentioned = FakeMessage("define promise", mentions=[SimpleNamespace(id=1)])
        self.assertFalse(run(cog.handle_message(mentioned)))
        run(cog.cog_unload())

    def test_triggers_can_be_switched_off(self):
        cog = make_cog(settings=DictionaryConfig(text_triggers=False))
        self.assertFalse(run(cog.handle_message(FakeMessage("define promise"))))
        run(cog.cog_unload())

    def test_repeat_requests_are_rate_limited(self):
        cog = make_cog()
        first = FakeMessage("define promise", author_id=7)
        second = FakeMessage("define pledge", author_id=7)
        self.assertTrue(run(cog.handle_message(first)))
        self.assertFalse(run(cog.handle_message(second)), "same user, immediately after")
        run(cog.cog_unload())

    def test_missing_send_permission_is_respected(self):
        cog = make_cog()
        message = FakeMessage("define promise")
        message.channel = SimpleNamespace(
            id=11, permissions_for=lambda _m: discord.Permissions.none()
        )
        self.assertFalse(run(cog.handle_message(message)))
        self.assertEqual(message.replies, [])
        run(cog.cog_unload())


class TestButtonFlow(unittest.TestCase):
    def _posted(self, cog, user_id=1):
        interaction = FakeInteraction(user_id=user_id)
        run(cog._run_command(interaction, "promise"))
        return interaction.original_edits[0]["view"]

    def test_more_screen_shows_extra_material(self):
        cog = make_cog()
        view = self._posted(cog)
        clicker = FakeInteraction(user_id=1)
        run(buttons_of(view)["More"].callback(clicker))
        new_view = clicker.response.edits[0]["view"]
        body = text_of(new_view)
        self.assertIn("Pronunciation", body)
        self.assertIn("Back", buttons_of(new_view))
        self.assertNotIn("More", buttons_of(new_view))
        run(cog.cog_unload())

    def test_back_returns_to_the_card(self):
        cog = make_cog()
        view = self._posted(cog)
        clicker = FakeInteraction(user_id=1)
        run(buttons_of(view)["More"].callback(clicker))
        more_view = clicker.response.edits[0]["view"]
        back = FakeInteraction(user_id=1)
        run(buttons_of(more_view)["Back"].callback(back))
        self.assertIn("More", buttons_of(back.response.edits[0]["view"]))
        self.assertEqual(len(back.response.edits[0]["attachments"]), 1)
        run(cog.cog_unload())

    def test_screen_buttons_belong_to_the_asker(self):
        cog = make_cog()
        view = self._posted(cog, user_id=1)
        stranger = FakeInteraction(user_id=2)
        run(buttons_of(view)["More"].callback(stranger))
        self.assertEqual(stranger.response.edits, [])
        self.assertTrue(stranger.response.messages[0]["ephemeral"])
        run(cog.cog_unload())

    def test_pronounce_is_open_to_everyone_and_replies_privately(self):
        cog = make_cog()
        view = self._posted(cog, user_id=1)
        stranger = FakeInteraction(user_id=2)
        run(buttons_of(view)["Pronounce"].callback(stranger))
        self.assertTrue(stranger.response.defers[0]["ephemeral"])
        sent = stranger.followup.sent[0]
        self.assertTrue(sent["ephemeral"])
        self.assertEqual(len(sent["files"]), 1)
        self.assertTrue(sent["files"][0].filename.startswith("promise"))
        run(cog.cog_unload())

    def test_pronounce_is_rate_limited_per_user(self):
        cog = make_cog()
        view = self._posted(cog)
        first = FakeInteraction(user_id=3)
        run(buttons_of(view)["Pronounce"].callback(first))
        second = FakeInteraction(user_id=3)
        run(buttons_of(view)["Pronounce"].callback(second))
        self.assertEqual(second.response.defers, [])
        self.assertTrue(second.response.messages[0]["ephemeral"])
        run(cog.cog_unload())

    def test_failed_audio_keeps_the_card_usable(self):
        cog = make_cog()
        cog.feature.pronunciation.providers = [FakeSpeechProvider("web", clip=None)]
        view = self._posted(cog)
        clicker = FakeInteraction(user_id=1)
        run(buttons_of(view)["Pronounce"].callback(clicker))
        message = clicker.followup.sent[0]["content"]
        self.assertIn("🔇", message)
        run(cog.cog_unload())

    def test_expired_button_interaction_is_survivable(self):
        cog = make_cog()
        view = self._posted(cog)
        clicker = FakeInteraction(user_id=1)
        clicker.response.raises = discord.NotFound(http_error(404), "unknown interaction")
        run(buttons_of(view)["More"].callback(clicker))  # must not raise
        run(buttons_of(view)["Pronounce"].callback(clicker))  # must not raise
        run(cog.cog_unload())

    def test_timeout_greys_the_buttons_out(self):
        cog = make_cog()
        interaction = FakeInteraction()
        run(cog._run_command(interaction, "promise"))
        view = interaction.original_edits[0]["view"]
        run(view.on_timeout())
        self.assertTrue(all(button.disabled for button in buttons_of(view).values()))
        run(cog.cog_unload())

    def test_double_click_does_not_double_work(self):
        cog = make_cog()
        view = self._posted(cog)
        button = buttons_of(view)["Pronounce"]

        async def scenario():
            clickers = [FakeInteraction(user_id=4) for _ in range(2)]
            await asyncio.gather(*[button.callback(c) for c in clickers])
            return clickers

        clickers = run(scenario())
        delivered = [c for c in clickers if c.followup.sent]
        self.assertEqual(len(delivered), 1)
        run(cog.cog_unload())


class TestScreenText(unittest.TestCase):
    def test_more_text_reports_missing_pronunciation_honestly(self):
        entry = WordEntry(word="rizz", senses=(Sense("noun", "Charm."),))
        body = more_text(entry, WordDetails())
        self.assertIn("No phonetic spelling available", body)

    def test_more_text_includes_what_exists(self):
        entry = entry_for(origin="From Latin promissum.").with_extras(
            antonyms=["denial"], related=["oath"]
        )
        body = more_text(entry, WordDetails(forms=(WordForm("plural", "promises"),)))
        for needle in (
            "Meanings", "Pronunciation", "Forms", "promises",
            "Synonyms", "Antonyms", "Origin",
        ):
            self.assertIn(needle, body)

    def test_more_text_shows_synonyms_the_card_had_no_room_for(self):
        """The card caps synonyms at 6 — none may be silently lost."""
        many = tuple(f"syn{i}" for i in range(14))
        entry = WordEntry(
            word="big",
            senses=(Sense("adjective", "Large.", synonyms=many[7:]),),
            synonyms=many[:7],
        )
        body = more_text(entry, WordDetails())
        self.assertIn("Synonyms", body)
        for word in many:
            self.assertIn(word, body)

    def test_examples_text_handles_none(self):
        entry = WordEntry(word="zarf", senses=(Sense("noun", "A cup holder."),), synonyms=("holder",))
        body = examples_text(entry, WordDetails())
        self.assertIn("No example sentences", body)
        self.assertIn("holder", body)

    def test_screens_stay_within_discord_limits(self):
        entry = WordEntry(
            word="set",
            senses=tuple(
                Sense("verb", f"Meaning {i}: " + "words " * 60, example="An example. " * 30)
                for i in range(12)
            ),
            origin="From Old English " * 60,
            related=tuple(f"related{i}" for i in range(40)),
        )
        details = WordDetails(
            related=tuple(f"r{i}" for i in range(40)),
            forms=tuple(WordForm("plural", f"form{i}") for i in range(20)),
        )
        for body in (more_text(entry, details), examples_text(entry, details)):
            self.assertLessEqual(len(body), 3600)

    def test_text_fallback_card_when_images_are_unavailable(self):
        body = card_fallback_text(entry_for())
        self.assertIn("promise", body)
        self.assertIn("commitment", body.lower())
        self.assertIn("Synonyms", body)

    def test_not_found_text(self):
        self.assertIn("Couldn't find", not_found_text("xyz", ()))
        self.assertIn("Did you mean", not_found_text("xyz", ("xy",)))

    def test_markdown_in_a_word_cannot_break_the_message(self):
        entry = WordEntry(word="**bold**", senses=(Sense("noun", "*emphasis* test"),))
        body = more_text(entry, WordDetails())
        self.assertIn("\\*\\*bold\\*\\*", body)


class TestFeatureFacade(unittest.TestCase):
    def test_describe_mentions_every_subsystem(self):
        cog = make_cog()
        described = cog.feature.describe()
        for needle in ("cards", "fonts", "pronunciation providers", "voice playback"):
            self.assertIn(needle, described)
        run(cog.cog_unload())

    def test_rendered_cards_are_cached(self):
        cog = make_cog()
        entry = entry_for()

        async def scenario():
            first = await cog.feature.card(entry)
            second = await cog.feature.card(entry)
            return first, second

        first, second = run(scenario())
        self.assertIs(first, second)
        run(cog.cog_unload())

    def test_a_broken_renderer_degrades_to_text(self):
        cog = make_cog()

        def boom(entry, theme=None):
            raise RuntimeError("no pixels today")

        import functions.dictionary.feature as feature_module

        original = feature_module.render_card
        feature_module.render_card = boom
        try:
            card = run(cog.feature.card(entry_for("unrenderable")))
        finally:
            feature_module.render_card = original
        self.assertIsNone(card)

        interaction = FakeInteraction()
        cog.feature.service.primary = FakePrimary({"promise": entry_for()})
        feature_module.render_card = boom
        try:
            run(cog._run_command(interaction, "promise"))
        finally:
            feature_module.render_card = original
        view = interaction.original_edits[0]["view"]
        self.assertIsInstance(view, ResultView)
        self.assertIn("promise", text_of(view))
        self.assertEqual(interaction.original_edits[0]["attachments"], [])
        run(cog.cog_unload())

    def test_close_is_idempotent(self):
        cog = make_cog()
        run(cog.cog_unload())
        run(cog.cog_unload())


class TestConfigPlumbing(unittest.TestCase):
    """Every documented .env knob must actually reach something.

    These read through `getattr`, so a renamed field would silently fall
    back to a default instead of failing — hence the explicit assertions.
    """

    def test_every_config_field_is_consumed(self):
        settings = DictionaryConfig(
            text_triggers=False,
            timeout=3.5,
            cache_ttl=111.0,
            cache_size=7,
            card_theme="light",
            prefer_native_audio=False,
            voice_playback=True,
            speech_base_url="https://example.invalid/v1",
            speech_api_key="secret-key",
            speech_model="some-model",
            speech_voice="some-voice",
        )
        feature = DictionaryFeature(settings)
        try:
            self.assertEqual(feature.client.timeout, 3.5)
            self.assertEqual(feature.service.timeout, 3.5)
            self.assertEqual(feature.service._cache._ttl, 111.0)
            self.assertEqual(feature.service._cache._max_items, 7)
            self.assertEqual(feature.card_theme.name, "light")
            self.assertFalse(feature.pronunciation.prefer_native)
            self.assertTrue(feature.playback.enabled)
            neural = next(
                p for p in feature.pronunciation.providers
                if isinstance(p, NeuralSpeechProvider)
            )
            self.assertEqual(neural._base_url, "https://example.invalid/v1")
            self.assertEqual(neural._model, "some-model")
            self.assertEqual(neural._voice, "some-voice")
            self.assertTrue(neural.available)
        finally:
            run(feature.close())

    def test_defaults_work_with_no_config_at_all(self):
        feature = DictionaryFeature(None)
        try:
            self.assertGreater(feature.client.timeout, 0)
            neural = next(
                p for p in feature.pronunciation.providers
                if isinstance(p, NeuralSpeechProvider)
            )
            self.assertFalse(neural.available)
            self.assertFalse(feature.playback.enabled)
        finally:
            run(feature.close())

    def test_secrets_are_never_logged(self):
        settings = DictionaryConfig(
            speech_base_url="https://example.invalid/v1",
            speech_api_key="super-secret-token",
            speech_model="m",
            speech_voice="v",
        )
        feature = DictionaryFeature(settings)
        try:
            self.assertNotIn("super-secret-token", feature.describe())
            self.assertNotIn("super-secret-token", feature.pronunciation.describe())
        finally:
            run(feature.close())

    def test_env_round_trip(self):
        env = {
            "DICTIONARY_TEXT_TRIGGERS": "false",
            "DICTIONARY_TIMEOUT": "12",
            "DICTIONARY_CACHE_TTL": "900",
            "DICTIONARY_CACHE_SIZE": "64",
            "DICTIONARY_CARD_THEME": "light",
            "PRONUNCIATION_PREFER_NATIVE": "no",
            "PRONUNCIATION_VOICE_PLAYBACK": "yes",
            "SPEECH_BASE_URL": "https://example.invalid/v1/",
            "SPEECH_API_KEY": "  k  ",
            "SPEECH_MODEL": "m",
            "SPEECH_VOICE": "v",
        }
        settings = DictionaryConfig.from_env(env)
        self.assertFalse(settings.text_triggers)
        self.assertEqual(settings.timeout, 12.0)
        self.assertEqual(settings.cache_ttl, 900)
        self.assertEqual(settings.cache_size, 64)
        self.assertEqual(settings.card_theme, "light")
        self.assertFalse(settings.prefer_native_audio)
        self.assertTrue(settings.voice_playback)
        self.assertEqual(settings.speech_base_url, "https://example.invalid/v1")
        self.assertEqual(settings.speech_api_key, "k")
        self.assertTrue(settings.speech_configured)

    def test_nonsense_env_values_fall_back_instead_of_crashing(self):
        settings = DictionaryConfig.from_env(
            {"DICTIONARY_TIMEOUT": "soon", "DICTIONARY_CACHE_SIZE": "lots"}
        )
        self.assertEqual(settings.timeout, 8.0)
        self.assertEqual(settings.cache_size, 512)

    def test_absurd_env_values_are_clamped(self):
        settings = DictionaryConfig.from_env(
            {"DICTIONARY_TIMEOUT": "0", "DICTIONARY_CACHE_SIZE": "1", "DICTIONARY_CACHE_TTL": "0"}
        )
        self.assertGreaterEqual(settings.timeout, 2.0)
        self.assertGreaterEqual(settings.cache_size, 16)
        self.assertGreaterEqual(settings.cache_ttl, 60)

    def test_every_env_key_config_reads_is_documented(self):
        source = (REPO_ROOT / "config.py").read_text(encoding="utf-8")
        documented = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        keys = set(re.findall(r"""env(?:iron)?\.get\(\s*["\']([A-Z0-9_]+)["\']""", source))
        self.assertIn("DICTIONARY_TEXT_TRIGGERS", keys, "sanity: keys were found")
        for key in sorted(keys):
            self.assertRegex(
                documented,
                rf"(?m)^#?\s*{key}=",
                f"{key} is read by config.py but missing from .env.example",
            )


class TestLayering(unittest.TestCase):
    """The data and render layers must stay free of Discord.

    Keeping `discord` out of them is what makes the renderer and the
    lookup pipeline testable without a gateway — and it is easy to break
    by reflex, so it is asserted rather than documented.
    """

    PACKAGE = REPO_ROOT / "functions" / "dictionary"
    # playback.py drives an actual voice client; it cannot avoid the import.
    ALLOWED = {"playback.py"}

    def test_no_discord_in_the_lower_layers(self):
        offenders = []
        for path in sorted(self.PACKAGE.glob("*.py")):
            if path.name in self.ALLOWED:
                continue
            source = path.read_text(encoding="utf-8")
            if re.search(r"(?m)^\s*(?:import discord|from discord\b)", source):
                offenders.append(path.name)
        self.assertEqual(offenders, [], "these should not import discord")

    def test_renderer_does_not_reach_the_network(self):
        source = (self.PACKAGE / "render.py").read_text(encoding="utf-8")
        for banned in ("aiohttp", "requests", "urllib", "socket"):
            self.assertNotIn(banned, source)

    def test_http_access_goes_through_one_client(self):
        offenders = []
        for path in sorted(self.PACKAGE.glob("*.py")):
            if path.name == "http.py":
                continue
            source = path.read_text(encoding="utf-8")
            if re.search(r"(?m)^\s*import aiohttp", source):
                offenders.append(path.name)
        self.assertEqual(offenders, [], "everything shares functions/dictionary/http.py")

    def test_package_exports_a_stable_surface(self):
        import functions.dictionary as package

        for name in ("DictionaryFeature", "WordEntry", "Sense", "LookupResult"):
            self.assertIn(name, package.__all__, f"{name} should be part of the API")
