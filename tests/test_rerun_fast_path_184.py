"""Re-run fast path: a verified output already on disk completes as a no-op
without re-spawning the converter (issue #184 site 1).

Harness mirrors ``tests/test_mode_parity_fixes.py``: build a real ``JobManager``,
stub the mode's underlying ``_service.convert`` so the converter is observable,
and drive a single job through ``_process_job`` directly.
"""

import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.models import ConversionMode, JobStatus
from app.services import job_manager as job_manager_module
from app.services.job_manager import JobManager


@pytest.fixture(autouse=True)
def _isolate_concurrency(monkeypatch):
    """Stub the process-global, file-backed FIFO concurrency manager.

    ``_process_job`` acquires/releases ``services.concurrency_manager`` before
    the fast path. Its FIFO lives in a shared on-disk lock dir, so within one
    pytest process an earlier test that leaks a lower-numbered ticket would make
    a real ``acquire()`` here block our job behind it forever. Stub the manager
    so these tests exercise the fast path deterministically and never touch the
    shared lock dir (and so they can't leak a ticket onto later tests either).
    """
    cm = job_manager_module.concurrency_manager
    monkeypatch.setattr(cm, "acquire", AsyncMock(return_value=True))
    monkeypatch.setattr(cm, "reserve_ticket", lambda *a, **k: 1)
    monkeypatch.setattr(cm, "release", lambda *a, **k: None)
    monkeypatch.setattr(cm, "release_ticket", lambda *a, **k: None)


def _stub_convert():
    """Return ``(calls, fake_convert)``; the stub records every invocation and
    writes its output file so size accounting has something to measure."""
    calls: list[tuple] = []

    async def fake_convert(input_path, output_path, mode, compression=None, cancel_event=None):
        calls.append((input_path, output_path, mode))
        Path(output_path).write_bytes(b"freshly-converted")
        yield {"progress": 100, "message": "Done"}

    return calls, fake_convert


@pytest.mark.asyncio
async def test_fast_path_completes_noop_when_output_verified(tmp_path: Path, monkeypatch):
    """Verified output already present → COMPLETED, converter NOT spawned."""
    source = tmp_path / "game.cue"
    output = tmp_path / "game.chd"
    source.write_bytes(b"source")
    output.write_bytes(b"previously-converted-and-verified")

    monkeypatch.setattr(job_manager_module.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(job_manager_module.settings, "data_mount_root", str(tmp_path))

    calls, fake_convert = _stub_convert()
    monkeypatch.setattr(
        job_manager_module.registry.for_mode("createcd")._service, "convert", fake_convert,
    )
    monkeypatch.setattr(
        job_manager_module.verification_store, "is_verified", AsyncMock(return_value=True),
    )

    manager = JobManager(max_concurrent=1, max_job_history=5)
    events: list[dict] = []

    async def capture(job_id, payload):
        events.append(payload)

    monkeypatch.setattr(manager, "_notify_subscribers", capture)

    job = await manager.create_job(
        str(source), ConversionMode.CREATECD, output_path=str(output),
    )
    await manager._process_job(job.id)

    assert job.status == JobStatus.COMPLETED
    assert calls == []  # the converter was never spawned
    assert job.output_size == os.path.getsize(str(output))
    complete = [e for e in events if e.get("type") == "complete"]
    assert complete and complete[-1]["verified"] is True
    assert complete[-1]["source_deleted"] is False
    # The pre-existing artifact is untouched.
    assert output.read_bytes() == b"previously-converted-and-verified"


@pytest.mark.asyncio
async def test_fast_path_skipped_when_not_verified(tmp_path: Path, monkeypatch):
    """Output present but NOT verified → no no-op; falls through to the existing
    'output already exists' lock rejection (converter still not spawned)."""
    source = tmp_path / "game.cue"
    output = tmp_path / "game.chd"
    source.write_bytes(b"source")
    output.write_bytes(b"unverified-output")

    monkeypatch.setattr(job_manager_module.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(job_manager_module.settings, "data_mount_root", str(tmp_path))

    calls, fake_convert = _stub_convert()
    monkeypatch.setattr(
        job_manager_module.registry.for_mode("createcd")._service, "convert", fake_convert,
    )
    monkeypatch.setattr(
        job_manager_module.verification_store, "is_verified", AsyncMock(return_value=False),
    )

    manager = JobManager(max_concurrent=1, max_job_history=5)
    job = await manager.create_job(
        str(source), ConversionMode.CREATECD, output_path=str(output),
    )
    await manager._process_job(job.id)

    assert job.status == JobStatus.FAILED
    assert "already exists" in (job.error_message or "")
    assert calls == []


@pytest.mark.asyncio
async def test_fast_path_skipped_for_overwrite_runs_converter(tmp_path: Path, monkeypatch):
    """An overwrite job regenerates even when a verified output exists: the fast
    path is skipped, _clear_existing_output drops the file, the converter runs."""
    source = tmp_path / "game.cue"
    output = tmp_path / "game.chd"
    source.write_bytes(b"source")
    output.write_bytes(b"stale-output")

    monkeypatch.setattr(job_manager_module.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(job_manager_module.settings, "data_mount_root", str(tmp_path))

    calls, fake_convert = _stub_convert()
    monkeypatch.setattr(
        job_manager_module.registry.for_mode("createcd")._service, "convert", fake_convert,
    )
    monkeypatch.setattr(
        job_manager_module.verification_store, "is_verified", AsyncMock(return_value=True),
    )
    # _clear_existing_output clears the tool-wide verification record on overwrite.
    monkeypatch.setattr(job_manager_module.verification_store, "clear", AsyncMock())
    # createcd runs the disc-ID embed after convert; a fake source has no ID.
    monkeypatch.setattr(job_manager_module, "disc_id_from_source", lambda p: None)

    manager = JobManager(max_concurrent=1, max_job_history=5)
    job = await manager.create_job(
        str(source), ConversionMode.CREATECD, output_path=str(output), allow_overwrite=True,
    )
    await manager._process_job(job.id)

    assert job.status == JobStatus.COMPLETED
    assert len(calls) == 1  # the converter ran
    assert output.read_bytes() == b"freshly-converted"


@pytest.mark.asyncio
async def test_fast_path_excludes_delete_on_verify(tmp_path: Path, monkeypatch):
    """A delete-on-verify job is deliberately excluded from the no-op fast path
    (its source deletion runs the full guarded path). With a non-overwrite
    existing output it falls through to the lock rejection rather than the
    converter — confirming it does NOT silently complete-as-noop."""
    source = tmp_path / "game.cue"
    output = tmp_path / "game.chd"
    source.write_bytes(b"source")
    output.write_bytes(b"previously-converted-and-verified")

    monkeypatch.setattr(job_manager_module.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(job_manager_module.settings, "data_mount_root", str(tmp_path))

    calls, fake_convert = _stub_convert()
    monkeypatch.setattr(
        job_manager_module.registry.for_mode("createcd")._service, "convert", fake_convert,
    )
    is_verified = AsyncMock(return_value=True)
    monkeypatch.setattr(job_manager_module.verification_store, "is_verified", is_verified)

    manager = JobManager(max_concurrent=1, max_job_history=5)
    job = await manager.create_job(
        str(source), ConversionMode.CREATECD, output_path=str(output), delete_on_verify=True,
    )
    await manager._process_job(job.id)

    assert job.status != JobStatus.COMPLETED
    assert calls == []
    # The fast path short-circuits on delete_on_verify before ever consulting
    # the verification store.
    is_verified.assert_not_called()
