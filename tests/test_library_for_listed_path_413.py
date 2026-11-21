"""Which Library holds a file is found from the path as listed (#413).

A folder Library's system folder can be a symlink that points outside the
Library root (``ROMs/psx -> ../../esde_real/psx``). The file is reached through
the Library's folders, so the Library's opt-in Referrers (the ES-DE gamelist)
cover it. Matching only the resolved path found no Library, so the gamelist
Referrer never ran and the Source was deleted while ``gamelists/psx`` still
named it (smoke run 8, row 48).

Drives the real pipeline (convert route -> planner -> job manager), as
``tests/test_referrers_m3u.py`` does: only the tool is stubbed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException
from services import libraries

from app.models import (
    ConversionMode,
    DeletePlanRequest,
    DuplicateAction,
    JobCreateRequest,
    JobStatus,
)
from app.routes import convert as convert_routes
from app.services import ledger

from .test_esde_gamelists_folder_387 import _db_fixture
from .test_referrers_m3u import CONTAINER_ONLY, _cue
from .test_referrers_m3u import _volume as _m3u_volume

_FIXTURES = (_m3u_volume, _db_fixture)

GAMELIST = (
    b'<?xml version="1.0"?>\n'
    b"<gameList>\n\t<game><path>./Game.cue</path></game>\n</gameList>\n"
)


def _esde_library(volume: Path) -> tuple[Path, Path]:
    """A folder Library at ``esde/ROMs`` with the ES-DE switch on and its
    gamelists folder at ``esde/gamelists``, holding ``gamelists/psx``."""
    roms, gamelists = volume / "esde" / "ROMs", volume / "esde" / "gamelists"
    roms.mkdir(parents=True)
    (gamelists / "psx").mkdir(parents=True)
    (gamelists / "psx" / "gamelist.xml").write_bytes(GAMELIST)
    library = libraries.register(str(roms))
    libraries.update(library.id, es_frontend=True, gamelists_folder=str(gamelists))
    return roms, gamelists / "psx" / "gamelist.xml"


async def _convert(source: Path):
    job = await convert_routes.create_job(JobCreateRequest(
        file_path=str(source), mode=ConversionMode.CREATECD,
        duplicate_action=DuplicateAction.SKIP, delete_on_verify=True,
        container_only=CONTAINER_ONLY,
    ))
    await convert_routes.job_manager._process_job(job.id)
    return job


@pytest.mark.asyncio
async def test_a_system_folder_symlinked_outside_the_library_keeps_its_gamelist(volume, db):
    """The repro: ROMs/psx -> ../../esde_real/psx, inside the Volume but
    outside the Library root. The job, given the path as listed, finds the
    Library, so the gamelist follows the output before the Source goes."""
    roms, gamelist = _esde_library(volume)
    real = volume / "esde_real" / "psx"
    real.mkdir(parents=True)
    _cue(real / "Game.cue")
    (roms / "psx").symlink_to(Path("..") / ".." / "esde_real" / "psx", target_is_directory=True)
    source = roms / "psx" / "Game.cue"

    plan = await convert_routes.delete_plan(DeletePlanRequest(
        file_paths=[str(source)], mode=ConversionMode.CREATECD,
    ))
    job = await _convert(source)

    assert {"referrer": "gamelist", "target": str(gamelist), "entries": 1, "edits": [
        {"old": "./Game.cue", "new": "./Game.chd"},
    ]} in plan["items"][0]["referrer_changes"]
    assert job.status == JobStatus.COMPLETED, job.error_message
    assert gamelist.read_bytes() == GAMELIST.replace(b"Game.cue", b"Game.chd")
    assert not (real / "Game.cue").exists() and (real / "Game.chd").is_file()
    assert {
        "referrer": "gamelist", "target": str(gamelist), "status": "updated", "entries": 1,
    } in ledger.row_from_job(job)["referrers_updated"]


@pytest.mark.asyncio
async def test_a_system_folder_reached_through_another_library_keeps_the_source(volume, db):
    """ROMs/psx -> another Library's psx folder: listed in one Library, living
    in another. Whose Referrers cover it can't be told, so nothing is deleted
    and the job says why."""
    roms, gamelist = _esde_library(volume)
    other = volume / "other"
    (other / "psx").mkdir(parents=True)
    libraries.register(str(other))
    _cue(other / "psx" / "Game.cue")
    (roms / "psx").symlink_to(other / "psx", target_is_directory=True)

    source = roms / "psx" / "Game.cue"

    plan = await convert_routes.delete_plan(DeletePlanRequest(
        file_paths=[str(source)], mode=ConversionMode.CREATECD,
    ))
    job = await _convert(source)

    assert any("more than one Library" in w for w in plan["items"][0]["warnings"])
    assert job.status == JobStatus.COMPLETED, job.error_message
    assert job.message.startswith(
        "converted, source kept: couldn't tell which Library holds it ("
    ), job.message
    assert "more than one Library" in job.message
    assert job.deleted_paths == []
    assert (other / "psx" / "Game.cue").is_file() and (other / "psx" / "Game.bin").is_file()
    assert (other / "psx" / "Game.chd").is_file()
    assert gamelist.read_bytes() == GAMELIST
    [record] = ledger.row_from_job(job)["referrers_updated"]
    assert record["referrer"] == "library" and record["status"] == "failed"


@pytest.mark.asyncio
async def test_a_system_folder_symlinked_outside_every_volume_is_still_refused(
    volume, db, tmp_path_factory,
):
    """Security: finding the Library from the path as listed must not let a
    symlink out of every Volume through. The job is refused before it is
    queued, as before, and nothing is written."""
    roms, gamelist = _esde_library(volume)
    outside = tmp_path_factory.mktemp("outside") / "psx"
    outside.mkdir()
    _cue(outside / "Game.cue")
    (roms / "psx").symlink_to(outside, target_is_directory=True)

    with pytest.raises(HTTPException) as refused:
        await _convert(roms / "psx" / "Game.cue")

    assert refused.value.status_code == 403
    assert (outside / "Game.cue").is_file() and not (outside / "Game.chd").exists()
    assert gamelist.read_bytes() == GAMELIST


# ---- the shared lookup ------------------------------------------------------


def test_library_for_finds_the_library_from_the_path_as_listed(volume, db):
    roms, _ = _esde_library(volume)
    (volume / "esde_real" / "psx").mkdir(parents=True)
    (roms / "psx").symlink_to(volume / "esde_real" / "psx", target_is_directory=True)

    found = libraries.library_for(str(roms / "psx" / "Game.cue"))

    assert found is not None and found.root == str(roms)


def test_library_for_still_finds_a_file_reached_from_outside_by_where_it_lives(volume, db):
    """A link outside every Library into one (``links/psx -> ROMs/psx``): the
    file lives in that Library, so its Referrers still apply, as before."""
    roms, _ = _esde_library(volume)
    (roms / "psx").mkdir()
    (volume / "links").mkdir()
    (volume / "links" / "psx").symlink_to(roms / "psx", target_is_directory=True)

    found = libraries.library_for(str(volume / "links" / "psx" / "Game.cue"))

    assert found is not None and found.root == str(roms)


def test_library_for_refuses_to_choose_between_two_libraries(volume, db):
    """Listed in one Library, living in another: raises, so every caller
    fails closed (the hook keeps the Source, staging hides the output)."""
    roms, _ = _esde_library(volume)
    (volume / "other" / "psx").mkdir(parents=True)
    libraries.register(str(volume / "other"))
    (roms / "psx").symlink_to(volume / "other" / "psx", target_is_directory=True)

    with pytest.raises(libraries.AmbiguousLibrary, match="more than one Library"):
        libraries.library_for(str(roms / "psx" / "Game.cue"))


@pytest.mark.asyncio
async def test_a_folder_library_inside_romm_uses_its_own_referrers(volume, db):
    """#514: ``esde/ROMs`` is a folder Library inside RomM's folder ``esde``.
    The nearest root owns the file, so the folder Library's gamelist follows
    the output, and the Source goes as it would anywhere else."""
    from services.romm import settings as romm_settings

    roms, gamelist = _esde_library(volume)
    await romm_settings.save({"url": "http://romm:8080", "library_root": str(volume / "esde")})
    (roms / "psx").mkdir()
    _cue(roms / "psx" / "Game.cue")

    assert libraries.library_for(str(roms / "psx" / "Game.cue")).root == str(roms)
    job = await _convert(roms / "psx" / "Game.cue")

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert gamelist.read_bytes() == GAMELIST.replace(b"Game.cue", b"Game.chd")
    assert (roms / "psx" / "Game.chd").is_file() and not (roms / "psx" / "Game.cue").exists()
