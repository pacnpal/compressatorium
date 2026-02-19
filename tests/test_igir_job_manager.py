"""Tests for igir job manager features: backpressure, archival, stuck detection, recovery."""
import asyncio
import contextlib
import time
from datetime import datetime, timezone

import pytest
from unittest.mock import AsyncMock, Mock, patch

from app.models import (
    ConversionJob,
    ConversionMode,
    IgirCommand,
    IgirJob,
    IgirJobCreateRequest,
    JobStatus,
)
from app.services.chdman import ConversionCancelled
from app.services.job_manager import JobManager, QueueBackpressureError


@pytest.fixture
def jm(monkeypatch, tmp_path):
    """Create a JobManager with igir support and mocked settings."""
    monkeypatch.setattr(
        "app.services.job_manager.settings.max_concurrent_jobs", 1,
    )
    monkeypatch.setattr(
        "app.services.job_manager.settings.max_job_history", 500,
    )
    monkeypatch.setattr(
        "app.services.job_manager.settings.max_igir_concurrent", 1,
    )
    monkeypatch.setattr(
        "app.services.job_manager.settings.max_queue_depth", 0,
    )
    monkeypatch.setattr(
        "app.services.job_manager.settings.concurrency_lock_dir",
        str(tmp_path / "locks"),
    )
    # Mock igir_service to avoid import issues
    monkeypatch.setattr(
        "app.services.job_manager.igir_service.build_options_summary",
        lambda req: "test summary",
    )
    monkeypatch.setattr(
        "app.services.job_manager.igir_service.build_command_preview",
        lambda req: "igir copy --input /data/roms --output /data/out",
    )
    return JobManager(max_concurrent=1, max_job_history=500)


@pytest.fixture
def basic_igir_request():
    return IgirJobCreateRequest(
        commands=[IgirCommand.COPY],
        input_paths=["/data/roms"],
        output_path="/data/out",
    )


# ──────────────── Backpressure ────────────────


