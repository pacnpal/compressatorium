"""Tests for the makeps3iso folder->iso directory-input tool (issue #98 Phase 2).

Covers the end-to-end seam: detector->tool resolution, the file-listing /
search directory annotation, the service convert (mocked subprocess), and the
``plan_job`` directory branch (valid / invalid / trailing slash).
"""
from __future__ import annotations

import asyncio
import struct
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.models import ConversionJob, ConversionMode, InputKind, JobStatus
from app.routes import convert as convert_routes
from app.routes import files as files_routes
from app.services.makeps3iso import ConversionCancelled, makeps3iso_service
from app.services.tools import registry


def _make_sfo(pairs: list[tuple[str, str]]) -> bytes:
    keys_blob = b""
    key_offsets = []
    for key, _ in pairs:
        key_offsets.append(len(keys_blob))
        keys_blob += key.encode("ascii") + b"\x00"
    data_blob = b""
    data_meta = []
    for _, value in pairs:
        encoded = value.encode("utf-8") + b"\x00"
        data_meta.append((len(data_blob), len(encoded)))
        data_blob += encoded
    num = len(pairs)
    index_size = num * 16
    key_table_off = 20 + index_size
    data_table_off = key_table_off + len(keys_blob)
    header = struct.pack("<4sHH", b"\x00PSF", 1, 1)
    header += struct.pack("<III", key_table_off, data_table_off, num)
    index = b""
    for key_off, (data_off, data_len) in zip(key_offsets, data_meta):
        index += struct.pack("<HHIII", key_off, 0x0204, data_len, data_len, data_off)
    return header + index + keys_blob + data_blob


def _make_ps3_folder(root: Path) -> Path:
    """A minimal valid PS3 disc/JB folder (PS3_GAME/ root + PS3_DISC.SFB)."""
    game = root / "PS3_GAME"
    game.mkdir(parents=True)
    (game / "PARAM.SFO").write_bytes(
        _make_sfo([("TITLE_ID", "BLES01807"), ("TITLE", "Some Game")])
    )
    (root / "PS3_DISC.SFB").write_bytes(b"\x00")
    return root


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


@pytest.mark.asyncio
async def test_plan_job_accepts_valid_ps3_dir(tmp_path):
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
async def test_plan_job_handles_trailing_slash(tmp_path):
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
