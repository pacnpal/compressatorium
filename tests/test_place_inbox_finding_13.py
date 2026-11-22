"""Place it (plan item 13, spec §8.4): a watch-folder file with no console.

``POST /api/findings/{id}/place {platform}`` moves the file (a sheet with its
tracks) into ``<watch folder>/<console>/``, never over another file, clears
its problem, and marks it settled so the next poll takes it in through the
normal path. Only the two "which console?" kinds of a watch folder can be
placed, and only a file still inside its watch folder and a Drive.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException
from services import db as _db
from services import findings, platform_detect
from services.inbox import intake

from config import settings

DISC = b"\0" * 4096
CUE = 'FILE "Game.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n'


@pytest.fixture(name="vol")
def _vol(tmp_path, monkeypatch):
    volume = tmp_path / "vol"
    volume.mkdir()
    monkeypatch.setattr(settings, "chd_volumes", str(volume))
    platform_detect.clear_cache()
    return volume


@pytest.fixture(name="db")
def _db_fixture(tmp_path: Path, monkeypatch):
    from services import inbox, libraries
    from services.inbox import intake
    from services.job_manager import job_manager
    from services.romm import settings as romm_settings

    monkeypatch.setattr(job_manager, "max_concurrent", 1)
    monkeypatch.setattr(job_manager, "_semaphore", asyncio.Semaphore(1))
    if _db.engine is not None:
        _db.engine.dispose()
    _db.init_engine(str(tmp_path / "compressatorium.db"), create_schema=True)
    romm_settings.reset_for_tests()
    libraries.reset_for_tests()
    inbox.reset_for_tests()
    intake.reset_for_tests()
    yield
    intake.reset_for_tests()
    inbox.reset_for_tests()
    libraries.reset_for_tests()
    romm_settings.reset_for_tests()
    if _db.engine is not None:
        _db.engine.dispose()
    _db.engine = None
    _db.SessionLocal = None


def _watch_folder(vol: Path):
    from services import inbox, libraries

    (vol / "ROMs").mkdir()
    (vol / "Downloads").mkdir()
    library = libraries.register(str(vol / "ROMs"))
    return inbox.register(str(vol / "Downloads"), library.id, 60)


def _drop(path: Path, data: bytes = DISC) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


async def _poll(box, now: float) -> dict:
    from services import inbox
    from services.inbox import intake

    result = await intake.poll(inbox.get(box.id), now=now)
    await intake.drain()
    return result


async def _problem(box) -> dict:
    """Drop-and-settle leaves one "Unknown console" problem: return it."""
    await _poll(box, now=0)
    await _poll(box, now=61)
    [row] = [f for f in findings.list_findings() if f["kind"] in findings.INBOX_KINDS]
    assert row["kind"] == findings.INBOX_UNSORTED
    return row


async def _place(row: dict, platform: str, now: float = 61):
    return await intake.place(row, platform, now=now)


@pytest.mark.asyncio
async def test_a_placed_iso_moves_into_its_console_folder_and_is_taken_in(vol, db):
    box = _watch_folder(vol)
    game = _drop(vol / "Downloads" / "Game.iso")
    row = await _problem(box)

    # Any of the console's names: the folder takes its own slug.
    placed = await _place(row, "playstation2")

    moved = vol / "Downloads" / "ps2" / "Game.iso"
    assert placed["path"] == str(moved.resolve())
    assert placed["platform"] == "ps2"
    assert moved.read_bytes() == DISC and not game.exists()
    # Its problem is gone at once, not on the next poll.
    assert [f for f in findings.list_findings() if f["kind"] in findings.INBOX_KINDS] == []

    # Settled already: the next poll takes it in, with no settle wait.
    await _poll(box, now=62)
    await _poll(box, now=63)
    assert (vol / "Downloads" / "processed" / "ps2" / "Game.iso").exists()
    assert (vol / "ROMs" / "ps2" / "Game.iso").read_bytes() == DISC
    assert [f for f in findings.list_findings() if f["kind"] in findings.INBOX_KINDS] == []


@pytest.mark.asyncio
async def test_a_cue_moves_with_its_bin(vol, db):
    box = _watch_folder(vol)
    cue = _drop(vol / "Downloads" / "Game.cue", CUE.encode())
    track = _drop(vol / "Downloads" / "Game.bin", b"\0" * 2352 * 4)
    row = await _problem(box)

    await _place(row, "psx")

    target = vol / "Downloads" / "psx"
    assert sorted(p.name for p in target.iterdir()) == ["Game.bin", "Game.cue"]
    assert not cue.exists() and not track.exists()


@pytest.mark.asyncio
async def test_a_taken_name_moves_nothing(vol, db):
    box = _watch_folder(vol)
    cue = _drop(vol / "Downloads" / "Game.cue", CUE.encode())
    track = _drop(vol / "Downloads" / "Game.bin", b"\0" * 2352 * 4)
    row = await _problem(box)
    # Only the track's name is taken: the sheet stays too.
    other = _drop(vol / "Downloads" / "psx" / "Game.bin", b"someone else's")

    with pytest.raises(intake.PlaceRefused) as caught:
        await _place(row, "psx")

    assert caught.value.status == 409
    assert cue.exists() and track.exists()
    assert other.read_bytes() == b"someone else's"
    assert not (vol / "Downloads" / "psx" / "Game.cue").exists()


@pytest.mark.asyncio
async def test_a_file_outside_its_watch_folder_is_refused(vol, db):
    box = _watch_folder(vol)
    _drop(vol / "Downloads" / "Game.iso")
    row = await _problem(box)
    elsewhere = _drop(vol / "Elsewhere" / "Game.iso")
    kept = _drop(vol / "Downloads" / "processed" / "Old.iso")

    for path in (elsewhere, kept):
        with pytest.raises(intake.PlaceRefused) as caught:
            await _place({**row, "path": str(path)}, "ps2")
        assert caught.value.status == 403, path
        assert path.exists()
    assert not (vol / "Downloads" / "ps2").exists()


@pytest.mark.asyncio
async def test_only_a_watch_folder_console_problem_can_be_placed(vol, db):
    box = _watch_folder(vol)
    _drop(vol / "Downloads" / "Game.iso")
    row = await _problem(box)

    for kind in (findings.UNSORTED, findings.INBOX_FAILED, findings.NOT_INTACT):
        with pytest.raises(intake.PlaceRefused) as caught:
            await _place({**row, "kind": kind}, "ps2")
        assert caught.value.status == 400, kind
    with pytest.raises(intake.PlaceRefused) as caught:
        await _place(row, "not-a-console")
    assert caught.value.status == 400


@pytest.mark.asyncio
async def test_a_file_already_gone_clears_its_problem(vol, db):
    box = _watch_folder(vol)
    game = _drop(vol / "Downloads" / "Game.iso")
    row = await _problem(box)
    game.unlink()

    with pytest.raises(intake.PlaceRefused) as caught:
        await _place(row, "ps2")

    assert caught.value.status == 404
    assert [f for f in findings.list_findings() if f["kind"] in findings.INBOX_KINDS] == []


@pytest.mark.asyncio
async def test_the_route_answers_and_the_rows_name_the_watch_folder(vol, db, monkeypatch):
    from routes import findings as routes
    from services.inbox import intake

    box = _watch_folder(vol)
    _drop(vol / "Downloads" / "sub" / "Game.iso")
    await _poll(box, now=0)
    await _poll(box, now=61)

    [row] = (await routes.list_findings())["findings"]
    # Never the internal key: the watch folder's name and the path inside it.
    assert row["inbox_name"] == "Downloads"
    assert row["relative"] == "sub/Game.iso"

    polled = []

    async def fake_poll(inbox, **_kw):
        polled.append(inbox.id)
        return {}

    monkeypatch.setattr(intake, "poll", fake_poll)
    out = await routes.place_finding(row["id"], routes.PlaceRequest(platform="ps2"))
    await asyncio.sleep(0)
    assert out["path"].endswith("/Downloads/ps2/Game.iso")
    assert polled == [box.id]

    with pytest.raises(HTTPException) as caught:
        await routes.place_finding(999999, routes.PlaceRequest(platform="ps2"))
    assert caught.value.status_code == 404


def test_the_console_problem_no_longer_says_to_move_it_by_hand():
    source = (Path(__file__).resolve().parents[1] / "app" / "services" / "inbox"
              / "intake.py").read_text(encoding="utf-8")
    assert "Move it into a folder named after its" not in source


@pytest.mark.asyncio
async def test_a_file_another_job_reads_is_not_moved(vol, db):
    """Codex 4658826950: a conversion started from Files holds shared input
    locks on what it reads. Place it takes the exclusive locks intake's own
    move takes on every file, so it refuses while a reader holds one, and
    moves once the reader is done."""
    from services.lock_manager import lock_manager

    box = _watch_folder(vol)
    cue = _drop(vol / "Downloads" / "Game.cue", CUE.encode())
    track = _drop(vol / "Downloads" / "Game.bin", b"\0" * 2352 * 4)
    row = await _problem(box)
    # A job reading the track (not the sheet): the whole set must stay put.
    assert lock_manager.acquire_input_lock(str(track.resolve()))
    try:
        with pytest.raises(intake.PlaceRefused) as caught:
            await _place(row, "psx")
        assert caught.value.status == 409
        assert "in use" in caught.value.message
        assert cue.exists() and track.exists()
        assert not (vol / "Downloads" / "psx").exists()
    finally:
        lock_manager.release_input_lock(str(track.resolve()))

    await _place(row, "psx")
    moved = vol.resolve() / "Downloads" / "psx"
    assert sorted(p.name for p in moved.iterdir()) == ["Game.bin", "Game.cue"]
    # Its locks were let go after the move: a reader can take the old paths.
    assert lock_manager.acquire_input_lock(str(track.resolve()))
    lock_manager.release_input_lock(str(track.resolve()))


@pytest.mark.asyncio
async def test_a_sheet_naming_a_track_in_another_folder_moves_nothing(vol, db):
    """Codex 4658826953: `../Tracks/Game.bin` is dropped by the confined track
    resolver, so the sheet looked trackless and would move alone. The sheet's
    references are checked first, as intake checks them, and nothing moves."""
    box = _watch_folder(vol)
    cue = _drop(
        vol / "Downloads" / "sub" / "Game.cue",
        b'FILE "../Tracks/Game.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n',
    )
    track = _drop(vol / "Downloads" / "Tracks" / "Game.bin", b"\0" * 2352 * 4)
    await _poll(box, now=0)
    await _poll(box, now=61)
    rows = [f for f in findings.list_findings()
            if f["kind"] in findings.INBOX_KINDS and f["path"].endswith("Game.cue")]
    assert rows, findings.list_findings()
    row = {**rows[0], "kind": findings.INBOX_UNSORTED}

    with pytest.raises(intake.PlaceRefused) as caught:
        await _place(row, "psx")

    assert caught.value.status == 400
    assert "sheet" in caught.value.message
    assert cue.exists() and track.exists()
    assert not (vol / "Downloads" / "psx").exists()
