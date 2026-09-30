"""Plain data models for the recipe feature.

Kept dependency-free (no discord, no aiohttp) so the whole matching layer
can be unit-tested offline and reused from anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class RecipeIngredient:
    """One ingredient line of a recipe."""

    name: str  # canonical ingredient name (lower-case)
    quantity: str = ""  # human-readable amount, e.g. "2 cups cooked"
    optional: bool = False

    @classmethod
    def from_dict(cls, raw: dict) -> RecipeIngredient:
        return cls(
            name=str(raw.get("name", "")).strip().lower(),
            quantity=str(raw.get("quantity", "")).strip(),
            optional=bool(raw.get("optional", False)),
        )


@dataclass(frozen=True, slots=True)
class Recipe:
    """A single recipe from the bundled dataset (or any future source)."""

    id: str
    name: str
    emoji: str = "🍽"
    cuisine: str = ""
    diet: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    time_minutes: int = 0
    servings: int = 0
    difficulty: str = ""
    ingredients: tuple[RecipeIngredient, ...] = ()
    instructions: tuple[str, ...] = ()
    image: str | None = None
    source: str = "Paradise recipe book"

    @classmethod
    def from_dict(cls, raw: dict) -> Recipe:
        return cls(
            id=str(raw.get("id", "")).strip(),
            name=str(raw.get("name", "")).strip(),
            emoji=str(raw.get("emoji", "🍽")).strip() or "🍽",
            cuisine=str(raw.get("cuisine", "")).strip().lower(),
            diet=tuple(str(d).strip().lower() for d in raw.get("diet", []) if str(d).strip()),
            categories=tuple(
                str(c).strip().lower() for c in raw.get("categories", []) if str(c).strip()
            ),
            time_minutes=int(raw.get("time_minutes", 0) or 0),
            servings=int(raw.get("servings", 0) or 0),
            difficulty=str(raw.get("difficulty", "")).strip(),
            ingredients=tuple(
                RecipeIngredient.from_dict(i)
                for i in raw.get("ingredients", [])
                if str(i.get("name", "")).strip()
            ),
            instructions=tuple(
                str(step).strip() for step in raw.get("instructions", []) if str(step).strip()
            ),
            image=(str(raw["image"]).strip() or None) if raw.get("image") else None,
            source=str(raw.get("source", "Paradise recipe book")).strip()
            or "Paradise recipe book",
        )


@dataclass(slots=True)
class MatchResult:
    """The outcome of matching one recipe against a user's ingredients."""

    recipe: Recipe
    have: list[str] = field(default_factory=list)  # required, non-pantry, user has
    missing: list[str] = field(default_factory=list)  # required, non-pantry, absent
    pantry_used: list[str] = field(default_factory=list)  # required staples assumed on hand
    optional_have: list[str] = field(default_factory=list)
    optional_missing: list[str] = field(default_factory=list)
    score: float = 0.0

    @property
    def need_count(self) -> int:
        """Ingredients the cook would actually have to source (excludes staples)."""
        return len(self.have) + len(self.missing)

    @property
    def have_count(self) -> int:
        return len(self.have)

    @property
    def coverage(self) -> float:
        total = self.need_count
        return (self.have_count / total) if total else 1.0

    @property
    def match_label(self) -> str:
        """A friendly "8/9 ingredients" style label."""
        total = self.need_count
        if total == 0:
            return "pantry staples only"
        return f"{self.have_count}/{total} ingredients"
