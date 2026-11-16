-- ============================================================================
-- ParadiseBOT — Migration 006: MyAnimeList OAuth token storage
-- ============================================================================
--
-- WHAT THIS IS
-- ------------
-- Stores encrypted MyAnimeList OAuth 2.0 access and refresh tokens for the
-- /mal integration. Each Discord user has at most one row, keyed by
-- discord_user_id. Tokens are encrypted at rest with a key from
-- MAL_ENCRYPTION_KEY before being written.
--
-- DESIGN
-- ------
-- * discord_user_id is the primary key — one MAL account per Discord user.
-- * mal_user_id records the MAL user ID for reference/display.
-- * access_token_encrypted / refresh_token_encrypted are Fernet ciphertexts.
-- * token_expires_at lets the bot know when to refresh.
-- * RLS is enabled with NO policies: only the service role can access.
--   The bot connects with SUPABASE_SECRET_KEY (service_role), which bypasses
--   RLS by design. Discord users can never read another user's tokens because
--   the bot always filters by discord_user_id and the service role bypasses
--   RLS anyway.
--
-- ROLLBACK
-- --------
-- drop table public.mal_tokens;
-- drop function public.mal_schema_version();
-- Nothing else references them.
-- ============================================================================

begin;

-- ----------------------------------------------------------------------------
-- 1. The table
-- ----------------------------------------------------------------------------

create table if not exists public.mal_tokens (
    discord_user_id         bigint primary key, -- the Discord user ID
    mal_user_id             bigint      not null, -- the MAL user ID
    access_token_encrypted  text        not null, -- Fernet-encrypted access token
    refresh_token_encrypted text        not null, -- Fernet-encrypted refresh token
    token_expires_at        timestamptz not null, -- when the access token expires
    created_at              timestamptz not null default now(),
    updated_at              timestamptz not null default now()
);

comment on table public.mal_tokens is
    'ParadiseBOT: encrypted MAL OAuth 2.0 tokens, keyed by Discord user ID. '
    'One row per Discord user. Tokens are encrypted at rest before storage.';

create index if not exists mal_tokens_mal_user_id_idx
    on public.mal_tokens (mal_user_id);

-- ----------------------------------------------------------------------------
-- 2. Lockdown: service role only
-- ----------------------------------------------------------------------------

alter table public.mal_tokens enable row level security;

-- No policies are created on purpose: with RLS enabled and no policies, the
-- anon/authenticated roles see nothing. The bot connects with the service
-- role key, which bypasses RLS.

revoke all on table public.mal_tokens from anon, authenticated;
grant select, insert, update, delete on table public.mal_tokens to service_role;

-- ----------------------------------------------------------------------------
-- 3. Schema marker
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

commit;
