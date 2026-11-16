"""An ES-DE gamelists folder per Library (#387).

ES-DE keeps its gamelists in a folder of its own (``~/ES-DE/gamelists``), one
subfolder per system, named like the system's ROM folder: the gamelist for
``ROMs/ps2`` is ``gamelists/ps2/gamelist.xml``, and its ``<path>`` entries are
relative to ``ROMs/ps2``. A Library can name that folder (inside a Volume);
the gamelist Referrer then updates the system's gamelist there too, with every
guarantee of #386: path-only edits, one backup, an atomic write, the Source
kept on any failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from services import db as _db
from services import referrers
from services.referrers.gamelist import GamelistReferrer

from app.models import (
    ConversionMode,
    DeletePlanRequest,
    DuplicateAction,
    JobCreateRequest,
    JobStatus,
)
from app.routes import convert as convert_routes
from app.services import ledger
from config import settings

from .test_referrers_m3u import CONTAINER_ONLY, _cue
from .test_referrers_m3u import _volume as _m3u_volume

_FIXTURES = (_m3u_volume,)


class _Library:
    """The parts of a Library the hook asks: is the Referrer on, and its options."""

    id = "folder:test"
    name = "Test"

    def __init__(self, gamelists: Path | None, root: Path) -> None:
        # The Library root is ES-DE's ROM directory: each folder directly
        # under it is a system (#387).
        self.root = str(root)
        self._options = {"gamelists_folder": str(gamelists)} if gamelists else {}

    def referrer_enabled(self, referrer_id: str) -> bool:
        return referrer_id == "gamelist"

    def referrer_options(self, referrer_id: str) -> dict:
        return dict(self._options) if referrer_id == "gamelist" else {}


def _esde(vol: Path, gamelists: Path | None) -> _Library:
    """A Library whose ROM directory is ``vol/ROMs``, with *gamelists*."""
    return _Library(gamelists, vol / "ROMs")


GAMELIST = (
    b'<?xml version="1.0"?>\n'
    b"<alternativeEmulator><label>PCSX2</label></alternativeEmulator>\n"
    b"<gameList>\n"
    b"\t<game>\n"
    b"\t\t<path>./Game.iso</path>\n"
    b"\t\t<name>Game</name>\n"
    b"\t\t<favorite>true</favorite>\n"
    b"\t\t<playcount>4</playcount>\n"
    b"\t</game>\n"
    b"\t<!-- kept as written -->\n"
    b"\t<game>\n"
    b"\t\t<path>./Other.iso</path>\n"
    b"\t</game>\n"
    b"</gameList>\n"
)


@pytest.fixture(name="vol")
def _vol(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "chd_volumes", str(tmp_path))
    return tmp_path


def _file(path: Path, data: bytes = b"disc") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


async def _replace(old: Path, new: Path, library):
    if not new.exists():
        _file(new, b"output")
    return await referrers.update_before_source_removal(
        str(old), {str(old): str(new)}, library=library,
    )


# ---- through the Referrer contract ----------------------------------------


@pytest.mark.asyncio
async def test_the_systems_gamelist_in_the_esde_folder_follows_the_image(vol):
    """ES-DE's own layout. The <alternativeEmulator> before <gameList> (ES-DE
    writes it there) is kept, as is every other byte."""
    source = _file(vol / "ROMs" / "ps2" / "Game.iso")
    gamelist = _file(vol / "ES-DE" / "gamelists" / "ps2" / "gamelist.xml", GAMELIST)

    outcome = await _replace(
        source, vol / "ROMs" / "ps2" / "Game.chd", _esde(vol, vol / "ES-DE" / "gamelists"),
    )

    assert outcome.ok, outcome.reason
    assert gamelist.read_bytes() == GAMELIST.replace(
        b"<path>./Game.iso</path>", b"<path>./Game.chd</path>",
    )
    assert (gamelist.parent / "gamelist.xml.bak").read_bytes() == GAMELIST
    assert outcome.records == [{
        "referrer": "gamelist", "target": str(gamelist), "status": "updated", "entries": 1,
    }]


@pytest.mark.asyncio
async def test_a_nested_image_resolves_against_the_system_rom_folder(vol):
    source = _file(vol / "ROMs" / "psx" / "Multi Disc" / "Game (Disc 1).cue")
    data = b"<gameList><game><path>./Multi Disc/Game (Disc 1).cue</path></game></gameList>"
    gamelist = _file(vol / "gamelists" / "psx" / "gamelist.xml", data)

    outcome = await _replace(
        source, vol / "ROMs" / "psx" / "Multi Disc" / "Game (Disc 1).chd",
        _esde(vol, vol / "gamelists"),
    )

    assert outcome.ok, outcome.reason
    assert gamelist.read_bytes() == data.replace(b"1).cue", b"1).chd")


@pytest.mark.asyncio
async def test_without_the_folder_the_esde_gamelist_is_left_and_the_platform_folder_used(vol):
    source = _file(vol / "ROMs" / "ps2" / "Game.iso")
    esde = _file(vol / "gamelists" / "ps2" / "gamelist.xml", GAMELIST)
    beside = _file(vol / "ROMs" / "ps2" / "gamelist.xml", GAMELIST)

    outcome = await _replace(
        source, vol / "ROMs" / "ps2" / "Game.chd", _esde(vol, None))

    assert outcome.ok, outcome.reason
    assert esde.read_bytes() == GAMELIST
    assert beside.read_bytes() != GAMELIST


@pytest.mark.asyncio
@pytest.mark.parametrize("decoy", [False, True])
async def test_a_subfolder_named_like_its_system_resolves_against_the_system(vol, decoy):
    """Codex PRRT_kwDOQ9nULM64Jpv8 / JsQa: in ROMs/ps2/ps2/Game.iso the system
    is ps2, the folder directly under the ROM directory, so gamelists/ps2's
    ./ps2/Game.iso is read from ROMs/ps2. With *decoy*, ROMs/ps2/ps2/ps2/Game.iso
    (where a nearer base would have pointed it) exists and stays untouched."""
    source = _file(vol / "ROMs" / "ps2" / "ps2" / "Game.iso")
    if decoy:
        _file(vol / "ROMs" / "ps2" / "ps2" / "ps2" / "Game.iso", b"another disc")
    data = b"<gameList><game><path>./ps2/Game.iso</path></game></gameList>"
    gamelist = _file(vol / "gamelists" / "ps2" / "gamelist.xml", data)

    outcome = await _replace(
        source, vol / "ROMs" / "ps2" / "ps2" / "Game.chd",
        _esde(vol, vol / "gamelists"),
    )

    assert outcome.ok, outcome.reason
    assert gamelist.read_bytes() == data.replace(b"Game.iso", b"Game.chd")


@pytest.mark.asyncio
async def test_a_subfolder_named_like_another_system_leaves_that_gamelist_alone(vol):
    """Codex PRRT_kwDOQ9nULM64JsQa: ROMs/ps2/psx/Game.iso belongs to ps2. The
    psx system's gamelist, which also lists a ./Game.iso, is not touched."""
    source = _file(vol / "ROMs" / "ps2" / "psx" / "Game.iso")
    psx_data = b"<gameList><game><path>./Game.iso</path></game></gameList>"
    psx = _file(vol / "gamelists" / "psx" / "gamelist.xml", psx_data)
    ps2_data = b"<gameList><game><path>./psx/Game.iso</path></game></gameList>"
    ps2 = _file(vol / "gamelists" / "ps2" / "gamelist.xml", ps2_data)

    outcome = await _replace(
        source, vol / "ROMs" / "ps2" / "psx" / "Game.chd",
        _esde(vol, vol / "gamelists"),
    )

    assert outcome.ok, outcome.reason
    assert psx.read_bytes() == psx_data
    assert not (psx.parent / "gamelist.xml.bak").exists()
    assert ps2.read_bytes() == ps2_data.replace(b"Game.iso", b"Game.chd")


