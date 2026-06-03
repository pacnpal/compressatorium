"""Tests for the romz service (handheld ROM .7z/.zip packer).

The ``7z`` binary isn't available in the sandbox, so the subprocess paths
(compress / extract / verify) are exercised by stubbing the shared
``SubprocessRunner`` the service drives, while the pure read-side logic
(member listing, single-member validation, info, output paths) runs against
real ``zipfile`` archives created with the stdlib.
"""
from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path

import pytest

from app.services import romz as romz_mod
from app.services.romz import RomzService, _compress_flags, _parse_progress


def _make_zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


# ----- pure helpers --------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "preset", "must_contain"),
    [
        ("romz_7z", "max", ["-t7z", "-m0=lzma2", "-mx=9", "-md=256m", "-mfb=273"]),
        ("romz_7z", "default", ["-t7z", "-m0=lzma2", "-mx=7", "-md=64m"]),
        ("romz_7z", "fast", ["-t7z", "-m0=lzma2", "-mx=1"]),
        ("romz_7z", None, ["-mx=9", "-md=256m"]),       # default token -> max
        ("romz_zip", "max", ["-tzip", "-mx=9"]),
        ("romz_zip", "fast", ["-tzip", "-mx=1"]),
    ],
)
def test_compress_flags(mode, preset, must_contain):
    flags = _compress_flags(mode, preset)
    for token in must_contain:
        assert token in flags
    # zip never uses the lzma2 dictionary knobs.
    if mode == "romz_zip":
        assert not any(f.startswith("-md=") for f in flags)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (" 45% 3 - game.gba", 45),
        ("100%", 100),
        ("Compressing game.gba", None),
        ("250%", None),  # out of range
    ],
)
def test_parse_progress(line, expected):
    assert _parse_progress(line) == expected


@pytest.mark.parametrize(
    ("mode", "name", "output_dir", "expected_name"),
    [
        ("romz_7z", "Game.gba", None, "Game.gba.7z"),
        ("romz_zip", "Game.nds", None, "Game.nds.zip"),
        ("romz_7z", "Game.gb", "/out", "Game.gb.7z"),
    ],
)
def test_compress_output_path_preserves_rom_extension(
    mode, name, output_dir, expected_name,
):
    out = RomzService.get_output_path_for_mode(mode, f"/data/{name}", output_dir)
    assert Path(out).name == expected_name
    if output_dir:
        assert out.startswith(output_dir)


# ----- archive read side (real zipfile) ------------------------------------


def test_single_rom_member_ok(tmp_path):
    archive = _make_zip(tmp_path / "Game.gba.7z.zip", {"Game.gba": b"ROMDATA"})
    # extension is .zip here so listing uses zipfile
    assert RomzService._single_rom_member(str(archive)) == "Game.gba"


def test_single_rom_member_rejects_no_rom(tmp_path):
    archive = _make_zip(tmp_path / "doc.zip", {"readme.txt": b"hi"})
    with pytest.raises(ValueError, match="no Game Boy"):
        RomzService._single_rom_member(str(archive))


def test_single_rom_member_rejects_multifile(tmp_path):
    archive = _make_zip(
        tmp_path / "two.zip", {"a.gba": b"a", "b.txt": b"b"},
    )
    with pytest.raises(ValueError, match="more than one file"):
        RomzService._single_rom_member(str(archive))


def test_extract_output_path_uses_archived_member_basename(tmp_path):
    archive = _make_zip(tmp_path / "Game.zip", {"Real Name.gba": b"ROM"})
    out = RomzService.get_output_path_for_mode(
        "romz_extract", str(archive), str(tmp_path / "out"),
    )
    assert Path(out).name == "Real Name.gba"


def test_extract_output_path_falls_back_when_unreadable(tmp_path):
    missing = tmp_path / "Game.gba.7z"  # no such file
    out = RomzService.get_output_path_for_mode("romz_extract", str(missing))
    # Falls back to stripping the archive suffix (this tool's own convention).
    assert Path(out).name == "Game.gba"


def test_info_for_archive_reports_contained_rom_and_ratio(tmp_path):
    archive = _make_zip(tmp_path / "Game.zip", {"Game.gba": b"X" * 1000})
    info = romz_mod.romz_service.info(str(archive))
    assert info["compressed"] is True
    assert info["extension"] == ".zip"
    assert info["contained_name"] == "Game.gba"
    assert info["original_size"] == 1000
    assert info["ratio"] is not None


def test_info_for_loose_rom(tmp_path):
    rom = tmp_path / "Game.gba"
    rom.write_bytes(b"ROM")
    info = romz_mod.romz_service.info(str(rom))
    assert info["compressed"] is False
    assert info["format"] == "Game Boy Advance ROM"
    assert info["contained_name"] is None


