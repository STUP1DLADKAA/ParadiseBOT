"""Discord commands for the MyAnimeList integration.

Provides the ``/mal`` slash-command group: connect, disconnect, profile,
search, add, update, remove, list. Uses select menus, buttons, and modals
for an interactive anime-management experience.

All commands are guild-only (inherited from ``ParadiseTree``). User MAL
tokens are bound to the Discord user ID and never leave the bot process
except encrypted at rest in Supabase.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from functions.helpers import Colour, shorten

log = logging.getLogger("paradise.commands.mal")

MAL_ANIME_URL = "https://myanimelist.net/anime/{}"

# Status values accepted by the MAL API.
MAL_STATUSES = (
    ("watching", "Watching"),
    ("completed", "Completed"),
    ("on_hold", "On Hold"),
    ("dropped", "Dropped"),
    ("plan_to_watch", "Plan to Watch"),
)

# Entries per page for /mal list.
LIST_PAGE_SIZE = 5


# ----------------------------------------------------------------------
# UI components
# ----------------------------------------------------------------------


class AnimeSelect(discord.ui.Select):
    """Select menu for choosing an anime from search results or a list."""

    def __init__(self, options: list[discord.SelectOption], placeholder: str):
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options[:25],  # Discord limit
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.view.selected_value = self.values[0]  # type: ignore[attr-defined]
        self.view.stop()


class AnimeSelectView(discord.ui.View):
    """Wrapper view for an anime select menu."""

    def __init__(self, select: AnimeSelect, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.add_item(select)
        self.selected_value: str | None = None


class ConfirmView(discord.ui.View):
    """Yes / No confirmation buttons."""

    def __init__(self, timeout: float = 60):
        super().__init__(timeout=timeout)
        self.confirmed: bool | None = None

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = False
        self.stop()
        await interaction.response.defer()


class ListView(discord.ui.View):
    """Pagination controls for /mal list."""

    def __init__(self, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.page = 0
        self.action: str | None = None  # "prev", "next", "close"

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.action = "prev"
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.action = "next"
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.action = "close"
        self.stop()
        await interaction.response.defer()


class AddModal(discord.ui.Modal, title="Add to MyAnimeList"):
    """Modal for adding an anime: status, episodes, score."""

    def __init__(self, anime_title: str, anime_id: int):
        super().__init__()
        self.anime_title = anime_title
        self.anime_id = anime_id
        self.submitted = False

        self.status = discord.ui.TextInput(
            label="Status",
            placeholder="watching, completed, on_hold, dropped, plan_to_watch",
            default="plan_to_watch",
            max_length=20,
        )
        self.episodes = discord.ui.TextInput(
            label="Episodes watched",
            placeholder="0",
            default="0",
            max_length=5,
        )
        self.score = discord.ui.TextInput(
            label="Score (0-10, optional)",
            placeholder="0",
            default="0",
            max_length=2,
        )

        self.add_item(self.status)
        self.add_item(self.episodes)
        self.add_item(self.score)

    async def on_submit(self, interaction: discord.Interaction):
        self.submitted = True
        self.status_value = self.status.value.strip().lower()
        try:
            self.episodes_value = int(self.episodes.value.strip() or "0")
        except ValueError:
            self.episodes_value = 0
        try:
            self.score_value = int(self.score.value.strip() or "0")
        except ValueError:
            self.score_value = 0
        self.stop()

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        await interaction.response.send_message(
            "Something went wrong with that form. Try again.", ephemeral=True
        )
        self.stop()


class UpdateModal(discord.ui.Modal, title="Update MyAnimeList Entry"):
    """Modal for updating an existing entry."""

    def __init__(
        self,
        anime_title: str,
        current_status: str,
        current_episodes: int,
        current_score: int,
    ):
        super().__init__()
        self.anime_title = anime_title
        self.submitted = False

        self.status = discord.ui.TextInput(
            label="Status",
            placeholder="watching, completed, on_hold, dropped, plan_to_watch",
            default=current_status,
            max_length=20,
        )
        self.episodes = discord.ui.TextInput(
            label="Episodes watched",
            placeholder=str(current_episodes),
            default=str(current_episodes),
            max_length=5,
        )
        self.score = discord.ui.TextInput(
            label="Score (0-10, optional)",
            placeholder=str(current_score) if current_score else "0",
            default=str(current_score) if current_score else "0",
            max_length=2,
        )

        self.add_item(self.status)
        self.add_item(self.episodes)
        self.add_item(self.score)

    async def on_submit(self, interaction: discord.Interaction):
        self.submitted = True
        self.status_value = self.status.value.strip().lower()
        try:
            self.episodes_value = int(self.episodes.value.strip() or "0")
        except ValueError:
            self.episodes_value = 0
        try:
            self.score_value = int(self.score.value.strip() or "0")
        except ValueError:
            self.score_value = 0
        self.stop()

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        await interaction.response.send_message(
            "Something went wrong with that form. Try again.", ephemeral=True
        )
        self.stop()


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _anime_embed(anime: dict) -> discord.Embed:
    """Build a Discord embed for an anime search result."""
    node = anime.get("node", anime)
    title = node.get("title", "Unknown")
    synopsis = node.get("synopsis") or "No synopsis available."
    mean = node.get("mean")
    popularity = node.get("popularity")
    num_episodes = node.get("num_episodes")
    media_type = node.get("media_type", "").title()
    status = node.get("status", "").title()
    main_picture = node.get("main_picture", {})
    image_url = main_picture.get("medium") if main_picture else None

    embed = discord.Embed(
        title=title,
        url=MAL_ANIME_URL.format(node.get("id", "")),
        description=shorten(synopsis, 400),
        colour=Colour.ACCENT,
    )
    if image_url:
        embed.set_thumbnail(url=image_url)
    embed.add_field(name="Type", value=media_type or "—", inline=True)
    embed.add_field(name="Status", value=status or "—", inline=True)
    if num_episodes:
        embed.add_field(name="Episodes", value=str(num_episodes), inline=True)
    if mean:
        embed.add_field(name="Mean Score", value=f"{mean}/10", inline=True)
    if popularity:
        embed.add_field(name="Popularity", value=f"#{popularity}", inline=True)
    embed.set_footer(text="MyAnimeList • ParadiseBOT")
    return embed


def shorten_text(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"


# ----------------------------------------------------------------------
# Cog
# ----------------------------------------------------------------------


mal_group = app_commands.Group(
    name="mal",
    description="Manage your MyAnimeList anime list from Discord",
)


class MalCog(commands.Cog):
    """MyAnimeList integration cog."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.tree.add_command(mal_group)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(mal_group)

    @property
    def service(self):
        """The MalService instance, or None if MAL is not configured."""
        return getattr(self.bot, "mal_service", None)

    def _require_service(self) -> bool:
        """True if the MAL service is configured."""
        return self.service is not None

    # ------------------------------------------------------------------
    # /mal connect
    # ------------------------------------------------------------------

    @mal_group.command(description="Link your MyAnimeList account via OAuth")
    async def connect(self, interaction: discord.Interaction) -> None:
        """Start the PKCE OAuth flow to link a MAL account."""
        if not self._require_service():
            await interaction.response.send_message(
                "The MAL integration is not configured on this bot. "
                "Ask an administrator to set MAL_CLIENT_ID, MAL_CLIENT_SECRET, "
                "MAL_CALLBACK_URL, and MAL_ENCRYPTION_KEY.",
                ephemeral=True,
            )
            return

        discord_user_id = interaction.user.id
        authorize_url, _state = self.service.start_connect_flow(discord_user_id)

        view = discord.ui.View()
        view.add_item(
            discord.ui.Button(
                label="Authorize on MyAnimeList",
                style=discord.ButtonStyle.link,
                url=authorize_url,
            )
        )
        await interaction.response.send_message(
            "Click the button below to authorize ParadiseBOT on MyAnimeList. "
            "You'll be redirected back here after approval — then run `/mal profile` "
            "to verify the link.\n\n"
            "No MAL password is ever collected by this bot.",
            view=view,
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /mal disconnect
    # ------------------------------------------------------------------

    @mal_group.command(description="Unlink your MyAnimeList account")
    async def disconnect(self, interaction: discord.Interaction) -> None:
        """Revoke and delete the user's MAL tokens."""
        if not self._require_service():
            await interaction.response.send_message(
                "The MAL integration is not configured on this bot.", ephemeral=True
            )
            return

        discord_user_id = interaction.user.id
        if not await self.service.store.has_tokens(discord_user_id):
            await interaction.response.send_message(
                "You don't have a linked MyAnimeList account.", ephemeral=True
            )
            return

        view = ConfirmView()
        await interaction.response.send_message(
            "This will revoke your MAL tokens and unlink your account. "
            "Continue?", view=view, ephemeral=True
        )
        await view.wait()

        if view.confirmed is True:
            ok = await self.service.disconnect(discord_user_id, revoke=True)
            if ok:
                await interaction.followup.send(
                    "Your MyAnimeList account has been unlinked.", ephemeral=True
                )
            else:
                await interaction.followup.send(
                    "Could not unlink — try again later.", ephemeral=True
                )
        else:
            await interaction.followup.send(
                "Unlink cancelled.", ephemeral=True
            )

    # ------------------------------------------------------------------
    # /mal profile
    # ------------------------------------------------------------------

    @mal_group.command(description="View your linked MyAnimeList profile")
    async def profile(self, interaction: discord.Interaction) -> None:
        """Show the user's MAL profile and list stats."""
        if not self._require_service():
            await interaction.response.send_message(
                "The MAL integration is not configured on this bot.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        discord_user_id = interaction.user.id
        profile = await self.service.get_profile(discord_user_id)
        if profile is None:
            await interaction.followup.send(
                "You don't have a linked MyAnimeList account. "
                "Run `/mal connect` first.", ephemeral=True
            )
            return

        name = profile.get("name", "Unknown")
        mal_user_id = profile.get("id", 0)
        stats = profile.get("anime_statistics", {})

        embed = discord.Embed(
            title=f"MyAnimeList Profile — {name}",
            url=MAL_ANIME_URL.format(mal_user_id) if mal_user_id else None,
            description="Linked to your Discord account.",
            colour=Colour.ACCENT,
        )
        if stats:
            anime_stats = stats.get("anime", {})
            if anime_stats:
                num_watched = anime_stats.get("num_watched_episodes", 0)
                num_days = anime_stats.get("num_days_watched", 0)
                embed.add_field(name="Episodes Watched", value=str(num_watched), inline=True)
                embed.add_field(name="Days Watched", value=str(num_days), inline=True)
        embed.set_footer(text="MyAnimeList • ParadiseBOT")

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /mal search
    # ------------------------------------------------------------------

    @mal_group.command(description="Search for anime on MyAnimeList")
    @app_commands.describe(query="Anime title to search for")
    async def search(self, interaction: discord.Interaction, query: str) -> None:
        """Search anime and show results as a select menu."""
        if not self._require_service():
            await interaction.response.send_message(
                "The MAL integration is not configured on this bot.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)

        result = await self.service.search_anime(query, limit=10)
        if result is None:
            await interaction.followup.send(
                "Could not search MyAnimeList right now. Try again later.",
                ephemeral=True,
            )
            return

        data = result.get("data", [])
        if not data:
            await interaction.followup.send(
                f"No anime found matching **{query}**.", ephemeral=True
            )
            return

        options = []
        for item in data:
            node = item.get("node", item)
            title = node.get("title", "Unknown")
            anime_id = node.get("id", 0)
            options.append(
                discord.SelectOption(
                    label=shorten_text(title, 100),
                    value=str(anime_id),
                    description=shorten_text(node.get("synopsis", ""), 100),
                )
            )

        select = AnimeSelect(
            options=options,
            placeholder="Select an anime to view details",
        )
        view = AnimeSelectView(select)
        await interaction.followup.send(
            f"Search results for **{query}** — select an anime:",
            view=view,
            ephemeral=True,
        )
        await view.wait()

        if view.selected_value is None:
            return

        anime_id = int(view.selected_value)
        anime = await self.service.get_anime(anime_id)
        if anime is None:
            await interaction.followup.send(
                "Could not fetch details for that anime.", ephemeral=True
            )
            return

        embed = _anime_embed(anime)

        add_button = discord.ui.Button(
            label="Add to My List",
            style=discord.ButtonStyle.success,
        )

        async def _add_button_callback(button_interaction: discord.Interaction):
            if button_interaction.user.id != interaction.user.id:
                await button_interaction.response.send_message(
                    "Only the user who searched can add this anime.", ephemeral=True
                )
                return
            modal = AddModal(
                anime_title=anime.get("node", anime).get("title", "Unknown"),
                anime_id=anime_id,
            )
            await button_interaction.response.send_modal(modal)
            await modal.wait()
            if not modal.submitted:
                return
            await self._handle_add_submission(
                button_interaction,
                anime_id,
                modal.status_value,
                modal.episodes_value,
                modal.score_value,
            )

        add_button.callback = _add_button_callback
        add_view = discord.ui.View()
        add_view.add_item(add_button)
        await interaction.followup.send(embed=embed, view=add_view, ephemeral=True)

    async def _handle_add_submission(
        self,
        interaction: discord.Interaction,
        anime_id: int,
        status: str,
        episodes: int,
        score: int,
    ) -> None:
        """Handle an add-anime submission from a modal."""
        discord_user_id = interaction.user.id

        valid_statuses = {v for v, _ in MAL_STATUSES}
        if status not in valid_statuses:
            await interaction.followup.send(
                f"Invalid status '{status}'. Use one of: "
                f"{', '.join(sorted(valid_statuses))}.",
                ephemeral=True,
            )
            return

        fields: dict = {"status": status}
        if episodes > 0:
            fields["num_watched_episodes"] = episodes
        if score > 0:
            fields["score"] = score

        result = await self.service.update_entry(discord_user_id, anime_id, **fields)
        if result is None:
            await interaction.followup.send(
                "Failed to add anime to your list. You may need to re-auth "
                "with `/mal connect`.", ephemeral=True
            )
            return

        anime = await self.service.get_anime(anime_id)
        title = "this anime"
        if anime:
            title = anime.get("node", anime).get("title", "this anime")
        await interaction.followup.send(
            f"✅ Added **{title}** to your MyAnimeList list as **{status}**.",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /mal add
    # ------------------------------------------------------------------

    @mal_group.command(description="Add an anime to your MyAnimeList")
    @app_commands.describe(
        anime_id="MyAnimeList anime ID (find it with /mal search)",
        status="watching, completed, on_hold, dropped, or plan_to_watch",
        episodes="Number of episodes watched",
        score="Score out of 10 (optional)",
    )
    async def add(
        self,
        interaction: discord.Interaction,
        anime_id: app_commands.Range[int, 1],
        status: str | None = None,
        episodes: app_commands.Range[int, 0, 10000] | None = None,
        score: app_commands.Range[int, 0, 10] | None = None,
    ) -> None:
        """Add an anime to the user's MAL list."""
        if not self._require_service():
            await interaction.response.send_message(
                "The MAL integration is not configured on this bot.", ephemeral=True
            )
            return

        valid_statuses = {v for v, _ in MAL_STATUSES}
        if status is not None and status not in valid_statuses:
            await interaction.response.send_message(
                f"Invalid status '{status}'. Use one of: "
                f"{', '.join(sorted(valid_statuses))}.",
                ephemeral=True,
            )
            return

        if status is None or episodes is None:
            anime = await self.service.get_anime(anime_id)
            if anime is None:
                await interaction.response.send_message(
                    "Could not find that anime on MyAnimeList.", ephemeral=True
                )
                return
            title = anime.get("node", anime).get("title", "Unknown")
            modal = AddModal(anime_title=title, anime_id=anime_id)
            if status:
                modal.status.default = status
            if episodes is not None:
                modal.episodes.default = str(episodes)
            if score is not None:
                modal.score.default = str(score)

            await interaction.response.send_modal(modal)
            await modal.wait()
            if not modal.submitted:
                return
            status = modal.status_value
            episodes = modal.episodes_value
            score = modal.score_value

        await interaction.response.defer(thinking=True, ephemeral=True)
        await self._handle_add_submission(
            interaction, anime_id, status, episodes, score or 0
        )

    # ------------------------------------------------------------------
    # /mal update
    # ------------------------------------------------------------------

    @mal_group.command(description="Update an entry in your MyAnimeList")
    @app_commands.describe(
        anime_id="MyAnimeList anime ID (find it with /mal list)",
        status="watching, completed, on_hold, dropped, or plan_to_watch",
        episodes="Number of episodes watched",
        score="Score out of 10 (optional)",
    )
    async def update(
        self,
        interaction: discord.Interaction,
        anime_id: app_commands.Range[int, 1],
        status: str | None = None,
        episodes: app_commands.Range[int, 0, 10000] | None = None,
        score: app_commands.Range[int, 0, 10] | None = None,
    ) -> None:
        """Update an existing anime entry."""
        if not self._require_service():
            await interaction.response.send_message(
                "The MAL integration is not configured on this bot.", ephemeral=True
            )
            return

        discord_user_id = interaction.user.id

        animelist = await self.service.get_list(discord_user_id, limit=1000)
        if animelist is None:
            await interaction.response.send_message(
                "Could not fetch your MyAnimeList.", ephemeral=True
            )
            return

        current_entry = None
        for item in animelist.get("data", []):
            node = item.get("node", {})
            if node.get("id") == anime_id:
                current_entry = item
                break

        if current_entry is None:
            await interaction.response.send_message(
                "That anime is not in your MyAnimeList.", ephemeral=True
            )
            return

        node = current_entry.get("node", {})
        list_status = current_entry.get("list_status", {})
        title = node.get("title", "Unknown")
        current_status = list_status.get("status", "plan_to_watch")
        current_episodes = list_status.get("num_watched_episodes", 0)
        current_score = list_status.get("score", 0) or 0

        valid_statuses = {v for v, _ in MAL_STATUSES}
        if status is not None and status not in valid_statuses:
            await interaction.response.send_message(
                f"Invalid status '{status}'. Use one of: "
                f"{', '.join(sorted(valid_statuses))}.",
                ephemeral=True,
            )
            return

        if status is None or episodes is None or score is None:
            modal = UpdateModal(
                anime_title=title,
                current_status=current_status,
                current_episodes=current_episodes,
                current_score=current_score,
            )
            if status:
                modal.status.default = status
            if episodes is not None:
                modal.episodes.default = str(episodes)
            if score is not None:
                modal.score.default = str(score)

            await interaction.response.send_modal(modal)
            await modal.wait()
            if not modal.submitted:
                return
            status = modal.status_value
            episodes = modal.episodes_value
            score = modal.score_value

        await interaction.response.defer(thinking=True, ephemeral=True)

        fields: dict = {}
        if status is not None:
            fields["status"] = status
        if episodes is not None:
            fields["num_watched_episodes"] = episodes
        if score is not None:
            fields["score"] = score

        if not fields:
            await interaction.followup.send("No changes provided.", ephemeral=True)
            return

        result = await self.service.update_entry(discord_user_id, anime_id, **fields)
        if result is None:
            await interaction.followup.send(
                "Failed to update your list entry. You may need to re-auth "
                "with `/mal connect`.", ephemeral=True
            )
            return

        await interaction.followup.send(
            f"✅ Updated **{title}** in your MyAnimeList.", ephemeral=True
        )

    # ------------------------------------------------------------------
    # /mal remove
    # ------------------------------------------------------------------

    @mal_group.command(description="Remove an anime from your MyAnimeList")
    @app_commands.describe(anime_id="MyAnimeList anime ID (find it with /mal list)")
    async def remove(
        self,
        interaction: discord.Interaction,
        anime_id: app_commands.Range[int, 1],
    ) -> None:
        """Remove an anime from the user's MAL list with confirmation."""
        if not self._require_service():
            await interaction.response.send_message(
                "The MAL integration is not configured on this bot.", ephemeral=True
            )
            return

        discord_user_id = interaction.user.id

        animelist = await self.service.get_list(discord_user_id, limit=1000)
        if animelist is None:
            await interaction.response.send_message(
                "Could not fetch your MyAnimeList.", ephemeral=True
            )
            return

        title = "this anime"
        for item in animelist.get("data", []):
            node = item.get("node", {})
            if node.get("id") == anime_id:
                title = node.get("title", "this anime")
                break

        view = ConfirmView()
        await interaction.response.send_message(
            f"Are you sure you want to remove **{title}** from your MyAnimeList?",
            view=view,
            ephemeral=True,
        )
        await view.wait()

        if view.confirmed is not True:
            await interaction.followup.send("Removal cancelled.", ephemeral=True)
            return

        await interaction.followup.send("Removing...", ephemeral=True)
        ok = await self.service.remove_entry(discord_user_id, anime_id)
        if ok:
            await interaction.edit_original_response(
                content=f"✅ Removed **{title}** from your MyAnimeList.",
                view=None,
            )
        else:
            await interaction.edit_original_response(
                content="❌ Failed to remove. The anime may not be in your list.",
                view=None,
            )

    # ------------------------------------------------------------------
    # /mal list
    # ------------------------------------------------------------------

    @mal_group.command(description="View your MyAnimeList anime list")
    @app_commands.describe(
        status="Filter by status (watching, completed, on_hold, dropped, plan_to_watch)",
        page="Page number (starting at 1)",
    )
    async def list(
        self,
        interaction: discord.Interaction,
        status: str | None = None,
        page: app_commands.Range[int, 1] | None = 1,
    ) -> None:
        """Show the user's MAL anime list with pagination."""
        if not self._require_service():
            await interaction.response.send_message(
                "The MAL integration is not configured on this bot.", ephemeral=True
            )
            return

        discord_user_id = interaction.user.id

        await interaction.response.defer(thinking=True)

        animelist = await self.service.get_list(
            discord_user_id, status=status, limit=1000
        )
        if animelist is None:
            await interaction.followup.send(
                "Could not fetch your MyAnimeList. You may need to re-auth "
                "with `/mal connect`.", ephemeral=True
            )
            return

        entries = animelist.get("data", [])
        if not entries:
            filter_text = f" with status '{status}'" if status else ""
            await interaction.followup.send(
                f"Your MyAnimeList{filter_text} is empty.", ephemeral=True
            )
            return

        total = len(entries)
        page_idx = (page or 1) - 1
        total_pages = (total + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE
        page_idx = max(0, min(page_idx, total_pages - 1))
        start = page_idx * LIST_PAGE_SIZE
        end = min(start + LIST_PAGE_SIZE, total)
        page_entries = entries[start:end]

        filter_text = f" ({status})" if status else ""
        embed = discord.Embed(
            title=f"MyAnimeList{filter_text} — Page {page_idx + 1}/{total_pages}",
            description=f"{total} entr{'y' if total == 1 else 'ies'}",
            colour=Colour.ACCENT,
        )
        for i, entry in enumerate(page_entries, start + 1):
            node = entry.get("node", {})
            list_status = entry.get("list_status", {})
            entry_title = node.get("title", "Unknown")
            anime_id = node.get("id", 0)
            entry_status = list_status.get("status", "—").replace("_", " ").title()
            ep_count = list_status.get("num_watched_episodes", 0)
            score = list_status.get("score")
            score_text = f"{score}/10" if score else "—"
            embed.add_field(
                name=f"{i}. {entry_title}",
                value=(
                    f"Status: {entry_status} • Episodes: {ep_count} • Score: {score_text}\n"
                    f"<{MAL_ANIME_URL.format(anime_id)}>"
                ),
                inline=False,
            )
        embed.set_footer(text="MyAnimeList • ParadiseBOT")

        view = ListView()
        view.prev.disabled = page_idx == 0
        view.next.disabled = page_idx >= total_pages - 1

        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        """Global error handler for this cog."""
        log.error("MAL command error: %s", error, exc_info=error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "Something went wrong with that MAL command. Try again later.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "Something went wrong with that MAL command. Try again later.",
                    ephemeral=True,
                )
        except discord.DiscordException:
            log.warning("Could not send MAL error response", exc_info=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MalCog(bot))
