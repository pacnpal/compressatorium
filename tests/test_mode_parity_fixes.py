"""Regression tests for cross-mode parity fixes."""

import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.models import (
    BatchJobCreateRequest,
    ConversionMode,
    DeletePlanRequest,
    JobCreateRequest,
    JobStatus,
)
from app.routes import convert as convert_routes
from app.routes import files as files_routes
from app.services import job_manager as job_manager_module
from app.services.job_manager import JobManager, QueueBackpressureError
from app.utils.delete_plan import build_delete_snapshot


@pytest.fixture(name="files_env")
def _files_env(tmp_path: Path, monkeypatch):
    """Create file fixtures used by files route tests."""
    game_iso = tmp_path / "game.iso"
    game_rvz = tmp_path / "game.rvz"
    wia_iso = tmp_path / "wia.iso"
    wia_output = tmp_path / "wia.wia"
    other_iso = tmp_path / "other.iso"

    game_iso.write_bytes(b"iso")
    game_rvz.write_bytes(b"rvz")
    wia_iso.write_bytes(b"iso")
    wia_output.write_bytes(b"wia")
    other_iso.write_bytes(b"iso")

    monkeypatch.setattr(files_routes.settings, "chd_volumes", str(tmp_path))

    return {
        "root": str(tmp_path),
        "game_iso": str(game_iso),
        "game_rvz": str(game_rvz),
        "wia_iso": str(wia_iso),
        "wia_output": str(wia_output),
        "other_iso": str(other_iso),
    }


def test_job_manager_default_max_concurrent_is_one():
    """JobManager should default to serial processing unless explicitly overridden."""
    manager = JobManager()
    assert manager.max_concurrent == 1


@pytest.mark.asyncio
async def test_list_files_populates_has_rvz(files_env):
    """Directory listing should surface Dolphin output presence for inputs."""
    listing = await files_routes.list_files(path=files_env["root"])
    by_name = {entry.name: entry for entry in listing.entries}

    assert by_name["game.iso"].dolphin_convertible is True
    assert by_name["game.iso"].has_rvz is True
    assert by_name["game.iso"].dolphin_ready is True
    assert by_name["game.iso"].dolphin_path == files_env["game_rvz"]
    assert by_name["wia.iso"].has_rvz is True
    assert by_name["wia.iso"].dolphin_ready is True
    assert by_name["wia.iso"].dolphin_path == files_env["wia_output"]
    assert by_name["other.iso"].dolphin_convertible is True
    assert by_name["other.iso"].has_rvz is False
    assert by_name["other.iso"].dolphin_ready is False
    assert by_name["game.rvz"].has_rvz is True
    assert by_name["game.rvz"].dolphin_ready is True


@pytest.mark.asyncio
async def test_search_files_populates_has_rvz(files_env):
    """Recursive search results should include Dolphin-product state consistently."""
    results = await files_routes.search_files(
        path=files_env["root"], recursive=True, include_archives=False
    )
    by_name = {Path(item["path"]).name: item for item in results["files"]}

    assert by_name["game.iso"]["dolphin_convertible"] is True
    assert by_name["game.iso"]["has_rvz"] is True
    assert by_name["game.iso"]["dolphin_ready"] is True
    assert by_name["game.iso"]["dolphin_path"] == files_env["game_rvz"]
    assert by_name["wia.iso"]["has_rvz"] is True
    assert by_name["wia.iso"]["dolphin_ready"] is True
    assert by_name["wia.iso"]["dolphin_path"] == files_env["wia_output"]
    assert by_name["other.iso"]["dolphin_convertible"] is True
    assert by_name["other.iso"]["has_rvz"] is False
    assert by_name["other.iso"]["dolphin_ready"] is False


@pytest.mark.asyncio
async def test_delete_on_verify_error_messages_include_dolphin_and_3ds():
    """Delete-on-verify validation message should reflect all supported tool modes."""
    request = DeletePlanRequest(
        file_paths=["/tmp/any.chd"],
        mode=ConversionMode.EXTRACTCD,
    )
    with pytest.raises(HTTPException) as exc_info:
        await convert_routes.delete_plan(request)
    assert exc_info.value.status_code == 400
    assert "create/copy/Dolphin/3DS" in str(exc_info.value.detail)

    create_request = JobCreateRequest(
        file_path="/tmp/any.chd",
        mode=ConversionMode.EXTRACTCD,
        delete_on_verify=True,
    )
    with pytest.raises(HTTPException) as create_exc:
        await convert_routes.create_job(create_request)
    assert create_exc.value.status_code == 400
    assert "create/copy/Dolphin/3DS" in str(create_exc.value.detail)


