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


@pytest.fixture(autouse=True)
def _work_area(tmp_path, monkeypatch) -> Path:
    """The app's own work area (``CHD_TEMP_DIR``), where archives are
    unpacked: outside every Volume and the Inbox."""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(settings, "temp_dir", str(work))
    return work


def _unpack_dirs() -> list[Path]:
    return sorted(Path(settings.temp_dir).glob("inbox_unpack_*"))


def _stage_folders(root: Path) -> list[Path]:
    """What unpacking left: a hidden stage folder in the Inbox (there should
    never be one), or an unpack folder in the work area."""
    return [*root.rglob(".compressatorium-stage"), *_unpack_dirs()]


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


@pytest.mark.asyncio
@pytest.mark.parametrize("folder", ["arcade", "mame"])
async def test_an_arcade_romset_zip_is_placed_whole(vol, db, monkeypatch, folder):
    """On a Platform whose Image is the archive itself (an arcade romset),
    the zip is never unpacked: gated, then copied in as one file."""
    _verifies(monkeypatch)
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    members = {"pacman.6e": _bytes("6e", 4096), "pacman.6f": _bytes("6f", 4096)}
    archive = _zip(vol / "Downloads" / folder / "pacman.zip", members)
    data = archive.read_bytes()

    await _polls(box)

    placed = vol / "ROMs" / "arcade" / "pacman.zip"
    assert placed.read_bytes() == data
    assert not (vol / "ROMs" / "arcade" / "pacman.6e").exists()
    assert (vol / "Downloads" / "processed" / folder / "pacman.zip").exists()
    assert _stage_folders(vol / "Downloads") == []
    assert _inbox_findings() == []


@pytest.mark.asyncio
async def test_an_arcade_romset_zip_still_goes_through_the_gate(vol, db, monkeypatch):
    _verifies(monkeypatch)
    monkeypatch.setattr(settings, "archive_max_total_size", 0)
    monkeypatch.setattr(settings, "archive_inbox_max_total_size", 1024)
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    archive = _zip(vol / "Downloads" / "arcade" / "big.zip", {"big.rom": _bytes("big", 4096)})

    await _polls(box)

    assert archive.exists()
    assert not (vol / "ROMs" / "arcade").exists()
    [finding] = _inbox_findings()
    assert "CHD_ARCHIVE_INBOX_MAX_TOTAL_SIZE" in finding["reason"]


@pytest.mark.asyncio
async def test_a_disc_platforms_zip_is_still_unpacked(vol, db, monkeypatch):
    _verifies(monkeypatch)
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    disc = _bytes("psx-disc")
    archive = _zip(vol / "Downloads" / "psx" / "Game.zip", {"Game.iso": disc})

    await _polls(box)

    assert (vol / "ROMs" / "psx" / "Game.iso").read_bytes() == disc
    assert not (vol / "ROMs" / "psx" / "Game.zip").exists()
    assert not archive.exists()
    assert (vol / "Downloads" / "processed" / "psx" / "Game.zip").exists()


def test_the_platform_registry_says_which_platforms_keep_archives_whole():
    from services import platform_detect

    assert platform_detect.archive_is_image("arcade")
    assert platform_detect.archive_is_image(platform_detect.canonical_slug("fbneo"))
    assert not platform_detect.archive_is_image("psx")
    assert not platform_detect.archive_is_image("gba")
    assert not platform_detect.archive_is_image(None)


# ---- a restart midway --------------------------------------------------------


@pytest.mark.asyncio
async def test_an_image_placed_before_a_restart_counts_as_placed(vol, db, monkeypatch):
    """Unpacked, one Image placed, then the app restarts before the archive
    moved: unpacked again, the Image already in the Library is the same
    bytes, so it counts as placed with no Finding and the archive completes."""
    from services.inbox import intake

    _verifies(monkeypatch)
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    rom = _bytes("restart")
    archive = _zip(vol / "Downloads" / "gba" / "Game.zip", {"Game.gba": rom})
    placed = vol / "ROMs" / "gba" / "Game.gba"
    for now in range(8):
        await _poll(box, now=now)
        if placed.exists():
            break
    assert placed.exists() and archive.exists()

    intake.reset_for_tests()  # a restart: everything in memory is gone
    await _polls(box, start=20)

    assert placed.read_bytes() == rom
    assert not archive.exists()
    assert (vol / "Downloads" / "processed" / "gba" / "Game.zip").exists()
    assert _stage_folders(vol / "Downloads") == []
    assert _inbox_findings() == []