@pytest.mark.asyncio
async def test_a_symlinked_system_folder_keeps_its_es_de_name(vol):
    """Codex PRRT_kwDOQ9nULM64JsQn: ROMs/ps2 -> actual/PlayStation2. ES-DE
    knows the system as ps2, so gamelists/ps2 is the one updated, even though
    the resolved path names PlayStation2."""
    real = vol / "actual" / "PlayStation2"
    source = _file(real / "Game.iso")
    (vol / "ROMs").mkdir()
    (vol / "ROMs" / "ps2").symlink_to(real, target_is_directory=True)
    data = b"<gameList><game><path>./Game.iso</path></game></gameList>"
    gamelist = _file(vol / "gamelists" / "ps2" / "gamelist.xml", data)

    outcome = await _replace(source, real / "Game.chd", _esde(vol, vol / "gamelists"))

    assert outcome.ok, outcome.reason
    assert gamelist.read_bytes() == data.replace(b"Game.iso", b"Game.chd")


@pytest.mark.asyncio
async def test_a_gamelist_for_another_system_is_left_alone(vol):
    source = _file(vol / "ROMs" / "ps2" / "Game.iso")
    other = _file(vol / "gamelists" / "gc" / "gamelist.xml", GAMELIST)

    outcome = await _replace(
        source, vol / "ROMs" / "ps2" / "Game.chd", _esde(vol, vol / "gamelists"))

    assert outcome.ok
    assert outcome.records == []
    assert other.read_bytes() == GAMELIST


