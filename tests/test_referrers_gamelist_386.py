"""The gamelist.xml Referrer, opt-in per Library (#386).

An EmulationStation frontend (ES-DE, Batocera, Knulli, muOS) keys each game in
its Platform folder's ``gamelist.xml`` on ``<path>``. When a Library is marked
as used by one, a conversion that replaces a Source rewrites the matching
``<path>`` text before the Source goes, and leaves every other byte as it was.

Format variants go through the Referrer contract (the hook the job calls, with
a Library that turns the Referrer on); the whole flow goes through a conversion
job and the delete-confirmation route against a registered folder Library.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from services import db as _db
from services import referrers
from services.referrers import Phase
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

from .test_referrers_m3u import CONTAINER_ONLY
from .test_referrers_m3u import _volume as _m3u_volume

_FIXTURES = (_m3u_volume,)


class _Library:
    """The part of a Library the hook asks: whether an opt-in Referrer is on."""

    id = "folder:test"
    name = "Test"

    def __init__(self, on: bool = True) -> None:
        self._on = on

    def referrer_enabled(self, referrer_id: str) -> bool:
        return self._on and referrer_id == "gamelist"

    def referrer_options(self, referrer_id: str) -> dict:
        # No ES-DE gamelists folder (#387): the Platform folder layout.
        return {}


ON = _Library()

ESDE = (
    b'<?xml version="1.0"?>\r\n'
    b"<gameList>\r\n"
    b"\t<!-- scraped by ES-DE -->\r\n"
    b'\t<game id="42" source="ScreenScraper.fr">\r\n'
    b"\t\t<path>./Game.iso</path>\r\n"
    b"\t\t<name>Game</name>\r\n"
    b"\t\t<desc>A &quot;classic&quot; &amp; more</desc>\r\n"
    b"\t\t<image>./media/Game.iso.png</image>\r\n"
    b"\t\t<favorite>true</favorite>\r\n"
    b"\t\t<playcount>12</playcount>\r\n"
    b"\t</game>\r\n"
    b"\t<game>\r\n"
    b"\t\t<name>Other</name>\r\n"
    b"\t\t<path>./Other.iso</path>\r\n"
    b"\t</game>\r\n"
    b"\t<folder><path>./multi</path><name>Multi</name></folder>\r\n"
    b"</gameList>\r\n"
)


@pytest.fixture(name="vol")
def _vol(tmp_path, monkeypatch):
    """tmp_path is the Volume, so the walk for gamelists stops at it."""
    monkeypatch.setattr(settings, "chd_volumes", str(tmp_path))
    return tmp_path


def _file(path: Path, data: bytes = b"disc") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


async def _replace(old: Path, new: Path, library=ON, extra: dict | None = None):
    """The hook as the job calls it: the output already exists."""
    if not new.exists():
        _file(new, b"output")
    renames = {str(old): str(new), **(extra or {})}
    return await referrers.update_before_source_removal(str(old), renames, library=library)


def _changed_spans(before: bytes, after: bytes) -> list[tuple[bytes, bytes]]:
    """The (old, new) byte runs that differ, found by a common prefix and suffix."""
    start = 0
    while start < min(len(before), len(after)) and before[start] == after[start]:
        start += 1
    end = 0
    while (end < min(len(before), len(after)) - start
           and before[-1 - end] == after[-1 - end]):
        end += 1
    return [(before[start:len(before) - end], after[start:len(after) - end])]


# ---- the Referrer's declarations ------------------------------------------


def test_the_gamelist_referrer_is_opt_in_and_in_the_file_phase():
    assert GamelistReferrer.opt_in is True
    assert GamelistReferrer.phase is Phase.FILE
    assert "gamelist" in {r.id for r in referrers.registry.all()}


# ---- format variants, through the Referrer contract -----------------------


@pytest.mark.asyncio
async def test_only_the_path_text_changes_byte_for_byte(vol):
    psx = vol / "psx"
    source = _file(psx / "Game.iso")
    _file(psx / "Other.iso")
    gamelist = _file(psx / "gamelist.xml", ESDE)

    outcome = await _replace(source, psx / "Game.chd")

    assert outcome.ok, outcome.reason
    after = gamelist.read_bytes()
    # One span differs, and it is the path text; the declaration, CRLF
    # endings, comment, attributes, entities, the media path that also says
    # "Game.iso", the other game and the folder entry are untouched.
    assert _changed_spans(ESDE, after) == [(b"iso", b"chd")]
    assert after == ESDE.replace(b"<path>./Game.iso</path>", b"<path>./Game.chd</path>")
    assert (psx / "gamelist.xml.bak").read_bytes() == ESDE
    assert outcome.records == [{
        "referrer": "gamelist", "target": str(gamelist), "status": "updated", "entries": 1,
    }]


@pytest.mark.asyncio
@pytest.mark.parametrize("library", [None, _Library(on=False)])
async def test_a_library_without_the_switch_leaves_the_gamelist_alone(vol, library):
    psx = vol / "psx"
    source = _file(psx / "Game.iso")
    gamelist = _file(psx / "gamelist.xml", ESDE)

    outcome = await _replace(source, psx / "Game.chd", library=library)
    planned = referrers.plan_changes(str(source), {str(source): str(psx / "Game.chd")}, library)

    assert outcome.ok
    assert outcome.records == []
    assert planned == []
    assert gamelist.read_bytes() == ESDE
    assert not (psx / "gamelist.xml.bak").exists()


@pytest.mark.asyncio
async def test_a_nested_image_and_a_bare_relative_path(vol):
    """The gamelist sits in the Platform folder; the Image is in a subfolder.
    A cue and its track are both replaced; only the cue is listed."""
    psx = vol / "psx"
    cue = _file(psx / "Multi Disc" / "Game (Disc 1).cue")
    track = _file(psx / "Multi Disc" / "Game (Disc 1).bin")
    data = (
        b"<gameList><game><path>./Multi Disc/Game (Disc 1).cue</path></game>"
        b"<game><path>Multi Disc/Game (Disc 1).cue</path></game></gameList>"
    )
    gamelist = _file(psx / "gamelist.xml", data)
    output = psx / "Multi Disc" / "Game (Disc 1).chd"

    outcome = await _replace(cue, output, extra={str(track): str(output)})

    assert outcome.ok, outcome.reason
    assert gamelist.read_bytes() == data.replace(b"1).cue", b"1).chd")


@pytest.mark.asyncio
async def test_an_absolute_path_and_backslashes_keep_their_style(vol):
    psx = vol / "psx"
    source = _file(psx / "sub" / "Game.iso")
    absolute = str(source).encode()
    data = (
        b"<gameList><game><path>" + absolute + b"</path></game>"
        b"<game><path>.\\sub\\Game.iso</path></game></gameList>"
    )
    gamelist = _file(psx / "gamelist.xml", data)

    outcome = await _replace(source, psx / "sub" / "Game.chd")

    assert outcome.ok, outcome.reason
    assert gamelist.read_bytes() == data.replace(b"Game.iso", b"Game.chd")


@pytest.mark.asyncio
async def test_an_output_in_another_folder_keeps_the_dot_slash(vol):
    psx = vol / "psx"
    source = _file(psx / "Game.iso")
    data = b"<gameList><game><path>./Game.iso</path></game></gameList>"
    gamelist = _file(psx / "gamelist.xml", data)

    outcome = await _replace(source, psx / "chd" / "Game.chd")

    assert outcome.ok, outcome.reason
    assert gamelist.read_bytes() == data.replace(b"./Game.iso", b"./chd/Game.chd")


@pytest.mark.asyncio
async def test_entries_for_files_that_were_not_replaced_are_untouched(vol):
    psx = vol / "psx"
    source = _file(psx / "Game.iso")
    _file(psx / "other" / "Game.iso")  # same name, another file
    data = (
        b"<gameList><game><path>./other/Game.iso</path></game>"
        b"<game><path>./Game.iso</path></game></gameList>"
    )
    gamelist = _file(psx / "gamelist.xml", data)

    outcome = await _replace(source, psx / "Game.chd")

    assert outcome.ok, outcome.reason
    assert gamelist.read_bytes() == (
        b"<gameList><game><path>./other/Game.iso</path></game>"
        b"<game><path>./Game.chd</path></game></gameList>"
    )


@pytest.mark.asyncio
async def test_a_gamelist_not_naming_the_image_is_left_alone(vol):
    psx = vol / "psx"
    source = _file(psx / "Game.iso")
    data = b"<gameList><game><path>./Other.iso</path></game></gameList>"
    gamelist = _file(psx / "gamelist.xml", data)

    outcome = await _replace(source, psx / "Game.chd")

    assert outcome.ok
    assert outcome.records == []
    assert gamelist.read_bytes() == data
    assert not (psx / "gamelist.xml.bak").exists()


@pytest.mark.asyncio
async def test_a_legacy_encoding_and_escaped_names_survive(vol):
    """A windows-1252 gamelist: the new text is written in that encoding, and
    an escaped ``&amp;`` stays escaped."""
    psx = vol / "psx"
    source = _file(psx / "Tom & Jérôme.iso")
    data = (
        '<?xml version="1.0" encoding="windows-1252"?>\n'
        "<gameList>\n"
        "  <game><path>./Tom &amp; Jérôme.iso</path><name>Pokémon</name></game>\n"
        "</gameList>\n"
    ).encode("cp1252")
    gamelist = _file(psx / "gamelist.xml", data)

    outcome = await _replace(source, psx / "Tom & Jérôme.chd")

    assert outcome.ok, outcome.reason
    assert gamelist.read_bytes() == data.replace(b"me.iso<", b"me.chd<")
    assert b"J\xe9r\xf4me.chd" in gamelist.read_bytes()


@pytest.mark.asyncio
async def test_a_utf8_bom_and_a_name_needing_escape(vol):
    psx = vol / "psx"
    source = _file(psx / "A.iso")
    data = "﻿<gameList><game><path>./A.iso</path></game></gameList>".encode()
    gamelist = _file(psx / "gamelist.xml", data)

    outcome = await _replace(source, psx / "A <&> B.chd")

    assert outcome.ok, outcome.reason
    assert gamelist.read_bytes() == data.replace(b"./A.iso", b"./A &lt;&amp;&gt; B.chd")


@pytest.mark.asyncio
async def test_the_backup_is_kept_once(vol):
    psx = vol / "psx"
    one = _file(psx / "One.iso")
    two = _file(psx / "Two.iso")
    data = (
        b"<gameList><game><path>./One.iso</path></game>"
        b"<game><path>./Two.iso</path></game></gameList>"
    )
    gamelist = _file(psx / "gamelist.xml", data)

    assert (await _replace(one, psx / "One.chd")).ok
    assert (await _replace(two, psx / "Two.chd")).ok

    assert gamelist.read_bytes() == data.replace(b".iso", b".chd")
    assert (psx / "gamelist.xml.bak").read_bytes() == data


def test_planning_writes_nothing_and_counts_the_entries(vol):
    psx = vol / "psx"
    source = _file(psx / "Game.iso")
    _file(psx / "Game.chd")
    data = (
        b"<gameList><game><path>./Game.iso</path></game>"
        b"<game><path>Game.iso</path></game></gameList>"
    )
    gamelist = _file(psx / "gamelist.xml", data)

    [change] = referrers.plan_changes(
        str(source), {str(source): str(psx / "Game.chd")}, ON,
    )

    assert gamelist.read_bytes() == data
    assert change.to_dict() == {
        "referrer": "gamelist", "target": str(gamelist), "entries": 2,
        "edits": [
            {"old": "./Game.iso", "new": "./Game.chd"},
            {"old": "Game.iso", "new": "Game.chd"},
        ],
    }


# ---- fail closed: the Source is kept ---------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("data, why", [
    (b"<gameList><game><path>./Game.iso</path></game>", "can't be parsed"),
    (b"<gameList><game><path>./Game.iso</game></gameList>", "can't be parsed"),
    (
        b'<!DOCTYPE gameList [<!ENTITY g "Game.iso">]>'
        b"<gameList><game><path>./&g;</path></game></gameList>",
        "can't be parsed",
    ),
    (
        b"<gameList><game><path><![CDATA[./Game.iso]]></path></game></gameList>",
        "can't be edited in place",
    ),
    (
        b"<gameList><game><path>/elsewhere/roms/psx/Game.iso</path></game></gameList>",
        "doesn't point at it",
    ),
    # Codex PRRT_kwDOQ9nULM64JsQt: not a gamelist at all, so it can't say the
    # Source isn't listed.
    (b"", "isn't a gamelist"),
    (b'<?xml version="1.0"?>\n', "isn't a gamelist"),
    (b"./Game.iso\n", "isn't a gamelist"),
    (b"<gameList></gameList>\nstray ./Game.iso\n", "isn't a gamelist"),
])
async def test_a_gamelist_that_cant_be_updated_keeps_the_source(vol, data, why):
    psx = vol / "psx"
    source = _file(psx / "Game.iso")
    gamelist = _file(psx / "gamelist.xml", data)

    outcome = await _replace(source, psx / "Game.chd")

    assert not outcome.ok
    assert outcome.failed == str(gamelist)
    assert why in outcome.reason
    assert outcome.kept_message.startswith("converted, source kept: couldn't update gamelist.xml")
    assert gamelist.read_bytes() == data
    assert source.is_file()


@pytest.mark.asyncio
async def test_a_utf16_gamelist_naming_the_source_keeps_it(vol):
    psx = vol / "psx"
    source = _file(psx / "Game.iso")
    data = '<?xml version="1.0" encoding="UTF-16"?><gameList><game><path>./Game.iso</path>' \
        "</game></gameList>"
    gamelist = _file(psx / "gamelist.xml", data.encode("utf-16"))

    outcome = await _replace(source, psx / "Game.chd")

    assert not outcome.ok
    assert "can't be edited in place" in outcome.reason
    assert gamelist.read_bytes() == data.encode("utf-16")


@pytest.mark.asyncio
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file permissions",
)
async def test_a_read_only_gamelist_keeps_the_source(vol):
    psx = vol / "psx"
    source = _file(psx / "Game.iso")
    gamelist = _file(psx / "gamelist.xml", ESDE)
    gamelist.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    try:
        outcome = await _replace(source, psx / "Game.chd")
    finally:
        gamelist.chmod(stat.S_IRUSR | stat.S_IWUSR)

    assert not outcome.ok
    assert outcome.reason == "is read-only"
    assert gamelist.read_bytes() == ESDE


@pytest.mark.asyncio
async def test_a_symlinked_gamelist_keeps_the_source(vol):
    psx = vol / "psx"
    source = _file(psx / "Game.iso")
    real = _file(vol / "elsewhere.xml", ESDE)
    (psx / "gamelist.xml").symlink_to(real)

    outcome = await _replace(source, psx / "Game.chd")

    assert not outcome.ok
    assert outcome.reason == "is a symlink"
    assert real.read_bytes() == ESDE


# ---- the whole flow: a folder Library, a job, the delete confirmation ------


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


GAMELIST = (
    b'<?xml version="1.0"?>\n'
    b"<gameList>\n"
    b"  <game>\n"
    b"    <path>./Game (Disc 1).cue</path>\n"
    b"    <name>Game</name>\n"
    b"    <playcount>3</playcount>\n"
    b"  </game>\n"
    b"  <game>\n"
    b"    <path>./Other Game.cue</path>\n"
    b"  </game>\n"
    b"</gameList>\n"
)


def _library(volume: Path, *, es_frontend: bool):
    from services import libraries

    library = libraries.register(str(volume))
    if es_frontend:
        libraries.update(library.id, es_frontend=True)
    return library


async def _convert(source: Path):
    job = await convert_routes.create_job(JobCreateRequest(
        file_path=str(source), mode=ConversionMode.CREATECD,
        duplicate_action=DuplicateAction.SKIP, delete_on_verify=True,
        container_only=CONTAINER_ONLY,
    ))
    await convert_routes.job_manager._process_job(job.id)
    return job


@pytest.mark.asyncio
async def test_a_conversion_in_an_es_library_updates_the_gamelist(volume, db):
    _library(volume, es_frontend=True)
    gamelist = volume / "gamelist.xml"
    gamelist.write_bytes(GAMELIST)
    source = volume / "Game (Disc 1).cue"

    plan = await convert_routes.delete_plan(DeletePlanRequest(
        file_paths=[str(source)], mode=ConversionMode.CREATECD,
    ))
    job = await _convert(source)

    # The confirmation lists the gamelist and how many entries it changes.
    changes = plan["items"][0]["referrer_changes"]
    assert {"referrer": "gamelist", "target": str(gamelist), "entries": 1, "edits": [
        {"old": "./Game (Disc 1).cue", "new": "./Game (Disc 1).chd"},
    ]} in changes
    assert job.status == JobStatus.COMPLETED, job.error_message
    assert gamelist.read_bytes() == GAMELIST.replace(b"(Disc 1).cue", b"(Disc 1).chd")
    assert (volume / "gamelist.xml.bak").read_bytes() == GAMELIST
    assert not source.exists() and (volume / "Game (Disc 1).chd").is_file()
    # The Ledger names the gamelist and the number of entries.
    assert {
        "referrer": "gamelist", "target": str(gamelist), "status": "updated", "entries": 1,
    } in ledger.row_from_job(job)["referrers_updated"]


@pytest.mark.asyncio
async def test_a_library_without_the_switch_never_touches_the_gamelist(volume, db):
    _library(volume, es_frontend=False)
    gamelist = volume / "gamelist.xml"
    gamelist.write_bytes(GAMELIST)
    source = volume / "Game (Disc 1).cue"

    plan = await convert_routes.delete_plan(DeletePlanRequest(
        file_paths=[str(source)], mode=ConversionMode.CREATECD,
    ))
    job = await _convert(source)

    assert all(c["referrer"] != "gamelist" for c in plan["items"][0]["referrer_changes"])
    assert job.status == JobStatus.COMPLETED, job.error_message
    assert not source.exists()
    assert gamelist.read_bytes() == GAMELIST
    assert not (volume / "gamelist.xml.bak").exists()


@pytest.mark.asyncio
async def test_a_broken_gamelist_keeps_the_source_and_the_output(volume, db):
    _library(volume, es_frontend=True)
    gamelist = volume / "gamelist.xml"
    broken = GAMELIST.replace(b"</gameList>", b"")
    gamelist.write_bytes(broken)
    source = volume / "Game (Disc 1).cue"

    plan = await convert_routes.delete_plan(DeletePlanRequest(
        file_paths=[str(source)], mode=ConversionMode.CREATECD,
    ))
    job = await _convert(source)

    assert any(
        "gamelist.xml" in w and "the source will be kept" in w
        for w in plan["items"][0].get("warnings", [])
    )
    assert job.status == JobStatus.COMPLETED, job.error_message
    assert job.message.startswith("converted, source kept: couldn't update gamelist.xml (")
    assert "can't be parsed" in job.message
    assert source.is_file() and (volume / "Game (Disc 1).bin").is_file()
    assert (volume / "Game (Disc 1).chd").is_file()
    assert job.deleted_paths == []
    assert gamelist.read_bytes() == broken


# ---- the Library setting ----------------------------------------------------


def test_the_switch_is_off_by_default_and_persisted(tmp_path, db, monkeypatch):
    from services import libraries

    monkeypatch.setattr(settings, "chd_volumes", str(tmp_path))
    (tmp_path / "roms").mkdir()
    library = libraries.register(str(tmp_path / "roms"))
    assert library.referrer_enabled("gamelist") is False
    assert libraries.describe(library)["es_frontend"] is False

    libraries.update(library.id, es_frontend=True)
    libraries.reset_for_tests()  # read back from the database
    again = libraries.get(library.id)

    assert again.referrer_enabled("gamelist") is True
    assert again.referrer_enabled("romm_swap") is False
    assert libraries.describe(again)["es_frontend"] is True


@pytest.mark.asyncio
async def test_the_library_api_sets_the_switch(tmp_path, db, monkeypatch):
    from routes import libraries as library_routes

    monkeypatch.setattr(settings, "chd_volumes", str(tmp_path))
    (tmp_path / "roms").mkdir()
    created = await library_routes.register_library(
        library_routes.RegisterLibraryRequest(path=str(tmp_path / "roms")),
    )
    assert created["es_frontend"] is False

    updated = await library_routes.update_library(
        created["id"], library_routes.UpdateLibraryRequest(es_frontend=True),
    )

    assert updated["es_frontend"] is True
    listed = {lib["id"]: lib for lib in await library_routes.list_libraries()}
    assert listed[created["id"]]["es_frontend"] is True


@pytest.mark.asyncio
async def test_the_romm_library_has_the_switch_in_its_settings(db):
    from services.romm import settings as romm_settings
    from services.romm.library import romm_library

    await romm_settings.load(force=True)
    assert romm_settings.public()["es_frontend"] is False
    assert romm_library.referrer_enabled("gamelist") is False

    await romm_settings.save({"es_frontend": True})

    assert romm_library.referrer_enabled("gamelist") is True
    assert romm_library.referrer_enabled("romm_swap") is False


def test_the_rules_view_shows_the_switch_with_its_note():
    """Static check: the editor offers the switch for every Library, and its
    help text says to close ES-DE first."""
    src = Path(__file__).resolve().parents[1] / "src" / "lib"
    editor = (src / "components" / "library" / "FrontendSelect.svelte").read_text()
    folder = (src / "stores" / "libraries.svelte.js").read_text()
    romm = (src / "stores" / "romm.svelte.js").read_text()
    assert "Game list app" in editor and "Batocera or Knulli" in editor
    assert "Close ES-DE" in editor
    assert "source.setEsFrontend" in editor
    assert "es_frontend" in folder and "es_frontend" in romm


def test_the_switch_has_a_screenshot_entry():
    root = Path(__file__).resolve().parents[1]
    shots = (root / "shots.yml").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")
    for theme in ("light", "dark"):
        name = f"docs/screenshots/library-frontend-{theme}.png"
        assert f"output: {name}" in shots
        assert name in readme
    assert "document.querySelector('.frontend')" in shots


def test_the_delete_confirmation_shows_how_many_entries_change():
    """Static check: each Referrer change reads "updates gamelist.xml, 3 entries"."""
    src = Path(__file__).resolve().parents[1] / "src" / "lib"
    modal = (src / "components" / "modals" / "DeletePlanModal.svelte").read_text()
    assert "updates {shortName(change.target)}, {entryCount(change)}:" in modal
    assert "change?.entries" in modal
