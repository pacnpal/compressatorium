"""The disc ID read from a chdman-made CHD (#362).

The old in-process reader parsed the v5 header at the wrong offsets and
expected an uncompressed map, so no CHD chdman makes ever opened, and a scan
recorded ``game_id`` NULL for every one. Now the header is read with MAME's v5
layout (``parse_chd_header``), and the few disc sectors the ISO 9660 walk
needs are read by the bundled chdman (``chdman extractraw`` with a byte
range), so no CHD map or codec is decoded here.

chdman is faked at the ``SubprocessRunner.run_capture`` seam: ``extractraw``
serves the requested byte range of an in-memory "logical image" of the CHD.
The real-binary check is ``tests/test_chd_disc_id_362_real.py``.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from .test_disc_id import _make_iso

# The first 124 bytes of CHDs made by the bundled chdman 0.285 from small test
# images: `createdvd -hs 2048` of a PS2-style ISO, and `createcd` of a one-track
# MODE2/2352 cue.
REAL_DVD_HEADER = bytes.fromhex(
    "4d436f6d707248440000007c000000056c7a6d617a6c696268756666666c6163"
    "000000000000b0000000000000000132000000000000007c0000080000000800"
    "06904a86fc9788db70e8399cc34de72bdcc5b7f8a6fce6ca89d5de23ad24f915"
    "46cb5543baae48480000000000000000000000000000000000000000"
)
REAL_CD_HEADER = bytes.fromhex(
    "4d436f6d707248440000007c0000000563646c7a63647a6c6364666c00000000"
    "000000000000e58000000000000001db000000000000007c00004c8000000990"
    "ee2104ce00317c884b9fdcf8e660a5d2ccec363fab5a2ad89e43fd470e214df6"
    "779909380a4c24cc0000000000000000000000000000000000000000"
)
PS2_CNF = b"BOOT2 = cdrom0:\\SLUS_203.12;1\r\nVER = 1.00\r\n"
PS1_CNF = b"BOOT = cdrom:\\SCUS_941.63;1\r\n"
BOUND = 5.0


@pytest.fixture(name="disc_id")
def _disc_id():
    from services import disc_id

    return disc_id


def _mode2_frames(cooked: bytes) -> bytes:
    """A cooked ISO as a CD CHD stores it: 2448-byte units, Mode 2 Form 1
    (24-byte header, 2048 bytes of data) plus 96 bytes of subcode."""
    out = bytearray()
    for start in range(0, len(cooked), 2048):
        unit = bytearray(2448)
        unit[0:12] = b"\x00" + b"\xff" * 10 + b"\x00"
        unit[15] = 2
        unit[24:24 + 2048] = cooked[start:start + 2048]
        out += unit
    return bytes(out)


class FakeExtractraw:
    """``chdman extractraw -i CHD -o OUT -isb START -ib LENGTH``, served from
    each CHD's logical image; records every range asked for."""

    def __init__(self) -> None:
        self.images: dict[str, bytes] = {}
        self.ranges: list[tuple[int, int]] = []
        self.result: tuple | None = None    # force (rc, out, err) for every call

    async def run_capture(self, cmd, **kwargs):
        assert "extractraw" in cmd, cmd
        start = int(cmd[cmd.index("-isb") + 1])
        length = int(cmd[cmd.index("-ib") + 1])
        self.ranges.append((start, length))
        if self.result is not None:
            return self.result
        image = self.images[cmd[cmd.index("-i") + 1]]
        Path(cmd[cmd.index("-o") + 1]).write_bytes(image[start:start + length])
        return 0, b"", b""


@pytest.fixture(name="chdman")
def _chdman(monkeypatch):
    from services.chdman import chdman_service

    fake = FakeExtractraw()
    monkeypatch.setattr(chdman_service.runner, "run_capture", fake.run_capture)
    return fake


def _chd(tmp_path, name, header, image, fake):
    chd = tmp_path / name
    chd.write_bytes(header + b"\0" * 64)
    fake.images[str(chd)] = image
    return chd


# ---------------------------------------------------------------------------
# The v5 header, read with MAME's layout
# ---------------------------------------------------------------------------


