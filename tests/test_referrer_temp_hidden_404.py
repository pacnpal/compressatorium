"""A Referrer's temp file stays out of RomM's sight (#404).

Referrers write through ``atomic_write_bytes`` / ``write_new_bytes``: a
``.tmp-XXXXXXXX`` file beside the target, then a rename. RomM 5.4.1 indexes
dot-prefixed files, so in a RomM Library the temp file goes in the hidden
staging slot (``services.staging``, #398) instead. ``FakeRomm.scan_platform``
lists the Platform folder the way RomM would, from the write's ``guard`` (it
runs after the temp file is written and before the rename).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from config import settings
from services import db as _db
from services.referrers import atomic_write_bytes, write_new_bytes
from utils.junk import STAGE_DIR_NAME

from .fake_romm import FakeRomm


@pytest.fixture(name="vol")
def _vol(tmp_path: Path, monkeypatch):
    from services import libraries
    from services.romm import client as romm_client_module
    from services.romm import settings as romm_settings

    vol = tmp_path / "vol"
    for platform in (vol / "romm" / "ps2", vol / "roms" / "ps2", vol / "loose"):
        platform.mkdir(parents=True)
    monkeypatch.setattr(settings, "chd_volumes", str(vol))
    monkeypatch.setattr(settings, "data_mount_root", str(vol))
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "config"))
    if _db.engine is not None:
        _db.engine.dispose()
    _db.init_engine(str(tmp_path / "compressatorium.db"), create_schema=True)
    romm_settings.reset_for_tests()
    libraries.reset_for_tests()
    monkeypatch.setattr(
        type(romm_client_module.romm_client), "library_root",
        property(lambda _self: str((vol / "romm").resolve())),
    )
    libraries.register(str(vol / "roms"))
    yield vol
    libraries.reset_for_tests()
    romm_settings.reset_for_tests()
    if _db.engine is not None:
        _db.engine.dispose()
    _db.engine = None
    _db.SessionLocal = None


class _Scanner:
    """A guard that scans the Platform folder as RomM would, and notes whether
    a temp file exists in the hidden stage (so the window was real)."""

    def __init__(self, platform: Path):
        self.platform = platform
        self.seen: list[list[str]] = []
        self.temp_existed = False

    def __call__(self) -> None:
        self.seen.append(FakeRomm.scan_platform(self.platform))
        stage = self.platform / STAGE_DIR_NAME
        self.temp_existed = any(p.name.startswith(".tmp-") for p in stage.rglob("*"))


def test_atomic_write_in_a_romm_library_hides_the_temp_file(vol):
    platform = vol / "romm" / "ps2"
    target = platform / "Game.m3u"
    target.write_bytes(b"old\n")
    scan = _Scanner(platform)
    atomic_write_bytes(str(target), b"new\n", guard=scan)
    assert scan.temp_existed
    assert scan.seen == [["Game.m3u"]]
    assert target.read_bytes() == b"new\n"
    assert os.listdir(platform) == ["Game.m3u"]  # slot and stage folder gone


def test_write_new_in_a_romm_library_hides_the_temp_file(vol):
    platform = vol / "romm" / "ps2"
    target = platform / "Disc.m3u"
    scan = _Scanner(platform)
    write_new_bytes(str(target), b"a\n", guard=scan)
    assert scan.temp_existed
    assert scan.seen == [[]]
    assert target.read_bytes() == b"a\n"
    assert os.listdir(platform) == ["Disc.m3u"]


def test_a_refused_write_leaves_nothing_and_the_mode_is_kept(vol):
    platform = vol / "romm" / "ps2"
    target = platform / "Game.m3u"
    target.write_bytes(b"old\n")
    os.chmod(target, 0o640)

    def refuse() -> None:
        raise PermissionError("no")

    with pytest.raises(PermissionError):
        atomic_write_bytes(str(target), b"new\n", guard=refuse)
    assert target.read_bytes() == b"old\n"
    assert os.listdir(platform) == ["Game.m3u"]
    atomic_write_bytes(str(target), b"new\n")
    assert (target.stat().st_mode & 0o777) == 0o640


def test_write_new_never_replaces_in_a_romm_library(vol):
    platform = vol / "romm" / "ps2"
    target = platform / "Disc.m3u"
    target.write_bytes(b"mine\n")
    with pytest.raises(FileExistsError):
        write_new_bytes(str(target), b"other\n")
    assert target.read_bytes() == b"mine\n"
    assert os.listdir(platform) == ["Disc.m3u"]


def test_a_folder_library_keeps_the_temp_file_beside_the_target(vol):
    platform = vol / "roms" / "ps2"
    target = platform / "Game.m3u"
    target.write_bytes(b"old\n")
    seen: list[list[str]] = []
    atomic_write_bytes(str(target), b"new\n", guard=lambda: seen.append(os.listdir(platform)))
    assert any(name.startswith(".tmp-") for name in seen[0])
    assert STAGE_DIR_NAME not in seen[0]
    assert target.read_bytes() == b"new\n"


def test_outside_any_library_keeps_the_temp_file_beside_the_target(vol):
    loose = vol / "loose"
    seen: list[list[str]] = []
    write_new_bytes(str(loose / "x.m3u"), b"a\n", guard=lambda: seen.append(os.listdir(loose)))
    assert any(name.startswith(".tmp-") for name in seen[0])
    assert STAGE_DIR_NAME not in seen[0]


def test_the_one_time_backup_is_unchanged_in_a_romm_library(vol):
    from services.referrers.files import BACKUP_SUFFIX, _rewrite_locked

    platform = vol / "romm" / "ps2"
    target = platform / "Game.m3u"
    target.write_bytes(b"one\n")

    def read(path: str) -> bytes:
        return Path(path).read_bytes()

    _rewrite_locked(str(target), lambda _b: b"two\n", read)
    _rewrite_locked(str(target), lambda _b: b"three\n", read)
    backup = Path(str(target) + BACKUP_SUFFIX)
    assert backup.read_bytes() == b"one\n"
    assert target.read_bytes() == b"three\n"
    assert sorted(os.listdir(platform)) == sorted(["Game.m3u", backup.name])
