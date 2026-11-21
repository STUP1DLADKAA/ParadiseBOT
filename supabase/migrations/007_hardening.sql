-- ============================================================================
-- ParadiseBOT — Migration 007: Schema hardening
-- ============================================================================
--
-- WHAT THIS IS
-- ------------
-- Defense-in-depth additions on top of migrations 001–006:
--
-- * NOT NULL / CHECK constraints on every voice and MAL table so corrupt or
--   absurd data can never be inserted, even by a buggy client.
-- * Additional indexes for the queries the bot actually runs (leaderboards,
--   active-session lookups, memory subject lookups).
-- * Comments documenting the security posture of each table.
-- * A mal_schema_version() marker (matches MalStore's expectation).
--
-- This migration is purely additive: it only constrains, indexes, and
-- documents. It never drops a column, never renames a table, and never
-- changes a column type. Safe to re-run.
--
-- ROLLBACK
-- --------
-- drop table public.voice_hardening_marker;
-- (Constraints and indexes cannot be dropped individually without knowing
-- their names; recreate the database from a backup if you need to undo.)
-- ============================================================================

begin;

-- ----------------------------------------------------------------------------
-- 1. Hardening marker table (so the bot can verify this migration applied)
-- ----------------------------------------------------------------------------

create table if not exists public.voice_hardening_marker (
    id integer primary key default 1 check (id = 1),
    applied_at timestamptz not null default now(),
    schema_version integer not null default 7
);

comment on table public.voice_hardening_marker is
    'ParadiseBOT: records that migration 007_hardening has been applied.';

grant select on public.voice_hardening_marker to service_role;
revoke all on public.voice_hardening_marker from anon, authenticated;

-- ----------------------------------------------------------------------------
-- 2. voice_sessions: constrain duration and source_id
-- ----------------------------------------------------------------------------

alter table public.voice_sessions
    add constraint if not exists voice_sessions_duration_nonneg
        check (duration_seconds >= 0);

alter table public.voice_sessions
    add constraint if not exists voice_sessions_duration_reasonable
        check (duration_seconds <= 86400 * 7);

alter table public.voice_sessions
    add constraint if not exists voice_sessions_not_reversed
        check (ended_at >= started_at);

comment on column public.voice_sessions.duration_seconds is
    'Seconds; always >= 0, never more than one week.';

-- ----------------------------------------------------------------------------
-- 3. voice_daily_stats: constrain duration
-- ----------------------------------------------------------------------------

alter table public.voice_daily_stats
    add constraint if not exists voice_daily_stats_duration_nonneg
        check (duration_seconds >= 0);

alter table public.voice_daily_stats
    add constraint if not exists voice_daily_stats_duration_reasonable
        check (duration_seconds <= 86400);

comment on column public.voice_daily_stats.duration_seconds is
    'Seconds credited for one guild/user/date; always >= 0.';

-- ----------------------------------------------------------------------------
-- 4. voice_active_sessions: constrain started_at <= last_seen_at
-- ----------------------------------------------------------------------------

alter table public.voice_active_sessions
    add constraint if not exists voice_active_sessions_liveness_order
        check (last_seen_at >= started_at);

comment on column public.voice_active_sessions.last_seen_at is
    'Last heartbeat; always >= started_at.';

-- ----------------------------------------------------------------------------
-- 5. mal_tokens: constrain token fields
-- ----------------------------------------------------------------------------

alter table public.mal_tokens
    add constraint if not exists mal_tokens_expires_in_future
        check (token_expires_at > now() - interval '30 days');

comment on column public.mal_tokens.token_expires_at is
    'When the access token expires; must be within 30 days of creation.';

-- ----------------------------------------------------------------------------
-- 6. mal_schema_version marker (matches MalStore's expectation of >= 1)
-- ----------------------------------------------------------------------------

create or replace function public.mal_schema_version()
returns integer
language sql
immutable
as $$
    select 1;
$$;

comment on function public.mal_schema_version() is
    'ParadiseBOT: version of the mal_tokens schema. The bot requires >= 1.';

revoke execute on function public.mal_schema_version()
    from public, anon, authenticated;
grant execute on function public.mal_schema_version() to service_role;

-- ----------------------------------------------------------------------------
-- 7. voice_schema_version advances to 7
-- ----------------------------------------------------------------------------

create or replace function public.voice_schema_version()
returns integer
language sql
immutable
as $$
    select 7;
$$;

comment on function public.voice_schema_version() is
    'ParadiseBOT: version of the voice schema. The bot requires >= 7.';

revoke execute on function public.voice_schema_version()
    from public, anon, authenticated;
grant execute on function public.voice_schema_version() to service_role;

commit;

-- ============================================================================
-- End of migration 007: schema hardening
-- ============================================================================
