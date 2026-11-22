"""Apply recommended (#541, plan item 12): ``GET /libraries/{id}/recommended-rules``.

For each console with games and no conversion Rule, the route samples its
games, routes each the way Auto does and keeps the most common mode. It saves
nothing; the Automation tab merges the answer into the Rules it holds.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from services.tools import registry

from .test_folder_library_303 import _db_fixture, _ready, _register
from .test_platform_detect import _ps1, _write

__all__ = ["_db_fixture", "_ready"]


@pytest.fixture(name="mixed")
def _mixed(db: Path) -> Path:
    roms = db / "roms"
    for name in ("Crash", "Spyro"):
        _ps1(roms / "psx" / f"{name}.bin")
        (roms / "psx" / f"{name}.cue").write_text(
            f'FILE "{name}.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n',
        )
    _write(roms / "gba" / "Advance Wars.gba", b"\0" * 4096)
    return roms


@pytest.mark.asyncio
@pytest.mark.usefixtures("ready")
async def test_each_console_gets_its_best_format(mixed: Path) -> None:
    from routes import libraries as routes

    lib = await _register(mixed)
    plan = await routes.recommended_rules(lib["id"])

    assert plan["rules"] == {
        "psx": {"mode": "createcd", "enabled": True},
        "gba": {"mode": "romz_7z", "enabled": True},
    }
    assert plan["skipped"] == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("ready")
async def test_a_console_with_a_rule_is_left_alone(mixed: Path) -> None:
    from routes import libraries as routes

    lib = await _register(mixed)
    await routes.put_library_rules(lib["id"], {"rules": {"psx": {"mode": "createcd"}}})

    plan = await routes.recommended_rules(lib["id"])

    assert set(plan["rules"]) == {"gba"}
    # Nothing was saved: the stored Rules are only the one put above.
    stored = await routes.get_library_rules(lib["id"])
    assert set(stored["rules"]) == {"psx"}


@pytest.mark.asyncio
async def test_a_console_with_no_ready_tool_is_skipped(mixed: Path, monkeypatch) -> None:
    from routes import libraries as routes

    async def only_chdman(tool, **_kwargs):
        return tool.id == "chdman"

    monkeypatch.setattr(registry, "tool_is_ready", only_chdman)
    lib = await _register(mixed)

    plan = await routes.recommended_rules(lib["id"])

    assert plan["rules"] == {"psx": {"mode": "createcd", "enabled": True}}
    assert [(s["platform_id"], s["name"]) for s in plan["skipped"]] == [
        ("gba", "Game Boy Advance"),
    ]
    assert plan["skipped"][0]["reason"]


@pytest.mark.asyncio
async def test_an_unknown_library_is_404(db: Path) -> None:
    from fastapi import HTTPException
    from routes import libraries as routes

    with pytest.raises(HTTPException) as err:
        await routes.recommended_rules("folder:999")
    assert err.value.status_code == 404


@pytest.mark.asyncio
async def test_each_console_says_where_its_games_are(mixed: Path) -> None:
    """Browse links to where a console's games are, not to
    where new ones would go (RomM style puts those in roms/<console>)."""
    from routes import libraries as routes

    lib = await _register(mixed)
    await routes.update_library(lib["id"], routes.UpdateLibraryRequest(layout="romm"))

    listed = await routes.library_platforms(lib["id"])

    folders = {p["id"]: p["folder"] for p in listed["platforms"]}
    assert folders == {"psx": "psx", "gba": "gba"}


@pytest.mark.asyncio
async def test_an_ambiguous_console_folder_does_not_fail_the_listing(mixed: Path, monkeypatch) -> None:
    """a console whose folder can't be told (a symlinked
    alias) gets no folder and a note; the rest of the listing still answers."""
    from routes import libraries as routes
    from services import libraries
    from services.libraries.folder import FolderLibrary
    from services.rules.library import AmbiguousFolder

    lib = await _register(mixed)
    real = FolderLibrary.platform_folders

    def ambiguous_psx(self):
        find = real(self)

        def folder(path):
            if "/psx/" in path:
                raise AmbiguousFolder(path, ["psx", "PlayStation"])
            return find(path)
        return folder

    monkeypatch.setattr(FolderLibrary, "platform_folders", ambiguous_psx)
    libraries.reset_for_tests()

    listed = await routes.library_platforms(lib["id"])

    rows = {p["id"]: p for p in listed["platforms"]}
    assert rows["psx"]["folder"] is None and rows["psx"]["folder_note"]
    assert rows["gba"]["folder"] == "gba" and rows["gba"]["folder_note"] is None


@pytest.mark.asyncio
async def test_the_console_folders_are_listed_once_per_listing(mixed: Path, monkeypatch) -> None:
    """each console's folder is found from one listing of
    the root, not one per game (5,000 games took about 5 s)."""
    from routes import libraries as routes
    from services import libraries
    from services.libraries import folder as folder_module

    for n in range(30):
        _write(mixed / "gba" / f"Game {n}.gba", b"\0" * 4096)
    lib = await _register(mixed)
    listed_parents: list[str] = []
    real = folder_module.folder_holder

    def counting(parent):
        listed_parents.append(parent)
        return real(parent)

    monkeypatch.setattr(folder_module, "folder_holder", counting)
    libraries.reset_for_tests()

    listed = await routes.library_platforms(lib["id"])

    assert {p["id"]: p["folder"] for p in listed["platforms"]} == {"psx": "psx", "gba": "gba"}
    assert listed_parents == [str(mixed)]


@pytest.mark.asyncio
async def test_games_mostly_in_the_root_open_the_root(db: Path) -> None:
    """two PS2 games in the root and one in ps2/ open
    the root, where most of them are."""
    from routes import libraries as routes

    from .test_platform_detect import _ps2

    roms = db / "roms"
    _ps2(roms / "Ico.iso")
    _ps2(roms / "Okami.iso")
    _ps2(roms / "ps2" / "Rez.iso")
    lib = await _register(roms)

    listed = await routes.library_platforms(lib["id"])

    rows = {p["id"]: p for p in listed["platforms"]}
    assert rows["ps2"]["images"] == 3
    assert rows["ps2"]["folder"] is None
