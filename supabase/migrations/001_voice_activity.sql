-- ============================================================================
-- ParadiseBot — Voice Activity schema migration 001 (Supabase / PostgreSQL)
-- ============================================================================
-- Project : "Paradise" (ref: gxcjtbiyamyyxokgehpe, region: ap-south-1)
-- Applies : ONE TIME, manually, in the Supabase Dashboard → SQL Editor.
-- Version : 1  (reported to the bot by public.voice_schema_version())
--
-- WHAT THIS CREATES
--   Tables (all in schema `public`, all RLS-enabled with NO policies):
--     voice_sessions          one row per completed VC session (history)
--     voice_daily_stats       the authoritative daily aggregate (integer seconds)
--     voice_active_sessions   one row per user currently in VC (restart safety)
--     voice_member_profiles   last-known Discord identity per (guild, user)
--     voice_dm_reports        daily DM report sent-state
--     voice_migration_state   exactly-once ledger for the SQLite migration
--
--   Helper functions (all prefixed voice_):
--     voice_schema_version()            schema marker checked by the bot
--     voice_record_session(...)         atomic session insert + midnight split
--     voice_close_active_session(...)   atomic close (record + delete row)
--     voice_heartbeat_active(jsonb)     batched last_seen_at upsert (LWW)
--     voice_delete_active_if_older(...) guarded active-row delete
--     voice_leaderboard(...)            today/yesterday/week/month/all_time
--     voice_user_breakdown(...)         per-user 5-period aggregation
--     voice_daily_server_total(...)     guild total for one day
--     voice_daily_records(...)          all guild/user records for one day
--     voice_upsert_profiles(jsonb)      batched identity upsert
--     voice_merge_daily_batch(jsonb)    idempotent migration merge (ledger)
--     voice_fetch_*_batch(...)          migration verification reads
--
-- DESIGN NOTES
--   * Durations are plain INTEGER seconds. No intervals, no floats, no JSON.
--   * Discord snowflakes are BIGINT (exact, no precision loss).
--   * Days are DATE; event times are TIMESTAMPTZ (UTC).
--   * The bot connects with the server-side SECRET key (service_role),
--     which bypasses RLS. anon/authenticated get nothing: RLS is enabled
--     with zero policies, and EXECUTE is revoked from them on every helper.
--   * "This Week"/"This Month" intentionally match the bot's historical
--     rolling-window definitions:
--       week  = dates between (today - 7) and today, inclusive  (8 days)
--       month = dates between (today - 30) and today, inclusive (31 days)
--     Do NOT "fix" these to calendar weeks/months — that would silently
--     change leaderboards.
--
-- The whole script runs in a single transaction: it either fully applies
-- or not at all. It is safe to re-run (IF NOT EXISTS / OR REPLACE).
-- ============================================================================

begin;

-- ----------------------------------------------------------------------------
-- 1. Tables
-- ----------------------------------------------------------------------------

-- One row per COMPLETED voice session. Historical record; never aggregated
-- away. `source_id` carries the old SQLite row id for migration idempotency
-- (NULL for sessions recorded live by the bot).
create table if not exists public.voice_sessions (
    id               bigint generated always as identity primary key,
    guild_id         bigint not null,
    user_id          bigint not null,
    channel_id       bigint not null,
    started_at       timestamptz not null,
    ended_at         timestamptz not null,
    duration_seconds integer not null check (duration_seconds >= 0),
    source_id        bigint,
    created_at       timestamptz not null default now()
);
comment on table public.voice_sessions is
    'ParadiseBot: one row per completed voice session (full history).';
comment on column public.voice_sessions.duration_seconds is
    'Session length in whole seconds (integer, never negative).';
comment on column public.voice_sessions.source_id is
    'Old SQLite row id during migration; NULL for live-recorded sessions.';

-- Migration dedupe by old SQLite id (NULLs never conflict).
create unique index if not exists voice_sessions_source_id_uidx
    on public.voice_sessions (source_id);
-- Retry-safe identity for live sessions: the same close event replayed
-- (network blip, restart recovery) can never be counted twice.
create unique index if not exists voice_sessions_identity_uidx
    on public.voice_sessions (guild_id, user_id, started_at, ended_at);

