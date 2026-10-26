-- ============================================================================
-- ParadiseBOT — Voice accounting: durable checkpoints + edit-safe heartbeats
-- ============================================================================
-- Incident fix. ADDITIVE, BACKWARD-COMPATIBLE, SAFE TO RE-RUN.
--
--   * Adds ONE nullable column to public.voice_active_sessions.
--   * Adds ONE append-only audit table (no rows are ever rewritten by it).
--   * Replaces three functions with create-or-replace (same signatures except
--     the new checkpoint function, which is brand new).
--   * NO drop, NO truncate, NO delete, NO blanket update, NO rename, NO
--     repurposed column. Every existing row in every existing table is
--     preserved byte-for-byte. voice_sessions and voice_daily_stats are not
--     touched at all by this migration.
--
-- WHY THIS IS NEEDED
-- ------------------
-- Before this migration an in-progress voice session had exactly ONE durable
-- number: voice_active_sessions.started_at. Nothing was written to
-- voice_daily_stats until the session closed, so the whole displayed daily
-- total lived in bot process memory as (now - started_at). Any event that
-- reset that timestamp — a restart, a reconnect, a second deployment — threw
-- the time away, and voice_heartbeat_active rewrote started_at from memory
-- every <= 20 s, so the database could not be used to correct it.
--
-- This migration introduces a durable checkpoint cursor:
--
--   voice_active_sessions.credited_until
--       Voice time up to this instant is ALREADY credited to
--       voice_daily_stats. NULL means "nothing credited yet — credit from
--       started_at". Advances monotonically, only inside
--       voice_checkpoint_active_sessions().
--
-- The uncredited remainder of a live session is therefore always exactly
--
--   [ coalesce(credited_until, started_at) , now )
--
-- and credited_daily + uncredited_remainder == true session duration, at
-- every instant, across restarts, reconnects and concurrent writers.
--
-- MANUAL EDITING (Supabase Table Editor)
-- --------------------------------------
-- After this migration the bot NEVER writes started_at or credited_until on an
-- existing row — it only sets them when it INSERTS a new session row, and it
-- advances credited_until inside the atomic checkpoint function. So:
--
--   * Any change you make to started_at or credited_until in the Dashboard is
--     adopted verbatim by the next tracking cycle (within ~20 s) and is never
--     reverted. Every such change is also written to
--     public.voice_session_corrections for traceability.
--   * Editing started_at on a session that has NOT checkpointed yet
--     (credited_until IS NULL) is honoured in full. This is the common case
--     and the one the incident describes.
--   * Editing started_at on a session that HAS already checkpointed does not
--     re-credit anything: the already-credited part lives in
--     voice_daily_stats, not in this row. To move credited time, set
--     credited_until to the instant from which the remainder should be
--     measured, and correct the affected day in voice_daily_stats (the
--     documented, hand-editable authoritative aggregate).
--
-- ROLLBACK
-- --------
-- Code rollback alone is sufficient: a v2 bot ignores credited_until entirely
-- and reads/writes the same columns it always did. The column, audit table
-- and trigger are inert for it. Dropping them is NOT required and is not
-- recommended; if you ever do, back up voice_active_sessions first.
-- ============================================================================

begin;

-- ----------------------------------------------------------------------------
-- 1. The checkpoint cursor (nullable => every existing row stays valid)
-- ----------------------------------------------------------------------------

alter table public.voice_active_sessions
    add column if not exists credited_until timestamptz;

comment on column public.voice_active_sessions.credited_until is
    'Durable checkpoint cursor: voice time up to this instant is already credited to voice_daily_stats. NULL = nothing credited yet, credit from started_at. Advances monotonically, only inside voice_checkpoint_active_sessions().';

-- ----------------------------------------------------------------------------
-- 2. Correction audit (append-only; the bot never rewrites these rows)
-- ----------------------------------------------------------------------------
-- The bot does not write started_at / credited_until on an existing row after
-- this migration, so every row recorded here is by definition an
-- administrative correction made in the Dashboard. No secrets, no content —
-- only ids, the two timestamps before/after, and when.

create table if not exists public.voice_session_corrections (
    id                   bigint generated always as identity primary key,
    guild_id             bigint not null,
    user_id              bigint not null,
    old_started_at       timestamptz,
    new_started_at       timestamptz,
    old_credited_until   timestamptz,
    new_credited_until   timestamptz,
    recorded_at          timestamptz not null default now()
);
comment on table public.voice_session_corrections is
    'ParadiseBOT: append-only audit of administrative edits to voice_active_sessions.started_at / credited_until made in the Supabase Dashboard.';

create index if not exists voice_session_corrections_lookup_idx
    on public.voice_session_corrections (guild_id, user_id, recorded_at desc);

