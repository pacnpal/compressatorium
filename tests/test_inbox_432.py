"""Inbox (#432): archives, DAT names and taken names.

An arriving zip or 7z is unpacked under the archive limits before its Platform
is worked out; each Image in it is then taken like a loose file, and the
archive moves to ``processed/`` once every one of them is placed. Where a DAT
is loaded, each Image is Recognized before it is placed and takes its
top-ranked DAT name, by the same rule the rename to DAT name uses. Two DATs
the ranking puts level that name it differently hold it with a Finding until
the ranking orders them. A name already in the Library holds it with a
Finding; nothing is overwritten.

The tick is called directly, with a fake clock and real files.
"""

from __future__ import annotations

import hashlib
import random
import zipfile
from pathlib import Path

import pytest
from services import findings

from config import settings

from . import test_inbox_431 as inbox_431

_vol = inbox_431._vol
_db_fixture = inbox_431._db_fixture
_queue = inbox_431._queue
_library = inbox_431._library
_inbox = inbox_431._inbox
_drop = inbox_431._drop
_poll = inbox_431._poll
_inbox_findings = inbox_431._inbox_findings
_verifies = inbox_431._verifies


async def _polls(box, count: int = 8, start: int = 0) -> dict:
    """*count* polls one second apart (the Inboxes here settle at once)."""
    result: dict = {}
    for now in range(start, start + count):
        result = await _poll(box, now=now)
    return result


def _zip(path: Path, members: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def _seven_zip(path: Path, members: dict[str, bytes]) -> Path:
    import py7zr

    path.parent.mkdir(parents=True, exist_ok=True)
    with py7zr.SevenZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(data, name)
    return path


def _bytes(seed: str, size: int = 64 * 1024) -> bytes:
    """*size* bytes that don't compress (an archive of them stays under the
    expansion cap), the same for the same *seed*."""
    return random.Random(seed).randbytes(size)


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


async def _dat(name: str, games: dict[str, str]) -> str:
    """Import a DAT naming each SHA1 in *games* (``{game name: sha1}``)."""
    from services.dat_store import dat_store

    body = "".join(
        f'<game name="{game}"><rom name="{game}.iso" size="4" sha1="{sha1}"/></game>'
        for game, sha1 in games.items()
    )
    xml = (
        '<?xml version="1.0"?><datafile><header>'
        f"<name>{name}</name><description>{name}</description></header>"
        f"{body}</datafile>"
    )
    result = await dat_store.import_dat(xml)
    return result["id"]


def _stage_folders(root: Path) -> list[Path]:
    return [p for p in root.rglob(".compressatorium-stage")]


# ---- archives --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_zip_is_unpacked_its_image_placed_and_the_zip_moves_to_processed(
    vol, db, monkeypatch,
):
    _verifies(monkeypatch)
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    rom = _bytes("rom")
    archive = _zip(vol / "Downloads" / "gba" / "Game.zip", {"Game.gba": rom})

    await _polls(box)

    assert (vol / "ROMs" / "gba" / "Game.gba").read_bytes() == rom
    assert not (vol / "ROMs" / "gba" / "Game.zip").exists()
    assert not archive.exists()
    assert (vol / "Downloads" / "processed" / "gba" / "Game.zip").exists()
    # The unpacked copy was scratch: nothing of it is left in the Inbox.
    assert not (vol / "Downloads" / "processed" / "gba" / "Game.gba").exists()
    assert _stage_folders(vol / "Downloads") == []
    assert _inbox_findings() == []


@pytest.mark.asyncio
async def test_a_7z_is_unpacked_too(vol, db, monkeypatch):
    _verifies(monkeypatch)
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    rom = _bytes("seven")
    archive = _seven_zip(vol / "Downloads" / "gba" / "Game.7z", {"Game.gba": rom})

    await _polls(box)

    assert (vol / "ROMs" / "gba" / "Game.gba").read_bytes() == rom
    assert not archive.exists()
    assert (vol / "Downloads" / "processed" / "gba" / "Game.7z").exists()
    assert _stage_folders(vol / "Downloads") == []