class TestIgirBackpressure:
    @pytest.mark.asyncio
    async def test_no_backpressure_when_disabled(self, jm, basic_igir_request, monkeypatch):
        """When max_queue_depth=0, backpressure is disabled."""
        monkeypatch.setattr(
            "app.services.job_manager.settings.max_queue_depth", 0,
        )
        job = await jm.create_igir_job(basic_igir_request)
        assert job.status == JobStatus.QUEUED

    @pytest.mark.asyncio
    async def test_backpressure_rejects_when_full(self, jm, basic_igir_request, monkeypatch):
        """When queue is at capacity, new jobs are rejected."""
        monkeypatch.setattr(
            "app.services.job_manager.settings.max_queue_depth", 1,
        )
        # First job should succeed
        job1 = await jm.create_igir_job(basic_igir_request)
        assert job1.status == JobStatus.QUEUED

        # Second job should be rejected
        with pytest.raises(QueueBackpressureError):
            await jm.create_igir_job(basic_igir_request)

    @pytest.mark.asyncio
    async def test_backpressure_allows_after_completion(self, jm, basic_igir_request, monkeypatch):
        """After a job completes, new jobs can be created."""
        monkeypatch.setattr(
            "app.services.job_manager.settings.max_queue_depth", 1,
        )
        job1 = await jm.create_igir_job(basic_igir_request)
        # Mark as completed
        job1.status = JobStatus.COMPLETED

        # Now should be able to create another job
        job2 = await jm.create_igir_job(basic_igir_request)
        assert job2.status == JobStatus.QUEUED

    @pytest.mark.asyncio
    async def test_igir_backpressure_counts_conversion_jobs(
        self, jm, basic_igir_request, monkeypatch,
    ):
        """Creating an igir job should honor queued conversion jobs in max_queue_depth."""
        monkeypatch.setattr(
            "app.services.job_manager.settings.max_queue_depth", 1,
        )
        jm.jobs["conv-job"] = ConversionJob(
            id="conv-job",
            file_path="/data/game.iso",
            filename="game.iso",
            mode=ConversionMode.CREATECD,
            status=JobStatus.QUEUED,
            created_at=datetime.now(timezone.utc),
            output_path="/data/game.chd",
        )

        with pytest.raises(QueueBackpressureError):
            await jm.create_igir_job(basic_igir_request)

    @pytest.mark.asyncio
    async def test_conversion_backpressure_counts_igir_jobs(self, jm, monkeypatch):
        """Creating a conversion job should honor queued igir jobs in max_queue_depth."""
        monkeypatch.setattr(
            "app.services.job_manager.settings.max_queue_depth", 1,
        )
        jm._igir_jobs["igir-job"] = IgirJob(
            id="igir-job",
            commands=[IgirCommand.COPY],
            input_paths=["/data/roms"],
            output_path="/data/out",
            status=JobStatus.QUEUED,
            created_at=datetime.now(timezone.utc),
        )

        async with jm._create_lock:
            with pytest.raises(QueueBackpressureError):
                jm._enforce_queue_backpressure_locked(1)

    @pytest.mark.asyncio
    async def test_preview_counts_toward_backpressure(
        self, jm, basic_igir_request, monkeypatch,
    ):
        """Dry-run previews should consume queue-depth capacity while active."""
        monkeypatch.setattr(
            "app.services.job_manager.settings.max_queue_depth", 1,
        )

        preview_started = asyncio.Event()
        preview_release = asyncio.Event()

        async def _fake_run(_request, cancel_event=None):
            preview_started.set()
            yield {"progress": 0, "message": "starting", "phase": "starting"}
            await preview_release.wait()
            yield {"progress": 100, "message": "done", "phase": "done"}

        monkeypatch.setattr("app.services.job_manager.igir_service.run", _fake_run)

        async def _consume_preview():
            async for _ in jm.run_igir_preview(basic_igir_request):
                pass

        preview_task = asyncio.create_task(_consume_preview())
        await asyncio.wait_for(preview_started.wait(), timeout=1.0)

        with pytest.raises(QueueBackpressureError):
            await jm.create_igir_job(basic_igir_request)

        preview_release.set()
        await asyncio.wait_for(preview_task, timeout=1.0)

    @pytest.mark.asyncio
    async def test_preview_cancellation_while_waiting_releases_backpressure_slot(
        self, jm, basic_igir_request, monkeypatch,
    ):
        """Cancelled previews should release reserved capacity even before semaphore acquire."""
        monkeypatch.setattr(
            "app.services.job_manager.settings.max_queue_depth", 1,
        )

        async def _consume_preview():
            async for _ in jm.run_igir_preview(basic_igir_request):
                pass

        await jm._igir_semaphore.acquire()
        preview_task = asyncio.create_task(_consume_preview())
        try:
            for _ in range(100):
                if jm._igir_preview_active == 1:
                    break
                await asyncio.sleep(0.01)
            assert jm._igir_preview_active == 1

            preview_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await preview_task
        finally:
            jm._igir_semaphore.release()

        assert jm._igir_preview_active == 0

        # Ensure leaked preview capacity does not block new submissions.
        job = await jm.create_igir_job(basic_igir_request)
        assert job.status == JobStatus.QUEUED


# ──────────────── Archival ────────────────


