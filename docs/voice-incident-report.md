# Voice Activity Leaderboard — Incident Report

**Component:** ParadiseBOT voice activity tracking (`functions/voice/`, `commands/vc.py`, `supabase/`)
**Branch:** `arena/279e3e57-paradisebot`
**Status:** fixed in-tree, verified locally. **Not yet deployed** — see §H.

---

## A. Root cause

Four independent defects compounded into the three reported symptoms.

### RC1 — Nothing was ever durably credited during a session (issues A, B, C)

`voice_daily_stats` was written **only when a session closed**. For the whole
life of a session, the number shown on the leaderboard was computed in process
memory as `now − ActiveVoiceSession.started_at`. There was no checkpoint, no
cursor, and no partial credit.

Consequences:

* A session that never closed cleanly (crash, restart, gateway blip, a missed
  `VOICE_STATE_UPDATE`) contributed **zero** to the database. A 6-hour session
  ending badly was worth 0h, not 6h — this is issue **C**.
* Because the displayed total was one subtraction of an in-memory field,
  anything that moved `started_at` forward moved the *entire* total down. That
  is issue **A**: the daily total decreasing from 6h to 5h while the user
  stayed connected.
* Tracking "freezing" (issue **B**) is the same defect seen from the other
  side: once `started_at` had been pushed forward, the displayed total stopped
  advancing correctly even though the user was still in voice.

### RC2 — The restart race (`functions/voice/tracker.py`)

The single largest loss. `reconcile_guilds()` marked the recovery flags
(`_recovered`, `_recovered_remote`) **before** `_recover_persisted_sessions()`
had run, and the reconcile loop is started from `commands/vc.py::cog_load`
before `store.start()` has finished bootstrapping.

While the store was still bootstrapping, `get_active_sessions()` returned only
local-mirror rows — empty in steady state. So recovery "succeeded" having
recovered nothing, and Phase 2 then treated every user it could see in a voice
channel as a brand-new join, inventing `started_at = now`. The old heartbeat
wrote that invented value back over the durable row.

Measured against the pre-fix tree, with a user in voice for 6 hours across a
restart (`tools/repro_restart_race.py`):

```
TIME LOST BY THE RESTART RACE: 5h59m00s
  before=6h00m00s  after=0h01m00s
  voice_active_sessions.started_at: 2026-03-10T01:30:00+00:00  ->  2026-03-10T07:30:00+00:00
```

The same harness against the fixed tree:

```
TIME LOST BY THE RESTART RACE: 0h00m00s
  before=6h00m00s  after=6h01m00s
  voice_active_sessions.started_at: 2026-03-10T01:30:00+00:00 (unchanged)
```

### RC3 — The heartbeat overwrote `started_at` every 20 seconds (issue D)

`voice_heartbeat_active` upserted the full row on conflict, `started_at`
included. Any edit made in the Supabase Dashboard was reverted by the next
heartbeat, at most one reconcile interval (20 s) later. Manual correction was
therefore impossible in practice.

### RC4 — Two overlapping instances could move `started_at` forward

The old guard was last-write-wins on `last_seen_at` only. Two processes (a
rolling deploy, or a second instance started by mistake) could each advance
`started_at` past a checkpoint cursor the other had already passed, and
neither would notice.

### What was ruled out

Deliberately investigated and found **not** to be causes, so they are not
re-litigated here: `format_duration` capping; `voice_record_session` replay
double-counting (idempotent via `voice_sessions_identity_uidx`); midnight
rollover double-counting; AFK/deafen flips losing time; `time_windows`
disagreeing with migration 002 (both are ISO Monday→Monday); the local-mirror
merge picking a stale row. Isolated blips, restarts, outages, channel moves and
two-instance runs each preserved the total on their own — the 6-hour loss
specifically required the restart-during-bootstrap race.

---

## B. Files changed

26 files, +4862 / −122 (`git diff --stat main HEAD`).

| file | change |
| --- | --- |
| `supabase/migrations/003_voice_checkpoint.sql` | **new**, 426 lines. Additive, idempotent. |
| `supabase/validate_locally.mjs` | 91 → **93** checks; runs all three migrations. |
| `supabase/README.md` | schema v3, accounting model, admin-edit workflow, diagnostics. |
| `functions/voice/tracker.py` | +353. Checkpoint model, recovery ordering, health reporting. |
| `functions/voice/supabase_database.py` | +177. v3 policy, checkpoint RPC, v2 heartbeat echo. |
| `functions/voice/database.py` | +68. `credited_until` column, guarded `ALTER`, monotonic upsert. |
| `functions/voice/store.py`, `sqlite_store.py` | checkpoint interface; heartbeat returns the durable row. |
| `functions/voice/models.py` | `credited_until` + `credit_from` on `ActiveVoiceSession`. |
| `functions/voice/render.py` | +82. Measured layout (issue F). |
| `config.py`, `commands/vc.py`, `.env.example` | `VOICE_CHECKPOINT_SECONDS` (default 120). |
| `tests/test_voice_checkpoint.py` | **new**, 962 lines, 38 tests. |
| `tests/test_voice_render.py` | +16 tests; the 4 pre-existing tests retained. |
| `tests/test_voice_supabase.py`, `tests/voice_fakes.py` | schema-v3 policy tests; fake client at v3. |
| `tools/repro_restart_race.py`, `repro_incident.py`, `repro_incident2.py`, `fuzz_voice.py`, `trace_seed.py` | **new** reproducers and fuzzer. |
| `tools/review_agent.py` | DB001/DB002 no longer fire on SQL comments. |