@pytest.mark.asyncio
@pytest.mark.parametrize("data, why", [
    (b"<gameList><game><path>./Game.iso</path></game>", "can't be parsed"),
    (b"<gameList><game><path>/home/deck/ROMs/ps2/Game.iso</path></game></gameList>",
     "doesn't point at it"),
])
async def test_an_esde_gamelist_that_cant_be_updated_keeps_the_source(vol, data, why):
    source = _file(vol / "ROMs" / "ps2" / "Game.iso")
    gamelist = _file(vol / "gamelists" / "ps2" / "gamelist.xml", data)

    outcome = await _replace(
        source, vol / "ROMs" / "ps2" / "Game.chd", _esde(vol, vol / "gamelists"))

    assert not outcome.ok
    assert outcome.failed == str(gamelist) and why in outcome.reason
    assert gamelist.read_bytes() == data and source.is_file()


@pytest.mark.asyncio
async def test_a_gamelists_folder_outside_the_volumes_keeps_the_source(vol, tmp_path_factory):
    """Set while it was inside, then the Volumes changed: fail closed."""
    outside = tmp_path_factory.mktemp("elsewhere")
    gamelist = _file(outside / "ps2" / "gamelist.xml", GAMELIST)
    source = _file(vol / "ROMs" / "ps2" / "Game.iso")

    outcome = await _replace(
        source, vol / "ROMs" / "ps2" / "Game.chd", _esde(vol, outside))

    assert not outcome.ok
    assert "gamelists folder" in outcome.reason
    assert gamelist.read_bytes() == GAMELIST


def test_the_registry_binds_the_referrer_to_the_librarys_folder(vol):
    [bound] = [
        r for r in referrers.registry.active(_esde(vol, vol / "gamelists"))
        if r.id == "gamelist"
    ]
    assert isinstance(bound, GamelistReferrer)
    assert bound.gamelists_folder == str(vol / "gamelists")
    assert bound.rom_directory == str(vol / "ROMs")
    # The registered one is untouched: another Library gets its own binding.
    [plain] = [r for r in referrers.registry.all() if r.id == "gamelist"]
    assert plain.gamelists_folder is None


# ---- the setting ------------------------------------------------------------


@pytest.fixture(name="db")
def _db_fixture(tmp_path: Path):
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


def test_a_folder_library_saves_its_gamelists_folder(vol, db):
    from services import libraries

    (vol / "roms").mkdir()
    (vol / "gamelists").mkdir()
    library = libraries.register(str(vol / "roms"))
    assert libraries.describe(library)["gamelists_folder"] is None

    # The caller checks the folder first (bounded in the routes), then saves.
    checked = libraries.check_gamelists_folder(str(vol / "gamelists"))
    libraries.update(library.id, gamelists_folder=checked)
    libraries.reset_for_tests()
    again = libraries.get(library.id)

    assert libraries.describe(again)["gamelists_folder"] == str((vol / "gamelists").resolve())
    assert again.referrer_options("gamelist") == {
        "gamelists_folder": str((vol / "gamelists").resolve()),
    }
    libraries.update(library.id, gamelists_folder="")
    assert libraries.describe(libraries.get(library.id))["gamelists_folder"] is None


def test_a_gamelists_folder_outside_every_volume_is_refused(vol, db, tmp_path_factory):
    from services import libraries

    (vol / "roms").mkdir()
    library = libraries.register(str(vol / "roms"))
    outside = tmp_path_factory.mktemp("outside")

    with pytest.raises(libraries.LibraryPathError) as refused:
        libraries.check_gamelists_folder(str(outside))
    assert refused.value.status == 403
    assert libraries.describe(libraries.get(library.id))["gamelists_folder"] is None


