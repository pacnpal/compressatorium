"""Opt-in: the Round-trip against each real tool's output (#301).

Every test here runs the production conversion (the service's own
``convert``, so the same command line, flags and defaults the app uses) and
then checks that the Source and the output have equal content digests and that
the output passes its container check. Each test skips when its binary is not
installed, so the normal suite never runs them. The Docker image ships all of
them but JWUDTool's 25 GB input and nsz's keys; see docs/RELEASE_NOTES.md.

Run inside the image (or anywhere the binaries are on PATH)::

    PYTHONPATH=app python -m pytest -q tests/test_round_trip_301_real.py -rs

Inputs that cannot be generated: ``NSZ_TEST_NSP`` / ``NSZ_TEST_XCI`` (real
Switch dumps, with prod.keys where the app finds them, e.g. ``SWITCH_KEYS``)
and ``JWUD_TEST_WUD`` (a real 25 GB Wii U image). ``MAXCSO_TEST_LARGE=1``
adds a 2.1 GiB ISO, so maxcso picks the larger block it uses for big images.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import pytest
from services.tools import registry


def _have(path: str | None) -> bool:
    if not path:
        return False
    if os.path.isfile(path) and os.access(path, os.X_OK):
        return True
    return shutil.which(path) is not None


async def _convert(tool_id: str, source: Path, output: Path, mode: str, **kwargs) -> None:
    """The production conversion: the tool's service.convert, drained."""
    tool = registry.get(tool_id)
    async for _update in tool.convert(str(source), str(output), mode, **kwargs):
        pass


async def _assert_round_trips(tool_id: str, source: Path, output: Path) -> None:
    tool = registry.get(tool_id)
    check = await tool.verify(str(output))
    assert check["valid"], check
    source_digest = await tool.content_digest(str(source))
    output_digest = await tool.content_digest(str(output))
    assert source_digest is not None
    assert source_digest == output_digest


def _iso(path: Path, sectors: int) -> Path:
    """A sector-aligned image: random, zeros (compressible), and a pattern."""
    third = sectors // 3
    pattern = bytes((i * 7) % 251 for i in range(2048))
    path.write_bytes(
        os.urandom(third * 2048) + b"\0" * (third * 2048)
        + pattern * (sectors - 2 * third),
    )
    return path


# ---------------------------------------------------------------------------
# maxcso, and cso_to_chd
# ---------------------------------------------------------------------------

_MAXCSO = registry.get("cso")._service.maxcso_path


@pytest.mark.skipif(not _have(_MAXCSO), reason="maxcso is not installed")
@pytest.mark.parametrize("mode", [
    "cso_compress", "cso2_compress", "zso_compress", "dax_compress",
])
@pytest.mark.parametrize("compression", [None, "fast", "max"])
def test_real_maxcso_output_round_trips(tmp_path, mode, compression):
    iso = _iso(tmp_path / "Game.iso", 300)
    out = Path(registry.get("cso").output_path(mode, str(iso)))
    asyncio.run(_convert("cso", iso, out, mode, compression=compression))
    asyncio.run(_assert_round_trips("cso", iso, out))


@pytest.mark.skipif(
    not _have(_MAXCSO) or not os.environ.get("MAXCSO_TEST_LARGE"),
    reason="set MAXCSO_TEST_LARGE=1 with maxcso installed (writes a 2.1 GiB ISO)",
)
@pytest.mark.parametrize("mode", ["cso_compress", "zso_compress"])
def test_real_maxcso_large_iso_round_trips(tmp_path, mode):
    """Past 2 GiB maxcso picks a larger block than 2048 on its own; the last
    block is short (the image is not a block multiple)."""
    iso = tmp_path / "Big.iso"
    with open(iso, "wb") as fh:
        fh.write(os.urandom(64 << 20))
        fh.truncate((2 << 30) + 100 * 2048)
    out = Path(registry.get("cso").output_path(mode, str(iso)))
    asyncio.run(_convert("cso", iso, out, mode, compression=None))
    asyncio.run(_assert_round_trips("cso", iso, out))


_CHDMAN = registry.get("chdman")._service.chdman_path


@pytest.mark.skipif(
    not (_have(_MAXCSO) and _have(_CHDMAN)), reason="maxcso and chdman are needed",
)
def test_real_cso_to_chd_round_trips(tmp_path):
    iso = _iso(tmp_path / "Game.iso", 300)
    cso = tmp_path / "Game.cso"
    asyncio.run(_convert("cso", iso, cso, "cso_compress"))
    iso.unlink()
    chain = registry.get("chain")
    out = Path(chain.output_path("cso_to_chd", str(cso)))
    asyncio.run(_convert("chain", cso, out, "cso_to_chd"))
    asyncio.run(_assert_round_trips("chain", cso, out))


# ---------------------------------------------------------------------------
# chdman Create HD, Create raw, Create DVD and Copy
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _have(_CHDMAN), reason="chdman is not installed")
@pytest.mark.parametrize("mode", [
    "createhd",
    "createdvd",
    pytest.param("createraw", marks=pytest.mark.xfail(
        strict=True, raises=RuntimeError,
        reason="pre-existing: the app runs createraw without --unitsize, which "
               "chdman 0.285 refuses, so the conversion itself fails",
    )),
])
def test_real_chdman_image_modes_round_trip(tmp_path, mode):
    image = _iso(tmp_path / "Disk.iso", 600)
    out = Path(registry.get("chdman").output_path(mode, str(image)))
    asyncio.run(_convert("chdman", image, out, mode))
    asyncio.run(_assert_round_trips("chdman", image, out))


