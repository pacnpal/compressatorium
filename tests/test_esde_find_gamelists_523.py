"""ES-DE's gamelists folder, found in the Volumes (#523).

ES-DE 2.0+ reads only ``ES-DE/gamelists/<system>/gamelist.xml``, never a
gamelist in the ROM folders, so the Rules editor searches the Volumes for that
tree and offers what it finds with one click.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from services import libraries

from config import settings


@pytest.fixture(name="vol")
def _vol(tmp_path, monkeypatch):
    volume = tmp_path / "vol"
    volume.mkdir()
    monkeypatch.setattr(settings, "chd_volumes", str(volume))
    return volume


def _gamelists(at: Path, system: str = "ps2") -> Path:
    (at / system).mkdir(parents=True)
    (at / system / "gamelist.xml").write_text("<gameList/>")
    return at


def test_finds_the_es_de_gamelists_folder(vol):
    found = _gamelists(vol / "home" / "ES-DE" / "gamelists")
    assert libraries.find_gamelists_folders() == [str(found.resolve())]


def test_a_gamelists_folder_with_no_system_gamelist_is_not_offered(vol):
    (vol / "ES-DE" / "gamelists" / "ps2").mkdir(parents=True)
    assert libraries.find_gamelists_folders() == []


def test_the_search_stops_at_its_depth(vol):
    deep = vol
    for name in ("a", "b", "c", "d", "e"):
        deep = deep / name
    _gamelists(deep / "ES-DE" / "gamelists")
    assert libraries.find_gamelists_folders() == []


def test_a_symlink_out_of_the_volumes_is_not_followed(vol, tmp_path):
    outside = _gamelists(tmp_path / "outside" / "ES-DE" / "gamelists")
    os.symlink(outside.parent.parent, vol / "link")
    assert libraries.find_gamelists_folders() == []


def test_es_de_folders_come_first(vol):
    other = _gamelists(vol / "a" / "gamelists")
    esde = _gamelists(vol / "z" / "ES-DE" / "gamelists")
    assert libraries.find_gamelists_folders() == [str(esde.resolve()), str(other.resolve())]


@pytest.mark.asyncio
async def test_the_route_lists_what_it_found(vol):
    from routes import libraries as library_routes

    found = _gamelists(vol / "ES-DE" / "gamelists")
    assert await library_routes.find_gamelists_folders() == {"folders": [str(found.resolve())]}


def test_the_editor_offers_a_frontend_select_and_no_muos_claim():
    root = Path(__file__).resolve().parents[1]
    editor = (root / "src/lib/components/library/FrontendSelect.svelte").read_text()
    assert 'label="Game list app"' in editor
    assert "api.findGamelistsFolders" in editor
    assert "Knulli or muOS" not in editor
    gamelist = (root / "app/services/referrers/gamelist.py").read_text()
    assert "Knulli, muOS" not in gamelist
