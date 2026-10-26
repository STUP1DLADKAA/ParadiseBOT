# ParadiseBOT on Supabase — voice activity setup

The `/vc` leaderboard, `/vc stats`, and the daily DM reports are backed by the
**Supabase PostgreSQL database** (project **“Paradise”**, ref
`gxcjtbiyamyyxokgehpe`, region `ap-south-1`). SQLite remains only for user
settings (timezones, DM preferences) and as a bounded, temporary fallback
queue while Supabase is unreachable.

> **The bot never creates or alters Supabase tables.** The schema below is
> applied **manually, once**, by a project owner. The bot only *verifies* it
> (via `voice_schema_version()`) and refuses to track voice data until it
> exists.

## 1. Apply the schema (one-time, ~3 minutes)

1. Open the Supabase Dashboard → your **Paradise** project.
2. Go to **SQL Editor** → **New query**.
3. For a new database, run [`migrations/001_voice_activity.sql`](migrations/001_voice_activity.sql) first.
4. Run [`migrations/002_voice_ist_calendar.sql`](migrations/002_voice_ist_calendar.sql) second.
5. Run [`migrations/003_voice_checkpoint.sql`](migrations/003_voice_checkpoint.sql) third.

Each migration is transactional and safe to re-run. If the project already has
schema version 1, apply migrations 002 **and** 003. If it is already at 2,
apply **only migration 003**. The bot requires schema version **3**.

> **The bot can be deployed before 003 is applied.** Against a version-2
> database it runs in a documented *degraded mode*: tracking continues, but
> checkpointing is a silent no-op and the older heartbeat is used. It logs one
> warning at startup. Applying 003 removes the degradation at the next
> restart — no data is lost either way, and 003 is purely additive.

Migration 002 changes no tables: it replaces the reporting
functions and safely rebuckets eligible daily aggregates, while preserving:

- every historical `voice_sessions` row;
- per-user totals whose old daily aggregate does not reconcile exactly with
  its session history (including manual Dashboard edits);
- all member profiles, DM marks, active-session/outbox data, and lifetime time.

The migration recalculates dates only for users whose aggregate sum exactly
matches their complete session history. The changed week/month definitions are
Asia/Kolkata calendar periods (ISO Monday-to-Monday and first-of-month to
first-of-next-month); future dates are excluded from the in-progress period.

Migration 003 adds durable checkpointing. It is **additive and idempotent**:

* one new **nullable** column, `voice_active_sessions.credited_until` — the
  high-water mark of time already written to `voice_daily_stats`;
* one new append-only audit table, `voice_session_corrections`, which records
  any manual change to `started_at` (it rewrites nothing);
* `voice_checkpoint_active_sessions()` — credits `[coalesce(credited_until,
  started_at), now]` and advances the cursor with `greatest()` under
  `SELECT … FOR UPDATE`, **in the same transaction** as the
  `voice_record_session` insert, so crediting is atomic, idempotent and safe
  against out-of-order or repeated calls;
* `voice_heartbeat_active()` no longer writes `started_at` on conflict —
  heartbeats only refresh `channel_id`, `last_seen_at`, `is_afk` and
  `updated_at`, so a Dashboard edit is never reverted;
* `voice_heartbeat_active_v2()` — the same edit-safe upsert, but it also
  **echoes the durable row back** (`o_started_at`, `o_cursor`) so the bot
  adopts the database's values instead of writing its own over them;
* `voice_close_active_session()` credits from the cursor, so a checkpoint
  followed by a close can never credit the same interval twice.

It drops nothing, truncates nothing, and does not touch `voice_sessions` or
`voice_daily_stats` at all. Verify it locally with `node validate_locally.mjs`
(93 checks, run against real PostgreSQL via PGlite) before applying it.

Migration 001 creates:

| table | purpose |
| --- | --- |
| `voice_sessions` | one row per completed VC session (append-only history) |
| `voice_daily_stats` | per-guild/user/day totals (what leaderboards read) |
| `voice_active_sessions` | one compact row per user currently in VC (restart recovery + heartbeats) |
| `voice_member_profiles` | last-known display name / username / avatar |
| `voice_dm_reports` | which daily report DMs were already sent (exactly-once) |
| `voice_migration_state` | ledger making the SQLite→Supabase migration exactly-once |

plus the `voice_*()` functions that do all aggregation, midnight splitting
and atomic session closing **inside PostgreSQL**, and:

* **RLS enabled with zero policies** on every table — the anon/public key
  can read nothing.