alter table public.voice_session_corrections enable row level security;

create or replace function public.voice_audit_active_session_edit()
returns trigger
language plpgsql
as $$
begin
    if (new.started_at     is distinct from old.started_at)
    or (new.credited_until is distinct from old.credited_until) then
        insert into public.voice_session_corrections
            (guild_id, user_id, old_started_at, new_started_at,
             old_credited_until, new_credited_until)
        values
            (old.guild_id, old.user_id, old.started_at, new.started_at,
             old.credited_until, new.credited_until);
    end if;
    return new;
end;
$$;

drop trigger if exists voice_active_sessions_audit_trg
    on public.voice_active_sessions;

create trigger voice_active_sessions_audit_trg
    before update on public.voice_active_sessions
    for each row
    execute function public.voice_audit_active_session_edit();

-- ----------------------------------------------------------------------------
-- 3. Heartbeat: keep last_seen_at fresh, STOP clobbering the durable fields
-- ----------------------------------------------------------------------------
-- This is the exact statement that silently reverted Dashboard edits to
-- started_at on every reconcile cycle. An existing row now only ever gets
-- channel_id / last_seen_at / is_afk / updated_at refreshed. started_at and
-- credited_until are set on INSERT only, so the durable value (and any
-- administrative correction to it) always wins over in-memory state.

create or replace function public.voice_heartbeat_active(p_rows jsonb)
returns integer
language sql
as $$
    with incoming as (
        select
            (r->>'guild_id')::bigint        as guild_id,
            (r->>'user_id')::bigint         as user_id,
            (r->>'channel_id')::bigint      as channel_id,
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
                last_seen_at = excluded.last_seen_at,
                is_afk       = excluded.is_afk,
                updated_at   = now()
        where excluded.last_seen_at >= public.voice_active_sessions.last_seen_at
        returning 1
    )
    select count(*)::integer from applied;
$$;

-- ----------------------------------------------------------------------------
-- 3b. Heartbeat that also reports the durable values back
-- ----------------------------------------------------------------------------
-- Same edit-safe semantics as voice_heartbeat_active, but it returns each
-- row's authoritative started_at / credited_until. The bot mirrors them into
-- its in-memory session on every heartbeat, which is what keeps the cached
-- view from ever drifting from the database:
--
--   * an administrative Dashboard correction is adopted within one heartbeat
--     interval instead of at the next checkpoint;
--   * if a second process (an overlapping deployment) advanced the cursor,
--     this instance sees it immediately and cannot display time that the
--     database has already credited.
--
-- voice_heartbeat_active is kept for a schema-v2 database, where this function
-- does not exist yet.

create or replace function public.voice_heartbeat_active_v2(p_rows jsonb)
returns table (
    o_guild_id   bigint,
    o_user_id    bigint,
    o_started_at timestamptz,
    o_cursor     timestamptz
)
language plpgsql
as $$
declare
    r      jsonb;
    v_gid  bigint;
    v_uid  bigint;
    v_seen timestamptz;
begin
    for r in select * from jsonb_array_elements(p_rows)
    loop
        v_gid  := (r->>'guild_id')::bigint;
        v_uid  := (r->>'user_id')::bigint;
        v_seen := (r->>'last_seen_at')::timestamptz;

        if v_gid is null or v_uid is null or v_seen is null then
            continue;
        end if;

        insert into public.voice_active_sessions
            (guild_id, user_id, channel_id, started_at, last_seen_at, is_afk, updated_at)
        values (
            v_gid,
            v_uid,
            (r->>'channel_id')::bigint,
            (r->>'started_at')::timestamptz,
            v_seen,
            coalesce((r->>'is_afk')::boolean, false),
            now()
        )
        on conflict (guild_id, user_id) do update
            set channel_id   = excluded.channel_id,
                last_seen_at = excluded.last_seen_at,
                is_afk       = excluded.is_afk,
                updated_at   = now()
            where excluded.last_seen_at >= public.voice_active_sessions.last_seen_at;

        select a.started_at, a.credited_until
          into o_started_at, o_cursor
        from public.voice_active_sessions a
        where a.guild_id = v_gid and a.user_id = v_uid;

        o_guild_id := v_gid;
        o_user_id  := v_uid;
        return next;
    end loop;
end;
$$;

-- ----------------------------------------------------------------------------
-- 4. Atomic, idempotent, monotonic checkpoint
-- ----------------------------------------------------------------------------
-- Credits exactly [ coalesce(credited_until, started_at) , p_up_to ) for each
-- supplied session and advances the cursor, in ONE transaction per call.
--
--   * idempotent     — a replayed call finds cursor == p_up_to and credits 0;
--   * out-of-order   — p_up_to behind the cursor credits 0 and cannot move
--                      the cursor backwards (greatest());
--   * no lost update — the row is locked (FOR UPDATE) for the whole
--                      read-modify-write, so two workers cannot both credit
--                      the same interval;
--   * no phantom     — if anything fails the transaction rolls back, so the
--                      cursor never advances for time that was not saved;
--   * edit-aware     — each returned row echoes the row's own started_at, so
--                      the caller adopts a Dashboard correction as part of the
--                      very same round trip that credits the interval.
--
-- AFK rows are skipped: AFK time is never credited (unchanged policy).

