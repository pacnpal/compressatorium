"""Tests for JobManager external-job API and METADATA_SCAN non-cancellability."""

import asyncio
import pytest

from app.models import ConversionMode, JobStatus
from app.services.job_manager import JobManager


def _make_manager() -> JobManager:
    """Return a fresh JobManager with default settings."""
    return JobManager(max_concurrent=1, max_job_history=10)


# ---------------------------------------------------------------------------
# create_external_job
# ---------------------------------------------------------------------------

def test_create_external_job_registers_job():
    mgr = _make_manager()
    job = mgr.create_external_job(
        filename="Metadata Scan",
        mode=ConversionMode.METADATA_SCAN,
        message="Starting…",
    )

    assert job.id in mgr.jobs
    assert mgr.jobs[job.id] is job
    assert job.status == JobStatus.PROCESSING
    assert job.progress == 0
    assert job.message == "Starting…"
    assert job.mode == ConversionMode.METADATA_SCAN
    assert job.started_at is not None


def test_create_external_job_returns_distinct_ids():
    mgr = _make_manager()
    j1 = mgr.create_external_job("Scan 1", ConversionMode.METADATA_SCAN)
    j2 = mgr.create_external_job("Scan 2", ConversionMode.METADATA_SCAN)
    assert j1.id != j2.id


# ---------------------------------------------------------------------------
# update_external_job
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_external_job_clamps_progress():
    mgr = _make_manager()
    job = mgr.create_external_job("Scan", ConversionMode.METADATA_SCAN)
    job_id = job.id

    await mgr.update_external_job(job_id, progress=150)
    assert mgr.jobs[job_id].progress == 100

    await mgr.update_external_job(job_id, progress=-10)
    assert mgr.jobs[job_id].progress == 0


@pytest.mark.asyncio
async def test_update_external_job_updates_message():
    mgr = _make_manager()
    job = mgr.create_external_job("Scan", ConversionMode.METADATA_SCAN)
    job_id = job.id

    await mgr.update_external_job(job_id, message="Phase 1 [1/5]")
    assert mgr.jobs[job_id].message == "Phase 1 [1/5]"


@pytest.mark.asyncio
async def test_update_external_job_notifies_subscriber():
    mgr = _make_manager()
    job = mgr.create_external_job("Scan", ConversionMode.METADATA_SCAN)
    job_id = job.id

    queue = mgr.subscribe(job_id)
    await mgr.update_external_job(job_id, progress=42, message="In progress")

    assert not queue.empty()
    payload = queue.get_nowait()
    assert payload["type"] == "progress"
    assert payload["job_id"] == job_id
    assert payload["progress"] == 42
    assert payload["message"] == "In progress"


@pytest.mark.asyncio
async def test_update_external_job_noop_for_unknown_id():
    """Updating a non-existent job should not raise."""
    mgr = _make_manager()
    await mgr.update_external_job("unknown", progress=50, message="x")


# ---------------------------------------------------------------------------
# finish_external_job
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_finish_external_job_success_removes_from_live_jobs():
    mgr = _make_manager()
    job = mgr.create_external_job("Scan", ConversionMode.METADATA_SCAN)
    job_id = job.id

    await mgr.finish_external_job(job_id, success=True)

    assert job_id not in mgr.jobs
    # Archived for brief lookup
    assert job_id in mgr._archived_jobs


@pytest.mark.asyncio
async def test_finish_external_job_success_notifies_complete():
    mgr = _make_manager()
    job = mgr.create_external_job("Scan", ConversionMode.METADATA_SCAN)
    job_id = job.id

    queue = mgr.subscribe(job_id)
    await mgr.finish_external_job(job_id, success=True)

    payload = queue.get_nowait()
    assert payload["type"] == "complete"
    assert payload["status"] == JobStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_finish_external_job_failure_notifies_error():
    mgr = _make_manager()
    job = mgr.create_external_job("Scan", ConversionMode.METADATA_SCAN)
    job_id = job.id

    queue = mgr.subscribe(job_id)
    await mgr.finish_external_job(job_id, success=False, error_message="boom")

    payload = queue.get_nowait()
    assert payload["type"] == "error"
    assert payload["status"] == JobStatus.FAILED.value
    assert payload["error_message"] == "boom"


@pytest.mark.asyncio
async def test_finish_external_job_sets_progress_100_on_success():
    mgr = _make_manager()
    job = mgr.create_external_job("Scan", ConversionMode.METADATA_SCAN)
    job_id = job.id
    await mgr.update_external_job(job_id, progress=97)

    await mgr.finish_external_job(job_id, success=True)

    # Job is archived, retrieve from archive
    archived_job, _ = mgr._archived_jobs[job_id]
    assert archived_job.progress == 100


@pytest.mark.asyncio
async def test_finish_external_job_noop_for_unknown_id():
    """Finishing a non-existent job should not raise."""
    mgr = _make_manager()
    await mgr.finish_external_job("unknown", success=True)


# ---------------------------------------------------------------------------
# METADATA_SCAN non-cancellability
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_job_returns_false_for_metadata_scan():
    mgr = _make_manager()
    job = mgr.create_external_job("Scan", ConversionMode.METADATA_SCAN)
    job_id = job.id

    result = await mgr.cancel_job(job_id)

    assert result is False
    # Job must still be registered (not touched)
    assert job_id in mgr.jobs
    assert mgr.jobs[job_id].status == JobStatus.PROCESSING


@pytest.mark.asyncio
async def test_cancel_all_skips_metadata_scan_jobs():
    mgr = _make_manager()
    scan_job = mgr.create_external_job("Scan", ConversionMode.METADATA_SCAN)
    scan_id = scan_job.id

    result = await mgr.cancel_all_jobs()

    # cancel_all should report 0 requested for metadata scans
    requested = result.get("requested", 0)
    assert requested == 0
    # The scan job must not have been cancelled
    assert scan_id in mgr.jobs
    assert mgr.jobs[scan_id].status == JobStatus.PROCESSING
