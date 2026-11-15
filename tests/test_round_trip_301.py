"""Round-trip check for the remaining tools and chains (#301).

Every tool with delete-source proves content equality before deleting:
maxcso, z3ds and JWUDTool decode the output to a hashing stream (no temp
file); nsz compares the SHA256 its own verify rebuilds; 7z/zip compare each
member's CRC and size with the Source; ``cso_to_chd`` compares the decoded
CSO with the CHD's data SHA1. Binaries are faked at the runner seam; the
containers are real files laid out per each format.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import struct
import zipfile
from pathlib import Path

import pytest
from services.tools import registry
from services.tools.base import ContentDigest, ContentDigestUnavailable

from .cso_fixtures import write_ciso, write_dax

IMAGE = "image_data_sha1"


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _image(size: int = 6 * 2048) -> bytes:
    """An ISO-ish image: a run of one byte (exercises an LZ4 match), then
    varied bytes. Sector-aligned, as maxcso requires."""
    body = bytes((i * 7 + i // 300) % 251 for i in range(size - 40))
    return b"\x00" * 40 + body


# ---------------------------------------------------------------------------
# maxcso: CSO v1/v2, ZSO and DAX decoded in Python to a hashing stream
# ---------------------------------------------------------------------------


def _cso():
    return registry.get("cso")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [
    "cso1", "cso1_shift_plain", "cso2_deflate", "cso2_lz4", "zso", "zso_shift", "dax",
    "dax_nc", "cso1_16k_short_last", "zso_16k_short_last",
])
async def test_maxcso_container_digest_is_the_sha1_of_the_image_it_decodes_to(
    tmp_path, kind,
):
    image = _image()
    out = tmp_path / "Game.cso"
    if kind.endswith("_16k_short_last"):
        # A larger block, and an image that is not a block multiple, so the
        # last block is short (maxcso pads it on disk).
        image = _image(10 * 2048)
        if kind.startswith("zso"):
            out = tmp_path / "Game.zso"
        write_ciso(
            out, image, block=16384,
            magic=b"ZISO" if kind.startswith("zso") else b"CISO",
        )
    elif kind == "cso1":
        write_ciso(out, image)
    elif kind == "cso1_shift_plain":
        write_ciso(out, image, shift=2, plain_blocks={1, 5})
    elif kind == "cso2_deflate":
        write_ciso(out, image, version=2, plain_blocks={2})
    elif kind == "cso2_lz4":
        write_ciso(out, image, version=2, lz4=True)
    elif kind.startswith("zso"):
        out = tmp_path / "Game.zso"
        write_ciso(
            out, image, magic=b"ZISO", shift=2 if kind == "zso_shift" else 0,
            plain_blocks={3},
        )
    else:
        out = tmp_path / "Game.dax"
        big = image * 3  # more than one 8 KiB frame
        image = big
        write_dax(out, big, nc_frames={1} if kind == "dax_nc" else ())

    digest = await _cso().content_digest(str(out))

    assert digest == ContentDigest(kind=IMAGE, value=_sha1(image))


@pytest.mark.asyncio
async def test_maxcso_iso_digest_is_the_file_sha1(tmp_path):
    iso = tmp_path / "Game.iso"
    iso.write_bytes(_image())
    assert await _cso().content_digest(str(iso)) == ContentDigest(
        kind=IMAGE, value=_sha1(iso.read_bytes()),
    )


@pytest.mark.asyncio
async def test_a_corrupt_cso_block_is_unavailable_or_differs(tmp_path):
    image = _image()
    out = tmp_path / "Game.cso"
    write_ciso(out, image)
    data = bytearray(out.read_bytes())
    data[-30] ^= 0xFF
    out.write_bytes(bytes(data))
    try:
        digest = await _cso().content_digest(str(out))
    except ContentDigestUnavailable:
        return
    assert digest.value != _sha1(image)


@pytest.mark.asyncio
async def test_a_truncated_cso_is_unavailable(tmp_path):
    out = tmp_path / "Game.cso"
    write_ciso(out, _image())
    out.write_bytes(out.read_bytes()[:-100])
    with pytest.raises(ContentDigestUnavailable):
        await _cso().content_digest(str(out))


@pytest.mark.asyncio
async def test_a_file_that_is_no_container_is_unavailable(tmp_path):
    out = tmp_path / "Game.cso"
    out.write_bytes(b"NOPE" + b"\0" * 60)
    with pytest.raises(ContentDigestUnavailable):
        await _cso().content_digest(str(out))


def test_lz4_decodes_a_long_literal_run_and_a_far_non_overlapping_match():
    from utils.cso_decode import lz4_block_decompress

    literals = bytes((i * 31) % 253 for i in range(300))
    # 300 literals (15 + 255 + 30), then a match of 20 at offset 260 (two
    # offset bytes, non-overlapping), then 5 trailing literals.
    block = bytes([0xFF, 255, 30]) + literals + (260).to_bytes(2, "little")
    block += bytes([1]) + bytes([0x50]) + b"tail!"
    # token 0xFF: literal 15+255+30, match 15+1 (+4 = 20); then 5 literals.
    expected = literals + literals[40:60] + b"tail!"
    assert lz4_block_decompress(block, len(expected)) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "value"), [
    ("block", 1024),          # below a sector
    ("block", 3 * 2048),      # not a power of two
    ("block", 0x80000),       # above maxcso's MAX_BLOCK_SIZE
    ("total", 6 * 2048 + 1),  # not sector-aligned
    ("shift", 40),            # an index shift past 31
])
async def test_cso_headers_maxcso_would_refuse_are_unavailable(tmp_path, field, value):
    out = tmp_path / "Game.cso"
    write_ciso(out, _image())
    data = bytearray(out.read_bytes())
    offset, fmt = {"block": (16, "<I"), "total": (8, "<Q"), "shift": (21, "<B")}[field]
    struct.pack_into(fmt, data, offset, value)
    out.write_bytes(bytes(data))
    with pytest.raises(ContentDigestUnavailable):
        await _cso().content_digest(str(out))


@pytest.mark.parametrize("mode", [
    "cso_compress", "cso2_compress", "zso_compress", "dax_compress",
])
def test_every_maxcso_compress_mode_declares_round_trip(mode):
    assert registry.mode_supports_round_trip(mode)


# ---------------------------------------------------------------------------
# End to end: POST /jobs -> plan_job -> job_manager -> delete decision.
# Each converter is stubbed to write a real container built by the test, and
# its container check passes; the Round-trip itself runs for real.
# ---------------------------------------------------------------------------

_STUBBED_TOOLS = ("cso", "z3ds", "jwud", "nsz", "romz", "chdman")


@pytest.fixture(name="e2e")
def _e2e(tmp_path, monkeypatch, db_path, reset_db_engine):
    from services import db as _db

    from app.routes import convert as convert_routes

    _db.init_engine(db_path)
    for name, value in (
        ("chd_volumes", str(tmp_path)), ("data_mount_root", str(tmp_path)),
        ("temp_dir", str(tmp_path / "temp")), ("max_queue_depth", 0),
    ):
        monkeypatch.setattr(convert_routes.settings, name, value)

    state: dict = {"tmp_path": tmp_path, "routes": convert_routes, "writes": []}

    async def fake_convert(input_path, output_path, mode="", *, compression=None,
                           cancel_event=None):
        state["writes"].append((input_path, output_path, mode))
        state["writer"](Path(input_path), Path(output_path))
        yield {"progress": 100, "message": "done"}

    async def container_check_passes(path, *, cancel_event=None):
        return {"valid": True, "message": "ok"}

    for tool_id in _STUBBED_TOOLS:
        service = registry.get(tool_id)._service
        monkeypatch.setattr(service, "convert", fake_convert)
        monkeypatch.setattr(service, "verify", container_check_passes)
    yield state
    reset_db_engine()


async def _run(e2e, source, mode, **request_fields):
    from app.models import ConversionMode, DuplicateAction, JobCreateRequest

    routes = e2e["routes"]
    request = JobCreateRequest(
        file_path=str(source),
        mode=ConversionMode(mode),
        duplicate_action=DuplicateAction.SKIP,
        delete_on_verify=True,
        **request_fields,
    )
    job = await routes.create_job(request)
    await routes.job_manager._process_job(job.id)
    return job


def _assert_round_tripped(job, *sources):
    assert job.status.value == "completed", job.error_message
    assert job.check_passed == "round_tripped"
    for source in sources:
        assert not Path(source).exists(), source


def _assert_kept(job, *sources, reason="content differs"):
    assert job.status.value == "failed"
    assert reason in job.error_message, job.error_message
    assert "Source kept" in job.error_message or "Source is kept" in job.error_message
    assert job.check_passed == "intact"
    for source in sources:
        assert Path(source).exists(), source


def _binary(env_var: str, default: str, name: str) -> str | None:
    """The real binary for an opt-in test, or None to skip it."""
    import shutil

    for candidate in (os.environ.get(env_var), default):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return shutil.which(name)


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "magic", "version"), [
    ("cso_compress", b"CISO", 1),
    ("cso2_compress", b"CISO", 2),
    ("zso_compress", b"ZISO", 1),
])
async def test_maxcso_job_with_a_matching_container_deletes_the_iso(
    e2e, mode, magic, version,
):
    iso = e2e["tmp_path"] / "Game.iso"
    iso.write_bytes(_image())
    e2e["writer"] = lambda src, out: write_ciso(
        out, src.read_bytes(), magic=magic, version=version,
    )

    job = await _run(e2e, iso, mode)

    _assert_round_tripped(job, iso)


@pytest.mark.asyncio
async def test_maxcso_dax_job_with_a_matching_container_deletes_the_iso(e2e):
    iso = e2e["tmp_path"] / "Game.iso"
    iso.write_bytes(_image() * 3)
    e2e["writer"] = lambda src, out: write_dax(out, src.read_bytes())

    _assert_round_tripped(await _run(e2e, iso, "dax_compress"), iso)


@pytest.mark.asyncio
async def test_maxcso_job_whose_container_holds_another_image_keeps_the_iso(e2e):
    iso = e2e["tmp_path"] / "Game.iso"
    iso.write_bytes(_image())
    e2e["writer"] = lambda src, out: write_ciso(out, src.read_bytes()[:-1] + b"!")

    _assert_kept(await _run(e2e, iso, "cso_compress"), iso)


@pytest.mark.asyncio
async def test_maxcso_container_only_skips_the_decode(e2e):
    iso = e2e["tmp_path"] / "Game.iso"
    iso.write_bytes(_image())
    e2e["writer"] = lambda src, out: out.write_bytes(b"not even a container")

    job = await _run(e2e, iso, "cso_compress", container_only=True)

    assert job.status.value == "completed", job.error_message
    assert job.check_passed == "intact" and not iso.exists()


@pytest.mark.asyncio
async def test_cancel_while_decoding_keeps_the_iso_without_a_verdict(e2e, monkeypatch):
    """Cancel lands mid-decode: the decoder stops and the job is cancelled,
    with no verdict and the Source in place."""
    import threading

    from utils.delete_plan import build_delete_snapshot

    from app.models import ConversionMode

    iso = e2e["tmp_path"] / "Game.iso"
    iso.write_bytes(_image())
    e2e["writer"] = lambda src, out: write_ciso(out, src.read_bytes())
    manager = e2e["routes"].job_manager
    loop = asyncio.get_running_loop()
    stopped = threading.Event()

    def endless(path, stop=None):
        loop.call_soon_threadsafe(manager._cancel_events[job.id].set)
        try:
            while not stop.is_set():
                yield b"\0" * 4096
        finally:
            stopped.set()

    monkeypatch.setattr("services.tools.maxcso.iter_decoded", endless)
    job = await manager.create_job(
        str(iso), ConversionMode.CSO_COMPRESS,
        output_path=str(iso.with_suffix(".cso")),
        delete_on_verify=True, delete_snapshot=build_delete_snapshot(str(iso)),
    )
    await manager._process_job(job.id)

    assert job.status.value == "cancelled"
    assert iso.exists() and job.error_message is None
    assert job.check_passed == "intact"
    assert await loop.run_in_executor(None, stopped.wait, 5)


# ---------------------------------------------------------------------------
# z3ds: the zstd payload piped through a decoder, stdout hashed
# ---------------------------------------------------------------------------


def _z3ds():
    return registry.get("z3ds")


def write_z3ds(path: Path, payload: bytes, *, metadata: bytes = b"meta" * 8,
               uncompressed: int = 0) -> None:
    """A Z3DS container: 0x20-byte header, metadata, then ``payload``."""
    header = struct.pack(
        "<4s4sBBHIQQ", b"Z3DS", b"NCSD", 1, 0, 0x20, len(metadata),
        len(payload), uncompressed,
    )
    path.write_bytes(header + metadata + payload)


# A stand-in decoder: what a test's "compressed" payload is, it writes back.
_CAT = [
    __import__("sys").executable, "-c",
    "import shutil, sys; shutil.copyfileobj(sys.stdin.buffer, sys.stdout.buffer)",
]


@pytest.fixture(name="z3ds_decoder")
def _z3ds_decoder(monkeypatch):
    monkeypatch.setattr(_z3ds()._service, "decode_command", lambda: list(_CAT))


@pytest.mark.asyncio
async def test_z3ds_rom_digest_is_the_file_sha1(tmp_path):
    rom = tmp_path / "Game.3ds"
    rom.write_bytes(b"cartridge" * 999)
    assert await _z3ds().content_digest(str(rom)) == ContentDigest(
        kind=IMAGE, value=_sha1(rom.read_bytes()),
    )


@pytest.mark.asyncio
async def test_z3ds_container_digest_hashes_the_decoded_payload(tmp_path, z3ds_decoder):
    rom = b"cartridge" * 999
    out = tmp_path / "Game.z3ds"
    write_z3ds(out, rom)
    assert await _z3ds().content_digest(str(out)) == ContentDigest(
        kind=IMAGE, value=_sha1(rom),
    )


@pytest.mark.asyncio
async def test_z3ds_decoder_failure_is_unavailable(tmp_path, monkeypatch):
    out = tmp_path / "Game.z3ds"
    write_z3ds(out, b"payload")
    monkeypatch.setattr(
        _z3ds()._service, "decode_command",
        lambda: [__import__("sys").executable, "-c", "import sys; sys.exit(1)"],
    )
    with pytest.raises(Exception):  # noqa: B017 - any failure is "not proven"
        await _z3ds().content_digest(str(out))


@pytest.mark.asyncio
async def test_z3ds_without_zstd_is_unavailable(tmp_path, monkeypatch):
    out = tmp_path / "Game.z3ds"
    write_z3ds(out, b"payload")
    monkeypatch.setattr(_z3ds()._service, "decode_command", lambda: None)
    with pytest.raises(ContentDigestUnavailable, match="zstd"):
        await _z3ds().content_digest(str(out))


def test_z3ds_compress_declares_round_trip():
    assert registry.mode_supports_round_trip("z3ds_compress")


@pytest.mark.asyncio
async def test_z3ds_job_with_a_matching_container_deletes_the_rom(e2e, z3ds_decoder):
    rom = e2e["tmp_path"] / "Game.3ds"
    rom.write_bytes(b"cartridge" * 999)
    e2e["writer"] = lambda src, out: write_z3ds(out, src.read_bytes())

    _assert_round_tripped(await _run(e2e, rom, "z3ds_compress"), rom)


@pytest.mark.asyncio
async def test_z3ds_job_whose_container_holds_another_rom_keeps_it(e2e, z3ds_decoder):
    rom = e2e["tmp_path"] / "Game.cci"
    rom.write_bytes(b"cartridge" * 999)
    e2e["writer"] = lambda src, out: write_z3ds(out, src.read_bytes()[1:])

    _assert_kept(await _run(e2e, rom, "z3ds_compress"), rom)


_ZSTD = _binary("ZSTD_PATH", "/usr/bin/zstd", "zstd")


@pytest.mark.skipif(_ZSTD is None, reason="zstd is not installed")
@pytest.mark.asyncio
async def test_real_zstd_decodes_a_multi_frame_payload(tmp_path):
    """Opt-in: the real decoder the tool runs, over a payload of several zstd
    frames and a skippable frame, as a seekable-zstd Z3DS payload is laid out."""
    zstandard = pytest.importorskip("zstandard")
    rom = os.urandom(300_000) + b"\0" * 200_000
    c = zstandard.ZstdCompressor()
    payload = c.compress(rom[:250_000]) + c.compress(rom[250_000:])
    payload += struct.pack("<II", 0x184D2A5E, 8) + b"seektabl"
    out = tmp_path / "Game.zcci"
    write_z3ds(out, payload, uncompressed=len(rom))

    digest = await _z3ds().content_digest(str(out))

    assert digest.value == _sha1(rom)


# ---------------------------------------------------------------------------
# JWUDTool: the WUX sector table decoded in Python to a hashing stream
# ---------------------------------------------------------------------------

_WUX_SECTOR = 0x8000


def _jwud():
    return registry.get("jwud")


@pytest.fixture(name="small_wud")
def _small_wud(monkeypatch):
    """A Wii U image is always 25 GB; shrink the pinned size for tests."""
    from services import jwudtool

    size = 5 * _WUX_SECTOR + 1234
    monkeypatch.setattr(jwudtool, "WUD_IMAGE_SIZE", size)
    monkeypatch.setattr(jwudtool, "WUD_SPLIT_PART_SIZE", 2 * _WUX_SECTOR)
    return size


def _wud_image(size: int) -> bytes:
    # Sectors 1 and 3 equal (deduplicated in the WUX), the rest distinct.
    same = b"\x5a" * _WUX_SECTOR
    sectors = [bytes([i]) * _WUX_SECTOR for i in range(6)]
    sectors[1] = sectors[3] = same
    return b"".join(sectors)[:size]


def write_wux(path: Path, image: bytes) -> None:
    """A WUX laid out as JNUSLib writes it: header, one u32 per sector, the
    deduplicated sector array at the next sector boundary."""
    count = (len(image) + _WUX_SECTOR - 1) // _WUX_SECTOR
    unique: dict[bytes, int] = {}
    table = []
    for i in range(count):
        sector = image[i * _WUX_SECTOR:(i + 1) * _WUX_SECTOR].ljust(_WUX_SECTOR, b"\0")
        table.append(unique.setdefault(sector, len(unique)))
    header = struct.pack("<4sIIIQ", b"WUX0", 0x1099D02E, _WUX_SECTOR, 0, len(image))
    header = header.ljust(0x20, b"\0")
    start = 0x20 + count * 4 + _WUX_SECTOR - 1
    start -= start % _WUX_SECTOR
    head = header + struct.pack(f"<{count}I", *table)
    path.write_bytes(head.ljust(start, b"\0") + b"".join(unique))


@pytest.mark.asyncio
async def test_wux_digest_is_the_sha1_of_the_image_it_decodes_to(tmp_path, small_wud):
    image = _wud_image(small_wud)
    out = tmp_path / "game.wux"
    write_wux(out, image)

    assert await _jwud().content_digest(str(out)) == ContentDigest(
        kind=IMAGE, value=_sha1(image),
    )


@pytest.mark.asyncio
async def test_wud_digest_covers_every_part_of_a_split_set(tmp_path, small_wud):
    image = _wud_image(small_wud)
    part = 2 * _WUX_SECTOR
    for i in range(3):
        (tmp_path / f"game_part{i + 1}.wud").write_bytes(image[i * part:(i + 1) * part])

    digest = await _jwud().content_digest(str(tmp_path / "game_part1.wud"))

    assert digest == ContentDigest(kind=IMAGE, value=_sha1(image))


@pytest.mark.asyncio
async def test_split_wud_parts_are_found_off_the_event_loop(tmp_path, small_wud, monkeypatch):
    """Finding the sibling parts stats the Source's volume, so it must not run
    on the event loop, where a dead mount would freeze every job."""
    import threading

    image = _wud_image(small_wud)
    part = 2 * _WUX_SECTOR
    for i in range(3):
        (tmp_path / f"game_part{i + 1}.wud").write_bytes(image[i * part:(i + 1) * part])
    tool = _jwud()
    threads: list[str] = []
    real = tool.source_companions

    def recording(path):
        threads.append(threading.current_thread().name)
        return real(path)

    monkeypatch.setattr(tool, "source_companions", recording)
    loop_thread = threading.current_thread().name

    digest = await tool.content_digest(str(tmp_path / "game_part1.wud"))

    assert digest.value == _sha1(image)
    assert threads and loop_thread not in threads


@pytest.mark.asyncio
async def test_a_blocked_stat_of_the_wud_parts_still_meets_the_deadline(
    tmp_path, small_wud, monkeypatch,
):
    """A sibling-part stat that never returns (a hung mount) must not stop the
    deadline: it runs off the loop, so wait_for fires on time."""
    import threading
    import time

    (tmp_path / "game_part1.wud").write_bytes(b"part one")
    tool = _jwud()
    release = threading.Event()

    def hung_stat(_path):
        release.wait(30)
        return []

    monkeypatch.setattr(tool, "source_companions", hung_stat)
    started = time.monotonic()
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                tool.content_digest(str(tmp_path / "game_part1.wud")), timeout=0.5,
            )
        assert time.monotonic() - started < 5
    finally:
        release.set()


@pytest.mark.asyncio
async def test_a_wux_whose_sector_array_is_cut_short_is_unavailable(tmp_path, small_wud):
    out = tmp_path / "game.wux"
    write_wux(out, _wud_image(small_wud))
    out.write_bytes(out.read_bytes()[:-_WUX_SECTOR - 100])
    with pytest.raises(ContentDigestUnavailable):
        await _jwud().content_digest(str(out))


def test_jwud_compress_declares_round_trip():
    assert registry.mode_supports_round_trip("jwud_compress")


@pytest.mark.asyncio
async def test_jwud_job_with_a_matching_wux_deletes_the_whole_split_set(e2e, small_wud):
    image = _wud_image(small_wud)
    part = 2 * _WUX_SECTOR
    parts = []
    for i in range(3):
        p = e2e["tmp_path"] / f"game_part{i + 1}.wud"
        p.write_bytes(image[i * part:(i + 1) * part])
        parts.append(p)
    e2e["writer"] = lambda src, out: write_wux(out, image)

    _assert_round_tripped(await _run(e2e, parts[0], "jwud_compress"), *parts)


@pytest.mark.asyncio
async def test_jwud_job_whose_wux_holds_another_image_keeps_the_wud(e2e, small_wud):
    image = _wud_image(small_wud)
    wud = e2e["tmp_path"] / "game.wud"
    wud.write_bytes(image)
    e2e["writer"] = lambda src, out: write_wux(out, image[:-1] + b"?")

    _assert_kept(await _run(e2e, wud, "jwud_compress"), wud)


# ---------------------------------------------------------------------------
# nsz: the NSP SHA256 that nsz -V rebuilds; an XCZ is decompressed and hashed
# ---------------------------------------------------------------------------

SWITCH = "switch_image_sha256"


def _nsz():
    return registry.get("nsz")


class FakeNsz:
    """``nsz -V`` prints the rebuilt NSP's SHA256; ``nsz -D`` writes the XCI."""

    def __init__(self) -> None:
        self.verify_stdout = b""
        self.rc = 0
        self.decompressed: bytes | None = None
        self.calls: list[list[str]] = []
        self.homes: list[str] = []
        self.on_call = None

    async def run_capture(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        self.homes.append((kwargs.get("env") or {}).get("HOME", ""))
        if self.on_call is not None:
            return self.on_call(cmd)
        if "-V" in cmd:
            return self.rc, self.verify_stdout, b""
        if "-D" in cmd:
            work = Path(cmd[cmd.index("-o") + 1])
            src = Path(cmd[-1])
            if self.decompressed is not None:
                (work / (src.stem + ".xci")).write_bytes(self.decompressed)
            return self.rc, b"", b""
        raise AssertionError(cmd)


@pytest.fixture(name="nsz_fake")
def _nsz_fake(monkeypatch, tmp_path):
    from config import settings

    keys = tmp_path / "prod.keys"
    keys.write_text("header_key = 00\n")
    service = _nsz()._service
    fake = FakeNsz()
    monkeypatch.setattr(service._runner, "run_capture", fake.run_capture)
    monkeypatch.setattr(service, "resolved_keys_file", lambda: str(keys))
    monkeypatch.setattr(settings, "temp_dir", str(tmp_path / "scratch"))
    return fake


def _verify_stdout(sha256: str) -> bytes:
    """``nsz -V`` output in the shape 4.6.1 and 5.0.0 both print."""
    return (
        "[VERIFY NSZ] Game.nsz\n"
        "[NCA HASH] " + "ab" * 32 + "\n"
        "[VERIFIED] Game/0123.ncz\n"
        f"[NSP SHA256] {sha256}\n"
    ).encode()


@pytest.mark.asyncio
async def test_nsp_digest_is_the_file_sha256(tmp_path):
    nsp = tmp_path / "Game.nsp"
    nsp.write_bytes(b"PFS0" + b"switch" * 500)
    assert await _nsz().content_digest(str(nsp)) == ContentDigest(
        kind=SWITCH, value=hashlib.sha256(nsp.read_bytes()).hexdigest(),
    )


@pytest.mark.asyncio
async def test_nsz_digest_is_the_nsp_sha256_nsz_verify_rebuilds(tmp_path, nsz_fake):
    out = tmp_path / "Game.nsz"
    out.write_bytes(b"PFS0 ncz")
    nsz_fake.verify_stdout = _verify_stdout("CD" * 32)

    digest = await _nsz().content_digest(str(out))

    assert digest == ContentDigest(kind=SWITCH, value="cd" * 32)
    assert nsz_fake.calls[0][-2:] == ["-V", str(out)]
    assert nsz_fake.homes[0]  # run with the private keys HOME


@pytest.mark.asyncio
@pytest.mark.parametrize(("stdout", "rc"), [
    (b"[VERIFY NSZ] Game.nsz\n", 0),        # no hash line
    (_verify_stdout("cd" * 32), 1),         # a failed verify
    (_verify_stdout("cd" * 32) * 2, 0),     # two hashes: ambiguous
])
async def test_an_nsz_verify_without_one_clean_hash_is_unavailable(
    tmp_path, nsz_fake, stdout, rc,
):
    out = tmp_path / "Game.nsz"
    out.write_bytes(b"x")
    nsz_fake.verify_stdout, nsz_fake.rc = stdout, rc
    with pytest.raises(ContentDigestUnavailable):
        await _nsz().content_digest(str(out))


@pytest.mark.asyncio
async def test_xcz_digest_decompresses_to_scratch_and_removes_it(tmp_path, nsz_fake):
    xci = b"HEAD" * 4000
    out = tmp_path / "Game.xcz"
    out.write_bytes(b"xcz")
    nsz_fake.decompressed = xci

    digest = await _nsz().content_digest(str(out))

    assert digest == ContentDigest(kind=SWITCH, value=hashlib.sha256(xci).hexdigest())
    assert "-D" in nsz_fake.calls[0]
    assert os.listdir(tmp_path / "scratch") == []


@pytest.mark.asyncio
async def test_the_xcz_digest_makes_no_blocking_stat_on_the_event_loop(
    tmp_path, nsz_fake, monkeypatch,
):
    """Every filesystem call on the XCZ path runs off the loop: a stat that
    hangs on the loop thread would stop the deadline from firing."""
    import threading
    import time

    from services import nsz as nsz_module

    loop_thread = threading.get_ident()
    real_isfile = os.path.isfile
    release = threading.Event()

    def isfile(path):
        if threading.get_ident() == loop_thread:
            release.wait(30)  # a hung mount, on the loop
        return real_isfile(path)

    monkeypatch.setattr(nsz_module.os.path, "isfile", isfile)
    out = tmp_path / "Game.xcz"
    out.write_bytes(b"xcz")
    nsz_fake.decompressed = b"HEAD" * 4000
    started = time.monotonic()
    try:
        digest = await asyncio.wait_for(_nsz().content_digest(str(out)), timeout=5)
        assert digest.value == hashlib.sha256(b"HEAD" * 4000).hexdigest()
        assert time.monotonic() - started < 5
    finally:
        release.set()


@pytest.mark.asyncio
async def test_an_xcz_that_decompresses_to_nothing_is_unavailable(tmp_path, nsz_fake):
    out = tmp_path / "Game.xcz"
    out.write_bytes(b"xcz")
    with pytest.raises(ContentDigestUnavailable):
        await _nsz().content_digest(str(out))
    assert os.listdir(tmp_path / "scratch") == []


@pytest.mark.asyncio
async def test_an_xcz_with_no_scratch_room_is_unavailable_without_decompressing(
    tmp_path, nsz_fake, monkeypatch,
):
    from services import disk

    def no_room(*_a, **_k):
        raise disk.InsufficientDiskSpace("not enough room")

    monkeypatch.setattr(disk, "ensure_headroom", no_room)
    out = tmp_path / "Game.xcz"
    out.write_bytes(b"xcz")
    with pytest.raises(ContentDigestUnavailable, match="space"):
        await _nsz().content_digest(str(out))
    assert nsz_fake.calls == []


@pytest.mark.asyncio
async def test_the_container_check_hash_is_reused_so_nsz_verify_runs_once(
    tmp_path, nsz_fake,
):
    out = tmp_path / "Game.nsz"
    out.write_bytes(b"PFS0 ncz")
    nsz_fake.verify_stdout = _verify_stdout("cd" * 32)

    check = await _nsz().verify(str(out))
    digest = await _nsz().content_digest(str(out))

    assert check["valid"] is True
    assert digest == ContentDigest(kind=SWITCH, value="cd" * 32)
    assert sum("-V" in c for c in nsz_fake.calls) == 1


@pytest.mark.asyncio
async def test_a_changed_nsz_is_not_given_the_earlier_hash(tmp_path, nsz_fake):
    out = tmp_path / "Game.nsz"
    out.write_bytes(b"PFS0 ncz")
    nsz_fake.verify_stdout = _verify_stdout("cd" * 32)
    await _nsz().verify(str(out))
    out.write_bytes(b"PFS0 another ncz")
    nsz_fake.verify_stdout = _verify_stdout("ef" * 32)

    digest = await _nsz().content_digest(str(out))

    assert digest.value == "ef" * 32
    assert sum("-V" in c for c in nsz_fake.calls) == 2


def test_xcz_scratch_size_never_trusts_a_small_cart_header(tmp_path):
    from services.nsz import xcz_decoded_size

    out = tmp_path / "Game.xcz"
    head = bytearray(0x200)
    head[0x100:0x104] = b"HEAD"
    head[0x118:0x11C] = (0).to_bytes(4, "little")  # claims one page
    out.write_bytes(bytes(head) + b"\0" * 10_000)
    assert xcz_decoded_size(str(out)) >= 2 * out.stat().st_size


@pytest.mark.asyncio
async def test_an_xci_nsz_does_not_rebuild_keeps_the_source_and_says_why(e2e, nsz_fake):
    xci = e2e["tmp_path"] / "Game.xci"
    xci.write_bytes(b"HEAD" * 4000)
    e2e["writer"] = lambda src, out: out.write_bytes(b"xcz container")
    nsz_fake.decompressed = b"HEAD" * 3999 + b"\0\0\0\0"

    job = await _run(e2e, xci, "nsz_compress")

    _assert_kept(job, xci, reason="nsz did not rebuild this XCI byte-for-byte")
    assert "so the Source is kept" in job.error_message


def test_nsz_compress_declares_round_trip():
    assert registry.mode_supports_round_trip("nsz_compress")


@pytest.mark.asyncio
async def test_nsz_job_whose_rebuilt_nsp_matches_deletes_the_nsp(e2e, nsz_fake):
    nsp = e2e["tmp_path"] / "Game.nsp"
    nsp.write_bytes(b"PFS0" + b"switch" * 500)
    e2e["writer"] = lambda src, out: out.write_bytes(b"nsz container")
    nsz_fake.verify_stdout = _verify_stdout(hashlib.sha256(nsp.read_bytes()).hexdigest())

    _assert_round_tripped(await _run(e2e, nsp, "nsz_compress"), nsp)


@pytest.mark.asyncio
async def test_nsz_job_whose_rebuilt_nsp_differs_keeps_the_nsp(e2e, nsz_fake):
    """What an NSP whose NDV0 delta fragments nsz dropped looks like."""
    nsp = e2e["tmp_path"] / "Game.nsp"
    nsp.write_bytes(b"PFS0" + b"switch" * 500)
    e2e["writer"] = lambda src, out: out.write_bytes(b"nsz container")
    nsz_fake.verify_stdout = _verify_stdout("00" * 32)

    _assert_kept(await _run(e2e, nsp, "nsz_compress"), nsp)


@pytest.mark.asyncio
async def test_nsz_job_on_an_xci_compares_the_decompressed_xci(e2e, nsz_fake):
    xci = e2e["tmp_path"] / "Game.xci"
    xci.write_bytes(b"HEAD" * 4000)
    e2e["writer"] = lambda src, out: out.write_bytes(b"xcz container")
    nsz_fake.decompressed = xci.read_bytes()

    _assert_round_tripped(await _run(e2e, xci, "nsz_compress"), xci)


# ---------------------------------------------------------------------------
# romz (7z / zip): each member's CRC and size against the Source file
# ---------------------------------------------------------------------------

MEMBERS = "rom_members_crc32"


def _romz():
    return registry.get("romz")


def _zip(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)


def _7z(path: Path, members: dict[str, bytes]) -> None:
    py7zr = pytest.importorskip("py7zr")
    with py7zr.SevenZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(data, name)


@pytest.mark.asyncio
@pytest.mark.parametrize("pack", [_zip, _7z])
async def test_an_archive_holding_the_rom_has_the_rom_digest(tmp_path, pack):
    rom = tmp_path / "Game.gba"
    rom.write_bytes(b"cartridge" * 777)
    archive = tmp_path / f"Game.gba{'.zip' if pack is _zip else '.7z'}"
    pack(archive, {"Game.gba": rom.read_bytes()})

    source = await _romz().content_digest(str(rom))
    output = await _romz().content_digest(str(archive))

    assert source.kind == MEMBERS
    assert source == output


@pytest.mark.asyncio
@pytest.mark.parametrize("members", [
    {"Game.gba": b"cartridge" * 776},                          # other bytes
    {"Renamed.gba": b"cartridge" * 777},                       # renamed
    {"Game.gba": b"cartridge" * 777, "extra.txt": b"x"},       # extra member
    {},                                                        # missing
], ids=["crc", "renamed", "extra", "missing"])
@pytest.mark.parametrize("pack", [_zip, _7z])
async def test_an_archive_with_another_member_set_differs(tmp_path, pack, members):
    rom = tmp_path / "Game.gba"
    rom.write_bytes(b"cartridge" * 777)
    archive = tmp_path / f"Game.gba{'.zip' if pack is _zip else '.7z'}"
    pack(archive, members)

    source = await _romz().content_digest(str(rom))
    output = await _romz().content_digest(str(archive))

    assert source != output


def _zip_without_utf8_flag(path: Path, name: str, data: bytes) -> None:
    """A zip whose member name is UTF-8 bytes with no UTF-8 flag, the way
    p7zip writes it. zipfile then reads the name as cp437."""
    raw = name.encode("utf-8")
    placeholder = "x" * len(raw)
    _zip(path, {placeholder: data})
    path.write_bytes(path.read_bytes().replace(placeholder.encode(), raw))


@pytest.mark.asyncio
async def test_a_non_ascii_rom_name_matches_its_archived_member(tmp_path):
    import unicodedata

    name = "Pokémon Ruby (Español).gba"
    rom = tmp_path / unicodedata.normalize("NFD", name)  # as macOS may store it
    rom.write_bytes(b"cartridge" * 777)
    flagless = tmp_path / "a.zip"
    _zip_without_utf8_flag(flagless, unicodedata.normalize("NFC", name), rom.read_bytes())
    flagged = tmp_path / "b.zip"
    _zip(flagged, {unicodedata.normalize("NFC", name): rom.read_bytes()})

    source = await _romz().content_digest(str(rom))
    assert await _romz().content_digest(str(flagless)) == source
    assert await _romz().content_digest(str(flagged)) == source


def test_romz_compress_modes_declare_round_trip():
    assert registry.mode_supports_round_trip("romz_7z")
    assert registry.mode_supports_round_trip("romz_zip")


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "pack"), [("romz_zip", _zip), ("romz_7z", _7z)])
async def test_romz_job_whose_archive_holds_the_rom_deletes_it(e2e, mode, pack):
    rom = e2e["tmp_path"] / "Game.gba"
    rom.write_bytes(b"cartridge" * 777)
    e2e["writer"] = lambda src, out: pack(out, {src.name: src.read_bytes()})

    _assert_round_tripped(await _run(e2e, rom, mode), rom)