@pytest.mark.asyncio
async def test_a_different_image_under_the_same_name_is_still_held_after_a_restart(
    vol, db, monkeypatch,
):
    _verifies(monkeypatch)
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    other = _drop(vol / "ROMs" / "gba" / "Game.gba", _bytes("someone else's", 64 * 1024))
    archive = _zip(vol / "Downloads" / "gba" / "Game.zip", {"Game.gba": _bytes("mine")})

    await _polls(box)

    assert other.read_bytes() == _bytes("someone else's", 64 * 1024)
    assert archive.exists()
    [finding] = _inbox_findings()
    assert "Game.gba is already in the Library" in finding["reason"]


@pytest.mark.asyncio
async def test_a_loose_file_already_in_the_library_byte_for_byte_counts_as_placed(vol, db):
    disc = _bytes("same")
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    existing = _drop(vol / "ROMs" / "ps2" / "Game.iso", disc)
    game = _drop(vol / "Downloads" / "ps2" / "Game.iso", disc)

    await _polls(box)

    assert existing.read_bytes() == disc
    assert not game.exists()
    assert (vol / "Downloads" / "processed" / "ps2" / "Game.iso").read_bytes() == disc
    assert _inbox_findings() == []


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


# ---- review fixes (Codex on PR #453) -----------------------------------------


@pytest.mark.asyncio
async def test_an_archive_swapped_before_the_gate_is_gated_as_the_file_unpacked(
    vol, db, monkeypatch,
):
    """Codex 4608007766 (P1): an archive replaced after it settled, before
    the gate, must be gated as the very file that is unpacked. The
    replacement is over the cap, so nothing is written and it is held."""
    import os

    from services.inbox import intake

    _verifies(monkeypatch)
    monkeypatch.setattr(settings, "archive_max_total_size", 0)
    monkeypatch.setattr(settings, "archive_inbox_max_total_size", 2048)
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    archive = _zip(vol / "Downloads" / "gba" / "Game.zip", {"Game.gba": _bytes("ok", 1024)})
    bomb = _zip(vol / "bomb.zip", {"Game.gba": _bytes("big", 4096)})
    real = intake._identities
    swapped: list[bool] = []

    def swap_then_read(inbox, files):
        if not swapped and files == [str(archive.resolve())]:
            swapped.append(True)
            os.replace(bomb, archive)
        return real(inbox, files)

    monkeypatch.setattr(intake, "_identities", swap_then_read)
    unpack = intake._unpack_blocking
    unpacked_sizes: list[int] = []

    def recording(*args, **kwargs):
        slot, images = unpack(*args, **kwargs)
        unpacked_sizes.extend(os.path.getsize(i) for i in images)
        return slot, images

    monkeypatch.setattr(intake, "_unpack_blocking", recording)

    await _polls(box)

    assert swapped
    # Nothing over the cap was ever written, not even for a moment.
    assert all(size <= 2048 for size in unpacked_sizes)
    assert not (vol / "ROMs" / "gba").exists()
    assert _stage_folders(vol / "Downloads") == []
    [finding] = _inbox_findings()
    assert "CHD_ARCHIVE_INBOX_MAX_TOTAL_SIZE" in finding["reason"]


