"""Tests for the maxcso service (compress/decompress/verify/output paths).

These mock ``asyncio.create_subprocess_exec`` in the service module, so they
need neither the real ``maxcso`` binary nor real disc images.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.services import maxcso as maxcso_module
from app.services.chdman import ConversionCancelled

service = maxcso_module.maxcso_service


class _FakeProcess:
    """Reads preset stdout chunks then EOF; wait() sets the return code."""

    def __init__(self, pid: int, chunks: list[bytes], returncode: int = 0):
        self.pid = pid
        self._chunks = list(chunks)
        self.returncode = None
        self._final_rc = returncode
        self.killed = False
        self.stdout = self

    async def read(self, _n: int) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        return b""

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = -9 if self.killed else self._final_rc
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def communicate(self):
        if self.returncode is None:
            self.returncode = self._final_rc
        return b"", b""


def _write_output(argv: list[str]) -> None:
    """Mimic maxcso: write the file named after the ``-o`` argument."""
    out_path = argv[argv.index("-o") + 1]
    Path(out_path).write_bytes(b"converted")


async def _drain(agen) -> list[dict]:
    return [u async for u in agen]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "src", "out"),
    [
        ("cso_compress", "game.iso", "game.cso"),
        ("zso_compress", "game.iso", "game.zso"),
        ("cso_decompress", "game.cso", "game.iso"),
        ("cso_decompress", "game.zso", "game.iso"),
    ],
)
async def test_convert_happy_path(tmp_path, monkeypatch, mode, src, out):
    src_path = tmp_path / src
    src_path.write_bytes(b"input-bytes")
    out_path = tmp_path / out

    captured = {}

    async def fake_exec(*args, **_kwargs):
        captured["argv"] = list(args)
        _write_output(list(args))
        return _FakeProcess(pid=4242, chunks=[b"42%\n"])

    monkeypatch.setattr(maxcso_module.asyncio, "create_subprocess_exec", fake_exec)

    updates = await _drain(service.convert(str(src_path), str(out_path), mode))

    assert updates[-1]["progress"] == 100
    assert out_path.read_bytes() == b"converted"
    argv = captured["argv"]
    assert ("--decompress" in argv) == (mode == "cso_decompress")
    assert ("--format=zso" in argv) == (mode == "zso_compress")


@pytest.mark.asyncio
async def test_convert_nonzero_exit_raises(tmp_path, monkeypatch):
    src_path = tmp_path / "game.iso"
    src_path.write_bytes(b"input")
    out_path = tmp_path / "game.cso"

    async def fake_exec(*_args, **_kwargs):
        return _FakeProcess(pid=5, chunks=[b"bad input\n"], returncode=1)

    monkeypatch.setattr(maxcso_module.asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(RuntimeError, match="exit code 1"):
        await _drain(service.convert(str(src_path), str(out_path), "cso_compress"))


class _CancelStdout:
    def __init__(self, stop: asyncio.Event):
        self._stop = stop
        self._first = True

    async def read(self, _n: int) -> bytes:
        if self._first:
            self._first = False
            return b"working\n"
        await self._stop.wait()
        return b""


class _CancelProcess:
    def __init__(self, pid: int, stop: asyncio.Event):
        self.pid = pid
        self._stop = stop
        self.stdout = _CancelStdout(stop)
        self.returncode = None

    def terminate(self) -> None:
        self.returncode = -15
        self._stop.set()

    def kill(self) -> None:
        self.returncode = -9
        self._stop.set()

    async def wait(self) -> int:
        await self._stop.wait()
        if self.returncode is None:
            self.returncode = -15
        return self.returncode


@pytest.mark.asyncio
async def test_convert_cancel_removes_partial_output(tmp_path, monkeypatch):
    src_path = tmp_path / "game.iso"
    src_path.write_bytes(b"input")
    out_path = tmp_path / "game.cso"

    cancel_event = asyncio.Event()
    cancel_event.set()

    async def fake_exec(*args, **_kwargs):
        _write_output(list(args))  # partial output written directly to -o path
        return _CancelProcess(pid=7, stop=asyncio.Event())

    monkeypatch.setattr(maxcso_module.asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(ConversionCancelled):
        await _drain(
            service.convert(
                str(src_path), str(out_path), "cso_compress", cancel_event=cancel_event,
            ),
        )
    assert not out_path.exists()


@pytest.mark.asyncio
async def test_verify_passes_on_zero_exit(tmp_path, monkeypatch):
    cso_path = tmp_path / "game.cso"
    cso_path.write_bytes(b"data")

    async def fake_exec(*_args, **_kwargs):
        return _FakeProcess(pid=11, chunks=[], returncode=0)

    monkeypatch.setattr(maxcso_module.asyncio, "create_subprocess_exec", fake_exec)

    result = await service.verify(str(cso_path))
    assert result["valid"] is True


@pytest.mark.asyncio
async def test_verify_fails_on_nonzero_exit(tmp_path, monkeypatch):
    cso_path = tmp_path / "game.cso"
    cso_path.write_bytes(b"data")

    async def fake_exec(*_args, **_kwargs):
        return _FakeProcess(pid=12, chunks=[b"crc mismatch\n"], returncode=2)

    monkeypatch.setattr(maxcso_module.asyncio, "create_subprocess_exec", fake_exec)

    result = await service.verify(str(cso_path))
    assert result["valid"] is False
    assert "failed" in result["message"].lower()


@pytest.mark.asyncio
async def test_verify_rejects_uncompressed_extension(tmp_path):
    iso = tmp_path / "game.iso"
    iso.write_bytes(b"data")
    result = await service.verify(str(iso))
    assert result["valid"] is False
    assert "extension" in result["message"].lower()


@pytest.mark.parametrize(
    ("mode", "inp", "expected_suffix"),
    [
        ("cso_compress", "/data/game.iso", ".cso"),
        ("zso_compress", "/data/game.iso", ".zso"),
        ("cso_decompress", "/data/game.cso", ".iso"),
        ("cso_decompress", "/data/game.zso", ".iso"),
        ("cso_decompress", "/data/game.dax", ".iso"),
    ],
)
def test_output_path_for_mode(mode, inp, expected_suffix):
    out = service.get_output_path_for_mode(mode, inp)
    assert out.endswith(f"game{expected_suffix}")


def test_output_path_rejects_unknown_mode():
    with pytest.raises(ValueError, match="Unsupported maxcso mode"):
        service.get_output_path_for_mode("bogus_mode", "/data/game.iso")


def test_build_command_flags():
    compress = service._build_command("/data/game.iso", "/data/game.cso", "cso_compress")
    assert "--decompress" not in compress
    assert "--format=zso" not in compress

    zso = service._build_command("/data/game.iso", "/data/game.zso", "zso_compress")
    assert "--format=zso" in zso

    decompress = service._build_command("/data/game.cso", "/data/game.iso", "cso_decompress")
    assert "--decompress" in decompress
    assert "--format=zso" not in decompress


def test_info_reports_compression_state(tmp_path):
    cso_path = tmp_path / "game.cso"
    cso_path.write_bytes(b"x" * 2048)
    info = service.info(str(cso_path))
    assert info["compressed"] is True
    assert info["extension"] == ".cso"

    iso = tmp_path / "game.iso"
    iso.write_bytes(b"x" * 2048)
    assert service.info(str(iso))["compressed"] is False
