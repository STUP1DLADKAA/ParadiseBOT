"""The dictionary: `/dictionary`, `/define`, and plain-English requests.

The experience is deliberately one step: ask, get a card, optionally hear
it. Everything heavy lives in ``functions/dictionary`` — this module is
only the Discord skin:

    search  →  loading card  →  rendered result  →  pronunciation / more

Entry points, all sharing one pipeline:

- ``/dictionary <word>`` — the command,
- ``/define <word>`` — the older name, kept working,
- plain chat: ``define hello``, ``dictionary hello``, ``Define "hello"``,
  ``what does hello mean?``, ``meaning of hello``.

Interaction rules: the card's 🔊 button is open to everyone (the audio
comes back privately), while the buttons that *change* the shared message
belong to whoever asked. Every response is guarded against expired
interactions, deleted messages and double-clicks.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time

import discord
from discord.ext import commands

from functions.dictionary import (
    DictionaryFeature,
    LookupResult,
    WordDetails,
    WordEntry,
    match_text_request,
)
from functions.helpers import Colour, ParadiseError, shorten

log = logging.getLogger(__name__)

CARD_FILENAME = "dictionary.png"
VIEW_TIMEOUT = 600          # 10 minutes of interactivity, then buttons grey out
PRONOUNCE_COOLDOWN = 5.0    # per user, per card
TEXT_TRIGGER_COOLDOWN = 8.0     # per user, for plain-chat requests
TEXT_CHANNEL_COOLDOWN = 3.0     # per channel, for plain-chat requests
MAX_SCREEN_CHARS = 3600

SCREEN_CARD = "card"
SCREEN_MORE = "more"
SCREEN_EXAMPLES = "examples"

GENERIC_ERROR = "the dictionary is having a moment. try again in a bit."


# ---------------------------------------------------------------------------
# Text for the non-image screens
# ---------------------------------------------------------------------------


def loading_text(term: str) -> str:
    return f"## 🔎 Looking up “{discord.utils.escape_markdown(term)}”…"


def not_found_text(
    query: str,
    suggestions: tuple[str, ...],
    suggested_word: str = "",
) -> str:
    """Copy for a miss, with a deliberately different ambiguous-match state."""
    safe = discord.utils.escape_markdown(query)
    if suggested_word:
        suggestion = discord.utils.escape_markdown(suggested_word)
        return "\n".join(
            [
                f"## ✦ Did you mean “{suggestion}”?",
                "",
                f"Couldn't find an exact entry for “{safe}”, so I left the choice with you.",
            ]
        )

    lines = [f"## 🔎 Couldn't find “{safe}”.", ""]
    if suggestions:
        lines.append("Did you mean one of these?")
    else:
        lines.append(
            "No dictionary entry came back for that one — check the spelling, "
            "or try the base form of the word."
        )
    return "\n".join(lines)


def _clip(text: str, limit: int = MAX_SCREEN_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _bullet_list(values: tuple[str, ...], limit: int = 12) -> str:
    return "  ·  ".join(discord.utils.escape_markdown(v) for v in values[:limit])


def card_fallback_text(entry: WordEntry) -> str:
    """A tidy text card, used only when image rendering isn't available."""
    lines = [f"## {discord.utils.escape_markdown(entry.word)}"]
    variants = entry.phonetic_variants[:2]
    if variants:
        spelling = "  ".join(
            f"`{v.text}`" + (f" *{v.label}*" if v.label else "") for v in variants
        )
        lines.append(spelling)
    sense = entry.primary
    if sense is not None:
        lines.append("")
        part = f"*{sense.part_of_speech}* — " if sense.part_of_speech else ""
        lines.append(f"{part}{discord.utils.escape_markdown(sense.definition)}")
        if sense.example:
            lines.append(f"> {discord.utils.escape_markdown(sense.example)}")
    synonyms = entry.all_synonyms(6)
    if synonyms:
        lines.append("")
        lines.append(f"**Synonyms** · {_bullet_list(synonyms, 6)}")
    return _clip("\n".join(lines))