@pytest.mark.skipif(not romz_mod.HAS_7Z, reason="py7zr not installed")
def test_single_rom_member_reads_real_7z(tmp_path):
    """Exercise the py7zr listing branch with a genuine .7z archive."""
    import py7zr

    archive = tmp_path / "Game.7z"
    with py7zr.SevenZipFile(archive, "w") as zf:
        zf.writestr(b"ROMDATA", "Game.nds")
    assert RomzService._single_rom_member(str(archive)) == "Game.nds"
    info = romz_mod.romz_service.info(str(archive))
    assert info["compressed"] is True
    assert info["contained_name"] == "Game.nds"


# ----- convert / verify (stubbed runner) -----------------------------------


@pytest.fixture
def stub_runner(monkeypatch):
    """Replace the service's SubprocessRunner.run/run_capture with recorders."""
    calls: dict[str, object] = {}

    async def fake_run(cmd, *, input_path, output_path, parse_progress,
                       cancel_event=None, fail_label="", complete_message=""):
        calls["run_cmd"] = cmd
        calls["output_path"] = output_path
        # Simulate the binary producing the output file.
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"OUT")
        yield {"progress": 100, "message": complete_message}

    async def fake_capture(cmd, *, timeout=None, cancel_event=None,
                           stderr_to_stdout=False):
        calls["capture_cmd"] = cmd
        return calls.get("capture_rc", 0), calls.get("capture_out", b"Everything is Ok"), b""

    svc = romz_mod.romz_service
    monkeypatch.setattr(svc._runner, "run", fake_run)
    monkeypatch.setattr(svc._runner, "run_capture", fake_capture)
    return svc, calls


def test_convert_compress_builds_7z_command_and_clears_stale_output(
    tmp_path, stub_runner,
):
    svc, calls = stub_runner
    rom = tmp_path / "Game.gba"
    rom.write_bytes(b"ROM")
    out = tmp_path / "Game.gba.7z"
    out.write_bytes(b"STALE")  # must be removed before `7z a` appends

    async def _drain():
        return [u async for u in svc.convert(str(rom), str(out), "romz_7z",
                                             compression="max")]

    updates = asyncio.run(_drain())
    assert updates[-1]["progress"] == 100
    cmd = calls["run_cmd"]
    assert "a" in cmd and str(out) in cmd and str(rom) in cmd
    assert "-mx=9" in cmd and "-mfb=273" in cmd
    # The stale archive was replaced (not appended to).
    assert out.read_bytes() == b"OUT"


def test_convert_extract_resolves_member_and_runs_7z_e(tmp_path, stub_runner):
    svc, calls = stub_runner
    archive = _make_zip(tmp_path / "Game.zip", {"Game.gba": b"ROM"})
    out = tmp_path / "out" / "Game.gba"

    async def _drain():
        return [u async for u in svc.convert(str(archive), str(out), "romz_extract")]

    asyncio.run(_drain())
    cmd = calls["run_cmd"]
    assert "e" in cmd
    assert "Game.gba" in cmd            # the resolved single member
    assert any(c.startswith("-o") for c in cmd)


def test_convert_cleans_partial_output_on_failure(tmp_path, monkeypatch):
    svc = romz_mod.romz_service
    rom = tmp_path / "Game.gba"
    rom.write_bytes(b"ROM")
    out = tmp_path / "Game.gba.7z"

    async def boom(cmd, *, input_path, output_path, parse_progress,
                   cancel_event=None, fail_label="", complete_message=""):
        Path(output_path).write_bytes(b"PARTIAL")
        raise RuntimeError("7z failed")
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(svc._runner, "run", boom)

    async def _drain():
        return [u async for u in svc.convert(str(rom), str(out), "romz_7z")]

    with pytest.raises(RuntimeError, match="7z failed"):
        asyncio.run(_drain())
    assert not out.exists()  # partial archive removed


def test_verify_pass(tmp_path, stub_runner):
    svc, calls = stub_runner
    archive = _make_zip(tmp_path / "Game.zip", {"Game.gba": b"ROM"})
    result = asyncio.run(svc.verify(str(archive)))
    assert result["valid"] is True
    assert calls["capture_cmd"][:2] == [svc.sevenzip_path, "t"]


def test_verify_fail(tmp_path, stub_runner):
    svc, calls = stub_runner
    calls["capture_rc"] = 2
    calls["capture_out"] = b"ERROR: CRC failed"
    archive = _make_zip(tmp_path / "Game.zip", {"Game.gba": b"ROM"})
    result = asyncio.run(svc.verify(str(archive)))
    assert result["valid"] is False
    assert "failed" in result["message"].lower()


def test_verify_rejects_non_archive(tmp_path, stub_runner):
    svc, _ = stub_runner
    rom = tmp_path / "Game.gba"
    rom.write_bytes(b"ROM")
    result = asyncio.run(svc.verify(str(rom)))
    assert result["valid"] is False
    assert "extension" in result["message"].lower()
