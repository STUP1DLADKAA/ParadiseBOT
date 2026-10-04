"""Entertainment discovery: ``/example`` and natural-chat requests.

The Discord skin over ``functions.entertainment``:

- ``/example <name>`` — the main search: live autocomplete while typing,
  a clean info card, and two buttons anyone can use (where to watch /
  more like this);
- ``/trending``, ``/upcoming``, ``/recommend`` — the focused entry points;
- plain chat: "recommend anime like Attack on Titan", "what's trending?",
  "where can I watch Oppenheimer in India"… answered in-channel.

Ambiguous queries come back as "which one did you mean?" with pick
buttons; every failure is a friendly string, never a stack trace.
"""

from __future__ import annotations

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from functions.entertainment import EntertainmentService, Identity
from functions.entertainment import nlu
from functions.entertainment.format import (
    format_recommendations,
    format_title_card,
    format_trending,
    format_upcoming,
    loading_line,
    title_heading,
)
from functions.entertainment.models import MediaTitle
from functions.helpers import Colour, ParadiseError, shorten

log = logging.getLogger(__name__)

VIEW_TIMEOUT = 600           # buttons live for 10 minutes
TEXT_TRIGGER_COOLDOWN = 10.0  # per user, plain chat
TEXT_CHANNEL_COOLDOWN = 4.0   # per channel, plain chat
MAX_CARD_CHARS = 3900

CATEGORY_CHOICES = {
    "everything": "all",
    "movies": "movies",
    "series & web series": "series",
    "anime": "anime",
    "web series only": "web",
}


def cooldown(seconds: float = 5):
    return app_commands.checks.cooldown(1, seconds, key=lambda i: (i.guild_id, i.user.id))


def _embed(heading: str, body: str, *, poster: str = "", url: str | None = None) -> discord.Embed:
    embed = discord.Embed(
        title=shorten(heading, 250),
        description=body[:MAX_CARD_CHARS],
        colour=Colour.ACCENT,
        url=url,
    )
    if poster:
        embed.set_thumbnail(url=poster)
    return embed


def _title_embed(title: MediaTitle) -> discord.Embed:
    identity = title.identity
    embed = _embed(
        title_heading(title),
        format_title_card(title),
        poster=title.poster or "",
        url=f"https://www.imdb.com/title/{identity.imdb_id}/" if identity and identity.imdb_id else None,
    )
    if title.sources:
        embed.set_footer(text="sources: " + " · ".join(title.sources))
    return embed


# ---------------------------------------------------------------------------
# Buttons & views
# ---------------------------------------------------------------------------