@pytest.mark.asyncio
async def test_an_archives_platform_is_worked_out_from_what_it_holds(vol, db, monkeypatch):
    """Unpacked before the Platform is worked out: a zip dropped at the top of
    the Inbox whose Image sits in a Platform folder inside it is placed."""
    _verifies(monkeypatch)
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    rom = _bytes("inside")
    archive = _zip(vol / "Downloads" / "Bundle.zip", {"gba/Game.gba": rom})

    await _polls(box)

    assert (vol / "ROMs" / "gba" / "Game.gba").read_bytes() == rom
    assert not archive.exists()
    assert (vol / "Downloads" / "processed" / "Bundle.zip").exists()


@pytest.mark.asyncio
async def test_an_archive_moves_only_once_every_image_in_it_is_placed(vol, db, monkeypatch):
    """One Image placed, one Unsorted: the archive stays in the Inbox with a
    Finding naming the one that couldn't be placed."""
    _verifies(monkeypatch)
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    rom = _bytes("good")
    archive = _zip(
        vol / "Downloads" / "Bundle.zip",
        {"gba/Good.gba": rom, "Mystery.iso": _bytes("mystery", 4096)},
    )

    await _polls(box)
    before = sorted(str(p) for p in (vol / "ROMs").rglob("*"))
    held = _inbox_findings()
    # Later ticks change nothing.
    await _polls(box, count=4, start=20)

    assert (vol / "ROMs" / "gba" / "Good.gba").read_bytes() == rom
    assert archive.exists()
    assert not (vol / "Downloads" / "processed" / "Bundle.zip").exists()
    [finding] = held
    assert finding["kind"] == findings.INBOX_UNSORTED
    assert finding["path"] == str(archive.resolve())
    assert "Mystery.iso" in finding["reason"]
    assert sorted(str(p) for p in (vol / "ROMs").rglob("*")) == before
    assert _inbox_findings() == held


@pytest.mark.asyncio
async def test_an_archive_over_the_limits_stays_unpacked_with_a_finding(vol, db, monkeypatch):
    _verifies(monkeypatch)
    monkeypatch.setattr(settings, "archive_max_total_size", 0)
    monkeypatch.setattr(settings, "archive_inbox_max_total_size", 1024)
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    archive = _zip(vol / "Downloads" / "gba" / "Game.zip", {"Game.gba": _bytes("big", 4096)})

    await _polls(box)

    assert archive.exists()
    assert not (vol / "ROMs" / "gba").exists()
    assert _stage_folders(vol / "Downloads") == []
    [finding] = _inbox_findings()
    assert finding["kind"] == findings.INBOX_FAILED
    assert "CHD_ARCHIVE_INBOX_MAX_TOTAL_SIZE" in finding["reason"]


@pytest.mark.asyncio
async def test_an_archive_with_an_unsafe_member_name_is_not_unpacked(vol, db, monkeypatch):
    """Members are validated before anything is written: a name with a
    backslash (a Windows path the gate's ``/`` split can't see) refuses the
    whole archive."""
    _verifies(monkeypatch)
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    archive = _zip(
        vol / "Downloads" / "gba" / "Game.zip",
        {"Game.gba": _bytes("ok"), "..\\evil.gba": _bytes("evil")},
    )

    await _polls(box)

    assert archive.exists()
    assert not (vol / "ROMs" / "gba").exists()
    assert not (vol / "Downloads" / "evil.gba").exists()
    assert _stage_folders(vol / "Downloads") == []
    [finding] = _inbox_findings()
    assert finding["kind"] == findings.INBOX_FAILED
    assert "evil.gba" in finding["reason"]


