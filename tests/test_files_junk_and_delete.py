"""File browser: junk filtering + delete (recursive opt-in for non-empty dirs)."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from app.routes import files as files_routes


@pytest.fixture(name="vol")
def _vol(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(files_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(files_routes.settings, "data_mount_root", str(tmp_path))
    return tmp_path


@pytest.mark.parametrize(
    "junk",
    [".DS_Store", "Thumbs.db", "thumbs.db", "._game.nsp", "desktop.ini", ".nfs0001"],
)
def test_is_junk_entry(junk):
    assert files_routes._is_junk_entry(junk) is True


@pytest.mark.parametrize("keep", ["game.nsp", "disc.cue", ".switch", "Game (USA).iso"])
def test_is_not_junk(keep):
    assert files_routes._is_junk_entry(keep) is False


@pytest.mark.asyncio
async def test_listing_hides_junk(vol: Path):
    (vol / "game.nsp").write_bytes(b"x")
    (vol / ".DS_Store").write_bytes(b"x")
    (vol / "Thumbs.db").write_bytes(b"x")
    (vol / "._game.nsp").write_bytes(b"x")
    (vol / "@eaDir").mkdir()
    (vol / "lost+found").mkdir()
    (vol / "#recycle").mkdir()

    listing = await files_routes.list_files(path=str(vol))
    names = {e.name for e in listing.entries}
    assert names == {"game.nsp"}


@pytest.mark.asyncio
async def test_delete_nonempty_dir_requires_recursive(vol: Path):
    d = vol / "folder"
    d.mkdir()
    (d / "a.txt").write_bytes(b"x")
    (d / "sub").mkdir()

    # recursive=False is what HTTP resolves when the param is absent; pass it
    # explicitly here since a direct call leaves Query(...) defaults unresolved.
    with pytest.raises(HTTPException) as exc:
        await files_routes.delete_file(path=str(d), recursive=False)
    assert exc.value.status_code == 409
    assert "not empty" in exc.value.detail.lower()
    assert d.exists()  # nothing deleted on the refusal

    result = await files_routes.delete_file(path=str(d), recursive=True)
    assert result["success"] is True
    assert not d.exists()


@pytest.mark.asyncio
async def test_delete_empty_dir_needs_no_recursive(vol: Path):
    d = vol / "empty"
    d.mkdir()
    result = await files_routes.delete_file(path=str(d), recursive=False)
    assert result["success"] is True
    assert not d.exists()


@pytest.mark.asyncio
async def test_delete_file(vol: Path):
    f = vol / "x.bin"
    f.write_bytes(b"x")
    result = await files_routes.delete_file(path=str(f), recursive=False)
    assert result["success"] is True
    assert not f.exists()