@pytest.mark.asyncio
async def test_romz_job_whose_archive_has_an_extra_member_keeps_the_rom(e2e):
    rom = e2e["tmp_path"] / "Game.gba"
    rom.write_bytes(b"cartridge" * 777)
    e2e["writer"] = lambda src, out: _zip(
        out, {src.name: src.read_bytes(), "readme.txt": b"hi"},
    )

    _assert_kept(await _run(e2e, rom, "romz_zip"), rom)


# ---------------------------------------------------------------------------
# Chains: the first step's decoded Source against the final output
# ---------------------------------------------------------------------------


def _chdman_info(data_sha1: str) -> bytes:
    return (
        "chdman - MAME Compressed Hunks of Data (CHD) manager 0.285\n"
        "File Version: 5\nLogical size: 12,288 bytes\n"
        f"SHA1:         {'11' * 20}\nData SHA1:    {data_sha1}\n"
        "Metadata:     Tag='DVD '  Index=0  Length=1 bytes\n"
    ).encode()


@pytest.fixture(name="chdman_info")
def _chdman_info_fixture(monkeypatch):
    state = {"data_sha1": "00" * 20, "calls": []}

    async def fake_capture(cmd, **kwargs):
        state["calls"].append(list(cmd))
        if "info" in cmd:
            return 0, _chdman_info(state["data_sha1"]), b""
        return 0, b"", b""

    monkeypatch.setattr(
        registry.get("chdman")._service._runner, "run_capture", fake_capture,
    )
    return state