def _stored_zip(path: Path, name: str, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr(name, data)
    return path


def test_a_member_bigger_than_it_declares_is_stopped(tmp_path, monkeypatch):
    """Codex 4608007766, defence in depth: the bytes actually written are
    counted, and a member that yields more than its header declares is
    stopped there, never written whole."""
    import copy

    from services.archive import ArchiveSafetyRefused, archive_service

    archive = _stored_zip(tmp_path / "Game.zip", "Game.gba", _bytes("liar", 64 * 1024))
    real = zipfile.ZipFile.infolist

    def lying(self):
        infos = []
        for info in real(self):
            told = copy.copy(info)
            told.file_size = 1024
            infos.append(told)
        return infos

    monkeypatch.setattr(zipfile.ZipFile, "infolist", lying)
    dest = tmp_path / "out"
    dest.mkdir()

    with pytest.raises(ArchiveSafetyRefused, match="more than it declares"):
        archive_service.extract_members(str(archive), str(dest))

    written = dest / "Game.gba"
    assert not written.exists() or written.stat().st_size <= 1024


@pytest.mark.parametrize("limit", ["max_member", "max_total"])
@pytest.mark.parametrize("kind", ["zip", "7z"])
def test_unpacking_stops_at_the_cap_whatever_the_header_says(tmp_path, limit, kind):
    from services.archive import ArchiveSafetyRefused, archive_service

    data = _bytes("big", 64 * 1024)
    archive = (
        _stored_zip(tmp_path / "Game.zip", "Game.gba", data) if kind == "zip"
        else _seven_zip(tmp_path / "Game.7z", {"Game.gba": data})
    )
    dest = tmp_path / "out"
    dest.mkdir()

    with pytest.raises(ArchiveSafetyRefused):
        archive_service.extract_members(str(archive), str(dest), **{limit: 1024})

    written = dest / "Game.gba"
    assert not written.exists() or written.stat().st_size <= 1024


def _hung_hash(monkeypatch):
    """The whole-file SHA-1 read hangs (storage that stopped answering)
    until the returned event is set; *started* is set once it hangs."""
    import threading

    from services import file_hasher

    release, started = threading.Event(), threading.Event()

    def hung(_hasher, _path, progress=None):
        started.set()
        release.wait(30)

    monkeypatch.setattr(file_hasher, "_update_from_file", hung)
    return release, started


@pytest.mark.asyncio
async def test_a_recognize_read_that_stalls_fails_within_its_bound(vol, db, monkeypatch):
    """Codex 4608007769 (P1): the SHA-1 fallback is stall-bounded, so a hung
    read fails the job with a Finding and gives its conversion slot back."""
    import asyncio

    from services.inbox import intake

    await _dat("Sony - PlayStation 2 (Redump)", {"Other": _sha1(b"other")})
    monkeypatch.setattr(intake, "_RECOGNIZE_STALL_SECONDS", 0.3)
    release, _started = _hung_hash(monkeypatch)
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    game = _drop(vol / "Downloads" / "ps2" / "Game.iso", _bytes("hung"))
    try:
        await asyncio.wait_for(_polls(box, count=3), 10)
    finally:
        release.set()

    assert game.exists()
    assert intake.job_manager.slots_in_use() == 0
    [finding] = _inbox_findings()
    assert finding["kind"] == findings.INBOX_FAILED
    assert "couldn't be Recognized" in finding["reason"]


@pytest.mark.asyncio
async def test_cancelling_a_recognize_mid_hash_ends_it_promptly(vol, db, monkeypatch):
    import asyncio

    from models import ConversionMode, JobStatus
    from services import inbox
    from services.inbox import intake

    await _dat("Sony - PlayStation 2 (Redump)", {"Other": _sha1(b"other")})
    release, started = _hung_hash(monkeypatch)
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    _drop(vol / "Downloads" / "ps2" / "Game.iso", _bytes("cancel"))
    before = set(intake.job_manager.jobs)
    try:
        await intake.poll(inbox.get(box.id), now=0)
        await intake.poll(inbox.get(box.id), now=1)
        assert await asyncio.to_thread(started.wait, 10)
        [job] = [
            j for job_id, j in intake.job_manager.jobs.items()
            if job_id not in before and j.mode == ConversionMode.INBOX_RECOGNIZE
        ]
        await intake.job_manager.cancel_job(job.id)
        await asyncio.wait_for(intake.drain(), 5)
    finally:
        release.set()

    assert intake.job_manager.get_job(job.id).status == JobStatus.CANCELLED
    assert intake.job_manager.slots_in_use() == 0
    [finding] = _inbox_findings()
    assert "cancelled" in finding["reason"]


@pytest.mark.asyncio
async def test_removing_an_inbox_keeps_a_slot_a_queued_conversion_reads(vol, db, queue):
    """Codex 4608007773 (P2): a slot a queued conversion reads from outlives
    the Inbox's removal, and goes once that job ends."""
    import asyncio

    from models import JobStatus
    from services import inbox
    from services.inbox import intake

    library = _library(vol)
    await inbox_431._ps2_rule(library)
    box = _inbox(vol, library, settle=0)
    _zip(vol / "Downloads" / "ps2" / "Game.zip", {"Game.iso": _bytes("queued")})
    await _polls(box, count=6)
    [job] = queue.submitted
    source = Path(job["paths"][0])
    assert source.parent in _unpack_dirs() and source.exists()

    await asyncio.to_thread(inbox.remove, box.id)

    assert source.exists()  # the queued conversion can still read it
    queue.jobs["job0"].status = JobStatus.COMPLETED
    intake.job_manager._notify_terminal(queue.jobs["job0"])
    await intake.drain()

    assert _stage_folders(vol / "Downloads") == []


@pytest.mark.asyncio
async def test_removing_an_inbox_with_no_job_on_a_slot_removes_it_at_once(
    vol, db, monkeypatch,
):
    import asyncio

    from services import inbox

    _verifies(monkeypatch)
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    _zip(vol / "Downloads" / "Bundle.zip", {"Mystery.iso": _bytes("held", 4096)})
    await _polls(box)
    assert _stage_folders(vol / "Downloads")

    await asyncio.to_thread(inbox.remove, box.id)

    assert _stage_folders(vol / "Downloads") == []


@pytest.mark.asyncio
async def test_an_archive_is_unpacked_in_the_work_area_not_the_inbox(vol, db, monkeypatch):
    """Codex 4608030847 (P1): the unpack folder is the app's own, outside the
    Inbox (and every Volume), where no Inbox writer has any right."""
    from services.inbox import intake

    _verifies(monkeypatch)
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    _zip(vol / "Downloads" / "Bundle.zip", {"Mystery.iso": _bytes("where", 4096)})

    await _polls(box)

    [unpacked] = intake._states[box.id].unpacked.values()
    slot = Path(unpacked.slot.path)
    assert slot.parent == Path(settings.temp_dir).resolve()
    assert not slot.is_relative_to(vol)
    assert list((vol / "Downloads").rglob(".compressatorium-stage")) == []


@pytest.mark.asyncio
async def test_an_unpack_folder_swapped_for_a_link_writes_nothing_outside(
    vol, db, tmp_path, monkeypatch,
):
    """Codex 4608030847 (P1): the unpack folder replaced by a link to another
    folder mid-extract. Every byte goes beneath the folder held open, so
    nothing lands where the link points, and the archive is refused with a
    Finding."""
    import os

    from services.archive import ArchiveService

    _verifies(monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    real = ArchiveService.extract_members

    def swap_then_extract(self, archive_path, destination, **kwargs):
        os.rename(destination, destination + ".moved")
        os.symlink(outside, destination)
        return real(self, archive_path, destination, **kwargs)

    monkeypatch.setattr(ArchiveService, "extract_members", swap_then_extract)
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    archive = _zip(vol / "Downloads" / "gba" / "Game.zip", {"sub/Game.gba": _bytes("swap")})

    await _polls(box, count=3)

    assert list(outside.iterdir()) == []
    assert archive.exists()
    assert not (vol / "ROMs" / "gba").exists()
    [finding] = _inbox_findings()
    assert finding["path"] == str(archive.resolve())
    assert "unpack folder was replaced" in finding["reason"]


@pytest.mark.parametrize("kind", ["zip", "7z"])
def test_extraction_writes_through_the_held_folder(tmp_path, kind):
    """Codex 4608030847: every format is unpacked through descriptors (a
    nested folder made with ``mkdir(dir_fd=...)``, each file ``O_EXCL``), and
    the bytes come out right."""
    import os

    from services.archive import archive_service

    members = {
        "Game (Disc 1)/Game.cue": b"FILE \"Game.bin\" BINARY\n",
        "Game (Disc 1)/Game.bin": _bytes("track", 8192),
        "readme.txt": b"hello",
        "empty.dat": b"",
    }
    archive = (
        _zip(tmp_path / "Set.zip", members) if kind == "zip"
        else _seven_zip(tmp_path / "Set.7z", members)
    )
    dest = tmp_path / "out"
    dest.mkdir()
    fd = os.open(dest, os.O_RDONLY | os.O_DIRECTORY)
    try:
        written = archive_service.extract_members(str(archive), str(dest), destination_fd=fd)
    finally:
        os.close(fd)

    assert sorted(written) == sorted(str(dest / name) for name in members)
    for name, data in members.items():
        assert (dest / name).read_bytes() == data


# ---- an unpacked Image's conversion keeps its Source identity --------------


@pytest.mark.asyncio
async def test_an_unpacked_images_conversion_carries_its_identity_and_folder(vol, db, queue):
    """The conversion of an Image unpacked from an archive is queued with its
    Source identity, rooted at its unpack folder, and that folder as the one
    trusted root outside the Volumes."""
    library = _library(vol)
    await inbox_431._ps2_rule(library)
    box = _inbox(vol, library, settle=0)
    _zip(vol / "Downloads" / "ps2" / "Game.zip", {"Game.iso": _bytes("identity")})

    await _polls(box, count=6)

    [job] = queue.submitted
    [source] = job["paths"]
    [slot] = _unpack_dirs()
    identity = job["source_identities"][source]
    assert identity["root"] == str(slot)
    assert identity["snapshot"]["size"] == len(_bytes("identity"))
    assert job["trusted_roots"] == {source: str(slot)}


def _unpacked_cue(tmp_path):
    """A cue and its track in a fresh unpack folder: the folder and the cue."""
    from services.inbox import intake

    slot = intake._UnpackSlot()
    folder = Path(slot.path)
    (folder / "Game.bin").write_bytes(_bytes("track", 2352 * 4))
    cue = folder / "Game.cue"
    cue.write_text('FILE "Game.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n')
    return slot, cue


async def _queued(cue: Path, slot, *, trusted: bool = True):
    """A real queued createcd job for *cue*, carrying its identity under the
    unpack folder (as intake queues it), and its folder as trusted root."""
    from models import ConversionMode
    from services.job_manager import job_manager
    from utils.path_utils import source_identity

    identity = source_identity(str(cue), slot.path)
    identity["members"] = {"Game.bin": source_identity(
        str(cue.parent / "Game.bin"), slot.path,
    )["snapshot"]}
    [job] = await job_manager.create_batch_jobs(
        [str(cue)], ConversionMode.CREATECD, output_dir=str(cue.parent.parent / "out"),
        source_identities={str(cue): identity},
        trusted_roots={str(cue): slot.path} if trusted else None,
    )
    return job


@pytest.mark.asyncio
async def test_an_unpacked_images_job_passes_its_identity_bind_and_companion_checks(
    vol, db, tmp_path,
):
    """The real job pipeline's checks on a queued conversion of an unpacked
    Image: the Source identity check takes it under its trusted folder, the
    bind reads it there, and the companion check finds its track."""
    from services.job_manager import _reads, _trusted_roots, job_manager
    from utils.path_utils import source_companions_are_safe

    slot, cue = _unpacked_cue(tmp_path)
    job = await _queued(cue, slot)
    try:
        assert job._trusted_root == slot.path
        assert await job_manager._source_identity_problem_bounded(job) is None
        assert await job_manager._bind_source_bounded(job) is None
        assert slot.path in _trusted_roots(job)
        assert source_companions_are_safe(
            _reads(job), job.mode.value, within=_trusted_roots(job),
        )
    finally:
        await job_manager.cancel_job(job.id)
        if job._bound is not None:
            job._bound.close()
        slot.close()


@pytest.mark.asyncio
async def test_a_job_without_its_trusted_folder_is_still_refused(vol, db, tmp_path):
    """Trust is per job and set when it is created: the same Source queued
    without its unpack folder as trusted root is refused, being outside the
    Volumes."""
    from services.job_manager import job_manager

    slot, cue = _unpacked_cue(tmp_path)
    job = await _queued(cue, slot, trusted=False)
    try:
        problem = await job_manager._source_identity_problem_bounded(job)
        assert problem and "Volumes" in problem
    finally:
        await job_manager.cancel_job(job.id)
        slot.close()


@pytest.mark.asyncio
async def test_an_unpacked_image_swapped_after_queueing_is_refused(vol, db, tmp_path):
    import os

    from services.job_manager import job_manager

    slot, cue = _unpacked_cue(tmp_path)
    job = await _queued(cue, slot)
    try:
        replacement = cue.parent / "other.cue"
        replacement.write_text(cue.read_text() + "REM swapped\n")
        os.replace(replacement, cue)

        problem = await job_manager._source_identity_problem_bounded(job)
        assert problem and "isn't the file" in problem
    finally:
        await job_manager.cancel_job(job.id)
        slot.close()


def test_a_source_outside_its_trusted_folder_is_refused(tmp_path):
    """The trusted folder is the only place outside the Volumes a Source is
    taken from: a path outside it, or an identity rooted anywhere else, is
    refused."""
    from services.inbox import intake
    from utils.path_utils import source_identity, source_identity_problem

    slot = intake._UnpackSlot()
    other = intake._UnpackSlot()
    try:
        inside = Path(slot.path) / "Game.iso"
        inside.write_bytes(_bytes("inside"))
        outside = Path(other.path) / "Game.iso"
        outside.write_bytes(_bytes("inside"))
        identity = source_identity(str(inside), slot.path)

        assert source_identity_problem(str(inside), identity, slot.path) is None
        assert source_identity_problem(str(outside), identity, slot.path)
        rooted_elsewhere = source_identity(str(outside), other.path)
        assert source_identity_problem(str(outside), rooted_elsewhere, slot.path)
        link = Path(slot.path) / "linked"
        link.symlink_to(other.path)
        assert source_identity_problem(
            str(link / "Game.iso"), {**identity, "root": slot.path}, slot.path,
        )
    finally:
        slot.close()
        other.close()