class TestIgirArchival:
    @pytest.mark.asyncio
    async def test_deleted_job_is_archived(self, jm, basic_igir_request):
        """Deleting a completed job archives it for lookup."""
        job = await jm.create_igir_job(basic_igir_request)
        job.status = JobStatus.COMPLETED
        job_id = job.id

        await jm.delete_igir_job(job_id)

        # Job should no longer be in active list
        assert jm._igir_jobs.get(job_id) is None

        # But should be retrievable via get_igir_job (archived)
        archived = jm.get_igir_job(job_id)
        assert archived is not None
        assert archived.id == job_id
        assert archived.status == JobStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_archived_job_ttl_expiry(self, jm, basic_igir_request):
        """Archived jobs expire after TTL."""
        job = await jm.create_igir_job(basic_igir_request)
        job.status = JobStatus.COMPLETED
        job_id = job.id

        await jm.delete_igir_job(job_id)

        # Manually expire the archived job
        jm._archived_igir_jobs[job_id] = (
            jm._archived_igir_jobs[job_id][0],
            time.monotonic() - jm.ARCHIVED_JOB_TTL_SECONDS - 10,
        )

        # After pruning, job should not be found
        result = jm.get_igir_job(job_id)
        assert result is None

    @pytest.mark.asyncio
    async def test_clear_completed_archives_jobs(self, jm, basic_igir_request):
        """clear_completed_igir_jobs archives before removing."""
        job = await jm.create_igir_job(basic_igir_request)
        job.status = JobStatus.COMPLETED
        job_id = job.id

        deleted = await jm.clear_completed_igir_jobs()
        assert job_id in deleted

        # Should be in archive
        assert job_id in jm._archived_igir_jobs

    @pytest.mark.asyncio
    async def test_archived_lookup_refresh_moves_entry_to_lru_tail(
        self, jm, basic_igir_request,
    ):
        """Refreshing an archived job should move it behind older entries."""
        first = await jm.create_igir_job(basic_igir_request)
        second = await jm.create_igir_job(basic_igir_request)
        first.status = JobStatus.COMPLETED
        second.status = JobStatus.COMPLETED
        await jm.delete_igir_job(first.id)
        await jm.delete_igir_job(second.id)

        refreshed = jm.get_igir_job(first.id)
        assert refreshed is not None
        assert list(jm._archived_igir_jobs.keys())[-1] == first.id

        stale_second = jm._archived_igir_jobs[second.id][0]
        jm._archived_igir_jobs[second.id] = (
            stale_second,
            time.monotonic() - jm.ARCHIVED_JOB_TTL_SECONDS - 10,
        )

        jm._prune_archived_igir_jobs()

        assert second.id not in jm._archived_igir_jobs
        assert first.id in jm._archived_igir_jobs

    @pytest.mark.asyncio
    async def test_prune_igir_jobs_limits_history(self, jm, basic_igir_request):
        """Pruning should remove terminal igir jobs when history exceeds max."""
        jm.max_job_history = 1

        first = await jm.create_igir_job(basic_igir_request)
        second = await jm.create_igir_job(basic_igir_request)
        third = await jm.create_igir_job(basic_igir_request)

        first.status = JobStatus.COMPLETED
        second.status = JobStatus.FAILED
        third.status = JobStatus.QUEUED

        await jm._prune_igir_jobs(exclude_id=third.id)

        assert len(jm._igir_jobs) == 1
        assert third.id in jm._igir_jobs
        assert first.id not in jm._igir_jobs
        assert second.id not in jm._igir_jobs
        assert first.id in jm._archived_igir_jobs
        assert second.id in jm._archived_igir_jobs

    @pytest.mark.asyncio
    async def test_cancel_queued_igir_job_prunes_older_terminal_jobs(
        self, jm, basic_igir_request,
    ):
        """Cancelling a queued job should trigger igir history pruning."""
        jm.max_job_history = 1

        completed = await jm.create_igir_job(basic_igir_request)
        completed.status = JobStatus.COMPLETED
        queued = await jm.create_igir_job(basic_igir_request)

        assert await jm.cancel_igir_job(queued.id) is True

        assert completed.id not in jm._igir_jobs
        assert completed.id in jm._archived_igir_jobs


# ──────────────── Stuck Detection ────────────────


class TestIgirStuckStatus:
    @pytest.mark.asyncio
    async def test_not_stuck_when_empty(self, jm):
        """Empty queue is not stuck."""
        status = jm.igir_stuck_status()
        assert status["is_stuck"] is False
        assert status["queued_count"] == 0
        assert status["processing_count"] == 0

    @pytest.mark.asyncio
    async def test_not_stuck_when_processing(self, jm, basic_igir_request):
        """Queue with processing job is not stuck."""
        job = await jm.create_igir_job(basic_igir_request)
        job.status = JobStatus.PROCESSING

        status = jm.igir_stuck_status()
        assert status["is_stuck"] is False

    @pytest.mark.asyncio
    async def test_stuck_when_queued_not_processing(self, jm, basic_igir_request):
        """Queue with queued jobs but none processing is stuck."""
        job = await jm.create_igir_job(basic_igir_request)
        assert job.status == JobStatus.QUEUED

        status = jm.igir_stuck_status()
        assert status["is_stuck"] is True
        assert status["queued_count"] == 1
        assert status["processing_count"] == 0

    @pytest.mark.asyncio
    async def test_stuck_duration_tracked(self, jm, basic_igir_request):
        """Stuck duration is tracked across calls."""
        job = await jm.create_igir_job(basic_igir_request)

        status1 = jm.igir_stuck_status()
        assert status1["is_stuck"] is True
        # First call sets stuck_detected_at, so stuck_for_seconds should be near 0
        assert "stuck_for_seconds" not in status1 or status1.get("stuck_for_seconds", 0) >= 0

        # Second call should show duration
        status2 = jm.igir_stuck_status()
        assert status2["is_stuck"] is True
        assert "stuck_for_seconds" in status2


# ──────────────── Recovery ────────────────


