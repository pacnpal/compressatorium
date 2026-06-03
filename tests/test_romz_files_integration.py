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
    # A real zip the existing archive scanner can open.
    with zipfile.ZipFile(tmp_path / "Bundle.zip", "w") as zf:
        zf.writestr("inner.gba", b"rom")

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
