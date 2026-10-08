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

// Newer heartbeat may move started_at backwards (restart recovery resumes).
await scalar("select public.voice_heartbeat_active($1)", [JSON.stringify([
  { guild_id: G, user_id: 301, channel_id: C, started_at: "2026-10-05T09:30:00+00:00", last_seen_at: "2026-10-05T10:25:00+00:00", is_afk: false },
])]);
check("newer heartbeat can resume an older started_at",
  norm(await scalar("select started_at from public.voice_active_sessions where guild_id=$1 and user_id=301", [G])) === "2026-10-05T09:30:00.000Z");

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
console.log("\n=================================================================");
console.log(`RESULT: ${passed} passed, ${failed} failed`);
if (failed > 0) {
  console.log("Failures:");
  for (const f of failures) console.log("  - " + f);
  process.exit(1);
}
console.log("The migration SQL is valid and behaves as designed.");
console.log("Next: run it once in the Supabase Dashboard (see supabase/README.md).");