@pytest.mark.skipif(not _have(_CHDMAN), reason="chdman is not installed")
def test_real_raw_chd_has_no_metadata_and_its_data_sha1_is_the_image(tmp_path):
    """The tagless branch, against a raw CHD real chdman writes (with the unit
    size the app's createraw does not pass yet)."""
    import subprocess

    image = _iso(tmp_path / "Disk.iso", 600)
    chd = tmp_path / "Disk.chd"
    subprocess.run(
        [_CHDMAN, "createraw", "-i", str(image), "-o", str(chd), "-hs", "4096", "-us", "512"],
        check=True, capture_output=True,
    )
    asyncio.run(_assert_round_trips("chdman", image, chd))


@pytest.mark.skipif(not _have(_CHDMAN), reason="chdman is not installed")
def test_real_chdman_copy_round_trips(tmp_path):
    image = _iso(tmp_path / "Disk.iso", 600)
    chd = tmp_path / "Disk.chd"
    asyncio.run(_convert("chdman", image, chd, "createdvd"))
    out = Path(registry.get("chdman").output_path("copy", str(chd)))
    asyncio.run(_convert("chdman", chd, out, "copy"))
    asyncio.run(_assert_round_trips("chdman", chd, out))


# ---------------------------------------------------------------------------
# z3ds
# ---------------------------------------------------------------------------

_Z3DS = registry.get("z3ds")._service.z3ds_compressor_path


@pytest.mark.skipif(
    not _have(_Z3DS) or shutil.which("zstd") is None,
    reason="z3ds_compressor and zstd are needed",
)
@pytest.mark.parametrize("ext", [".3ds", ".cia", ".cxi"])
def test_real_z3ds_output_round_trips(tmp_path, ext):
    rom = tmp_path / f"Game{ext}"
    rom.write_bytes(os.urandom(1 << 20) + b"\0" * (3 << 20))
    out = Path(registry.get("z3ds").output_path("z3ds_compress", str(rom)))
    asyncio.run(_convert("z3ds", rom, out, "z3ds_compress"))
    asyncio.run(_assert_round_trips("z3ds", rom, out))


# ---------------------------------------------------------------------------
# romz (7z / zip)
# ---------------------------------------------------------------------------

_SEVENZIP = registry.get("romz")._service.sevenzip_path


@pytest.mark.skipif(not _have(_SEVENZIP), reason="7z is not installed")
@pytest.mark.parametrize("mode", ["romz_7z", "romz_zip"])
@pytest.mark.parametrize("name", ["Game.gba", "Pokémon Rubí (España).gba"])
def test_real_romz_archive_round_trips(tmp_path, mode, name):
    rom = tmp_path / name
    rom.write_bytes(os.urandom(200_000))
    out = Path(registry.get("romz").output_path(mode, str(rom)))
    asyncio.run(_convert("romz", rom, out, mode, compression="max"))
    asyncio.run(_assert_round_trips("romz", rom, out))


# ---------------------------------------------------------------------------
# nsz and JWUDTool: need real dumps the tests cannot generate
# ---------------------------------------------------------------------------

_NSZ = registry.get("nsz")._service.nsz_path


@pytest.mark.skipif(
    not _have(_NSZ) or not os.environ.get("NSZ_TEST_NSP"),
    reason="needs nsz, prod.keys and a real NSP in NSZ_TEST_NSP",
)
def test_real_nsz_output_round_trips(tmp_path):
    """The app's own compression (no --keep). A base-game NSP should round
    trip; an update NSP whose NDV0 fragments nsz drops is expected not to."""
    nsp = Path(os.environ["NSZ_TEST_NSP"])
    out = tmp_path / (nsp.stem + ".nsz")
    asyncio.run(_convert("nsz", nsp, out, "nsz_compress"))
    asyncio.run(_assert_round_trips("nsz", nsp, out))


@pytest.mark.skipif(
    not _have(_NSZ) or not os.environ.get("NSZ_TEST_XCI"),
    reason="needs nsz, prod.keys and a real XCI in NSZ_TEST_XCI",
)
def test_real_nsz_xci_records_whether_it_rebuilds(tmp_path):
    """Records what nsz does with a real XCI: equal digests, or the specific
    reason the Round-trip gives. Either is a pass; the run's output is the
    evidence for DESIGN §3.3.12."""
    xci = Path(os.environ["NSZ_TEST_XCI"])
    out = tmp_path / (xci.stem + ".xcz")
    asyncio.run(_convert("nsz", xci, out, "nsz_compress"))
    tool = registry.get("nsz")
    source = asyncio.run(tool.content_digest(str(xci)))
    output = asyncio.run(tool.content_digest(str(out)))
    print(f"XCI rebuilt byte for byte: {source == output}")
    assert source.kind == output.kind


_JWUD = registry.get("jwud")._service.jwudtool_path


@pytest.mark.skipif(
    not _have(_JWUD) or not os.environ.get("JWUD_TEST_WUD"),
    reason="needs JWUDTool and a real 25 GB Wii U image in JWUD_TEST_WUD",
)
def test_real_jwudtool_output_round_trips(tmp_path):
    wud = Path(os.environ["JWUD_TEST_WUD"])
    out = tmp_path / (wud.stem + ".wux")
    asyncio.run(_convert("jwud", wud, out, "jwud_compress"))
    asyncio.run(_assert_round_trips("jwud", wud, out))
