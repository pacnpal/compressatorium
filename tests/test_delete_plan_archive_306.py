"""Deleting an archive Source never takes anything else with it (#306).

Fail closed (review r9 C1): an archive may be deleted only when every member
belongs to the Image that was converted, meaning its own file, the tracks its
sheet names (through ``utils.sheet_text.resolve_track``) and its split parts.
Any other member, whatever its type, keeps the archive. There is no list of
harmless sidecars.
"""
from __future__ import annotations

import zipfile
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from utils.delete_plan import build_delete_plan

_CUE = (
    'FILE "Game (Track 1).bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n'
    'FILE "Game (Track 2).bin" BINARY\n  TRACK 02 AUDIO\n    INDEX 01 00:00:00\n'
)


def _zip(path, members):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def _errors(source):
    return build_delete_plan(source)["errors"]


@pytest.mark.parametrize("extra", [
    "Other.nsp",                              # another disc Image
    "Game.gba",                               # a cartridge ROM
    "Other.bin",                              # a loose raw image
    "Other.zip",                              # a nested archive
    "BLES00001/PS3_GAME/USRDIR/EBOOT.BIN",    # a PS3 disc folder
    "readme.txt",                             # notes: no harmless sidecars
    "__MACOSX/._Game.iso",                    # not even junk
])
def test_any_other_member_keeps_the_archive(tmp_path, extra):
    pack = _zip(tmp_path / "pack.zip", {"Game.iso": b"x" * 64, extra: b"y"})

    errors = _errors(f"{pack}::Game.iso")

    assert any(extra in error for error in errors), errors


def test_an_image_alone_is_deleted(tmp_path):
    pack = _zip(tmp_path / "pack.zip", {"Game.iso": b"x" * 64})

    assert _errors(f"{pack}::Game.iso") == []


def test_a_cue_and_the_tracks_it_names_are_deleted(tmp_path):
    pack = _zip(tmp_path / "cd.zip", {
        "Game.cue": _CUE,
        "Game (Track 1).bin": b"1" * 64,
        "Game (Track 2).bin": b"2" * 64,
    })

    assert _errors(f"{pack}::Game.cue") == []


def test_a_cue_set_in_a_folder_is_deleted(tmp_path):
    pack = _zip(tmp_path / "cd.zip", {
        "disc/": b"",
        "disc/Game.cue": _CUE,
        "disc/Game (Track 1).bin": b"1" * 64,
        "disc/Game (Track 2).bin": b"2" * 64,
    })

    assert _errors(f"{pack}::disc/Game.cue") == []


def test_a_bin_the_cue_does_not_name_keeps_the_archive(tmp_path):
    pack = _zip(tmp_path / "cd.zip", {
        "Game.cue": _CUE,
        "Game (Track 1).bin": b"1" * 64,
        "Game (Track 2).bin": b"2" * 64,
        "Bonus.bin": b"3" * 64,
    })

    errors = _errors(f"{pack}::Game.cue")

    assert any("Bonus.bin" in error for error in errors), errors


def test_the_summary_names_the_first_few_extra_members(tmp_path):
    pack = _zip(tmp_path / "pack.zip", {
        "Game.iso": b"x", "a.txt": b"1", "b.txt": b"2", "c.txt": b"3", "d.txt": b"4",
    })

    (error,) = [e for e in _errors(f"{pack}::Game.iso") if "a.txt" in e]

    assert "b.txt" in error and "c.txt" in error and "d.txt" not in error
    assert "..." in error


@pytest.mark.asyncio
async def test_the_web_batch_refuses_it_too(tmp_path, monkeypatch):
    """The second layer: a web batch with delete-on-verify is refused, naming
    the extra member."""
    from app.models import BatchJobCreateRequest, ConversionMode, DuplicateAction
    from app.routes import convert as convert_routes

    monkeypatch.setattr(convert_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "data_mount_root", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "max_queue_depth", 0)
    monkeypatch.setattr(convert_routes.job_manager, "get_queue_depth", lambda: 0)
    monkeypatch.setattr(
        convert_routes.job_manager, "create_jobs_atomic", AsyncMock(return_value=[]),
    )
    pack = _zip(tmp_path / "pack.zip", {"Game.iso": b"x" * 64, "Game.gba": b"rom"})

    with pytest.raises(HTTPException) as exc:
        await convert_routes.create_batch_jobs(BatchJobCreateRequest(
            file_paths=[f"{pack}::Game.iso"],
            mode=ConversionMode.CREATEDVD,
            duplicate_action=DuplicateAction.SKIP,
            delete_on_verify=True,
        ))

    assert exc.value.status_code == 400
    assert "Game.gba" in exc.value.detail
    assert pack.exists()


