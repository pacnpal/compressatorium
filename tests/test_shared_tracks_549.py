"""A track two sheets share is never deleted out from under one of them
(#549 review, Codex 4654246812).

Two listed ``.cue``/``.gdi`` sheets can name the same track. The listing then
folds it under neither (it stays its own row), deleting a batch refuses a
file that a sheet outside the batch still names, and a delete-on-verify plan
refuses to take a track that another sheet names.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.datastructures import Headers

from app.routes import files as files_routes


@pytest.fixture(name="vol")
def _vol(tmp_path: Path, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(settings, "data_mount_root", str(tmp_path))
    return tmp_path


def _write(path: Path, data: bytes = b"\0" * 64) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _two_sheets_one_track(folder: Path) -> tuple[Path, Path, Path, Path]:
    shared = _write(folder / "Shared.bin")
    own = _write(folder / "A (Track 2).bin")
    a = _write(folder / "A.cue", (
        b'FILE "Shared.bin" BINARY\n  TRACK 01 MODE2/2352\n'
        b'FILE "A (Track 2).bin" BINARY\n  TRACK 02 AUDIO\n'
    ))
    b = _write(folder / "B.cue", b'FILE "Shared.bin" BINARY\n  TRACK 01 MODE2/2352\n')
    return a, b, shared, own


@pytest.mark.asyncio
async def test_a_shared_track_is_folded_under_no_sheet(vol):
    a, _b, _shared, _own = _two_sheets_one_track(vol / "psx")

    listing = await files_routes.list_files(
        path=str(vol / "psx"), show_archives=True, summarize_archives=False,
    )
    marks = {e.name: e.companion_of for e in listing.entries}

    assert marks["Shared.bin"] is None
    assert marks["A (Track 2).bin"] == str(a)


def _confirmed():
    return SimpleNamespace(headers=Headers({"x-chd-action-confirm": "delete-file"}))


@pytest.mark.asyncio
async def test_a_batch_never_deletes_a_track_another_sheet_still_names(vol):
    a, b, shared, own = _two_sheets_one_track(vol / "psx")

    response = await files_routes.delete_files_batch(
        _confirmed(), files_routes.BulkDeleteRequest(paths=[str(a), str(own), str(shared)]),
    )

    by_path = {r["path"]: r for r in response["results"]}
    assert by_path[str(shared)]["success"] is False
    assert "B.cue" in by_path[str(shared)]["error"]
    assert shared.exists() and b.exists()
    assert not a.exists() and not own.exists()


@pytest.mark.asyncio
async def test_a_batch_deletes_a_shared_track_with_every_sheet_that_names_it(vol):
    a, b, shared, own = _two_sheets_one_track(vol / "psx")

    response = await files_routes.delete_files_batch(
        _confirmed(),
        files_routes.BulkDeleteRequest(paths=[str(a), str(b), str(own), str(shared)]),
    )

    assert response["failed"] == 0, response
    assert not any(p.exists() for p in (a, b, own, shared))


def test_a_delete_on_verify_plan_keeps_a_track_another_sheet_names(vol):
    from utils.delete_plan import build_delete_plan

    a, _b, _shared, _own = _two_sheets_one_track(vol / "psx")

    plan = build_delete_plan(str(a), confine_to_volumes=True)

    assert any("B.cue" in error for error in plan["errors"]), plan