def test_a_gamelists_folder_that_cant_be_confirmed_is_refused(vol, db, monkeypatch):
    """Fail closed: "couldn't tell" (a root probe that failed) is not inside."""
    from services import libraries

    (vol / "roms").mkdir()
    (vol / "gamelists").mkdir()
    library = libraries.register(str(vol / "roms"))
    monkeypatch.setattr(libraries, "volume_containment", lambda *_a, **_k: None)

    with pytest.raises(libraries.LibraryPathError) as refused:
        libraries.check_gamelists_folder(str(vol / "gamelists"))
    assert refused.value.status == 503
    assert libraries.describe(libraries.get(library.id))["gamelists_folder"] is None


@pytest.mark.asyncio
async def test_the_library_api_sets_and_refuses_the_folder(vol, db, tmp_path_factory):
    from fastapi import HTTPException
    from routes import libraries as library_routes

    (vol / "roms").mkdir()
    (vol / "gamelists").mkdir()
    created = await library_routes.register_library(
        library_routes.RegisterLibraryRequest(path=str(vol / "roms")),
    )
    assert created["gamelists_folder"] is None

    updated = await library_routes.update_library(
        created["id"], library_routes.UpdateLibraryRequest(gamelists_folder=str(vol / "gamelists")),
    )
    assert updated["gamelists_folder"] == str((vol / "gamelists").resolve())

    with pytest.raises(HTTPException) as refused:
        await library_routes.update_library(
            created["id"],
            library_routes.UpdateLibraryRequest(
                gamelists_folder=str(tmp_path_factory.mktemp("away")),
            ),
        )
    assert refused.value.status_code == 403


@pytest.mark.asyncio
async def test_a_folder_check_past_its_bound_saves_nothing_later(vol, db, monkeypatch):
    """Codex PRRT_kwDOQ9nULM64Jpv1: the route answers 503 "nothing was saved"
    when the folder check outlasts its bound. The abandoned check, recovering
    later, must not save the folder then, nor overwrite a save made since."""
    import asyncio
    import threading

    from fastapi import HTTPException
    from routes import libraries as library_routes
    from services import libraries

    (vol / "roms").mkdir()
    (vol / "gamelists").mkdir()
    created = await library_routes.register_library(
        library_routes.RegisterLibraryRequest(path=str(vol / "roms")),
    )
    release, finished = threading.Event(), threading.Event()
    real_check = libraries.check_gamelists_folder

    def stalled_check(path):
        release.wait(10)
        try:
            return real_check(path)
        finally:
            finished.set()

    monkeypatch.setattr(libraries, "ROOT_CHECK_SECONDS", 0.2)
    monkeypatch.setattr(libraries, "check_gamelists_folder", stalled_check)

    with pytest.raises(HTTPException) as refused:
        await library_routes.update_library(
            created["id"],
            library_routes.UpdateLibraryRequest(gamelists_folder=str(vol / "gamelists")),
        )
    assert refused.value.status_code == 503
    # A later save, after the 503: the switch turned on.
    await library_routes.update_library(
        created["id"], library_routes.UpdateLibraryRequest(es_frontend=True),
    )
    release.set()
    assert await asyncio.to_thread(finished.wait, 5)
    await asyncio.sleep(0.2)  # let the abandoned worker run to its end

    libraries.reset_for_tests()  # read back what the database holds
    stored = libraries.describe(libraries.get(created["id"]))
    assert stored["gamelists_folder"] is None
    assert stored["es_frontend"] is True


@pytest.mark.asyncio
async def test_romm_saves_and_refuses_its_gamelists_folder(vol, db, tmp_path_factory):
    from fastapi import HTTPException
    from routes import romm as romm_routes
    from services.romm import settings as romm_settings
    from services.romm.library import romm_library

    await romm_settings.load(force=True)
    (vol / "gamelists").mkdir()
    assert romm_settings.public()["gamelists_folder"] == ""

    await romm_routes.put_romm_settings(
        romm_routes.RommSettingsPatch(gamelists_folder=str(vol / "gamelists")),
    )
    assert romm_library.referrer_options("gamelist") == {
        "gamelists_folder": str((vol / "gamelists").resolve()),
    }

    with pytest.raises(HTTPException) as refused:
        await romm_routes.put_romm_settings(
            romm_routes.RommSettingsPatch(gamelists_folder=str(tmp_path_factory.mktemp("x"))),
        )
    assert refused.value.status_code == 403
    assert romm_library.referrer_options("gamelist") == {
        "gamelists_folder": str((vol / "gamelists").resolve()),
    }


