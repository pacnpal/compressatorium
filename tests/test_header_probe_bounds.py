"""Hostile headers: a size read from an image must never drive a huge read.

The header probes run on any file Auto is pointed at (and on an
unauthenticated ``/api/jobs/auto-plan``), so a planted image whose ISO 9660
directory or file extent claims 0xffffffff bytes must be parsed in bounded
memory and simply yield no header. The images here are a few KiB of real
structure padded *sparse* to 64 MiB, so an unbounded read has bytes to eat
without the test writing them.
"""
from __future__ import annotations

import struct
import tracemalloc

import pytest
from services import platform_detect
from services.platform_detect import detect_platform

from .test_disc_id import _make_iso

_SPARSE_SIZE = 64 * 1024 * 1024
_PEAK_LIMIT = 8 * 1024 * 1024
_PVD_ROOT_SIZE = 16 * 2048 + 156 + 10   # PVD root directory record, size field


@pytest.fixture(autouse=True)
def _fresh_cache(tmp_path, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "chd_volumes", str(tmp_path))
    platform_detect.clear_cache()
    yield
    platform_detect.clear_cache()


def _hostile_cooked(field: str, cnf: bytes) -> bytearray:
    data = bytearray(_make_iso({"SYSTEM.CNF": cnf}))
    if field == "root":
        offset = _PVD_ROOT_SIZE
    else:
        offset = data.index(b"SYSTEM.CNF") - 33 + 10   # file record, extent size
    struct.pack_into("<I", data, offset, 0xFFFFFFFF)
    return data


def _probe(path: str):
    tracemalloc.start()
    try:
        result = detect_platform(path)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return result, peak


@pytest.mark.parametrize("field", ["root", "extent"])
def test_a_hostile_iso_probes_in_bounded_memory_and_gives_no_header(tmp_path, field):
    path = tmp_path / "downloads" / "game.iso"
    path.parent.mkdir(parents=True)
    with open(path, "wb") as fh:
        fh.write(_hostile_cooked(field, b"BOOT2 = cdrom0:\\SLUS_203.12;1\n"))
        fh.truncate(_SPARSE_SIZE)

    result, peak = _probe(str(path))

    assert peak < _PEAK_LIMIT, f"probe allocated {peak} bytes"
    assert (result.platform, result.how) == (None, "unsorted")


@pytest.mark.parametrize("field", ["root", "extent"])
def test_a_hostile_raw_bin_probes_in_bounded_memory_and_gives_no_header(tmp_path, field):
    cooked = _hostile_cooked(field, b"BOOT = cdrom:\\SCUS_941.63;1\n")
    path = tmp_path / "downloads" / "game.bin"
    path.parent.mkdir(parents=True)
    with open(path, "wb") as fh:
        for start in range(0, len(cooked), 2048):
            sector = bytearray(2352)
            sector[24:24 + 2048] = cooked[start:start + 2048]
            fh.write(sector)
        fh.truncate(_SPARSE_SIZE)

    result, peak = _probe(str(path))

    assert peak < _PEAK_LIMIT, f"probe allocated {peak} bytes"
    assert (result.platform, result.how) == (None, "unsorted")


def test_an_honest_iso_still_names_its_platform(tmp_path):
    path = tmp_path / "downloads" / "game.iso"
    path.parent.mkdir(parents=True)
    path.write_bytes(_make_iso({"SYSTEM.CNF": b"BOOT2 = cdrom0:\\SLUS_203.12;1\n"}))

    assert detect_platform(str(path)).platform == "ps2"
