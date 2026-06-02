"""Regression tests for z3ds verification safety checks."""

import asyncio
import struct
from pathlib import Path

import pytest

from app.services import z3ds_compress as z3ds_module


class _FakeStdin:
    def __init__(self, *, fail_write: bool = False):
        self.buffer = bytearray()
        self.closed = False
        self._fail_write = fail_write

    def write(self, chunk: bytes) -> None:
        if self._fail_write:
            raise OSError("simulated stream write failure")
        self.buffer.extend(chunk)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _FakeProcess:
    def __init__(self, pid: int, *, fail_write: bool = False):
        self.pid = pid
        self.stdin = _FakeStdin(fail_write=fail_write)
        self.stdout = None
        self.stderr = None
        self.returncode = None
        self.killed = False
        self.wait_called = False

    async def communicate(self):
        if self.returncode is None:
            self.returncode = -9 if self.killed else 0
        return b"", b""

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        self.wait_called = True
        if self.returncode is None:
            self.returncode = -9 if self.killed else 0
        return self.returncode


def _build_z3ds_container(
    *,
    metadata_size: int,
    payload: bytes,
    underlying_magic: bytes = b"CART",
) -> bytes:
    header = struct.pack(
        "<4s4sBBHIQQ",
        b"Z3DS",
        underlying_magic,
        1,
        0,
        0x20,
        metadata_size,
        len(payload),
        1024,
    )
    metadata = b"M" * metadata_size
    return header + metadata + payload


@pytest.mark.asyncio
async def test_verify_fails_when_zstd_missing(tmp_path: Path, monkeypatch):
    """Verification must fail closed when deep integrity tooling is unavailable."""
    rom_path = tmp_path / "game.z3ds"
    rom_path.write_bytes(b"Z3DS" + (b"\x00" * 256))

    monkeypatch.setattr(z3ds_module.shutil, "which", lambda name: None)

    result = await z3ds_module.z3ds_compress_service.verify(str(rom_path))

    assert result["valid"] is False
    assert "zstd" in result["message"].lower()


@pytest.mark.asyncio
async def test_verify_reads_payload_from_header_offset(tmp_path: Path, monkeypatch):
    """Verification should stream from computed header+metadata offset, not a fixed skip."""
    payload = b"zstd-stream-bytes"
    rom_path = tmp_path / "game.z3ds"
    rom_path.write_bytes(_build_z3ds_container(metadata_size=0x10, payload=payload))

    process = _FakeProcess(pid=10101)

    async def _fake_exec(*_args, **_kwargs):
        return process

    monkeypatch.setattr(z3ds_module.shutil, "which", lambda _name: "/usr/bin/zstd")
    monkeypatch.setattr(
        z3ds_module.asyncio,
        "create_subprocess_exec",
        _fake_exec,
    )

    result = await z3ds_module.z3ds_compress_service.verify(str(rom_path))

    assert result["valid"] is True
    assert bytes(process.stdin.buffer) == payload


@pytest.mark.asyncio
async def test_verify_cleans_tracked_pid_on_stream_failure(tmp_path: Path, monkeypatch):
    """Verification must always untrack/reap child process on streaming errors."""
    payload = b"zstd-stream-bytes"
    rom_path = tmp_path / "game.z3ds"
    rom_path.write_bytes(_build_z3ds_container(metadata_size=0x10, payload=payload))

    process = _FakeProcess(pid=20202, fail_write=True)

    async def _fake_exec(*_args, **_kwargs):
        return process

    monkeypatch.setattr(z3ds_module.shutil, "which", lambda _name: "/usr/bin/zstd")
    monkeypatch.setattr(
        z3ds_module.asyncio,
        "create_subprocess_exec",
        _fake_exec,
    )

    service = z3ds_module.z3ds_compress_service
    before = set(service.active_pids())
    result = await service.verify(str(rom_path))
    after = set(service.active_pids())

    assert result["valid"] is False
    assert process.wait_called is True
    assert process.pid not in after
    assert after == before


class _HangingProcess(_FakeProcess):
    """Streams normally, but ``communicate()`` never returns."""

    async def communicate(self):
        await asyncio.sleep(10)
        return b"", b""


@pytest.mark.asyncio
async def test_verify_times_out_when_process_hangs(tmp_path: Path, monkeypatch):
    """A hung zstd verify must trip the verify timeout and reap the child."""
    payload = b"zstd-stream-bytes"
    rom_path = tmp_path / "game.z3ds"
    rom_path.write_bytes(_build_z3ds_container(metadata_size=0x10, payload=payload))

    process = _HangingProcess(pid=30303)

    async def _fake_exec(*_args, **_kwargs):
        return process

    monkeypatch.setattr(z3ds_module.shutil, "which", lambda _name: "/usr/bin/zstd")
    monkeypatch.setattr(z3ds_module.asyncio, "create_subprocess_exec", _fake_exec)
    # Bound the verify subprocess at a tiny timeout so the hang trips it fast.
    monkeypatch.setattr(z3ds_module, "verify_timeout", lambda _owner=None: 0.05)

    service = z3ds_module.z3ds_compress_service
    result = await service.verify(str(rom_path))

    assert result["valid"] is False
    assert "timed out" in result["message"].lower()
    assert process.killed is True
    assert process.pid not in set(service.active_pids())
