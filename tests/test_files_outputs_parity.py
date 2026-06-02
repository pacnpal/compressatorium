"""Phase 7: registry-driven FileEntry outputs must stay in lock-step with the
legacy per-tool booleans, and the legacy JSON contract must not drift.

These tests are the safety net for the refactor that replaced the six
hand-written flag blocks in ``routes/files.py`` with a single registry loop.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.routes import files as files_routes
from app.services.lock_manager import lock_manager

# The exact legacy field surface that frontend (Phase 8) still reads. Phase 7
# only adds ``convertible_by``/``outputs`` on top of these.
LEGACY_FILEENTRY_KEYS = {
    "name", "path", "type", "size", "extension", "convertible", "has_chd",
    "has_rvz", "dolphin_ready", "dolphin_path", "chd_ready",
    "dolphin_convertible", "z3ds_convertible", "has_z3ds", "z3ds_ready",
    "z3ds_path", "nsz_convertible", "has_nsz", "nsz_ready", "nsz_path",
    "archive_items", "archive_has_chd", "archive_truncated",
    "media_type",
}
LEGACY_SEARCH_KEYS = {
    "name", "path", "size", "extension", "chd_path", "has_chd", "has_rvz",
    "dolphin_ready", "dolphin_path", "chd_ready", "convertible",
    "dolphin_convertible", "z3ds_convertible", "has_z3ds", "z3ds_ready",
    "z3ds_path", "nsz_convertible", "has_nsz", "nsz_ready", "nsz_path",
    "in_archive",
}
# Archive members carry the same registry-driven flags as on-disk search hits
# (issue #128) plus the archive-specific locator keys.
LEGACY_ARCHIVE_KEYS = LEGACY_SEARCH_KEYS | {
    "archive_path", "internal_path", "output_stem",
}
NEW_KEYS = {"convertible_by", "outputs"}


@pytest.fixture(name="parity_env")
def _parity_env(tmp_path: Path, monkeypatch):
    """A tree exercising every detection branch across the three tools."""
    # chdman-only source with no output.
    (tmp_path / "lonely.cue").write_bytes(b"cue")
    # chdman-only source with a finished output.
    (tmp_path / "done.cue").write_bytes(b"cue")
    (tmp_path / "done.chd").write_bytes(b"chd")
    # chdman-only source with a mid-conversion (locked, no file) output.
    (tmp_path / "prog.cue").write_bytes(b"cue")
    # dolphin-only source with a finished sibling output.
    (tmp_path / "disc.wbfs").write_bytes(b"wbfs")
    (tmp_path / "disc.rvz").write_bytes(b"rvz")
    # self-format dolphin input (.rvz is itself a dolphin output format).
    (tmp_path / "movie.rvz").write_bytes(b"rvz")
    # z3ds source with a finished output.
    (tmp_path / "rom.3ds").write_bytes(b"3ds")
    (tmp_path / "rom.z3ds").write_bytes(b"z3ds")

    # Archive whose member maps to an existing sibling .chd.
    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("inner.iso", b"iso-bytes")
    (tmp_path / "inner.chd").write_bytes(b"chd")

    monkeypatch.setattr(files_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(files_routes.settings, "data_mount_root", str(tmp_path))

    lock_path = str(tmp_path / "prog.chd")
    assert lock_manager.acquire_lock(lock_path) is True
    try:
        yield {"root": str(tmp_path)}
    finally:
        lock_manager.release_lock(lock_path)


def _assert_agreement(legacy: dict, outputs: list, convertible_by: list[str]) -> None:
    """The legacy booleans must be reconstructable from outputs/convertible_by."""
    by_tool = {o.tool_id: o for o in outputs}

    assert legacy["has_chd"] == ("chdman" in by_tool)
    assert legacy["chd_ready"] == (
        by_tool["chdman"].exists if "chdman" in by_tool else False
    )

    assert legacy["has_rvz"] == ("dolphin" in by_tool)
    assert legacy["dolphin_ready"] == (
        by_tool["dolphin"].exists if "dolphin" in by_tool else False
    )
    assert legacy["dolphin_path"] == (
        by_tool["dolphin"].path if "dolphin" in by_tool else None
    )

    assert legacy["has_z3ds"] == ("z3ds" in by_tool)
    assert legacy["z3ds_ready"] == (
        by_tool["z3ds"].exists if "z3ds" in by_tool else False
    )
    assert legacy["z3ds_path"] == (
        by_tool["z3ds"].path if "z3ds" in by_tool else None
    )

    assert legacy["convertible"] == ("chdman" in convertible_by)
    assert legacy["dolphin_convertible"] == ("dolphin" in convertible_by)
    assert legacy["z3ds_convertible"] == ("z3ds" in convertible_by)


@pytest.mark.asyncio
async def test_list_files_outputs_agree_with_legacy(parity_env):
    listing = await files_routes.list_files(path=parity_env["root"])
    by_name = {e.name: e for e in listing.entries}
    root = parity_env["root"]

    # Archives are a documented exception: has_chd is set from archive-member
    # scanning, not from registry outputs, so the agreement invariant applies
    # only to plain files.
    for entry in listing.entries:
        if entry.type != "file":
            continue
        _assert_agreement(entry.model_dump(), entry.outputs, entry.convertible_by)

    # No output present.
    lonely = by_name["lonely.cue"]
    assert lonely.convertible_by == ["chdman"]
    assert lonely.outputs == []
    assert lonely.has_chd is False and lonely.chd_ready is False

    # Finished chdman output.
    done = by_name["done.cue"]
    assert done.has_chd is True and done.chd_ready is True
    assert [o.tool_id for o in done.outputs] == ["chdman"]
    assert done.outputs[0].exists is True and done.outputs[0].ready is True

    # Mid-conversion chdman output (locked, file absent).
    prog = by_name["prog.cue"]
    assert prog.has_chd is True and prog.chd_ready is False
    assert prog.outputs[0].tool_id == "chdman"
    assert prog.outputs[0].exists is False and prog.outputs[0].ready is False
    assert prog.outputs[0].path == str(Path(root) / "prog.chd")

    # Finished dolphin output for a dolphin-only input.
    disc = by_name["disc.wbfs"]
    assert disc.has_rvz is True and disc.dolphin_ready is True
    assert disc.dolphin_path == str(Path(root) / "disc.rvz")
    assert disc.convertible is False  # .wbfs is not chdman-convertible

    # Self-format dolphin input detects itself.
    movie = by_name["movie.rvz"]
    assert movie.has_rvz is True and movie.dolphin_ready is True
    assert movie.dolphin_path == str(Path(root) / "movie.rvz")

    # Finished z3ds output.
    rom = by_name["rom.3ds"]
    assert rom.has_z3ds is True and rom.z3ds_ready is True
    assert rom.z3ds_path == str(Path(root) / "rom.z3ds")
    assert [o.tool_id for o in rom.outputs] == ["z3ds"]

    # Archive: CHD-only detection via the archive special-casing; the new
    # registry-driven fields stay empty (archives never emit tool outputs).
    bundle = by_name["bundle.zip"]
    assert bundle.type == "archive"
    assert bundle.has_chd is True  # inner.chd exists for the member stem
    assert bundle.has_rvz is False and bundle.z3ds_ready is False
    assert bundle.convertible_by == []
    assert bundle.outputs == []


@pytest.mark.asyncio
async def test_search_files_outputs_agree_with_legacy(parity_env):
    results = await files_routes.search_files(
        path=parity_env["root"], recursive=True, include_archives=True,
    )
    by_name = {Path(item["path"]).name: item for item in results["files"]}

    for item in results["files"]:
        _assert_agreement(item, item["outputs"], item["convertible_by"])

    assert by_name["lonely.cue"]["outputs"] == []
    assert by_name["lonely.cue"]["convertible_by"] == ["chdman"]
    assert by_name["done.cue"]["chd_ready"] is True
    assert by_name["prog.cue"]["has_chd"] is True
    assert by_name["prog.cue"]["chd_ready"] is False
    assert by_name["disc.wbfs"]["dolphin_path"].endswith("disc.rvz")
    assert by_name["movie.rvz"]["dolphin_ready"] is True
    assert by_name["rom.3ds"]["z3ds_ready"] is True


@pytest.mark.asyncio
async def test_list_files_json_keys_are_additive(parity_env):
    """Every legacy FileEntry key is preserved; only convertible_by/outputs added."""
    listing = await files_routes.list_files(path=parity_env["root"])
    for entry in listing.entries:
        keys = set(entry.model_dump().keys())
        assert LEGACY_FILEENTRY_KEYS <= keys
        assert keys - LEGACY_FILEENTRY_KEYS == NEW_KEYS


@pytest.mark.asyncio
async def test_search_files_json_keys_are_additive(parity_env):
    """Search file dicts keep every legacy key; archive dicts stay untouched."""
    results = await files_routes.search_files(
        path=parity_env["root"], recursive=True, include_archives=True,
    )
    for item in results["files"]:
        keys = set(item.keys())
        assert LEGACY_SEARCH_KEYS <= keys
        assert keys - LEGACY_SEARCH_KEYS == NEW_KEYS

    # Archive members are now registry-driven too (issue #128): they expose the
    # same per-tool flags as on-disk hits, plus archive locator keys, and their
    # legacy booleans must be reconstructable from outputs/convertible_by.
    for item in results["archives"]:
        keys = set(item.keys())
        assert LEGACY_ARCHIVE_KEYS <= keys
        assert keys - LEGACY_ARCHIVE_KEYS == NEW_KEYS
        assert item["in_archive"] is True
        _assert_agreement(item, item["outputs"], item["convertible_by"])

    # bundle.zip::inner.iso has a sibling inner.chd, so the member surfaces as
    # CHDMAN-convertible with a finished output — and as Dolphin-convertible
    # (.iso is accepted by both) even though no .rvz sibling exists.
    inner = next(i for i in results["archives"] if i["name"] == "inner.iso")
    assert inner["convertible"] is True
    assert inner["has_chd"] is True and inner["chd_ready"] is True
    assert "chdman" in inner["convertible_by"]
    assert inner["dolphin_convertible"] is True
    assert inner["has_rvz"] is False
