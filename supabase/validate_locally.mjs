// ============================================================================
// ParadiseBot — local validation harness for the Supabase voice schema
// ============================================================================
// Runs migrations 001 and 002 against a REAL PostgreSQL (PGlite — Postgres
// compiled to WASM) inside this Node process and asserts the behaviours the
// bot depends on: IST splitting, calendar windows, retry idempotency, legacy
// aggregate rebucketing, the migration ledger, RLS lockdown, etc.
//
// It touches nothing outside this process — safe to run any time:
//
//   cd supabase
//   npm install @electric-sql/pglite   # one-time, local node_modules
//   node validate_locally.mjs
//
// Green output here means the migration SQL itself is valid and behaves as
// designed. It does NOT replace the one-time execution in the Supabase
// Dashboard (see supabase/README.md).
// ============================================================================

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { PGlite } from "@electric-sql/pglite";

const here = dirname(fileURLToPath(import.meta.url));
const baseMigrationSql = readFileSync(join(here, "migrations", "001_voice_activity.sql"), "utf8");
const istMigrationSql = readFileSync(join(here, "migrations", "002_voice_ist_calendar.sql"), "utf8");
const checkpointMigrationSql = readFileSync(join(here, "migrations", "003_voice_checkpoint.sql"), "utf8");

const db = new PGlite();
let passed = 0;
let failed = 0;
const failures = [];

function check(name, condition, detail = "") {
  if (condition) {
    passed++;
    console.log(`  ok   ${name}`);
  } else {
    failed++;
    failures.push(name + (detail ? ` — ${detail}` : ""));
    console.log(`  FAIL ${name}${detail ? " — " + detail : ""}`);
  }
}

// PGlite parses DATE/TIMESTAMPTZ columns into JS Date objects; normalise
// everything to plain ISO strings so comparisons behave like the JSON the
// Supabase REST API returns.
function norm(v) {
  return v instanceof Date ? v.toISOString() : v;
}
function normDate(v) {
  return v instanceof Date ? v.toISOString().slice(0, 10) : String(v).slice(0, 10);
}

async function q(sql, params) {
  const res = await db.query(sql, params);
  return res.rows;
}

async function scalar(sql, params) {
  const rows = await q(sql, params);
  return rows[0] ? Object.values(rows[0])[0] : null;
}

// ---------------------------------------------------------------------------
console.log("\n== 0. apply base schema and the additive IST migration ==");
// Supabase already has anon/authenticated/service_role; create equivalents
// with the same RLS-relevant properties (service_role has BYPASSRLS there).
await db.exec(`
  do $$ begin
    if not exists (select 1 from pg_roles where rolname = 'anon') then
      create role anon nologin;
    end if;
    if not exists (select 1 from pg_roles where rolname = 'authenticated') then
      create role authenticated nologin;
    end if;
    if not exists (select 1 from pg_roles where rolname = 'service_role') then
      create role service_role nologin bypassrls;
    end if;
  end $$;
`);
await db.exec(baseMigrationSql);
await db.exec(baseMigrationSql);
console.log("  ok   base migration applied and re-applied (idempotent)");
passed++;

// Simulate legacy daily aggregates whose source sessions are still stored
// untouched in UTC. One account reconciles exactly and can be safely rebucketed;
// one does not, so migration 002 must preserve its hand-edited aggregate.
await db.query(
  "insert into public.voice_sessions (guild_id,user_id,channel_id,started_at,ended_at,duration_seconds) values ($1,$2,$3,$4,$5,$6)",
  [778, 701, 1, "2026-09-30T18:20:00+00:00", "2026-09-30T18:50:00+00:00", 1800]);
await db.query(
  "insert into public.voice_daily_stats (guild_id,user_id,date,duration_seconds) values ($1,$2,$3,$4)",
  [778, 701, "2026-09-30", 1800]);
await db.query(
  "insert into public.voice_sessions (guild_id,user_id,channel_id,started_at,ended_at,duration_seconds) values ($1,$2,$3,$4,$5,$6)",
  [778, 702, 1, "2026-09-30T18:20:00+00:00", "2026-09-30T18:50:00+00:00", 1800]);
await db.query(
  "insert into public.voice_daily_stats (guild_id,user_id,date,duration_seconds) values ($1,$2,$3,$4)",
  [778, 702, "2026-09-30", 1234]);

await db.exec(istMigrationSql);
const rebucketed = await q(
  "select date,duration_seconds from public.voice_daily_stats where guild_id=778 and user_id=701 order by date");
check("IST migration rebuckets a reconciled session", rebucketed.length === 2
  && normDate(rebucketed[0].date) === "2026-09-30" && rebucketed[0].duration_seconds === 600
  && normDate(rebucketed[1].date) === "2026-10-01" && rebucketed[1].duration_seconds === 1200,
  JSON.stringify(rebucketed));
const preserved = await scalar(
  "select duration_seconds from public.voice_daily_stats where guild_id=778 and user_id=702 and date='2026-09-30'");
check("IST migration preserves unreconciled/manual aggregates", preserved === 1234, `got ${preserved}`);
try {
  await db.exec(istMigrationSql);
  console.log("  ok   IST migration re-applied without reinterpreting data");
  passed++;
} catch (e) {
  failed++;
  failures.push("IST migration not re-runnable: " + e.message);
  console.log("  FAIL IST migration not re-runnable — " + e.message);
}