# --------------------------------------------------------------------------- #
# Unsafe member names keep the archive and create nothing (codex 4577029535)
# --------------------------------------------------------------------------- #


def _escape_name():
    import uuid

    return f"../outside-{uuid.uuid4().hex}"


def _symlink_entry(zf, name, target):
    info = zipfile.ZipInfo(name)
    info.external_attr = 0o120777 << 16
    zf.writestr(info, target)


@pytest.mark.parametrize("kind", ["dotdot", "absolute", "symlink"])
def test_an_unsafe_member_name_keeps_the_archive_and_writes_nothing(
    tmp_path, monkeypatch, kind,
):
    import os
    import tempfile

    from config import settings

    app_temp = tmp_path / "app-temp"
    monkeypatch.setattr(settings, "temp_dir", str(app_temp))
    escape = _escape_name()
    pack = tmp_path / "pack.zip"
    with zipfile.ZipFile(pack, "w") as zf:
        zf.writestr("Game.iso", b"x" * 64)
        if kind == "dotdot":
            zf.writestr(escape, b"y")
            bad = escape
        elif kind == "absolute":
            bad = f"/{escape[3:]}"
            zf.writestr(bad, b"y")
        else:
            bad = "link"
            _symlink_entry(zf, bad, "/etc/passwd")
    before = set(os.listdir(tempfile.gettempdir()))

    errors = _errors(f"{pack}::Game.iso")

    assert any(bad.lstrip("/") in error for error in errors), errors
    # Nothing was created from a member name, inside or outside the temp dir.
    landed = os.path.join(tempfile.gettempdir(), escape[3:])
    assert not os.path.exists(landed)
    # Where an escape from the app temp dir's extraction folder would land.
    assert not (app_temp / escape[3:]).exists()
    assert not os.path.exists(os.path.join(str(tmp_path), escape[3:]))
    assert set(os.listdir(tempfile.gettempdir())) - before == set()


@pytest.mark.asyncio
async def test_the_web_batch_refuses_an_archive_with_an_unsafe_member(tmp_path, monkeypatch):
    from app.models import BatchJobCreateRequest, ConversionMode, DuplicateAction
    from app.routes import convert as convert_routes

    monkeypatch.setattr(convert_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "data_mount_root", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "max_queue_depth", 0)
    monkeypatch.setattr(convert_routes.job_manager, "get_queue_depth", lambda: 0)
    monkeypatch.setattr(
        convert_routes.job_manager, "create_jobs_atomic", AsyncMock(return_value=[]),
    )
    escape = _escape_name()
    pack = _zip(tmp_path / "pack.zip", {"Game.iso": b"x" * 64, escape: b"y"})

    with pytest.raises(HTTPException) as exc:
        await convert_routes.create_batch_jobs(BatchJobCreateRequest(
            file_paths=[f"{pack}::Game.iso"],
            mode=ConversionMode.CREATEDVD,
            duplicate_action=DuplicateAction.SKIP,
            delete_on_verify=True,
        ))

    assert exc.value.status_code == 400
    assert escape in exc.value.detail
    assert not (tmp_path.parent / escape[3:]).exists()


# --------------------------------------------------------------------------- #
# Review r10: the layout lives under the app's temp dir; duplicate and
# non-canonical names; an ambiguous sheet
# --------------------------------------------------------------------------- #


def test_the_membership_check_works_with_an_unwritable_system_temp(tmp_path, monkeypatch):
    """r10 I1: a read-only root (system temp unwritable) must not refuse every
    archive delete; the check works under the app's own temp dir."""
    import os
    import tempfile

    from config import settings

    blocked = tmp_path / "ro-tmp"
    blocked.mkdir()
    os.chmod(blocked, 0o500)
    monkeypatch.setenv("TMPDIR", str(blocked))
    monkeypatch.setattr(tempfile, "tempdir", str(blocked))
    monkeypatch.setattr(settings, "temp_dir", str(tmp_path / "app-temp"))
    pack = _zip(tmp_path / "cd.zip", {
        "Game.cue": _CUE,
        "Game (Track 1).bin": b"1" * 64,
        "Game (Track 2).bin": b"2" * 64,
    })
    try:
        assert _errors(f"{pack}::Game.cue") == []
        assert os.listdir(blocked) == []
    finally:
        os.chmod(blocked, 0o700)


