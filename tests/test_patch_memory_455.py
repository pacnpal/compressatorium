"""Patching a large ROM keeps its memory bounded (#455).

A 400 MiB ROM under the default ``CHD_PATCH_MAX_ROM_SIZE`` used to get 7z
OOM-killed: the source and the patched bytes stayed in memory while romz's
``-mx=9 -md=256m -mfb=273 -mmt=on`` pack ran. Now:

* the engine patches one mutable buffer (IPS in place; BPS into one target,
  reading the source through a view), with no whole-ROM copy on the way out;
* the bench writes the patched ROM to its scratch file and holds no ROM bytes
  while 7z runs, and reads the archive back by digest, not by a second copy;
* the pack keeps romz's profile but with a dictionary sized from the ROM and
  capped, and a fixed thread count, so 7z's memory doesn't grow with the ROM or
  the core count.

The ROMs here are synthetic: a few MiB of patterned bytes.
"""

from __future__ import annotations

import os
import stat
import sys
import tracemalloc
from pathlib import Path

import httpx
import pytest
from services import patch_engine
from services.patch_engine import apply_patch_in_place

from . import test_patch_bench_428 as bench
from .test_patch_engine_428 import (
    SOURCE_READ,
    TARGET_READ,
    _action,
    _bps,
    _ips,
    _record,
    _rle,
)

_db_fixture = bench._db_fixture
_env = bench._env
_client = bench._client

MIB = 1 << 20