check("schema version is 2", (await scalar("select public.voice_schema_version()")) === 2);

// ---------------------------------------------------------------------------
console.log("\n== 0b. apply the additive checkpoint migration (003) ==");
// Snapshot the data that must survive untouched, so the migration can be
// proven non-destructive rather than merely asserted to be.
const beforeCounts = {
  sessions: Number(await scalar("select count(*) from public.voice_sessions")),
  daily: Number(await scalar("select count(*) from public.voice_daily_stats")),
  dailySeconds: Number(await scalar(
    "select coalesce(sum(duration_seconds),0) from public.voice_daily_stats")),
  sessionSeconds: Number(await scalar(
    "select coalesce(sum(duration_seconds),0) from public.voice_sessions")),
  profiles: Number(await scalar("select count(*) from public.voice_member_profiles")),
};

await db.exec(checkpointMigrationSql);
await db.exec(checkpointMigrationSql);
console.log("  ok   checkpoint migration applied and re-applied (idempotent)");
passed++;

check("schema version is 3", (await scalar("select public.voice_schema_version()")) === 3);
check("voice_active_sessions gained credited_until",
  Number(await scalar(
    "select count(*) from information_schema.columns " +
    "where table_name='voice_active_sessions' and column_name='credited_until'")) === 1);
check("credited_until is nullable (existing rows stay valid)",
  (await scalar(
    "select is_nullable from information_schema.columns " +
    "where table_name='voice_active_sessions' and column_name='credited_until'")) === "YES");

const afterCounts = {
  sessions: Number(await scalar("select count(*) from public.voice_sessions")),
  daily: Number(await scalar("select count(*) from public.voice_daily_stats")),
  dailySeconds: Number(await scalar(
    "select coalesce(sum(duration_seconds),0) from public.voice_daily_stats")),
  sessionSeconds: Number(await scalar(
    "select coalesce(sum(duration_seconds),0) from public.voice_sessions")),
  profiles: Number(await scalar("select count(*) from public.voice_member_profiles")),
};
check("migration preserved every voice_sessions row and every second",
  afterCounts.sessions === beforeCounts.sessions
  && afterCounts.sessionSeconds === beforeCounts.sessionSeconds,
  `${JSON.stringify(beforeCounts)} -> ${JSON.stringify(afterCounts)}`);
check("migration preserved every voice_daily_stats row and every second",
  afterCounts.daily === beforeCounts.daily
  && afterCounts.dailySeconds === beforeCounts.dailySeconds,
  `${JSON.stringify(beforeCounts)} -> ${JSON.stringify(afterCounts)}`);
check("migration preserved every member profile",
  afterCounts.profiles === beforeCounts.profiles);

// ---------------------------------------------------------------------------
console.log("\n== 0c. durable checkpoint accounting (the incident fix) ==");
const CG = 4242, CU = 9001, CC = 71;
await db.query("delete from public.voice_active_sessions where guild_id=$1 and user_id=$2",
  [CG, CU]);
await db.query("delete from public.voice_daily_stats where guild_id=$1 and user_id=$2",
  [CG, CU]);
await db.query("delete from public.voice_sessions where guild_id=$1 and user_id=$2", [CG, CU]);

// A session opens at 07:00 IST (01:30Z) and is still running.
await db.query("select public.voice_heartbeat_active($1::jsonb)", [JSON.stringify([{
  guild_id: CG, user_id: CU, channel_id: CC,
  started_at: "2026-03-10T01:30:00+00:00", last_seen_at: "2026-03-10T01:30:00+00:00",
  is_afk: false,
}])]);

const cp = async (rows) =>
  q("select * from public.voice_checkpoint_active_sessions($1::jsonb)", [JSON.stringify(rows)]);

// Test 1/2 — a 4-hour continuous session, checkpointed twice.
let c1 = await cp([{ guild_id: CG, user_id: CU, up_to: "2026-03-10T03:30:00+00:00" }]);
check("checkpoint credits exactly the interval since the cursor (2h)",
  c1.length === 1 && c1[0].o_credited_seconds === 7200, JSON.stringify(c1));
let c2 = await cp([{ guild_id: CG, user_id: CU, up_to: "2026-03-10T05:30:00+00:00" }]);
check("next checkpoint credits only the new interval (2h), not the whole session",
  c2.length === 1 && c2[0].o_credited_seconds === 7200, JSON.stringify(c2));

// Test 3 — the same checkpoint replayed must credit nothing.
let c3 = await cp([{ guild_id: CG, user_id: CU, up_to: "2026-03-10T05:30:00+00:00" }]);
check("repeated checkpoint is credited exactly once (0s on replay)",
  c3.length === 1 && c3[0].o_credited_seconds === 0, JSON.stringify(c3));

// Test 4 — an out-of-order (older) checkpoint must not rewind the cursor.
let c4 = await cp([{ guild_id: CG, user_id: CU, up_to: "2026-03-10T02:00:00+00:00" }]);
const cursorAfter = await scalar(
  "select credited_until from public.voice_active_sessions where guild_id=$1 and user_id=$2",
  [CG, CU]);
