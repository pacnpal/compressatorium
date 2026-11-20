"""RomM connects itself (#532): discovery, pairing code, library path, RomM's
own structure and exclusions, and a connection test that names the field.

Every RomM here is :class:`FakeRomm`, answering in RomM's own shapes (see its
docstring for the sources).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from routes import romm as romm_routes
from services import db as _db
from services.romm import RommClient, RommError
from services.romm import connect as romm_connect
from services.rules.library import PlacementUnknown

from config import settings as app_settings

from .fake_romm import FakeRomm

TOKEN = {
    "id": 3, "name": "compressatorium", "scopes": ["platforms.read", "roms.read"],
    "expires_at": None, "last_used_at": None, "created_at": "2026-11-20T00:00:00Z",
    "user_id": 1, "raw_token": "rmm_" + "a" * 64,
}


@pytest.fixture(name="romm_db")
def _romm_db(tmp_path: Path, monkeypatch):
    from services.romm import settings as romm_settings

    if _db.engine is not None:
        _db.engine.dispose()
    _db.init_engine(str(tmp_path / "compressatorium.db"), create_schema=True)
    romm_settings.reset_for_tests()
    romm_connect.reset_for_tests()
    monkeypatch.setattr(app_settings, "chd_volumes", str(tmp_path))
    yield tmp_path
    romm_settings.reset_for_tests()
    romm_connect.reset_for_tests()
    if _db.engine is not None:
        _db.engine.dispose()
    _db.engine = None
    _db.SessionLocal = None


@pytest.fixture(name="fake")
def _fake():
    fake = FakeRomm(
        version="5.4.1", fs_platforms=["snes", "gba"],
        structure="roms/{platform}/{game}", pair_codes={"ABCD2345": TOKEN},
        excluded={"EXCLUDED_SINGLE_EXT": ["bak"], "EXCLUDED_SINGLE_FILES": ["*.nfo"]},
    ).start()
    yield fake
    fake.stop()


# ---- pairing ----------------------------------------------------------------


def test_the_client_trades_a_pairing_code_for_its_token(fake):
    client = RommClient(base_url=fake.url, token="")
    # Dashes and case don't matter, as in RomM.
    assert client.exchange_pair_code("abcd-2345")["raw_token"] == TOKEN["raw_token"]
    # Single use.
    with pytest.raises(RommError) as caught:
        client.exchange_pair_code("ABCD2345")
    assert caught.value.status == 404


@pytest.mark.asyncio
async def test_pairing_saves_the_url_and_token_and_never_returns_the_token(romm_db, fake):
    from services.romm import settings as romm_settings

    answer = await romm_routes.pair_romm(
        romm_routes.PairRequest(url=fake.url, code="ABCD-2345"),
    )

    assert romm_settings.effective()["url"] == fake.url
    assert romm_settings.token() == TOKEN["raw_token"]
    assert TOKEN["raw_token"] not in repr(answer)
    assert answer["settings"]["token_set"] is True
    # What the token can't do yet, so the user knows before a feature fails.
    assert answer["missing_scopes"] == ["roms.write", "tasks.run"]


@pytest.mark.asyncio
async def test_a_wrong_pairing_code_says_to_get_a_new_one(romm_db, fake):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as caught:
        await romm_routes.pair_romm(romm_routes.PairRequest(url=fake.url, code="ZZZZ9999"))
    assert caught.value.status_code == 400
    assert "Pair" in caught.value.detail


# ---- discovery --------------------------------------------------------------


@pytest.mark.asyncio
async def test_discovery_finds_romm_and_its_library(romm_db, fake):
    library = romm_db / "romm" / "library"
    (library / "roms" / "snes").mkdir(parents=True)
    (library / "roms" / "gba").mkdir()

    found = await romm_connect.discover(("http://127.0.0.1:1", fake.url), timeout=2)

    assert found == {"url": fake.url, "version": "5.4.1", "library_root": str(library)}
    # Only the public heartbeat was asked: nothing about the owner is sent.
    assert fake.paths() == ["/api/heartbeat"]


@pytest.mark.asyncio
async def test_discovery_with_nothing_answering_finds_nothing(romm_db):
    assert await romm_connect.discover(("http://127.0.0.1:1",), timeout=2) is None


def test_the_library_path_follows_romms_platform_folders(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "chd_volumes", str(tmp_path))
    # RomM's library, and a decoy holding one of the names.
    (tmp_path / "media" / "romm" / "roms" / "snes").mkdir(parents=True)
    (tmp_path / "media" / "romm" / "roms" / "gba").mkdir()
    (tmp_path / "other" / "snes").mkdir(parents=True)

    found = romm_connect.find_library_root(["snes", "gba"])

    assert found == str(tmp_path / "media" / "romm")
    assert romm_connect.find_library_root(["n64"]) is None


# ---- the connection test ------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unreachable_romm_fails_only_the_check_it_ran(romm_db):
    result = await romm_routes.test_romm_connection(
        romm_routes.RommSettingsPatch(url="http://127.0.0.1:1", token="rmm_x"),
    )
    assert result["reachable"] is False
    # Not tried, so not failed.
    assert result["authorized"] is None
    assert result["library_root_mounted"] is None
    assert result["field"] == "url"


@pytest.mark.asyncio
async def test_no_token_names_the_token_field(romm_db, fake):
    result = await romm_routes.test_romm_connection(
        romm_routes.RommSettingsPatch(url=fake.url),
    )
    assert result["reachable"] is True
    assert result["authorized"] is False
    assert result["field"] == "token"
    assert "ROMM_" not in result["error"]


@pytest.mark.asyncio
async def test_the_connection_test_suggests_the_library_path(romm_db, fake):
    library = romm_db / "lib"
    (library / "roms" / "snes").mkdir(parents=True)

    result = await romm_routes.test_romm_connection(
        romm_routes.RommSettingsPatch(url=fake.url, token="rmm_x"),
    )

    assert result["authorized"] is True
    assert result["library_root_mounted"] is None
    assert result["library_root_suggestion"] == str(library)
    assert result["field"] == "library_root"


# ---- RomM's structure and exclusions ------------------------------------------


@pytest.mark.parametrize(("template", "parsed"), [
    ("roms/{platform}/{game}", (("roms",), ())),
    ("{platform}/roms/{game}", ((), ("roms",))),
    ("roms/{platform}/{category}/{game}", (("roms",), (None,))),
    ("{game}", None),
    ("{x}/{platform}/{game}", None),
    ("roms/../{platform}/{game}", None),
])
def test_romms_structure_templates_parse(template, parsed):
    assert romm_connect.parse_structure(template) == parsed


@pytest.mark.asyncio
async def test_the_layout_is_read_from_romm(romm_db, fake):
    from services.romm import settings as romm_settings

    await romm_settings.save({"url": fake.url, "token": "rmm_x"})
    await romm_connect.refresh_layout()

    assert romm_connect.structure() == (("roms",), ())
    assert romm_connect.excluded("Game.BAK")
    assert romm_connect.excluded("Game.nfo")
    assert not romm_connect.excluded("Game.iso")


@pytest.fixture(name="romm_root")
def _romm_root(tmp_path, monkeypatch):
    from services.romm import client as romm_client_module

    monkeypatch.setattr(app_settings, "chd_volumes", str(tmp_path))
    root = tmp_path / "romm"
    root.mkdir()
    monkeypatch.setattr(
        type(romm_client_module.romm_client), "library_root",
        property(lambda _self: str(root)),
    )
    monkeypatch.setattr(romm_client_module.romm_client, "platforms", lambda: [
        {"id": 3, "slug": "ps2", "fs_slug": "ps2"},
    ])
    return root


def test_placement_follows_romms_declared_structure(romm_root):
    from services.romm.library import romm_library

    # Nothing on disk to tell by: RomM says where.
    with patch.object(romm_connect, "structure", return_value=((), ("roms",))):
        placed = romm_library.placement("ps2")
    assert placed.folder == str(romm_root / "ps2" / "roms")
    assert placed.guessed is False


def test_placement_refuses_a_structure_with_a_folder_per_game(romm_root):
    from services.romm.library import romm_library

    with patch.object(romm_connect, "structure", return_value=(("roms",), (None,))), \
            pytest.raises(PlacementUnknown):
        romm_library.placement("ps2")


@pytest.mark.asyncio
async def test_a_rule_skips_files_romm_excludes(romm_db):
    """A file RomM ignores is never converted, and a target format RomM
    ignores isn't written at all (the game would vanish from RomM)."""
    from services.job_manager import job_manager
    from services.romm import auto as romm_auto
    from services.romm import settings as romm_settings
    from services.rules import sweep as rules_sweep

    lib = romm_db / "library" / "roms" / "gc"
    lib.mkdir(parents=True)
    (lib / "Game.iso").write_bytes(b"\0" * 32)
    (lib / "Skip.iso").write_bytes(b"\0" * 32)
    await romm_settings.save({"url": "http://romm:8080", "library_root": str(lib.parents[1])})
    await romm_auto.set_rules({"7": {"mode": "dolphin_rvz", "enabled": True}})
    roms = [
        {"id": n, "name": name, "full_path": f"roms/gc/{name}.iso", "fs_name": f"{name}.iso",
         "fs_size_bytes": 32, "platform_slug": "ngc"}
        for n, name in ((1, "Game"), (2, "Skip"))
    ]

    def ignores(name: str) -> bool:
        return name == "Skip.iso"

    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(job_manager, "get_active_job_candidates", return_value=[]), \
            patch.object(rules_sweep, "is_within_configured_volumes", return_value=True), \
            patch.object(romm_connect, "excluded", side_effect=ignores):
        preview = await romm_auto.sweep(ignore_schedule=True, dry_run=True)
    assert preview["queued"] == 1, preview
    assert preview["skipped_filtered"] == 1, preview

    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(job_manager, "get_active_job_candidates", return_value=[]), \
            patch.object(rules_sweep, "is_within_configured_volumes", return_value=True), \
            patch.object(romm_connect, "excluded", side_effect=lambda n: n.endswith(".rvz")):
        refused = await romm_auto.sweep(ignore_schedule=True, dry_run=True)
    assert refused["queued"] == 0, refused
    assert {"platform_id": 7, "error": "output_excluded"} in refused["errors"], refused