def test_cso_to_chd_declares_round_trip_and_nkit_to_rvz_still_cannot_delete():
    assert registry.mode_supports_round_trip("cso_to_chd")
    assert not registry.spec("nkit_to_rvz").supports_delete_on_verify


@pytest.mark.asyncio
async def test_chain_digests_its_source_with_the_first_steps_tool(tmp_path, chdman_info):
    image = _image()
    cso = tmp_path / "Game.cso"
    write_ciso(cso, image)
    chain = registry.get("chain")

    assert await chain.content_digest(str(cso)) == ContentDigest(
        kind=IMAGE, value=_sha1(image),
    )
    assert chdman_info["calls"] == []  # the Source never went to chdman


def _chain_writer(image: bytes):
    """Step 1 (cso_decompress) writes the ISO, step 2 (createdvd) the CHD."""
    def write(_src: Path, out: Path) -> None:
        out.write_bytes(image if out.suffix == ".iso" else b"MComprHD fake")
    return write


@pytest.mark.asyncio
async def test_cso_to_chd_with_the_same_disc_deletes_the_cso(e2e, chdman_info):
    image = _image()
    cso = e2e["tmp_path"] / "Game.cso"
    write_ciso(cso, image)
    e2e["writer"] = _chain_writer(image)
    chdman_info["data_sha1"] = _sha1(image)

    job = await _run(e2e, cso, "cso_to_chd")

    _assert_round_tripped(job, cso)
    assert [m for _i, _o, m in e2e["writes"]] == ["cso_decompress", "createdvd"]


