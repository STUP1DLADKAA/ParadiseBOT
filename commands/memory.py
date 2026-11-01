"""``/memory`` — teach Paradise things it should remember.

The command belongs to the lord alone (``PARADISE_LORD_USER_ID``, default
922359954025885737): he adds facts, preferences and server lore, and the AI
folds them into its answers from then on. For everyone else the command
politely does not exist — a non-lord never learns what is stored, they just
get told the memories are not theirs to touch.

The store itself lives on the bot (``bot.memory``, a
:class:`functions.memory.MemoryStore`) so the AI service can read it without
knowing anything about this cog.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from functions.helpers import shorten
from functions.memory import MAX_MEMORY_CHARS, MemoryStore

log = logging.getLogger(__name__)

#: What a non-lord sees. Short, final, and — unlike /edit — honest about why.
DENIED_LINE: str = "these memories aren't yours to touch. only my lord decides what i remember."

MAX_LIST_CHARS = 1900  # keep the list inside one Discord message


class Memory(commands.GroupCog, name="memory"):
    """The lord's memory: add, list, remove and clear remembered facts."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # -- permissions ------------------------------------------------------

    def _lord_id(self) -> int:
        config = getattr(self.bot, "config", None)
        return int(getattr(config, "lord_user_id", 0) or 0)

    def _store(self) -> MemoryStore | None:
        return getattr(self.bot, "memory", None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        lord_id = self._lord_id()
        if not lord_id or interaction.user.id != lord_id:
            await interaction.response.send_message(DENIED_LINE, ephemeral=True)
            return False
        return True

    # -- subcommands ------------------------------------------------------

    @app_commands.command(
        name="add",
        description="Teach Paradise a fact, preference or piece of server lore",
    )
    @app_commands.describe(
        text="What to remember (a name, a rule, an inside joke, anything)"
    )
    async def add(
        self,
        interaction: discord.Interaction,
        text: app_commands.Range[str, 3, MAX_MEMORY_CHARS],
    ) -> None:
        store = self._store()
        if store is None:
            await interaction.response.send_message(
                "my memory isn't wired up right now. blame the owner, not me.",
                ephemeral=True,
            )
            return
        entry = store.add(text, author_id=interaction.user.id)
        if entry is None:
            await interaction.response.send_message(
                "that's not something i can remember, my lord.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"noted, my lord — memory #{entry.id}: *{entry.text}*",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(
        name="list", description="Everything Paradise currently remembers"
    )
    async def list_memories(self, interaction: discord.Interaction) -> None:
        store = self._store()
        entries = store.entries() if store is not None else []
        if not entries:
            await interaction.response.send_message(
                "you haven't taught me anything yet, my lord. use /memory add.",
                ephemeral=True,
            )
            return
        lines: list[str] = []
        for entry in entries:
            line = f"`#{entry.id}` {entry.text}"
            lines.append(shorten(line, 100))
        body = shorten("\n".join(lines), MAX_LIST_CHARS)
        await interaction.response.send_message(
            f"i remember {len(entries)} thing(s), my lord:\n{body}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(
        name="remove", description="Make Paradise forget one memory by its id"
    )
    @app_commands.describe(id="The memory number from /memory list")
    async def remove(
        self,
        interaction: discord.Interaction,
        id: app_commands.Range[int, 1],  # noqa: A002 - the /memory id
    ) -> None:
        store = self._store()
        if store is None or not store.remove(int(id)):
            await interaction.response.send_message(
                f"i don't remember anything under #{id}, my lord. /memory list has the ids.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"forgotten. memory #{id} is gone, my lord.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(
        name="clear", description="Wipe every memory Paradise has (no undo)"
    )
    async def clear(self, interaction: discord.Interaction) -> None:
        store = self._store()
        dropped = store.clear() if store is not None else 0
        await interaction.response.send_message(
            f"clean slate, my lord. {dropped} memory(ies) erased.",
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Memory(bot))
