# Voice pipeline optimisation — final report

Scope: the Discord voice-activity tracker (`functions/voice/*`, `commands/vc.py`) and its
Supabase persistence. Constraint: preserve every feature, permission, event, table, SQL
function and stored value. No schema change, no data rewrite, no new service or dependency.

Reproduce everything below offline:

```bash
pip install -r requirements.txt pytest
python3 -m pytest -q                                   # 668 passed
python3 benchmarks/voice_pipeline.py --label baseline   # must run on origin/main
python3 benchmarks/voice_pipeline.py --label after
python3 benchmarks/voice_pipeline.py --compare baseline after
python3 tools/review_agent.py --base origin/main
```

Raw numbers: `benchmarks/results/{baseline,after,legacy}.json`. Method, weights and the
data-safety analysis in detail: `docs/performance.md`.

---

## 1. Summary of changes

Five focused commits on `arena/27dab2a5-paradisebot`, PR **#27** (open, not merged):

| Commit | Contents |
| --- | --- |
| `b5bfb5f` perf(voice): batch identity and DM-report Supabase traffic | batched profile upsert, batched DM-report lookup, SQLite connection reuse, bulk DM preferences, outbox drain grouping, request metrics |
| `242586f` perf(voice): coalesce heartbeat writes, keep events off the event loop | lock split (state vs persistence), coalesced heartbeat writes, mute/deafen no-write, batched reconcile/leaderboard/DM paths, bounded shutdown flush |
| `e5edd0d` test(voice): cover coalesced writes, batching and shutdown flush | 21 new tests + call-recording test double |
| `60ed9dd` perf(bench): add offline voice-pipeline harness and measured results | harness, three result files, `docs/performance.md` |
| `ed0f95f` feat(tools): add diff review agent and CI | `tools/review_agent.py`, `.github/workflows/ci.yml` |

17 files, +3294 / −185 lines. No change to `supabase/` or `requirements.txt`.

New configuration: `VOICE_WRITE_COALESCE_SECONDS` (default 2, clamp 0–10; `0` restores the
old one-write-per-event behaviour). Documented in `.env.example` and `README.md`.

## 2. Bottlenecks identified

1. **One Supabase write per voice-state event.** A mute toggle rewrote the same active row with
   a new `last_seen_at`, although `is_muted`/`is_deaf` are never stored. In the benchmark,
   48 toggles cost 48 RPCs and 41 ms of awaited handler time each.
2. **Network I/O inside one global tracker lock.** A slow request (15 s timeout) serialised every
   voice event in every guild.
3. **Per-member identity traffic.** A join cost two RPCs (profile + heartbeat); reconcile and
   leaderboard rendering issued one profile RPC per member; the leaderboard read profile rows
   twice per render.
4. **Per-user DM-report queries.** One `has_dm_report_sent` plus one SQLite preference lookup per
   member, every day.
5. **SQLite churn.** A connection was opened, configured and abandoned per call, and the
   maintenance loop ran five `COUNT(*)` probes per cycle.
6. **Outbox flushing one request per row**, each preceded by its own `COUNT(*)` depth query.

## 3. Voice / Supabase changes

* `functions/voice/tracker.py`
  * **State vs persistence locks.** `_state_lock` guards the in-memory dictionaries and is never
    held across an `await`; `_persist_lock` serialises every row write and close, so a delayed
    heartbeat can never land after the close that removed the row.
  * **Coalesced writes.** Joins and leaves are written/closed before the handler returns (crash
    safety, unchanged). Moves and repeated heartbeats are merged per session (last wins) into one
    batched `voice_heartbeat_active` RPC per window. Mute/deafen toggles persist nothing.
  * **Stale-snapshot guard.** A queued snapshot is dropped if the session's `started_at` or
    `is_afk` changed in the meantime, so it can never resurrect a closed or restarted session.
  * **Reconcile** scans in memory (no awaits), then performs one batched heartbeat plus any closes
    the scan implied, re-queueing rows if that write fails.
  * **Bounded shutdown.** `stop()` cancels loops, flushes identity rows, closes every open session
    under `asyncio.wait_for` (2× request timeout, clamped 5–60 s); on timeout the local
    mirror/outbox holds the data for the next start.
  * Bounded memory: profile-signature LRU (4096), pending-profile cap, named tasks, deaf-warning
    tasks cancelled and cleared on stop/leave.
* `functions/voice/store.py` — non-abstract `upsert_member_profiles`, `get_sent_dm_reports`,
  `get_user_dm_preferences` defaults, so every backend (and test double) keeps working.
* `functions/voice/supabase_database.py` — one `voice_upsert_profiles` per chunk, one
  `voice_fetch_dm_report_batch` per guild/day, grouped outbox drain, throttled outbox depth
  accounting, per-operation request metrics. All RPCs used already exist in
  `001_voice_activity.sql`; **no migration was added or modified**.
