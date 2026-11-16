"""Real chdman: the disc ID of a new CHD, without ``addmeta`` (#352).

Opt-in: runs the actual ``chdman``. It SKIPS (never fails) when the binary is
missing, so CI and normal ``pytest`` runs stay green. Point ``CHDMAN`` at a
binary, or have ``chdman`` (or the configured ``CHDMAN_PATH``) on the system.

What it pins down against the real tool:
- chdman refuses ``addmeta`` on a compressed CHD ("File not writeable"), the
  reason the app no longer tries to tag one;
- ``ChdmanTool.post_convert`` records the Source's disc ID for a real CHD in
  the metadata store and runs no chdman metadata command;
- ``extract_from_chd`` reads the disc ID back from the real CHD's sectors.
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest
from services.tools import registry

from config import settings

from .test_disc_id import _make_iso

SYSTEM_CNF = b"BOOT2 = cdrom0:\\SLUS_203.12;1\r\nVER = 1.00\r\n"


def _binary() -> str | None:
    for candidate in (os.environ.get("CHDMAN"), settings.chdman_path, "chdman"):
        if candidate and (os.path.isfile(candidate) or shutil.which(candidate)):
            return shutil.which(candidate) or candidate
    return None


@pytest.fixture(name="chdman_bin")
def _chdman_bin(monkeypatch):
    binary = _binary()
    if binary is None:
        pytest.skip("no chdman binary (set CHDMAN to run)")
    tool = registry.get("chdman")
    monkeypatch.setattr(tool._service, "chdman_path", binary)
    monkeypatch.setattr(tool, "binary_path", binary, raising=False)
    return binary


@pytest.fixture(name="db")
def _db(db_path, reset_db_engine):
    from services import db as _db

    _db.init_engine(db_path)
    yield
    reset_db_engine()


async def _real_dvd_chd(tmp_path):
    iso = tmp_path / "Game.iso"
    iso.write_bytes(_make_iso({"SYSTEM.CNF": SYSTEM_CNF}))
    chd = tmp_path / "Game.chd"
    tool = registry.get("chdman")
    async for _ in tool.convert(str(iso), str(chd), "createdvd", compression="zlib"):
        pass
    assert chd.is_file()
    return iso, chd


@pytest.mark.asyncio
async def test_real_chdman_refuses_addmeta_on_a_compressed_chd(tmp_path, chdman_bin):
    _iso, chd = await _real_dvd_chd(tmp_path)

    result = subprocess.run(
        [chdman_bin, "addmeta", "-i", str(chd), "-t", "GAME", "-vt", "SLUS-20312"],
        capture_output=True, text=True, check=False,
    )

    assert result.returncode != 0
    assert "not writeable" in (result.stdout + result.stderr).lower()


@pytest.mark.asyncio
async def test_real_chd_gets_its_disc_id_without_a_tag(
    tmp_path, chdman_bin, db, monkeypatch,
):
    from services.chd_metadata_store import chd_metadata_store
    from services.disc_id import extract_from_chd

    iso, chd = await _real_dvd_chd(tmp_path)
    tool = registry.get("chdman")
    runner = tool._service.runner
    real_capture = runner.run_capture
    commands: list[list[str]] = []

    async def recording_capture(cmd, **kwargs):
        commands.append(list(cmd))
        return await real_capture(cmd, **kwargs)

    monkeypatch.setattr(runner, "run_capture", recording_capture)

    await tool.post_convert(str(iso), str(chd), "createdvd")

    assert await chd_metadata_store.get_disc_id_info(str(chd)) == ("SLUS-20312", None)
    assert not any(
        sub in cmd for cmd in commands for sub in ("addmeta", "delmeta", "dumpmeta")
    ), commands
    # And the CHD itself still yields the ID, read from its own sectors.
    found = await extract_from_chd(str(chd), chdman_bin)
    assert found and found["game_id"] == "SLUS-20312"
