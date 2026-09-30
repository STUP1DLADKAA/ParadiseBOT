"""Ingredient normalization for the recipe feature.

Turns messy, real-world user input into a set of canonical ingredient
names. Understands quantities ("2 eggs", "one tomato"), units ("4 slices
bread"), plurals, common spelling variations and Hindi/Hinglish names
("aloo", "pyaaz", "tamatar", "hari mirchi", ...).

All the vocabulary lives in ``ingredients.json`` so it can be expanded
without touching this code.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

_DATA_PATH = Path(__file__).with_name("ingredients.json")


class IngredientData:
    """Loaded ingredient vocabulary (aliases, pantry, substitutions)."""

    __slots__ = (
        "_descriptors",
        "_quantity_words",
        "_unit_re",
        "_units",
        "aliases",
        "pantry",
        "substitutions",
    )

    def __init__(self, raw: dict) -> None:
        # Normalise every key/value to lower-case for robust lookups.
        self.aliases: dict[str, str] = {
            str(k).strip().lower(): str(v).strip().lower()
            for k, v in raw.get("aliases", {}).items()
        }
        self.pantry: frozenset[str] = frozenset(
            str(p).strip().lower() for p in raw.get("pantry", []) if str(p).strip()
        )
        self.substitutions: dict[str, str] = {
            str(k).strip().lower(): str(v).strip()
            for k, v in raw.get("substitutions", {}).items()
        }
        self._units: frozenset[str] = frozenset(
            str(u).strip().lower() for u in raw.get("units", []) if str(u).strip()
        )
        self._quantity_words: dict[str, int] = {
            str(k).strip().lower(): int(v)
            for k, v in raw.get("quantity_words", {}).items()
        }
        self._descriptors: frozenset[str] = frozenset(
            str(d).strip().lower() for d in raw.get("descriptors", []) if str(d).strip()
        )
        # A word is "noise" if it's a unit, a quantity word or a descriptor.
        noise = self._units | set(self._quantity_words) | self._descriptors
        # Longest first so multi-word units would match, though we tokenize by word.
        self._unit_re = noise

    # -- canonicalization --------------------------------------------------

    def canonical(self, phrase: str) -> str | None:
        """Best-effort canonical name for a single ingredient phrase."""
        cleaned = self._strip_noise(phrase)
        if not cleaned:
            return None

        # 1) Direct alias hit on the whole phrase.
        if cleaned in self.aliases:
            return self.aliases[cleaned]

        # 2) Singularized whole phrase.
        singular = _singularize(cleaned)
        if singular in self.aliases:
            return self.aliases[singular]

        # 3) Progressive suffix trimming: "green chilli chopped" -> "green chilli".
        words = cleaned.split()
        for length in range(len(words), 0, -1):
            candidate = " ".join(words[:length])
            if candidate in self.aliases:
                return self.aliases[candidate]
            cand_singular = _singularize(candidate)
            if cand_singular in self.aliases:
                return self.aliases[cand_singular]

        # 4) Nothing matched an alias — fall back to the cleaned singular form so
        #    unknown-but-real ingredients still participate in matching.
        return singular or cleaned

    def _strip_noise(self, phrase: str) -> str:
        text = phrase.lower().strip()
        # Drop anything in brackets, e.g. "onion (chopped)".
        text = re.sub(r"\([^)]*\)", " ", text)
        # Replace fractions/decimals/quantities and separators with spaces.
        text = re.sub(r"[0-9]+([./][0-9]+)?", " ", text)
        text = re.sub(r"[^a-z\s-]", " ", text)
        tokens = [t for t in text.split() if t]
        kept = [t for t in tokens if t not in self._unit_re]
        # If stripping removed everything (e.g. input was just "2 cups"),
        # fall back to the non-quantity-word tokens.
        if not kept:
            kept = [t for t in tokens if t not in self._quantity_words]
        return " ".join(kept).strip()

    def is_pantry(self, canonical_name: str) -> bool:
        return canonical_name in self.pantry

    def substitution_for(self, canonical_name: str) -> str | None:
        return self.substitutions.get(canonical_name)


def _singularize(text: str) -> str:
    """A deliberately small English singularizer (last word only)."""
    if not text:
        return text
    words = text.split()
    last = words[-1]
    if len(last) > 3:
        if last.endswith("ies"):
            last = last[:-3] + "y"
        elif last.endswith(("ses", "xes", "zes", "oes")):
            last = last[:-2]
        elif last.endswith("s") and not last.endswith("ss"):
            last = last[:-1]
    words[-1] = last
    return " ".join(words)


@lru_cache(maxsize=1)
def get_ingredient_data() -> IngredientData:
    """Load (and cache) the bundled ingredient vocabulary."""
    with _DATA_PATH.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    return IngredientData(raw)


def _split_ingredients(raw: str) -> list[str]:
    """Split a free-form ingredient string into individual items."""
    # Users separate with commas, newlines, semicolons, slashes, bullets,
    # or the words "and"/"aur" (Hindi for "and").
    parts = re.split(r"[,\n;/•·]| and | aur | & |\+", raw, flags=re.IGNORECASE)
    return [p.strip() for p in parts if p.strip()]


def normalize_input(raw: str, data: IngredientData | None = None) -> list[str]:
    """Parse user input into an ordered, de-duplicated list of canonical names."""
    data = data or get_ingredient_data()
    seen: list[str] = []
    for chunk in _split_ingredients(raw or ""):
        name = data.canonical(chunk)
        if name and name not in seen:
            seen.append(name)
    return seen


def prettify(name: str) -> str:
    """Human-friendly display form of a canonical ingredient name."""
    return name[:1].upper() + name[1:] if name else name