* `functions/voice/database.py` — one SQLite connection per thread (+`close()` called on cog
  unload), single-statement `has_pending_voice_data`, bulk DM preferences.
* `commands/vc.py`, `config.py` — wire the window and shutdown budget, close SQLite handles.

## 4. Optimisations applied

Write coalescing (one batched upsert per window instead of one per event); locked-section
shrinkage (no I/O under the state lock); request batching (profiles, DM reports, outbox);
request elimination (mute/deafen toggles, duplicate profile read); SQLite connection reuse and
single-statement probes; bounded flush at shutdown; no new dependencies or services.

## 5. Before/after measurements

Offline harness, real tracker + real Supabase store, in-memory Supabase double, **40 ms simulated
RTT**, 12 users, 48 mute toggles, 12 moves, 12 joins/leaves, 12-way join burst, shutdown case.
Baseline is `origin/main` measured with the identical harness (`/tmp/paradise-baseline` worktree).

| Metric | Baseline | After | Change |
| --- | --- | --- | --- |
| Mute/deafen event latency (mean) | 41.43 ms | 0.03 ms | **−99.9 %** |
| Mute/deafen p95 | 42.14 ms | 0.05 ms | **−99.9 %** |
| Join latency (mean) | 82.66 ms | 41.12 ms | **−50.3 %** |
| Supabase requests (whole workload) | 101 | 30 | **−70.3 %** |
| Requests per mute toggle | 1.00 | 0.00 | **−100 %** |
| Requests per join | 2.00 | 1.00 | **−50 %** |
| Requests per move | 1.00 | 0.17 | **−83 %** |
| Reconcile cycle | 91.01 ms | 86.61 ms | −4.8 % |
| Leaderboard render | 125.36 ms | 84.88 ms | **−32.3 %** |
| 12 concurrent joins | 534.9 ms | 489.2 ms | −8.5 % |
| Peak traced memory | 185.8 KiB | 115.8 KiB | **−37.7 %** |
| Event-loop max gap | 7.2 ms | 6.1 ms | −15 % |
| Persistence delay (12-row move burst) | 0.14 ms | 1552 ms | trade-off ≤ 1 window |
| Shutdown flush + close | 41.2 ms | 82.1 ms | +1 batched identity write |
| Stored time, both runs | 8064 s | 8064 s | identical |
| Active rows left after stop | 0 | 0 | identical |

`--coalesce 0` reproduces the legacy path: 1.00 request per event, toggle 41.00 ms (baseline
41.43 ms), same stored time — the window is a scheduling change, not a semantic one.

## 6. Weighted overall efficiency

`(baseline − after) / baseline` per metric, clamped to `[0, 1]`, weights reflect how often the
path runs and how directly it affects users:

| Metric | Weight | Improvement | Contribution |
| --- | --- | --- | --- |
| High-frequency event latency | 30 % | 99.9 % | 0.300 |
| Supabase request count | 30 % | 70.3 % | 0.211 |
| Join latency | 10 % | 50.3 % | 0.050 |
| Leaderboard render | 10 % | 32.3 % | 0.032 |
| Concurrent join burst | 10 % | 8.5 % | 0.009 |
| Peak memory | 5 % | 37.7 % | 0.019 |
| Reconcile cycle | 5 % | 4.8 % | 0.002 |
| **Overall** | **100 %** | | **≈ 62.3 %** |

Confidence: **moderate**. The latency and request-count deltas are structural (a toggle now
performs zero I/O; a window performs one write) and the harness is deterministic apart from
`asyncio.sleep` jitter. The figure is a summary of one synthetic workload — **not** a
production-wide percentage. CPU time, live Supabase latency, startup time and 24 h error rates
are **not measured** (listed as such rather than estimated).

## 7. Test results

* `python3 -m pytest -q` → **668 passed, 29 subtests passed** (647 before this work).
* `tests/test_voice_pipeline.py` → 21 new tests: one batched RPC per window, no write for a pure
  mute toggle, immediate durable join, no heartbeat after a close, stale-snapshot drop, burst
  batching, profile coalescing, outbox fallback when Supabase is unavailable, bounded shutdown
  with preserved stored time and zero active rows, legacy mode.
* Review agent self-test → OK (5 injected findings, rules `ASY001/DB001/DB002/SEC001/TEST001`).
* CI (GitHub Actions, run 40172794480): **Tests (Python 3.11) pass (39 s)**,
  **Review agent (diff) pass (6 s)**.
* Not run: live Supabase integration (no credentials in this environment), Discord gateway
  login. Those paths are covered by the in-memory double and the existing suite only.

## 8. Review-agent findings

`tools/review_agent.py` is a stdlib-only reviewer that analyses a **diff**, never the whole repo,
and never edits, merges, deploys or contacts GitHub. Rules: committed/pasted secrets (SEC001,
SEC002), destructive or data-affecting SQL and schema migrations (DB001, DB002), suspicious
deletes (DB004), `select('*')` (PERF001), blocking calls inside coroutines via AST (ASY001),
unbounded `gather` (ASY002), unnamed/unowned tasks (ASY003), swallowed exceptions (ERR001),
retry loops without backoff (ERR002), buffered writes without a flush or bounded shutdown
(DATA001, DATA002), and production changes with no test change (TEST001).