def more_text(entry: WordEntry, details: WordDetails) -> str:
    """Everything that didn't belong on the card."""
    lines = [f"## 📚 {discord.utils.escape_markdown(entry.word)}", ""]

    grouped = entry.senses_by_part()
    if grouped:
        lines.append("**Meanings**")
        shown = 0
        for part, senses in grouped:
            if shown >= 6:
                break
            lines.append(f"*{part}*")
            for sense in senses[:3]:
                if shown >= 6:
                    break
                shown += 1
                lines.append(f"{shown}. {discord.utils.escape_markdown(sense.definition)}")
            lines.append("")

    variants = entry.phonetic_variants
    lines.append("**Pronunciation**")
    if variants:
        for variant in variants[:4]:
            label = f" — {variant.label}" if variant.label else ""
            audio = "  🔊" if variant.has_audio else ""
            lines.append(f"`{variant.text}`{label}{audio}")
    else:
        lines.append("-# No phonetic spelling available for this word.")
    lines.append("")

    if details.forms:
        lines.append("**Forms**")
        lines.append(
            "  ·  ".join(
                f"{form.label}: **{discord.utils.escape_markdown(form.value)}**"
                for form in details.forms[:6]
            )
        )
        lines.append("")

    # The card only has room for a handful; this is the full list.
    synonyms = entry.all_synonyms(16)
    if synonyms:
        lines.append("**Synonyms**")
        lines.append(_bullet_list(synonyms, 16))
        lines.append("")

    antonyms = entry.all_antonyms(10)
    if antonyms:
        lines.append("**Antonyms**")
        lines.append(_bullet_list(antonyms, 10))
        lines.append("")

    related = details.related or entry.related
    if related:
        lines.append("**Related words**")
        lines.append(_bullet_list(related, 12))
        lines.append("")

    if entry.origin:
        lines.append("**Origin**")
        lines.append(f"-# {discord.utils.escape_markdown(shorten(entry.origin, 400))}")
        lines.append("")

    if entry.sources:
        lines.append(f"-# Source: {entry.sources[0]}")
    return _clip("\n".join(lines).strip())


def examples_text(entry: WordEntry, details: WordDetails) -> str:
    lines = [f"## 📖 {discord.utils.escape_markdown(entry.word)} in use", ""]
    shown = 0
    for part, senses in entry.senses_by_part():
        for sense in senses:
            if not sense.example or shown >= 6:
                continue
            shown += 1
            label = f"*{part}* · " if part else ""
            lines.append(f"{label}{discord.utils.escape_markdown(sense.definition)}")
            lines.append(f"> {discord.utils.escape_markdown(sense.example)}")
            lines.append("")
    if not shown:
        lines.append("-# No example sentences came back for this word.")
        lines.append("")
        synonyms = entry.all_synonyms(8)
        if synonyms:
            lines.append("**Try these close words instead**")
            lines.append(_bullet_list(synonyms, 8))
    return _clip("\n".join(lines).strip())


def has_extra_examples(entry: WordEntry) -> bool:
    return len(entry.examples) > 1


# ---------------------------------------------------------------------------
# Session state — one per posted card
# ---------------------------------------------------------------------------


class Editor:
    """Edits the message a result lives in, whichever way it was posted."""

    def __init__(
        self,
        *,
        interaction: discord.Interaction | None = None,
        message: discord.Message | None = None,
    ) -> None:
        self.interaction = interaction
        self.message = message

    async def edit(self, **kwargs) -> None:
        try:
            if self.interaction is not None:
                await self.interaction.edit_original_response(**kwargs)
            elif self.message is not None:
                await self.message.edit(**kwargs)
        except discord.NotFound:
            log.debug("Dictionary message vanished before it could be updated")
        except discord.HTTPException:
            log.debug("Could not update dictionary message", exc_info=True)