@pytest.mark.asyncio
async def test_cso_to_chd_whose_chd_holds_another_disc_keeps_the_cso(e2e, chdman_info):
    image = _image()
    cso = e2e["tmp_path"] / "Game.zso"
    write_ciso(cso, image, magic=b"ZISO")
    e2e["writer"] = _chain_writer(image)
    chdman_info["data_sha1"] = _sha1(image + b"x")

    _assert_kept(await _run(e2e, cso, "cso_to_chd"), cso)


# ---------------------------------------------------------------------------
# Modes that must keep refusing delete-source
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["folder_to_iso", "xiso_rewrite"])
def test_ps3_folder_to_iso_and_the_xiso_trim_offer_no_delete_source(mode):
    spec = registry.spec(mode)
    assert not spec.supports_delete_on_verify
    assert not spec.supports_round_trip


@pytest.mark.parametrize("mode", ["folder_to_iso", "xiso_rewrite"])
def test_the_route_refuses_delete_source_for_them(mode):
    from fastapi import HTTPException

    from app.routes import convert as convert_routes

    with pytest.raises(HTTPException) as err:
        convert_routes._validate_delete_on_verify(registry.spec(mode), True)
    assert err.value.status_code == 400


def test_every_mode_with_delete_source_round_trips():
    """After #301 no mode deletes its Source on the container check alone."""
    left = {
        s.mode for s in registry.mode_specs()
        if s.supports_delete_on_verify and not s.supports_round_trip
    }
    assert left == set()