# ---- the whole flow: a job and the delete confirmation ---------------------


@pytest.mark.asyncio
async def test_a_conversion_updates_the_esde_gamelist(volume, db):
    from services import libraries

    roms, gamelists = volume / "ROMs", volume / "ES-DE" / "gamelists"
    (roms / "psx").mkdir(parents=True)
    _cue(roms / "psx" / "Game.cue")
    data = (
        b'<?xml version="1.0"?>\n'
        b"<gameList>\n\t<game><path>./Game.cue</path></game>\n</gameList>\n"
    )
    gamelist = _file(gamelists / "psx" / "gamelist.xml", data)
    library = libraries.register(str(roms))
    libraries.update(library.id, es_frontend=True, gamelists_folder=str(gamelists))
    source = roms / "psx" / "Game.cue"

    plan = await convert_routes.delete_plan(DeletePlanRequest(
        file_paths=[str(source)], mode=ConversionMode.CREATECD,
    ))
    job = await convert_routes.create_job(JobCreateRequest(
        file_path=str(source), mode=ConversionMode.CREATECD,
        duplicate_action=DuplicateAction.SKIP, delete_on_verify=True,
        container_only=CONTAINER_ONLY,
    ))
    await convert_routes.job_manager._process_job(job.id)

    assert {"referrer": "gamelist", "target": str(gamelist), "entries": 1, "edits": [
        {"old": "./Game.cue", "new": "./Game.chd"},
    ]} in plan["items"][0]["referrer_changes"]
    assert job.status == JobStatus.COMPLETED, job.error_message
    assert gamelist.read_bytes() == data.replace(b"Game.cue", b"Game.chd")
    assert not source.exists() and (roms / "psx" / "Game.chd").is_file()
    assert {
        "referrer": "gamelist", "target": str(gamelist), "status": "updated", "entries": 1,
    } in ledger.row_from_job(job)["referrers_updated"]


@pytest.mark.asyncio
async def test_a_broken_esde_gamelist_keeps_the_source_in_a_job(volume, db):
    from services import libraries

    roms, gamelists = volume / "ROMs", volume / "gamelists"
    (roms / "psx").mkdir(parents=True)
    _cue(roms / "psx" / "Game.cue")
    gamelist = _file(gamelists / "psx" / "gamelist.xml", b"<gameList><game><path>./Game.cue")
    library = libraries.register(str(roms))
    libraries.update(library.id, es_frontend=True, gamelists_folder=str(gamelists))
    source = roms / "psx" / "Game.cue"

    job = await convert_routes.create_job(JobCreateRequest(
        file_path=str(source), mode=ConversionMode.CREATECD,
        duplicate_action=DuplicateAction.SKIP, delete_on_verify=True,
        container_only=CONTAINER_ONLY,
    ))
    await convert_routes.job_manager._process_job(job.id)

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert job.message.startswith("converted, source kept: couldn't update gamelist.xml (")
    assert source.is_file() and (roms / "psx" / "Game.chd").is_file()
    assert gamelist.read_bytes() == b"<gameList><game><path>./Game.cue"


# ---- the view -----------------------------------------------------------------


def test_the_rules_editor_offers_the_folder_beside_the_switch():
    root = Path(__file__).resolve().parents[1]
    editor = (root / "src/lib/components/views/RuleEditor.svelte").read_text()
    assert "ES-DE gamelists folder" in editor
    assert "source.setGamelistsFolder" in editor
    for store in ("libraries.svelte.js", "romm.svelte.js"):
        assert "gamelists_folder" in (root / "src/lib/stores" / store).read_text()
    shots = (root / "shots.yml").read_text()
    readme = (root / "README.md").read_text()
    for theme in ("light", "dark"):
        assert f"output: docs/screenshots/rules-esde-gamelists-{theme}.png" in shots
        assert f"docs/screenshots/rules-esde-gamelists-{theme}.png" in readme