class CardButtons(discord.ui.View):
    """Follow-up actions on a title card — open to everyone."""

    def __init__(self, service: EntertainmentService, identity: Identity) -> None:
        super().__init__(timeout=VIEW_TIMEOUT)
        self.service = service
        self.identity = identity
        self._busy: set[int] = set()
        # 🎬 Trailer: a deterministic, key-less link (YouTube search for the
        # official trailer) — always available for media identities.
        if identity.is_media and identity.title:
            query = f"{identity.title}" + (f" {identity.year}" if identity.year else "") + " official trailer"
            from urllib.parse import quote

            self.add_item(
                discord.ui.Button(
                    label="Trailer",
                    emoji="🎬",
                    style=discord.ButtonStyle.link,
                    url=f"https://www.youtube.com/results?search_query={quote(query)}",
                    row=0,
                )
            )

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        try:
            if self.message:
                await self.message.edit(view=self)
        except (discord.NotFound, discord.HTTPException):
            pass

    def _allowed(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in self._busy:
            return False
        self._busy.add(interaction.user.id)
        return True

    @discord.ui.button(label="Where to watch", emoji="📡", style=discord.ButtonStyle.primary)
    async def where_to_watch(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._allowed(interaction):
            await interaction.response.send_message("one sec — still working on that.", ephemeral=True)
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            heading, body = await self.service.streaming_report(identity=self.identity)
            await interaction.followup.send(embed=_embed(heading, body), ephemeral=True)
        except ParadiseError as error:
            await _followup(interaction, str(error))
        except Exception:  # noqa: BLE001 - buttons never raise at the user
            log.exception("Where-to-watch button failed")
            await _followup(interaction, "couldn't verify streaming right now — try again in a bit.")
        finally:
            self._busy.discard(interaction.user.id)

    @discord.ui.button(label="More like this", emoji="🍿", style=discord.ButtonStyle.secondary)
    async def more_like_this(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._allowed(interaction):
            await interaction.response.send_message("one sec — still working on that.", ephemeral=True)
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            picks = await self.service.recommend_for_identity(self.identity)
            body = format_recommendations(picks, "More like this", f"seeded by **{self.identity.title}**")
            await interaction.followup.send(embed=_embed(f"More like {self.identity.title}", body), ephemeral=True)
        except ParadiseError as error:
            await _followup(interaction, str(error))
        except Exception:  # noqa: BLE001
            log.exception("More-like-this button failed")
            await _followup(interaction, "couldn't fetch recommendations right now — try again in a bit.")
        finally:
            self._busy.discard(interaction.user.id)


async def _followup(interaction: discord.Interaction, text: str) -> None:
    try:
        await interaction.followup.send(text, ephemeral=True)
    except (discord.NotFound, discord.HTTPException):
        log.debug("Interaction expired before a follow-up could be sent")


class CandidateButton(discord.ui.Button["CandidateView"]):
    def __init__(self, cog: "Entertainment", identity: Identity, invoker_id: int) -> None:
        super().__init__(
            label=shorten(identity.label(), 80),
            style=discord.ButtonStyle.secondary,
        )
        self.cog = cog
        self.identity = identity
        self.invoker_id = invoker_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "run `/example` yourself to look up your own pick.", ephemeral=True
            )
            return
        try:
            await interaction.response.defer()
        except (discord.NotFound, discord.HTTPException):
            return
        await self.cog.deliver_card(
            interaction,
            self.identity.title,
            identity=self.identity,
        )


class CandidateView(discord.ui.View):
    """'Which one did you mean?' — pick buttons for the asker."""

    def __init__(self, cog: "Entertainment", candidates: list[Identity], invoker_id: int) -> None:
        super().__init__(timeout=VIEW_TIMEOUT)
        for candidate in candidates[:4]:
            self.add_item(CandidateButton(cog, candidate, invoker_id))


# ---------------------------------------------------------------------------
# Autocomplete (module level — the decorator needs it at class-creation time)
# ---------------------------------------------------------------------------


async def autocomplete_name(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Live suggestions for the /example input box.

    Must answer within Discord's 3-second window: local memory answers
    instantly, a live IMDb lookup runs under a hard 1.4s budget, and a
    slow network still returns *something* useful.
    """
    cog = interaction.client.get_cog("Entertainment")
    if cog is None:
        return []
    try:
        pairs = await cog.service.autocomplete(current)
    except Exception:  # noqa: BLE001 - never break the input box
        log.debug("Autocomplete failed for %r", current, exc_info=True)
        pairs = []
    return [app_commands.Choice[str](name=name, value=value) for name, value in pairs[:10]]


# ---------------------------------------------------------------------------
# The cog
# ---------------------------------------------------------------------------


class Entertainment(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.settings = getattr(bot.config, "entertainment", None)
        self.service = EntertainmentService(self.settings)
        self._user_last: dict[int, float] = {}
        self._channel_last: dict[int, float] = {}
        log.info("Entertainment ready — %s", self.service.describe())

    async def cog_unload(self) -> None:
        try:
            await self.service.close()
        except Exception:  # noqa: BLE001 - shutdown is best effort
            log.debug("Entertainment shutdown failed", exc_info=True)

    # -- /example -----------------------------------------------------------

    @app_commands.command(
        name="example",
        description="Look up any movie, anime, TV show or web series — live from the web",
    )
    @app_commands.describe(name="A title, franchise or character — autocomplete as you type")
    @cooldown()
    @app_commands.autocomplete(name=autocomplete_name)
    async def example(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, 100],
    ) -> None:
        await interaction.response.defer()
        await self.deliver_card(interaction, name)

    # -- /trending ------------------------------------------------------------

    @app_commands.command(description="What's trending, popular or top-rated right now")
    @app_commands.describe(
        category="What kind of entertainment",
        mode="Trending ≠ popular ≠ highest rated — pick your question",
    )
    @app_commands.choices(
        category=[
            app_commands.Choice(name="everything", value="all"),
            app_commands.Choice(name="movies", value="movies"),
            app_commands.Choice(name="series & web series", value="series"),
            app_commands.Choice(name="anime", value="anime"),
        ],
        mode=[
            app_commands.Choice(name="🔥 trending (live buzz)", value="trending"),
            app_commands.Choice(name="👍 popular (charts)", value="popular"),
            app_commands.Choice(name="⭐ highest rated", value="top rated"),
        ],
    )
    @cooldown(8)
    async def trending(
        self,
        interaction: discord.Interaction,
        category: app_commands.Choice[str] | None = None,
        mode: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer()
        category_value = category.value if category else "all"
        mode_value = mode.value if mode else "trending"
        try:
            heading, entries, notes = await self.service.trending_report(
                category=category_value, mode=mode_value
            )
        except ParadiseError as error:
            await interaction.followup.send(str(error))
            return
        except Exception:  # noqa: BLE001
            log.exception("Trending failed")
            await interaction.followup.send(
                "I couldn't verify what's trending right now. please try again in a bit."
            )
            return
        await interaction.followup.send(
            embed=_embed(heading, format_trending(entries, notes, heading))
        )

    # -- /upcoming --------------------------------------------------------------

    @app_commands.command(description="Upcoming movies, anime and series releases")
    @app_commands.describe(
        category="What's coming",
        days="How far ahead to look (days)",
        region="Country for TV schedules (e.g. India, US, Japan)",
    )
    @app_commands.choices(
        category=[
            app_commands.Choice(name="everything", value="all"),
            app_commands.Choice(name="movies", value="movies"),
            app_commands.Choice(name="anime", value="anime"),
            app_commands.Choice(name="series & web series", value="series"),
            app_commands.Choice(name="web series only", value="web"),
        ]
    )
    @cooldown(8)
    async def upcoming(
        self,
        interaction: discord.Interaction,
        category: app_commands.Choice[str] | None = None,
        days: app_commands.Range[int, 1, 90] = 30,
        region: app_commands.Range[str, 1, 30] | None = None,
    ) -> None:
        await interaction.response.defer()
        try:
            report = await self.service.upcoming_report(
                category=category.value if category else "all",
                days=days,
                region=region or "",
            )
        except ParadiseError as error:
            await interaction.followup.send(str(error))
            return
        except Exception:  # noqa: BLE001
            log.exception("Upcoming failed")
            await interaction.followup.send(
                "I couldn't verify upcoming releases right now. please try again in a bit."
            )
            return
        heading, entries, notes = report
        await interaction.followup.send(
            embed=_embed(heading, format_upcoming(entries, notes, heading))
        )

    # -- /recommend ----------------------------------------------------------------

    @app_commands.command(description="Recommendations: 'like Interstellar', 'underrated anime', …")
    @app_commands.describe(query="A title you liked, or a vibe ('short anime under 25 episodes')")
    @cooldown(6)
    async def recommend(
        self,
        interaction: discord.Interaction,
        query: app_commands.Range[str, 1, 200],
    ) -> None:
        await interaction.response.defer()
        intent = nlu.parse_recommend_query(query)
        try:
            heading, picks, intro = await self.service.recommend_report(
                subject=intent.subject,
                media_type=intent.media_type,
                genre=intent.genre,
                tone=intent.tone,
                era=intent.era,
                max_episodes=intent.max_episodes,
                limit=intent.count or 6,
            )
        except ParadiseError as error:
            await interaction.followup.send(str(error))
            return
        except Exception:  # noqa: BLE001
            log.exception("Recommendations failed")
            await interaction.followup.send(
                "I couldn't verify recommendations right now. please try again in a bit."
            )
            return
        await interaction.followup.send(
            embed=_embed(heading, format_recommendations(picks, heading, intro))
        )

    # -- the shared card pipeline --------------------------------------------------

    async def deliver_card(
        self,
        interaction: discord.Interaction,
        query: str,
        *,
        identity: Identity | None = None,
    ) -> None:
        """Run a lookup and edit the deferred response into a card."""
        try:
            if identity is not None:
                title = await self.service.build_card(identity)
                await self._post_card(interaction, title)
                return

            outcome = await self.service.lookup(query)
        except ParadiseError as error:
            await _edit(interaction, content=str(error))
            return
        except Exception:  # noqa: BLE001 - never crash a channel
            log.exception("Example lookup failed for %r", query)
            await _edit(
                interaction,
                content="I couldn't verify the latest information right now. please try again in a bit.",
            )
            return

        if not outcome.found or outcome.title is None:
            if outcome.candidates:
                view = CandidateView(self, outcome.candidates, interaction.user.id)
                await _edit(
                    interaction,
                    content=(
                        f"🔍 I found multiple results for “{discord.utils.escape_markdown(query)}” "
                        "— which one did you mean?"
                    ),
                    view=view,
                )
            else:
                await _edit(
                    interaction,
                    content=(
                        f"I couldn't find a reliable match for "
                        f"**{discord.utils.escape_markdown(query)}**. try the autocomplete "
                        "suggestions, or check the spelling?"
                    ),
                )
            return

        await self._post_card(interaction, outcome.title, stale=outcome.stale)

    async def _post_card(
        self, interaction: discord.Interaction, title: MediaTitle, *, stale: bool = False
    ) -> None:
        embed = _title_embed(title)
        if stale:
            embed.set_footer(
                text=(embed.footer.text + " · showing a cached card (live refresh failed)")
                if embed.footer and embed.footer.text
                else "showing a cached card — live refresh failed just now"
            )
        view = CardButtons(self.service, title.identity) if title.identity else None
        message = await _edit(interaction, embed=embed, view=view)
        if view is not None and message is not None:
            view.message = message

    # -- mention triggers (via the assistant router) ------------------------------

    async def handle_mention(self, message: discord.Message, text: str) -> bool:
        """True when an addressed entertainment request was answered.

        Called by the assistant router after it classified the mention as
        entertainment (its NLU already ran; we re-parse here because the
        intent object isn't carried across cogs).
        """
        if message.guild is None:
            return False
        intent = self.service.parse_chat(text)
        if intent is None or not intent.is_actionable:
            return False

        channel = message.channel
        permissions = channel.permissions_for(message.guild.me)
        if not permissions.send_messages:
            return False

        try:
            placeholder = await message.reply(
                embed=_embed(
                    "🔎 Searching…",
                    loading_line(intent.subject or intent.raw or "that"),
                ),
                mention_author=False,
            )
        except discord.HTTPException:
            log.debug("Could not post entertainment loading state", exc_info=True)
            return False

        embed = await self._answer_intent(intent, placeholder)
        try:
            await placeholder.edit(embed=embed)
        except discord.HTTPException:
            log.debug("Could not deliver entertainment answer", exc_info=True)
        return True

    # -- plain-chat triggers ------------------------------------------------------

    async def handle_message(self, message: discord.Message) -> bool:
        """True when this message was an entertainment request and was answered."""
        if self.settings is not None and not self.settings.text_triggers:
            return False
        if message.author.bot or message.guild is None:
            return False
        if self.bot.user is not None and any(
            user.id == self.bot.user.id for user in message.mentions
        ):
            return False  # mentions belong to the AI, not to us
        intent = self.service.parse_chat(message.content)
        if intent is None or not intent.is_actionable:
            return False
        if not self._trigger_allowed(message):
            return False

        channel = message.channel
        permissions = channel.permissions_for(message.guild.me)
        if not permissions.send_messages:
            return False

        try:
            placeholder = await message.reply(
                embed=_embed(
                    "🔎 Searching…",
                    loading_line(intent.subject or intent.raw or "that"),
                ),
                mention_author=False,
            )
        except discord.HTTPException:
            log.debug("Could not post entertainment loading state", exc_info=True)
            return False

        embed = await self._answer_intent(intent, placeholder)
        try:
            await placeholder.edit(embed=embed)
        except discord.HTTPException:
            log.debug("Could not deliver entertainment answer", exc_info=True)
        return True

    async def _answer_intent(
        self, intent: nlu.ChatIntent, placeholder: discord.Message
    ) -> discord.Embed:
        try:
            if intent.kind == nlu.RECOMMEND:
                heading, picks, intro = await self.service.recommend_report(
                    subject=intent.subject,
                    media_type=intent.media_type,
                    genre=intent.genre,
                    tone=intent.tone,
                    era=intent.era,
                    max_episodes=intent.max_episodes,
                    limit=intent.count or 6,
                )
                return _embed(heading, format_recommendations(picks, heading, intro))

            if intent.kind == nlu.TRENDING:
                mode = "trending"
                lowered = intent.raw.casefold()
                if "highest rated" in lowered or "top rated" in lowered or (
                    "best" in lowered and "rated" in lowered
                ):
                    mode = "top rated"
                elif "popular" in lowered and "trending" not in lowered:
                    mode = "popular"
                heading, entries, notes = await self.service.trending_report(
                    category=intent.media_type or "all",
                    window=intent.region or "",
                    mode=mode,
                    platform=intent.platform,
                )
                return _embed(heading, format_trending(entries, notes, heading))

            if intent.kind == nlu.UPCOMING:
                days = 7
                lowered = intent.raw.casefold()
                if "month" in lowered:
                    days = 30
                elif "week" in lowered:
                    days = 7
                elif "friday" in lowered:
                    days = 7
                report = await self.service.upcoming_report(
                    category=intent.media_type or "all",
                    days=days,
                    region=intent.region or "",
                    platform=intent.platform,
                    subject=intent.subject,
                    season=intent.season,
                    media_type=intent.media_type or "",
                )
                if isinstance(report[1], str):
                    return _embed(report[0], report[1])
                heading, entries, notes = report
                return _embed(heading, format_upcoming(entries, notes, heading))

            if intent.kind == nlu.STREAMING:
                heading, body = await self.service.streaming_report(
                    subject=intent.subject,
                    region=intent.region,
                    platform=intent.platform,
                )
                return _embed(heading, body)

            if intent.kind == nlu.RATING:
                heading, body = await self.service.rating_report(intent.subject)
                return _embed(heading, body)

            if intent.kind == nlu.INFO:
                detailed = "everything" in intent.raw.casefold() or "all about" in intent.raw.casefold()
                outcome = await self.service.lookup(intent.subject, detailed=detailed)
                if outcome.found and outcome.title is not None:
                    embed = _title_embed(outcome.title)
                    view = CardButtons(self.service, outcome.title.identity) if outcome.title.identity else None
                    if view is not None:
                        try:
                            await placeholder.edit(view=view)
                        except discord.HTTPException:
                            pass
                    return embed
                if outcome.candidates:
                    listing = "\n".join(
                        f"• {candidate.label()}" for candidate in outcome.candidates
                    )
                    return _embed(
                        "I found multiple results",
                        f"which one did you mean?\n{listing}\n\n-# run `/example {outcome.query}` and pick from autocomplete",
                    )
                return _embed(
                    "Not found",
                    f"I couldn't find a reliable match for “{intent.subject}”.",
                )

        except ParadiseError as error:
            return _embed("🔎 Entertainment", str(error))
        except Exception:  # noqa: BLE001 - chat answers never raise
            log.exception("Chat intent failed (%s)", intent.kind)
            return _embed(
                "🔎 Entertainment",
                "I couldn't verify the latest information right now. please try again in a bit.",
            )

        return _embed("🔎 Entertainment", "I'm not sure how to look that up — try `/example <title>`.")

    def _trigger_allowed(self, message: discord.Message) -> bool:
        now = time.monotonic()
        if now - self._user_last.get(message.author.id, 0.0) < TEXT_TRIGGER_COOLDOWN:
            return False
        if now - self._channel_last.get(message.channel.id, 0.0) < TEXT_CHANNEL_COOLDOWN:
            return False
        self._user_last[message.author.id] = now
        self._channel_last[message.channel.id] = now
        if len(self._user_last) > 512:  # pragma: no cover - tidy-up
            cutoff = now - TEXT_TRIGGER_COOLDOWN
            self._user_last = {k: v for k, v in self._user_last.items() if v > cutoff}
            self._channel_last = {k: v for k, v in self._channel_last.items() if v > cutoff}
        return True


async def _edit(interaction: discord.Interaction, **kwargs):
    try:
        if interaction.response.is_done():
            return await interaction.edit_original_response(**kwargs)
        await interaction.response.defer()
        return await interaction.edit_original_response(**kwargs)
    except (discord.NotFound, discord.HTTPException):
        log.debug("Could not edit entertainment response", exc_info=True)
        return None


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Entertainment(bot))