@pytest.mark.asyncio
async def test_an_archive_inside_an_archive_is_not_unpacked(vol, db, monkeypatch):
    _verifies(monkeypatch)
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    inner = vol / "inner.zip"
    _zip(inner, {"Game.gba": _bytes("inner")})
    archive = _zip(vol / "Downloads" / "gba" / "Outer.zip", {"Inner.zip": inner.read_bytes()})

    await _polls(box)

    assert archive.exists()
    assert not (vol / "ROMs" / "gba").exists()
    [finding] = _inbox_findings()
    assert finding["path"] == str(archive.resolve())
    assert "archive inside an archive" in finding["reason"]


# ---- DAT names -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_recognized_image_takes_its_dat_name(vol, db):
    disc = _bytes("disc")
    await _dat("Sony - PlayStation 2 (Redump)", {"Great Game (USA)": _sha1(disc)})
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    game = _drop(vol / "Downloads" / "ps2" / "great_game.iso", disc)

    await _polls(box)

    assert (vol / "ROMs" / "ps2" / "Great Game (USA).iso").read_bytes() == disc
    assert not (vol / "ROMs" / "ps2" / "great_game.iso").exists()
    assert not game.exists()
    # The original keeps its own name in processed/.
    assert (vol / "Downloads" / "processed" / "ps2" / "great_game.iso").exists()
    assert _inbox_findings() == []


@pytest.mark.asyncio
async def test_an_image_in_an_archive_takes_its_dat_name(vol, db, monkeypatch):
    _verifies(monkeypatch)
    rom = _bytes("cart")
    await _dat("Nintendo - Game Boy Advance (No-Intro)", {"Cart Game (Europe)": _sha1(rom)})
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    _zip(vol / "Downloads" / "gba" / "cart.zip", {"cart.gba": rom})

    await _polls(box)

    assert (vol / "ROMs" / "gba" / "Cart Game (Europe).gba").read_bytes() == rom
    assert (vol / "Downloads" / "processed" / "gba" / "cart.zip").exists()


@pytest.mark.asyncio
async def test_an_unrecognized_image_keeps_its_own_name(vol, db):
    await _dat("Sony - PlayStation 2 (Redump)", {"Other Game": _sha1(b"other")})
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    _drop(vol / "Downloads" / "ps2" / "Homebrew.iso", _bytes("homebrew"))

    await _polls(box)

    assert (vol / "ROMs" / "ps2" / "Homebrew.iso").exists()


@pytest.mark.asyncio
async def test_an_unranked_conflict_is_held_until_the_ranking_orders_it(vol, db):
    """Two DATs the ranking puts level name it differently: it is neither
    guessed at nor placed. Once the user ranks them, the next poll places it
    under the top-ranked DAT's name."""
    from services.dat_store import dat_store

    disc = _bytes("contested")
    first = await _dat("Set A", {"Name From A": _sha1(disc)})
    second = await _dat("Set B", {"Name From B": _sha1(disc)})
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    game = _drop(vol / "Downloads" / "ps2" / "download.iso", disc)

    await _polls(box)

    assert game.exists()
    assert not (vol / "ROMs" / "ps2").exists()
    [finding] = _inbox_findings()
    assert finding["kind"] == findings.INBOX_CONFLICT
    assert "Name From A" in finding["reason"] and "Name From B" in finding["reason"]
    assert "rank" in finding["reason"]

    await dat_store.set_ranking([second, first])
    await _polls(box, start=20)

    assert (vol / "ROMs" / "ps2" / "Name From B.iso").read_bytes() == disc
    assert not game.exists()
    assert _inbox_findings() == []