class DictionarySession:
    """Everything a posted card needs to keep answering button presses."""

    def __init__(
        self,
        feature: DictionaryFeature,
        result: LookupResult,
        invoker_id: int,
        card: bytes | None,
    ) -> None:
        self.feature = feature
        self.result = result
        self.entry: WordEntry = result.entry  # type: ignore[assignment]
        self.invoker_id = invoker_id
        self.card = card
        self.details: WordDetails | None = None
        self.editor: Editor | None = None
        self.screen = SCREEN_CARD
        self.screen_lock = asyncio.Lock()
        self._pronounce_at: dict[int, float] = {}
        self._pronouncing: set[int] = set()

    # -- attachments -------------------------------------------------------

    def file(self) -> discord.File | None:
        """A fresh File each time — a BytesIO can only be uploaded once."""
        if not self.card:
            return None
        return discord.File(io.BytesIO(self.card), filename=CARD_FILENAME)

    def attachments(self) -> list[discord.File]:
        card = self.file()
        return [card] if card is not None else []

    # -- pronunciation throttling -----------------------------------------

    def pronounce_wait(self, user_id: int) -> float:
        last = self._pronounce_at.get(user_id, 0.0)
        return max(0.0, last + PRONOUNCE_COOLDOWN - time.monotonic())

    def begin_pronounce(self, user_id: int) -> bool:
        if user_id in self._pronouncing:
            return False
        self._pronouncing.add(user_id)
        self._pronounce_at[user_id] = time.monotonic()
        if len(self._pronounce_at) > 64:  # pragma: no cover - tidy-up
            self._pronounce_at.clear()
        return True

    def end_pronounce(self, user_id: int) -> None:
        self._pronouncing.discard(user_id)

    async def load_details(self) -> WordDetails:
        if self.details is None:
            self.details = await self.feature.details(self.entry)
        return self.details


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


async def _guard(interaction: discord.Interaction, session: DictionarySession) -> bool:
    """Only the asker may change what everyone else is looking at."""
    if interaction.user.id == session.invoker_id:
        return True
    await _safe_respond(
        interaction,
        "that card belongs to someone else — run `/dictionary` for your own "
        "(the 🔊 button works for everyone though).",
    )
    return False


async def _safe_respond(interaction: discord.Interaction, content: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)
    except discord.NotFound:
        log.debug("Interaction expired before a reply could be sent")
    except discord.HTTPException:
        log.debug("Could not reply to interaction", exc_info=True)


class LoadingView(discord.ui.LayoutView):
    """The 'looking it up' state — same shape as the result it becomes."""

    def __init__(self, term: str) -> None:
        super().__init__(timeout=60)
        container = discord.ui.Container(accent_colour=Colour.ACCENT)
        container.add_item(discord.ui.TextDisplay(loading_text(term)))
        self.add_item(container)


class NoticeView(discord.ui.LayoutView):
    """A plain message in the same visual language as everything else."""

    def __init__(self, text: str) -> None:
        super().__init__(timeout=60)
        container = discord.ui.Container(accent_colour=Colour.ACCENT)
        container.add_item(discord.ui.TextDisplay(_clip(text)))
        self.add_item(container)


class CardButtons(discord.ui.ActionRow["ResultView"]):
    def __init__(self, session: DictionarySession, *, screen: str) -> None:
        super().__init__()
        self.session = session
        self.screen = screen
        if screen != SCREEN_CARD:
            self.add_item(BackButton(session))
        self.add_item(PronounceButton(session))
        if screen != SCREEN_MORE:
            self.add_item(MoreButton(session))
        if has_extra_examples(session.entry) and screen != SCREEN_EXAMPLES:
            self.add_item(ExamplesButton(session))


