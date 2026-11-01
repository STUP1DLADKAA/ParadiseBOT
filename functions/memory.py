"""Long-term memory for Paradise — the things its lord asks it to remember.

A tiny SQLite-backed store (one table, a handful of rows, a hard cap). The
lord — and only the lord — manages it through ``/memory`` (commands/memory.py);
the AI service folds whatever is stored into its answers
(:func:`functions.ai.AIService._memory_block`), which is what makes replies
feel like they come from someone who has actually lived in the server.

Design notes:

* **Synchronous sqlite3 with a lock**, exactly like the voice tracker's
  :class:`functions.voice.database.VoiceDatabase` — every call is short, the
  dataset is tiny, and this keeps the whole thing dependency-free.
* **Relevance by word overlap**, not embeddings: memories are short facts
  ("Aryan only plays Valorant"), and a stop-word-filtered word match plus a
  recency fallback is plenty for a 200-row cap.
* **Never raises** from the read path — a memory problem must never take the
  AI down with it.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

#: Hard ceiling on a single memory's text. Kept small on purpose: these are
#: facts, not essays, and the AI block has to stay cheap to include.
MAX_MEMORY_CHARS = 300

#: Words too common to be relevant when scoring a memory against a question.
_STOP_WORDS = frozenset(
    """a an and are as at be been but by can did do does for from had has have
    he her hers him his how i if in into is it its just like me my no not of
    on or our out she so some than that the their them then there these they
    this to too us was we were what when where which who why will with you
    your""".split()
)

_WORD_RE = re.compile(r"[a-z0-9']+")

__all__ = ["MAX_MEMORY_CHARS", "MemoryEntry", "MemoryStore", "clean_memory_text"]


def clean_memory_text(text: str | None) -> str:
    """Sanitise a raw memory into something safe to store and replay.

    Mentions (``<@…>``, ``@everyone``) are stripped so a memory can never ping
    anyone or blow up a mention; control characters and markdown go too.
    """
    cleaned = re.sub(r"<@[!&]?\d+>", "", str(text or ""))
    cleaned = cleaned.replace("@everyone", "").replace("@here", "")
    cleaned = re.sub(r"[*_`~|>\\]", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:MAX_MEMORY_CHARS]


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    """One remembered fact."""

    id: int
    text: str
    author_id: int
    created_at: datetime


class MemoryStore:
    """A small, capped, SQLite-backed list of things to remember."""

    def __init__(self, db_path: str | Path = "data/memory.db", *, max_entries: int = 200) -> None:
        self.db_path = Path(db_path)
        self.max_entries = max(1, int(max_entries))
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    # -- plumbing --------------------------------------------------------

    def _connection(self) -> sqlite3.Connection:
        """Open (once) and return the connection, creating the table."""
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    author_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            self._conn.commit()
        return self._conn

    def close(self) -> None:
        """Close the connection (safe to call more than once)."""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:  # pragma: no cover - closing never matters
                    pass
                self._conn = None

    @staticmethod
    def _row_to_entry(row: sqlite3.Row | tuple) -> MemoryEntry:
        return MemoryEntry(
            id=int(row[0]),
            text=str(row[1]),
            author_id=int(row[2]),
            created_at=datetime.fromisoformat(str(row[3])),
        )

    @classmethod
    def _ensure_utc(cls, created_at: datetime) -> str:
        stamp = created_at
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(timezone.utc).isoformat()

    # -- writes ----------------------------------------------------------

    def add(self, text: str, author_id: int = 0) -> MemoryEntry | None:
        """Store one memory. Returns it, or ``None`` if nothing usable was left."""
        cleaned = clean_memory_text(text)
        if not cleaned:
            return None
        now = datetime.now(timezone.utc)
        with self._lock:
            conn = self._connection()
            try:
                cursor = conn.execute(
                    "INSERT INTO memories (text, author_id, created_at) VALUES (?, ?, ?)",
                    (cleaned, int(author_id), self._ensure_utc(now)),
                )
                # Enforce the cap: the oldest rows go first.
                overflow = conn.execute(
                    "SELECT COUNT(*) FROM memories"
                ).fetchone()[0] - self.max_entries
                if overflow > 0:
                    conn.execute(
                        "DELETE FROM memories WHERE id IN "
                        "(SELECT id FROM memories ORDER BY id LIMIT ?)",
                        (overflow,),
                    )
                conn.commit()
            except sqlite3.Error:
                log.warning("Could not store a memory", exc_info=True)
                conn.rollback()
                return None
        return MemoryEntry(
            id=int(cursor.lastrowid or 0),
            text=cleaned,
            author_id=int(author_id),
            created_at=now,
        )

    def remove(self, memory_id: int) -> bool:
        """Forget the memory with this id. Returns True when a row went away."""
        with self._lock:
            conn = self._connection()
            try:
                cursor = conn.execute(
                    "DELETE FROM memories WHERE id = ?", (int(memory_id),)
                )
                conn.commit()
            except sqlite3.Error:
                log.warning("Could not remove memory %s", memory_id, exc_info=True)
                conn.rollback()
                return False
            return cursor.rowcount > 0

    def clear(self) -> int:
        """Forget everything. Returns how many memories were dropped."""
        with self._lock:
            conn = self._connection()
            try:
                count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
                conn.execute("DELETE FROM memories")
                conn.commit()
            except sqlite3.Error:
                log.warning("Could not clear memories", exc_info=True)
                conn.rollback()
                return 0
            return int(count)

    # -- reads -----------------------------------------------------------

    def entries(self, *, limit: int | None = None) -> list[MemoryEntry]:
        """All memories, newest first."""
        with self._lock:
            try:
                conn = self._connection()
                rows = conn.execute(
                    "SELECT id, text, author_id, created_at FROM memories "
                    "ORDER BY id DESC LIMIT ?",
                    (self.max_entries if limit is None else max(0, int(limit)),),
                ).fetchall()
            except sqlite3.Error:
                log.warning("Could not read memories", exc_info=True)
                return []
        return [self._row_to_entry(row) for row in rows]

    def relevant(self, question: str, *, limit: int = 6) -> list[str]:
        """The memories most related to ``question``, best first.

        Scoring is word overlap with the question's keywords; ties keep the
        more recent memory. When nothing overlaps, the most recent memories
        are returned instead — they are context even when they do not match,
        and a question like "what do you remember about us" deserves an
        answer too. Never raises.
        """
        wanted = min(10, max(1, int(limit)))
        entries = self.entries()
        if not entries:
            return []
        keywords = {
            word
            for word in _WORD_RE.findall(str(question or "").lower())
            if word not in _STOP_WORDS and len(word) > 1
        }
        if not keywords:
            return [entry.text for entry in entries[:wanted]]

        def score(entry: MemoryEntry) -> int:
            words = set(_WORD_RE.findall(entry.text.lower()))
            return len(keywords & words)

        ranked = sorted(
            ((score(entry), index, entry) for index, entry in enumerate(entries)),
            key=lambda item: (-item[0], item[1]),
        )
        hits = [entry.text for points, _, entry in ranked if points > 0]
        if len(hits) < wanted:
            chosen_ids = {text for text in hits}
            for _, _, entry in ranked:
                if entry.text not in chosen_ids:
                    hits.append(entry.text)
                    chosen_ids.add(entry.text)
                if len(hits) >= wanted:
                    break
        return hits[:wanted]

    def __len__(self) -> int:
        with self._lock:
            try:
                return int(
                    self._connection()
                    .execute("SELECT COUNT(*) FROM memories")
                    .fetchone()[0]
                )
            except sqlite3.Error:
                return 0
