# Performance notes — voice-activity pipeline

This document records what was measured, what changed, and what the trade-offs
are. Everything here is reproducible offline with no credentials and no
production data.

```bash
# baseline (pristine main) and current branch, same harness:
python3 benchmarks/voice_pipeline.py --label baseline
python3 benchmarks/voice_pipeline.py --label after
python3 benchmarks/voice_pipeline.py --compare baseline after
# legacy equivalence check (per-event writes, no coalescing window):
python3 benchmarks/voice_pipeline.py --coalesce 0 --label legacy
```

Raw results live in `benchmarks/results/*.json`.

## Method

* The real `SupabaseVoiceDatabase` + `VoiceTracker` run against the in-memory
  Supabase double from the test suite (`tests/voice_fakes.py`), with **40 ms of
  simulated round-trip latency per request** — roughly Delhi → `ap-south-1`.
* Workload: 12 users join, 4 rounds of mute toggles each (48 events), a burst of
  12 channel moves, one reconciliation cycle, one leaderboard render, 12 leaves.
  Then a separate 12-way concurrent join burst and a shutdown-durability case.
* The only difference between the two runs is the production code; the harness
  and the fake client are identical (the harness degrades gracefully when a
  method does not exist on the older tracker).
* Metrics: per-event handler latency (wall clock, awaited), Supabase request
  counts by operation, persistence delay for a burst of state changes, event-loop
  max gap (5 ms ticker), reconcile/leaderboard/stop duration, peak `tracemalloc`.

Not measured: live Supabase latency, real CPU time, startup time (needs a
Discord gateway login), 24-hour error rates. Those are marked *not measured* in
the report rather than estimated.

## What was slow

1. **One Supabase write per voice-state event.** Every mute/unmute and channel
   move called `upsert_active_session` → one batched-heartbeat RPC. A five-person
   conversation produced a steady trickle of writes that carried no new
   persisted information: `is_muted`/`is_deaf` are never stored, so a mute
   toggle rewrote the same row with a new `last_seen_at`.
2. **All of that I/O ran inside one global tracker lock.** A slow request
   (timeout 15 s) serialised *every* voice event for *every* guild.
3. **Identity writes were per member.** Joins cost two RPCs (profile upsert +
   heartbeat); a reconciliation scan could issue one profile RPC per member;
   leaderboard rendering issued one per ranked member.
4. **The leaderboard read the same profile rows twice** per render.
5. **The daily DM report did one `has_dm_report_sent` query per member** plus one
   SQLite lookup per member for the DM preference.
6. **Every SQLite call opened, configured and abandoned a connection**, including
   five `COUNT(*)` queries per maintenance cycle to detect pending migration rows.
7. **Outbox flushing was strictly one request per queued row**, and every queued
   row ran `COUNT(*)` over the outbox to update the warning threshold.

## What changed

| Area | Change |
| --- | --- |
| `functions/voice/tracker.py` | In-memory state transitions under a non-blocking state lock; heartbeat snapshots coalesced into one row per session and written by a single batched upsert per `VOICE_WRITE_COALESCE_SECONDS` (default 2 s); all row writes and closes serialised by one persistence lock so a delayed heartbeat can never resurrect a closed session; identity writes batched; bounded, timed shutdown flush |
| `functions/voice/supabase_database.py` | `upsert_member_profiles` (one `voice_upsert_profiles` RPC per chunk), `get_sent_dm_reports` (one `voice_fetch_dm_report_batch` RPC per guild/day), coalesced outbox drain for profile/DM-report runs, throttled outbox depth accounting, per-operation request metrics |
| `functions/voice/database.py` | One reused SQLite connection per thread (+`close()`), single-statement `has_pending_voice_data`, bulk `get_user_dm_preferences` |
| `functions/voice/store.py` | Non-abstract `upsert_member_profiles`, `get_sent_dm_reports`, `get_user_dm_preferences` defaults so every backend (including test doubles) keeps working |
| `commands/vc.py`, `config.py`, `.env.example`, `README.md` | New `VOICE_WRITE_COALESCE_SECONDS` (0 restores the old behaviour), bounded shutdown budget, local SQLite handles closed on unload |

Unchanged: the Supabase schema, every table, every SQL function, the meaning of
stored time, IST midnight splitting, AFK/deafen exclusion rules, retry
idempotency, the outbox format and the local mirror semantics.

## Results

Simulated 40 ms RTT, 12 users, 48 mute toggles, 12 moves, 12 joins, 12 leaves.
Both runs produced identical stored time (8064 s) and left zero active rows.

