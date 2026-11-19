"""What a check that raised proves (#306, review round 5).

``inconclusive`` is an allowlist, never a catch-all: a missing tool, library or
prerequisite, a failed launch, a timeout, or an OSError reading the input says
nothing about the file. A header or format error, a decoder that exits non-zero
and a broken pipe from a decoder that quit early are verdicts on the file.
One helper (``verify_exception_event``) classifies for every tool.
"""
from __future__ import annotations

import asyncio
import errno
import shutil
import struct
import zipfile

import pytest
from services.subprocess_runner import (
    ToolUnavailable,
    collect_verify,
    verify_exception_event,
)


@pytest.mark.parametrize("exc", [
    ToolUnavailable("zstd is not installed"),
    FileNotFoundError(errno.ENOENT, "No such file or directory", "maxcso"),
    PermissionError(errno.EACCES, "Permission denied"),
    OSError(errno.EIO, "Input/output error"),
    asyncio.TimeoutError(),
])
def test_could_not_run_is_inconclusive(exc):
    event = verify_exception_event(exc)
    assert event["valid"] is False
    assert event.get("inconclusive") is True


@pytest.mark.parametrize("exc", [
    ValueError("Invalid Z3DS file: missing Z3DS header"),
    BrokenPipeError(errno.EPIPE, "Broken pipe"),
    ConnectionResetError(errno.ECONNRESET, "Connection reset by peer"),
    zipfile.BadZipFile("File is not a zip file"),
    struct.error("unpack requires a buffer of 32 bytes"),
    RuntimeError("something unexpected"),
])
def test_anything_else_is_a_verdict(exc):
    event = verify_exception_event(exc)
    assert event["valid"] is False
    assert "inconclusive" not in event


# --------------------------------------------------------------------------- #
# z3ds: the two reproductions
# --------------------------------------------------------------------------- #


def _z3ds(tmp_path, payload: bytes, magic: bytes = b"Z3DS"):
    header = struct.pack("<4s4sBBHIQQ", magic, b"NCSD", 1, 0, 0x20, 0, len(payload), 0)
    path = tmp_path / "Game.z3ds"
    path.write_bytes(header + payload)
    return str(path)


def _verify(service, path):
    return asyncio.run(collect_verify(service.verify_stream(path), fallback_message="failed"))


@pytest.mark.skipif(shutil.which("zstd") is None, reason="needs zstd")
def test_z3ds_garbage_payload_is_a_verdict(tmp_path):
    from services.z3ds_compress import Z3DSCompressService

    result = _verify(Z3DSCompressService(), _z3ds(tmp_path, b"\xde\xad\xbe\xef" * 4096))

    assert result["valid"] is False
    assert "inconclusive" not in result, result


@pytest.mark.skipif(shutil.which("zstd") is None, reason="needs zstd")
def test_z3ds_bad_header_is_a_verdict(tmp_path):
    from services.z3ds_compress import Z3DSCompressService

    result = _verify(Z3DSCompressService(), _z3ds(tmp_path, b"x" * 64, magic=b"NOPE"))

    assert result["valid"] is False
    assert "inconclusive" not in result, result
    assert "Z3DS header" in result["message"]


def test_z3ds_without_zstd_is_inconclusive_and_not_ready_to_verify(tmp_path, monkeypatch):
    from services import z3ds_compress
    from services.tools import registry
    from services.z3ds_compress import Z3DSCompressService

    monkeypatch.setattr(z3ds_compress.shutil, "which", lambda name: None)

    result = _verify(Z3DSCompressService(), _z3ds(tmp_path, b"x" * 64))
    assert result.get("inconclusive") is True

    tool = registry.get("z3ds")
    assert asyncio.run(registry.tool_is_ready(tool, purpose="verify")) is False
    assert "zstd" in tool.verify_not_ready_reason


# --------------------------------------------------------------------------- #
# Other tools
# --------------------------------------------------------------------------- #


def test_romz_corrupt_archive_is_a_verdict(tmp_path):
    from services.romz import RomzService

    bad = tmp_path / "Game.gba.zip"
    bad.write_bytes(b"PK\x03\x04" + b"\x00" * 64)

    result = _verify(RomzService(), str(bad))

    assert result["valid"] is False
    assert "inconclusive" not in result, result


def test_romz_without_its_archive_library_is_inconclusive(tmp_path, monkeypatch):
    from services import romz as romz_module
    from services.romz import RomzService

    archive = tmp_path / "Game.gba.7z"
    archive.write_bytes(b"7z" + b"\x00" * 64)

    def missing(path):
        raise ToolUnavailable("py7zr is required to read .7z archives")

    service = RomzService()
    monkeypatch.setattr(service, "_single_rom_member", missing)
    monkeypatch.setattr(romz_module, "run_detached", _run_inline)

    result = _verify(service, str(archive))

    assert result.get("inconclusive") is True


async def _run_inline(func, *args, cancel_event=None, **kwargs):
    return func(*args, **kwargs)


def test_jwud_corrupt_header_is_a_verdict(tmp_path):
    from services.jwudtool import JwudToolService

    bad = tmp_path / "Game.wux"
    bad.write_bytes(b"NOTWUX" + b"\x00" * 1024)

    result = _verify(JwudToolService(), str(bad))

    assert result["valid"] is False
    assert "inconclusive" not in result, result


def test_xiso_read_error_is_inconclusive(tmp_path, monkeypatch):
    from services import extract_xiso
    from services.extract_xiso import ExtractXisoService

    image = tmp_path / "Game.xiso.iso"
    image.write_bytes(b"\x00" * 4096)

    def unreadable(*args, **kwargs):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(extract_xiso, "_check_xiso_structure", unreadable, raising=False)
    service = ExtractXisoService()
    monkeypatch.setattr(service, "_structural_check", unreadable, raising=False)

    result = _verify(service, str(image))

    assert result["valid"] is False
    assert result.get("inconclusive") is True, result


def test_wux_verify_is_ready_without_java(tmp_path, monkeypatch):
    """A .wux check is pure Python: no launcher or Java needed."""
    from services.tools import registry

    tool = registry.get("jwud")
    # No JWUDTool launcher where it is configured (#494's shared probe).
    monkeypatch.setattr(tool._service, "jwudtool_path", str(tmp_path / "jwudtool"))
    assert asyncio.run(registry.tool_is_ready(tool, purpose="verify")) is True
    assert asyncio.run(registry.tool_is_ready(tool)) is False



class _BrokenReads:
    """A file whose reads fail with EIO after a good open."""

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def seek(self, offset, whence=0):
        return offset

    def read(self, size=-1):
        raise OSError(errno.EIO, "Input/output error")


def test_strict_iso_reads_raise_from_the_pvd_and_directory_reads(monkeypatch):
    """_read_pvd and _list_dir swallowed I/O errors even under strict, so the
    makeps3iso read-back reported a failure on a read error."""
    from services import disc_id

    monkeypatch.setattr(disc_id, "open", _BrokenReads, raising=False)

    assert disc_id.read_iso_file("/x.iso", ["PS3_GAME", "PARAM.SFO"]) is None
    with pytest.raises(OSError):
        disc_id.read_iso_file("/x.iso", ["PS3_GAME", "PARAM.SFO"], strict=True)

    with pytest.raises(OSError):
        disc_id._list_dir(_BrokenReads(), 20, 2048, strict=True)
    assert disc_id._list_dir(_BrokenReads(), 20, 2048) == []
