"""Long-term memory for Paradise — the things its lord asks it to remember.

A tiny store of facts the bot keeps **permanently**: SQLite is the working
copy (one table, a handful of rows, a hard cap), and when Supabase is
configured the rows are mirrored there too (see :class:`MemoryMirror` and
``supabase/migrations/005_memory.sql``), so the memories survive even a
reinstall or a move to another machine.

Every memory is either **server-wide** or **about a specific member**
(``subject_id`` + a display-name snapshot). That distinction is the whole
point: when that member talks to Paradise, their memories are handed to the
AI so the reply can reference them *at the right moment* — a joke about the
year it took them to hit level 100, exactly when they show up again.

Design notes:

* **Synchronous sqlite3 with a lock**, exactly like the voice tracker's
  :class:`functions.voice.database.VoiceDatabase` — every call is short, the
  dataset is tiny, and this keeps the whole thing dependency-free.
* **Relevance by word overlap + subject identity**, not embeddings: memories
  are short facts ("Aryan only plays Valorant"), and a stop-word-filtered
  word match plus "who is talking" is plenty for a 200-row cap.
* **Never raises** from any read path — a memory problem must never take the
  AI down with it.
* **Mirror bookkeeping** is plain columns: ``synced_at`` marks rows the
  mirror has pushed, and ``memory_tombstones`` remembers deletions until the
  mirror has deleted them remotely.
"""

from __future__ import annotations

import asyncio
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


def _ensure_utc(created_at: datetime) -> str:
    """ISO-8601 UTC timestamp for storage (naive datetimes assumed UTC)."""
    stamp = created_at
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc).isoformat()

__all__ = [
    "MAX_MEMORY_CHARS",
    "MemoryEntry",
    "MemoryMirror",
    "MemoryStore",
    "clean_memory_text",
]


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
    #: Who this memory is ABOUT (0 = server-wide, not about one member).
    subject_id: int = 0
    #: Display-name snapshot of the subject at the time it was stored.
    subject_name: str = ""