def _big_rom(size: int) -> bytes:
    return (bytes(range(251)) * (size // 251 + 1))[:size]


# ----------------------------------------------------------------------
# the engine: one mutable buffer
# ----------------------------------------------------------------------


def test_an_ips_patch_is_applied_in_place_on_the_buffer_given() -> None:
    rom = bytearray(_big_rom(4096))
    patch = _ips(_record(16, b"PATCHED!"), _rle(100, 4, 0xAA))

    result = apply_patch_in_place(rom, patch, max_target=1 << 20)

    assert result.target is rom
    assert result.kind == "ips" and result.checked is False
    assert rom[16:24] == b"PATCHED!" and rom[100:104] == b"\xaa" * 4


def test_an_in_place_ips_patch_can_still_grow_and_truncate_the_rom() -> None:
    rom = bytearray(b"\x01" * 32)
    grown = apply_patch_in_place(rom, _ips(_record(40, b"end")), max_target=64).target
    assert bytes(grown) == b"\x01" * 32 + bytes(8) + b"end"
    cut = apply_patch_in_place(bytearray(b"\x02" * 32), _ips(truncate=10), max_target=64).target
    assert bytes(cut) == b"\x02" * 10


def test_an_in_place_bps_patch_gives_the_target_and_checks_it() -> None:
    source = _big_rom(4096)
    target = source[:100] + b"translated" + source[110:]
    actions = (
        _action(SOURCE_READ, 100) + _action(TARGET_READ, 10) + b"translated"
        + _action(SOURCE_READ, len(source) - 110)
    )
    result = apply_patch_in_place(bytearray(source), _bps(source, target, actions), max_target=8192)
    assert bytes(result.target) == target
    assert result.kind == "bps" and result.checked is True


def _peak_while(func) -> int:
    """Bytes allocated at the peak of *func*, above what was live before it."""
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        before = tracemalloc.get_traced_memory()[0]
        func()
        return tracemalloc.get_traced_memory()[1] - before
    finally:
        tracemalloc.stop()


def test_applying_an_ips_patch_allocates_no_copy_of_the_rom() -> None:
    size = 8 * MIB
    rom = bytearray(_big_rom(size))
    patch = _ips(_record(16, b"PATCHED!"), _rle(5000, 300, 0))

    peak = _peak_while(lambda: apply_patch_in_place(rom, patch, max_target=size))

    assert peak < size // 8, peak


def test_applying_a_bps_patch_allocates_one_target_and_no_source_copy() -> None:
    size = 8 * MIB
    source = _big_rom(size)
    target = source[:100] + b"translated" + source[110:]
    patch = _bps(source, target, (
        _action(SOURCE_READ, 100) + _action(TARGET_READ, 10) + b"translated"
        + _action(SOURCE_READ, size - 110)
    ))
    rom = bytearray(source)

    peak = _peak_while(lambda: apply_patch_in_place(rom, patch, max_target=size))

    # The target buffer itself, and nothing ROM-sized besides.
    assert peak < size + size // 8, peak


@pytest.mark.parametrize(("gap", "length"), [(1, 1), (1, 1000), (3, 1000), (7, 7), (5, 3), (64, 4097)])
def test_an_overlapping_target_copy_matches_a_byte_by_byte_copy(gap: int, length: int) -> None:
    start = 10
    at = start + gap
    expected = bytearray(_big_rom(at)) + bytearray(length)
    for i in range(length):
        expected[at + i] = expected[start + i]
    target = bytearray(_big_rom(at)) + bytearray(length)
    with memoryview(target) as view:
        patch_engine._target_copy(view, start, at, length)
    assert target == expected


def test_a_long_run_copies_without_a_buffer_of_its_own() -> None:
    size = 8 * MIB
    target = bytearray(size)
    target[:4] = b"abcd"
    with memoryview(target) as view:
        peak = _peak_while(lambda: patch_engine._target_copy(view, 0, 4, size - 4))
    assert target[:12] == b"abcdabcdabcd" and target[-4:] == b"abcd"
    assert peak < size // 8, peak


def test_the_pure_apply_patch_still_leaves_its_source_alone() -> None:
    source = _big_rom(1024)
    result = patch_engine.apply_patch(source, _ips(_record(0, b"X")), max_target=4096)
    assert bytes(result.target)[:1] == b"X" and source[:1] != b"X"


# ----------------------------------------------------------------------
# the bench: nothing ROM-sized held while 7z runs
# ----------------------------------------------------------------------


def _bps_for(source: bytes) -> bytes:
    target = source[:100] + b"translated" + source[110:]
    return _bps(source, target, (
        _action(SOURCE_READ, 100) + _action(TARGET_READ, 10) + b"translated"
        + _action(SOURCE_READ, len(source) - 110)
    ))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["ips", "bps"])
async def test_no_rom_bytes_are_held_while_7z_packs_the_patched_rom(
    env: Path, client: httpx.AsyncClient, monkeypatch, kind: str,
) -> None:
    from services.romz import romz_service

    size = 16 * MIB
    source = _big_rom(size)
    rom = bench._write(env / "roms" / "Big.gba", source)
    patch_bytes = _ips(_record(16, b"PATCHED!")) if kind == "ips" else _bps_for(source)
    patch = bench._write(env / f"Fix.{kind}", patch_bytes)
    del source

    seen: dict = {}
    real_convert = romz_service.convert

    async def watched_convert(*args, **kwargs):
        live, peak = tracemalloc.get_traced_memory()
        seen["live"], seen["peak"] = live - seen["baseline"], peak - seen["baseline"]
        async for update in real_convert(*args, **kwargs):
            yield update

    monkeypatch.setattr(romz_service, "convert", watched_convert)
    tracemalloc.start()
    try:
        seen["baseline"] = tracemalloc.get_traced_memory()[0]
        tracemalloc.reset_peak()
        job = await bench._run(client, {"image_path": str(rom), "patch_path": str(patch)})
    finally:
        tracemalloc.stop()

    assert job.status.value == "completed", job.error_message
    # When 7z starts, neither the ROM nor its patched copy is in memory.
    assert seen["live"] < size // 4, seen["live"]
    # Reading, patching and writing it cost one ROM for IPS (patched in
    # place), two for BPS (the source and the target it builds), and no copy
    # beyond that. (The read-back after 7z costs py7zr's LZMA window, which
    # the pack's dictionary bounds, not the ROM.)
    allowed = size + size // 2 if kind == "ips" else 2 * size + size // 2
    assert seen["peak"] < allowed, (seen["peak"], allowed)


_ARGV_7Z = """\
import os, sys
import py7zr
args = sys.argv[1:]
with open(os.environ["ARGV_LOG"], "w") as log:
    log.write("\\n".join(args))
out, src = args[args.index("--") + 1:]
with py7zr.SevenZipFile(out, "w") as archive:
    archive.write(src, os.path.basename(src))
"""


def _stub_7z(tmp_path: Path, monkeypatch, body: str) -> None:
    from services.romz import romz_service

    stub = tmp_path / "bin2" / "7z"
    stub.parent.mkdir(exist_ok=True)
    stub.write_text(f"#!{sys.executable}\n{body}")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(romz_service, "sevenzip_path", str(stub))


@pytest.mark.asyncio
async def test_the_pack_bounds_7z_memory_with_a_capped_dictionary_and_two_threads(
    env: Path, client: httpx.AsyncClient, monkeypatch, tmp_path: Path,
) -> None:
    log = tmp_path / "argv.txt"
    monkeypatch.setenv("ARGV_LOG", str(log))
    _stub_7z(tmp_path, monkeypatch, _ARGV_7Z)
    rom = bench._write(env / "roms" / "Game.gba", bench.ROM)
    patch = bench._write(env / "Fix.ips", bench._ips_fix()[0])

    job = await bench._run(client, {"image_path": str(rom), "patch_path": str(patch)})

    assert job.status.value == "completed", job.error_message
    argv = log.read_text().split("\n")
    switches = argv[: argv.index("--")]
    # romz's LZMA2 profile, so the archive is the same kind of .7z ...
    for kept in ("a", "-t7z", "-m0=lzma2", "-mx=9", "-mfb=273"):
        assert kept in switches, switches
    # ... but its memory is bounded: no 256 MiB dictionary, no thread per core.
    assert "-md=256m" not in switches and "-mmt=on" not in switches, switches
    assert "-mmt=2" in switches, switches
    [dictionary] = [s for s in switches if s.startswith("-md=")]
    assert dictionary == "-md=1m", dictionary  # a 16 KiB ROM needs no more


@pytest.mark.parametrize(
    ("rom_size", "dictionary"),
    [
        (1, MIB),
        (16 * 1024, MIB),
        (MIB, MIB),
        (MIB + 1, 2 * MIB),
        (10 * MIB, 16 * MIB),
        (64 * MIB, 64 * MIB),
        (150 * MIB, 64 * MIB),
        (400 * MIB, 64 * MIB),
        (512 * MIB, 64 * MIB),
    ],
)
def test_the_pack_dictionary_is_sized_from_the_rom_and_capped(
    rom_size: int, dictionary: int,
) -> None:
    from services import patch_bench

    assert patch_bench.pack_dictionary(rom_size) == dictionary


def test_7zs_memory_for_the_pack_stays_under_a_gibibyte_at_the_rom_cap() -> None:
    """7-Zip's own table puts the BT4 match finder at about 11.5 times the
    dictionary; one LZMA2 block encoder runs at ``-mmt=2``."""
    from services import patch_bench

    from config import settings

    estimate = patch_bench.pack_memory_estimate(settings.patch_max_rom_size)
    assert estimate < 1 << 30, estimate


# ----------------------------------------------------------------------
# a 7z killed by the OOM killer
# ----------------------------------------------------------------------


_KILLED_7Z = """\
import os, signal, sys
sys.stdout.write("\\b\\b\\b\\b 89% 1 + Big Patch Me (Patched).gba\\n")
sys.stdout.flush()
os.kill(os.getpid(), signal.SIGKILL)
"""


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="signals are POSIX")
async def test_a_7z_killed_by_a_signal_fails_the_job_in_plain_words(
    env: Path, client: httpx.AsyncClient, monkeypatch, tmp_path: Path,
) -> None:
    _stub_7z(tmp_path, monkeypatch, _KILLED_7Z)
    rom = bench._write(env / "roms" / "Big Patch Me.gba", bench.ROM)
    patch = bench._write(env / "Fix.ips", bench._ips_fix()[0])

    job = await bench._run(client, {"image_path": str(rom), "patch_path": str(patch)})

    assert job.status.value == "failed"
    assert "7z was killed (signal 9), most likely out of memory" in job.error_message
    assert "89%" not in job.error_message
    assert "\b" not in job.error_message
    assert sorted(p.name for p in rom.parent.iterdir()) == ["Big Patch Me.gba"]


