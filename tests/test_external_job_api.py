"""Tests for JobManager external-job API and METADATA_SCAN non-cancellability."""

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
async def test_finish_external_job_success_keeps_job_in_live_jobs():
    """Completed external jobs must remain in self.jobs for the Clear Done flow."""
    mgr = _make_manager()
    job = mgr.create_external_job("Scan", ConversionMode.METADATA_SCAN)
    job_id = job.id

    await mgr.finish_external_job(job_id, success=True)

    # Job must still be listed (not deleted) so /api/jobs returns it
    assert job_id in mgr.jobs
    assert mgr.jobs[job_id].status == JobStatus.COMPLETED
    # Also archived for brief SSE-subscriber lookup
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

    # Job remains in self.jobs with progress=100
    assert mgr.jobs[job_id].progress == 100


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


# ---------------------------------------------------------------------------
# Sentinel file path
# ---------------------------------------------------------------------------

def test_create_external_job_uses_sentinel_file_path():
    """create_external_job must use a sentinel path, never an empty string."""
    mgr = _make_manager()
    job = mgr.create_external_job("Scan", ConversionMode.METADATA_SCAN)
    assert job.file_path.startswith("/__external_jobs__/")
    assert job.file_path != ""
    assert job.file_path != "/"


def test_sentinel_path_does_not_interfere_with_path_in_use_checks():
    """The sentinel path must not cause false positives in _is_path_in_use_by_other_job."""
    mgr = _make_manager()
    # Register a fake conversion job with a real-looking path
    conversion_job_id = "conv0001"
    from app.models import ConversionJob, JobStatus as JS
    from datetime import datetime, timezone
    conv_job = ConversionJob(
        id=conversion_job_id,
        file_path="/data/roms/game.iso",
        filename="game.iso",
        mode=ConversionMode.CREATECD,
        status=JS.PROCESSING,
        progress=0,
        created_at=datetime.now(timezone.utc),
        started_at=datetime.now(timezone.utc),
    )
    mgr.jobs[conversion_job_id] = conv_job

    # Register an external scan job
    scan_job = mgr.create_external_job("Scan", ConversionMode.METADATA_SCAN)

    # Path-in-use detection is unaffected by the scan job: conv_job genuinely
    # holds /data/roms/game.iso, so the check correctly returns True.
    assert mgr._is_path_in_use_by_other_job(scan_job.id, "/data/roms/game.iso")
    # More importantly: the conversion job's path check must not match the sentinel
    assert not mgr._is_path_in_use_by_other_job(conversion_job_id, "/__external_jobs__/" + scan_job.id)


# ---------------------------------------------------------------------------
# Backpressure / stuck detection exclusion
# ---------------------------------------------------------------------------

def test_metadata_scan_does_not_count_toward_queue_depth():
    mgr = _make_manager()
    mgr.create_external_job("Scan", ConversionMode.METADATA_SCAN)
    assert mgr.get_queue_depth() == 0


def test_metadata_scan_does_not_trigger_stuck_detection():
    """A running METADATA_SCAN with no conversion jobs must not be considered stuck."""
    mgr = _make_manager()
    # With only a METADATA_SCAN processing and no conversion jobs enqueued,
    # is_stuck must remain False.
    mgr.create_external_job("Scan", ConversionMode.METADATA_SCAN)
    assert mgr.is_stuck() is False


# ---------------------------------------------------------------------------
# Clear Done compatibility
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_completed_external_job_visible_for_clear_done():
    """Completed external jobs must stay in self.jobs so Clear Done can remove them."""
    mgr = _make_manager()
    job = mgr.create_external_job("Scan", ConversionMode.METADATA_SCAN)
    job_id = job.id
    await mgr.finish_external_job(job_id, success=True)

    # Job is still in the live dict (not deleted)
    assert job_id in mgr.jobs
    assert mgr.jobs[job_id].status == JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_finish_external_job_prunes_old_completed_jobs():
    """finish_external_job() must enforce max_job_history so old terminal jobs
    don't accumulate indefinitely (the just-finished job is preserved)."""
    mgr = JobManager(max_concurrent=1, max_job_history=3)

    # Fill history with 3 already-completed external jobs
    for i in range(3):
        j = mgr.create_external_job(f"OldScan{i}", ConversionMode.METADATA_SCAN)
        await mgr.finish_external_job(j.id, success=True)

    # All 3 are visible so far
    assert len(mgr.jobs) == 3

    # Adding a 4th and finishing it should prune the oldest to keep ≤ max_job_history
    j4 = mgr.create_external_job("NewScan", ConversionMode.METADATA_SCAN)
    await mgr.finish_external_job(j4.id, success=True)

    # After pruning, total should not exceed max_job_history (3)
    assert len(mgr.jobs) <= 3
    # The just-finished job must be preserved
    assert j4.id in mgr.jobs