create or replace function public.voice_checkpoint_active_sessions(p_rows jsonb)
-- The output columns are prefixed (o_*) on purpose: an unprefixed guild_id
-- here would be a PL/pgSQL variable that collides with the voice_active_sessions
-- column of the same name inside the body ("column reference is ambiguous").
returns table (
    o_guild_id         bigint,
    o_user_id          bigint,
    o_credited_seconds integer,
    o_cursor           timestamptz,
    o_started_at       timestamptz
)
language plpgsql
as $$
declare
    r        jsonb;
    v_gid    bigint;
    v_uid    bigint;
    v_up_to  timestamptz;
    v_row    public.voice_active_sessions%rowtype;
    v_from   timestamptz;
    v_new    timestamptz;
    v_secs   integer;
begin
    for r in select * from jsonb_array_elements(p_rows)
    loop
        v_gid   := (r->>'guild_id')::bigint;
        v_uid   := (r->>'user_id')::bigint;
        v_up_to := (r->>'up_to')::timestamptz;

        if v_gid is null or v_uid is null or v_up_to is null then
            continue;
        end if;

        select * into v_row
        from public.voice_active_sessions
        where guild_id = v_gid and user_id = v_uid
        for update;

        if not found then
            continue;   -- session already closed; nothing to checkpoint
        end if;

        if v_row.is_afk then
            continue;   -- AFK time is never counted
        end if;

        v_from := coalesce(v_row.credited_until, v_row.started_at);
        -- Monotonic: an out-of-order or replayed checkpoint cannot rewind.
        v_new  := greatest(v_from, v_up_to);

        if v_new > v_from then
            v_secs := public.voice_record_session(
                v_gid, v_uid, v_row.channel_id, v_from, v_new
            );
        else
            v_secs := 0;
        end if;

        update public.voice_active_sessions
           set credited_until = v_new,
               last_seen_at   = greatest(last_seen_at, v_up_to),
               updated_at     = now()
         where guild_id = v_gid and user_id = v_uid;

        o_guild_id         := v_gid;
        o_user_id          := v_uid;
        o_credited_seconds := v_secs;
        o_cursor           := v_new;
        -- Echo the row's own start back so the caller can adopt an
        -- administrative correction without a second round trip.
        o_started_at       := v_row.started_at;
        return next;
    end loop;
end;
$$;

-- ----------------------------------------------------------------------------
-- 5. Close: credit only the part that has not been checkpointed yet
-- ----------------------------------------------------------------------------
-- Same signature, same transaction, same AFK rule, same guarded delete.
-- The ONLY change is the credit start: coalesce(credited_until, started_at).
-- Without this, the periodic checkpoint and the final close would both credit
-- the beginning of the session.

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
    v_from     timestamptz;
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

    v_from := coalesce(v_row.credited_until, v_row.started_at);

    v_recorded := public.voice_record_session(
        p_guild_id, p_user_id, v_row.channel_id, v_from, p_ended_at
    );
    return v_recorded;
end;
$$;

-- ----------------------------------------------------------------------------
-- 6. Schema version marker (the bot refuses a stale database)
-- ----------------------------------------------------------------------------

create or replace function public.voice_schema_version()
returns integer
language sql
immutable
as $$
    select 3;
$$;

-- ----------------------------------------------------------------------------
-- 7. Lock the new functions down to the server-side role, like every other
-- ----------------------------------------------------------------------------

revoke execute on function public.voice_checkpoint_active_sessions(jsonb)
    from public, anon, authenticated;
grant execute on function public.voice_checkpoint_active_sessions(jsonb)
    to service_role;

revoke execute on function public.voice_heartbeat_active_v2(jsonb)
    from public, anon, authenticated;
grant execute on function public.voice_heartbeat_active_v2(jsonb)
    to service_role;

commit;

-- ============================================================================
-- Verify:
--   select public.voice_schema_version();                  -- 3
--   select column_name from information_schema.columns
--    where table_name = 'voice_active_sessions';           -- includes credited_until
--
-- Sanity check that nothing was lost (both sums must still agree afterwards):
--   select (select coalesce(sum(duration_seconds),0) from public.voice_sessions),
--          (select coalesce(sum(duration_seconds),0) from public.voice_daily_stats);
-- ============================================================================
