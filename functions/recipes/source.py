"""Recipe sources.

The default, always-available source is the bundled JSON dataset — no API
key, no network, no scraping. The ``RecipeSource`` protocol keeps the door
open for adding an external provider (e.g. a free recipe API) later without
touching the matching or UI layers.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from .models import Recipe

log = logging.getLogger(__name__)

_RECIPES_PATH = Path(__file__).with_name("recipes.json")


class RecipeSource(Protocol):
    """Anything that can supply recipes to the matching engine."""

    def all_recipes(self) -> list[Recipe]:  # pragma: no cover - interface
        ...


class BundledRecipeSource:
    """Loads recipes from the JSON dataset shipped with the bot."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _RECIPES_PATH
        self._recipes: list[Recipe] | None = None

    def all_recipes(self) -> list[Recipe]:
        if self._recipes is None:
            self._recipes = self._load()
        return self._recipes

    def _load(self) -> list[Recipe]:
        try:
            with self._path.open(encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            log.exception("Could not load bundled recipes from %s", self._path)
            return []
        recipes: list[Recipe] = []
        for entry in raw:
            try:
                recipe = Recipe.from_dict(entry)
            except (TypeError, ValueError, AttributeError):
                log.warning("Skipping malformed recipe entry", exc_info=True)
                continue
            if recipe.id and recipe.name and recipe.ingredients:
                recipes.append(recipe)
        return recipes


@lru_cache(maxsize=1)
def get_default_source() -> BundledRecipeSource:
    """The always-available bundled recipe source (cached)."""
    return BundledRecipeSource()
