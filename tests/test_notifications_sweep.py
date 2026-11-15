"""Notifications (#293) through the real ``romm_auto.sweep()``: the sweep hooks.

The unit tests in test_notifications.py call the hooks directly; these prove the
sweep itself calls them, with RomM and the queue stubbed the way test_romm.py
does.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from models import JobStatus
from routes import romm as romm_routes
from services import db as _db
from services import notifications
from services.job_manager import job_manager
from services.romm import settings as romm_settings
from services.rules import sweep as rules_sweep
from services.tools import registry


@pytest.fixture(autouse=True)
def _env(tmp_path: Path):
    if _db.engine is not None:
        _db.engine.dispose()
    _db.init_engine(str(tmp_path / "compressatorium.db"), create_schema=True)
    romm_settings.reset_for_tests()
    notifications._reset_for_tests()
    yield
    romm_settings.reset_for_tests()
    if _db.engine is not None:
        _db.engine.dispose()
    _db.engine = None
    _db.SessionLocal = None


class _Job:
    def __init__(self, path: str, job_id: str, owner: str | None = None) -> None:
        self.file_path, self.id, self.owner = path, job_id, owner
        self.filename = Path(path).name
        self.status = JobStatus.PROCESSING
        self.mode = "dolphin_rvz"
        self.error_message = None
        self.input_bytes = self.output_size = None
        self.deleted_paths: list[str] = []


@pytest.fixture
def sent(monkeypatch):
    out: list[notifications.Event] = []
    monkeypatch.setitem(notifications.TRANSPORTS, "webhook", lambda url, ev: out.append(ev))
    return out


async def _setup(tmp_path: Path):
    from services.preferences_store import preferences_store
    from services.romm import auto as romm_auto

    lib = tmp_path / "library" / "roms" / "gc"
    lib.mkdir(parents=True)
    (lib / "Game.iso").write_bytes(b"\0" * 32)
    await romm_settings.save({
        "url": "http://romm:8080", "library_root": str(tmp_path / "library"),
    })
    await romm_auto.set_rules({"7": {"mode": "dolphin_rvz", "enabled": True}})
    await preferences_store.put(notifications.PREF_KEY, {"destinations": [
        {"id": "d1", "kind": "webhook", "url": "https://hooks.example/t", "label": ""}]})
    return [{
        "id": 1, "name": "Game", "full_path": "roms/gc/Game.iso",
        "fs_name": "Game.iso", "fs_size_bytes": 32, "platform_slug": "ngc",
    }]


@pytest.mark.asyncio
async def test_a_sweep_sends_one_message_once_its_jobs_end(tmp_path, sent):
    from services.romm import auto as romm_auto

    roms = await _setup(tmp_path)
    made: list[_Job] = []

    async def _batch(paths, mode, **kwargs):
        made.extend(_Job(p, f"job-{i}", kwargs.get("owner")) for i, p in enumerate(paths))
        return list(made)

    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(job_manager, "create_batch_jobs", _batch), \
            patch.object(job_manager, "get_active_job_candidates", return_value=[]), \
            patch.object(rules_sweep, "is_within_configured_volumes", return_value=True):
        result = await romm_auto.sweep(ignore_schedule=True)
    assert result["queued"] == 1

    await notifications.drain()
    assert sent == []  # the job is still running: nothing to report yet

    made[0].status = JobStatus.COMPLETED
    await notifications.on_job_finished(made[0])
    await notifications.drain()
    assert [e.kind for e in sent] == ["sweep_finished"]
    assert sent[0].data["queued"] == 1 and sent[0].data["completed"] == 1


@pytest.mark.asyncio
async def test_a_preview_sends_nothing(tmp_path, sent):
    from services.romm import auto as romm_auto

    roms = await _setup(tmp_path)
    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(job_manager, "get_active_job_candidates", return_value=[]), \
            patch.object(rules_sweep, "is_within_configured_volumes", return_value=True):
        await romm_auto.sweep(ignore_schedule=True, dry_run=True)
    await notifications.drain()
    assert sent == []


@pytest.mark.asyncio
async def test_a_platform_that_keeps_failing_sends_one_message_over_many_ticks(
    tmp_path, sent,
):
    """The error path skips the schedule clock, so every tick re-runs it."""
    from services.romm import auto as romm_auto

    roms = await _setup(tmp_path)
    tool = registry.for_mode("dolphin_rvz")
    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(job_manager, "get_active_job_candidates", return_value=[]), \
            patch.object(type(tool), "is_ready", AsyncMock(return_value=False)), \
            patch.object(rules_sweep, "is_within_configured_volumes", return_value=True):
        for _ in range(10):
            result = await romm_auto.sweep()
            assert result["errors"] == [{"platform_id": 7, "error": "tool_not_ready"}]
    await notifications.drain()
    assert len(sent) == 1 and "tool_not_ready" in sent[0].body


@pytest.mark.asyncio
async def test_a_sweep_that_fails_after_queueing_still_closes_and_reports(tmp_path, sent):
    from services.romm import auto as romm_auto

    roms = await _setup(tmp_path)
    made: list[_Job] = []

    async def _batch(paths, mode, **kwargs):
        made.extend(_Job(p, f"job-{i}", kwargs.get("owner")) for i, p in enumerate(paths))
        return list(made)

    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(job_manager, "create_batch_jobs", _batch), \
            patch.object(job_manager, "get_active_job_candidates", return_value=[]), \
            patch.object(rules_sweep, "_record_run", AsyncMock(side_effect=RuntimeError("db"))), \
            patch.object(rules_sweep, "is_within_configured_volumes", return_value=True), \
            pytest.raises(RuntimeError):
        await romm_auto.sweep(ignore_schedule=True)

    assert notifications._open_sweep is None  # closed, not left to swallow the next one
    made[0].status = JobStatus.FAILED
    made[0].error_message = "boom"
    await notifications.on_job_finished(made[0])
    await notifications.drain()
    assert len(sent) == 1 and sent[0].data["failed"] == 1
    assert "sweep_failed" in sent[0].body


@pytest.mark.asyncio
async def test_an_unrelated_run_does_not_re_alert_a_known_failure(tmp_path, sent):
    from services.romm import auto as romm_auto

    roms = await _setup(tmp_path)
    tool = registry.for_mode("dolphin_rvz")
    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(job_manager, "get_active_job_candidates", return_value=[]), \
            patch.object(type(tool), "is_ready", AsyncMock(return_value=False)), \
            patch.object(rules_sweep, "is_within_configured_volumes", return_value=True):
        await romm_auto.sweep()
        await romm_auto.sweep(platform_ids=[])  # inspects nothing
        await romm_auto.sweep()
    await notifications.drain()
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_a_sweep_job_that_fails_during_batch_creation_is_not_sent_twice(
    tmp_path, sent, monkeypatch,
):
    """create_jobs_atomic enqueues, then awaits _prune_jobs: a job can fail in
    that window, before the sweep has seen the batch call return."""
    import asyncio

    from models import ConversionMode
    from services import job_manager as jm

    src = tmp_path / "Game.iso"
    src.write_bytes(b"x")
    manager = jm.JobManager(max_concurrent=1, max_job_history=5)
    manager.add_terminal_listener(notifications.on_job_finished)
    from services.preferences_store import preferences_store
    await preferences_store.put(notifications.PREF_KEY, {"destinations": [
        {"id": "d1", "kind": "webhook", "url": "https://hooks.example/t", "label": ""}]})

    async def failing_prune():
        for job in manager.get_all_jobs():
            job.status = JobStatus.FAILED
            job.error_message = "no disc"
            manager._notify_terminal(job)
        await asyncio.sleep(0.05)  # the listener task runs before the call returns

    monkeypatch.setattr(manager, "_prune_jobs", failing_prune)
    owner = notifications.sweep_owner()
    jobs = await manager.create_batch_jobs(
        [str(src)], ConversionMode.CREATECD, output_paths={str(src): str(tmp_path / "g.chd")},
        owner=owner,
    )
    try:
        notifications.sweep_jobs_queued(jobs)
        notifications.sweep_finished({"queued": 1, "errors": [], "platforms": []})
        await notifications.drain()
    finally:
        for job in jobs:
            jm.concurrency_manager.release(job.id)
    assert [e.kind for e in sent] == ["sweep_finished"]
    assert sent[0].data["failed"] == 1 and sent[0].data["queued"] == 1


@pytest.mark.asyncio
async def test_cancelling_a_sweep_is_not_reported_as_a_failure(tmp_path, sent):
    import asyncio

    from services.romm import auto as romm_auto

    await _setup(tmp_path)
    with patch.object(
        rules_sweep, "_sweep_locked", AsyncMock(side_effect=asyncio.CancelledError),
    ), pytest.raises(asyncio.CancelledError):
        await romm_auto.sweep()
    await notifications.drain()
    assert sent == []


@pytest.mark.asyncio
async def test_a_sweep_job_whose_listener_runs_after_the_batch_returns_is_not_sent_twice(
    tmp_path, sent, monkeypatch,
):
    """The mirror order: the batch call resumes first and sees a finished job."""
    import asyncio

    from models import ConversionMode
    from services import job_manager as jm
    from services.preferences_store import preferences_store

    src = tmp_path / "Game.iso"
    src.write_bytes(b"x")
    manager = jm.JobManager(max_concurrent=1, max_job_history=5)
    manager.add_terminal_listener(notifications.on_job_finished)
    await preferences_store.put(notifications.PREF_KEY, {"destinations": [
        {"id": "d1", "kind": "webhook", "url": "https://hooks.example/t", "label": ""}]})
    loop = asyncio.get_running_loop()

    async def late_listener_prune():
        for job in manager.get_all_jobs():
            job.status = JobStatus.FAILED
            job.error_message = "no disc"
            loop.call_later(0.1, manager._notify_terminal, job)  # after the call returns

    monkeypatch.setattr(manager, "_prune_jobs", late_listener_prune)
    owner = notifications.sweep_owner()
    jobs = await manager.create_batch_jobs(
        [str(src)], ConversionMode.CREATECD, output_paths={str(src): str(tmp_path / "g.chd")},
        owner=owner,
    )
    try:
        assert jobs[0].status == JobStatus.FAILED  # already finished when we see it
        notifications.sweep_jobs_queued(jobs)
        notifications.sweep_finished({"queued": 1, "errors": [], "platforms": []})
        await asyncio.sleep(0.3)
        await notifications.drain()
    finally:
        for job in jobs:
            jm.concurrency_manager.release(job.id)
    assert [e.kind for e in sent] == ["sweep_finished"]


@pytest.mark.asyncio
async def test_the_real_sweep_claims_its_batch_before_the_queue_call(tmp_path, sent):
    """Fails if the sweep stops passing its owner token to the queue: a job that fails inside
    the queue call would then be sent alone as well as in the sweep message."""
    from services.romm import auto as romm_auto

    roms = await _setup(tmp_path)

    async def _batch(paths, mode, **kwargs):
        jobs = [_Job(p, f"job-{i}", kwargs.get("owner")) for i, p in enumerate(paths)]
        for job in jobs:  # ends, and its listener runs, before the call returns
            job.status = JobStatus.FAILED
            job.error_message = "no disc"
            await notifications.on_job_finished(job)
        return jobs

    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(job_manager, "create_batch_jobs", _batch), \
            patch.object(job_manager, "get_active_job_candidates", return_value=[]), \
            patch.object(rules_sweep, "is_within_configured_volumes", return_value=True):
        await romm_auto.sweep(ignore_schedule=True)
    await notifications.drain()
    assert [e.kind for e in sent] == ["sweep_finished"]
    assert sent[0].data["failed"] == 1


@pytest.mark.asyncio
async def test_a_manual_job_that_fails_during_preflight_is_not_absorbed_into_the_sweep(
    tmp_path, sent, monkeypatch,
):
    """A manual failure on the same Source while create_jobs_atomic awaits its
    reservation keys: one job_failed, and the sweep counts only its own job."""
    import asyncio
    from datetime import datetime, timezone

    from models import ConversionJob, ConversionMode
    from services import job_manager as jm
    from services.preferences_store import preferences_store

    src = tmp_path / "Game.iso"
    src.write_bytes(b"x")
    manager = jm.JobManager(max_concurrent=1, max_job_history=5)
    manager.add_terminal_listener(notifications.on_job_finished)
    await preferences_store.put(notifications.PREF_KEY, {"destinations": [
        {"id": "d1", "kind": "webhook", "url": "https://hooks.example/t", "label": ""}]})
    real_keys = manager._reservation_keys
    now = datetime.now(timezone.utc)
    manual = ConversionJob(
        id="manual", file_path=str(src), filename="Game.iso", mode=ConversionMode.CREATECD,
        status=JobStatus.FAILED, created_at=now, completed_at=now, error_message="boom",
    )

    async def keys_after_a_manual_failure(*args, **kwargs):
        await notifications.on_job_finished(manual)  # ends while the sweep preflights
        await asyncio.sleep(0)
        return await real_keys(*args, **kwargs)

    monkeypatch.setattr(manager, "_reservation_keys", keys_after_a_manual_failure)
    owner = notifications.sweep_owner()
    jobs = await manager.create_batch_jobs(
        [str(src)], ConversionMode.CREATECD, output_paths={str(src): str(tmp_path / "g.chd")},
        owner=owner,
    )
    try:
        assert [j.owner for j in jobs] == [owner]
        notifications.sweep_jobs_queued(jobs)
        notifications.sweep_finished({"queued": 1, "errors": [], "platforms": []})
        jobs[0].status = JobStatus.COMPLETED
        await notifications.on_job_finished(jobs[0])
        await notifications.drain()
    finally:
        for job in jobs:
            jm.concurrency_manager.release(job.id)
    kinds = sorted(e.kind for e in sent)
    assert kinds == ["job_failed", "sweep_finished"]
    summary = next(e for e in sent if e.kind == "sweep_finished")
    assert summary.data["queued"] == 1 and summary.data["completed"] == 1