# ----------------------------------------------------------------------
# romz: the max profile with a bounded dictionary and thread count
# ----------------------------------------------------------------------


def test_romz_bounds_the_max_profile_when_asked() -> None:
    from services.romz import _compress_flags

    assert _compress_flags("romz_7z", "max", dictionary=64 * MIB, threads=2) == [
        "-t7z", "-m0=lzma2", "-mx=9", "-md=64m", "-mfb=273", "-mmt=2",
    ]
    assert "-md=1536k" in _compress_flags("romz_7z", "max", dictionary=1536 * 1024)
    # Unasked, the profile is romz's reference one.
    assert _compress_flags("romz_7z", "max") == [
        "-t7z", "-m0=lzma2", "-mx=9", "-md=256m", "-mfb=273", "-mmt=on",
    ]


# ----------------------------------------------------------------------
# reading a ROM into one buffer
# ----------------------------------------------------------------------


def test_read_beneath_mutable_reads_the_file_into_one_bytearray(tmp_path: Path) -> None:
    from utils.path_utils import read_beneath_mutable

    root = os.path.realpath(tmp_path)
    data = _big_rom(4 * MIB)
    (tmp_path / "Game.gba").write_bytes(data)

    peak = 0
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        before = tracemalloc.get_traced_memory()[0]
        got = read_beneath_mutable(root, "Game.gba", max_bytes=8 * MIB)
        peak = tracemalloc.get_traced_memory()[1] - before
    finally:
        tracemalloc.stop()

    assert isinstance(got, bytearray) and got == data
    assert peak < 4 * MIB + MIB, peak  # the file once, not chunks and a join