class TestIgirRecovery:
    @pytest.mark.asyncio
    async def test_recovery_requeues_jobs(self, jm, basic_igir_request):
        """Recovery re-queues queued jobs that are missing from the queue."""
        job = await jm.create_igir_job(basic_igir_request)
        assert job.status == JobStatus.QUEUED

        # Simulate a stuck state where the queued job was dropped from the queue.
        _ticket, queued_job_id = jm._igir_queue.get_nowait()
        jm._igir_queue.task_done()
        assert queued_job_id == job.id
        jm._igir_queued_job_ids.discard(job.id)

        result = await jm.recover_igir_stuck()
        assert result["success"] is True
        assert result["requeued_jobs"] == 1

    @pytest.mark.asyncio
    async def test_recovery_skips_already_queued_jobs(self, jm, basic_igir_request):
        """Recovery should not duplicate queue entries for already queued jobs."""
        job = await jm.create_igir_job(basic_igir_request)
        assert job.status == JobStatus.QUEUED
        assert jm._igir_queue.qsize() == 1

        result = await jm.recover_igir_stuck()
        assert result["success"] is True
        assert result["requeued_jobs"] == 0
        assert jm._igir_queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_recovery_cooldown(self, jm, basic_igir_request):
        """Recovery respects cooldown period."""
        job = await jm.create_igir_job(basic_igir_request)

        # First recovery should succeed
        result1 = await jm.recover_igir_stuck()
        assert result1["success"] is True

        # Second recovery within cooldown should fail
        result2 = await jm.recover_igir_stuck()
        assert result2["success"] is False
        assert "cooldown_remaining" in result2

    @pytest.mark.asyncio
    async def test_recovery_after_cooldown(self, jm, basic_igir_request):
        """Recovery succeeds after cooldown expires."""
        job = await jm.create_igir_job(basic_igir_request)

        result1 = await jm.recover_igir_stuck()
        assert result1["success"] is True

        # Simulate cooldown expiry
        jm._last_igir_stuck_recovery_at = (
            time.monotonic() - jm.STUCK_RECOVERY_COOLDOWN_SECONDS - 1
        )

        result2 = await jm.recover_igir_stuck()
        assert result2["success"] is True

    @pytest.mark.asyncio
    async def test_recovery_first_attempt_ignores_zero_timestamp_on_fresh_uptime(
        self, jm, basic_igir_request, monkeypatch,
    ):
        """Initial recovery should not be throttled when last recovery timestamp is 0."""
        await jm.create_igir_job(basic_igir_request)
        monkeypatch.setattr("app.services.job_manager.time.monotonic", lambda: 10.0)

        result = await jm.recover_igir_stuck()
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_recovery_requires_true_stuck_state(self, jm, basic_igir_request):
        """Recovery should not run when there is active igir processing."""
        processing_job = await jm.create_igir_job(basic_igir_request)
        queued_job = await jm.create_igir_job(basic_igir_request)
        processing_job.status = JobStatus.PROCESSING

        # Simulate dispatcher dequeuing jobs (including one waiting on semaphore).
        while not jm._igir_queue.empty():
            _ticket, dequeued_job_id = jm._igir_queue.get_nowait()
            jm._igir_queue.task_done()
            jm._igir_queued_job_ids.discard(dequeued_job_id)

        assert queued_job.status == JobStatus.QUEUED
        assert jm._igir_queue.qsize() == 0

        result = await jm.recover_igir_stuck()
        assert result["success"] is False
        assert result["queued_jobs"] == 1
        assert result["processing_jobs"] == 1
        assert jm._igir_queue.qsize() == 0
        assert jm._last_igir_stuck_recovery_at == 0


class TestConversionRecovery:
    @pytest.mark.asyncio
    async def test_recovery_first_attempt_ignores_zero_timestamp_on_fresh_uptime(
        self, jm, monkeypatch,
    ):
        """Conversion recovery should not cooldown-throttle when no prior recovery ran."""
        monkeypatch.setattr("app.services.job_manager.time.monotonic", lambda: 10.0)
        monkeypatch.setattr(
            "app.services.job_manager.lock_manager.cleanup_stale_locks_periodic",
            lambda: 0,
        )

        result = await jm.recover_from_stuck_state()
        assert result["success"] is True