Findings history:

| Round | Finding | Resolution |
| --- | --- | --- |
| 1 | Reconcile heartbeat failure path dropped queued heartbeat rows | rows are re-queued into the dirty map and retried on the next window (`242586f`) |
| 1 | Background task created without a name/lifetime check (`ASY003`) | benchmark loop ticker named and awaited before the scenario returns |
| 1 | False positives in blocking-call detection (regex matched the agent's own source and module-level CLI code) | detection rewritten as AST analysis limited to `async def` bodies, agent skips its own file |
| 1 | `DATA002` false negative on a multi-line `asyncio.wait_for(...timeout=...)` | pattern widened; verified by the self-test |
| Final | `--base origin/main` over 17 files | **No findings** (CI review job also passes) |

The agent does not decide to change code. The workflow it supports: it reports on a proposed
diff → a human approves which findings to implement → each approved fix is committed with a test
on the dedicated branch → tests + agent are re-run → the PR is updated. No auto-merge, no deploy.

## 9. GitHub status

* Branch: `arena/27dab2a5-paradisebot` (pushed, tracks `origin`).
* PR: **#27** — open, mergeable, base `main`, five commits, not merged, not deployed.
* CI: both jobs green on the current head (see §7). The review job has `contents: read` only and
  needs no secret.
* Nothing was applied to Supabase; no database, migration or production data was touched.

## 10. DB / data compatibility

* **No migration added or modified.** `git diff origin/main...HEAD -- supabase/` is empty.
* **No table, column, type, constraint, index, RLS policy, trigger or SQL function changed.**
  All 15 RPCs and 4 tables the Python code addresses already exist in
  `001_voice_activity.sql` / `002_voice_ist_calendar.sql` (verified by cross-checking every
  `.rpc(...)` / `.table(...)` name against the migrations).
* **Stored values are computed by the same rules**: IST midnight splitting, AFK/deafen exclusion,
  duplicate prevention via `voice_sessions_identity_uidx` and `source_id`, and the existing
  "close from the persisted row's `started_at`" semantics are untouched.
* **Nothing recalculated or rewritten.** No historical row is read for update, deleted or
  re-derived; the change is limited to how often and how many rows are sent per request.
* **Outbox format unchanged** — existing queued rows from a previous version are drained by the
  same code paths (grouped, still idempotent).

## 11. Compatibility confirmation

* Commands, permissions, listeners and user-visible output are unchanged; the only new input is
  an optional environment variable whose default preserves the new (faster) behaviour and whose
  `0` value restores the old one.
* Non-Supabase deployments, the SQLite-only backend and the test double keep working: new store
  methods are concrete defaults, not new abstract requirements.
* No new dependency (`requirements.txt` untouched), no added service, no framework change.
* Failure behaviour is not weakened: retries stay bounded, HTTP timeouts unchanged, errors are
  logged without secrets, and a Supabase outage still falls back to the durable local
  mirror/outbox.

## 12. Remaining issues

* A **session start** is still one synchronous RPC (deliberate crash safety), so simultaneous
  joins still serialise on that write; joins remain ~41 ms at 40 ms RTT.
* A window that contains both identity and heartbeat work costs two requests (one each, batched).
* Reconciliation is still a full-guild scan every 20 s; it is in-memory now, but it is O(members
  in voice) per guild per cycle.
* `commands/voice_presence.py` (`join_busiest`) still scans every voice channel every 30 s; it is
  unrelated to time accounting and was left untouched.
* Coalescing adds up to one window (2 s) of end-to-end persistence delay for `last_seen_at`
  during in-progress sessions (documented trade-off; stored *time* is unaffected).
* Benchmarks use an in-memory Supabase double; live latency and production CPU were not measured.

## 13. Further recommendations

1. Measure against a staging Supabase project (`ap-south-1`) before/after with the same harness to
   confirm the simulated RTT assumption; the harness accepts `--latency`.
2. If join latency becomes a concern, write the active row from the local mirror first
   (write-behind) while keeping it durable on the same host — no schema change needed.
3. Ask the owner (separately, not applied here) whether a partial index on
   `voice_active_sessions (guild_id)` or a covering index for `voice_leaderboard` would help at
   current scale; measure first, apply only with approval.
4. Consider moving `join_busiest`'s channel scan to an event-driven cache in a later, separate
   change.
5. Keep `VOICE_WRITE_COALESCE_SECONDS` at 2 s; raising it above 5 s buys little (the window cost
   is dominated by the first request) and widens the persistence-delay window.
6. Wire the review agent into branch protection as a required check once the team is comfortable
   with it; today it only fails on blocking findings and never approves automatically.