# ---- taken names -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_dat_name_already_in_the_library_is_held_and_never_replaced(vol, db):
    disc = _bytes("taken")
    await _dat("Sony - PlayStation 2 (Redump)", {"Great Game (USA)": _sha1(disc)})
    library = _library(vol)
    existing = _drop(vol / "ROMs" / "ps2" / "Great Game (USA).iso", b"the Library's own")
    box = _inbox(vol, library, settle=0)
    game = _drop(vol / "Downloads" / "ps2" / "great_game.iso", disc)

    await _polls(box)

    assert existing.read_bytes() == b"the Library's own"
    assert game.read_bytes() == disc
    assert sorted(p.name for p in (vol / "ROMs" / "ps2").iterdir()) == ["Great Game (USA).iso"]
    [finding] = _inbox_findings()
    assert finding["kind"] == findings.INBOX_FAILED
    assert "Great Game (USA).iso is already in the Library" in finding["reason"]


@pytest.mark.asyncio
async def test_a_conversion_writes_to_the_dat_name(vol, db, queue):
    disc = _bytes("convert")
    await _dat("Sony - PlayStation 2 (Redump)", {"Great Game (USA)": _sha1(disc)})
    library = _library(vol)
    await inbox_431._ps2_rule(library)
    box = _inbox(vol, library, settle=0)
    game = _drop(vol / "Downloads" / "ps2" / "great_game.iso", disc)

    await _polls(box, count=4)

    [job] = queue.submitted
    assert job["output_paths"] == {
        str(game.resolve()): str(vol / "ROMs" / "ps2" / "Great Game (USA).cso"),
    }


@pytest.mark.asyncio
async def test_a_conversions_dat_name_already_in_the_library_is_held(vol, db, queue):
    disc = _bytes("convert-taken")
    await _dat("Sony - PlayStation 2 (Redump)", {"Great Game (USA)": _sha1(disc)})
    library = _library(vol)
    await inbox_431._ps2_rule(library)
    existing = _drop(vol / "ROMs" / "ps2" / "Great Game (USA).cso", b"already converted")
    box = _inbox(vol, library, settle=0)
    game = _drop(vol / "Downloads" / "ps2" / "great_game.iso", disc)

    await _polls(box, count=4)

    assert queue.submitted == []
    assert existing.read_bytes() == b"already converted"
    assert game.exists()
    [finding] = _inbox_findings()
    assert finding["kind"] == findings.INBOX_FAILED
    assert "Great Game (USA).cso is already in the Library" in finding["reason"]


# ---- jobs and cleanup ------------------------------------------------------


@pytest.mark.asyncio
async def test_unpacking_and_recognizing_run_as_jobs(vol, db, monkeypatch):
    """Both are jobs you can see and cancel, counted under the conversion
    slots like an Inbox copy."""
    from models import ConversionMode
    from services.inbox import intake

    _verifies(monkeypatch)
    rom = _bytes("jobs")
    await _dat("Nintendo - Game Boy Advance (No-Intro)", {"Job Game": _sha1(rom)})
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    _zip(vol / "Downloads" / "gba" / "jobs.zip", {"jobs.gba": rom})

    await _polls(box)

    modes = {job.mode for job in intake.job_manager.jobs.values()}
    assert {
        ConversionMode.INBOX_UNPACK, ConversionMode.INBOX_RECOGNIZE, ConversionMode.INBOX_COPY,
    } <= modes
    assert (vol / "ROMs" / "gba" / "Job Game.gba").exists()


@pytest.mark.asyncio
async def test_what_was_unpacked_goes_when_its_archive_leaves_the_inbox(vol, db, monkeypatch):
    """An archive held for one of its Images, then taken out of the Inbox by
    hand: what was unpacked from it is removed and its Finding cleared."""
    _verifies(monkeypatch)
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    archive = _zip(vol / "Downloads" / "Bundle.zip", {"Mystery.iso": _bytes("left", 4096)})
    await _polls(box)
    assert _stage_folders(vol / "Downloads")
    assert _inbox_findings()

    archive.rename(vol / "Bundle.zip")
    await _polls(box, count=2, start=20)

    assert _stage_folders(vol / "Downloads") == []
    assert _inbox_findings() == []