class TestIgirDispatcher:
    @pytest.mark.asyncio
    async def test_dispatcher_cleans_metadata_for_cancelled_queued_job(
        self, jm, basic_igir_request,
    ):
        """Queued cancelled jobs should release request/progress bookkeeping."""
        job = await jm.create_igir_job(basic_igir_request)
        await jm.cancel_igir_job(job.id)

        assert job.id in jm._igir_requests
        assert job.id in jm._igir_last_progress_at

        jm._running = True
        dispatcher_task = asyncio.create_task(jm._igir_dispatcher_loop())
        try:
            await asyncio.wait_for(jm._igir_queue.join(), timeout=1.0)
        finally:
            jm._running = False
            dispatcher_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await dispatcher_task

        assert job.id not in jm._igir_requests
        assert job.id not in jm._igir_last_progress_at
        assert job.id not in jm._igir_cancel_events

    @pytest.mark.asyncio
    async def test_dispatcher_runs_jobs_in_parallel_when_allowed(self, monkeypatch, tmp_path):
        """Igir dispatcher should schedule jobs concurrently when max_igir_concurrent > 1."""
        monkeypatch.setattr(
            "app.services.job_manager.settings.max_concurrent_jobs", 1,
        )
        monkeypatch.setattr(
            "app.services.job_manager.settings.max_job_history", 500,
        )
        monkeypatch.setattr(
            "app.services.job_manager.settings.max_igir_concurrent", 2,
        )
        monkeypatch.setattr(
            "app.services.job_manager.settings.max_queue_depth", 0,
        )
        monkeypatch.setattr(
            "app.services.job_manager.settings.concurrency_lock_dir",
            str(tmp_path / "locks"),
        )
        monkeypatch.setattr(
            "app.services.job_manager.igir_service.build_options_summary",
            lambda req: "test summary",
        )

        local_jm = JobManager(max_concurrent=1, max_job_history=500)
        request = IgirJobCreateRequest(
            commands=[IgirCommand.COPY],
            input_paths=["/data/roms"],
            output_path="/data/out",
        )
        await local_jm.create_igir_job(request)
        await local_jm.create_igir_job(request)

        started: list[str] = []
        both_started = asyncio.Event()
        release_workers = asyncio.Event()

        async def fake_process(job_id: str):
            started.append(job_id)
            if len(started) >= 2:
                both_started.set()
            await release_workers.wait()

        monkeypatch.setattr(local_jm, "_process_igir_job", fake_process)

        local_jm._running = True
        dispatcher_task = asyncio.create_task(local_jm._igir_dispatcher_loop())
        try:
            await asyncio.wait_for(both_started.wait(), timeout=1.0)
            release_workers.set()
            await asyncio.wait_for(local_jm._igir_queue.join(), timeout=1.0)
        finally:
            local_jm._running = False
            dispatcher_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await dispatcher_task

    @pytest.mark.asyncio
    async def test_dispatcher_cleans_metadata_when_cancelled_waiting_for_semaphore(
        self, monkeypatch, tmp_path,
    ):
        """Cancelled jobs should release bookkeeping even if cancelled before _process_igir_job starts."""
        monkeypatch.setattr(
            "app.services.job_manager.settings.max_concurrent_jobs", 1,
        )
        monkeypatch.setattr(
            "app.services.job_manager.settings.max_job_history", 500,
        )
        monkeypatch.setattr(
            "app.services.job_manager.settings.max_igir_concurrent", 2,
        )
        monkeypatch.setattr(
            "app.services.job_manager.settings.max_queue_depth", 0,
        )
        monkeypatch.setattr(
            "app.services.job_manager.settings.concurrency_lock_dir",
            str(tmp_path / "locks"),
        )
        monkeypatch.setattr(
            "app.services.job_manager.igir_service.build_options_summary",
            lambda req: "test summary",
        )
        monkeypatch.setattr(
            "app.services.job_manager.igir_service.build_command_preview",
            lambda req: "igir copy --input /data/roms --output /data/out",
        )

        local_jm = JobManager(max_concurrent=1, max_job_history=500)
        request = IgirJobCreateRequest(
            commands=[IgirCommand.COPY],
            input_paths=["/data/roms"],
            output_path="/data/out",
        )

        first = await local_jm.create_igir_job(request)
        second = await local_jm.create_igir_job(request)
        third = await local_jm.create_igir_job(request)

        started = 0
        both_started = asyncio.Event()
        release_workers = asyncio.Event()

        async def fake_run(_request, cancel_event=None):
            nonlocal started
            started += 1
            if started >= 2:
                both_started.set()
            yield {"progress": 1, "message": "working", "phase": "starting"}
            while not release_workers.is_set():
                if cancel_event and cancel_event.is_set():
                    raise ConversionCancelled("cancelled")
                await asyncio.sleep(0.01)
            yield {"progress": 100, "message": "done", "phase": "done"}

        monkeypatch.setattr("app.services.job_manager.igir_service.run", fake_run)

        local_jm._running = True
        dispatcher_task = asyncio.create_task(local_jm._igir_dispatcher_loop())
        try:
            await asyncio.wait_for(both_started.wait(), timeout=1.0)

            assert await local_jm.cancel_igir_job(third.id) is True
            assert local_jm.get_igir_job(third.id).status == JobStatus.CANCELLED

            release_workers.set()
            await asyncio.wait_for(local_jm._igir_queue.join(), timeout=2.0)

            # Let worker tasks finish their cleanup.
            await asyncio.sleep(0.05)
        finally:
            local_jm._running = False
            dispatcher_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await dispatcher_task

        assert first.status in {JobStatus.COMPLETED, JobStatus.CANCELLED}
        assert second.status in {JobStatus.COMPLETED, JobStatus.CANCELLED}
        assert third.status == JobStatus.CANCELLED
        assert third.id not in local_jm._igir_requests
        assert third.id not in local_jm._igir_last_progress_at
        assert third.id not in local_jm._igir_cancel_events


