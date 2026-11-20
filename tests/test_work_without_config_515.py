"""Work out of the box (#515): detect what can be found, default what is safe,
and say what is missing instead of hiding it.

* A tool program the image installs at a fixed path (``/usr/bin/chdman``) that
  isn't there, with its ``*_PATH`` unset, is looked up on ``PATH`` by name, so
  a Homebrew or distro install works with no setting. A path the operator set
  is never replaced.
* ``7z`` missing but ``7zz`` (the newer 7-Zip package's name) there: use it.
* ``GET /api/tools`` says *why* each unavailable tool can't run, so the UI
  shows the tool with its one-line fix instead of hiding it.
* A folder Library with two folders for one Platform places in the one that
  already holds the most Images, marked ``guessed``, instead of refusing.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import config
from config import Settings, settings


def _executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


# ---- binaries found on PATH ------------------------------------------------


def test_a_missing_default_binary_is_found_on_path(tmp_path, monkeypatch):
    found = _executable(tmp_path / "chdman")
    monkeypatch.setenv("PATH", str(tmp_path))

    assert config.detected_binary(str(tmp_path / "nowhere" / "chdman")) == str(found)


def test_a_default_binary_found_nowhere_keeps_the_default(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    default = str(tmp_path / "nowhere" / "chdman")

    assert config.detected_binary(default) == default


def test_a_default_binary_that_is_there_is_kept(tmp_path, monkeypatch):
    there = _executable(tmp_path / "chdman")
    elsewhere = tmp_path / "path"
    elsewhere.mkdir()
    _executable(elsewhere / "chdman")
    monkeypatch.setenv("PATH", str(elsewhere))

    assert config.detected_binary(str(there)) == str(there)


def test_settings_use_path_for_an_unset_missing_binary(tmp_path, monkeypatch):
    if os.path.exists("/usr/local/bin/maxcso"):
        pytest.skip("the image's maxcso is installed here")
    found = _executable(tmp_path / "maxcso")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("MAXCSO_PATH", raising=False)

    assert Settings(data_dir=str(tmp_path)).maxcso_path == str(found)


def test_a_binary_path_the_operator_set_is_never_replaced(tmp_path, monkeypatch):
    _executable(tmp_path / "maxcso")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("MAXCSO_PATH", "/opt/mine/maxcso")

    assert Settings(data_dir=str(tmp_path)).maxcso_path == "/opt/mine/maxcso"


def test_7zz_stands_in_for_a_missing_7z(tmp_path, monkeypatch):
    found = _executable(tmp_path / "7zz")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("SEVENZIP_PATH", raising=False)

    assert Settings(data_dir=str(tmp_path)).sevenzip_path == str(found)


def test_7z_on_path_is_kept(tmp_path, monkeypatch):
    _executable(tmp_path / "7z")
    _executable(tmp_path / "7zz")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("SEVENZIP_PATH", raising=False)

    assert Settings(data_dir=str(tmp_path)).sevenzip_path == "7z"


# ---- the tool list says why ------------------------------------------------


@pytest.mark.asyncio
async def test_the_tool_list_says_why_a_tool_is_unavailable(tmp_path, monkeypatch):
    from services.tools import binaries, not_ready_message, registry

    from app.routes import info as info_routes

    tool = registry.get("cso")
    monkeypatch.setattr(tool._service, "maxcso_path", str(tmp_path / "maxcso-gone"))
    binaries.forget_binary_probes()

    tools = await info_routes.list_tools()

    assert "cso" in tools["unavailable"]
    assert tools["reasons"]["cso"] == not_ready_message(tool)
    assert "MAXCSO_PATH" in tools["reasons"]["cso"]
    assert set(tools["reasons"]) == set(tools["unavailable"])


# ---- folder Library: a best guess, not a refusal ---------------------------


@pytest.fixture(name="vol")
def _vol(tmp_path, monkeypatch):
    from services import platform_detect

    monkeypatch.setattr(settings, "chd_volumes", str(tmp_path))
    platform_detect.clear_cache()
    return tmp_path


@pytest.fixture(name="db")
def _db_fixture(tmp_path: Path):
    from services import db as _db
    from services import libraries
    from services.romm import settings as romm_settings

    if _db.engine is not None:
        _db.engine.dispose()
    _db.init_engine(str(tmp_path / "compressatorium.db"), create_schema=True)
    romm_settings.reset_for_tests()
    libraries.reset_for_tests()
    yield
    libraries.reset_for_tests()
    romm_settings.reset_for_tests()
    if _db.engine is not None:
        _db.engine.dispose()
    _db.engine = None
    _db.SessionLocal = None


def _library(vol: Path):
    from services import libraries

    (vol / "ROMs").mkdir(exist_ok=True)
    library = libraries.register(str(vol / "ROMs"))
    libraries.update(library.id, layout="esde")
    return libraries.get(library.id)


def _images(folder: Path, count: int) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for n in range(count):
        (folder / f"Game {n}.iso").write_bytes(b"x")


def test_two_folders_for_one_platform_place_in_the_fuller_one(vol, db):
    library = _library(vol)
    _images(vol / "ROMs" / "ps2", 1)
    _images(vol / "ROMs" / "playstation2", 3)

    placed = library.placement("ps2")

    assert placed.folder == str(vol / "ROMs" / "playstation2")
    assert placed.guessed is True
    assert placed.exists is True


def test_a_tie_is_broken_the_same_way_every_time(vol, db):
    library = _library(vol)
    (vol / "ROMs" / "ps2").mkdir()
    (vol / "ROMs" / "playstation2").mkdir()

    first = library.placement("ps2")

    assert first.guessed is True
    assert library.placement("ps2").folder == first.folder


def test_one_folder_for_a_platform_is_not_a_guess(vol, db):
    library = _library(vol)
    (vol / "ROMs" / "ps2").mkdir()

    assert library.placement("ps2").guessed is False


def test_the_placements_list_marks_a_guess(vol, db):
    from routes.libraries import _placements

    library = _library(vol)
    _images(vol / "ROMs" / "ps2", 2)
    _images(vol / "ROMs" / "playstation2", 1)

    rows = {row["platform"]: row for row in _placements(library)}

    assert rows["ps2"]["folder"] == "ps2"
    assert rows["ps2"]["guessed"] is True
    assert rows["ps2"]["error"] is None