* `EXECUTE` on the functions revoked from `public`/`anon`/`authenticated`
  and granted only to `service_role` (the role the server-side secret key
  uses; it bypasses RLS by design).

## 2. Get your credentials

In the Dashboard go to **Project Settings → API**:

* **Project URL** → `SUPABASE_URL`
* **service_role / secret key** (newer projects show it as
  `sb_secret_…`) → `SUPABASE_SECRET_KEY`

> ⚠️ **Never use the public/anon key (`sb_publishable_…`) for the bot** — it
> is restricted by RLS (which we deliberately locked down to zero policies),
> so nothing would work. Conversely, the secret key bypasses RLS and belongs
> **only** in the private `.env` file on your server: never commit it, never
> log it, never paste it anywhere (including Discord).

## 3. Configure the bot

Copy `.env.example` to `.env` if you haven't, then fill in:

```env
SUPABASE_URL=https://gxcjtbiyamyyxokgehpe.supabase.co
SUPABASE_SECRET_KEY=sb_secret_…        # your real secret key

VOICE_HEARTBEAT_INTERVAL=20            # seconds; 15–30 recommended
VOICE_MIGRATION_BATCH_SIZE=500         # rows per migration batch
VOICE_FLUSH_INTERVAL=30                # retry cadence during outages
VOICE_SUPABASE_TIMEOUT=15              # per-request timeout (seconds)
```

If the two Supabase variables are missing, the bot still starts fine —
voice tracking is simply disabled with a clear log message and `/vc` explains
what to do. Nothing else breaks and no data is lost.

## 4. What happens on startup

1. Load credentials → if missing, degraded mode (voice off, no data loss).
2. Connect and **verify** the schema via `voice_schema_version()` (the bot
   never creates tables). Missing/stale → a log message names the exact SQL
   file to run; it retries every 60 s.
3. **Drain the fallback outbox** (any writes queued during a previous
   outage), in strict order.
4. **Migrate** the historical SQLite voice data into Supabase (see below).
5. **Recover active sessions**: users still in a VC resume with their
   original join time; users who left while the bot was down are closed at
   their last heartbeat.
6. Start tracking + heartbeats (default every 20 s — one compact
   `UPDATE`-style upsert per user, **no** new rows per heartbeat). Each
   heartbeat re-reads the durable row, so a Dashboard edit or a cursor
   advanced by another instance is adopted rather than overwritten.
7. **Checkpoint** every `VOICE_CHECKPOINT_SECONDS` (default 120 s), writing
   the elapsed interval into `voice_daily_stats` and advancing
   `credited_until` in the same transaction. A final checkpoint runs on
   shutdown. Failures are counted and reported in the log and in the
   `VOICE_ALERT_CHANNEL_ID` channel — never swallowed.

A Supabase outage never blocks startup and never loses data: completed
sessions, profiles and DM-report marks queue in the local SQLite outbox, and
in-progress sessions are mirrored one row per user locally, then flushed
automatically when the connection returns.

## 5. The SQLite migration (automatic)

The first run (and later cycles) moves the pre-Supabase voice history from
`data/paradise.db` into Supabase automatically:

* **Bounded batches** (default 500 rows) — memory use never depends on DB size.
* **Verify-then-delete** — a local row is deleted only after the matching
  Supabase row was uploaded **and read back**. Any failure (network,
  verification mismatch) simply leaves the rows local; the next cycle retries.
* **Idempotent** — sessions dedupe on their old SQLite id (`source_id`);
  daily totals merge through the `voice_migration_state` ledger, so replays
  and crashes can never double-count. Live activity recorded *during* the
  migration is preserved.
* **Progressive** — verified batches are deleted from SQLite as they land, so
  `paradise.db` shrinks progressively. Only the five `voice_*` tables are
  touched: **`user_settings` (and anything else in that file) stays**. The
  bot never deletes `paradise.db` itself.
* Progress and totals are logged (`Migrated N sessions; …`, `Daily totals …
  SQLite X seconds -> Supabase Y seconds`).

## 6. Sanity checks (optional, SQL Editor)

