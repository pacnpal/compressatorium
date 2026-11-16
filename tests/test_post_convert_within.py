"""The disc ID of an archived set extracted outside the Volumes (``within``).

The job worker extracts an archived .cue/.gdi set into a temp dir it creates,
which is not a Volume. It passes that root to ``post_convert`` as ``within``,
the same value its job gate uses, so the extracted tracks are trusted there and
nowhere else. A track that is refused or unreadable records no disc ID, with a
warning naming the sheet, never silently.
"""
from __future__ import annotations

import asyncio
import builtins
import logging
from pathlib import Path

import pytest
from services.tools import chdman as chdman_mod
from services.tools import registry

from .test_disc_id import _make_iso


def _run(coro):
    return asyncio.run(coro)


def _ps1_bin(path: Path) -> Path:
    """A raw Mode 2 PS1 track whose SYSTEM.CNF names SCUS-94163."""
    cooked = _make_iso({"SYSTEM.CNF": b"BOOT = cdrom:\\SCUS_941.63;1\n"})
    raw = bytearray()
    for start in range(0, len(cooked), 2048):
        sector = bytearray(2352)
        sector[24:24 + 2048] = cooked[start:start + 2048]
        raw += sector
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(raw))
    return path


@pytest.fixture(name="env")
def _env(tmp_path, monkeypatch):
    from config import settings

    volume = tmp_path / "volume"
    temp = tmp_path / "job-temp" / "x1"     # the worker's extraction root
    volume.mkdir()
    temp.mkdir(parents=True)
    monkeypatch.setattr(settings, "chd_volumes", str(volume))

    # What post_convert records for the output (#352: the app's database,
    # not a tag in the CHD).
    embedded: list[tuple] = []

    async def _record(path, game_id, title):
        embedded.append((path, game_id, title))

    monkeypatch.setattr(chdman_mod, "_record_disc_id", _record)
    out = volume / "Game.chd"
    out.write_bytes(b"MComprHD")
    return {"volume": volume, "temp": temp, "out": out, "embedded": embedded}


def test_an_archived_cue_extracted_outside_the_volumes_is_tagged(env):
    _ps1_bin(env["temp"] / "game.bin")
    cue = env["temp"] / "game.cue"
    cue.write_text('FILE "game.bin" BINARY\n  TRACK 01 MODE2/2352\n')

    _run(registry.get("chdman").post_convert(
        str(cue), str(env["out"]), "createcd", within=[str(env["temp"])],
    ))

    assert [game_id for _p, game_id, _t in env["embedded"]] == ["SCUS-94163"]


def test_an_archived_cue_naming_a_dotdot_track_is_not_tagged_and_warns(
    env, monkeypatch, caplog,
):
    outside = _ps1_bin(env["temp"].parent / "x.bin")   # one folder above the root
    cue = env["temp"] / "game.cue"
    cue.write_text('FILE "../x.bin" BINARY\n  TRACK 01 MODE2/2352\n')
    opened: list[str] = []
    real_open = builtins.open

    def spy(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy)

    with caplog.at_level(logging.WARNING):
        _run(registry.get("chdman").post_convert(
            str(cue), str(env["out"]), "createcd", within=[str(env["temp"])],
        ))

    assert env["embedded"] == []
    assert not any(Path(p).resolve() == outside.resolve() for p in opened)
    assert any("game.cue" in r.getMessage() for r in caplog.records
               if r.levelno >= logging.WARNING)


def test_a_volume_cue_is_tagged_as_before(env):
    folder = env["volume"] / "ps1"
    _ps1_bin(folder / "game.bin")
    cue = folder / "game.cue"
    cue.write_text('FILE "game.bin" BINARY\n  TRACK 01 MODE2/2352\n')

    _run(registry.get("chdman").post_convert(str(cue), str(env["out"]), "createcd"))

    assert [game_id for _p, game_id, _t in env["embedded"]] == ["SCUS-94163"]


def test_without_within_an_extracted_cue_is_refused_with_a_warning(env, caplog):
    _ps1_bin(env["temp"] / "game.bin")
    cue = env["temp"] / "game.cue"
    cue.write_text('FILE "game.bin" BINARY\n  TRACK 01 MODE2/2352\n')

    with caplog.at_level(logging.WARNING):
        _run(registry.get("chdman").post_convert(str(cue), str(env["out"]), "createcd"))

    assert env["embedded"] == []
    assert any("game.cue" in r.getMessage() for r in caplog.records
               if r.levelno >= logging.WARNING)
