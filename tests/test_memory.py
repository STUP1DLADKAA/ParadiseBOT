"""Unit tests for the lord's long-term memory (functions/memory.py)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from functions.memory import (
    MAX_MEMORY_CHARS,
    MemoryEntry,
    MemoryStore,
    clean_memory_text,
)

LORD_ID = 922359954025885737


def make_store(**kwargs) -> MemoryStore:
    tmp = tempfile.TemporaryDirectory()
    store = MemoryStore(Path(tmp.name) / "memory.db", **kwargs)
    store._tmp = tmp  # keep the directory alive with the store
    return store


class TestCleanMemoryText(unittest.TestCase):
    def test_mentions_and_markdown_are_stripped(self):
        cleaned = clean_memory_text("<@123> loves <@!456> @everyone **very much**")
        self.assertEqual(cleaned, "loves very much")
        self.assertNotIn("<@", cleaned)
        self.assertNotIn("@everyone", cleaned)

    def test_whitespace_is_collapsed_and_capped(self):
        cleaned = clean_memory_text("  a   \n\t b  " + "x" * 500)
        self.assertTrue(cleaned.startswith("a b "))
        self.assertLessEqual(len(cleaned), MAX_MEMORY_CHARS)

    def test_garbage_becomes_empty(self):
        self.assertEqual(clean_memory_text(""), "")
        self.assertEqual(clean_memory_text(None), "")
        self.assertEqual(clean_memory_text("   <@1>  "), "")


class TestMemoryStore(unittest.TestCase):
    def setUp(self):
        self.store = make_store()

    def tearDown(self):
        self.store.close()
        if hasattr(self.store, "_tmp"):
            self.store._tmp.cleanup()

    def test_add_list_remove_clear_roundtrip(self):
        self.assertEqual(self.store.entries(), [])
        first = self.store.add("Aryan only plays Valorant", author_id=LORD_ID)
        second = self.store.add("Rohan sleeps at 4am", author_id=LORD_ID)
        self.assertIsInstance(first, MemoryEntry)
        self.assertGreater(first.id, 0)

        entries = self.store.entries()
        self.assertEqual(len(entries), 2)
        # newest first
        self.assertEqual(entries[0].text, "Rohan sleeps at 4am")
        self.assertEqual(entries[1].text, "Aryan only plays Valorant")
        self.assertEqual(entries[0].author_id, LORD_ID)

        self.assertTrue(self.store.remove(second.id))
        self.assertFalse(self.store.remove(second.id))
        self.assertEqual(len(self.store.entries()), 1)

        dropped = self.store.clear()
        self.assertEqual(dropped, 1)
        self.assertEqual(self.store.entries(), [])
        self.assertEqual(self.store.clear(), 0)

    def test_useless_text_is_rejected(self):
        self.assertIsNone(self.store.add("   "))
        self.assertIsNone(self.store.add("<@123>"))
        self.assertEqual(len(self.store), 0)

    def test_cap_evicts_oldest_first(self):
        store = make_store(max_entries=3)
        try:
            for index in range(5):
                store.add(f"fact number {index}", author_id=LORD_ID)
            texts = [entry.text for entry in store.entries()]
            self.assertEqual(len(texts), 3)
            self.assertEqual(
                texts, ["fact number 4", "fact number 3", "fact number 2"]
            )
        finally:
            store.close()
            store._tmp.cleanup()

    def test_relevant_scores_by_word_overlap(self):
        self.store.add("Aryan only plays Valorant", author_id=LORD_ID)
        self.store.add("Rohan sleeps at 4am", author_id=LORD_ID)
        self.store.add("the server was founded in 2023", author_id=LORD_ID)

        hits = self.store.relevant("who plays valorant in this server?")
        self.assertTrue(hits)
        self.assertEqual(hits[0], "Aryan only plays Valorant")

        hits = self.store.relevant("what does rohan do at night?")
        self.assertEqual(hits[0], "Rohan sleeps at 4am")

    def test_relevant_falls_back_to_recent_when_no_overlap(self):
        self.store.add("Aryan only plays Valorant", author_id=LORD_ID)
        self.store.add("Rohan sleeps at 4am", author_id=LORD_ID)
        # Nothing overlaps with "explain quantum tunnelling" -> recent memories
        hits = self.store.relevant("explain quantum tunnelling")
        self.assertEqual(len(hits), 2)

    def test_relevant_on_empty_store_and_stopwords(self):
        self.assertEqual(self.store.relevant("anything"), [])
        self.store.add("Rohan sleeps at 4am", author_id=LORD_ID)
        # A question of only stop words still returns the recent memories.
        self.assertEqual(len(self.store.relevant("the of and")), 1)

    def test_relevant_limit_is_respected(self):
        for index in range(10):
            self.store.add(f"valorant fact {index}", author_id=LORD_ID)
        self.assertEqual(len(self.store.relevant("valorant", limit=4)), 4)


class TestPersistence(unittest.TestCase):
    def test_memories_survive_reconnects(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            path = Path(tmp.name) / "memory.db"
            store = MemoryStore(path)
            entry = store.add("the founding father built this place", author_id=LORD_ID)
            store.close()

            reopened = MemoryStore(path)
            entries = reopened.entries()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].id, entry.id)
            self.assertEqual(entries[0].text, "the founding father built this place")
            reopened.close()
        finally:
            tmp.cleanup()


def make_interaction(user_id: int):
    from unittest.mock import AsyncMock

    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, display_name=f"User{user_id}", bot=False),
        response=SimpleNamespace(send_message=AsyncMock()),
    )


class TestMemoryCommand(unittest.IsolatedAsyncioTestCase):
    """commands/memory.py: the command is the lord's, and only his."""

    LORD_ID = LORD_ID
    OTHER_ID = 111222333

    def _cog(self, *, lord_id: int = LORD_ID):
        from commands.memory import Memory

        tmp = tempfile.TemporaryDirectory()
        bot = SimpleNamespace(
            config=SimpleNamespace(lord_user_id=lord_id),
            memory=MemoryStore(Path(tmp.name) / "memory.db"),
        )
        cog = Memory(bot)
        cog._tmp = tmp
        return cog

    def tearDown(self):
        pass

    async def test_only_the_lord_passes_the_interaction_check(self):
        cog = self._cog()
        denied = make_interaction(self.OTHER_ID)
        self.assertFalse(await cog.interaction_check(denied))
        denied.response.send_message.assert_awaited_once()

        allowed = make_interaction(self.LORD_ID)
        self.assertTrue(await cog.interaction_check(allowed))
        allowed.response.send_message.assert_not_awaited()

    async def test_add_lists_removes_and_clears(self):
        cog = self._cog()
        store = cog.bot.memory

        interaction = make_interaction(self.LORD_ID)
        await cog.add.callback(cog, interaction, "Aryan only plays Valorant")
        self.assertTrue(interaction.response.send_message.await_count >= 1)
        self.assertEqual(len(store.entries()), 1)

        interaction = make_interaction(self.LORD_ID)
        await cog.list_memories.callback(cog, interaction)
        listed = interaction.response.send_message.call_args.args[0]
        self.assertIn("Aryan only plays Valorant", listed)
        self.assertIn("1 thing", listed)

        entry = store.entries()[0]
        interaction = make_interaction(self.LORD_ID)
        await cog.remove.callback(cog, interaction, entry.id)
        removed = interaction.response.send_message.call_args.args[0]
        self.assertIn("forgotten", removed)
        self.assertEqual(store.entries(), [])

        store.add("temporary fact", author_id=self.LORD_ID)
        interaction = make_interaction(self.LORD_ID)
        await cog.clear.callback(cog, interaction)
        self.assertIn("1 memory", interaction.response.send_message.call_args.args[0])
        self.assertEqual(store.entries(), [])

    async def test_adding_garbage_is_rejected_politely(self):
        cog = self._cog()
        interaction = make_interaction(self.LORD_ID)
        await cog.add.callback(cog, interaction, "   <@123>   ")
        self.assertIn(
            "not something i can remember",
            interaction.response.send_message.call_args.args[0],
        )

    async def test_removing_an_unknown_id_fails_gently(self):
        cog = self._cog()
        interaction = make_interaction(self.LORD_ID)
        await cog.remove.callback(cog, interaction, 999)
        self.assertIn(
            "don't remember anything under #999",
            interaction.response.send_message.call_args.args[0],
        )

    async def test_a_missing_store_is_handled(self):
        from commands.memory import Memory

        bot = SimpleNamespace(config=SimpleNamespace(lord_user_id=LORD_ID))
        cog = Memory(bot)
        interaction = make_interaction(self.LORD_ID)
        await cog.add.callback(cog, interaction, "a fact")
        self.assertIn(
            "isn't wired up", interaction.response.send_message.call_args.args[0]
        )

    async def test_an_empty_list_gets_a_hint(self):
        cog = self._cog()
        interaction = make_interaction(self.LORD_ID)
        await cog.list_memories.callback(cog, interaction)
        self.assertIn(
            "haven't taught me anything",
            interaction.response.send_message.call_args.args[0],
        )


if __name__ == "__main__":
    unittest.main()