class MemoryStore:
    """A small, capped, SQLite-backed list of things to remember."""

    def __init__(self, db_path: str | Path = "data/memory.db", *, max_entries: int = 200) -> None:
        self.db_path = Path(db_path)
        self.max_entries = max(1, int(max_entries))
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    # -- plumbing --------------------------------------------------------

    def _connection(self) -> sqlite3.Connection:
        """Open (once) and return the connection, creating/upgrading the table."""
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
                    created_at TEXT NOT NULL,
                    subject_id INTEGER NOT NULL DEFAULT 0,
                    subject_name TEXT NOT NULL DEFAULT '',
                    synced_at TEXT
                )
                """
            )
            # Upgrade path for databases created by the first release of the
            # feature (which had no subject or sync columns): add whatever is
            # missing. Existing rows keep working as server-wide memories.
            columns = {
                row[1] for row in self._conn.execute("PRAGMA table_info(memories)")
            }
            if "subject_id" not in columns:
                self._conn.execute(
                    "ALTER TABLE memories ADD COLUMN subject_id INTEGER NOT NULL DEFAULT 0"
                )
            if "subject_name" not in columns:
                self._conn.execute(
                    "ALTER TABLE memories ADD COLUMN subject_name TEXT NOT NULL DEFAULT ''"
                )
            if "synced_at" not in columns:
                self._conn.execute("ALTER TABLE memories ADD COLUMN synced_at TEXT")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_tombstones (
                    id INTEGER PRIMARY KEY,
                    deleted_at TEXT NOT NULL
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
        # Rows from an un-migrated database (or a SELECT without the new
        # columns) carry fewer fields; the defaults keep them readable.
        return MemoryEntry(
            id=int(row[0]),
            text=str(row[1]),
            author_id=int(row[2]),
            created_at=datetime.fromisoformat(str(row[3])),
            subject_id=int(row[4]) if len(row) > 4 and row[4] is not None else 0,
            subject_name=str(row[5] or "") if len(row) > 5 else "",
        )

    # -- writes ----------------------------------------------------------

    def add(
        self,
        text: str,
        author_id: int = 0,
        *,
        subject_id: int = 0,
        subject_name: str = "",
    ) -> MemoryEntry | None:
        """Store one memory. Returns it, or ``None`` if nothing usable was left.

        ``subject_id``/``subject_name`` mark who the memory is *about* — those
        memories surface whenever that member talks to Paradise.
        """
        cleaned = clean_memory_text(text)
        if not cleaned:
            return None
        now = datetime.now(timezone.utc)
        with self._lock:
            conn = self._connection()
            try:
                cursor = conn.execute(
                    "INSERT INTO memories "
                    "(text, author_id, created_at, subject_id, subject_name, synced_at) "
                    "VALUES (?, ?, ?, ?, ?, NULL)",
                    (
                        cleaned,
                        int(author_id),
                        _ensure_utc(now),
                        max(0, int(subject_id or 0)),
                        str(subject_name or "")[:32],
                    ),
                )
                new_id = int(cursor.lastrowid or 0)
                # Enforce the cap: the oldest rows go first, and anything
                # evicted is tombstoned so the mirror deletes it remotely too.
                overflow = conn.execute(
                    "SELECT COUNT(*) FROM memories"
                ).fetchone()[0] - self.max_entries
                if overflow > 0:
                    evicted = [
                        int(row[0])
                        for row in conn.execute(
                            "SELECT id FROM memories ORDER BY id LIMIT ?",
                            (overflow,),
                        )
                    ]
                    conn.execute(
                        "DELETE FROM memories WHERE id IN "
                        "(SELECT id FROM memories ORDER BY id LIMIT ?)",
                        (overflow,),
                    )
                    for evicted_id in evicted:
                        conn.execute(
                            "INSERT OR REPLACE INTO memory_tombstones (id, deleted_at) "
                            "VALUES (?, ?)",
                            (evicted_id, _ensure_utc(now)),
                        )
                conn.commit()
            except sqlite3.Error:
                log.warning("Could not store a memory", exc_info=True)
                conn.rollback()
                return None
        return MemoryEntry(
            id=new_id,
            text=cleaned,
            author_id=int(author_id),
            created_at=now,
            subject_id=max(0, int(subject_id or 0)),
            subject_name=str(subject_name or "")[:32],
        )

    def remove(self, memory_id: int) -> bool:
        """Forget the memory with this id. Returns True when a row went away."""
        with self._lock:
            conn = self._connection()
            try:
                cursor = conn.execute(
                    "DELETE FROM memories WHERE id = ?", (int(memory_id),)
                )
                removed = cursor.rowcount > 0
                if removed:
                    conn.execute(
                        "INSERT OR REPLACE INTO memory_tombstones (id, deleted_at) "
                        "VALUES (?, ?)",
                        (int(memory_id), _ensure_utc(datetime.now(timezone.utc))),
                    )
                conn.commit()
            except sqlite3.Error:
                log.warning("Could not remove memory %s", memory_id, exc_info=True)
                conn.rollback()
                return False
            return removed

    def clear(self) -> int:
        """Forget everything. Returns how many memories were dropped."""
        with self._lock:
            conn = self._connection()
            try:
                rows = conn.execute("SELECT id FROM memories").fetchall()
                now = _ensure_utc(datetime.now(timezone.utc))
                for row in rows:
                    conn.execute(
                        "INSERT OR REPLACE INTO memory_tombstones (id, deleted_at) "
                        "VALUES (?, ?)",
                        (int(row[0]), now),
                    )
                conn.execute("DELETE FROM memories")
                conn.commit()
            except sqlite3.Error:
                log.warning("Could not clear memories", exc_info=True)
                conn.rollback()
                return 0
            return len(rows)

    # -- reads -----------------------------------------------------------

    def entries(self, *, limit: int | None = None) -> list[MemoryEntry]:
        """All memories, newest first."""
        with self._lock:
            try:
                conn = self._connection()
                rows = conn.execute(
                    "SELECT id, text, author_id, created_at, subject_id, subject_name "
                    "FROM memories ORDER BY id DESC LIMIT ?",
                    (self.max_entries if limit is None else max(0, int(limit)),),
                ).fetchall()
            except sqlite3.Error:
                log.warning("Could not read memories", exc_info=True)
                return []
        return [self._row_to_entry(row) for row in rows]

    def about(self, subject_id: int) -> list[MemoryEntry]:
        """The memories about one member, newest first. Never raises."""
        subject_id = int(subject_id or 0)
        if not subject_id:
            return []
        return [
            entry
            for entry in self.entries()
            if entry.subject_id == subject_id
        ]

    def relevant(self, question: str, *, limit: int = 6) -> list[MemoryEntry]:
        """The memories most related to ``question``, best first.

        Scoring is word overlap with the question's keywords; ties keep the
        more recent memory. When nothing overlaps, the most recent memories
        are returned instead — they are context even when they do not match,
        and a question like "what do you remember about us" deserves an
        answer too.
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
            return entries[:wanted]

        def score(entry: MemoryEntry) -> int:
            words = set(_WORD_RE.findall(entry.text.lower()))
            return len(keywords & words)

        ranked = sorted(
            ((score(entry), index, entry) for index, entry in enumerate(entries)),
            key=lambda item: (-item[0], item[1]),
        )
        hits = [entry for points, _, entry in ranked if points > 0]
        if len(hits) < wanted:
            chosen_ids = {entry.id for entry in hits}
            for _, _, entry in ranked:
                if entry.id not in chosen_ids:
                    hits.append(entry)
                    chosen_ids.add(entry.id)
                if len(hits) >= wanted:
                    break
        return hits[:wanted]

    def for_prompt(
        self,
        question: str,
        subject_ids: tuple[int, ...] | list[int] = (),
        *,
        limit: int = 6,
    ) -> list[MemoryEntry]:
        """The memories worth handing the AI for one reply.

        Priority: memories **about the people involved** (the asker, anyone
        mentioned — these are the "roast him about the murder mystery year"
        ammunition), then keyword-relevant memories, then the most recent
        ones as general context. Deduplicated and capped.
        """
        wanted = min(10, max(1, int(limit)))
        chosen: list[MemoryEntry] = []
        seen: set[int] = set()

        for subject_id in subject_ids:
            for entry in self.about(subject_id):
                if entry.id not in seen:
                    chosen.append(entry)
                    seen.add(entry.id)

        for entry in self.relevant(question, limit=wanted):
            if entry.id not in seen:
                chosen.append(entry)
                seen.add(entry.id)

        return chosen[:wanted]

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

    # -- mirror bookkeeping (used by MemoryMirror) ------------------------

    def unsynced(self) -> list[MemoryEntry]:
        """Rows the mirror has not pushed yet."""
        with self._lock:
            try:
                rows = self._connection().execute(
                    "SELECT id, text, author_id, created_at, subject_id, subject_name "
                    "FROM memories WHERE synced_at IS NULL ORDER BY id"
                ).fetchall()
            except sqlite3.Error:
                return []
        return [self._row_to_entry(row) for row in rows]

    def synced_ids(self) -> set[int]:
        """Ids of rows already mirrored remotely."""
        with self._lock:
            try:
                return {
                    int(row[0])
                    for row in self._connection()
                    .execute("SELECT id FROM memories WHERE synced_at IS NOT NULL")
                }
            except sqlite3.Error:
                return set()

    def mark_synced(self, ids: list[int] | tuple[int, ...]) -> None:
        if not ids:
            return
        stamp = _ensure_utc(datetime.now(timezone.utc))
        with self._lock:
            try:
                conn = self._connection()
                conn.executemany(
                    "UPDATE memories SET synced_at = ? WHERE id = ?",
                    [(stamp, int(memory_id)) for memory_id in ids],
                )
                conn.commit()
            except sqlite3.Error:
                log.warning("Could not mark memories as synced", exc_info=True)

    def import_remote(self, rows: list[dict]) -> list[int]:
        """Insert rows pulled from Supabase, keeping their remote ids.

        Returns the ids actually inserted. Rows whose id already exists
        locally are ignored — the local row is the one the bot wrote, so it
        wins ties (the push step will refresh the remote copy).
        """
        inserted: list[int] = []
        with self._lock:
            conn = self._connection()
            try:
                for row in rows:
                    memory_id = int(row.get("id") or 0)
                    if not memory_id:
                        continue
                    exists = conn.execute(
                        "SELECT 1 FROM memories WHERE id = ?", (memory_id,)
                    ).fetchone()
                    if exists:
                        continue
                    text = clean_memory_text(row.get("text"))
                    if not text:
                        continue
                    created = str(row.get("created_at") or "")
                    try:
                        created_dt = datetime.fromisoformat(
                            created.replace("Z", "+00:00")
                        )
                        created_iso = _ensure_utc(created_dt)
                    except ValueError:
                        created_iso = _ensure_utc(datetime.now(timezone.utc))
                    conn.execute(
                        "INSERT OR IGNORE INTO memories "
                        "(id, text, author_id, created_at, subject_id, subject_name, synced_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            memory_id,
                            text,
                            int(row.get("author_id") or 0),
                            created_iso,
                            int(row.get("subject_id") or 0),
                            str(row.get("subject_name") or "")[:32],
                            _ensure_utc(datetime.now(timezone.utc)),
                        ),
                    )
                    # Importing must never shrink the AUTOINCREMENT counter.
                    inserted.append(memory_id)
                # Keep the local AUTOINCREMENT counter ahead of every imported
                # id, so a brand-new memory can never reuse a remote id. (The
                # sqlite_sequence table only exists once something has been
                # inserted; a missing table simply means the counter is fresh
                # and the inserts above already moved it.)
                try:
                    conn.execute(
                        "UPDATE sqlite_sequence SET seq = MAX(seq, "
                        "(SELECT COALESCE(MAX(id), 0) FROM memories)) "
                        "WHERE name = 'memories'",
                    )
                except sqlite3.OperationalError:
                    pass
                conn.commit()
            except sqlite3.Error:
                log.warning("Could not import remote memories", exc_info=True)
                conn.rollback()
                return []
        return inserted

    def drop_ids(self, ids: list[int] | tuple[int, ...]) -> int:
        """Delete specific rows locally (remote deletes caught on pull)."""
        if not ids:
            return 0
        with self._lock:
            conn = self._connection()
            try:
                count = 0
                for memory_id in ids:
                    cursor = conn.execute(
                        "DELETE FROM memories WHERE id = ?", (int(memory_id),)
                    )
                    count += max(0, cursor.rowcount)
                conn.commit()
                return count
            except sqlite3.Error:
                log.warning("Could not drop memories", exc_info=True)
                conn.rollback()
                return 0

    def pending_tombstones(self) -> list[int]:
        with self._lock:
            try:
                return [
                    int(row[0])
                    for row in self._connection()
                    .execute("SELECT id FROM memory_tombstones ORDER BY id")
                ]
            except sqlite3.Error:
                return []

    def drop_tombstones(self, ids: list[int] | tuple[int, ...]) -> None:
        if not ids:
            return
        with self._lock:
            try:
                conn = self._connection()
                conn.executemany(
                    "DELETE FROM memory_tombstones WHERE id = ?",
                    [(int(memory_id),) for memory_id in ids],
                )
                conn.commit()
            except sqlite3.Error:
                log.warning("Could not clear tombstones", exc_info=True)