# ---------------------------------------------------------------------------
# chdman Create HD, Create raw and Copy: the CHD's data SHA1; Create LD has
# no comparable digest, so it offers no delete-source
# ---------------------------------------------------------------------------


def _info(data_sha1: str, tag: str | None) -> bytes:
    lines = [
        "chdman - MAME Compressed Hunks of Data (CHD) manager 0.285",
        "File Version: 5", "Logical size: 12,288 bytes",
        f"SHA1:         {'11' * 20}", f"Data SHA1:    {data_sha1}",
    ]
    if tag is not None:
        lines.append(f"Metadata:     Tag='{tag}'  Index=0  Length=1 bytes")
    return ("\n".join(lines) + "\n").encode()


@pytest.fixture(name="chd_infos")
def _chd_infos(monkeypatch):
    """``chdman info`` per CHD path: ``{path: (data_sha1, tag)}``."""
    infos: dict[str, tuple[str, str | None]] = {}

    async def fake_capture(cmd, **kwargs):
        if "info" in cmd:
            path = cmd[cmd.index("-i") + 1]
            sha1, tag = infos[path]
            return 0, _info(sha1, tag), b""
        return 0, b"", b""

    monkeypatch.setattr(
        registry.get("chdman")._service._runner, "run_capture", fake_capture,
    )
    return infos


