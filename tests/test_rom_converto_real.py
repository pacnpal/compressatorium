"""Opt-in: the real rom-converto binary on tiny synthetic Xbox 360 images (#422).

Skips unless the configured binary (``ROM_CONVERTO_PATH``, default
``/usr/local/bin/rom-converto``, which the Docker image ships) is installed,
so the normal suite never runs it. The inputs are the xiso suite's hand-built
XDVDFS images (a few KB of real data, the XGD3 one sparse): never a game.

Run inside the image, or point it at a downloaded release::

    ROM_CONVERTO_PATH=/path/to/rom-converto-cli-<os>-<arch> \\
        PYTHONPATH=app python -m pytest -q tests/test_rom_converto_real.py -rs
"""
from __future__ import annotations

import os
import shutil

import pytest

from app.services.tools import registry

from .test_xiso_service import build_image

TOOL = registry.get("rom_converto")


def _installed(path: str) -> bool:
    if os.sep in path:
        return os.path.isfile(path) and os.access(path, os.X_OK)
    return shutil.which(path) is not None


pytestmark = pytest.mark.skipif(
    not _installed(TOOL._service.binary_path),
    reason="rom-converto is not installed (set ROM_CONVERTO_PATH to run these)",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("layout", ["XISO", "XGD3"])
async def test_real_binary_writes_a_zar_that_verifies(tmp_path, layout):
    src = tmp_path / "Game.iso"
    expected = build_image(src, layout=layout)
    out = tmp_path / "Game.zar"

    assert await TOOL.is_ready(), TOOL.not_ready_reason
    updates = [u async for u in TOOL.convert(str(src), str(out), "zar_compress")]

    assert updates[-1]["progress"] == 100
    assert out.stat().st_size > 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Game.iso", "Game.zar"]

    check = await TOOL.verify(str(out))
    assert check["valid"], check

    info = TOOL.info_model(await TOOL.info(str(out)), str(out))
    assert info.format == "Xbox 360 ZArchive"
    assert info.file_count == expected["file_count"]
    assert info.logical_size == expected["data_bytes"]
    assert info.boot_executable is True


@pytest.mark.asyncio
async def test_real_binary_verify_fails_a_damaged_zar(tmp_path):
    src = tmp_path / "Game.iso"
    build_image(src, layout="XISO")
    out = tmp_path / "Game.zar"
    [_ async for _ in TOOL.convert(str(src), str(out), "zar_compress")]

    data = bytearray(out.read_bytes())
    data[:4] = b"\0\0\0\0"
    out.write_bytes(bytes(data))

    check = await TOOL.verify(str(out))
    assert check["valid"] is False


# --------------------------------------------------------------------------- #
# PSP and 3DS (#423)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_real_binary_refuses_a_pbp_without_a_game_image(tmp_path):
    """A PBP whose PSAR says NPUMDIMG but holds no game: rom-converto's own
    refusal reaches the job, and nothing is left behind. A working PBP needs a
    game's encrypted image, which can't be built here (spike #421)."""
    from .rom_converto_fixtures import pbp_bytes

    src = tmp_path / "Game.pbp"
    src.write_bytes(pbp_bytes(payload=b"\0" * 4096))

    with pytest.raises(RuntimeError, match="rom-converto"):
        [_ async for _ in TOOL.convert(str(src), str(tmp_path / "Game.iso"), "pbp_to_iso")]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Game.pbp"]


_HOMEBREW_CIA = os.environ.get("ROM_CONVERTO_TEST_CIA", "")


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.path.isfile(_HOMEBREW_CIA),
    reason="set ROM_CONVERTO_TEST_CIA to a homebrew CIA (FBI, MIT) to run this",
)
async def test_real_binary_converts_a_cia_to_cci_and_back(tmp_path):
    src = tmp_path / "Game.cia"
    shutil.copyfile(_HOMEBREW_CIA, src)
    cci = tmp_path / "Game.cci"

    [_ async for _ in TOOL.convert(str(src), str(cci), "cia_to_cci")]
    assert (await TOOL.verify(str(cci)))["valid"]

    back = tmp_path / "back" / "Game.cia"
    back.parent.mkdir()
    [_ async for _ in TOOL.convert(str(cci), str(back), "cci_to_cia")]
    assert (await TOOL.verify(str(back)))["valid"]
    # Not byte-equal to the original (unsigned, zero title key): no Round-trip.
    assert back.read_bytes() != src.read_bytes()