@pytest.mark.asyncio
async def test_create_job_returns_429_when_queue_is_full(tmp_path: Path, monkeypatch):
    """Single-job submissions should apply queue backpressure with HTTP 429."""
    source_path = tmp_path / "disc.iso"
    source_path.write_bytes(b"iso")

    monkeypatch.setattr(convert_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "max_queue_depth", 1)
    monkeypatch.setattr(convert_routes.job_manager, "get_queue_depth", lambda: 1)

    create_job_mock = AsyncMock()
    monkeypatch.setattr(convert_routes.job_manager, "create_job", create_job_mock)

    request = JobCreateRequest(
        file_path=str(source_path),
        mode=ConversionMode.CREATECD,
    )

    with pytest.raises(HTTPException) as exc_info:
        await convert_routes.create_job(request)

    assert exc_info.value.status_code == 429
    create_job_mock.assert_not_called()


@pytest.mark.asyncio
async def test_batch_create_returns_429_when_queue_capacity_would_be_exceeded(
    tmp_path: Path, monkeypatch,
):
    """Batch submissions should fail before partial enqueue when over capacity."""
    first = tmp_path / "a.iso"
    second = tmp_path / "b.iso"
    first.write_bytes(b"a")
    second.write_bytes(b"b")

    monkeypatch.setattr(convert_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "max_queue_depth", 2)
    monkeypatch.setattr(convert_routes.job_manager, "get_queue_depth", lambda: 1)

    create_job_mock = AsyncMock()
    monkeypatch.setattr(convert_routes.job_manager, "create_job", create_job_mock)

    request = BatchJobCreateRequest(
        file_paths=[str(first), str(second)],
        mode=ConversionMode.CREATECD,
    )

    with pytest.raises(HTTPException) as exc_info:
        await convert_routes.create_batch_jobs(request)

    assert exc_info.value.status_code == 429
    create_job_mock.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_all_jobs_endpoint_delegates_to_manager(monkeypatch):
    """Cancel-all route should return JobManager summary payload."""
    payload = {
        "requested": 2,
        "queued": 1,
        "processing": 1,
        "job_ids": ["a1b2c3d4", "e5f6g7h8"],
    }
    cancel_all_mock = AsyncMock(return_value=payload)
    monkeypatch.setattr(convert_routes.job_manager, "cancel_all_jobs", cancel_all_mock)

    result = await convert_routes.cancel_all_jobs()

    assert result == payload
    cancel_all_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_all_jobs_marks_queued_jobs_cancelled(tmp_path: Path):
    """JobManager cancel-all should cancel all queued jobs in one call."""
    first = tmp_path / "a.iso"
    second = tmp_path / "b.iso"
    first.write_bytes(b"a")
    second.write_bytes(b"b")

    manager = JobManager(max_concurrent=1, max_job_history=10)
    first_job = await manager.create_job(
        str(first), ConversionMode.CREATECD, output_path=str(tmp_path / "a.chd"),
    )
    second_job = await manager.create_job(
        str(second), ConversionMode.CREATECD, output_path=str(tmp_path / "b.chd"),
    )

    result = await manager.cancel_all_jobs()

    assert result["queued"] == 2
    assert result["processing"] == 0
    assert result["requested"] == 2
    assert set(result["job_ids"]) == {first_job.id, second_job.id}
    assert manager.get_job(first_job.id).status == JobStatus.CANCELLED
    assert manager.get_job(second_job.id).status == JobStatus.CANCELLED


@pytest.mark.asyncio
async def test_create_jobs_atomic_rejects_batch_without_partial_enqueue(
    tmp_path: Path, monkeypatch,
):
    """Atomic job creation should reject over-capacity batches before queuing any new job."""
    first = tmp_path / "a.iso"
    second = tmp_path / "b.iso"
    third = tmp_path / "c.iso"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    third.write_bytes(b"c")

    monkeypatch.setattr(job_manager_module.settings, "max_queue_depth", 2)
    manager = JobManager(max_concurrent=1, max_job_history=5)

    # Seed one queued job so adding two more would exceed max_queue_depth=2.
    seeded_job = await manager.create_job(
        str(first),
        ConversionMode.CREATECD,
        output_path=str(tmp_path / "a.chd"),
    )
    try:
        initial_jobs = manager.get_all_jobs()
        assert len(initial_jobs) == 1

        with pytest.raises(QueueBackpressureError):
            await manager.create_jobs_atomic(
                [
                    {
                        "file_path": str(second),
                        "output_path": str(tmp_path / "b.chd"),
                    },
                    {
                        "file_path": str(third),
                        "output_path": str(tmp_path / "c.chd"),
                    },
                ],
                ConversionMode.CREATECD,
            )

        final_jobs = manager.get_all_jobs()
        assert len(final_jobs) == 1
        assert final_jobs[0].file_path == str(first)
    finally:
        # Avoid leaking a FIFO ticket in the shared global concurrency manager.
        job_manager_module.concurrency_manager.release(seeded_job.id)