check("out-of-order checkpoint credits 0 and cannot rewind the cursor",
  c4[0]?.o_credited_seconds === 0 && norm(cursorAfter) === "2026-03-10T05:30:00.000Z",
  `${JSON.stringify(c4)} cursor=${norm(cursorAfter)}`);

const committed = Number(await scalar(
  "select coalesce(sum(duration_seconds),0) from public.voice_daily_stats " +
  "where guild_id=$1 and user_id=$2", [CG, CU]));
check("checkpoints accumulated exactly 4h in voice_daily_stats", committed === 14400, `got ${committed}`);

// Test 5 — the final close must credit ONLY the uncredited remainder.
const closed = await scalar(
  "select public.voice_close_active_session($1,$2,$3)",
  [CG, CU, "2026-03-10T07:30:00+00:00"]);
const finalDaily = Number(await scalar(
  "select coalesce(sum(duration_seconds),0) from public.voice_daily_stats " +
  "where guild_id=$1 and user_id=$2", [CG, CU]));
const finalSessions = Number(await scalar(
  "select coalesce(sum(duration_seconds),0) from public.voice_sessions " +
  "where guild_id=$1 and user_id=$2", [CG, CU]));
check("close credits only the remainder (2h), never re-crediting the checkpoints",
  closed === 7200, `got ${closed}`);
check("checkpointed session totals exactly 6h (no double count, no loss)",
  finalDaily === 21600 && finalSessions === 21600,
  `daily=${finalDaily} sessions=${finalSessions}`);

// Test 6 — the heartbeat must never clobber a Dashboard edit.
await db.query("delete from public.voice_daily_stats where guild_id=$1 and user_id=$2", [CG, CU]);
await db.query("delete from public.voice_sessions where guild_id=$1 and user_id=$2", [CG, CU]);
await db.query("select public.voice_heartbeat_active($1::jsonb)", [JSON.stringify([{
  guild_id: CG, user_id: CU, channel_id: CC,
  started_at: "2026-03-10T01:30:00+00:00", last_seen_at: "2026-03-10T03:00:00+00:00",
  is_afk: false,
}])]);
await db.query(
  "update public.voice_active_sessions set started_at=$1 where guild_id=$2 and user_id=$3",
  ["2026-03-10T00:30:00+00:00", CG, CU]);
// The bot now heartbeats with its own (stale) in-memory start time.
await db.query("select public.voice_heartbeat_active($1::jsonb)", [JSON.stringify([{
  guild_id: CG, user_id: CU, channel_id: CC,
  started_at: "2026-03-10T01:30:00+00:00", last_seen_at: "2026-03-10T03:00:20+00:00",
  is_afk: false,
}])]);
const editedStart = await scalar(
  "select started_at from public.voice_active_sessions where guild_id=$1 and user_id=$2",
  [CG, CU]);
check("heartbeat no longer reverts an admin edit to started_at",
  norm(editedStart) === "2026-03-10T00:30:00.000Z", `got ${norm(editedStart)}`);
const auditRows = await q(
  "select old_started_at, new_started_at from public.voice_session_corrections " +
  "where guild_id=$1 and user_id=$2 " +
  "and old_started_at is distinct from new_started_at order by id", [CG, CU]);
check("the administrative start-time correction is audited with old and new values",
  auditRows.length === 1
  && norm(auditRows[0].old_started_at) === "2026-03-10T01:30:00.000Z"
  && norm(auditRows[0].new_started_at) === "2026-03-10T00:30:00.000Z",
  JSON.stringify(auditRows));
const editedCredit = await cp([{ guild_id: CG, user_id: CU, up_to: "2026-03-10T03:00:00+00:00" }]);
check("the corrected start time is honoured in full (2.5h from 00:30Z)",
  editedCredit[0]?.o_credited_seconds === 9000, JSON.stringify(editedCredit));
check("the checkpoint echoes the corrected started_at so the bot can adopt it",
  norm(editedCredit[0]?.o_started_at) === "2026-03-10T00:30:00.000Z",
  String(norm(editedCredit[0]?.o_started_at)));
await db.query("select public.voice_close_active_session($1,$2,$3)",
  [CG, CU, "2026-03-10T03:00:00+00:00"]);

// Test 7 — a checkpoint for a session that no longer exists is a silent no-op.
const ghost = await cp([{ guild_id: CG, user_id: CU, up_to: "2026-03-10T09:00:00+00:00" }]);
check("checkpointing a closed session is a no-op (no phantom credit)",
  ghost.length === 0, JSON.stringify(ghost));

// Test 8 — AFK rows are never credited by a checkpoint (policy unchanged).
await db.query("select public.voice_heartbeat_active($1::jsonb)", [JSON.stringify([{
  guild_id: CG, user_id: CU, channel_id: CC,
  started_at: "2026-03-10T01:30:00+00:00", last_seen_at: "2026-03-10T01:30:00+00:00",
  is_afk: true,
}])]);
const afkCp = await cp([{ guild_id: CG, user_id: CU, up_to: "2026-03-10T05:00:00+00:00" }]);
check("checkpoint skips AFK rows (AFK time is never counted)",
  afkCp.length === 0, JSON.stringify(afkCp));