-- The authoritative daily aggregate. This is what /vc leaderboard and
-- /vc stats read, and what you can hand-edit in the Table Editor.
create table if not exists public.voice_daily_stats (
    guild_id         bigint not null,
    user_id          bigint not null,
    date             date   not null,
    duration_seconds integer not null default 0 check (duration_seconds >= 0),
    updated_at       timestamptz not null default now(),
    primary key (guild_id, user_id, date)
);
comment on table public.voice_daily_stats is
    'ParadiseBot: authoritative daily voice totals in seconds. Editing duration_seconds here is reflected by /vc immediately.';
comment on column public.voice_daily_stats.duration_seconds is
    'Voice time for this (guild, user, UTC day) in whole seconds.';

-- Covers the leaderboard queries: single-day boards and rolling windows.
-- (guild_id, user_id, date) lookups are served by the primary key itself.
create index if not exists voice_daily_stats_guild_date_idx
    on public.voice_daily_stats (guild_id, date);

-- Currently active sessions: exactly one compact row per (guild, user).
-- Heartbeats UPDATE this row; no history is kept here.
create table if not exists public.voice_active_sessions (
    guild_id     bigint not null,
    user_id      bigint not null,
    channel_id   bigint not null,
    started_at   timestamptz not null,
    last_seen_at timestamptz not null,
    is_afk       boolean not null default false,
    updated_at   timestamptz not null default now(),
    primary key (guild_id, user_id)
);
comment on table public.voice_active_sessions is
    'ParadiseBot: users currently in voice. One row per user; heartbeats only bump last_seen_at.';

-- Last-known Discord identity so departed members keep their names/avatars
-- on leaderboards. No snapshot history — just the latest per (guild, user).
create table if not exists public.voice_member_profiles (
    guild_id     bigint not null,
    user_id      bigint not null,
    display_name text not null,
    username     text not null,
    avatar_url   text,
    updated_at   timestamptz not null default now(),
    primary key (guild_id, user_id)
);
comment on table public.voice_member_profiles is
    'ParadiseBot: last-known Discord identity per member (survives them leaving the server).';

-- Daily DM report sent-state; the unique key makes double reports impossible.
create table if not exists public.voice_dm_reports (
    date     date   not null,
    guild_id bigint not null,
    user_id  bigint not null,
    sent_at  timestamptz not null default now(),
    primary key (date, guild_id, user_id)
);
comment on table public.voice_dm_reports is
    'ParadiseBot: which daily voice report DMs have already been sent.';

-- Exactly-once ledger for the SQLite -> Supabase migration of
-- voice_daily_stats. applied_seconds is the amount of the SQLite value that
-- has already been merged into voice_daily_stats for that key, so a retried
-- batch merges only the difference (see voice_merge_daily_batch).
-- Safe to truncate/drop AFTER the migration has fully completed.
create table if not exists public.voice_migration_state (
    guild_id        bigint not null,
    user_id         bigint not null,
    date            date   not null,
    applied_seconds integer not null default 0,
    applied_at      timestamptz not null default now(),
    primary key (guild_id, user_id, date)
);
comment on table public.voice_migration_state is
    'ParadiseBot migration ledger: how much of each old SQLite daily total has been merged. Can be dropped once migration finished.';

-- ----------------------------------------------------------------------------
-- 2. Row Level Security
-- ----------------------------------------------------------------------------
-- The bot is a trusted server-side process using the project's SECRET key
-- (Postgres role service_role), which carries BYPASSRLS and is never exposed
-- to browsers or Discord users. Therefore: RLS enabled, ZERO policies.
--   anon          -> sees/changes nothing
--   authenticated -> sees/changes nothing
--   service_role  -> full access (the bot)
--   dashboard     -> you, as project owner, via the postgres role
-- No public/insecure policy exists for these tables.

alter table public.voice_sessions        enable row level security;
alter table public.voice_daily_stats     enable row level security;
alter table public.voice_active_sessions enable row level security;
alter table public.voice_member_profiles enable row level security;
alter table public.voice_dm_reports      enable row level security;
alter table public.voice_migration_state enable row level security;

