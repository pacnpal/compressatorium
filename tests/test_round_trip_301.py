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