await db.query("delete from public.voice_active_sessions where guild_id=$1 and user_id=$2", [CG, CU]);

// Test 8b — the v2 heartbeat reports the durable values back to the bot.
await db.query("select public.voice_heartbeat_active($1::jsonb)", [JSON.stringify([{
  guild_id: CG, user_id: CU, channel_id: CC,
  started_at: "2026-03-10T01:30:00+00:00", last_seen_at: "2026-03-10T02:00:00+00:00",
  is_afk: false,
}])]);
await db.query(
  "update public.voice_active_sessions set started_at=$1, credited_until=$2 " +
  "where guild_id=$3 and user_id=$4",
  ["2026-03-10T00:30:00+00:00", "2026-03-10T01:50:00+00:00", CG, CU]);
const hb2 = await q("select * from public.voice_heartbeat_active_v2($1::jsonb)", [JSON.stringify([{
  guild_id: CG, user_id: CU, channel_id: CC,
  started_at: "2026-03-10T01:30:00+00:00", last_seen_at: "2026-03-10T02:00:20+00:00",
  is_afk: false,
}])]);
check("v2 heartbeat returns the authoritative started_at and cursor",
  hb2.length === 1
  && norm(hb2[0].o_started_at) === "2026-03-10T00:30:00.000Z"
  && norm(hb2[0].o_cursor) === "2026-03-10T01:50:00.000Z",
  JSON.stringify(hb2));
check("v2 heartbeat still refreshes liveness",
  norm(await scalar("select last_seen_at from public.voice_active_sessions " +
    "where guild_id=$1 and user_id=$2", [CG, CU])) === "2026-03-10T02:00:20.000Z");
check("v2 heartbeat never rewrites the durable started_at",
  norm(await scalar("select started_at from public.voice_active_sessions " +
    "where guild_id=$1 and user_id=$2", [CG, CU])) === "2026-03-10T00:30:00.000Z");
await db.query("delete from public.voice_active_sessions where guild_id=$1 and user_id=$2", [CG, CU]);
const hb2insert = await q("select * from public.voice_heartbeat_active_v2($1::jsonb)", [JSON.stringify([{
  guild_id: CG, user_id: CU, channel_id: CC,
  started_at: "2026-03-10T01:30:00+00:00", last_seen_at: "2026-03-10T02:00:00+00:00",
  is_afk: false,
}])]);
check("v2 heartbeat inserts a brand-new session with its own started_at",
  hb2insert.length === 1
  && norm(hb2insert[0].o_started_at) === "2026-03-10T01:30:00.000Z"
  && hb2insert[0].o_cursor === null,
  JSON.stringify(hb2insert));
await db.query("delete from public.voice_active_sessions where guild_id=$1 and user_id=$2", [CG, CU]);

// Test 9 — the new table is RLS-locked like every other voice table.
const correctionsRls = await scalar(
  "select relrowsecurity from pg_class c join pg_namespace n on n.oid=c.relnamespace " +
  "where n.nspname='public' and c.relname='voice_session_corrections'");
check("voice_session_corrections has row level security enabled", correctionsRls === true);
await db.exec("grant usage on schema public to anon;");
await db.exec("grant select on public.voice_session_corrections to anon;");
let anonCorrections = null;
try {
  await db.exec("set role anon");
  anonCorrections = await q("select count(*) as c from public.voice_session_corrections");
  await db.exec("reset role");
} catch (e) {
  await db.exec("reset role");
  anonCorrections = [{ c: "permission denied" }];
}
check("anon sees zero correction rows (RLS, no policies)",
  Number(anonCorrections[0].c) === 0, JSON.stringify(anonCorrections));
let anonCheckpointErr = null;
try {
  await db.exec("set role anon");
  await q("select * from public.voice_checkpoint_active_sessions('[]'::jsonb)");
  await db.exec("reset role");
} catch (e) {
  await db.exec("reset role");
  anonCheckpointErr = e.message;
}
check("anon cannot execute the checkpoint function",
  /permission denied for function voice_checkpoint_active_sessions/.test(anonCheckpointErr ?? ""),
  anonCheckpointErr ?? "no error");
let anonHb2Err = null;
try {
  await db.exec("set role anon");
  await q("select * from public.voice_heartbeat_active_v2('[]'::jsonb)");
  await db.exec("reset role");
} catch (e) {
  await db.exec("reset role");
  anonHb2Err = e.message;
}
check("anon cannot execute the v2 heartbeat function",
  /permission denied for function voice_heartbeat_active_v2/.test(anonHb2Err ?? ""),
  anonHb2Err ?? "no error");

// ---------------------------------------------------------------------------
console.log("\n== 1. voice_record_session: midnight splitting & retry safety ==");
const G = 999, U = 101, C = 555;

let r = await scalar("select public.voice_record_session($1,$2,$3,$4,$5)",
  [G, U, C, "2026-09-30T18:20:00+00:00", "2026-09-30T18:50:00+00:00"]);
check("returns session duration (1800s)", r === 1800, `got ${r}`);

let daily = await q(
  "select date, duration_seconds from public.voice_daily_stats where guild_id=$1 and user_id=$2 order by date",
  [G, U]);
