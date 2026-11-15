"""Track names in a sheet never take a probe outside the sheet's folder or the
Volumes.

A ``.cue`` / ``.gdi`` names its tracks itself. The Platform probe opens them,
so an absolute name, a ``..`` name, or one that resolves (through a symlink)
outside the configured Volumes would let a planted sheet read the signature of
any file the service can open. One resolver (``utils.sheet_text``) refuses
those for every reader; a refused track means the probe says nothing.
"""
from __future__ import annotations

import builtins
import io
from pathlib import Path

import pytest
from services import platform_detect
from services.platform_detect import detect_platform

from .test_disc_id import _make_ipbin


def _saturn_track(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sector = bytearray(2352)
    sector[16:16 + 16] = b"SEGA SEGASATURN "
    path.write_bytes(bytes(sector))
    return path


@pytest.fixture(name="layout")
def _layout(tmp_path, monkeypatch):
    from config import settings

    volume = tmp_path / "volume"
    (volume / "games").mkdir(parents=True)
    outside = _saturn_track(tmp_path / "outside" / "x.bin")
    monkeypatch.setattr(settings, "chd_volumes", str(volume))
    platform_detect.clear_cache()
    yield {"volume": volume, "outside": outside, "folder": volume / "games"}
    platform_detect.clear_cache()


@pytest.fixture(name="opened")
def _opened(monkeypatch):
    seen: list[str] = []
    real_open = builtins.open
    real_io_open = io.open

    def spy(file, *args, **kwargs):
        seen.append(str(file))
        return real_open(file, *args, **kwargs)

    def io_spy(file, *args, **kwargs):
        seen.append(str(file))
        return real_io_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy)
    monkeypatch.setattr(io, "open", io_spy)
    return seen


def _was_opened(seen: list[str], target: Path) -> bool:
    real = str(target.resolve())
    return any(Path(p).resolve() == Path(real) for p in seen if p)


@pytest.mark.parametrize("kind", ["absolute", "dotdot"])
def test_a_cue_naming_a_track_outside_is_never_opened(layout, opened, kind):
    name = str(layout["outside"]) if kind == "absolute" else "../../outside/x.bin"
    cue = layout["folder"] / "game.cue"
    cue.write_text(f'FILE "{name}" BINARY\n  TRACK 01 MODE1/2352\n')

    opened.clear()   # only what the probe opens counts
    result = detect_platform(str(cue))

    assert result.platform is None
    assert not _was_opened(opened, layout["outside"])


def test_a_gdi_naming_a_track_outside_is_never_opened(layout, opened):
    outside = layout["outside"].parent / "track01.bin"
    raw = bytearray(2352)
    raw[16:16 + 0x200] = _make_ipbin()
    outside.write_bytes(bytes(raw))
    gdi = layout["folder"] / "game.gdi"
    gdi.write_text(f'1\n1 0 4 2352 "{outside}" 0\n')

    opened.clear()   # only what the probe opens counts
    result = detect_platform(str(gdi))

    assert result.platform is None
    assert not _was_opened(opened, outside)


def test_a_track_symlinked_out_of_the_volume_is_refused(layout, opened):
    (layout["folder"] / "x.bin").symlink_to(layout["outside"])
    cue = layout["folder"] / "game.cue"
    cue.write_text('FILE "x.bin" BINARY\n  TRACK 01 MODE1/2352\n')

    opened.clear()   # only what the probe opens counts
    result = detect_platform(str(cue))

    assert result.platform is None
    assert not _was_opened(opened, layout["outside"])


def test_the_probe_and_the_cache_list_agree_on_tracks(layout):
    from services.tools import registry

    cue = layout["folder"] / "game.cue"
    cue.write_text('FILE "../../outside/x.bin" BINARY\n  TRACK 01 MODE1/2352\n')

    # Refused for the probe, and not a cache dependency either.
    assert registry.get("chdman").header_reads(str(cue)) == []


def test_a_track_inside_is_still_followed(layout):
    _saturn_track(layout["folder"] / "x.bin")
    cue = layout["folder"] / "game.cue"
    cue.write_text('FILE "x.bin" BINARY\n  TRACK 01 MODE1/2352\n')

    assert detect_platform(str(cue)).platform == "saturn"


@pytest.mark.parametrize("quoted", [True, False])
def test_a_gdi_naming_a_dotdot_track_is_never_opened(layout, opened, quoted):
    from utils.sheet_text import resolve_track

    # A Dreamcast track one folder up, inside the Volume, so only the ".."
    # rule (not the Volume check) can refuse it.
    outside = layout["volume"] / "x.bin"
    raw = bytearray(2352)
    raw[16:16 + 0x200] = _make_ipbin()
    outside.write_bytes(bytes(raw))
    name = '"../x.bin"' if quoted else "../x.bin"
    gdi = layout["folder"] / "game.gdi"
    gdi.write_text(f"1\n1 0 4 2352 {name} 0\n")

    with pytest.raises(ValueError):
        resolve_track(layout["folder"], "../x.bin")
    opened.clear()   # only what the probe opens counts
    result = detect_platform(str(gdi))

    assert result.platform is None
    assert not _was_opened(opened, outside)