class MemoryMirror:
    """Best-effort Supabase mirror of the memory store.

    SQLite is always the working copy — the bot reads and writes it directly,
    with zero configuration. When the shared Supabase project is configured
    (``SUPABASE_URL`` / ``SUPABASE_SECRET_KEY``, the same credentials the
    voice tracker uses), this job also copies every memory to
    ``paradise_memories`` and back, so the memories survive a reinstall or a
    move to another machine, and a row deleted in the Supabase Dashboard
    disappears from the bot on the next pass.

    Local ids *are* remote ids (SQLite is the only writer), which makes the
    reconcile steps idempotent and race-free:

    1. pull — insert remote rows missing locally, drop local rows that were
       synced once but vanished remotely (deleted in the Dashboard);
    2. push — upsert unsynced local rows;
    3. delete — remove tombstoned ids remotely, then forget the tombstones.

    Every failure is logged at debug level and retried on the next pass; the
    mirror can never take a reply (or the bot) down. The schema
    (``supabase/migrations/005_memory.sql``) is verified once, and a missing
    schema is a logged no-op — exactly like the voice store, the bot never
    creates Supabase tables itself.
    """

    #: The table and RPC names (see supabase/migrations/005_memory.sql).
    TABLE = "paradise_memories"
    VERSION_FUNCTION = "paradise_memory_schema_version"
    SCHEMA_VERSION = 1
    SCHEMA_MISSING_MESSAGE = (
        "The Paradise Supabase project is missing the memory table. Run "
        "supabase/migrations/005_memory.sql in the Dashboard SQL Editor to "
        "enable memory mirroring; the bot keeps using local storage meanwhile."
    )

    def __init__(
        self,
        store: MemoryStore,
        *,
        url: str = "",
        key: str = "",
        interval: float = 120.0,
        request_timeout: float = 10.0,
        client: object = None,
    ) -> None:
        self.store = store
        self._url = (url or "").strip()
        self._key = (key or "").strip()
        self.interval = max(15.0, float(interval))
        self._request_timeout = float(request_timeout)
        self._client = client  # injected in tests; created lazily otherwise
        self._schema_ok = False
        self._degraded_warned = False
        self.metrics: dict[str, int] = {"reconciles": 0, "failures": 0}

    @property
    def configured(self) -> bool:
        """True when mirroring is possible (credentials or injected client)."""
        return self._client is not None or bool(self._url and self._key)

    async def _ensure_client(self):
        if self._client is None:
            if not self._url or not self._key:
                raise RuntimeError("Supabase credentials are not configured")
            from supabase import AsyncClientOptions, acreate_client

            options = AsyncClientOptions(
                postgrest_client_timeout=self._request_timeout,
            )
            self._client = await acreate_client(self._url, self._key, options=options)
        return self._client

    async def _verify_schema(self, client) -> bool:
        if self._schema_ok:
            return True
        try:
            result = await client.rpc(self.VERSION_FUNCTION).execute()
            version = int((result.data or [0])[0] if isinstance(result.data, list) else result.data)
        except Exception:  # noqa: BLE001 - any shape error means "not applied"
            if not self._degraded_warned:
                log.warning("%s", self.SCHEMA_MISSING_MESSAGE)
                self._degraded_warned = True
            return False
        if version < self.SCHEMA_VERSION:
            if not self._degraded_warned:
                log.warning(
                    "Paradise memory schema version %s is older than supported (%s); "
                    "re-run supabase/migrations/005_memory.sql",
                    version,
                    self.SCHEMA_VERSION,
                )
                self._degraded_warned = True
            return False
        self._schema_ok = True
        return True

    async def reconcile(self) -> bool:
        """One pass: pull, push, delete. True when it completed cleanly."""
        if not self.configured:
            return False
        try:
            client = await self._ensure_client()
            if not await self._verify_schema(client):
                return False

            # 1. Pull: remote rows the bot does not have yet.
            result = await client.table(self.TABLE).select(
                "id, text, author_id, created_at, subject_id, subject_name"
            ).order("id").execute()
            remote_rows = list(result.data or [])
            remote_ids = {int(row.get("id") or 0) for row in remote_rows}
            imported = self.store.import_remote(remote_rows)
            if imported:
                log.info("Memory mirror imported %d row(s) from Supabase", len(imported))

            # 2. Remote deletes: rows we pushed once that are gone remotely.
            vanished = sorted(self.store.synced_ids() - remote_ids)
            if vanished:
                dropped = self.store.drop_ids(vanished)
                if dropped:
                    log.info("Memory mirror removed %d remotely-deleted row(s)", dropped)

            # 3. Push: local rows Supabase has not seen (upsert keeps ids).
            pending = self.store.unsynced()
            for row in pending:
                await (
                    client.table(self.TABLE)
                    .upsert(
                        {
                            "id": row.id,
                            "text": row.text,
                            "author_id": row.author_id,
                            "created_at": _ensure_utc(row.created_at),
                            "subject_id": row.subject_id,
                            "subject_name": row.subject_name,
                        },
                        on_conflict="id",
                    )
                    .execute()
                )
            if pending:
                self.store.mark_synced([row.id for row in pending])
                log.info("Memory mirror pushed %d row(s) to Supabase", len(pending))

            # 4. Deletes: tombstoned ids go, then the tombstones themselves.
            #    Scoped by construction: memory_tombstones only ever holds ids
            #    this store actually had (set at remove/clear time), so the
            #    in_() list is the exact intended delete set.
            tombstones = self.store.pending_tombstones()
            if tombstones:
                await client.table(self.TABLE).delete().in_("id", tombstones).execute()
                self.store.drop_tombstones(tombstones)

            self.metrics["reconciles"] += 1
            return True
        except Exception:  # noqa: BLE001 - the mirror must never raise
            self.metrics["failures"] += 1
            log.debug("Memory mirror reconcile failed; will retry", exc_info=True)
            return False

    async def run_forever(self) -> None:
        """Reconcile now, then on every interval tick. Runs until cancelled."""
        while True:
            await self.reconcile()
            await asyncio.sleep(self.interval)
