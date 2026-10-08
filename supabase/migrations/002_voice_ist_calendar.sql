-- ============================================================================
-- ParadiseBOT — Voice reporting periods in Asia/Kolkata (IST)
-- ============================================================================
-- Additive, transaction-safe upgrade from migration 001.
-- The voice_sessions history remains untouched. For members whose current
-- daily aggregate exactly reconciles with their complete session history, the
-- aggregate dates are rebuilt from those session timestamps in IST. If a
-- member's old aggregate does not reconcile (for example, a manually edited
-- total or a legacy aggregate with no matching sessions), those rows are
-- preserved byte-for-byte instead of guessing or discarding history.
--
-- New sessions are split at Asia/Kolkata midnights. Weekly windows are ISO
-- Monday-to-Monday and monthly windows are calendar-month boundaries.
-- ============================================================================

begin;

-- Re-bucket only accounts whose daily totals reconcile exactly with the
-- preserved session history. This is guarded by the schema version, so a
-- re-run after cutover cannot reinterpret already-IST aggregates as UTC.
do $$
declare
    v_version integer;
begin
    v_version := public.voice_schema_version();
    if v_version < 2 then
        create temporary table if not exists _voice_ist_rebucket (
            guild_id bigint not null,
            user_id bigint not null,
            date date not null,
            duration_seconds integer not null,
            primary key (guild_id, user_id, date)
        ) on commit drop;
        truncate table _voice_ist_rebucket;

        insert into _voice_ist_rebucket (guild_id, user_id, date, duration_seconds)
        with daily_totals as (
            select guild_id, user_id, sum(duration_seconds)::bigint as seconds
            from public.voice_daily_stats
            group by guild_id, user_id
        ),
        session_totals as (
            select guild_id, user_id, sum(duration_seconds)::bigint as seconds
            from public.voice_sessions
            group by guild_id, user_id
        ),
        reconciled as (
            select d.guild_id, d.user_id
            from daily_totals d
            join session_totals s using (guild_id, user_id)
            where d.seconds = s.seconds and s.seconds > 0
        ),
        ist_chunks as (
            select
                s.guild_id,
                s.user_id,
                dates.day_start::date as date,
                floor(extract(epoch from (
                    least(s.ended_at, (dates.day_start::date + 1)::timestamp at time zone 'Asia/Kolkata')
                    - s.started_at
                )))::integer
                - floor(extract(epoch from (
                    greatest(s.started_at, dates.day_start::date::timestamp at time zone 'Asia/Kolkata')
                    - s.started_at
                )))::integer as seconds
            from public.voice_sessions s
            join reconciled r using (guild_id, user_id)
            cross join lateral generate_series(
                ((s.started_at at time zone 'Asia/Kolkata')::date)::timestamp,
                ((s.ended_at at time zone 'Asia/Kolkata')::date)::timestamp,
                interval '1 day'
            ) as dates(day_start)
        )
        select guild_id, user_id, date, sum(seconds)::integer
        from ist_chunks
        where seconds > 0
        group by guild_id, user_id, date;

        -- Rebuild only reconciled users. All unreconciled daily history is
        -- retained unchanged, and every completed session row remains intact.
        delete from public.voice_daily_stats d
        using (
            select distinct guild_id, user_id from _voice_ist_rebucket
        ) r
        where d.guild_id = r.guild_id and d.user_id = r.user_id;

        insert into public.voice_daily_stats (guild_id, user_id, date, duration_seconds)
        select guild_id, user_id, date, duration_seconds
        from _voice_ist_rebucket
        on conflict (guild_id, user_id, date) do update
            set duration_seconds = excluded.duration_seconds,
                updated_at = now();
    end if;
end;
$$;

create or replace function public.voice_schema_version()
returns integer
language sql
immutable
as $$
    select 2;
$$;

-- Record one completed session and split it by Asia/Kolkata calendar date.
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
    v_duration integer;
    v_inserted boolean := false;
    v_day       date;
    v_day_start timestamptz;
    v_day_end   timestamptz;
    v_seconds   integer;