class TestIgirSubscribers:
    @pytest.mark.asyncio
    async def test_terminal_update_replaces_full_subscriber_queue(self, jm):
        """Terminal events should be delivered even when a subscriber queue is full."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        queue.put_nowait({"type": "progress", "job_id": "abc12345"})
        jm._igir_subscribers["abc12345"] = [queue]

        await jm._notify_igir_subscribers(
            "abc12345",
            {"type": "complete", "job_id": "abc12345", "status": JobStatus.COMPLETED.value},
        )

        assert queue.qsize() == 1
        update = queue.get_nowait()
        assert update["type"] == "complete"


# ──────────────── Route Tests ────────────────


class TestIgirStuckRoutes:
    @pytest.mark.asyncio
    async def test_stuck_status_endpoint(self, monkeypatch):
        """Stuck status endpoint returns status info."""
        from app.routes import igir as igir_routes

        mock_jm = Mock()
        mock_jm.igir_stuck_status = Mock(return_value={
            "is_stuck": False,
            "queued_count": 0,
            "processing_count": 0,
        })
        monkeypatch.setattr(igir_routes, "job_manager", mock_jm)

        result = await igir_routes.igir_stuck_status()
        assert result["is_stuck"] is False
        mock_jm.igir_stuck_status.assert_called_once()

    @pytest.mark.asyncio
    async def test_recover_endpoint_success(self, monkeypatch):
        """Recovery endpoint returns result on success."""
        from app.routes import igir as igir_routes

        mock_jm = Mock()
        mock_jm.recover_igir_stuck = AsyncMock(return_value={
            "success": True,
            "message": "Recovery attempt completed",
            "requeued_jobs": 1,
            "queued_jobs": 1,
            "processing_jobs": 0,
        })
        monkeypatch.setattr(igir_routes, "job_manager", mock_jm)

        result = await igir_routes.recover_igir_jobs()
        assert result["success"] is True
        assert result["requeued_jobs"] == 1

    @pytest.mark.asyncio
    async def test_recover_endpoint_cooldown(self, monkeypatch):
        """Recovery endpoint returns 429 during cooldown."""
        from fastapi import HTTPException
        from app.routes import igir as igir_routes

        mock_jm = Mock()
        mock_jm.recover_igir_stuck = AsyncMock(return_value={
            "success": False,
            "message": "Recovery attempted too recently, please wait",
            "cooldown_remaining": 45,
        })
        monkeypatch.setattr(igir_routes, "job_manager", mock_jm)

        with pytest.raises(HTTPException) as exc_info:
            await igir_routes.recover_igir_jobs()

        assert exc_info.value.status_code == 429


# ──────────────── Command Preview ────────────────


class TestIgirCommandPreview:
    @pytest.mark.asyncio
    async def test_command_preview_stored_on_job(self, jm, basic_igir_request):
        """Command preview is stored on the job at creation time."""
        job = await jm.create_igir_job(basic_igir_request)
        assert job.command_preview == "igir copy --input /data/roms --output /data/out"

    @pytest.mark.asyncio
    async def test_command_preview_persists_after_completion(self, jm, basic_igir_request):
        """Command preview persists when job status changes."""
        job = await jm.create_igir_job(basic_igir_request)
        job.status = JobStatus.COMPLETED
        retrieved = jm.get_igir_job(job.id)
        assert retrieved.command_preview == "igir copy --input /data/roms --output /data/out"


# ──────────────── Output Log ────────────────


class TestIgirOutputLog:
    @pytest.mark.asyncio
    async def test_output_log_none_initially(self, jm, basic_igir_request):
        """Output log is not available for a freshly created job."""
        job = await jm.create_igir_job(basic_igir_request)
        assert jm.get_igir_job_log(job.id) is None

    @pytest.mark.asyncio
    async def test_output_log_stored_on_completion(self, jm, basic_igir_request):
        """Output log is stored when provided via progress update."""
        job = await jm.create_igir_job(basic_igir_request)
        log_lines = ["Scanning...", "Found 100 files", "Done"]
        jm._igir_output_logs[job.id] = log_lines
        assert jm.get_igir_job_log(job.id) == log_lines

    @pytest.mark.asyncio
    async def test_output_log_cleaned_on_delete(self, jm, basic_igir_request):
        """Output log is cleaned up when job is deleted."""
        job = await jm.create_igir_job(basic_igir_request)
        job.status = JobStatus.COMPLETED
        jm._igir_output_logs[job.id] = ["line1", "line2"]

        await jm.delete_igir_job(job.id)
        assert jm.get_igir_job_log(job.id) is None

    @pytest.mark.asyncio
    async def test_output_log_missing_job_returns_none(self, jm):
        """Requesting log for non-existent job returns None."""
        assert jm.get_igir_job_log("nonexistent") is None


# ──────────────── Log Route ────────────────


class TestIgirLogRoute:
    @pytest.mark.asyncio
    async def test_log_endpoint_returns_log(self, monkeypatch):
        """Log endpoint returns the stored output log."""
        from datetime import datetime, timezone
        from app.routes import igir as igir_routes

        mock_jm = Mock()
        mock_jm.get_igir_job = Mock(return_value=IgirJob(
            id="abc12345",
            commands=[IgirCommand.COPY],
            input_paths=["/data/roms"],
            output_path="/data/out",
            status=JobStatus.COMPLETED,
            command_preview="igir copy --input /data/roms --output /data/out",
            created_at=datetime.now(timezone.utc),
        ))
        mock_jm.get_igir_job_log = Mock(return_value=["Scanning...", "Found 50 files", "Done"])
        monkeypatch.setattr(igir_routes, "job_manager", mock_jm)

        result = await igir_routes.get_igir_job_log("abc12345")
        assert result["job_id"] == "abc12345"
        assert result["line_count"] == 3
        assert result["lines"] == ["Scanning...", "Found 50 files", "Done"]
        assert result["command_preview"] == "igir copy --input /data/roms --output /data/out"

    @pytest.mark.asyncio
    async def test_log_endpoint_empty_log(self, monkeypatch):
        """Log endpoint returns empty log when none available."""
        from datetime import datetime, timezone
        from app.routes import igir as igir_routes

        mock_jm = Mock()
        mock_jm.get_igir_job = Mock(return_value=IgirJob(
            id="abc12345",
            commands=[IgirCommand.COPY],
            input_paths=["/data/roms"],
            output_path="/data/out",
            status=JobStatus.QUEUED,
            created_at=datetime.now(timezone.utc),
        ))
        mock_jm.get_igir_job_log = Mock(return_value=None)
        monkeypatch.setattr(igir_routes, "job_manager", mock_jm)

        result = await igir_routes.get_igir_job_log("abc12345")
        assert result["lines"] == []
        assert result["line_count"] == 0

    @pytest.mark.asyncio
    async def test_log_endpoint_404_for_missing_job(self, monkeypatch):
        """Log endpoint returns 404 for missing job."""
        from fastapi import HTTPException
        from app.routes import igir as igir_routes

        mock_jm = Mock()
        mock_jm.get_igir_job = Mock(return_value=None)
        monkeypatch.setattr(igir_routes, "job_manager", mock_jm)

        with pytest.raises(HTTPException) as exc_info:
            await igir_routes.get_igir_job_log("nonexistent")

        assert exc_info.value.status_code == 404
