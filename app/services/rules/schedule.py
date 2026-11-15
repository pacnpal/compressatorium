"""When a Rule runs: its weekday mask, its time window and its interval.

Pure. The sweep asks :func:`_is_due` for each Rule on every scheduler tick, so
per-Platform intervals and windows are honoured without a timer per Rule.

A conversion Rule is due once its interval has passed since its last run. A
check Rule (#305) runs once per window instead: it is due when the window is
open on an allowed weekday and it has not run since this window opened, so a
run that starts late never pushes the next night later, and a run that goes
past midnight still belongs to the window it started in.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from services.rules.rules import _parse_hhmm, is_check_rule


def _in_window(rule: dict, now: datetime) -> bool:
    """Is *now* inside the rule's allowed days and time-of-day window?

    Evaluated in the rule's own timezone. The editor collects plain wall-clock
    values, so comparing them against UTC would run a 22:00-04:00 window at
    22:00 UTC -- the middle of the working day for most of the world.

    A window whose end is before its start wraps midnight (22:00-04:00), which
    is the shape an overnight conversion window actually takes. One whose start
    equals its end (03:00-03:00) is the whole day, as no window is. The weekday
    is read in the same zone, so "Saturday" means the operator's Saturday.
    """
    try:
        now = now.astimezone(ZoneInfo(rule.get("timezone") or "UTC"))
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        pass
    start, end = _window(rule)
    current = now.time()

    if start is None and end is None:
        return now.weekday() in rule["days"]
    if start is None or end is None:
        # Belt and braces: `normalize_rule` pauses a rule with half a window,
        # but a blob edited straight in the database bypasses that, and the
        # wrong answer here is the one that converts all day.
        return False

    if start <= end:
        return now.weekday() in rule["days"] and start <= current <= end

    # An overnight window (22:00-04:00) belongs to the day it *started*, so the
    # 02:00 tail of a Monday window runs on Tuesday morning and a Tuesday-only
    # rule does not fire at 02:00 Tuesday (that tail began on Monday).
    if current >= start:
        return now.weekday() in rule["days"]
    if current <= end:
        return (now.weekday() - 1) % 7 in rule["days"]
    return False


def _window(rule: dict) -> tuple:
    """The window's ``(start, end)`` times; ``(None, None)`` when it is the whole day.

    No window, and a window whose start equals its end, are both the whole day:
    read literally, 03:00-03:00 is open for one instant a day and a scheduler
    ticking once a minute would almost never see it.
    """
    start = _parse_hhmm(rule["window_start"])
    end = _parse_hhmm(rule["window_end"])
    if start is not None and start == end:
        return None, None
    return start, end


def _window_opened(rule: dict, now: datetime) -> datetime:
    """When the window *now* falls in opened, in the rule's zone.

    Call only while :func:`_in_window` holds. An overnight window
    (22:00-02:00) seen at 01:00 opened the evening before; no window at all
    is the whole local day, so it opened at midnight.
    """
    try:
        zone = ZoneInfo(rule.get("timezone") or "UTC")
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        zone = timezone.utc
    local = now.astimezone(zone)
    start = _window(rule)[0] or time(0, 0)
    opened = _instant(datetime.combine(local.date(), start), zone)
    if opened > now:
        opened = _instant(datetime.combine(local.date() - timedelta(days=1), start), zone)
    return opened


def _instant(wall: datetime, zone) -> datetime:
    """The first real instant at or after the naive wall-clock time *wall* in *zone*.

    Clock changes make a wall time ambiguous. In the hour a spring-forward
    skips (02:30 on a US change day) it never happens, and the window opens at
    the first instant after it (03:00 in the new offset), once. In the hour a
    fall-back repeats it happens twice, and the window opens at the first.
    """
    readings = sorted(
        (wall.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc) for fold in (0, 1)),
    )
    earlier, later = readings

    def local(instant: datetime) -> datetime:
        return instant.astimezone(zone).replace(tzinfo=None)

    if local(earlier) == wall:
        return earlier   # it exists; when it happens twice, the first time
    # Skipped: the transition lies between the two readings. Find, to the
    # second, the first instant whose wall time is at or past *wall*.
    while later - earlier > timedelta(seconds=1):
        middle = earlier + (later - earlier) // 2
        if local(middle) >= wall:
            later = middle
        else:
            earlier = middle
    return later


def _last_run(state: dict) -> datetime | None:
    """The scheduled run's stamp, as an aware datetime. None when it never ran.

    Only ``last_run_at``: a check Rule's Run now writes ``last_manual_run_at``
    and does not stand in for the scheduled run.
    """
    last = state.get("last_run_at")
    if not last:
        return None
    try:
        last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
    except ValueError:
        return None
    if last_dt.tzinfo is None:
        # A hand-edited or pre-tz-aware state row. Subtracting a naive datetime
        # from an aware one raises TypeError, which would abort the whole sweep
        # over one stale preference blob -- read it as UTC, which is what this
        # module has always written.
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    return last_dt


def _is_due(rule: dict, state: dict, now: datetime) -> bool:
    if not rule["enabled"]:
        return False
    if not _in_window(rule, now):
        return False
    last_dt = _last_run(state)
    if last_dt is None:
        return True
    if is_check_rule(rule):
        # Once per window, counted from when this window opened.
        return last_dt < _window_opened(rule, now)
    elapsed_minutes = (now - last_dt).total_seconds() / 60
    return elapsed_minutes >= rule["interval_minutes"]