```sql
-- Schema present and at the expected version (must return 3):
select voice_schema_version();

-- Today's leaderboard for a guild:
select * from voice_leaderboard(
  <guild_id>, 'today', (now() at time zone 'Asia/Kolkata')::date
);

-- Bounded per-period totals for one user:
select * from voice_user_breakdown(
  <guild_id>, <user_id>, (now() at time zone 'Asia/Kolkata')::date
);

-- Checkpoint health: sessions whose cursor has stalled. Empty in a healthy
-- system; a row here means an instance stopped checkpointing that user.
select guild_id, user_id, started_at, credited_until,
       now() - coalesce(credited_until, started_at) as uncredited
from voice_active_sessions
where now() - coalesce(credited_until, started_at) > interval '15 minutes'
order by uncredited desc;

-- Who has been corrected by hand, and when:
select guild_id, user_id, old_started_at, new_started_at, recorded_at
from voice_session_corrections
order by recorded_at desc
limit 50;

-- Rows left to migrate (should trend to 0 after the bot runs):
-- (run in the SQL Editor against your local SQLite file instead — the bot
--  logs the same numbers every cycle.)
```

Manual edits in the Dashboard (e.g. changing
`voice_daily_stats.duration_seconds`) are reflected in `/vc` on the very next
render — the bot keeps no long-lived cache.

## 7. How time is accounted for, and how to correct it

A session's time is never recomputed from a single in-memory start time. Two
places hold it:

| where | holds |
| --- | --- |
| `voice_daily_stats.duration_seconds` | time that has already been **committed** (authoritative; leaderboards read this) |
| `voice_active_sessions.credited_until` | the cursor: everything **before** it is committed, everything after it is still live |

The displayed total is `committed + (now − coalesce(credited_until, started_at))`.
The bot advances the cursor every `VOICE_CHECKPOINT_SECONDS` (default 120 s),
and `voice_close_active_session()` credits the remainder when the user leaves.
Because the cursor only ever moves forward inside one transaction with the
stats insert, a repeated or out-of-order checkpoint cannot credit twice, and a
crash costs at most one interval.

### Editing an active session in the Dashboard

`started_at` is **yours to edit** — the tracking loop never writes it on an
existing row, and it re-reads the durable row every heartbeat, so your edit is
adopted within ~20 s rather than being reverted. What happens depends on
whether the interval you changed had already been credited:

* **`credited_until` is NULL** (nothing credited yet, i.e. a session younger
  than one checkpoint interval) — edit `started_at` and the correction is
  honoured in full. Nothing else is needed.
* **`credited_until` is set** — the time before it already lives in
  `voice_daily_stats`, so moving `started_at` alone changes the displayed
  *start* but does not re-credit anything. To also grant the corrected
  interval, move `credited_until` back with it; to revoke time, correct
  `voice_daily_stats` for the affected day(s) directly.

Every manual change to `started_at` is recorded in `voice_session_corrections`
with the old and new values, so a correction is always traceable.

> Do not hand-edit `voice_daily_stats` while the session is still live unless
> you also move `credited_until` — the next checkpoint credits from the
> cursor, not from your edit.

## 8. Updating the schema later

Schema changes are additive migration files (`migrations/002_….sql`, …) that
bump `voice_schema_version()`. The bot refuses voice tracking (with a log
message naming the file to run) until an owner applies the new migration in
the SQL Editor. Never edit the `voice_*` objects by hand.

## 9. Verifying this repo's SQL (developers)

`validate_locally.mjs` runs all three migrations plus 93 behavioral checks
against a real PostgreSQL engine (PGlite/WASM), covering IST session splitting,
reconciled-history rebucketing, manual-total preservation, idempotent replays,
ISO-week/calendar-month windows, last-write-wins heartbeats, guarded deletes,
atomic closes, AFK exclusion, the migration ledger, RLS lockdown,
service_role access, and the checkpoint guarantees — atomic/idempotent
crediting, a monotonic cursor, checkpoint-then-close not double-counting, and
`voice_heartbeat_active_v2` echoing the authoritative row instead of
overwriting it:

```bash
cd supabase && npm install --no-save --package-lock=false @electric-sql/pglite && node validate_locally.mjs
```

`tests/test_voice_supabase.py`, `tests/test_voice_checkpoint.py` and
`tests/test_voice_migration.py` exercise the Python store, the tracker's
checkpoint/recovery behaviour and the migrator offline against an in-memory
fake of the Supabase client that mirrors these exact semantics.
`tools/repro_*.py` reproduce each reported incident end to end, and
`tools/fuzz_voice.py` randomises joins, restarts, outages, guild changes and
Dashboard edits to assert that credited time never decreases and is never
counted twice.

> **Note:** this repository has **not** been verified against the live
> Supabase project — that requires the owner's credentials and the manual
> SQL step above. The local harness validates the migration files without
> connecting to the production Supabase project.
