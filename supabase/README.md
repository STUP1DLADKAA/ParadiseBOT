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

Each migration is transactional and safe to re-run. If the project already has
schema version 1, apply **only migration 002**. The bot requires schema version
2 after the cutover. Migration 002 changes no tables: it replaces the reporting
functions and safely rebuckets eligible daily aggregates, while preserving:

- every historical `voice_sessions` row;
- per-user totals whose old daily aggregate does not reconcile exactly with
  its session history (including manual Dashboard edits);
- all member profiles, DM marks, active-session/outbox data, and lifetime time.

The migration recalculates dates only for users whose aggregate sum exactly
matches their complete session history. The changed week/month definitions are
Asia/Kolkata calendar periods (ISO Monday-to-Monday and first-of-month to
first-of-next-month); future dates are excluded from the in-progress period.

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
   `UPDATE`-style upsert per user, **no** new rows per heartbeat).

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
-- Schema present and at the expected version (must return 2):
select voice_schema_version();

-- Today's leaderboard for a guild:
select * from voice_leaderboard(
  <guild_id>, 'today', (now() at time zone 'Asia/Kolkata')::date
);

-- Bounded per-period totals for one user:
select * from voice_user_breakdown(
  <guild_id>, <user_id>, (now() at time zone 'Asia/Kolkata')::date
);

-- Rows left to migrate (should trend to 0 after the bot runs):
-- (run in the SQL Editor against your local SQLite file instead — the bot
--  logs the same numbers every cycle.)
```

Manual edits in the Dashboard (e.g. changing
`voice_daily_stats.duration_seconds`) are reflected in `/vc` on the very next
render — the bot keeps no long-lived cache.

## 7. Updating the schema later

Schema changes are additive migration files (`migrations/002_….sql`, …) that
bump `voice_schema_version()`. The bot refuses voice tracking (with a log
message naming the file to run) until an owner applies the new migration in
the SQL Editor. Never edit the `voice_*` objects by hand.

## 8. Verifying this repo's SQL (developers)

`validate_locally.mjs` runs both migrations plus behavioral checks against a
real PostgreSQL engine (PGlite/WASM), covering IST session splitting,
reconciled-history rebucketing, manual-total preservation, idempotent replays,
ISO-week/calendar-month windows, last-write-wins heartbeats, guarded deletes,
atomic closes, AFK exclusion, the migration ledger, RLS lockdown, and
service_role access:

```bash
cd supabase && npm install --no-save --package-lock=false @electric-sql/pglite && node validate_locally.mjs
```

`tests/test_voice_supabase.py` and `tests/test_voice_migration.py` exercise
the Python store and migrator offline against an in-memory fake of the
Supabase client that mirrors these exact semantics.

> **Note:** this repository has **not** been verified against the live
> Supabase project — that requires the owner's credentials and the manual
> SQL step above. The local harness validates the migration files without
> connecting to the production Supabase project.