@pytest.mark.asyncio
async def test_z3ds_delete_on_verify_marks_output_verified(tmp_path: Path, monkeypatch):
    """3DS delete-on-verify should persist verified state like CHD/Dolphin modes."""
    source_path = tmp_path / "game.3ds"
    output_path = tmp_path / "game.z3ds"
    source_path.write_bytes(b"source")

    monkeypatch.setattr(job_manager_module.settings, "chd_volumes", str(tmp_path))

    async def fake_convert(
        input_path: str,
        destination_path: str,
        mode: str = "z3ds_compress",
        compression: str | None = None,
        cancel_event=None,
    ):
        # Simulate conversion writing an output file.
        Path(destination_path).write_bytes(b"converted")
        yield {"progress": 75, "message": "Compressing..."}
        yield {"progress": 100, "message": "Done"}

    async def fake_verify(path: str):
        return {"valid": True, "message": "File verified successfully"}

    mark_verified = AsyncMock()
    clear_verified = AsyncMock()
    clear_metadata = AsyncMock()

    monkeypatch.setattr(job_manager_module.z3ds_compress_service, "convert", fake_convert)
    monkeypatch.setattr(job_manager_module.z3ds_compress_service, "verify", fake_verify)
    monkeypatch.setattr(job_manager_module.verification_store, "mark_verified", mark_verified)
    monkeypatch.setattr(job_manager_module.verification_store, "clear", clear_verified)
    monkeypatch.setattr(job_manager_module.chd_metadata_store, "clear", clear_metadata)
    monkeypatch.setattr(
        job_manager_module,
        "build_delete_plan",
        lambda path: {
            "delete_paths": [os.path.realpath(str(source_path))],
            "missing_paths": [],
            "unsafe_paths": [],
            "errors": [],
        },
    )

    manager = JobManager(max_concurrent=1, max_job_history=5)
    snapshot = build_delete_snapshot(str(source_path))
    job = await manager.create_job(
        str(source_path),
        ConversionMode.Z3DS_COMPRESS,
        output_path=str(output_path),
        delete_on_verify=True,
        delete_snapshot=snapshot,
    )

    await manager._process_job(job.id)

    assert job.status == JobStatus.COMPLETED
    mark_verified.assert_called_once_with(
        str(output_path),
        source_path=str(source_path),
    )
    assert not source_path.exists()
    assert output_path.exists()


@pytest.mark.asyncio
async def test_delete_on_verify_rejects_inode_device_fingerprint_mismatch(
    tmp_path: Path, monkeypatch,
):
    """Delete-on-verify must refuse deleting a path whose fingerprint changed."""
    source_path = tmp_path / "game.3ds"
    output_path = tmp_path / "game.z3ds"
    source_path.write_bytes(b"source")
    snapshot = build_delete_snapshot(str(source_path))

    source_stat = source_path.stat()
    replacement = tmp_path / "replacement.3ds"
    replacement.write_bytes(b"source")
    os.utime(
        replacement,
        ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns),
    )
    os.replace(replacement, source_path)

    monkeypatch.setattr(job_manager_module.settings, "chd_volumes", str(tmp_path))

    async def fake_convert(
        input_path: str,
        destination_path: str,
        mode: str = "z3ds_compress",
        compression: str | None = None,
        cancel_event=None,
    ):
        Path(destination_path).write_bytes(b"converted")
        yield {"progress": 100, "message": "Done"}

    async def fake_verify(path: str):
        return {"valid": True, "message": "File verified successfully"}

    monkeypatch.setattr(job_manager_module.z3ds_compress_service, "convert", fake_convert)
    monkeypatch.setattr(job_manager_module.z3ds_compress_service, "verify", fake_verify)
    monkeypatch.setattr(job_manager_module.verification_store, "mark_verified", AsyncMock())
    monkeypatch.setattr(job_manager_module.verification_store, "clear", AsyncMock())
    monkeypatch.setattr(job_manager_module.chd_metadata_store, "clear", AsyncMock())
    monkeypatch.setattr(
        job_manager_module,
        "build_delete_plan",
        lambda path: {
            "delete_paths": [os.path.realpath(str(source_path))],
            "missing_paths": [],
            "unsafe_paths": [],
            "errors": [],
        },
    )

    manager = JobManager(max_concurrent=1, max_job_history=5)
    job = await manager.create_job(
        str(source_path),
        ConversionMode.Z3DS_COMPRESS,
        output_path=str(output_path),
        delete_on_verify=True,
        delete_snapshot=snapshot,
    )

    await manager._process_job(job.id)

    assert job.status == JobStatus.FAILED
    assert "fingerprint mismatch" in (job.error_message or "").lower()
    assert source_path.exists()
