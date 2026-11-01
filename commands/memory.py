"""``/memory`` — teach Paradise things it should remember.

The command belongs to the lords alone (``PARADISE_LORD_USER_IDS``, default
``922359954025885737`` and ``1049231767585964032`` — Stupid and Taher, equal
in authority): they add facts, preferences and server lore, and the AI
folds them into its answers from then on. For everyone else the command
politely does not exist — a non-lord never learns what is stored, they just
get told the memories are not theirs to touch.

A memory can be **about a member**: ``/memory add text: spent a year reaching
level 100 in murder mystery user:@Rohan`` (or just put the mention in the
text). Memories about a member surface whenever that member talks to Paradise,
which is what makes the bot reference them at exactly the right moment.

The store itself lives on the bot (``bot.memory``, a
:class:`functions.memory.MemoryStore`) so the AI service can read it without
knowing anything about this cog; ``bot.memory_mirror`` (a
:class:`functions.memory.MemoryMirror`) copies everything to Supabase when it
is configured, and mutations nudge it to sync soon rather than waiting for its
next scheduled pass.
"""

from __future__ import annotations

import asyncio
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from config import lord_ids_of
from functions.helpers import shorten
from functions.memory import MAX_MEMORY_CHARS, MemoryStore

log = logging.getLogger(__name__)

#: What a non-lord sees. Short, final, and — unlike /edit — honest about why.
DENIED_LINE: str = "these memories aren't yours to touch. only my lord decides what i remember."

MAX_LIST_CHARS = 1900  # keep the list inside one Discord message


def _clean_subject_name(user) -> str:
    name = (
        getattr(user, "display_name", None)
        or getattr(user, "global_name", None)
        or getattr(user, "name", None)
        or ""
    )
    return str(name).strip()[:32]


class Memory(commands.GroupCog, name="memory"):
    """The lords' memory: add, list, remove and clear remembered facts."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._mirror_tasks: set[asyncio.Task] = set()

    # -- permissions ------------------------------------------------------

    def _lord_ids(self) -> tuple[int, ...]:
        config = getattr(self.bot, "config", None)
        return lord_ids_of(config)

    def _store(self) -> MemoryStore | None:
        return getattr(self.bot, "memory", None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in self._lord_ids():
            await interaction.response.send_message(DENIED_LINE, ephemeral=True)
            return False
        return True

    # -- mirror nudge -----------------------------------------------------

    def _kick_mirror(self) -> None:
        """Ask the Supabase mirror to sync soon (never blocks the reply)."""
        mirror = getattr(self.bot, "memory_mirror", None)
        if mirror is None:
            return
        try:
            task = asyncio.create_task(
                mirror.reconcile(), name="paradise-memory-nudge"
            )
        except RuntimeError:  # no running loop (offline tests)
            return
        self._mirror_tasks.add(task)
        task.add_done_callback(self._mirror_tasks.discard)

    # -- subcommands ------------------------------------------------------

    @app_commands.command(
        name="add",
        description="Teach Paradise a fact — optionally about a specific member",
    )
    @app_commands.describe(
        text="What to remember. Put @user in it (or use the user option) to "
        "make it about that member.",
        user="Who this memory is about (optional)",
    )
    async def add(
        self,
        interaction: discord.Interaction,
        text: app_commands.Range[str, 3, MAX_MEMORY_CHARS],
        user: discord.User | None = None,
    ) -> None:
        store = self._store()
        if store is None:
            await interaction.response.send_message(
                "my memory isn't wired up right now. blame the owner, not me.",
                ephemeral=True,
            )
            return

        subject_id = int(getattr(user, "id", 0) or 0)
        subject_name = _clean_subject_name(user) if user is not None else ""
        # A mention inside the text also marks the subject (the first one
        # wins; the mention itself is stripped by the store's sanitiser).
        if not subject_id:
            match = re.search(r"<@!?(\d+)>", str(text))
            if match is not None:
                subject_id = int(match.group(1))
                resolved = None
                guild = getattr(interaction, "guild", None)
                if guild is not None and getattr(guild, "get_member", None):
                    resolved = guild.get_member(subject_id)
                if resolved is None:
                    get_user = getattr(self.bot, "get_user", None)
                    resolved = get_user(subject_id) if callable(get_user) else None
                subject_name = _clean_subject_name(resolved) if resolved is not None else ""

        entry = store.add(
            text,
            author_id=interaction.user.id,
            subject_id=subject_id,
            subject_name=subject_name,
        )
        if entry is None:
            await interaction.response.send_message(
                "that's not something i can remember, my lord.", ephemeral=True
            )
            return
        about = f" about **{entry.subject_name}**" if entry.subject_name else ""
        await interaction.response.send_message(
            f"noted, my lord — memory #{entry.id}{about}: *{entry.text}*",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self._kick_mirror()

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
            prefix = f"[about {entry.subject_name}] " if entry.subject_name else ""
            lines.append(shorten(f"`#{entry.id}` {prefix}{entry.text}", 100))
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
        self._kick_mirror()

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
        self._kick_mirror()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Memory(bot))
