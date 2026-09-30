"""Ingredient matching, ranking and filtering.

Pure logic, no AI and no network. Given a user's ingredients and a set of
recipes it decides which recipes are realistically cookable and ranks them.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import MatchResult, Recipe
from .normalize import IngredientData, get_ingredient_data
from .source import RecipeSource, get_default_source

# A recipe needs at least this fraction of its (non-staple) ingredients on
# hand, and at least one real match, to count as a "strong" suggestion.
_MIN_COVERAGE = 0.5
_MIN_MATCHES = 1


@dataclass(frozen=True, slots=True)
class RecipeFilters:
    diet: str | None = None
    cuisine: str | None = None
    max_time: int | None = None

    def matches(self, recipe: Recipe) -> bool:
        if self.diet and self.diet.lower() not in recipe.diet:
            return False
        if self.cuisine and self.cuisine.lower() != recipe.cuisine:
            return False
        if self.max_time and recipe.time_minutes and recipe.time_minutes > self.max_time:
            return False
        return True


def match_recipe(
    recipe: Recipe, user_set: set[str], data: IngredientData
) -> MatchResult:
    """Compare one recipe against the user's canonical ingredient set."""
    result = MatchResult(recipe=recipe)
    for ingredient in recipe.ingredients:
        name = ingredient.name
        has_it = name in user_set
        if data.is_pantry(name):
            # Staples are assumed available; never counted as "missing".
            if not ingredient.optional:
                result.pantry_used.append(name)
            continue
        if ingredient.optional:
            (result.optional_have if has_it else result.optional_missing).append(name)
        else:
            (result.have if has_it else result.missing).append(name)
    result.score = _score(result, user_set)
    return result


def _score(result: MatchResult, user_set: set[str]) -> float:
    """Higher is better. Rewards using what the user has, punishes shopping."""
    have = result.have_count
    missing = len(result.missing)
    coverage = result.coverage

    score = 0.0
    score += have * 3.0  # reward each real ingredient the user already has
    score += coverage * 6.0  # reward completeness of the recipe
    score += len(result.optional_have) * 0.75  # optional extras are a small bonus
    score -= missing * 2.2  # each thing to buy hurts
    score -= len(result.optional_missing) * 0.2  # optionals barely hurt

    # A recipe that uses a larger share of what the user actually typed feels
    # more relevant than one that only touches one of their ingredients.
    if user_set:
        used_ratio = have / len(user_set)
        score += used_ratio * 2.0

    # Gently prefer quicker, simpler dishes as a tie-breaker.
    if result.recipe.time_minutes:
        score -= result.recipe.time_minutes * 0.01
    return round(score, 4)


def rank_recipes(
    user_ingredients: list[str],
    *,
    filters: RecipeFilters | None = None,
    source: RecipeSource | None = None,
    data: IngredientData | None = None,
    limit: int | None = None,
) -> list[MatchResult]:
    """Return strong matches, best first. Empty list means nothing good matched."""
    data = data or get_ingredient_data()
    source = source or get_default_source()
    user_set = set(user_ingredients)

    results: list[MatchResult] = []
    for recipe in source.all_recipes():
        if filters and not filters.matches(recipe):
            continue
        result = match_recipe(recipe, user_set, data)
        if result.have_count < _MIN_MATCHES:
            continue
        if result.coverage < _MIN_COVERAGE:
            continue
        results.append(result)

    results.sort(
        key=lambda r: (
            -r.score,
            len(r.missing),
            r.recipe.time_minutes or 9999,
            r.recipe.name.lower(),
        )
    )
    return results[:limit] if limit else results


def near_misses(
    user_ingredients: list[str],
    *,
    filters: RecipeFilters | None = None,
    source: RecipeSource | None = None,
    data: IngredientData | None = None,
    limit: int = 3,
) -> list[MatchResult]:
    """Best-effort suggestions when nothing meets the strong-match bar.

    Used by the no-result experience: recipes the user is closest to being
    able to make, ranked by how few extra ingredients they'd need.
    """
    data = data or get_ingredient_data()
    source = source or get_default_source()
    user_set = set(user_ingredients)

    results: list[MatchResult] = []
    for recipe in source.all_recipes():
        if filters and not filters.matches(recipe):
            continue
        result = match_recipe(recipe, user_set, data)
        if result.have_count < 1:
            continue
        results.append(result)

    results.sort(key=lambda r: (len(r.missing), -r.have_count, -r.score))
    return results[:limit]


def suggest_substitution(name: str, data: IngredientData | None = None) -> str | None:
    """A conservative, sensible swap for a missing ingredient, if we have one."""
    data = data or get_ingredient_data()
    return data.substitution_for(name)
