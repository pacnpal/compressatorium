"""A FIFO, socket or device is never opened by Platform detection.

Opening a FIFO with no writer blocks forever, so a ``game.iso`` FIFO on a
Volume would strand the threadpool worker an Auto request runs detection on.
``detect_platform`` checks the input's file type (``lstat``, a symlink judged
by its target) before any header probe or open, and ``resolve_track`` refuses a
track that exists and is not a regular file or folder, so a sheet cannot point
the probe at one either. Each test runs detection on a daemon thread with a
short timeout, so a regression fails instead of hanging the suite.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest
from services import platform_detect
from services.platform_detect import detect_platform
from utils.sheet_text import resolve_track

pytestmark = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo")

_TIMEOUT = 3.0


def _saturn_track(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sector = bytearray(2352)
    sector[16:16 + 16] = b"SEGA SEGASATURN "
    path.write_bytes(bytes(sector))
    return path


@pytest.fixture(name="folder")
def _folder(tmp_path, monkeypatch):
    from config import settings

    volume = tmp_path / "volume"
    (volume / "games").mkdir(parents=True)
    monkeypatch.setattr(settings, "chd_volumes", str(volume))
    platform_detect.clear_cache()
    yield volume / "games"
    platform_detect.clear_cache()


def _release(fifo: Path) -> None:
    """Unblock a reader stuck opening ``fifo`` (a writer that leaves at once)."""
    try:
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return
    os.close(fd)


def _detect_with_timeout(path: Path, fifo: Path):
    box: dict = {}

    def run():
        box["result"] = detect_platform(str(path))

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(_TIMEOUT)
    blocked = worker.is_alive()
    if blocked:
        _release(fifo)
        worker.join(_TIMEOUT)
    assert not blocked, f"detect_platform blocked on {fifo.name}"
    return box["result"]


@pytest.mark.parametrize("name", ["game.iso", "game.cue", "game.gdi", "game.chd"])
def test_a_fifo_input_returns_no_platform_without_blocking(folder, name):
    fifo = folder / name
    os.mkfifo(fifo)

    result = _detect_with_timeout(fifo, fifo)

    assert result.platform is None


def test_a_symlink_to_a_fifo_is_judged_by_its_target(folder):
    fifo = folder / "real.iso"
    os.mkfifo(fifo)
    link = folder / "game.iso"
    link.symlink_to(fifo)

    result = _detect_with_timeout(link, fifo)

    assert result.platform is None


@pytest.mark.parametrize("ext", [".cue", ".gdi"])
def test_a_sheet_naming_a_fifo_track_returns_no_platform_without_blocking(folder, ext):
    fifo = folder / "game.bin"
    os.mkfifo(fifo)
    sheet = folder / f"game{ext}"
    if ext == ".cue":
        sheet.write_text('FILE "game.bin" BINARY\n  TRACK 01 MODE1/2352\n')
    else:
        sheet.write_text("1\n1 0 4 2352 game.bin 0\n")

    result = _detect_with_timeout(sheet, fifo)

    assert result.platform is None


def test_resolve_track_refuses_a_fifo_track(folder):
    os.mkfifo(folder / "game.bin")

    with pytest.raises(ValueError):
        resolve_track(folder, "game.bin")


def test_resolve_track_still_allows_a_missing_or_regular_track(folder):
    _saturn_track(folder / "game.bin")

    assert resolve_track(folder, "game.bin") == folder / "game.bin"
    assert resolve_track(folder, "absent.bin") == folder / "absent.bin"


def test_a_symlinked_regular_track_is_still_detected(folder):
    real = _saturn_track(folder / "data" / "track.bin")
    (folder / "game.bin").symlink_to(real)
    cue = folder / "game.cue"
    cue.write_text('FILE "game.bin" BINARY\n  TRACK 01 MODE1/2352\n')

    result = detect_platform(str(cue))

    assert result.platform == "saturn"