check("midnight split: exactly two daily rows", daily.length === 2, JSON.stringify(daily));
check("day 1 gets 600s (23:50→00:00)",
  normDate(daily[0]?.date) === "2026-09-30" && daily[0]?.duration_seconds === 600, JSON.stringify(daily[0]));
check("day 2 gets 1200s (00:00→00:20)",
  normDate(daily[1]?.date) === "2026-10-01" && daily[1]?.duration_seconds === 1200, JSON.stringify(daily[1]));

// Retrying the exact same event must not double-count.
r = await scalar("select public.voice_record_session($1,$2,$3,$4,$5)",
  [G, U, C, "2026-09-30T18:20:00+00:00", "2026-09-30T18:50:00+00:00"]);
check("duplicate retry returns 0", r === 0, `got ${r}`);
daily = await q(
  "select coalesce(sum(duration_seconds),0) as s from public.voice_daily_stats where guild_id=$1 and user_id=$2",
  [G, U]);
check("duplicate retry did not change daily totals", Number(daily[0].s) === 1800, `sum=${daily[0].s}`);
check("only one session row exists",
  (await scalar("select count(*) from public.voice_sessions where guild_id=$1 and user_id=$2", [G, U])) === 1);

// Multi-day session 22:30 IST (Sep 29) → 01:30 IST (Oct 1) = 27h.
await scalar("select public.voice_record_session($1,$2,$3,$4,$5)",
  [G, U, C, "2026-09-29T17:00:00+00:00", "2026-09-30T20:00:00+00:00"]);
const u1 = await q("select date, duration_seconds from public.voice_daily_stats where guild_id=$1 and user_id=$2 order by date", [G, U]);
check("multi-day IST split preserves all whole seconds",
  u1.length === 3 &&
  normDate(u1[0].date) === "2026-09-29" && u1[0].duration_seconds === 5400 &&
  normDate(u1[1].date) === "2026-09-30" && u1[1].duration_seconds === 86400 + 600 &&
  normDate(u1[2].date) === "2026-10-01" && u1[2].duration_seconds === 5400 + 1200,
  JSON.stringify(u1));

// Sub-second and invalid ranges are ignored.
check("sub-second session ignored",
  (await scalar("select public.voice_record_session($1,$2,$3,$4,$5)",
    [G, U, C, "2026-10-02T10:00:00+00:00", "2026-10-02T10:00:00.500+00:00"])) === 0);
check("reversed range ignored",
  (await scalar("select public.voice_record_session($1,$2,$3,$4,$5)",
    [G, U, C, "2026-10-02T11:00:00+00:00", "2026-10-02T10:00:00+00:00"])) === 0);

// A session ending exactly at midnight must not create a 0s row for the new day.
await scalar("select public.voice_record_session($1,$2,$3,$4,$5)",
  [G, 202, C, "2026-10-02T16:30:00+00:00", "2026-10-02T18:30:00+00:00"]);
const edge = await q("select date, duration_seconds from public.voice_daily_stats where guild_id=$1 and user_id=202 order by date", [G]);
check("session ending at midnight: single 7200s row",
  edge.length === 1 && normDate(edge[0].date) === "2026-10-02" && edge[0].duration_seconds === 7200,
  JSON.stringify(edge));

// ---------------------------------------------------------------------------
console.log("\n== 2. active sessions: heartbeat LWW, guarded delete, atomic close ==");
await scalar("select public.voice_heartbeat_active($1)", [JSON.stringify([
  { guild_id: G, user_id: 301, channel_id: C, started_at: "2026-10-05T10:00:00+00:00", last_seen_at: "2026-10-05T10:20:00+00:00", is_afk: false },
])]);
check("heartbeat inserts row",
  (await scalar("select count(*) from public.voice_active_sessions where guild_id=$1 and user_id=301", [G])) === 1);

// An out-of-order (older) heartbeat must not clobber the newer last_seen.
await scalar("select public.voice_heartbeat_active($1)", [JSON.stringify([
  { guild_id: G, user_id: 301, channel_id: C, started_at: "2026-10-05T10:00:00+00:00", last_seen_at: "2026-10-05T10:05:00+00:00", is_afk: false },
])]);
check("older heartbeat loses (LWW)",
  norm(await scalar("select last_seen_at from public.voice_active_sessions where guild_id=$1 and user_id=301", [G])) === "2026-10-05T10:20:00.000Z",
  String(await scalar("select last_seen_at from public.voice_active_sessions where guild_id=$1 and user_id=301", [G])));

// A heartbeat only refreshes liveness. started_at is the durable session
// origin and is set on INSERT only, so an in-memory value can never rewrite
// it -- this is what makes Dashboard corrections and crash recovery safe.
await scalar("select public.voice_heartbeat_active($1)", [JSON.stringify([
  { guild_id: G, user_id: 301, channel_id: C, started_at: "2026-10-05T09:30:00+00:00", last_seen_at: "2026-10-05T10:25:00+00:00", is_afk: false },
])]);
check("heartbeat refreshes liveness without rewriting the durable started_at",
  norm(await scalar("select started_at from public.voice_active_sessions where guild_id=$1 and user_id=301", [G])) === "2026-10-05T10:00:00.000Z"
  && norm(await scalar("select last_seen_at from public.voice_active_sessions where guild_id=$1 and user_id=301", [G])) === "2026-10-05T10:25:00.000Z",
  norm(await scalar("select started_at from public.voice_active_sessions where guild_id=$1 and user_id=301", [G])));

