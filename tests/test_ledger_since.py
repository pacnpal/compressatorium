"""History filters (restructure PR 4, spec §8.5): ``since`` and ``checked``.

``since`` takes ``24h``, ``7d`` or an ISO time and keeps the rows that
finished at or after it; Needs attention reads "{n} conversions failed in the
last day" from it. ``checked`` keeps the rows whose new file passed a check
(the "Checked" filter and the summary strip's link).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from models import ConversionJob, ConversionMode, JobStatus
from services import db as _db
from services import ledger


@pytest.fixture(autouse=True)
def _engine(db_path, reset_db_engine):
    _db.init_engine(db_path)
    yield
    reset_db_engine()


async def _row(job_id: str, finished: datetime, *, status=JobStatus.COMPLETED, check=None):
    await ledger.record(ConversionJob(
        id=job_id, file_path=f"/data/{job_id}.iso", filename=f"{job_id}.iso",
        mode=ConversionMode.CREATEDVD, status=status, created_at=finished,
        started_at=finished - timedelta(seconds=5), completed_at=finished,
        output_path=f"/data/{job_id}.chd", output_size=1, input_bytes=2,
        check_passed=check, deleted_paths=[],
    ))


def _ids(page):
    return sorted(r["job_id"] for r in page["items"])


@pytest.mark.asyncio
async def test_since_keeps_the_rows_that_finished_after_it():
    now = datetime.now(timezone.utc)
    await _row("hour", now - timedelta(hours=1))
    await _row("days", now - timedelta(days=3))
    await _row("weeks", now - timedelta(days=20))

    f = ledger.LedgerFilter
    assert _ids(await ledger.list_page(f(since="24h"))) == ["hour"]
    assert _ids(await ledger.list_page(f(since="7d"))) == ["days", "hour"]
    iso = (now - timedelta(days=4)).isoformat()
    assert _ids(await ledger.list_page(f(since=iso))) == ["days", "hour"]
    # A Z suffix reads as UTC, as the rows themselves are written.
    z = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert _ids(await ledger.list_page(f(since=z))) == ["days", "hour", "weeks"]
    assert _ids(await ledger.list_page(f())) == ["days", "hour", "weeks"]


@pytest.mark.asyncio
async def test_since_with_status_counts_the_failures_of_the_last_day():
    from routes import ledger as routes

    now = datetime.now(timezone.utc)
    await _row("old-fail", now - timedelta(days=2), status=JobStatus.FAILED)
    await _row("new-fail", now - timedelta(minutes=5), status=JobStatus.FAILED)
    await _row("new-ok", now - timedelta(minutes=5))

    page = await routes.ledger_list(
        q="", status="failed", tool="", platform="", since="24h", checked=False,
        limit=1, offset=0,
    )
    assert page["total"] == 1
    assert [r["job_id"] for r in page["items"]] == ["new-fail"]


@pytest.mark.asyncio
async def test_a_since_that_cant_be_read_is_refused():
    from routes import ledger as routes

    for bad in ("yesterday", "24x", "-3d", "2026-13-40"):
        with pytest.raises(HTTPException) as caught:
            await routes.ledger_list(
                q="", status="", tool="", platform="", since=bad, checked=False,
                limit=10, offset=0,
            )
        assert caught.value.status_code == 400, bad


@pytest.mark.asyncio
async def test_checked_keeps_the_rows_whose_new_file_passed_a_check():
    now = datetime.now(timezone.utc)
    await _row("intact", now, check="intact")
    await _row("exact", now, check="round_tripped")
    await _row("known", now, check="recognized")
    await _row("unchecked", now)

    page = await ledger.list_page(ledger.LedgerFilter(checked=True))
    assert _ids(page) == ["exact", "intact", "known"]


@pytest.mark.asyncio
async def test_the_csv_export_takes_the_same_filters():
    from routes import ledger as routes

    now = datetime.now(timezone.utc)
    await _row("recent", now - timedelta(hours=2), check="intact")
    await _row("old", now - timedelta(days=9), check="intact")

    resp = await routes.ledger_export(
        q="", status="", tool="", platform="", since="7d", checked=True,
    )
    body = "".join([chunk async for chunk in resp.body_iterator])
    assert "recent.iso" in body and "old.iso" not in body


@pytest.mark.asyncio
async def test_a_job_that_only_waited_is_recorded_as_waiting_with_why():
    """Codex 4658826956: a watch-folder archive with no room to unpack ends
    COMPLETED with `result.waiting` and a warning (finish_external_job_waiting).
    History says it waited, and why, instead of "Unpacked"."""
    from services.job_manager import JobManager

    manager = JobManager()
    job = manager.create_external_job("Game.7z", ConversionMode.INBOX_UNPACK)
    await manager.finish_external_job_waiting(job.id, "Waiting for room: 2 GB free, 9 GB needed")
    await ledger.record(job)

    [row] = await ledger.recent(5)
    assert row["status"] == "waiting"
    assert row["warning"] == "Waiting for room: 2 GB free, 9 GB needed"
    assert _ids(await ledger.list_page(ledger.LedgerFilter(status="waiting"))) == [job.id]
    # Waiting saved nothing and counts as no conversion.
    assert (await ledger.savings())["conversions"] == 0


def test_history_shows_a_waiting_row_as_waiting_with_its_reason():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src" / "lib" / "components" / "activity"
           / "HistoryList.svelte").read_text(encoding="utf-8")
    assert "{ value: 'waiting', label: 'Waiting' }" in src
    assert "row.status === 'waiting'" in src
    assert "`Waiting: ${row.warning" in src


@pytest.mark.asyncio
async def test_every_finished_job_keeps_its_warning():
    """Codex 4658906850: a job that ends with a warning (an eXoDOS repack whose
    config names files outside the game) keeps it on its row, whatever its
    status, beside a failure's own error."""
    now = datetime.now(timezone.utc)
    for job_id, status, error in (
        ("done", JobStatus.COMPLETED, None), ("broke", JobStatus.FAILED, "exit 1"),
    ):
        await ledger.record(ConversionJob(
            id=job_id, file_path=f"/data/{job_id}.zip", filename=f"{job_id}.zip",
            mode=ConversionMode.CREATEDVD, status=status, created_at=now,
            started_at=now, completed_at=now, output_path=f"/data/{job_id}.out",
            output_size=1, input_bytes=2, check_passed=None, deleted_paths=[],
            error_message=error, warning="The game may not start: dosbox.conf names C:\\X",
        ))
    rows = {r["job_id"]: r for r in await ledger.recent(5)}
    assert rows["done"]["warning"] == "The game may not start: dosbox.conf names C:\\X"
    assert rows["done"]["error"] is None
    assert rows["broke"]["warning"] == rows["done"]["warning"]
    assert rows["broke"]["error"] == "exit 1"
    assert "warning" in ledger.CSV_COLUMNS