def test_a_real_dvd_chd_header_reads_with_mames_v5_layout(disc_id):
    header = disc_id.parse_chd_header(REAL_DVD_HEADER)

    assert header.version == 5
    assert header.compressors == ("lzma", "zlib", "huff", "flac")
    assert header.logical_bytes == 45056
    assert header.map_offset == 306
    assert header.meta_offset == 124
    assert header.hunk_bytes == 2048
    assert header.unit_bytes == 2048


def test_a_real_cd_chd_header_reads_with_mames_v5_layout(disc_id):
    header = disc_id.parse_chd_header(REAL_CD_HEADER)

    assert header.compressors == ("cdlz", "cdzl", "cdfl", "")
    assert header.logical_bytes == 58752
    assert header.map_offset == 475
    assert header.hunk_bytes == 19584
    assert header.unit_bytes == 2448


@pytest.mark.parametrize(
    "raw",
    [b"", b"MComprHD" + b"\0" * 10, b"NotACHD!" + b"\0" * 116, REAL_DVD_HEADER[:100]],
    ids=["empty", "short", "foreign-magic", "truncated"],
)
def test_a_short_or_foreign_header_is_not_a_chd(disc_id, raw):
    assert disc_id.parse_chd_header(raw) is None


# ---------------------------------------------------------------------------
# Disc IDs from chdman-made CHDs, with no Source beside them
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_ps2_dvd_chd_yields_its_disc_id(tmp_path, disc_id, chdman):
    image = _make_iso({"SYSTEM.CNF": PS2_CNF})
    chd = _chd(tmp_path, "Game.chd", REAL_DVD_HEADER, image, chdman)

    result = await asyncio.wait_for(disc_id.extract_from_chd(str(chd), "chdman"), BOUND)

    assert result and result["game_id"] == "SLUS-20312"


@pytest.mark.asyncio
async def test_a_ps1_cd_chd_yields_its_disc_id(tmp_path, disc_id, chdman):
    image = _mode2_frames(_make_iso({"SYSTEM.CNF": PS1_CNF}))
    chd = _chd(tmp_path, "Game.chd", REAL_CD_HEADER, image, chdman)

    result = await asyncio.wait_for(disc_id.extract_from_chd(str(chd), "chdman"), BOUND)

    assert result and result["game_id"] == "SCUS-94163"


@pytest.mark.asyncio
async def test_only_the_sectors_the_walk_needs_are_read(tmp_path, disc_id, chdman):
    image = _make_iso({"SYSTEM.CNF": PS2_CNF}) + b"\0" * (2048 * 4000)
    chd = _chd(tmp_path, "Game.chd", REAL_DVD_HEADER, image, chdman)

    await disc_id.extract_from_chd(str(chd), "chdman")

    # The volume descriptor, the root directory, SYSTEM.CNF: a few sectors,
    # never the whole disc.
    assert 0 < len(chdman.ranges) <= 8
    assert sum(length for _start, length in chdman.ranges) <= 64 * 2048


@pytest.mark.asyncio
async def test_a_chdman_read_that_never_answers_is_storage_abandoned(
    tmp_path, disc_id, chdman,
):
    chd = _chd(tmp_path, "Stuck.chd", REAL_DVD_HEADER, b"", chdman)
    chdman.result = (None, b"", b"")      # the runner's answer to a timed-out child

    with pytest.raises(disc_id.DiscIdStorageAbandoned, match="Stuck.chd"):
        await asyncio.wait_for(disc_id.extract_from_chd(str(chd), "chdman"), BOUND)


@pytest.mark.asyncio
async def test_cancel_during_a_chdman_read_raises_read_cancelled(
    tmp_path, disc_id, monkeypatch,
):
    from services.chdman import chdman_service
    from services.subprocess_runner import ReadCancelled

    chd = tmp_path / "Game.chd"
    chd.write_bytes(REAL_DVD_HEADER)
    cancel = asyncio.Event()
    reads: list[list[str]] = []

    async def cancelled_mid_read(cmd, **kwargs):
        # Cancel lands while chdman reads; the runner answers None for it.
        reads.append(list(cmd))
        cancel.set()
        return None, b"", b""

    monkeypatch.setattr(chdman_service.runner, "run_capture", cancelled_mid_read)
    with pytest.raises(ReadCancelled):
        await asyncio.wait_for(
            disc_id.extract_from_chd(str(chd), "chdman", cancel_event=cancel), BOUND,
        )
    assert len(reads) == 1 and "extractraw" in reads[0]
