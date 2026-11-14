"""When a Rule runs: its weekday mask, its time window and its interval.

Pure. The sweep asks :func:`_is_due` for each Rule on every scheduler tick, so
per-Platform intervals and windows are honoured without a timer per Rule.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from services.rules.rules import _parse_hhmm


def _in_window(rule: dict, now: datetime) -> bool:
    """Is *now* inside the rule's allowed days and time-of-day window?

    Evaluated in the rule's own timezone. The editor collects plain wall-clock
    values, so comparing them against UTC would run a 22:00-04:00 window at
    22:00 UTC -- the middle of the working day for most of the world.

    A window whose end is before its start wraps midnight (22:00-04:00), which
    is the shape an overnight conversion window actually takes. The weekday is
    read in the same zone, so "Saturday" means the operator's Saturday.
    """
    try:
        now = now.astimezone(ZoneInfo(rule.get("timezone") or "UTC"))
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        pass
    start = _parse_hhmm(rule["window_start"])
    end = _parse_hhmm(rule["window_end"])
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


def _is_due(rule: dict, state: dict, now: datetime) -> bool:
    if not rule["enabled"]:
        return False
    if not _in_window(rule, now):
        return False
    last = state.get("last_run_at")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
    except ValueError:
        return True
    if last_dt.tzinfo is None:
        # A hand-edited or pre-tz-aware state row. Subtracting a naive datetime
        # from an aware one raises TypeError, which would abort the whole sweep
        # over one stale preference blob -- read it as UTC, which is what this
        # module has always written.
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    elapsed_minutes = (now - last_dt).total_seconds() / 60
    return elapsed_minutes >= rule["interval_minutes"]
