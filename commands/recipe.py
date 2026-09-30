"""The /recipe command: suggest recipes from ingredients you have.

Core matching is fully local (a bundled recipe book) — no AI and no API key
required. The result is a Discord Components V2 interface that feels like a
tiny cooking app: pick a recipe, read it, check what you're missing, or ask
for another idea, all without re-running the search.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from functions.helpers import Colour
from functions.recipes import (
    MatchResult,
    RecipeFilters,
    near_misses,
    normalize_input,
    prettify,
    rank_recipes,
    suggest_substitution,
)

log = logging.getLogger(__name__)

# How many suggestions to surface / cycle through.
STORE_LIMIT = 6
SHOW_LIMIT = 5
NEAR_MISS_LIMIT = 3
VIEW_TIMEOUT = 600  # seconds a recipe board stays interactive

DIET_CHOICES = [
    app_commands.Choice(name="Vegetarian", value="vegetarian"),
    app_commands.Choice(name="Vegan", value="vegan"),
    app_commands.Choice(name="Non-veg", value="non-veg"),
]
CUISINE_CHOICES = [
    app_commands.Choice(name="Indian", value="indian"),
    app_commands.Choice(name="Chinese", value="chinese"),
    app_commands.Choice(name="Italian", value="italian"),
    app_commands.Choice(name="Continental", value="continental"),
    app_commands.Choice(name="Asian", value="asian"),
]


# ---------------------------------------------------------------------------
# Formatting helpers (pure text — easy to eyeball on mobile)
# ---------------------------------------------------------------------------


def _dot_join(names: list[str]) -> str:
    return " • ".join(prettify(n) for n in names)


def _meta_line(result: MatchResult) -> str:
    recipe = result.recipe
    bits = []
    if recipe.time_minutes:
        bits.append(f"⏱ {recipe.time_minutes} min")
    if recipe.servings:
        bits.append(f"🍽 {recipe.servings} serving{'s' if recipe.servings != 1 else ''}")
    if recipe.difficulty:
        bits.append(f"📊 {recipe.difficulty}")
    return "  ·  ".join(bits)


def _tag_line(result: MatchResult) -> str:
    labels = {
        "high-protein": "High protein",
        "vegetable-rich": "Vegetable-rich",
        "lower-oil": "Lower-oil",
        "quick": "Quick",
        "balanced": "Balanced",
    }
    tags = [labels[c] for c in result.recipe.categories if c in labels]
    return "  ".join(f"`{t}`" for t in tags)


def _suggestions_text(session: RecipeSession) -> str:
    lines = [
        "## 🍳 Recipe Suggestions",
        f"Based on: **{_dot_join(session.ingredients_norm)}**",
        "",
    ]
    for index, result in enumerate(session.display_matches):
        recipe = result.recipe
        marker = "➡️ " if index == session.focus else ""
        lines.append(f"{marker}**{index + 1}. {recipe.emoji} {recipe.name}**")
        detail = f"Match: {result.match_label}"
        meta = _meta_line(result)
        if meta:
            detail += f"  ·  {meta}"
        lines.append(f"-# {detail}")
        lines.append("")
    lines.append("-# Pick one below, or hit 🔄 for another idea.")
    return "\n".join(lines).strip()


def _have_missing_block(result: MatchResult) -> str:
    lines: list[str] = []
    have = result.have + result.optional_have
    if have:
        lines.append("**You have**")
        lines.extend(f"✓ {prettify(n)}" for n in have)
    if result.pantry_used:
        lines.append(f"-# (assuming pantry staples: {_dot_join(result.pantry_used)})")
    lines.append("")
    if result.missing:
        lines.append("**Missing**")
        lines.extend(f"• {prettify(n)}" for n in result.missing)
    else:
        lines.append("✓ You have everything required.")
    if result.optional_missing:
        lines.append("")
        lines.append("**Optional extras**")
        lines.extend(f"◦ {prettify(n)}" for n in result.optional_missing)
    return "\n".join(lines).strip()


def _ingredients_block(result: MatchResult) -> str:
    lines = ["**Ingredients**"]
    for ing in result.recipe.ingredients:
        name = prettify(ing.name)
        qty = f" — {ing.quantity}" if ing.quantity else ""
        opt = "  _(optional)_" if ing.optional else ""
        lines.append(f"• {name}{qty}{opt}")
    return "\n".join(lines)


def _instructions_block(result: MatchResult) -> str:
    lines = ["**Instructions**"]
    for step_no, step in enumerate(result.recipe.instructions, start=1):
        lines.append(f"**{step_no}.** {step}")
    return "\n".join(lines)


def _missing_text(result: MatchResult) -> str:
    recipe = result.recipe
    lines = ["## 🧺 Missing Ingredients", f"**{recipe.emoji} {recipe.name}**", ""]
    real_missing = result.missing
    if not real_missing and not result.optional_missing:
        lines.append("✓ You already have everything this recipe needs. Get cooking.")
        return "\n".join(lines)

    if not real_missing:
        lines.append("You have everything essential — the rest is optional.")
    elif len(real_missing) == 1:
        lines.append("You already have almost all of this recipe.")
    else:
        lines.append("A few things to grab first.")
    lines.append("")

    if real_missing:
        lines.append("**You still need**")
        lines.extend(f"• {prettify(n)}" for n in real_missing)
    if result.optional_missing:
        lines.append("")
        lines.append("**Optional (nice to have)**")
        lines.extend(f"◦ {prettify(n)}" for n in result.optional_missing)

    subs = []
    for name in real_missing + result.optional_missing:
        swap = suggest_substitution(name)
        if swap:
            subs.append(f"• {prettify(name)} → {swap}")
    if subs:
        lines.append("")
        lines.append("**Possible substitutions**")
        lines.extend(subs)
    return "\n".join(lines).strip()


def _clip(text: str, limit: int = 3800) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# Session state (lives in memory on the view; no re-search needed)
# ---------------------------------------------------------------------------


class RecipeSession:
    def __init__(
        self,
        invoker_id: int,
        ingredients_norm: list[str],
        matches: list[MatchResult],
    ) -> None:
        self.invoker_id = invoker_id
        self.ingredients_norm = ingredients_norm
        self.matches = matches
        self.focus = 0

    @property
    def display_matches(self) -> list[MatchResult]:
        return self.matches[:SHOW_LIMIT]

    @property
    def current(self) -> MatchResult:
        return self.matches[self.focus % len(self.matches)]

    def advance(self) -> None:
        self.focus = (self.focus + 1) % len(self.matches)

    def set_focus(self, index: int) -> None:
        if 0 <= index < len(self.matches):
            self.focus = index


# ---------------------------------------------------------------------------
# Components V2 building blocks
# ---------------------------------------------------------------------------


async def _guard(interaction: discord.Interaction, session: RecipeSession) -> bool:
    """Only the person who ran /recipe may drive the buttons."""
    if interaction.user.id == session.invoker_id:
        return True
    await interaction.response.send_message(
        "this recipe board belongs to someone else — run `/recipe` to start your own.",
        ephemeral=True,
    )
    return False


class RecipePicker(discord.ui.ActionRow["RecipeBoard"]):
    def __init__(self, session: RecipeSession) -> None:
        super().__init__()
        self.session = session
        options = []
        for index, result in enumerate(session.display_matches):
            recipe = result.recipe
            options.append(
                discord.SelectOption(
                    label=f"{index + 1}. {recipe.name}"[:100],
                    value=str(index),
                    description=f"{result.match_label} · {recipe.time_minutes} min"[:100],
                    emoji=recipe.emoji if len(recipe.emoji) <= 2 else None,
                    default=(index == session.focus),
                )
            )
        self.pick.options = options

    @discord.ui.select(placeholder="Choose a recipe to open…", min_values=1, max_values=1)
    async def pick(
        self, interaction: discord.Interaction, select: discord.ui.Select
    ) -> None:
        if not await _guard(interaction, self.session):
            return
        self.session.set_focus(int(select.values[0]))
        await interaction.response.edit_message(
            view=RecipeBoard(self.session, screen="detail")
        )


class SuggestionButtons(discord.ui.ActionRow["RecipeBoard"]):
    def __init__(self, session: RecipeSession) -> None:
        super().__init__()
        self.session = session

    @discord.ui.button(label="View recipe", emoji="📖", style=discord.ButtonStyle.primary)
    async def view_recipe(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await _guard(interaction, self.session):
            return
        await interaction.response.edit_message(
            view=RecipeBoard(self.session, screen="detail")
        )

    @discord.ui.button(label="Another", emoji="🔄", style=discord.ButtonStyle.secondary)
    async def another(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await _guard(interaction, self.session):
            return
        self.session.advance()
        await interaction.response.edit_message(
            view=RecipeBoard(self.session, screen="detail")
        )

    @discord.ui.button(label="Missing", emoji="🧺", style=discord.ButtonStyle.secondary)
    async def missing(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await _guard(interaction, self.session):
            return
        await interaction.response.edit_message(
            view=RecipeBoard(self.session, screen="missing")
        )


class DetailButtons(discord.ui.ActionRow["RecipeBoard"]):
    def __init__(self, session: RecipeSession) -> None:
        super().__init__()
        self.session = session

    @discord.ui.button(label="Back", emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await _guard(interaction, self.session):
            return
        await interaction.response.edit_message(
            view=RecipeBoard(self.session, screen="list")
        )

    @discord.ui.button(label="Missing", emoji="🧺", style=discord.ButtonStyle.secondary)
    async def missing(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await _guard(interaction, self.session):
            return
        await interaction.response.edit_message(
            view=RecipeBoard(self.session, screen="missing")
        )

    @discord.ui.button(label="Another", emoji="🔄", style=discord.ButtonStyle.secondary)
    async def another(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await _guard(interaction, self.session):
            return
        self.session.advance()
        await interaction.response.edit_message(
            view=RecipeBoard(self.session, screen="detail")
        )


class MissingButtons(discord.ui.ActionRow["RecipeBoard"]):
    def __init__(self, session: RecipeSession) -> None:
        super().__init__()
        self.session = session

    @discord.ui.button(label="Back", emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await _guard(interaction, self.session):
            return
        await interaction.response.edit_message(
            view=RecipeBoard(self.session, screen="list")
        )

    @discord.ui.button(label="View recipe", emoji="📖", style=discord.ButtonStyle.primary)
    async def view_recipe(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await _guard(interaction, self.session):
            return
        await interaction.response.edit_message(
            view=RecipeBoard(self.session, screen="detail")
        )


class RecipeBoard(discord.ui.LayoutView):
    """The whole recipe experience, rebuilt per screen from session state."""

    def __init__(self, session: RecipeSession, *, screen: str = "list") -> None:
        super().__init__(timeout=VIEW_TIMEOUT)
        self.session = session
        container = discord.ui.Container(accent_colour=Colour.ACCENT)

        if screen == "detail":
            self._build_detail(container)
        elif screen == "missing":
            self._build_missing(container)
        else:
            self._build_list(container)

        self.add_item(container)

    # -- screens ----------------------------------------------------------

    def _build_list(self, container: discord.ui.Container) -> None:
        container.add_item(discord.ui.TextDisplay(_clip(_suggestions_text(self.session))))
        container.add_item(discord.ui.Separator())
        container.add_item(RecipePicker(self.session))
        container.add_item(SuggestionButtons(self.session))

    def _build_detail(self, container: discord.ui.Container) -> None:
        result = self.session.current
        recipe = result.recipe
        header = (
            f"# {recipe.emoji} {recipe.name}\n"
            f"-# {_meta_line(result)}"
        )
        tags = _tag_line(result)
        if tags:
            header += f"\n{tags}"

        if recipe.image:
            section = discord.ui.Section(
                discord.ui.TextDisplay(header),
                accessory=discord.ui.Thumbnail(recipe.image),
            )
            container.add_item(section)
        else:
            container.add_item(discord.ui.TextDisplay(header))

        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(_have_missing_block(result)))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(_clip(_ingredients_block(result), 1400)))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(_clip(_instructions_block(result), 2200)))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(f"-# {recipe.source}"))
        container.add_item(DetailButtons(self.session))

    def _build_missing(self, container: discord.ui.Container) -> None:
        result = self.session.current
        container.add_item(discord.ui.TextDisplay(_clip(_missing_text(result))))
        container.add_item(discord.ui.Separator())
        container.add_item(MissingButtons(self.session))


class NoMatchView(discord.ui.LayoutView):
    """Friendly no-result screen with concrete next steps."""

    def __init__(self, ingredients: list[str], suggestions: list[MatchResult]) -> None:
        super().__init__(timeout=VIEW_TIMEOUT)
        container = discord.ui.Container(accent_colour=Colour.ACCENT)
        lines = [
            "## 🍳 I couldn't find an exact match.",
            f"You gave me: **{_dot_join(ingredients) or '—'}**",
            "",
            "Try:",
            "• adding one more ingredient",
            "• removing a very specific one",
            "• using a broader name (e.g. `chilli` instead of a variety)",
        ]
        if suggestions:
            lines.append("")
            lines.append("**Closest ideas from what you have:**")
            for result in suggestions:
                need = ", ".join(prettify(n) for n in result.missing[:4]) or "just staples"
                lines.append(
                    f"• {result.recipe.emoji} **{result.recipe.name}** — "
                    f"you'd still need {need}"
                )
        container.add_item(discord.ui.TextDisplay("\n".join(lines)))
        self.add_item(container)


# ---------------------------------------------------------------------------
# The cog
# ---------------------------------------------------------------------------


class RecipeCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="recipe", description="Suggest recipes from ingredients you have"
    )
    @app_commands.describe(
        ingredients="What you've got, e.g. chicken, rice, onion, tomato (Hinglish is fine)",
        diet="Only show recipes for this diet",
        cuisine="Only show this cuisine",
        max_time="Only recipes that take at most this many minutes",
    )
    @app_commands.choices(diet=DIET_CHOICES, cuisine=CUISINE_CHOICES)
    @app_commands.checks.cooldown(1, 5, key=lambda i: (i.guild_id, i.user.id))
    async def recipe(
        self,
        interaction: discord.Interaction,
        ingredients: app_commands.Range[str, 1, 500],
        diet: app_commands.Choice[str] | None = None,
        cuisine: app_commands.Choice[str] | None = None,
        max_time: app_commands.Range[int, 5, 240] | None = None,
    ) -> None:
        parsed = normalize_input(ingredients)
        if not parsed:
            await interaction.response.send_message(
                "I couldn't spot any ingredients in that. try something like "
                "`/recipe ingredients: eggs, bread, onion`.",
                ephemeral=True,
            )
            return

        filters = RecipeFilters(
            diet=diet.value if diet else None,
            cuisine=cuisine.value if cuisine else None,
            max_time=max_time,
        )

        try:
            matches = rank_recipes(parsed, filters=filters, limit=STORE_LIMIT)
        except Exception:
            log.exception("Recipe ranking failed")
            await interaction.response.send_message(
                "something went sideways while cooking that up. try again in a bit.",
                ephemeral=True,
            )
            return

        try:
            if matches:
                session = RecipeSession(interaction.user.id, parsed, matches)
                await interaction.response.send_message(
                    view=RecipeBoard(session, screen="list")
                )
            else:
                fallback = near_misses(parsed, filters=filters, limit=NEAR_MISS_LIMIT)
                await interaction.response.send_message(
                    view=NoMatchView(parsed, fallback)
                )
        except discord.DiscordException:
            log.warning("Could not send recipe response", exc_info=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RecipeCog(bot))