Nothing was deleted. Test and `def` names were diffed between `main` and
`HEAD` for every changed `.py` file; no test function and no function was
removed. (An earlier revision of this work had overwritten
`tests/test_voice_render.py`, dropping four pre-existing tests; they were
restored verbatim in `ea337fb`.)

---

## C. Database impact

**No existing data is touched.** Migration 003 is additive and idempotent.

Added:

* `voice_active_sessions.credited_until` — one **nullable** column.
* `voice_session_corrections` — one new append-only audit table.
* `voice_checkpoint_active_sessions()`, `voice_heartbeat_active_v2()`.
* Replaced via `CREATE OR REPLACE`: `voice_heartbeat_active`,
  `voice_close_active_session`, `voice_schema_version` (1 → **3**).

Not done, deliberately:

* No `DROP`, no `TRUNCATE`, no `DELETE`, no blanket `UPDATE`, no `RENAME`.
* `voice_sessions` and `voice_daily_stats` are not referenced by any
  top-level statement in the migration.
* `voice_heartbeat_active` keeps its original signature. A new `_v2` function
  was added instead of changing the return type, because `CREATE OR REPLACE`
  cannot alter a return type and `DROP FUNCTION` would be destructive.

The only `DELETE` in the file is the pre-existing keyed single-row delete of a
liveness row inside `voice_close_active_session` — the normal end of a session,
present in migration 001.

**Rollback:** 003 cannot be un-applied without a destructive statement, and
none is provided. It does not need to be: it is inert until the bot calls the
new functions, so deploying the old bot against a v3 database is safe. The old
bot simply does not checkpoint.

---

## D. The accounting model

Time now lives in two places with a clear boundary:

| where | meaning |
| --- | --- |
| `voice_daily_stats.duration_seconds` | **committed** time. Authoritative. What leaderboards read. |
| `voice_active_sessions.credited_until` | the **cursor**. Everything before it is committed; everything after it is live. |

```
displayed total = committed + (now − coalesce(credited_until, started_at))
```

`voice_checkpoint_active_sessions()` credits
`[coalesce(credited_until, started_at), now]` and advances the cursor with
`greatest()` under `SELECT … FOR UPDATE`, **in the same transaction as the
`voice_record_session` insert**. That gives three properties at once:

* **Atomic** — the stats row and the cursor move together, or neither moves.
* **Idempotent** — replaying a checkpoint cannot credit twice, because the
  base is read from the cursor and the cursor only moves forward.
* **Out-of-order safe** — a late or duplicated call credits an empty interval.

`voice_close_active_session()` credits from the same cursor, so
checkpoint-then-close cannot double-count the tail of a session.

`voice_daily_stats` is upserted purely additively, so a committed total can
never decrease. Any observed decrease must come from the *live* part
shrinking — which is exactly what RC1/RC2 were doing.

Bot side: `ActiveVoiceSession.credit_from = max(started_at, credited_until)`.
Every live-window computation (`get_live_leaderboard`, `get_live_user_stats`,
`dispatch_daily_dm_reports`) and the completed-session record all use
`credit_from`, so `committed + live` equals the true session duration.

Midnight rollover resets the cursor. Shutdown checkpoints before closing.
`reconcile_guilds()` does nothing at all until `bot.guilds` is non-empty, so
the Phase-2 "invent a session" path can no longer fire during startup.

**AFK and deafened users are never credited.** `excluded_from_credit()` is
unchanged: an AFK channel or being deafened excludes; muted-only still counts.

---

## E. Editing an active session manually

`started_at` is safe to edit in the Dashboard. The heartbeat no longer writes
it on an existing row, and `_adopt_durable_state()` re-reads the durable row
every heartbeat, so an edit is adopted within ~20 s rather than reverted.

| situation | effect of editing `started_at` |
| --- | --- |
| `credited_until IS NULL` (session younger than one checkpoint interval) | honoured **in full**; nothing else needed |
| `credited_until` is set | the displayed *start* changes; already-credited time is **not** re-counted |

