-- ============================================================================
-- ParadiseBOT — Voice accounting, migration 004: stop rounding time away
-- ============================================================================
--
-- THE BUG
-- -------
-- A day in a voice channel was always credited a few minutes short: 24 hours
-- landed in the database as ~23 h 51 m. Nothing was wrong with the heartbeat,
-- the reconnect handling or the AFK rule — the loss came from this one line in
-- the checkpoint function:
--
--     update voice_active_sessions set credited_until = v_new,   -- = p_up_to
--        ...
--     where v_secs := voice_record_session(v_from, v_new);       -- floor()
--
-- The credit is an INTEGER number of seconds (voice_record_session floors
-- `ended_at - started_at`), but the cursor was then moved to the *unedited*
-- instant. The fraction of a second that the floor discarded was thrown away
-- with it, and it could never be recovered:
--
--     from = 12:00:00.70   up_to = 12:02:00.40   → credits 119 s
--     cursor = 12:02:00.40 → next interval starts at .40, so the 0.70 s
--     between 12:02:00.00 and 12:02:00.40 is credited *nowhere*.
--
-- With the default checkpoint window (VOICE_CHECKPOINT_SECONDS=120) that is
-- 720 discards a day, each losing up to a second, and because the reconcile
-- loop keeps a fixed phase the fractions are strongly correlated instead of
-- cancelling out. 720 × 0.75 s ≈ 9 minutes — exactly the 23 h 51 m a 24-hour
-- session was showing. The longer somebody stays, the more they lose, which is
-- why it only ever showed up on the marathon sessions.
--
-- THE FIX
-- -------
-- The cursor now advances by the time that was actually credited:
--
--     credited_until = v_from + make_interval(secs => v_secs)
--
-- so a discarded fraction simply stays inside the next interval and is
-- credited on the following checkpoint. Every second of wall-clock time is
-- credited exactly once, and a replayed or out-of-order checkpoint still
-- credits nothing (v_secs is 0, so the cursor does not move).
--
-- The close path gets the same treatment from the other direction: the last
-- partial second is rounded to the nearest whole second instead of being
-- floored away, so a session that ends mid-second does not lose it.
--
-- Nothing else changes: AFK time is still never credited, the daily rows are
-- still split by Asia/Kolkata date inside voice_record_session, and the tables
-- are untouched — this migration only replaces two functions and bumps the
-- schema marker.
--
-- ROLLBACK
-- --------
-- Re-apply supabase/migrations/003_voice_checkpoint.sql (it redefines both
-- functions and resets the marker). No data migration is involved, and no
-- column is added or dropped.
-- ============================================================================

begin;

-- ----------------------------------------------------------------------------
-- 1. Checkpoint: advance the cursor by the credited time, not by the request
-- ----------------------------------------------------------------------------
-- Same signature, same guarantees as migration 003 (idempotent, monotonic,
-- row-locked, edit-aware), plus: no fractional second is ever discarded.

create or replace function public.voice_checkpoint_active_sessions(p_rows jsonb)
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
    v_target timestamptz;
    v_cursor timestamptz;
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
        v_target := greatest(v_from, v_up_to);

        if v_target > v_from then
            v_secs := public.voice_record_session(
                v_gid, v_uid, v_row.channel_id, v_from, v_target
            );
        else
            v_secs := 0;
        end if;

        if v_secs is null then
            v_secs := 0;
        end if;

        -- THE FIX: + credited seconds, not up to the requested instant. Any
        -- sub-second remainder stays in [cursor, up_to) and is credited next
        -- time. A replayed call credits 0 and therefore moves nothing.
        v_cursor := v_from + make_interval(secs => v_secs);

        update public.voice_active_sessions
           set credited_until = greatest(v_from, v_cursor),
               last_seen_at   = greatest(last_seen_at, v_up_to),
               updated_at     = now()
         where guild_id = v_gid and user_id = v_uid;

        o_guild_id         := v_gid;
        o_user_id          := v_uid;
        o_credited_seconds := v_secs;
        o_cursor           := greatest(v_from, v_cursor);
        o_started_at       := v_row.started_at;
        return next;
    end loop;
end;
$$;

comment on function public.voice_checkpoint_active_sessions(jsonb) is
    'ParadiseBOT: credit [credited_until | started_at, up_to) into voice_daily_stats and advance the cursor by the credited integer seconds, so no fractional second is ever lost. Idempotent, monotonic, row-locked.';

-- ----------------------------------------------------------------------------
-- 2. Close: round the trailing remainder instead of dropping it
-- ----------------------------------------------------------------------------
-- Same signature, same AFK rule, same guarded delete. The end of the session
-- is rounded to the nearest whole second (at most half a second either way) so
-- the last shard of time is not floored away as well.

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
    v_seconds  integer;
    v_end      timestamptz;
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

    -- Exactly one row (primary key guild_id+user_id, locked FOR UPDATE above);
    -- the credit below is computed from the copy in v_row, so nothing is lost.
    delete from public.voice_active_sessions where guild_id = p_guild_id and user_id = p_user_id;

    if v_row.is_afk then
        return 0;  -- AFK time is never counted
    end if;

    v_from := coalesce(v_row.credited_until, v_row.started_at);

    -- Nearest whole second: a 0.6 s tail counts, a 0.3 s one does not. The
    -- recorded session still ends at a real boundary within half a second.
    v_seconds := greatest(
        0, round(extract(epoch from (p_ended_at - v_from)))::integer
    );
    v_end := v_from + make_interval(secs => v_seconds);

    if v_end <= v_from then
        return 0;
    end if;

    v_recorded := public.voice_record_session(
        p_guild_id, p_user_id, v_row.channel_id, v_from, v_end
    );
    return v_recorded;
end;
$$;

comment on function public.voice_close_active_session(bigint, bigint, timestamptz) is
    'ParadiseBOT: credit the uncheckpointed tail of a session, rounded to the nearest second, then delete the active row.';

-- ----------------------------------------------------------------------------
-- 3. Schema marker
-- ----------------------------------------------------------------------------

create or replace function public.voice_schema_version()
returns integer
language sql
immutable
as $$
    select 4;
$$;

-- Grants are inherited by CREATE OR REPLACE, but re-stating them keeps a fresh
-- project (running 001 → 004 in one go) correct even if a step was skipped.
grant execute on function public.voice_checkpoint_active_sessions(jsonb) to service_role;
grant execute on function public.voice_close_active_session(bigint, bigint, timestamptz) to service_role;

commit;