@pytest.mark.parametrize("mode", ["createhd", "createraw", "copy"])
def test_chdman_hd_raw_and_copy_declare_round_trip(mode):
    assert registry.mode_supports_round_trip(mode)


def test_createld_offers_no_delete_source():
    from fastapi import HTTPException

    from app.routes import convert as convert_routes

    spec = registry.spec("createld")
    assert not spec.supports_delete_on_verify and not spec.supports_round_trip
    with pytest.raises(HTTPException):
        convert_routes._validate_delete_on_verify(spec, True)


@pytest.mark.asyncio
async def test_a_chd_with_no_metadata_is_digested_by_its_data_sha1(tmp_path, chd_infos):
    """What createraw writes: no CD, DVD or hard-disk tag at all."""
    chd = tmp_path / "Game.chd"
    chd.write_bytes(b"x")
    chd_infos[str(chd)] = ("ab" * 20, None)

    assert await registry.get("chdman").content_digest(str(chd)) == ContentDigest(
        kind=IMAGE, value="ab" * 20,
    )


@pytest.mark.asyncio
async def test_metadata_lines_that_do_not_parse_are_not_read_as_a_raw_chd(
    tmp_path, monkeypatch,
):
    """Only a CHD with no Metadata lines at all is read like a createraw CHD.
    Lines in a format the tag reader does not know keep the Source."""
    chd = tmp_path / "Game.chd"
    chd.write_bytes(b"x")
    drifted = _info("ab" * 20, None) + b"Metadata:     Type=CHT2 (format changed)\n"

    async def fake_capture(cmd, **kwargs):
        return 0, drifted, b""

    monkeypatch.setattr(
        registry.get("chdman")._service._runner, "run_capture", fake_capture,
    )
    with pytest.raises(ContentDigestUnavailable, match="metadata"):
        await registry.get("chdman").content_digest(str(chd))


