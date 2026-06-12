"""Tests for the makeps3iso folder->iso directory-input tool (issue #98 Phase 2).

Covers the end-to-end seam: detector->tool resolution, the file-listing /
search directory annotation, the service convert (mocked subprocess), and the
``plan_job`` directory branch (valid / invalid / trailing slash).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.models import ConversionJob, ConversionMode, InputKind, JobStatus
from app.routes import convert as convert_routes
from app.routes import files as files_routes
from app.services.makeps3iso import ConversionCancelled, makeps3iso_service
from app.services.tools import registry

from .ps3_helpers import make_ps3_folder as _make_ps3_folder


# --------------------------------------------------------------------------- #
# Detector -> tool resolution
# --------------------------------------------------------------------------- #


def test_tools_for_directory_resolves_makeps3iso(tmp_path):
    folder = _make_ps3_folder(tmp_path / "MyGame")
    assert [t.id for t in registry.tools_for_directory(str(folder))] == ["makeps3iso"]
    plain = tmp_path / "plain"
    plain.mkdir()
    assert registry.tools_for_directory(str(plain)) == []


def test_folder_to_iso_spec_is_directory_kind():
    spec = registry.spec("folder_to_iso")
    assert spec.tool_id == "makeps3iso"
    assert InputKind.DIRECTORY in spec.input_kinds
    assert spec.output_ext == ".iso"
    assert spec.supports_delete_on_verify is False


# --------------------------------------------------------------------------- #
# File-listing / search annotation
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_list_files_annotates_convertible_directory(tmp_path, monkeypatch):
    _make_ps3_folder(tmp_path / "MyGame")
    (tmp_path / "plain").mkdir()
    monkeypatch.setattr(files_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(files_routes.settings, "data_mount_root", str(tmp_path))

    listing = await files_routes.list_files(path=str(tmp_path))
    by_name = {e.name: e for e in listing.entries}

    game = by_name["MyGame"]
    assert game.type == "directory"
    assert game.convertible_by == ["makeps3iso"]
    # No sibling .iso yet, so no detected output.
    assert game.outputs == []

    plain = by_name["plain"]
    assert plain.type == "directory"
    assert plain.convertible_by == []


@pytest.mark.asyncio
async def test_list_files_directory_output_badge(tmp_path, monkeypatch):
    _make_ps3_folder(tmp_path / "MyGame")
    (tmp_path / "MyGame.iso").write_bytes(b"iso")  # sibling output already present
    monkeypatch.setattr(files_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(files_routes.settings, "data_mount_root", str(tmp_path))

    listing = await files_routes.list_files(path=str(tmp_path))
    game = next(e for e in listing.entries if e.name == "MyGame")
    assert [o.tool_id for o in game.outputs] == ["makeps3iso"]
    assert game.outputs[0].exists is True
    assert game.outputs[0].path == str(tmp_path / "MyGame.iso")


@pytest.mark.asyncio
async def test_search_emits_convertible_directory(tmp_path, monkeypatch):
    # A PS3 folder nested in a subtree must surface as a selectable directory
    # row in recursive search (and not be recursed past as a job unit).
    sub = tmp_path / "sub"
    sub.mkdir()
    _make_ps3_folder(sub / "MyGame")
    monkeypatch.setattr(files_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(files_routes.settings, "data_mount_root", str(tmp_path))

    result = await files_routes.search_files(path=str(tmp_path))
    dir_rows = [f for f in result["files"] if f.get("type") == "directory"]
    assert len(dir_rows) == 1
    row = dir_rows[0]
    assert row["name"] == "MyGame"
    assert row["convertible_by"] == ["makeps3iso"]
    # The inner PS3_GAME files were NOT emitted as separate convertible hits.
    assert all("PARAM.SFO" not in f["name"] for f in result["files"])


# --------------------------------------------------------------------------- #
# plan_job directory branch
# --------------------------------------------------------------------------- #


def _confine_to_volume(monkeypatch, root):
    monkeypatch.setattr(convert_routes.settings, "chd_volumes", str(root))
    monkeypatch.setattr(convert_routes.settings, "data_mount_root", str(root))


@pytest.mark.asyncio
async def test_plan_job_accepts_valid_ps3_dir(tmp_path, monkeypatch):
    _confine_to_volume(monkeypatch, tmp_path)
    folder = _make_ps3_folder(tmp_path / "MyGame")
    plan = await convert_routes.plan_job(
        str(folder),
        spec=registry.spec("folder_to_iso"),
        mode="folder_to_iso",
        output_dir=None,
        duplicate_action=convert_routes.DuplicateAction.SKIP,
        delete_on_verify=False,
    )
    assert plan.output_path == str(tmp_path / "MyGame.iso")
    assert plan.display_filename == "MyGame"
    assert plan.allow_overwrite is False


@pytest.mark.asyncio
async def test_plan_job_rejects_output_outside_volumes(tmp_path, monkeypatch):
    # The PS3 folder is itself the volume root, so the default sibling output
    # ("<root>.iso") lands outside all configured volumes -> rejected.
    vol = tmp_path / "vol"
    _make_ps3_folder(vol)
    _confine_to_volume(monkeypatch, vol)
    with pytest.raises(convert_routes.SkipFile) as exc:
        await convert_routes.plan_job(
            str(vol),
            spec=registry.spec("folder_to_iso"),
            mode="folder_to_iso",
            output_dir=None,
            duplicate_action=convert_routes.DuplicateAction.SKIP,
            delete_on_verify=False,
        )
    assert exc.value.reason is convert_routes.SkipReason.PS3_OUTPUT_OUTSIDE_VOLUMES


@pytest.mark.asyncio
async def test_plan_job_rejects_non_ps3_dir(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(convert_routes.SkipFile) as exc:
        await convert_routes.plan_job(
            str(plain),
            spec=registry.spec("folder_to_iso"),
            mode="folder_to_iso",
            output_dir=None,
            duplicate_action=convert_routes.DuplicateAction.SKIP,
            delete_on_verify=False,
        )
    assert exc.value.reason is convert_routes.SkipReason.PS3_FOLDER_INVALID


@pytest.mark.asyncio
async def test_plan_job_rejects_output_inside_source(tmp_path):
    # An output_dir inside the source folder would make makeps3iso pack its own
    # in-progress ISO and corrupt the image — must be rejected.
    folder = _make_ps3_folder(tmp_path / "MyGame")
    with pytest.raises(convert_routes.SkipFile) as exc:
        await convert_routes.plan_job(
            str(folder),
            spec=registry.spec("folder_to_iso"),
            mode="folder_to_iso",
            output_dir=str(folder / "PS3_GAME"),
            duplicate_action=convert_routes.DuplicateAction.SKIP,
            delete_on_verify=False,
        )
    assert exc.value.reason is convert_routes.SkipReason.PS3_OUTPUT_INSIDE_SOURCE


@pytest.mark.asyncio
async def test_plan_job_handles_trailing_slash(tmp_path, monkeypatch):
    _confine_to_volume(monkeypatch, tmp_path)
    _make_ps3_folder(tmp_path / "MyGame")
    plan = await convert_routes.plan_job(
        str(tmp_path / "MyGame") + "/",  # trailing slash
        spec=registry.spec("folder_to_iso"),
        mode="folder_to_iso",
        output_dir=None,
        duplicate_action=convert_routes.DuplicateAction.SKIP,
        delete_on_verify=False,
    )
    # Normalized basename, not "" -> the output is "<folder>.iso", never ".iso".
    assert plan.output_path == str(tmp_path / "MyGame.iso")
    assert plan.display_filename == "MyGame"


# --------------------------------------------------------------------------- #
# Service convert (mocked subprocess)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_service_convert_argv_and_progress(tmp_path, monkeypatch):
    folder = str(_make_ps3_folder(tmp_path / "MyGame"))
    out_iso = str(tmp_path / "MyGame.iso")
    captured: dict = {}

    async def fake_run(cmd, *, output_path, complete_message, **_kwargs):
        captured["cmd"] = cmd
        Path(output_path).write_bytes(b"not a real iso")  # simulated build
        yield {"progress": 1, "message": "Starting"}
        yield {"progress": 50, "message": "Writing... 50%"}
        yield {"progress": 100, "message": complete_message}

    monkeypatch.setattr(makeps3iso_service._runner, "run", fake_run)

    updates = [
        u
        async for u in makeps3iso_service.convert(folder, out_iso, "folder_to_iso")
    ]

    # argv is exactly `makeps3iso <folder> <out.iso>` (ignoring any ionice wrap).
    assert captured["cmd"][-3:] == [
        makeps3iso_service.makeps3iso_path, folder, out_iso,
    ]
    # Progress drains to 100, and the final update is the readback message the
    # service appends (replacing the runner's bare 100%).
    assert updates[-1]["progress"] == 100
    assert "ISO build complete" in updates[-1]["message"]
    assert Path(out_iso).exists()


@pytest.mark.asyncio
async def test_service_convert_cancel_cleans_partial(tmp_path, monkeypatch):
    folder = str(_make_ps3_folder(tmp_path / "MyGame"))
    out_iso = str(tmp_path / "MyGame.iso")

    async def fake_run(cmd, *, output_path, **_kwargs):
        Path(output_path).write_bytes(b"partial")  # half-written ISO
        yield {"progress": 10, "message": "Writing..."}
        raise ConversionCancelled("cancelled")

    monkeypatch.setattr(makeps3iso_service._runner, "run", fake_run)

    async def _drain():
        return [
            u
            async for u in makeps3iso_service.convert(folder, out_iso, "folder_to_iso")
        ]

    with pytest.raises(ConversionCancelled):
        await _drain()
    # The partial ISO is removed so a retry isn't blocked by a truncated image.
    assert not Path(out_iso).exists()


@pytest.mark.asyncio
async def test_service_convert_cleans_partial_on_task_cancellation(tmp_path, monkeypatch):
    # Task cancellation / generator close raise BaseException-derived
    # CancelledError / GeneratorExit, which an `except Exception` would miss —
    # the partial ISO must still be cleaned up.
    folder = str(_make_ps3_folder(tmp_path / "MyGame"))
    out_iso = str(tmp_path / "MyGame.iso")

    async def fake_run(cmd, *, output_path, **_kwargs):
        Path(output_path).write_bytes(b"partial")
        yield {"progress": 10, "message": "Writing..."}
        raise asyncio.CancelledError

    monkeypatch.setattr(makeps3iso_service._runner, "run", fake_run)

    async def _drain():
        return [
            u
            async for u in makeps3iso_service.convert(folder, out_iso, "folder_to_iso")
        ]

    with pytest.raises(asyncio.CancelledError):
        await _drain()
    assert not Path(out_iso).exists()


# --------------------------------------------------------------------------- #
# Lock-manager subtree protection
# --------------------------------------------------------------------------- #


def test_directory_job_protects_subtree(tmp_path):
    from app.services.job_manager import job_manager

    folder = _make_ps3_folder(tmp_path / "MyGame")
    job = ConversionJob(
        id="ps3test1",
        file_path=str(folder),
        filename="MyGame",
        mode=ConversionMode.FOLDER_TO_ISO,
        status=JobStatus.PROCESSING,
        created_at=datetime.now(timezone.utc),
        output_path=str(tmp_path / "MyGame.iso"),
        input_kind=InputKind.DIRECTORY,
    )
    job_manager.jobs[job.id] = job
    try:
        # A path INSIDE the active folder is treated as in use (its whole
        # subtree is being packed), even though it hashes to a different key.
        inside = str(folder / "PS3_GAME" / "PARAM.SFO")
        assert job_manager.find_active_job_for_path(inside) is job
        assert job_manager._is_path_in_use_by_other_job("other", inside) is True
        # A sibling outside the folder is unaffected.
        outside = str(tmp_path / "Unrelated.iso")
        assert job_manager.find_active_job_for_path(outside) is None
    finally:
        job_manager.jobs.pop(job.id, None)


def test_dir_lock_serializes_subtree(tmp_path):
    # A directory subtree lock makes any path inside the folder contend on the
    # lock exactly like an output collision, so a concurrent per-file job can't
    # write into the tree while makeps3iso packs it.
    from app.services.lock_manager import lock_manager

    folder = str(_make_ps3_folder(tmp_path / "MyGame"))
    inside = str(tmp_path / "MyGame" / "PS3_GAME" / "extra.chd")
    sibling = str(tmp_path / "MyGame.iso")

    assert lock_manager.acquire_dir_lock(folder) is True
    try:
        # The whole subtree reads as locked; acquiring a per-file lock inside
        # the folder is refused.
        _exists, is_locked = lock_manager.check_file_status(inside)
        assert is_locked is True
        assert lock_manager.acquire_lock(inside) is False
        # A sibling output (the job's own .iso) is outside the subtree, so it
        # locks normally.
        assert lock_manager.acquire_lock(sibling) is True
        lock_manager.release_lock(sibling)
        # A second packing of the same folder is refused while it's locked.
        assert lock_manager.acquire_dir_lock(folder) is False
    finally:
        lock_manager.release_dir_lock(folder)

    # Once released, the subtree is free again.
    assert lock_manager.acquire_lock(inside) is True
    lock_manager.release_lock(inside)


@pytest.mark.asyncio
async def test_blocked_job_requeues_instead_of_failing(tmp_path):
    # A job whose output is inside a folder being packed waits in the queue and
    # is re-dispatched, rather than being failed. job_manager resolves its
    # lock/concurrency managers via absolute ``services.*`` imports, so import
    # those from the same module graph (the ``app.services.*`` aliases are
    # distinct singletons under PYTHONPATH=app).
    from app.services.job_manager import job_manager
    from services.concurrency_manager import concurrency_manager
    from services.lock_manager import lock_manager

    folder = str(_make_ps3_folder(tmp_path / "MyGame"))
    out_inside = str(tmp_path / "MyGame" / "PS3_GAME" / "extra.chd")
    job = ConversionJob(
        id="ps3wait1",
        file_path=str(tmp_path / "src.cue"),
        filename="src.cue",
        mode=ConversionMode.CREATECD,
        status=JobStatus.QUEUED,
        created_at=datetime.now(timezone.utc),
        output_path=out_inside,
        input_kind=InputKind.FILE,
    )
    job_manager.jobs[job.id] = job
    assert lock_manager.acquire_dir_lock(folder) is True
    try:
        # The output lives inside the packed folder -> a transient block: wait.
        assert job_manager._blocked_by_dir_lock(job) is True
        # Re-dispatch (no delay) re-queues the job; it is never failed.
        job_manager._schedule_dir_lock_requeue(job.id, delay=0)
        await asyncio.sleep(0.05)
        assert job.status == JobStatus.QUEUED
        drained, found = [], False
        while not job_manager._queue.empty():
            item = job_manager._queue.get_nowait()
            if item[1] == job.id:
                found = True
            else:
                drained.append(item)
        for item in drained:
            job_manager._queue.put_nowait(item)
        assert found
    finally:
        lock_manager.release_dir_lock(folder)
        concurrency_manager.release_ticket(job.id)
        job_manager.jobs.pop(job.id, None)
        job_manager._cancelled.discard(job.id)
    # With the folder lock gone, the job is no longer blocked.
    assert job_manager._blocked_by_dir_lock(job) is False