To also grant a corrected interval, move `credited_until` back with it. To
revoke time, correct `voice_daily_stats` for the affected day(s) directly.

This was reproduced explicitly (`tools/repro_incident2.py`, scenario 3):

```
[PASS] manual edit persists across tracking cycles
[PASS] already-credited time is NOT counted again after a start-time edit
[PASS] editing credited_until grants the corrected interval in full
```

Every manual change to `started_at` is recorded in
`voice_session_corrections` with old and new values. Note that the trigger
fires on `credited_until` changes too, so the bot's own cursor advances appear
there — filter with `where old_started_at is distinct from new_started_at`
when auditing real edits.

If a cursor is advanced by something other than this process, the bot adopts it
and logs `checkpoint cursor … advanced outside this process … check for an
overlapping deployment`. That is how RC4 becomes visible instead of silent.

---

## F. Leaderboard image

Rendering only. **No change** to the data model, ranking, period selection,
timezone, or eligibility, and no statistic is fabricated — the image and the
text embed consume the same `members` list from the same call in `/vc leaderboard`.

Defects fixed:

* **Columns were laid out from fixed offsets.** The name was truncated at a
  hardcoded 350 px, the progress bar sat at a hardcoded `x + w − 340`, and the
  right-anchored duration reserved no room for either. A 3-digit rank could
  reach the avatar and a long duration could run under the bar. Every column
  is now measured from the string occupying it: the rank gets a column as wide
  as its own text, the duration is measured first because it is right-anchored,
  the bar fits between name and duration, and the name takes what is left.
* **Row pitch disagreed with itself** — the canvas height reserved 78 px per
  row while the layout advanced 76. Replaced by `ROW_HEIGHT` / `ROW_PITCH`.
* **The server-total badge assumed 180 px.** It now sizes to its content, so a
  long total cannot spill.
* **A share could overflow its track.** Clamped for the bar *and* for the
  displayed percentage, so the card can never print a number the data does not
  support.

Unchanged and verified as adequate: top-3 visual hierarchy (gold/silver/bronze
podium, centre-first ordering), avatar fallback to a generated initial,
`_truncate_text` ellipsis, the empty-leaderboard message.

`fetch_avatar` already handled every failure path — no URL, corrupt cached
file, non-200, undecodable body — by falling back to the generated avatar.

---

## G. Verification

Everything below was actually run in this repository.

| check | command | result |
| --- | --- | --- |
| Full test suite | `.venv/bin/python -m pytest -q` | **726 passed, 36 subtests, 0 failed** |
| New checkpoint suite | `pytest tests/test_voice_checkpoint.py -q` | **38 passed** |
| Renderer suite | `pytest tests/test_voice_render.py -q` | **20 passed, 7 subtests** |
| Schema + behaviour | `cd supabase && node validate_locally.mjs` | **93 passed, 0 failed**, exit 0 |
| Randomised accounting | `python tools/fuzz_voice.py 40` | **0/40 iterations violated the invariant** |
| Restart race | `python tools/repro_restart_race.py` | **0h00m00s lost** (pre-fix: 5h59m00s) |
| Incident reproducers | `python tools/repro_incident.py` / `repro_incident2.py` | A/B/D all **False**; **9/9 invariants held** |
| Static review gate | `python tools/review_agent.py --base main --fail-on blocking` | **0 blocking**, exit 0 |
| Pipeline smoke | `python benchmarks/voice_pipeline.py` | runs end to end; shutdown recorded 150 s of 150 s expected |

**SQL is validated against real PostgreSQL**, not mocked: `validate_locally.mjs`
executes all three migrations in PGlite/WASM. Two of the checks run the
diagnostic queries published in `supabase/README.md` verbatim, so the
documentation cannot drift from the schema.

**Tests were mutation-tested** where a passing assertion could otherwise be
vacuous:

* Removing the share clamp fails both `TestShareBar` tests.
* Disabling SQL comment-stripping, downgrading the default DB001 branch, and
  making DB002 unconditional each fail `review_agent --self-test`.

**What was not verified:** nothing here has run against the live Supabase
project. That requires the owner's credentials and the manual SQL step in §H.
The local harness validates the migration files without connecting to
production.

---

## H. Deployment

The bot and the migration are **independently deployable**, in either order.

**Order 1 — bot first (safe, temporarily unimproved).** Against a v2 database
the bot logs one warning and runs in what the code calls **compatibility
mode**: tracking continues, the older heartbeat is used, checkpointing is a
silent no-op. Nothing is lost, but RC1 is not yet fixed.

**Order 2 — migration first (recommended).**

1. **Back up.** Supabase Dashboard → Database → Backups. Confirm a restore
   point exists before anything else.
2. **Apply 003.** SQL Editor → paste the whole of
   `supabase/migrations/003_voice_checkpoint.sql` → Run. It is one
   transaction: it applies completely or not at all, and it is safe to re-run.
