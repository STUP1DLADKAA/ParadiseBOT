"""Timezone-aware reporting windows for Paradise's voice leaderboards.

All persisted daily aggregates use the calendar date in Asia/Kolkata.  These
helpers are also used for live sessions so an in-progress voice session is
clipped to the period being rendered rather than added in full to every board.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc


def ensure_aware(value: datetime) -> datetime:
    """Treat legacy naive timestamps as UTC and return an aware datetime."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def ist_date(now: datetime | None = None) -> date:
    """Return the date at the supplied instant in the authoritative timezone."""
    instant = ensure_aware(now or datetime.now(UTC))
    return instant.astimezone(IST).date()


def next_ist_midnight(now: datetime | None = None) -> datetime:
    """The next daily reporting boundary, as an aware UTC instant."""
    instant = ensure_aware(now or datetime.now(UTC)).astimezone(IST)
    tomorrow = instant.date() + timedelta(days=1)
    return datetime.combine(tomorrow, time.min, tzinfo=IST).astimezone(UTC)


def period_bounds(
    period: str,
    target_date: date | None = None,
    *,
    now: datetime | None = None,
) -> tuple[datetime, datetime] | None:
    """Return inclusive-start / exclusive-end bounds for a VC period.

    ``today``, ``yesterday``, ``week`` and ``month`` are calendar windows in
    Asia/Kolkata.  ``all_time`` deliberately returns ``None`` because it has
    no lower reporting boundary.
    """
    if period == "all_time":
        return None
    day = target_date or ist_date(now)

    if period == "today":
        start_day, end_day = day, day + timedelta(days=1)
    elif period == "yesterday":
        start_day, end_day = day - timedelta(days=1), day
    elif period == "week":
        start_day = day - timedelta(days=day.weekday())
        end_day = start_day + timedelta(days=7)
    elif period == "month":
        start_day = day.replace(day=1)
        if start_day.month == 12:
            end_day = date(start_day.year + 1, 1, 1)
        else:
            end_day = date(start_day.year, start_day.month + 1, 1)
    else:
        raise ValueError(f"Unsupported voice reporting period: {period!r}")

    return (
        datetime.combine(start_day, time.min, tzinfo=IST),
        datetime.combine(end_day, time.min, tzinfo=IST),
    )


def period_overlap_seconds(
    started_at: datetime,
    ended_at: datetime,
    period: str,
    target_date: date | None = None,
    *,
    now: datetime | None = None,
) -> int:
    """Seconds of a session that overlap one named calendar reporting period."""
    start = ensure_aware(started_at).astimezone(UTC)
    end = ensure_aware(ended_at).astimezone(UTC)
    if end <= start:
        return 0
    bounds = period_bounds(period, target_date, now=now)
    if bounds is None:
        elapsed = end - start
        return max(0, elapsed.days * 86_400 + elapsed.seconds)
    period_start, period_end = (bound.astimezone(UTC) for bound in bounds)
    overlap_start = max(start, period_start)
    overlap_end = min(end, period_end)
    if overlap_end <= overlap_start:
        return 0
    # Attribute whole seconds by cumulative elapsed time from the session's
    # origin. This makes adjacent calendar windows add back to the session's
    # floor(total duration), even when the join/leave timestamps have
    # fractional seconds.
    elapsed_start = overlap_start - start
    elapsed_end = overlap_end - start
    floor_start = elapsed_start.days * 86_400 + elapsed_start.seconds
    floor_end = elapsed_end.days * 86_400 + elapsed_end.seconds
    return max(0, floor_end - floor_start)


def split_session_by_ist_midnight(
    started_at: datetime, ended_at: datetime
) -> dict[str, int]:
    """Split an elapsed voice session across IST calendar dates.

    Session endpoints are instants; each date's boundaries are constructed
    with the IANA ``Asia/Kolkata`` zone rather than a hardcoded UTC offset.
    """
    start = ensure_aware(started_at).astimezone(UTC)
    end = ensure_aware(ended_at).astimezone(UTC)
    if end <= start:
        return {}

    local_start = start.astimezone(IST)
    local_end = end.astimezone(IST)
    day = local_start.date()
    last_day = local_end.date()
    result: dict[str, int] = {}
    elapsed_microseconds = 0
    allocated_seconds = 0

    while day <= last_day:
        local_day_start = datetime.combine(day, time.min, tzinfo=IST).astimezone(UTC)
        local_day_end = datetime.combine(
            day + timedelta(days=1), time.min, tzinfo=IST
        ).astimezone(UTC)
        overlap_start = max(start, local_day_start)
        overlap_end = min(end, local_day_end)
        if overlap_end > overlap_start:
            overlap = overlap_end - overlap_start
            elapsed_microseconds += (
                (overlap.days * 86_400 + overlap.seconds) * 1_000_000
                + overlap.microseconds
            )
            cumulative_seconds = elapsed_microseconds // 1_000_000
            seconds = int(cumulative_seconds - allocated_seconds)
            allocated_seconds = cumulative_seconds
            if seconds:
                result[day.isoformat()] = result.get(day.isoformat(), 0) + seconds
        day += timedelta(days=1)

    return result


__all__ = [
    "IST",
    "UTC",
    "ensure_aware",
    "ist_date",
    "next_ist_midnight",
    "period_bounds",
    "period_overlap_seconds",
    "split_session_by_ist_midnight",
]