-- ----------------------------------------------------------------------------
-- 3. Schema version marker
-- ----------------------------------------------------------------------------
-- The bot calls this at startup. Bump it (and add 002_*.sql) whenever the
-- schema changes so the bot can refuse to run against a stale database.

create or replace function public.voice_schema_version()
returns integer
language sql
immutable
as $$
    select 1;
$$;

-- ----------------------------------------------------------------------------
-- 4. Write-path functions
-- ----------------------------------------------------------------------------

-- Record one completed session and split it across UTC midnights into the
-- daily aggregate — atomically, and safe to retry:
--   * duplicate (guild, user, started_at, ended_at) inserts are ignored and
--     do NOT re-increment the daily totals;
--   * the daily increments only run when the session row was actually new.
create or replace function public.voice_record_session(
    p_guild_id   bigint,
    p_user_id    bigint,
    p_channel_id bigint,
    p_started_at timestamptz,
    p_ended_at   timestamptz
)
returns integer
language plpgsql
as $$
declare
    v_start_ts  timestamp := p_started_at at time zone 'utc';
    v_end_ts    timestamp := p_ended_at   at time zone 'utc';
    v_duration  integer;
    v_inserted  boolean := false;
begin
    if p_started_at is null or p_ended_at is null or p_ended_at <= p_started_at then
        return 0;
    end if;

    -- Integer seconds, truncated (matches the bot's own computation).
    v_duration := floor(extract(epoch from (v_end_ts - v_start_ts)))::integer;
    if v_duration <= 0 then
        return 0;
    end if;

    insert into public.voice_sessions
        (guild_id, user_id, channel_id, started_at, ended_at, duration_seconds)
    values
        (p_guild_id, p_user_id, p_channel_id, p_started_at, p_ended_at, v_duration)
    on conflict (guild_id, user_id, started_at, ended_at) do nothing
    returning true into v_inserted;

    -- PL/pgSQL assigns NULL (not false) when the conflict skipped the insert.
    if v_inserted is not true then
        return 0;  -- retried event: already recorded, do not double-count
    end if;

    -- Split across UTC day boundaries: 23:50 -> 00:20 becomes 600s + 1200s.
    -- All arithmetic happens in naive-UTC so the session timezone never
    -- affects the result.
    insert into public.voice_daily_stats (guild_id, user_id, date, duration_seconds)
    select
        p_guild_id,
        p_user_id,
        d::date,
        floor(extract(epoch from (least(v_end_ts, d + interval '1 day')
                                   - greatest(v_start_ts, d))))::integer
    from generate_series(v_start_ts::date, v_end_ts::date, interval '1 day') as d
    where floor(extract(epoch from (least(v_end_ts, d + interval '1 day')
                                     - greatest(v_start_ts, d))))::integer > 0
    on conflict (guild_id, user_id, date) do update
        set duration_seconds = public.voice_daily_stats.duration_seconds
                                + excluded.duration_seconds,
            updated_at = now();

    return v_duration;
end;
$$;

-- Atomically close an active session: uses the persisted row's own
-- started_at / channel, records the session (voice_record_session),
-- and deletes the active row — all in one transaction. Returns the number
-- of seconds recorded, or NULL when there is no active row for that user.
-- AFK rows are deleted without recording (AFK time is never tracked).
create or replace function public.voice_close_active_session(
    p_guild_id bigint,
    p_user_id  bigint,
    p_ended_at timestamptz
)
returns integer
language plpgsql
as $$
declare
    v_row      public.voice_active_sessions%rowtype;
    v_recorded integer;
begin
    if p_ended_at is null then
        return null;
    end if;

    select * into v_row
    from public.voice_active_sessions
    where guild_id = p_guild_id and user_id = p_user_id
    for update;

    if not found then
        return null;
    end if;

    delete from public.voice_active_sessions
    where guild_id = p_guild_id and user_id = p_user_id;

    if v_row.is_afk then
        return 0;  -- AFK time is never counted
    end if;

    v_recorded := public.voice_record_session(
        p_guild_id, p_user_id, v_row.channel_id, v_row.started_at, p_ended_at
    );
    return v_recorded;
end;
$$;

-- Batched heartbeat: upserts the CURRENT state of active sessions.
-- Last-write-wins on last_seen_at, so a delayed/reordered heartbeat can
-- never clobber fresher data. One compact row per user — no history rows.
create or replace function public.voice_heartbeat_active(p_rows jsonb)
returns integer
language sql
as $$
    with incoming as (
        select
            (r->>'guild_id')::bigint     as guild_id,
            (r->>'user_id')::bigint      as user_id,
            (r->>'channel_id')::bigint   as channel_id,
            (r->>'started_at')::timestamptz as started_at,
            (r->>'last_seen_at')::timestamptz as last_seen_at,
            coalesce((r->>'is_afk')::boolean, false) as is_afk
        from jsonb_array_elements(p_rows) as r
    ),
    applied as (
        insert into public.voice_active_sessions
            (guild_id, user_id, channel_id, started_at, last_seen_at, is_afk, updated_at)
        select guild_id, user_id, channel_id, started_at, last_seen_at, is_afk, now()
        from incoming
        on conflict (guild_id, user_id) do update
            set channel_id   = excluded.channel_id,
                started_at   = excluded.started_at,
                last_seen_at = excluded.last_seen_at,
                is_afk       = excluded.is_afk,
                updated_at   = now()
        where excluded.last_seen_at >= public.voice_active_sessions.last_seen_at
        returning 1
    )
    select count(*)::integer from applied;
$$;

-- Guarded delete: removes the active row only if it belongs to the session
-- being closed (started_at <= p_started_before), so an out-of-order retry
-- can never delete a newer session's row. NULL threshold = unconditional.
create or replace function public.voice_delete_active_if_older(
    p_guild_id        bigint,
    p_user_id         bigint,
    p_started_before  timestamptz
)
returns integer
language sql
as $$
    with deleted as (
        delete from public.voice_active_sessions
        where guild_id = p_guild_id
          and user_id = p_user_id
          and (p_started_before is null or started_at <= p_started_before)
        returning 1
    )
    select count(*)::integer from deleted;
$$;

-- Batched identity upsert. Mirrors the bot's historic SQLite semantics:
--   * keeps the stored avatar when the incoming one is NULL;
--   * only rewrites the row when a visible value actually changed;
--   * last-write-wins by updated_at (migration rows carry their SQLite
--     timestamp so they never clobber a fresher live profile).
create or replace function public.voice_upsert_profiles(p_rows jsonb)
returns integer
language sql
as $$
    with incoming as (
        select
            (r->>'guild_id')::bigint     as guild_id,
            (r->>'user_id')::bigint      as user_id,
            r->>'display_name'           as display_name,
            r->>'username'               as username,
            nullif(r->>'avatar_url', '') as avatar_url,
            coalesce((r->>'updated_at')::timestamptz, now()) as updated_at
        from jsonb_array_elements(p_rows) as r
    ),
    applied as (
        insert into public.voice_member_profiles
            (guild_id, user_id, display_name, username, avatar_url, updated_at)
        select guild_id, user_id, display_name, username, avatar_url, updated_at
        from incoming
        on conflict (guild_id, user_id) do update
            set display_name = excluded.display_name,
                username     = excluded.username,
                avatar_url   = coalesce(excluded.avatar_url,
                                        public.voice_member_profiles.avatar_url),
                updated_at   = excluded.updated_at
        where (
                public.voice_member_profiles.display_name is distinct from excluded.display_name
             or public.voice_member_profiles.username     is distinct from excluded.username
             or (
                    excluded.avatar_url is not null
                and public.voice_member_profiles.avatar_url is distinct from excluded.avatar_url
                )
            )
          and excluded.updated_at >= public.voice_member_profiles.updated_at
        returning 1
    )
    select count(*)::integer from applied;
$$;

-- ----------------------------------------------------------------------------
-- 5. Read-path functions (leaderboards & reports)
-- ----------------------------------------------------------------------------

-- Ranked leaderboard for one guild. Periods EXACTLY as the bot has always
-- defined them (all boundaries in UTC, 'today' = p_target_date):
--   today      date = target
--   yesterday  date = target - 1
--   week       date between target - 7 and target   (rolling 8-day window)
--   month      date between target - 30 and target  (rolling 31-day window)
--   all_time   every date
create or replace function public.voice_leaderboard(
    p_guild_id    bigint,
    p_period      text,
    p_target_date date default null
)
returns table (user_id bigint, total_seconds bigint)
language sql
stable
as $$
    select v.user_id, sum(v.duration_seconds)::bigint as total
    from public.voice_daily_stats v
    where v.guild_id = p_guild_id
      and case
            when p_period = 'today' then
                v.date = coalesce(p_target_date, (now() at time zone 'utc')::date)
            when p_period = 'yesterday' then
                v.date = coalesce(p_target_date, (now() at time zone 'utc')::date) - 1
            when p_period = 'week' then
                v.date between coalesce(p_target_date, (now() at time zone 'utc')::date) - 7
                            and coalesce(p_target_date, (now() at time zone 'utc')::date)
            when p_period = 'month' then
                v.date between coalesce(p_target_date, (now() at time zone 'utc')::date) - 30
                            and coalesce(p_target_date, (now() at time zone 'utc')::date)
            when p_period = 'all_time' then true
            else false
          end
    group by v.user_id
    having sum(v.duration_seconds) > 0
    order by 2 desc, 1 asc;
$$;

-- One user's totals across every period, computed in a single scan.
create or replace function public.voice_user_breakdown(
    p_guild_id    bigint,
    p_user_id     bigint,
    p_target_date date default null
)
returns table (
    today_seconds     bigint,
    yesterday_seconds bigint,
    week_seconds      bigint,
    month_seconds     bigint,
    all_time_seconds  bigint
)
language sql
stable
as $$
    with bounds as (
        select coalesce(p_target_date, (now() at time zone 'utc')::date)::date as today_d
    )
    select
        coalesce(sum(case when v.date =  b.today_d       then v.duration_seconds else 0 end), 0)::bigint,
        coalesce(sum(case when v.date =  b.today_d - 1   then v.duration_seconds else 0 end), 0)::bigint,
        coalesce(sum(case when v.date between b.today_d - 7  and b.today_d
                          then v.duration_seconds else 0 end), 0)::bigint,
        coalesce(sum(case when v.date between b.today_d - 30 and b.today_d
                          then v.duration_seconds else 0 end), 0)::bigint,
        coalesce(sum(v.duration_seconds), 0)::bigint
    from public.voice_daily_stats v
    cross join bounds b
    where v.guild_id = p_guild_id
      and v.user_id = p_user_id;
$$;

-- Total tracked voice time for one guild on one day (daily DM reports).
create or replace function public.voice_daily_server_total(
    p_guild_id bigint,
    p_day      date
)
returns bigint
language sql
stable
as $$
    select coalesce(sum(v.duration_seconds), 0)::bigint
    from public.voice_daily_stats v
    where v.guild_id = p_guild_id
      and v.date = p_day;
$$;

-- Every positive (guild, user, seconds) record for one day, all guilds.
create or replace function public.voice_daily_records(p_day date)
returns table (guild_id bigint, user_id bigint, duration_seconds integer)
language sql
stable
as $$
    select v.guild_id, v.user_id, v.duration_seconds
    from public.voice_daily_stats v
    where v.date = p_day
      and v.duration_seconds > 0;
$$;

-- ----------------------------------------------------------------------------
-- 6. Migration functions (used only by the SQLite -> Supabase migrator)
-- ----------------------------------------------------------------------------

-- Exactly-once daily merge. For each incoming row:
--   delta = incoming_seconds - already_applied_seconds
--   voice_daily_stats.duration_seconds += delta
--   ledger.applied_seconds = incoming_seconds
-- Retrying a batch therefore merges 0 extra seconds, while live activity
-- recorded in between is preserved untouched.
create or replace function public.voice_merge_daily_batch(p_rows jsonb)
returns jsonb
language plpgsql
as $$
declare
    r         jsonb;
    v_guild   bigint;
    v_user    bigint;
    v_day     date;
    v_seconds integer;
    v_prev    integer;
    v_result  integer;
    v_results jsonb := '[]'::jsonb;
begin
    for r in select * from jsonb_array_elements(p_rows)
    loop
        v_guild   := (r->>'guild_id')::bigint;
        v_user    := (r->>'user_id')::bigint;
        v_day     := (r->>'date')::date;
        v_seconds := (r->>'duration_seconds')::integer;

        select m.applied_seconds into v_prev
        from public.voice_migration_state m
        where m.guild_id = v_guild and m.user_id = v_user and m.date = v_day;

        if v_prev is null then
            insert into public.voice_migration_state
                (guild_id, user_id, date, applied_seconds, applied_at)
            values (v_guild, v_user, v_day, v_seconds, now());
            v_prev := 0;
        else
            update public.voice_migration_state m
            set applied_seconds = v_seconds,
                applied_at = now()
            where m.guild_id = v_guild and m.user_id = v_user and m.date = v_day;
        end if;

        insert into public.voice_daily_stats (guild_id, user_id, date, duration_seconds, updated_at)
        values (v_guild, v_user, v_day, v_seconds, now())
        on conflict (guild_id, user_id, date) do update
            set duration_seconds = public.voice_daily_stats.duration_seconds
                                    + (excluded.duration_seconds - v_prev),
                updated_at = now();

        select d.duration_seconds into v_result
        from public.voice_daily_stats d
        where d.guild_id = v_guild and d.user_id = v_user and d.date = v_day;

        v_results := v_results || jsonb_build_object(
            'guild_id', v_guild,
            'user_id', v_user,
            'date', to_char(v_day, 'YYYY-MM-DD'),
            'resulting_seconds', v_result
        );
    end loop;

    return v_results;
end;
$$;

-- Verification reads for the migrator (batched by key; avoids URL limits).
create or replace function public.voice_fetch_sessions_batch(p_source_ids bigint[])
returns table (
    source_id       bigint,
    guild_id        bigint,
    user_id         bigint,
    channel_id      bigint,
    started_at      timestamptz,
    ended_at        timestamptz,
    duration_seconds integer
)
language sql
stable
as $$
    select s.source_id, s.guild_id, s.user_id, s.channel_id,
           s.started_at, s.ended_at, s.duration_seconds
    from public.voice_sessions s
    where s.source_id = any(p_source_ids);
$$;

create or replace function public.voice_fetch_daily_batch(p_rows jsonb)
returns table (
    guild_id         bigint,
    user_id          bigint,
    date             date,
    duration_seconds integer,
    updated_at       timestamptz
)
language sql
stable
as $$
    select d.guild_id, d.user_id, d.date, d.duration_seconds, d.updated_at
    from public.voice_daily_stats d
    join (
        select (r->>'guild_id')::bigint as guild_id,
               (r->>'user_id')::bigint  as user_id,
               (r->>'date')::date       as date
        from jsonb_array_elements(p_rows) as r
    ) k on d.guild_id = k.guild_id and d.user_id = k.user_id and d.date = k.date;
$$;

create or replace function public.voice_fetch_profiles_batch(p_keys jsonb)
returns table (
    guild_id     bigint,
    user_id      bigint,
    display_name text,
    username     text,
    avatar_url   text,
    updated_at   timestamptz
)
language sql
stable
as $$
    select p.guild_id, p.user_id, p.display_name, p.username, p.avatar_url, p.updated_at
    from public.voice_member_profiles p
    join (
        select (r->>'guild_id')::bigint as guild_id,
               (r->>'user_id')::bigint  as user_id
        from jsonb_array_elements(p_keys) as r
    ) k on p.guild_id = k.guild_id and p.user_id = k.user_id;
$$;

create or replace function public.voice_fetch_active_batch(p_keys jsonb)
returns table (
    guild_id     bigint,
    user_id      bigint,
    channel_id   bigint,
    started_at   timestamptz,
    last_seen_at timestamptz,
    is_afk       boolean
)
language sql
stable
as $$
    select a.guild_id, a.user_id, a.channel_id, a.started_at, a.last_seen_at, a.is_afk
    from public.voice_active_sessions a
    join (
        select (r->>'guild_id')::bigint as guild_id,
               (r->>'user_id')::bigint  as user_id
        from jsonb_array_elements(p_keys) as r
    ) k on a.guild_id = k.guild_id and a.user_id = k.user_id;
$$;

create or replace function public.voice_fetch_dm_report_batch(p_keys jsonb)
returns table (
    date     date,
    guild_id bigint,
    user_id  bigint,
    sent_at  timestamptz
)
language sql
stable
as $$
    select m.date, m.guild_id, m.user_id, m.sent_at
    from public.voice_dm_reports m
    join (
        select (r->>'date')::date      as date,
               (r->>'guild_id')::bigint as guild_id,
               (r->>'user_id')::bigint  as user_id
        from jsonb_array_elements(p_keys) as r
    ) k on m.date = k.date and m.guild_id = k.guild_id and m.user_id = k.user_id;
$$;

-- ----------------------------------------------------------------------------
-- 7. Lock the helper functions down to the server-side roles
-- ----------------------------------------------------------------------------
-- Functions are executable by the PUBLIC pseudo-role by default, so revoking
-- from anon/authenticated alone is not enough. Revoke from PUBLIC as well,
-- then grant EXECUTE back to service_role only (the bot's server-side
-- identity; the Dashboard's postgres role owns the functions anyway).

revoke execute on function public.voice_schema_version()         from public, anon, authenticated;
revoke execute on function public.voice_record_session(bigint, bigint, bigint, timestamptz, timestamptz) from public, anon, authenticated;
revoke execute on function public.voice_close_active_session(bigint, bigint, timestamptz) from public, anon, authenticated;
revoke execute on function public.voice_heartbeat_active(jsonb)  from public, anon, authenticated;
revoke execute on function public.voice_delete_active_if_older(bigint, bigint, timestamptz) from public, anon, authenticated;
revoke execute on function public.voice_upsert_profiles(jsonb)   from public, anon, authenticated;
revoke execute on function public.voice_leaderboard(bigint, text, date) from public, anon, authenticated;
revoke execute on function public.voice_user_breakdown(bigint, bigint, date) from public, anon, authenticated;
revoke execute on function public.voice_daily_server_total(bigint, date) from public, anon, authenticated;
revoke execute on function public.voice_daily_records(date)      from public, anon, authenticated;
revoke execute on function public.voice_merge_daily_batch(jsonb) from public, anon, authenticated;
revoke execute on function public.voice_fetch_sessions_batch(bigint[]) from public, anon, authenticated;
revoke execute on function public.voice_fetch_daily_batch(jsonb) from public, anon, authenticated;
revoke execute on function public.voice_fetch_profiles_batch(jsonb) from public, anon, authenticated;
revoke execute on function public.voice_fetch_active_batch(jsonb) from public, anon, authenticated;
revoke execute on function public.voice_fetch_dm_report_batch(jsonb) from public, anon, authenticated;

grant execute on function public.voice_schema_version()         to service_role;
grant execute on function public.voice_record_session(bigint, bigint, bigint, timestamptz, timestamptz) to service_role;
grant execute on function public.voice_close_active_session(bigint, bigint, timestamptz) to service_role;
grant execute on function public.voice_heartbeat_active(jsonb)  to service_role;
grant execute on function public.voice_delete_active_if_older(bigint, bigint, timestamptz) to service_role;
grant execute on function public.voice_upsert_profiles(jsonb)   to service_role;
grant execute on function public.voice_leaderboard(bigint, text, date) to service_role;
grant execute on function public.voice_user_breakdown(bigint, bigint, date) to service_role;
grant execute on function public.voice_daily_server_total(bigint, date) to service_role;
grant execute on function public.voice_daily_records(date)      to service_role;
grant execute on function public.voice_merge_daily_batch(jsonb) to service_role;
grant execute on function public.voice_fetch_sessions_batch(bigint[]) to service_role;
grant execute on function public.voice_fetch_daily_batch(jsonb) to service_role;
grant execute on function public.voice_fetch_profiles_batch(jsonb) to service_role;
grant execute on function public.voice_fetch_active_batch(jsonb) to service_role;
grant execute on function public.voice_fetch_dm_report_batch(jsonb) to service_role;

commit;

-- ============================================================================
-- Done. Verify with:
--   select public.voice_schema_version();   -- should return 1
-- The bot performs the same check automatically at startup.
-- ============================================================================