3. **Verify:**
   ```sql
   select voice_schema_version();  -- must return 3
   ```
4. **Deploy the bot** as usual (no new required env var).
5. **Confirm in the logs:** no compatibility-mode warning (`Supabase voice
   schema is version %s; the bot needs %s …`), and checkpoint health reported
   every cycle.

**Optional tuning** — `.env`:

```
VOICE_CHECKPOINT_SECONDS=120   # clamped 0..3600; 0 disables checkpointing
```

Lower means less time at risk on a crash and more write traffic. 120 s is one
RPC batch per interval for all active users, not one per user per second.

**Rollback.** Revert the bot to the previous release. Leave 003 applied — it
is inert without the new code paths, and removing it would require destructive
SQL, which this change deliberately does not provide.

---

## I. Risks

| risk | severity | mitigation |
| --- | --- | --- |
| 003 is applied incorrectly in the Dashboard | low | single transaction; `voice_schema_version()` must return 3; the bot refuses to track and names the file to run if it does not |
| Bot deployed before 003 | low | documented compatibility mode; one warning at startup; no data loss |
| Crash loses up to one checkpoint interval | low, by design | bounded by `VOICE_CHECKPOINT_SECONDS`; was **unbounded** before |
| An admin moves `credited_until` back into already-credited time | low | intentional and honoured — it grants that time again. Documented in §E and logged to `voice_session_corrections` |
| Two instances running at once | low | the durable cursor is adopted within one cycle and a WARNING names the likely cause; totals stay correct because crediting is idempotent |
| Checkpoint write volume | low | one batched RPC per interval; `benchmark` covers the loop |
| The fuzzer is deterministic per seed | low | `tools/fuzz_voice.py <iterations>` takes a count; `tools/trace_seed.py <seed> <ticks>` replays one seed tick by tick |

**Not a risk, but worth stating:** this change never rewrites history. If a
user's `voice_daily_stats` was already wrong before this fix, it stays wrong —
the fix stops the bleeding, it does not reconstruct the past. Correcting
historical totals is a separate, manual, per-user decision.

---

## J. Monitoring

**Health reporting** — `VoiceTracker.health_snapshot(now)` / `report_health()`
run every reconcile cycle and expose:

* `creditable_sessions` — non-AFK sessions in memory;
* `max_uncredited_seconds` — the largest `now − credit_from` gap;
* `stale_sessions` — sessions past `CHECKPOINT_STALE_MULTIPLIER × checkpoint_seconds`
  (4 × 120 s = 8 min) without a checkpoint;
* consecutive checkpoint failures.

Failures are **never swallowed**. `checkpoint_active_sessions` re-raises on
`VoiceStoreUnavailable` so the tracker can distinguish "failed" from "nothing
to do"; `reconcile_guilds` guards recovery against both `VoiceStoreError` and
unexpected exceptions so one failure cannot take the rest of the pass down.
Alerts go to `VOICE_ALERT_CHANNEL_ID`.

**SQL diagnostics** (also in `supabase/README.md` §6, both executed by the
validator):

```sql
-- Stalled checkpoints. Empty in a healthy system.
select guild_id, user_id, started_at, credited_until,
       now() - coalesce(credited_until, started_at) as uncredited
from voice_active_sessions
where now() - coalesce(credited_until, started_at) > interval '15 minutes'
order by uncredited desc;

-- Manual corrections, most recent first.
select guild_id, user_id, old_started_at, new_started_at, recorded_at
from voice_session_corrections
order by recorded_at desc limit 50;
```

**Log lines worth alerting on** (quoted from the source, split across lines in
the code but emitted as one message):

* `tracker.py` — `Checkpoint cursor for user %s in guild %s advanced outside
  this process (%s -> %s). If only one ParadiseBOT instance should be running,
  check for an overlapping deployment.` — a second instance, or an out-of-band
  edit to `credited_until`.
* `tracker.py` — `Voice checkpoint failed (%s consecutive); the interval stays
  uncredited and will be retried: %s` — Supabase is unreachable. Time is
  queued, not lost, but a sustained run needs attention.
* `tracker.py` — `Voice session recovery failed (database unreachable)` /
  `Voice session recovery failed unexpectedly` — recovery will be retried on
  the next cycle; Phase 2 refuses to create sessions until it succeeds.
* `supabase_database.py` — `Supabase voice schema is version %s; the bot needs
  %s for durable voice checkpoints. Tracking continues in compatibility mode
  …` — logged once; migration 003 has not been applied.

**Suggested alert thresholds:** any `stale_sessions > 0` sustained for more
than two cycles; any `max_uncredited_seconds > 900`; any non-zero consecutive
checkpoint failure count lasting more than 5 minutes.