begin
    if p_started_at is null or p_ended_at is null or p_ended_at <= p_started_at then
        return 0;
    end if;

    v_duration := floor(extract(epoch from (p_ended_at - p_started_at)))::integer;
    if v_duration <= 0 then
        return 0;
    end if;

    insert into public.voice_sessions
        (guild_id, user_id, channel_id, started_at, ended_at, duration_seconds)
    values
        (p_guild_id, p_user_id, p_channel_id, p_started_at, p_ended_at, v_duration)
    on conflict (guild_id, user_id, started_at, ended_at) do nothing
    returning true into v_inserted;

    if v_inserted is not true then
        return 0;
    end if;

    for v_day in
        select value::date
        from generate_series(
            ((p_started_at at time zone 'Asia/Kolkata')::date)::timestamp,
            ((p_ended_at at time zone 'Asia/Kolkata')::date)::timestamp,
            interval '1 day'
        ) as value
    loop
        v_day_start := v_day::timestamp at time zone 'Asia/Kolkata';
        v_day_end := (v_day + 1)::timestamp at time zone 'Asia/Kolkata';
        -- Allocate whole seconds from cumulative elapsed time, so fractional
        -- endpoint seconds never disappear at a midnight split. Chunks still
        -- add exactly to floor(whole session duration).
        v_seconds := floor(extract(epoch from (
            least(p_ended_at, v_day_end) - p_started_at
        )))::integer
        - floor(extract(epoch from (
            greatest(p_started_at, v_day_start) - p_started_at
        )))::integer;

        if v_seconds > 0 then
            insert into public.voice_daily_stats
                (guild_id, user_id, date, duration_seconds)
            values (p_guild_id, p_user_id, v_day, v_seconds)
            on conflict (guild_id, user_id, date) do update
                set duration_seconds = public.voice_daily_stats.duration_seconds
                                        + excluded.duration_seconds,
                    updated_at = now();
        end if;
    end loop;

    return v_duration;
end;
$$;

-- Calendar-window leaderboard, with all date values representing IST days.
create or replace function public.voice_leaderboard(
    p_guild_id    bigint,
    p_period      text,
    p_target_date date default null
)
returns table (user_id bigint, total_seconds bigint)
language sql
stable
as $$
    with target as (
        select coalesce(p_target_date, (now() at time zone 'Asia/Kolkata')::date)::date as today_d
    ), bounds as (
        select
            today_d,
            today_d - (extract(isodow from today_d)::integer - 1) as week_start,
            date_trunc('month', today_d::timestamp)::date as month_start
        from target
    )
    select v.user_id, sum(v.duration_seconds)::bigint as total
    from public.voice_daily_stats v
    cross join bounds b
    where v.guild_id = p_guild_id
      and case
            when p_period = 'today' then v.date = b.today_d
            when p_period = 'yesterday' then v.date = b.today_d - 1
            when p_period = 'week' then
                v.date >= b.week_start and v.date < b.week_start + 7 and v.date <= b.today_d
            when p_period = 'month' then
                v.date >= b.month_start
                and v.date < (b.month_start + interval '1 month')::date
                and v.date <= b.today_d
            when p_period = 'all_time' then true
            else false
          end
    group by v.user_id
    having sum(v.duration_seconds) > 0
    order by 2 desc, 1 asc;
$$;

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
    with target as (
        select coalesce(p_target_date, (now() at time zone 'Asia/Kolkata')::date)::date as today_d
    ), bounds as (
        select
            today_d,
            today_d - (extract(isodow from today_d)::integer - 1) as week_start,
            date_trunc('month', today_d::timestamp)::date as month_start
        from target
    )
    select
        coalesce(sum(case when v.date = b.today_d then v.duration_seconds else 0 end), 0)::bigint,
        coalesce(sum(case when v.date = b.today_d - 1 then v.duration_seconds else 0 end), 0)::bigint,
        coalesce(sum(case when v.date >= b.week_start and v.date < b.week_start + 7
                               and v.date <= b.today_d
                          then v.duration_seconds else 0 end), 0)::bigint,
        coalesce(sum(case when v.date >= b.month_start
                               and v.date < (b.month_start + interval '1 month')::date
                               and v.date <= b.today_d
                          then v.duration_seconds else 0 end), 0)::bigint,
        coalesce(sum(v.duration_seconds), 0)::bigint
    from public.voice_daily_stats v
    cross join bounds b
    where v.guild_id = p_guild_id and v.user_id = p_user_id;
$$;

commit;

-- Verify: select public.voice_schema_version();  -- should return 2
