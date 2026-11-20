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
