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

import csv
import io
import os
from dataclasses import dataclass
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


# --- Ledger view: filtered list, CSV export, metrics (#297) -----------------

CSV_COLUMNS = (
    "job_id", "finished_at", "tool", "mode", "source_path", "output_path",
    "input_bytes", "output_bytes", "duration_s", "status", "check_passed",
    "source_deleted", "deleted_paths", "referrers_updated", "library_id",
    "image_key", "platform", "error",
)
_CSV_PAGE = 500
# Always exported so a scrape sees a zero, not a missing series.
_METRIC_STATUSES = ("completed", "failed", "cancelled")


@dataclass(frozen=True)
class LedgerFilter:
    """What the Ledger view and its CSV export filter on.

    Empty strings mean "no filter". ``q`` matches the Source or output path.
    """

    q: str = ""
    status: str = ""
    tool: str = ""
    platform: str = ""

    def clauses(self) -> list:
        e = _db.LedgerEntry
        out = []
        if self.q:
            out.append(
                e.source_path.contains(self.q, autoescape=True)
                | e.output_path.contains(self.q, autoescape=True)
            )
        for column, value in (
            (e.status, self.status), (e.tool, self.tool), (e.platform, self.platform),
        ):
            if value:
                out.append(column == value)
        return out


def _list_sync(flt: LedgerFilter, limit: int, offset: int) -> dict:
    e = _db.LedgerEntry
    with _session() as session:
        total = session.scalar(
            select(func.count()).select_from(e).where(*flt.clauses())
        )
        rows = session.scalars(
            select(e).where(*flt.clauses())
            .order_by(e.finished_at.desc(), e.id.desc())
            .limit(limit).offset(offset)
        ).all()
        return {"items": [_entry_dict(r) for r in rows], "total": int(total or 0)}


async def list_page(flt: LedgerFilter, limit: int = 50, offset: int = 0) -> dict:
    """Newest-first page of rows matching *flt*, plus the total match count."""
    return await run_in_threadpool(_list_sync, flt, limit, offset)


def _facets_sync() -> dict:
    e = _db.LedgerEntry
    with _session() as session:
        return {
            name: sorted(v for v in session.scalars(select(column).distinct()) if v)
            for name, column in (
                ("status", e.status), ("tool", e.tool), ("platform", e.platform),
            )
        }


async def facets() -> dict:
    """Distinct status / tool / Platform values present, for the filter menus."""
    return await run_in_threadpool(_facets_sync)


def _csv_cell(value):
    if isinstance(value, (list, tuple)):
        value = "; ".join(str(v) for v in value)
    if value is None:
        return ""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        # Stops a spreadsheet from running a cell as a formula.
        return "'" + value
    return value


def _csv_chunk(rows: list[dict], header: bool) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    if header:
        writer.writerow(CSV_COLUMNS)
    for row in rows:
        writer.writerow([_csv_cell(row[c]) for c in CSV_COLUMNS])
    return buf.getvalue()


async def export_csv(flt: LedgerFilter):
    """Yield the CSV for every row matching *flt*, newest first, a page at a time."""
    offset = 0
    while True:
        page = await list_page(flt, _CSV_PAGE, offset)
        yield _csv_chunk(page["items"], header=offset == 0)
        offset += _CSV_PAGE
        if offset >= page["total"]:
            return


def _metrics_sync() -> tuple[dict[str, int], int, int]:
    e = _db.LedgerEntry
    with _session() as session:
        by_status = dict(session.execute(
            select(e.status, func.count()).group_by(e.status)
        ).all())
        bytes_in, bytes_out = session.execute(
            select(
                func.coalesce(func.sum(e.input_bytes), 0),
                func.coalesce(func.sum(e.output_bytes), 0),
            ).where(e.status == "completed")
        ).one()
    return by_status, int(bytes_in), int(bytes_out)


async def metrics_text(queue_depth: int) -> str:
    """Prometheus text exposition (v0.0.4) of the Ledger plus the live queue.

    The totals are counted from the Ledger, so a retention prune lowers them;
    Prometheus reads that as a counter reset.
    """
    by_status, bytes_in, bytes_out = await run_in_threadpool(_metrics_sync)
    statuses = sorted(set(_METRIC_STATUSES) | set(by_status))
    lines = [
        "# HELP compressatorium_jobs_total Finished jobs recorded in the Ledger, by status.",
        "# TYPE compressatorium_jobs_total counter",
        *(
            f'compressatorium_jobs_total{{status="{s}"}} {by_status.get(s, 0)}'
            for s in statuses
        ),
        "# HELP compressatorium_input_bytes_total Source bytes of completed conversions.",
        "# TYPE compressatorium_input_bytes_total counter",
        f"compressatorium_input_bytes_total {bytes_in}",
        "# HELP compressatorium_output_bytes_total Output bytes of completed conversions.",
        "# TYPE compressatorium_output_bytes_total counter",
        f"compressatorium_output_bytes_total {bytes_out}",
        "# HELP compressatorium_queue_depth Jobs queued or running now.",
        "# TYPE compressatorium_queue_depth gauge",
        f"compressatorium_queue_depth {queue_depth}",
    ]
    return "\n".join(lines) + "\n"