class PronounceButton(discord.ui.Button["ResultView"]):
    def __init__(self, session: DictionarySession) -> None:
        super().__init__(
            label="Pronounce", emoji="🔊", style=discord.ButtonStyle.primary
        )
        self.session = session

    async def callback(self, interaction: discord.Interaction) -> None:
        session = self.session
        user_id = interaction.user.id

        wait = session.pronounce_wait(user_id)
        if wait > 0:
            await _safe_respond(interaction, f"give it {wait:.0f}s.")
            return
        if not session.begin_pronounce(user_id):
            await _safe_respond(interaction, "already working on that one.")
            return

        try:
            try:
                await interaction.response.defer(ephemeral=True, thinking=True)
            except (discord.NotFound, discord.HTTPException):
                log.debug("Pronounce interaction expired before deferring")
                return

            try:
                outcome = await session.feature.pronounce(
                    session.entry.word,
                    entry=session.entry,
                    with_alternates=True,
                )
            except ParadiseError as error:
                await _followup(interaction, f"🔇 {error}")
                return
            except Exception:  # noqa: BLE001 - audio never breaks the card
                log.exception("Pronunciation failed for %r", session.entry.word)
                await _followup(
                    interaction,
                    "🔇 audio is temporarily unavailable — the definition still works.",
                )
                return

            spoken = await self._maybe_speak_aloud(interaction, outcome)
            if spoken:
                return

            files = [
                discord.File(
                    io.BytesIO(outcome.clip.data),
                    filename=outcome.clip.filename(session.entry.word),
                )
            ]
            for alternate in outcome.alternates[:1]:
                files.append(
                    discord.File(
                        io.BytesIO(alternate.data),
                        filename=alternate.filename(session.entry.word),
                    )
                )
            label = outcome.region_label
            detail = f" · {label}" if label else ""
            await _followup(
                interaction,
                f"🔊 **{discord.utils.escape_markdown(session.entry.word)}**{detail}",
                files=files,
            )
        finally:
            session.end_pronounce(user_id)

    async def _maybe_speak_aloud(
        self, interaction: discord.Interaction, outcome
    ) -> bool:
        """Play it in the asker's voice channel when that's actually possible."""
        playback = self.session.feature.playback
        if not playback.support():
            return False
        member = interaction.user
        if not isinstance(member, discord.Member) or playback.channel_for(member) is None:
            return False
        try:
            channel_name = await playback.speak(member, outcome.clip)
        except ParadiseError as error:
            log.debug("Voice playback declined: %s", error)
            return False
        except Exception:  # noqa: BLE001 - fall back to the attachment
            log.warning("Voice playback failed", exc_info=True)
            return False
        await _followup(
            interaction,
            f"🔊 said **{discord.utils.escape_markdown(self.session.entry.word)}** "
            f"in **{discord.utils.escape_markdown(channel_name)}**.",
        )
        return True


class MoreButton(discord.ui.Button["ResultView"]):
    def __init__(self, session: DictionarySession) -> None:
        super().__init__(label="More", emoji="📚", style=discord.ButtonStyle.secondary)
        self.session = session

    async def callback(self, interaction: discord.Interaction) -> None:
        await _switch(interaction, self.session, SCREEN_MORE)


class ExamplesButton(discord.ui.Button["ResultView"]):
    def __init__(self, session: DictionarySession) -> None:
        super().__init__(label="Examples", emoji="📖", style=discord.ButtonStyle.secondary)
        self.session = session

    async def callback(self, interaction: discord.Interaction) -> None:
        await _switch(interaction, self.session, SCREEN_EXAMPLES)


class BackButton(discord.ui.Button["ResultView"]):
    def __init__(self, session: DictionarySession) -> None:
        super().__init__(label="Back", emoji="⬅️", style=discord.ButtonStyle.secondary)
        self.session = session

    async def callback(self, interaction: discord.Interaction) -> None:
        await _switch(interaction, self.session, SCREEN_CARD)


async def _switch(
    interaction: discord.Interaction, session: DictionarySession, screen: str
) -> None:
    """Change screens once, even if someone mashes the button."""
    if not await _guard(interaction, session):
        return
    if session.screen_lock.locked():
        await _safe_respond(interaction, "one moment…")
        return
    async with session.screen_lock:
        if screen in (SCREEN_MORE, SCREEN_EXAMPLES):
            try:
                await session.load_details()
            except Exception:  # noqa: BLE001 - extras are optional
                log.debug("Could not load extra detail", exc_info=True)
                session.details = WordDetails()
        session.screen = screen
        view = ResultView(session, screen=screen)
        try:
            await interaction.response.edit_message(
                view=view, attachments=session.attachments()
            )
        except discord.NotFound:
            log.debug("Dictionary card expired before the screen could change")
        except discord.HTTPException:
            log.debug("Could not switch dictionary screen", exc_info=True)