// Guarded delete: a delete for an older session leaves a newer row alone.
await scalar("select public.voice_heartbeat_active($1)", [JSON.stringify([
  { guild_id: G, user_id: 302, channel_id: C, started_at: "2026-10-05T12:00:00+00:00", last_seen_at: "2026-10-05T12:10:00+00:00", is_afk: false },
])]);
let d = await scalar("select public.voice_delete_active_if_older($1,$2,$3)", [G, 302, "2026-10-05T11:00:00+00:00"]);
check("guarded delete of older session deletes nothing", Number(d) === 0, `deleted=${d}`);
d = await scalar("select public.voice_delete_active_if_older($1,$2,$3)", [G, 302, "2026-10-05T12:00:00+00:00"]);
check("guarded delete of matching session deletes the row", Number(d) === 1, `deleted=${d}`);

// Atomic close: uses the ROW's started_at, records the session, removes the row.
await scalar("select public.voice_heartbeat_active($1)", [JSON.stringify([
  { guild_id: G, user_id: 303, channel_id: 777, started_at: "2026-10-05T08:00:00+00:00", last_seen_at: "2026-10-05T08:30:00+00:00", is_afk: false },
])]);
r = await scalar("select public.voice_close_active_session($1,$2,$3)", [G, 303, "2026-10-05T09:00:00+00:00"]);
check("atomic close records 3600s from the row's started_at", r === 3600, `got ${r}`);
check("atomic close removed the active row",
  (await scalar("select count(*) from public.voice_active_sessions where guild_id=$1 and user_id=303", [G])) === 0);
check("atomic close wrote the daily total",
  (await scalar("select duration_seconds from public.voice_daily_stats where guild_id=$1 and user_id=303 and date='2026-10-05'", [G])) === 3600);
r = await scalar("select public.voice_close_active_session($1,$2,$3)", [G, 303, "2026-10-05T09:30:00+00:00"]);
check("closing a missing active row returns null", r === null, `got ${r}`);

// AFK rows are dropped without recording.
await scalar("select public.voice_heartbeat_active($1)", [JSON.stringify([
  { guild_id: G, user_id: 304, channel_id: 778, started_at: "2026-10-05T08:00:00+00:00", last_seen_at: "2026-10-05T08:30:00+00:00", is_afk: true },
])]);
r = await scalar("select public.voice_close_active_session($1,$2,$3)", [G, 304, "2026-10-05T09:00:00+00:00"]);
check("AFK close records 0s", r === 0, `got ${r}`);
check("AFK close removed the row",
  (await scalar("select count(*) from public.voice_active_sessions where guild_id=$1 and user_id=304", [G])) === 0);
check("AFK time never reached daily stats",
  (await scalar("select count(*) from public.voice_daily_stats where guild_id=$1 and user_id=304", [G])) === 0);

// ---------------------------------------------------------------------------
console.log("\n== 3. leaderboards: IST calendar-window semantics ==");
// Target date Friday 2026-10-09. The ISO week starts Monday Oct 5, and the
// calendar month starts Oct 1. Future daily rows are not counted.
const T = "2026-10-09";
const seedDates = {
  "2026-09-07": 11,
  "2026-09-30": 22,
  "2026-10-01": 33,
  "2026-10-02": 44,
  "2026-10-05": 55,
  "2026-10-08": 66,
  "2026-10-09": 77,
  "2026-10-10": 88,
};
for (const [day, secs] of Object.entries(seedDates)) {
  await db.query(
    "insert into public.voice_daily_stats (guild_id, user_id, date, duration_seconds) values ($1,$2,$3,$4)",
    [555, 501, day, secs]);
}
const board = async (period) =>
  (await scalar("select (public.voice_leaderboard($1,$2,$3)).total_seconds", [555, period, T]));

let b = await board("today");
check("today = target only (77s)", b === 77, `got ${b}`);
b = await board("yesterday");
check("yesterday = target-1 only (66s)", b === 66, `got ${b}`);
b = await board("week");
check("week = Monday Oct 5 through target (55+66+77=198)", b === 198, `got ${b}`);
b = await board("month");
check("month = Oct 1 through target (33+44+55+66+77=275)", b === 275, `got ${b}`);
b = await board("all_time");
check("all_time includes every row (396s)", b === 396, `got ${b}`);
check("unknown period returns no rows",
  (await scalar("select count(*) from public.voice_leaderboard($1,$2,$3)", [555, "bogus", T])) === 0);

// Multi-user ranking: order by total desc then user_id asc; zero rows excluded.
await db.query("insert into public.voice_daily_stats (guild_id, user_id, date, duration_seconds) values ($1,$2,$3,$4)", [555, 502, T, 55]);
await db.query("insert into public.voice_daily_stats (guild_id, user_id, date, duration_seconds) values ($1,$2,$3,$4)", [555, 503, T, 0]);
const rows = await q("select * from public.voice_leaderboard($1,$2,$3)", [555, "today", T]);
check("ranking ordered by seconds desc, user_id asc",
  rows.length === 2 && rows[0].user_id === 501 && rows[1].user_id === 502,
  JSON.stringify(rows));

