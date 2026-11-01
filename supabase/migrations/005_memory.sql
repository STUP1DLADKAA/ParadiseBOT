-- ============================================================================
-- ParadiseBOT — Memory, migration 005: the bot's long-term memory
-- ============================================================================
--
-- WHAT THIS IS
-- ------------
-- The /memory command (lord-only) stores facts the bot should remember
-- forever: "Rohan spent more than a year to reach level 100 in murder
-- mystery". Those memories are folded into the AI's answers from then on —
-- and memories *about a member* surface whenever that member talks to the
-- bot, so the reply can reference them at exactly the right moment.
--
-- SQLite (data/memory.db) is always the working copy and needs no setup.
-- This table is the optional mirror: when SUPABASE_URL / SUPABASE_SECRET_KEY
-- are configured, the bot copies every memory here (and back), so the
-- memories survive a reinstall or a move to another machine, and a row
-- deleted in the Dashboard disappears from the bot on the next sync pass.
--
-- DESIGN
-- ------
-- * The local SQLite row id IS the primary key here. SQLite is the only
--   writer (one bot, one process), so ids never collide and every sync step
--   is a plain idempotent upsert/delete-by-id.
-- * subject_id / subject_name mark who a memory is ABOUT (0 = server-wide).
--   subject_name is a display-name snapshot for prompts and /memory list;
--   the id is the stable identity behind it.
-- * Row-level security is enabled with NO policies: only the service role
--   (SUPABASE_SECRET_KEY, used server-side by the bot) can read or write.
--   The table holds no secrets, but it is the server's private lore.
-- * The cap lives in the bot (MEMORY_MAX_ENTRIES, default 200); the table
--   itself is unbounded so a manual Dashboard edit is never silently lost.
--
-- ROLLBACK
-- --------
-- drop table public.paradise_memories;
-- drop function public.paradise_memory_schema_version();
-- Nothing else references them. The bot detects the missing table and keeps
-- working from local storage alone.
-- ============================================================================

begin;

-- ----------------------------------------------------------------------------
-- 1. The table
-- ----------------------------------------------------------------------------

create table if not exists public.paradise_memories (
    id           bigint primary key,             -- the local SQLite row id
    text         text        not null,           -- the remembered fact
    author_id    bigint      not null default 0, -- who taught the bot (the lord)
    subject_id   bigint      not null default 0, -- who it is ABOUT (0 = server-wide)
    subject_name text        not null default '',-- display-name snapshot
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);

comment on table public.paradise_memories is
    'ParadiseBOT: the long-term memory behind /memory — facts the AI folds into its answers. id mirrors the bot''s local SQLite row id; subject_id marks who a memory is about.';

create index if not exists paradise_memories_subject_idx
    on public.paradise_memories (subject_id);

-- ----------------------------------------------------------------------------
-- 2. Lockdown: service role only
-- ----------------------------------------------------------------------------

alter table public.paradise_memories enable row level security;

-- No policies are created on purpose: with RLS enabled and no policies, the
-- anon/authenticated roles see nothing. The bot connects with the service
-- role key, which bypasses RLS.

revoke all on table public.paradise_memories from anon, authenticated;
grant select, insert, update, delete on table public.paradise_memories to service_role;

-- ----------------------------------------------------------------------------
-- 3. Schema marker (the bot verifies this before mirroring)
-- ----------------------------------------------------------------------------

create or replace function public.paradise_memory_schema_version()
returns integer
language sql
immutable
as $$
    select 1;
$$;

comment on function public.paradise_memory_schema_version() is
    'ParadiseBOT: version of the paradise_memories schema. The bot requires >= 1 to mirror memories.';

-- House style (see 001): EXECUTE is revoked from everyone but the service
-- role, so anon cannot even probe the schema version.
revoke execute on function public.paradise_memory_schema_version()
    from public, anon, authenticated;
grant execute on function public.paradise_memory_schema_version() to service_role;

commit;
