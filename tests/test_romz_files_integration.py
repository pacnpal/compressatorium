"""Regression tests for how romz interacts with the file listing.

The romz tool claims ``.7z``/``.zip`` as extract-mode inputs and ``.gb``/``.gbc``/
``.gba``/``.nds`` as compress sources. These tests pin two invariants:

1. A ``.7z``/``.zip`` produced by romz still lists as a browseable archive
   (``type == "archive"``) and is NOT marked directly convertible — the existing
   ``is_archive`` guard must win over romz's input claim, so archive browsing is
   undisturbed.
2. A loose handheld ROM is marked ``romz_convertible`` and badges its sibling
   archive output.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.routes import files as files_routes


@pytest.fixture(name="romz_env")
def _romz_env(tmp_path, monkeypatch):
    # Loose ROM source with no output yet.
    (tmp_path / "Solo.gba").write_bytes(b"rom")
    # ROM source with a finished .7z sibling (name preserves the ROM ext).
    (tmp_path / "Done.gb").write_bytes(b"rom")
    (tmp_path / "Done.gb.7z").write_bytes(b"7z")
    # A standalone archive that should remain a browseable archive.
    archive = tmp_path / "Pack.7z"
    archive.write_bytes(b"7z")
    # A real single-ROM zip: romz can verify/extract this one.
    with zipfile.ZipFile(tmp_path / "Bundle.zip", "w") as zf:
        zf.writestr("inner.gba", b"rom")
    # A multi-file zip that is NOT a romz output (a ROM plus an unrelated
    # sidecar). It must stay a browseable archive WITHOUT surfacing romz's
    # Verify/Info row-actions (issue #146).
    with zipfile.ZipFile(tmp_path / "Multi.zip", "w") as zf:
        zf.writestr("Game.gba", b"rom")
        zf.writestr("notes.txt", b"hello")
    # A real zip with no ROM at all: also not romz-verifiable.
    with zipfile.ZipFile(tmp_path / "Docs.zip", "w") as zf:
        zf.writestr("readme.txt", b"hello")

    monkeypatch.setattr(files_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(files_routes.settings, "data_mount_root", str(tmp_path))
    return {"root": str(tmp_path)}


@pytest.mark.asyncio
async def test_archives_stay_archives_after_romz_registers(romz_env):
    listing = await files_routes.list_files(path=romz_env["root"])
    by_name = {e.name: e for e in listing.entries}

    pack = by_name["Pack.7z"]
    assert pack.type == "archive"
    # Archives are never directly convertible, even though romz claims .7z.
    assert pack.romz_convertible is False
    assert pack.convertible_by == []

    bundle = by_name["Bundle.zip"]
    assert bundle.type == "archive"
    assert bundle.romz_convertible is False


@pytest.mark.asyncio
async def test_only_single_rom_archives_surface_romz_verify_info(romz_env):
    # Issue #146: the romz Verify/Info row-actions are gated on a per-archive
    # single-ROM-member check (the listing's `verifiable_by` flag), not on the
    # .7z/.zip extension alone.
    listing = await files_routes.list_files(path=romz_env["root"])
    by_name = {e.name: e for e in listing.entries}

    # Single-ROM archive: romz can verify/extract it, so it's offered.
    bundle = by_name["Bundle.zip"]
    assert bundle.type == "archive"
    assert bundle.verifiable_by == ["romz"]

    # Multi-file archive (ROM + sidecar): stays a browseable archive, but romz
    # Verify/Info must NOT be surfaced.
    multi = by_name["Multi.zip"]
    assert multi.type == "archive"
    assert multi.verifiable_by == []

    # Archive with no ROM at all: likewise not romz-verifiable.
    docs = by_name["Docs.zip"]
    assert docs.type == "archive"
    assert docs.verifiable_by == []

    # An unreadable/corrupt archive degrades to "not romz-ready" rather than
    # raising during the scan.
    pack = by_name["Pack.7z"]
    assert pack.type == "archive"
    assert pack.verifiable_by == []


@pytest.mark.asyncio
async def test_search_archive_rows_gate_romz_verify_info(romz_env):
    # The same gating applies to the recursive /files/search archive-container
    # rows (issue #146 also flags app/routes/files.py search).
    result = await files_routes.search_files(path=romz_env["root"])
    archives_by_name = {
        f["name"]: f for f in result["files"] if f.get("type") == "archive"
    }

    assert archives_by_name["Bundle.zip"]["verifiable_by"] == ["romz"]
    assert archives_by_name["Multi.zip"]["verifiable_by"] == []
    assert archives_by_name["Docs.zip"]["verifiable_by"] == []
    assert archives_by_name["Pack.7z"]["verifiable_by"] == []


@pytest.mark.asyncio
async def test_loose_rom_is_romz_convertible_and_badges_output(romz_env):
    listing = await files_routes.list_files(path=romz_env["root"])
    by_name = {e.name: e for e in listing.entries}
    root = romz_env["root"]

    solo = by_name["Solo.gba"]
    assert solo.type == "file"
    assert solo.romz_convertible is True
    assert "romz" in solo.convertible_by
    assert solo.has_romz is False

    done = by_name["Done.gb"]
    assert done.romz_convertible is True
    assert done.has_romz is True and done.romz_ready is True
    assert done.romz_path == str(Path(root) / "Done.gb.7z")
    assert any(o.tool_id == "romz" for o in done.outputs)
