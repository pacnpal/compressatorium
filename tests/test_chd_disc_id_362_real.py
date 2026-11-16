"""Real chdman: the library scan reads disc IDs from chdman-made CHDs (#362).

Opt-in: runs the actual ``chdman``. It SKIPS (never fails) when the binary is
missing. Point ``CHDMAN`` at a binary, or have ``chdman`` (or the configured
``CHDMAN_PATH``) on the system.

A PS1 CD CHD and a PS2 DVD CHD are made by the bundled chdman from small test
images, the images are deleted, and the library scan must still record both
disc IDs, read from the CHDs' own sectors. Their real v5 headers must read
with MAME's layout.
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from config import settings

from ._chd_fakes import mode2_frames
from .test_disc_id import _make_iso

PS2_CNF = b"BOOT2 = cdrom0:\\SLUS_203.12;1\r\nVER = 1.00\r\n"
PS1_CNF = b"BOOT = cdrom:\\SCUS_941.63;1\r\n"


def _binary() -> str | None:
    for candidate in (os.environ.get("CHDMAN"), settings.chdman_path, "chdman"):
        if candidate and (os.path.isfile(candidate) or shutil.which(candidate)):
            return shutil.which(candidate) or candidate
    return None


@pytest.fixture(name="chdman_bin")
def _chdman_bin(monkeypatch):
    from services.chdman import chdman_service

    binary = _binary()
    if binary is None:
        pytest.skip("no chdman binary (set CHDMAN to run)")
    monkeypatch.setattr(chdman_service, "chdman_path", binary)
    monkeypatch.setattr(settings, "chdman_path", binary)
    return binary


def _make_chds(tmp_path, binary):
    """A PS2 DVD CHD and a PS1 CD CHD in a Volume, with no Source left."""
    work = tmp_path / "work"
    volume = tmp_path / "volume"
    work.mkdir()
    volume.mkdir()
    iso = work / "PS2 Game.iso"
    iso.write_bytes(_make_iso({"SYSTEM.CNF": PS2_CNF}))
    subprocess.run(
        [binary, "createdvd", "-hs", "2048", "-f", "-i", str(iso),
         "-o", str(volume / "PS2 Game.chd")],
        check=True, capture_output=True,
    )
    # A Mode 2 Form 1 data track, as a PS1 disc is.
    bin_path = work / "PS1 Game.bin"
    bin_path.write_bytes(mode2_frames(_make_iso({"SYSTEM.CNF": PS1_CNF}), 2352, 24))
    cue = work / "PS1 Game.cue"
    cue.write_text(
        'FILE "PS1 Game.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n'
    )
    subprocess.run(
        [binary, "createcd", "-f", "-i", str(cue), "-o", str(volume / "PS1 Game.chd")],
        check=True, capture_output=True,
    )
    shutil.rmtree(work)
    return volume


def test_real_chd_headers_read_with_mames_v5_layout(tmp_path, chdman_bin):
    from services.disc_id import read_chd_header

    volume = _make_chds(tmp_path, chdman_bin)

    dvd = read_chd_header(str(volume / "PS2 Game.chd"))
    cd = read_chd_header(str(volume / "PS1 Game.chd"))

    assert dvd.version == 5 and dvd.unit_bytes == 2048 and dvd.hunk_bytes == 2048
    assert dvd.compressors[0] == "lzma"
    assert cd.unit_bytes == 2448 and cd.hunk_bytes % cd.unit_bytes == 0
    assert cd.compressors[:3] == ("cdlz", "cdzl", "cdfl")
    assert dvd.map_offset > 0 and cd.map_offset > 0


@pytest.mark.asyncio
async def test_the_scan_records_disc_ids_of_real_chdman_chds(
    tmp_path, chdman_bin, monkeypatch, db_path, reset_db_engine,
):
    from routes import info as info_routes
    from services import db as _db
    from services.chd_metadata_store import chd_metadata_store

    volume = _make_chds(tmp_path, chdman_bin)
    _db.init_engine(db_path)
    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(volume))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(volume))
    try:
        await info_routes.scan_metadata_task(force=True)

        ps2 = await chd_metadata_store.get_disc_id_info(str(volume / "PS2 Game.chd"))
        ps1 = await chd_metadata_store.get_disc_id_info(str(volume / "PS1 Game.chd"))
    finally:
        reset_db_engine()

    assert ps2[0] == "SLUS-20312"
    assert ps1[0] == "SCUS-94163"
