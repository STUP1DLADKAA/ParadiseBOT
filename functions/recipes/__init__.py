"""Recipe assistant core: ingredient normalization, matching and ranking.

Deliberately free of Discord and AI dependencies so it can be unit-tested
offline and so the basic /recipe command works with zero configuration.
"""

from __future__ import annotations

from .engine import (
    RecipeFilters,
    match_recipe,
    near_misses,
    rank_recipes,
    suggest_substitution,
)
from .models import MatchResult, Recipe, RecipeIngredient
from .normalize import (
    IngredientData,
    get_ingredient_data,
    normalize_input,
    prettify,
)
from .source import BundledRecipeSource, RecipeSource, get_default_source

__all__ = [
    "BundledRecipeSource",
    "IngredientData",
    "MatchResult",
    "Recipe",
    "RecipeFilters",
    "RecipeIngredient",
    "RecipeSource",
    "get_default_source",
    "get_ingredient_data",
    "match_recipe",
    "near_misses",
    "normalize_input",
    "prettify",
    "rank_recipes",
    "suggest_substitution",
]