// User breakdown mirrors the same windows.
const bd = (await q("select * from public.voice_user_breakdown($1,$2,$3)", [555, 501, T]))[0];
check("breakdown today", Number(bd.today_seconds) === 77, JSON.stringify(bd));
check("breakdown yesterday", Number(bd.yesterday_seconds) === 66);
check("breakdown week", Number(bd.week_seconds) === 198);
check("breakdown month", Number(bd.month_seconds) === 275);
check("breakdown all_time", Number(bd.all_time_seconds) === 396);
const bdNone = (await q("select * from public.voice_user_breakdown($1,$2,$3)", [555, 999999, T]))[0];
check("breakdown of unknown user is all zeros",
  Number(bdNone.today_seconds) === 0 && Number(bdNone.all_time_seconds) === 0);

check("daily server total",
  (await scalar("select public.voice_daily_server_total($1,$2)", [555, T])) === 132);
const recs = await q("select * from public.voice_daily_records($1)", [T]);
check("daily records exclude zero rows", recs.length === 2, JSON.stringify(recs));

// ---------------------------------------------------------------------------
console.log("\n== 4. profiles: coalesce, no-op writes, LWW ==");
let n = await scalar("select public.voice_upsert_profiles($1)", [JSON.stringify([
  { guild_id: 555, user_id: 601, display_name: "Alex", username: "alex", avatar_url: "http://img/a.png" },
])]);
check("profile insert applies", Number(n) === 1, `applied=${n}`);
const ts1 = norm(await scalar("select updated_at from public.voice_member_profiles where guild_id=555 and user_id=601"));
n = await scalar("select public.voice_upsert_profiles($1)", [JSON.stringify([
  { guild_id: 555, user_id: 601, display_name: "Alex", username: "alex", avatar_url: "http://img/a.png" },
])]);
const ts2 = norm(await scalar("select updated_at from public.voice_member_profiles where guild_id=555 and user_id=601"));
check("unchanged profile is not rewritten", Number(n) === 0 && ts1 === ts2, `applied=${n} ts1=${ts1} ts2=${ts2}`);
n = await scalar("select public.voice_upsert_profiles($1)", [JSON.stringify([
  { guild_id: 555, user_id: 601, display_name: "Alex R", username: "alex", avatar_url: null },
])]);
const prof = (await q("select * from public.voice_member_profiles where guild_id=555 and user_id=601"))[0];
check("rename applies and NULL avatar keeps the stored one",
  Number(n) === 1 && prof.display_name === "Alex R" && prof.avatar_url === "http://img/a.png",
  JSON.stringify(prof));
// An older timestamp (stale migration row) must not clobber the fresher row.
n = await scalar("select public.voice_upsert_profiles($1)", [JSON.stringify([
  { guild_id: 555, user_id: 601, display_name: "Stale Old Name", username: "alex", avatar_url: null, updated_at: "2020-01-01T00:00:00+00:00" },
])]);
check("stale (older updated_at) profile loses (LWW)", Number(n) === 0);
check("profile name still fresh",
  (await scalar("select display_name from public.voice_member_profiles where guild_id=555 and user_id=601")) === "Alex R");

// ---------------------------------------------------------------------------
console.log("\n== 5. migration ledger: exactly-once daily merge ==");
const batch = [
  { guild_id: 777, user_id: 701, date: "2026-09-30", duration_seconds: 5000 },
  { guild_id: 777, user_id: 702, date: "2026-09-30", duration_seconds: 800 },
];
let res = await q("select public.voice_merge_daily_batch($1) as r", [JSON.stringify(batch)]);
res = res[0].r;
check("first merge returns resulting values",
  res.length === 2 && res[0].resulting_seconds === 5000 && res[1].resulting_seconds === 800,
  JSON.stringify(res));
// Live activity lands on the same key between migration attempts.
await db.query("update public.voice_daily_stats set duration_seconds = duration_seconds + 600 where guild_id=777 and user_id=701 and date='2026-09-30'");
// Retry the same batch (e.g. verification failed previously):
res = (await q("select public.voice_merge_daily_batch($1) as r", [JSON.stringify(batch)]))[0].r;
check("retried merge adds 0 extra seconds",
  res[0].resulting_seconds === 5600 && res[1].resulting_seconds === 800,
  JSON.stringify(res));
check("ledger rows exist",
  (await scalar("select count(*) from public.voice_migration_state where guild_id=777")) === 2);

// fetch_* verification helpers
const fd = await q("select * from public.voice_fetch_daily_batch($1)", [JSON.stringify(batch)]);
check("fetch_daily_batch returns both keys with current values",
  fd.length === 2 && fd.find((x) => x.user_id === 701).duration_seconds === 5600,
  JSON.stringify(fd));
await db.query("insert into public.voice_sessions (guild_id,user_id,channel_id,started_at,ended_at,duration_seconds,source_id) values (777,701,1,'2026-09-30T10:00:00+00:00','2026-09-30T11:00:00+00:00',3600,42)");
const fs_ = await q("select * from public.voice_fetch_sessions_batch($1)", [[42, 43]]);
check("fetch_sessions_batch finds by source_id",
  fs_.length === 1 && fs_[0].source_id === 42 && fs_[0].duration_seconds === 3600);