def test_history_shows_a_jobs_warning():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src" / "lib" / "components" / "activity"
           / "HistoryList.svelte").read_text(encoding="utf-8")
    assert "{#if row.warning && row.status !== 'waiting'}" in src
    assert "`Waiting: ${row.warning" in src


@pytest.mark.asyncio
async def test_a_relative_since_is_fixed_once_for_a_whole_export(monkeypatch):
    """Codex 4658906854: `since=24h` is resolved once, when the export starts.
    With a clock that moves an hour between pages, a row near the cutoff that
    matched the first page is still exported on the last."""
    from routes import ledger as routes

    start = datetime.now(timezone.utc)
    clock = iter(start + timedelta(hours=h) for h in range(100))
    monkeypatch.setattr(ledger, "_now", lambda: next(clock))
    monkeypatch.setattr(ledger, "_CSV_PAGE", 1)
    for i, age in enumerate((1, 5, 22, 23.5)):
        await _row(f"r{i}", start - timedelta(hours=age))

    resp = await routes.ledger_export(
        q="", status="", tool="", platform="", since="24h", checked=False,
    )
    body = "".join([chunk async for chunk in resp.body_iterator])
    assert [f"r{i}.iso" in body for i in range(4)] == [True] * 4


@pytest.mark.asyncio
async def test_a_jobs_closing_summary_is_kept():
    """Codex 4659056907: an external job's last message is its outcome (a check
    run's "2 Intact, 1 inconclusive, 3 skipped"). Every row keeps it, and
    History shows it beside what the job did."""
    from services.job_manager import JobManager

    manager = JobManager()
    job = manager.create_external_job("Check 6 games in roms", ConversionMode.CHECK)
    await manager.update_external_job(job.id, message="2 Intact, 1 inconclusive, 3 skipped")
    await manager.finish_external_job(job.id, success=True)
    await ledger.record(job)

    [row] = await ledger.recent(5)
    assert row["message"] == "2 Intact, 1 inconclusive, 3 skipped"
    assert "message" in ledger.CSV_COLUMNS


def test_history_shows_a_jobs_closing_summary():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src" / "lib" / "components" / "activity"
           / "HistoryList.svelte").read_text(encoding="utf-8")
    assert "{#if !row.output_path && row.message && row.status === 'completed'}" in src