class ResultView(discord.ui.LayoutView):
    """The card, plus whichever extra panel is open."""

    def __init__(self, session: DictionarySession, *, screen: str = SCREEN_CARD) -> None:
        super().__init__(timeout=VIEW_TIMEOUT)
        self.session = session
        container = discord.ui.Container(accent_colour=Colour.ACCENT)

        if session.card:
            gallery = discord.ui.MediaGallery()
            gallery.add_item(
                media=f"attachment://{CARD_FILENAME}",
                description=f"Dictionary card for {session.entry.word}",
            )
            container.add_item(gallery)
        else:
            container.add_item(discord.ui.TextDisplay(card_fallback_text(session.entry)))

        notice = _result_notice(session.result)
        if notice:
            container.add_item(discord.ui.TextDisplay(notice))

        if screen != SCREEN_CARD:
            details = session.details or WordDetails()
            body = (
                more_text(session.entry, details)
                if screen == SCREEN_MORE
                else examples_text(session.entry, details)
            )
            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay(body))

        container.add_item(CardButtons(session, screen=screen))
        self.add_item(container)

    async def on_timeout(self) -> None:
        # Grey the buttons out rather than leaving dead controls behind.
        for item in self.walk_children():
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        editor = self.session.editor
        if editor is not None:
            await editor.edit(view=self, attachments=self.session.attachments())


def _result_notice(result: LookupResult) -> str:
    """The small honest footnotes under a card, when there are any."""
    bits: list[str] = []
    if result.resolved_from and result.entry is not None:
        shown = discord.utils.escape_markdown(result.entry.word)
        asked = discord.utils.escape_markdown(result.resolved_from)
        if result.auto_corrected:
            bits.append(f"-# Showing **{shown}** — corrected from “{asked}”.")
        else:
            bits.append(f"-# Showing **{shown}** — the base form of “{asked}”.")
    if result.partial:
        bits.append("-# Some sources were unavailable, so this entry is a little thin.")
    return "\n".join(bits)


class SuggestionButton(discord.ui.Button["NotFoundView"]):
    """A regular alternate word — it starts the normal fuzzy-aware pipeline."""

    def __init__(self, cog: "Dictionary", word: str, invoker_id: int) -> None:
        super().__init__(label=shorten(word, 24), style=discord.ButtonStyle.secondary)
        self.cog = cog
        self.word = word
        self.invoker_id = invoker_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.invoker_id:
            await _safe_respond(
                interaction, "run `/dictionary` to start your own lookup."
            )
            return
        try:
            await interaction.response.edit_message(
                view=LoadingView(self.word), attachments=[]
            )
        except discord.NotFound:
            return
        except discord.HTTPException:
            log.debug("Could not show loading state for a suggestion", exc_info=True)
            return
        await self.cog.deliver(
            self.word,
            Editor(interaction=interaction),
            invoker_id=self.invoker_id,
        )


class CorrectedSearchButton(SuggestionButton):
    """The affirmative side of a deliberately non-destructive correction."""

    def __init__(self, cog: "Dictionary", word: str, invoker_id: int) -> None:
        super().__init__(cog, word, invoker_id)
        self.label = shorten(f"Yes, search {word}", 80)
        self.style = discord.ButtonStyle.primary


class ExactSearchButton(discord.ui.Button["NotFoundView"]):
    """Let the asker opt out of fuzzy matching without a correction loop."""

    def __init__(self, cog: "Dictionary", query: str, invoker_id: int) -> None:
        super().__init__(
            label="Search exactly what I typed",
            style=discord.ButtonStyle.secondary,
        )
        self.cog = cog
        self.query = query
        self.invoker_id = invoker_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.invoker_id:
            await _safe_respond(
                interaction, "run `/dictionary` to start your own lookup."
            )
            return
        try:
            await interaction.response.edit_message(
                view=LoadingView(self.query), attachments=[]
            )
        except discord.NotFound:
            return
        except discord.HTTPException:
            log.debug("Could not show exact-search loading state", exc_info=True)
            return
        await self.cog.deliver(
            self.query,
            Editor(interaction=interaction),
            invoker_id=self.invoker_id,
            fuzzy=False,
        )


class NotFoundView(discord.ui.LayoutView):
    def __init__(
        self,
        cog: "Dictionary",
        query: str,
        suggestions: tuple[str, ...],
        invoker_id: int,
        *,
        suggested_word: str = "",
    ) -> None:
        super().__init__(timeout=VIEW_TIMEOUT)
        container = discord.ui.Container(accent_colour=Colour.ACCENT)
        container.add_item(
            discord.ui.TextDisplay(not_found_text(query, suggestions, suggested_word))
        )
        if suggested_word:
            confirmation = discord.ui.ActionRow()
            confirmation.add_item(CorrectedSearchButton(cog, suggested_word, invoker_id))
            confirmation.add_item(ExactSearchButton(cog, query, invoker_id))
            container.add_item(confirmation)

            # Keep genuinely different alternatives available without making
            # the focused two-button decision look like a noisy menu.
            alternatives = tuple(word for word in suggestions if word != suggested_word)
            if alternatives:
                row = discord.ui.ActionRow()
                for word in alternatives[:3]:
                    row.add_item(SuggestionButton(cog, word, invoker_id))
                container.add_item(row)
        elif suggestions:
            row = discord.ui.ActionRow()
            for word in suggestions[:4]:
                row.add_item(SuggestionButton(cog, word, invoker_id))
            container.add_item(row)
        self.add_item(container)