const fp = await q("select * from public.voice_fetch_profiles_batch($1)", [JSON.stringify([{ guild_id: 555, user_id: 601 }])]);
check("fetch_profiles_batch works", fp.length === 1 && fp[0].display_name === "Alex R");
const fa = await q("select * from public.voice_fetch_active_batch($1)", [JSON.stringify([{ guild_id: G, user_id: 301 }])]);
check("fetch_active_batch works", fa.length === 1 && fa[0].user_id === 301);
await db.query("insert into public.voice_dm_reports (date,guild_id,user_id) values ('2026-10-01',777,701)");
const fm = await q("select * from public.voice_fetch_dm_report_batch($1)", [JSON.stringify([{ date: "2026-10-01", guild_id: 777, user_id: 701 }, { date: "2026-10-01", guild_id: 777, user_id: 999 }])]);
check("fetch_dm_report_batch returns only existing keys", fm.length === 1 && fm[0].user_id === 701);

// Uniqueness: duplicate source_id and duplicate identity are rejected.
let dupErr = null;
try {
  await db.query("insert into public.voice_sessions (guild_id,user_id,channel_id,started_at,ended_at,duration_seconds,source_id) values (777,701,1,'2026-09-30T12:00:00+00:00','2026-09-30T13:00:00+00:00',3600,42)");
} catch (e) { dupErr = e.message; }
check("duplicate source_id rejected", /voice_sessions_source_id_uidx/.test(dupErr ?? ""), dupErr ?? "no error");
dupErr = null;
try {
  await db.query("insert into public.voice_sessions (guild_id,user_id,channel_id,started_at,ended_at,duration_seconds) values (777,701,1,'2026-09-30T10:00:00+00:00','2026-09-30T11:00:00+00:00',3600)");
} catch (e) { dupErr = e.message; }
check("duplicate session identity rejected", /voice_sessions_identity_uidx/.test(dupErr ?? ""), dupErr ?? "no error");

// ---------------------------------------------------------------------------
console.log("\n== 6. RLS: anon sees nothing, service_role works ==");
// Supabase grants table privileges to anon/authenticated/service_role by
// default; mirror that so we test RLS (not privilege) behaviour.
await db.exec("grant usage on schema public to anon, authenticated, service_role;");
await db.exec("grant select, insert, update, delete on all tables in schema public to anon, authenticated, service_role;");
await db.exec("set role anon");
let anonRows = null;
try {
  anonRows = await q("select count(*) as c from public.voice_daily_stats");
} catch (e) { anonRows = [{ c: "permission denied" }]; }
check("anon SELECT sees zero rows (RLS, no policies)", Number(anonRows[0].c) === 0, JSON.stringify(anonRows));
let anonInsertErr = null;
try {
  await db.query("insert into public.voice_daily_stats (guild_id,user_id,date,duration_seconds) values (1,1,'2026-01-01',1)");
} catch (e) { anonInsertErr = e.message; }
check("anon INSERT blocked by RLS", /row-level security/.test(anonInsertErr ?? ""), anonInsertErr ?? "no error");
let anonFnErr = null;
try {
  await q("select public.voice_leaderboard(555,'today',null)");
} catch (e) { anonFnErr = e.message; }
check("anon cannot execute helper functions (EXECUTE revoked from PUBLIC)",
  /permission denied for function voice_leaderboard/.test(anonFnErr ?? ""), anonFnErr ?? "no error");
await db.exec("reset role");
// service_role (with bypassrls, like the real Supabase role) sees everything.
await db.exec("set role service_role");
const svc = await q("select count(*) as c from public.voice_daily_stats");
check("service_role sees all rows", Number(svc[0].c) > 0);
await q("select public.voice_schema_version()");
await q("select public.voice_record_session(555,901,1,'2026-10-08T10:00:00+00:00','2026-10-08T10:30:00+00:00')");
check("service_role can execute helpers and write", true);
await db.exec("reset role");

// ---------------------------------------------------------------------------
// The two diagnostic queries published in supabase/README.md are executed
// here verbatim, so the documentation cannot drift from the real schema.
console.log("\n== 9. README diagnostic queries ==");
{
  const stalled = await q(`
    select guild_id, user_id, started_at, credited_until,
           now() - coalesce(credited_until, started_at) as uncredited
    from voice_active_sessions
    where now() - coalesce(credited_until, started_at) > interval '15 minutes'
    order by uncredited desc`);
  check("README: checkpoint-health query runs and returns a column set",
    Array.isArray(stalled));
}
{
  const corrections = await q(`
    select guild_id, user_id, old_started_at, new_started_at, recorded_at
    from voice_session_corrections
    order by recorded_at desc
    limit 50`);
  check("README: corrections-audit query runs against the real columns",
    Array.isArray(corrections));
}

// ---------------------------------------------------------------------------
console.log("\n=================================================================");
console.log(`RESULT: ${passed} passed, ${failed} failed`);
if (failed > 0) {
  console.log("Failures:");
  for (const f of failures) console.log("  - " + f);
  process.exit(1);
}
console.log("The migration SQL is valid and behaves as designed.");
console.log("Next: run it once in the Supabase Dashboard (see supabase/README.md).");