def test_a_duplicated_member_name_keeps_the_archive(tmp_path):
    """r10 M1: two entries named Game.iso are two different files; only one
    can be the converted Image."""
    import warnings

    pack = tmp_path / "pack.zip"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # zipfile warns on the duplicate
        with zipfile.ZipFile(pack, "w") as zf:
            zf.writestr("Game.iso", b"one")
            zf.writestr("Game.iso", b"two")

    errors = _errors(f"{pack}::Game.iso")

    assert any("Game.iso" in error for error in errors), errors


@pytest.mark.parametrize("other", ["./Game.iso", "sub/../Game.iso", "a//Game.iso"])
def test_a_non_canonical_member_name_keeps_the_archive(tmp_path, other):
    """r10 M1: such a name is refused, never collapsed onto the Image."""
    pack = _zip(tmp_path / "pack.zip", {"Game.iso": b"one", other: b"two"})

    errors = _errors(f"{pack}::Game.iso")

    assert any(other in error for error in errors), errors


def test_a_sheet_whose_spellings_match_two_members_keeps_the_archive(tmp_path):
    """r10 M2: the UTF-8 and Latin-1 readings of one track name match two
    different members; chdman opens only one, so the sheet is ambiguous."""
    utf8_name = "Café.bin"
    latin1_name = utf8_name.encode("utf-8").decode("latin-1")  # "CafÃ©.bin"
    cue = f'FILE "{utf8_name}" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n'
    pack = _zip(tmp_path / "cd.zip", {
        "Game.cue": cue.encode("utf-8"),
        utf8_name: b"1" * 64,
        latin1_name: b"2" * 64,
    })

    errors = _errors(f"{pack}::Game.cue")

    assert errors, "an ambiguous sheet must keep the archive"


def test_a_symlink_named_as_an_owned_track_keeps_the_archive(tmp_path):
    """review r11 M2: the symlink rule matters when the link has a name the
    Image would own; without it, this cue set would be deleted."""
    pack = tmp_path / "cd.zip"
    with zipfile.ZipFile(pack, "w") as zf:
        zf.writestr("Game.cue", _CUE)
        zf.writestr("Game (Track 1).bin", b"1" * 64)
        _symlink_entry(zf, "Game (Track 2).bin", "/etc/passwd")

    errors = _errors(f"{pack}::Game.cue")

    assert any("Game (Track 2).bin" in error for error in errors), errors


@pytest.mark.parametrize(("members", "why"), [
    ({"Game.iso": b"x", "../outside-reason": b"y"}, "unsafe member name ../outside-reason"),
    ({"Game.iso": b"x", "readme.txt": b"y"}, "also holds readme.txt"),
])
def test_the_summary_says_why_the_archive_was_kept(tmp_path, members, why):
    """review r11 M4."""
    pack = _zip(tmp_path / "pack.zip", members)

    errors = _errors(f"{pack}::Game.iso")

    assert any(why in error for error in errors), errors


def test_the_summary_names_a_duplicate_member(tmp_path):
    import warnings

    pack = tmp_path / "pack.zip"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with zipfile.ZipFile(pack, "w") as zf:
            zf.writestr("Game.iso", b"one")
            zf.writestr("Game.iso", b"two")

    errors = _errors(f"{pack}::Game.iso")

    assert any("duplicate member Game.iso" in error for error in errors), errors


def test_the_summary_names_an_ambiguous_sheet_spelling(tmp_path):
    utf8_name = "Café.bin"
    latin1_name = utf8_name.encode("utf-8").decode("latin-1")
    cue = f'FILE "{utf8_name}" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n'
    pack = _zip(tmp_path / "cd.zip", {
        "Game.cue": cue.encode("utf-8"), utf8_name: b"1", latin1_name: b"2",
    })

    errors = _errors(f"{pack}::Game.cue")

    assert any("ambiguous sheet spelling" in error for error in errors), errors