async def _followup(interaction: discord.Interaction, content: str, **kwargs) -> None:
    try:
        await interaction.followup.send(content, ephemeral=True, **kwargs)
    except discord.NotFound:
        log.debug("Interaction expired before the follow-up was sent")
    except discord.HTTPException:
        log.debug("Could not send follow-up", exc_info=True)


# ---------------------------------------------------------------------------
# The cog
# ---------------------------------------------------------------------------


class Dictionary(commands.Cog):
    """Chat triggers, the mention path and the card lifecycle."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.settings = getattr(bot.config, "dictionary", None)
        self.feature = DictionaryFeature(
            self.settings, elevenlabs=getattr(bot.config, "elevenlabs", None)
        )
        self._user_last: dict[int, float] = {}
        self._channel_last: dict[int, float] = {}
        log.info("Dictionary ready — %s", self.feature.describe())

    async def cog_unload(self) -> None:
        try:
            await self.feature.close()
        except Exception:  # noqa: BLE001 - shutdown is best effort
            log.debug("Dictionary shutdown failed", exc_info=True)

    # -- no public slash commands -----------------------------------------
    #
    # The dictionary has no registered slash command any more. Every path
    # into it is conversational:
    #
    #   @Paradise define ephemeral      @Paradise what does ephemeral mean
    #   "define ephemeral" in chat      (text trigger, see handle_message)
    #
    # The card, pronunciation and lookup pipeline below are unchanged and
    # are shared by both the text triggers and the mention assistant.

    # -- plain-chat triggers ----------------------------------------------

    async def handle_message(self, message: discord.Message) -> bool:
        """True when this message was a dictionary request and was answered."""
        if self.settings is not None and not self.settings.text_triggers:
            return False
        if message.author.bot or message.guild is None:
            return False
        if self.bot.user is not None and any(
            user.id == self.bot.user.id for user in message.mentions
        ):
            return False  # talking to Paradise is the AI's job, not the dictionary's
        term = match_text_request(message.content)
        if term is None:
            return False
        if not self._trigger_allowed(message):
            return False

        channel = message.channel
        permissions = channel.permissions_for(message.guild.me)
        if not permissions.send_messages:
            return False

        try:
            placeholder = await message.reply(
                view=LoadingView(term),
                mention_author=False,
            )
        except discord.HTTPException:
            log.debug("Could not post a dictionary loading state in chat", exc_info=True)
            return False

        await self.deliver(term, Editor(message=placeholder), invoker_id=message.author.id)
        return True

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

    # -- the shared pipeline ----------------------------------------------

    async def deliver(
        self,
        term: str,
        editor: Editor,
        *,
        invoker_id: int,
        fuzzy: bool = True,
    ) -> None:
        """Look a word up and replace the loading state with the answer."""
        try:
            result = await self.feature.lookup(term, fuzzy=fuzzy)
        except ParadiseError as error:
            await editor.edit(view=NoticeView(f"## 🔎 {error}"), attachments=[])
            return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad lookup must not spread
            log.exception("Dictionary lookup failed for %r", term)
            await editor.edit(view=NoticeView(f"## 🔎 {GENERIC_ERROR}"), attachments=[])
            return

        if not result.found or result.entry is None:
            view = NotFoundView(
                self,
                term,
                result.suggestions,
                invoker_id,
                suggested_word=result.suggested_word,
            )
            await editor.edit(view=view, attachments=[])
            return

        card = await self.feature.card(result.entry)
        session = DictionarySession(self.feature, result, invoker_id, card)
        session.editor = editor
        view = ResultView(session, screen=SCREEN_CARD)
        await editor.edit(view=view, attachments=session.attachments())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Dictionary(bot))
