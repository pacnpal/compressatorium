"""The duplicate finder (#388) with the real tools.

Opt-in, like the real-chdman tests of #300 and #352: each test SKIPS (never
fails) when its binary is missing, so CI and normal ``pytest`` runs stay green.
Point ``CHDMAN`` / ``DOLPHIN_TOOL`` at the binaries, or have ``chdman`` and
``dolphin-tool`` (or the configured paths) on the system; the app's Docker
image bundles both.

What it pins down against the real tools:

- a ``createdvd`` CHD's data SHA1 is the SHA1 of the ISO it was made from, and
  its overall SHA1 is not. This is what lets a DVD CHD carry a ``raw:`` key;
- an ISO, the RVZ dolphin-tool makes from it and the DVD CHD chdman makes from
  it are one group, found through the HTTP routes.

The disc is synthetic (a GameCube header and random data): neither tool reads
anything beyond the image bytes for these modes.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import struct
from pathlib import Path

import httpx
import pytest
from services import content_keys
from services.chd_metadata_store import chd_metadata_store
from services.tools import registry

from config import settings

from . import test_duplicates_388 as duplicates_388
from . import test_folder_library_303 as folder_library_303

_db_fixture = folder_library_303._db_fixture
_client = duplicates_388._client


def _binary(env: str, configured: str, name: str) -> str | None:
    for candidate in (os.environ.get(env), configured, name):
        if candidate and (os.path.isfile(candidate) or shutil.which(candidate)):
            return shutil.which(candidate) or candidate
    return None


@pytest.fixture(name="chdman_bin")
def _chdman_bin(monkeypatch):
    binary = _binary("CHDMAN", settings.chdman_path, "chdman")
    if binary is None:
        pytest.skip("no chdman binary (set CHDMAN to run)")
    tool = registry.get("chdman")
    monkeypatch.setattr(tool._service, "chdman_path", binary)
    monkeypatch.setattr(tool, "binary_path", binary, raising=False)
    return binary


@pytest.fixture(name="dolphin_bin")
def _dolphin_bin(monkeypatch):
    binary = _binary("DOLPHIN_TOOL", settings.dolphin_tool_path, "dolphin-tool")
    if binary is None:
        pytest.skip("no dolphin-tool binary (set DOLPHIN_TOOL to run)")
    tool = registry.get("dolphin")
    monkeypatch.setattr(tool._service, "dolphin_tool_path", binary)
    monkeypatch.setattr(tool, "binary_path", binary, raising=False)
    return binary


def _disc(path: Path, sectors: int = 2049) -> str:
    """A GameCube-headed image of *sectors* 2048-byte sectors (an odd count, so
    the CHD's last hunk is padded); returns its SHA1."""
    path.parent.mkdir(parents=True, exist_ok=True)
    head = bytearray(0x440)
    head[0:6] = b"GALE01"
    struct.pack_into(">I", head, 0x1C, 0xC2339F3D)
    data = bytes(head) + os.urandom(sectors * 2048 - len(head))
    path.write_bytes(data)
    return hashlib.sha1(data).hexdigest()


async def _createdvd(iso: Path, chd: Path) -> None:
    tool = registry.get("chdman")
    async for _ in tool.convert(str(iso), str(chd), "createdvd"):
        pass
    assert chd.is_file()


@pytest.mark.asyncio
async def test_a_createdvd_chd_data_sha1_is_the_iso_sha1(db: Path, chdman_bin: str) -> None:
    iso = db / "roms" / "Game.iso"
    sha1 = _disc(iso)
    chd = db / "roms" / "Game.chd"
    await _createdvd(iso, chd)

    # chdman info, read for the key and cached as a scan would.
    assert await content_keys.build(str(chd)) == f"raw:{sha1}"
    assert await content_keys.build(str(iso)) == f"raw:{sha1}"
    cached = await chd_metadata_store.get_fresh_metadata(str(chd))
    assert cached["data_sha1"].lower() == sha1
    # The overall SHA1 folds in the metadata: never the image's.
    assert cached["sha1"].lower() != sha1


@pytest.mark.asyncio
async def test_an_iso_its_rvz_and_its_dvd_chd_are_one_group(
    db: Path, client: httpx.AsyncClient, chdman_bin: str, dolphin_bin: str,
) -> None:
    roms = db / "roms"
    sha1 = _disc(roms / "Game.iso")
    await _createdvd(roms / "Game.iso", roms / "Game.chd")
    tool = registry.get("dolphin")
    async for _ in tool.convert(
        str(roms / "Game.iso"), str(roms / "Game.rvz"), "dolphin_rvz", compression="zstd:5",
    ):
        pass
    assert (roms / "Game.rvz").is_file()
    _disc(roms / "Other.iso")

    library = await duplicates_388._register(client, roms)
    await duplicates_388._find(client, library)

    found = await duplicates_388._duplicates(client)
    assert [row["image_key"] for row in found] == [f"raw:{sha1}"]
    assert duplicates_388._names(found[0]) == ["Game.chd", "Game.iso", "Game.rvz"]
