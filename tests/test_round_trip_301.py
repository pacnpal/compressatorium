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


def _image(size: int = 5 * 2048 + 700) -> bytes:
    """An ISO-ish image: a run of one byte (exercises an LZ4 match), then
    varied bytes, with a short last block."""
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
    "dax_nc",
])
async def test_maxcso_container_digest_is_the_sha1_of_the_image_it_decodes_to(
    tmp_path, kind,
):
    image = _image()
    out = tmp_path / "Game.cso"
    if kind == "cso1":
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
    assert "Source kept" in job.error_message
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

    from app.models import ConversionMode
    from utils.delete_plan import build_delete_snapshot

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


_Z3DS_BIN = _binary("Z3DS_COMPRESSOR_PATH", "/usr/local/bin/z3ds_compressor", "z3ds_compressor")


@pytest.mark.skipif(_Z3DS_BIN is None or _ZSTD is None, reason="z3ds_compressor is not installed")
@pytest.mark.asyncio
async def test_real_z3ds_compressor_output_decodes_to_the_rom(tmp_path):
    import subprocess

    rom = tmp_path / "Game.3ds"
    rom.write_bytes(os.urandom(1 << 20) + b"\0" * (1 << 20))
    out = tmp_path / "Game.z3ds"
    subprocess.run([_Z3DS_BIN, "-c", str(rom), str(out)], check=True, capture_output=True)

    digest = await _z3ds().content_digest(str(out))

    assert digest.value == _sha1(rom.read_bytes())


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


_JWUD = _binary("JWUDTOOL_PATH", "/usr/local/bin/jwudtool", "jwudtool")


@pytest.mark.skipif(
    _JWUD is None or not os.environ.get("JWUD_TEST_WUD"),
    reason="needs JWUDTool and a real Wii U image in JWUD_TEST_WUD",
)
@pytest.mark.asyncio
async def test_real_jwudtool_output_decodes_to_the_wud(tmp_path):
    """Opt-in: a WUX JWUDTool wrote decodes to the image it was made from.
    Needs a real 25 GB .wud (JNUSLib refuses any other size)."""
    import subprocess

    wud = os.environ["JWUD_TEST_WUD"]
    subprocess.run(
        [_JWUD, "-in", wud, "-out", str(tmp_path), "-compress", "-noVerify"],
        check=True, capture_output=True,
    )
    source = await _jwud().content_digest(wud)
    output = await _jwud().content_digest(str(tmp_path / "game.wux"))
    assert source == output


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


_NSZ_BIN = _binary("NSZ_PATH", "/usr/local/bin/nsz", "nsz")


@pytest.mark.skipif(
    _NSZ_BIN is None or not os.environ.get("NSZ_TEST_NSP"),
    reason="needs nsz, prod.keys and a real NSP in NSZ_TEST_NSP",
)
@pytest.mark.asyncio
async def test_real_nsz_output_rebuilds_the_nsp(tmp_path):
    """Opt-in: a real NSP compressed by the real nsz (with prod.keys set up
    as the app finds them) round-trips to the same SHA256."""
    import subprocess

    nsp = os.environ["NSZ_TEST_NSP"]
    subprocess.run(
        [_NSZ_BIN, "-C", "-K", "-o", str(tmp_path), nsp], check=True, capture_output=True,
    )
    out = tmp_path / (Path(nsp).stem + ".nsz")
    assert await _nsz().content_digest(nsp) == await _nsz().content_digest(str(out))


_MAXCSO = _binary("MAXCSO_PATH", "/usr/local/bin/maxcso", "maxcso")


@pytest.mark.skipif(_MAXCSO is None, reason="maxcso is not installed")
@pytest.mark.asyncio
@pytest.mark.parametrize("fmt", ["cso1", "cso2", "zso", "dax"])
async def test_real_maxcso_output_decodes_to_the_iso(tmp_path, fmt):
    """Opt-in: runs the real maxcso, so the Python decoder is checked against
    what maxcso writes, not only against this file's fixtures."""
    import subprocess

    iso = tmp_path / "Game.iso"
    iso.write_bytes(os.urandom(64 * 2048) + b"\0" * (64 * 2048) + _image(32 * 2048))
    ext = {"zso": ".zso", "dax": ".dax"}.get(fmt, ".cso")
    out = tmp_path / f"Game{ext}"
    subprocess.run(
        [_MAXCSO, f"--format={fmt}", str(iso), "-o", str(out)],
        check=True, capture_output=True,
    )
    digest = await _cso().content_digest(str(out))
    assert digest.value == _sha1(iso.read_bytes())