@pytest.fixture(name="chd_verify")
def _chd_verify(monkeypatch):
    """chdman verify per CHD path: ``{path: (valid, message)}``; default passes."""
    results: dict[str, tuple[bool, str]] = {}
    calls: list[str] = []

    async def fake_verify(path, *, cancel_event=None):
        calls.append(path)
        valid, message = results.get(path, (True, "ok"))
        return {"valid": valid, "message": message}

    monkeypatch.setattr(registry.get("chdman")._service, "verify", fake_verify)
    return results, calls


@pytest.mark.asyncio
async def test_a_copy_whose_source_chd_is_not_intact_keeps_it_and_says_so(
    e2e, chd_infos, chd_verify,
):
    results, calls = chd_verify
    source = e2e["tmp_path"] / "Game.chd"
    source.write_bytes(b"MComprHD source")
    chd_infos[str(source)] = ("ab" * 20, "DVD ")
    results[str(source)] = (False, "Raw SHA1 in header = ab..., actual = cd...")

    def write(_src, out):
        out.write_bytes(b"MComprHD copy")
        chd_infos[str(out)] = ("ab" * 20, "DVD ")

    e2e["writer"] = write
    job = await _run(e2e, source, "copy")

    _assert_kept(job, source, reason="the Source CHD is not Intact")
    assert str(source) in calls


@pytest.mark.asyncio
async def test_a_copy_checks_its_source_chd_before_comparing(e2e, chd_infos, chd_verify):
    _results, calls = chd_verify
    source = e2e["tmp_path"] / "Game.chd"
    source.write_bytes(b"MComprHD source")
    chd_infos[str(source)] = ("ab" * 20, "DVD ")

    def write(_src, out):
        out.write_bytes(b"MComprHD copy")
        chd_infos[str(out)] = ("ab" * 20, "DVD ")

    e2e["writer"] = write
    _assert_round_tripped(await _run(e2e, source, "copy"), source)
    assert calls.count(str(source)) == 1


