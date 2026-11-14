"""The Ledger: a permanent record of every job that reached a terminal status.

Written by a ``job_manager`` terminal listener (registered in ``main.py``), so
every runner outcome -- completed, failed, cancelled, external -- lands here
exactly once while the job object still says how it ended. Rows survive
restarts and are kept forever unless a retention (days) is set through the
``ledger`` preference, in which case older rows are pruned on each write.

"Saved" bytes count only completed rows whose Source was deleted: a kept
Source still occupies its space, so nothing was reclaimed yet.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from fastapi.concurrency import run_in_threadpool
from logging_setup import get_logger
from models import ConversionJob
from services import db as _db
from services.preferences_store import preferences_store
from services.tools import registry
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

logger = get_logger("ledger")

PREF_KEY = "ledger"
# 100 years: keeps `now - timedelta(days=…)` far inside datetime's range.
MAX_RETENTION_DAYS = 36500


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    # Fixed precision: isoformat() drops the fraction on a whole second, and
    # "…:00Z" sorts after "…:00.5Z", so mixed precision misorders rows and
    # pruning cutoffs that compare these strings.
    return (
        dt.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _tool_and_platform(mode: str) -> tuple[str | None, str | None]:
    """Tool id from the registry, and the Platform when the mode serves
    exactly one (otherwise unknown until detection lands, #294)."""
    try:
        tool = registry.for_mode(mode).id
    except KeyError:
        return None, None
    slugs = registry.mode_platform_slugs(mode)
    return tool, (next(iter(slugs)) if len(slugs) == 1 else None)


def row_from_job(job: ConversionJob) -> dict:
    mode = getattr(job.mode, "value", str(job.mode))
    tool, platform = _tool_and_platform(mode)
    # Deterministic (never now()), so a repeated notification keys the same row.
    finished = job.completed_at or job.created_at
    duration = (
        (finished - job.started_at).total_seconds() if job.started_at else None
    )
    return {
        "job_id": job.id,
        "finished_at": _iso(finished),
        "tool": tool,
        "mode": mode,
        "source_path": job.file_path,
        "output_path": job.output_path,
        "input_bytes": job.input_bytes,
        "output_bytes": job.output_size,
        "duration_s": duration,
        "status": getattr(job.status, "value", str(job.status)),
        "check_passed": job.check_passed or "none",
        "source_deleted": bool(job.deleted_paths),
        "deleted_paths": list(job.deleted_paths),
        "referrers_updated": [],
        "library_id": None,
        "image_key": None,
        "platform": platform,
        "error": job.error_message,
    }


def _session():
    if _db.SessionLocal is None:
        raise RuntimeError("ledger: db.init_engine() has not run")
    return _db.SessionLocal()


def _insert_sync(row: dict, retention_days: int | None) -> None:
    with _session() as session:
        # A job can be announced terminal more than once (a queued cancel is
        # notified by cancel_job and again by the runner), so the same
        # (job_id, finished_at) is a no-op rather than a second row.
        session.execute(
            sqlite_insert(_db.LedgerEntry)
            .values(**row)
            .on_conflict_do_nothing(index_elements=["job_id", "finished_at"])
        )
        if retention_days:
            cutoff = _iso(datetime.now(timezone.utc) - timedelta(days=retention_days))
            session.execute(
                delete(_db.LedgerEntry).where(_db.LedgerEntry.finished_at < cutoff)
            )
        session.commit()


async def retention_days() -> int | None:
    stored = await preferences_store.get(PREF_KEY) or {}
    days = stored.get("retention_days")
    if isinstance(days, int) and 0 < days <= MAX_RETENTION_DAYS:
        return days
    return None


async def record(job: ConversionJob) -> None:
    """Terminal listener: write *job*'s row. Never raises."""
    try:
        await run_in_threadpool(_insert_sync, row_from_job(job), await retention_days())
    except Exception:
        logger.warning("Failed to write Ledger row for job %s", job.id, exc_info=True)


def _entry_dict(e: _db.LedgerEntry) -> dict:
    return {c.name: getattr(e, c.name) for c in _db.LedgerEntry.__table__.columns}


def _recent_sync(limit: int) -> list[dict]:
    with _session() as session:
        rows = session.scalars(
            select(_db.LedgerEntry)
            .order_by(_db.LedgerEntry.finished_at.desc(), _db.LedgerEntry.id.desc())
            .limit(limit)
        ).all()
        return [_entry_dict(r) for r in rows]


async def recent(limit: int = 5) -> list[dict]:
    return await run_in_threadpool(_recent_sync, limit)


def _savings_sync() -> dict:
    saved = (
        func.coalesce(_db.LedgerEntry.input_bytes, 0)
        - func.coalesce(_db.LedgerEntry.output_bytes, 0)
    )
    where = (
        (_db.LedgerEntry.status == "completed")
        & _db.LedgerEntry.source_deleted.is_(True)
        & _db.LedgerEntry.input_bytes.is_not(None)
        & _db.LedgerEntry.output_bytes.is_not(None)
    )
    with _session() as session:
        total, count = session.execute(
            select(func.coalesce(func.sum(saved), 0), func.count()).where(where)
        ).one()
        by_platform: dict[str, int] = {}
        by_format: dict[str, int] = {}
        # ponytail: per-row Python split (format = output extension); move to
        # GROUP BY with a stored format column if the Ledger grows huge.
        for platform, output_path, value in session.execute(
            select(_db.LedgerEntry.platform, _db.LedgerEntry.output_path, saved).where(where)
        ):
            p = platform or "unsorted"
            by_platform[p] = by_platform.get(p, 0) + int(value)
            fmt = os.path.splitext(output_path or "")[1].lstrip(".").lower() or "other"
            by_format[fmt] = by_format.get(fmt, 0) + int(value)
    return {
        "saved_bytes": int(total),
        "conversions": int(count),
        "by_platform": by_platform,
        "by_format": by_format,
    }


async def savings() -> dict:
    return await run_in_threadpool(_savings_sync)