def test_read_beneath_mutable_keeps_read_beneaths_refusals(tmp_path: Path) -> None:
    from utils.path_utils import read_beneath_mutable

    root = os.path.realpath(tmp_path)
    (tmp_path / "Game.gba").write_bytes(b"x" * 100)
    os.symlink(os.path.join(root, "Game.gba"), os.path.join(root, "Link.gba"))
    with pytest.raises(ValueError):
        read_beneath_mutable(root, "Game.gba", max_bytes=99)
    with pytest.raises(OSError):
        read_beneath_mutable(root, "Link.gba", max_bytes=1000)


def test_read_fd_capped_reads_on_past_a_size_that_grew(tmp_path: Path, monkeypatch) -> None:
    from utils.path_utils import read_fd_capped

    path = tmp_path / "rom.bin"
    path.write_bytes(b"a" * 10)
    with open(path, "rb") as handle, open(path, "ab") as grower:
        real_fstat = os.fstat

        def fstat_then_grow(fd):
            info = real_fstat(fd)
            grower.write(b"b" * 5)
            grower.flush()
            return info

        monkeypatch.setattr(os, "fstat", fstat_then_grow)
        data = read_fd_capped(handle.fileno(), 100, "rom.bin")
    assert data == b"a" * 10 + b"b" * 5


def test_read_fd_capped_refuses_a_file_that_grew_past_the_cap(tmp_path: Path, monkeypatch) -> None:
    from utils.path_utils import read_fd_capped

    path = tmp_path / "rom.bin"
    path.write_bytes(b"a" * 10)
    with open(path, "rb") as handle, open(path, "ab") as grower:
        real_fstat = os.fstat

        def fstat_then_grow(fd):
            info = real_fstat(fd)
            grower.write(b"b" * 5)
            grower.flush()
            return info

        monkeypatch.setattr(os, "fstat", fstat_then_grow)
        with pytest.raises(ValueError, match="rom.bin is larger than 12 bytes"):
            read_fd_capped(handle.fileno(), 12, "rom.bin")


# ----------------------------------------------------------------------
# the read-back, by size and SHA1
# ----------------------------------------------------------------------


_WRONG_7Z = """\
import os, sys
import py7zr
args = sys.argv[1:]
out, src = args[args.index("--") + 1:]
with open(src, "rb") as handle:
    data = bytearray(handle.read())
data[0] ^= 0xFF
with py7zr.SevenZipFile(out, "w") as archive:
    archive.writestr(bytes(data), os.path.basename(src))
"""


@pytest.mark.asyncio
async def test_an_archive_that_doesnt_read_back_as_the_patched_rom_isnt_kept(
    env: Path, client: httpx.AsyncClient, monkeypatch, tmp_path: Path,
) -> None:
    _stub_7z(tmp_path, monkeypatch, _WRONG_7Z)
    rom = bench._write(env / "roms" / "Game.gba", bench.ROM)
    patch = bench._write(env / "Fix.ips", bench._ips_fix()[0])

    job = await bench._run(client, {"image_path": str(rom), "patch_path": str(patch)})

    assert job.status.value == "failed"
    assert "didn't read back as the patched ROM" in job.error_message
    assert sorted(p.name for p in rom.parent.iterdir()) == ["Game.gba"]