@pytest.mark.asyncio
async def test_create_jobs_do_not_verify_their_image_source(e2e, chd_infos, chd_verify):
    _results, calls = chd_verify
    image = e2e["tmp_path"] / "Disk.bin"
    image.write_bytes(b"sector" * 2048)

    def write(_src, out):
        out.write_bytes(b"MComprHD")
        chd_infos[str(out)] = (_sha1(image.read_bytes()), "GDDD")

    e2e["writer"] = write
    _assert_round_tripped(await _run(e2e, image, "createhd"), image)
    assert str(image) not in calls


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "tag"), [("createhd", "GDDD"), ("createraw", None)])
async def test_chdman_hd_and_raw_jobs_with_the_same_image_delete_it(
    e2e, chd_infos, mode, tag,
):
    image = e2e["tmp_path"] / "Disk.bin"
    image.write_bytes(b"sector" * 2048)

    def write(_src, out):
        out.write_bytes(b"MComprHD")
        chd_infos[str(out)] = (_sha1(image.read_bytes()), tag)

    e2e["writer"] = write
    _assert_round_tripped(await _run(e2e, image, mode), image)


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "tag"), [("createhd", "GDDD"), ("createraw", None)])
async def test_chdman_hd_and_raw_jobs_with_another_image_keep_it(
    e2e, chd_infos, mode, tag,
):
    image = e2e["tmp_path"] / "Disk.bin"
    image.write_bytes(b"sector" * 2048)

    def write(_src, out):
        out.write_bytes(b"MComprHD")
        chd_infos[str(out)] = ("00" * 20, tag)

    e2e["writer"] = write
    _assert_kept(await _run(e2e, image, mode), image)


@pytest.mark.asyncio
@pytest.mark.parametrize(("out_sha1", "deleted"), [("ab" * 20, True), ("cd" * 20, False)])
async def test_chdman_copy_compares_the_data_sha1_of_both_chds(
    e2e, chd_infos, out_sha1, deleted,
):
    source = e2e["tmp_path"] / "Game.chd"
    source.write_bytes(b"MComprHD source")
    chd_infos[str(source)] = ("ab" * 20, "DVD ")

    def write(_src, out):
        out.write_bytes(b"MComprHD copy")
        chd_infos[str(out)] = (out_sha1, "DVD ")

    e2e["writer"] = write
    job = await _run(e2e, source, "copy")
    if deleted:
        _assert_round_tripped(job, source)
    else:
        _assert_kept(job, source)


# ---------------------------------------------------------------------------
# within (#294 confines sheet and track reads to the Volumes unless a caller
# passes the trusted roots): every Round-trip hook takes it and passes it on
# ---------------------------------------------------------------------------


def _round_trip_tools():
    return sorted(
        {registry.for_mode(s.mode).id for s in registry.mode_specs() if s.supports_round_trip},
    )


@pytest.mark.parametrize("tool_id", _round_trip_tools())
def test_every_round_trip_tool_takes_within_on_both_hooks(tool_id):
    import inspect

    tool = registry.get(tool_id)
    for hook in (tool.content_digest, tool.source_problem):
        param = inspect.signature(hook).parameters.get("within")
        assert param is not None, f"{tool_id}.{hook.__name__} takes no within"
        assert param.kind is inspect.Parameter.KEYWORD_ONLY and param.default is None


@pytest.mark.asyncio
async def test_round_trip_passes_within_to_the_source_check_too():
    from services.round_trip import round_trip

    calls: list[tuple[str, object]] = []

    class Recorder:
        display_name = "Recorder"

        async def verify_timeout(self, path, *, cancel_event=None):
            return 0

        async def source_problem(self, path, *, cancel_event=None, within=None):
            calls.append(("source_problem", within))

        async def content_digest(self, path, *, cancel_event=None, within=None):
            return ContentDigest(kind="k", value="v")

    outcome = await round_trip(
        Recorder(), "/scratch/job/Game.chd", "/vol/Game_copy.chd", within=["/scratch/job"],
    )
    assert outcome.passed
    assert calls == [("source_problem", ["/scratch/job"])]


@pytest.mark.asyncio
async def test_a_chain_passes_within_on_to_its_first_steps_tool(tmp_path, monkeypatch):
    """A chain's Source is digested by its first step's tool; the job's trusted
    root for that Source must reach that tool, or a sheet in an extraction
    folder would be refused."""
    cso_tool = registry.get("cso")
    seen: list[object] = []
    original = cso_tool.content_digest

    async def recording(path, *, cancel_event=None, within=None):
        seen.append(within)
        return await original(path, cancel_event=cancel_event, within=within)

    monkeypatch.setattr(cso_tool, "content_digest", recording)
    cso = tmp_path / "Game.cso"
    write_ciso(cso, _image())

    digest = await registry.get("chain").content_digest(str(cso), within=[str(tmp_path)])

    assert digest.value == _sha1(_image())
    assert seen == [[str(tmp_path)]]


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool_id", "name", "write"), [
    ("cso", "Game.iso", lambda p: p.write_bytes(_image())),
    ("z3ds", "Game.3ds", lambda p: p.write_bytes(b"cartridge" * 999)),
    ("jwud", "game.wud", lambda p: p.write_bytes(b"\x00" * 4096)),
    ("nsz", "Game.nsp", lambda p: p.write_bytes(b"PFS0" + b"switch" * 99)),
    ("romz", "Game.gba", lambda p: p.write_bytes(b"cartridge" * 77)),
])
async def test_each_tool_digests_a_source_in_an_extraction_folder(
    tmp_path, tool_id, name, write,
):
    """An archive-member Source sits in the job's extraction folder, outside
    the Volumes. With that folder as ``within`` every tool digests it. None of
    these reads a sheet, so ``within`` only has to be accepted and passed on."""
    folder = tmp_path / "cmptr-extract-job"
    folder.mkdir()
    source = folder / name
    write(source)

    tool = registry.get(tool_id)
    with_root = await tool.content_digest(str(source), within=[str(folder)])
    assert with_root is not None
    assert with_root == await tool.content_digest(str(source))


@pytest.mark.asyncio
async def test_an_archive_member_iso_round_trips_through_cso_compress(e2e, monkeypatch):
    """End to end: a zip holding an ISO, converted to CSO with delete-source.
    The Source is the extracted member in the job's folder, digested with that
    folder as ``within``; the output side gets the Volumes."""
    import os

    from app.models import ConversionMode, DuplicateAction, JobCreateRequest

    archive = e2e["tmp_path"] / "Game.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Game.iso", _image())
    e2e["writer"] = lambda src, out: write_ciso(out, src.read_bytes())

    seen: list[tuple[str, object]] = []
    tool = _cso()
    original = tool.content_digest

    async def recording(path, *, cancel_event=None, within=None):
        seen.append((path, within))
        return await original(path, cancel_event=cancel_event, within=within)

    monkeypatch.setattr(tool, "content_digest", recording)
    routes = e2e["routes"]
    job = await routes.create_job(JobCreateRequest(
        file_path=f"{archive}::Game.iso", mode=ConversionMode.CSO_COMPRESS,
        duplicate_action=DuplicateAction.SKIP, delete_on_verify=True,
    ))
    await routes.job_manager._process_job(job.id)

    assert job.status.value == "completed", job.error_message
    assert job.check_passed == "round_tripped"
    (source_path, source_within), (output_path, output_within) = seen
    assert source_within == [os.path.dirname(source_path)]
    assert output_path == job.output_path and output_within is None


# The real-binary tests, run through each service's production convert, are
# in tests/test_round_trip_301_real.py.