| Metric | Baseline | After | Change |
| --- | --- | --- | --- |
| Mute/deafen event latency (mean) | 41.43 ms | 0.03 ms | **−99.9 %** |
| Mute/deafen event latency (p95) | 42.14 ms | 0.05 ms | **−99.9 %** |
| Join (session start) latency (mean) | 82.66 ms | 41.12 ms | **−50.3 %** |
| Supabase requests, whole workload | 101 | 30 | **−70.3 %** |
| Requests per mute-toggle event | 1.00 | 0.00 | **−100 %** |
| Requests per join event | 2.00 | 1.00 | **−50 %** |
| Requests per move event | 1.00 | 0.17 (one per 2 s window) | **−83 %** |
| Leaderboard render | 125.36 ms | 84.88 ms | **−32.3 %** |
| Reconciliation cycle | 91.01 ms | 86.61 ms | −4.8 % |
| 12 concurrent joins (total) | 534.9 ms | 489.2 ms | −8.5 % |
| Peak traced memory | 185.8 KiB | 115.8 KiB | **−37.7 %** |
| Event-loop max gap | 7.2 ms | 6.1 ms | −15 % (both far below a frame) |
| Persistence delay, 12-row move burst | 0.14 ms | 1552 ms | trade-off (≤ one window) |
| Shutdown flush + close | 41.2 ms | 82.1 ms | +1 batched identity write |

### Weighted efficiency score

Improvement per metric is `(baseline − after) / baseline`, clamped to `[0, 1]`
(regressions earn no credit). Weights reflect how often each path runs and how
directly it affects users.

| Metric | Weight | Improvement | Contribution |
| --- | --- | --- | --- |
| High-frequency event latency (mute/deafen mean) | 30 % | 99.9 % | 0.300 |
| Supabase request count (whole workload) | 30 % | 70.3 % | 0.211 |
| Session-start (join) latency | 10 % | 50.3 % | 0.050 |
| Leaderboard render latency | 10 % | 32.3 % | 0.032 |
| 12-way concurrent join burst | 10 % | 8.5 % | 0.009 |
| Peak memory | 5 % | 37.7 % | 0.019 |
| Reconciliation cycle | 5 % | 4.8 % | 0.002 |
| **Overall** | **100 %** | | **≈ 62.3 %** |

Confidence: **moderate**. The request-count and latency deltas are structural
(a mute toggle now performs zero I/O, a window performs one write), and the
harness is deterministic apart from `asyncio.sleep` jitter. The weighted total is
a summary of one synthetic workload with a simulated RTT — it is not a
production-wide percentage, and it excludes CPU time, live network latency and
the error rate.

### Legacy-mode equivalence check

`--coalesce 0` disables the write window and restores the original per-event
write path (the flag is also the production rollback: set
`VOICE_WRITE_COALESCE_SECONDS=0`). With the same final harness the legacy run
shows **1.00 request per join/leave/move/toggle event**, a toggle latency of
**41.00 ms** (baseline 41.43 ms) and the same stored time (150 s recorded,
0 active rows left). Coalescing is therefore a pure scheduling change: switching
it off reproduces the old behaviour, including its cost.

## Data-safety analysis of the coalesced writes

* **Maximum persistence delay.** One coalescing window (`VOICE_WRITE_COALESCE_SECONDS`,
  default 2 s, clamp 0–10 s) for a heartbeat; **0** for session start and session
  end, which are still written/closed before the handler returns.
* **Crash safety.** Unchanged in practice: the periodic reconciliation heartbeat
  already bounded crash loss to one reconcile interval (default 20 s). A crash
  now loses at most the same interval, never accumulated *time* — the start
  timestamp is durable from the join.
* **Duplicate prevention.** A coalesced snapshot is dropped if its session ended
  or restarted before the write, and every write/close is serialised by one
  persistence lock, so a heartbeat cannot land after its close. Session rows are
  additionally idempotent on their SQL identity in PostgreSQL, so a replayed
  close can never double count.
* **Shutdown.** `stop()` cancels the loops, flushes pending identity rows, closes
  every open session, and is wrapped in `asyncio.wait_for` with a bounded budget
  (2× request timeout, clamped to 5–60 s). On timeout the data remains in the
  store's local mirror/outbox for the next start.
* **Supabase unavailable.** Heartbeats fall back to the bounded local mirror;
  profile writes fall back to the durable outbox; session closes queue the
  session + guarded delete in the outbox. Nothing is dropped, and recovery is
  reconciled on the next start.

## Known remaining costs

* A *session start* still costs one synchronous RPC (deliberate crash safety).
  A burst of simultaneous joins therefore still serialises on that write.
* Identity writes and heartbeats both run in the same window, so a window with
  both pending costs two requests (one each, batched).
* Leaderboard rendering still performs one Supabase read plus one profile read
  per render (down from two) and one batched identity write if anything changed.
* Reconciliation remains a full-guild scan every 20 s; it is in-memory only now,
  but the scan itself is O(members in voice).
* `VoicePresence.join_busiest` still evaluates every voice channel every 30 s and
  on each human join; it is unrelated to time accounting and was left untouched.

## Review agent

`tools/review_agent.py` reviews a diff (not the repository) for the failure modes
that matter here: destructive SQL, schema migrations, committed credentials,
blocking calls inside coroutines, buffered writes without a bounded flush,
swallowed exceptions, unsafe retries, unbounded table reads, and production
changes with no test change.

```bash
python3 tools/review_agent.py --worktree             # uncommitted work
python3 tools/review_agent.py --base origin/main     # a branch
python3 tools/review_agent.py --json                 # machine-readable
python3 tools/review_agent.py --self-test            # verify the rules
```

It never edits, merges, deploys or contacts GitHub; CI runs it against the PR
diff and fails only on blocking findings. It does not approve changes on the
basis of tests passing, and it makes no performance claims.
