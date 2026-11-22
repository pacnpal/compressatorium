"""Beside RomM's converter (#385): the banner, the Rule flags, the marks and import.

RomM can convert its own library (``ROM_CONVERTO_ENABLED``), and when it does
two converters work on one library. The app reads RomM's converter config when
the RomM status is refreshed and warns; it never blocks. These drive the RomM
routes against :class:`tests.fake_romm.FakeRomm`, which serves the heartbeat and
``/api/config`` in RomM's own shapes.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from routes import romm as romm_routes
from routes import libraries as lib_routes
from services import db as _db
from services.romm import RommClient
from services.romm import repin as romm_repin
from services.tools import registry

from config import settings as app_settings

from .fake_romm import FakeRomm

PLATFORMS = [
    {"id": 7, "slug": "ngc", "display_name": "GameCube", "rom_count": 3},
    {"id": 8, "slug": "psx", "display_name": "PlayStation", "rom_count": 5},
    {"id": 9, "slug": "xbox360", "display_name": "Xbox 360", "rom_count": 1},
    {"id": 10, "slug": "wii", "display_name": "Wii", "rom_count": 2},
    {"id": 11, "slug": "gba", "display_name": "Game Boy Advance", "rom_count": 9},
    {"id": 12, "slug": "ps2", "display_name": "PlayStation 2", "rom_count": 4},
]
FORMATS = {
    "ngc": "rvz", "psx": "chd", "xbox360": "zar", "wii": "rvz", "ps2": "iso",
}


@pytest.fixture(name="fake")
def _fake():
    fake = FakeRomm(platform_formats=FORMATS, platforms=PLATFORMS).start()
    yield fake
    fake.stop()


@pytest.fixture(name="romm_db")
def _romm_db(tmp_path: Path, monkeypatch):
    from services.romm import settings as romm_settings

    if _db.engine is not None:
        _db.engine.dispose()
    _db.init_engine(str(tmp_path / "compressatorium.db"), create_schema=True)
    romm_settings.reset_for_tests()
    monkeypatch.setattr(app_settings, "chd_volumes", str(tmp_path))
    (tmp_path / "library").mkdir()
    yield tmp_path / "library"
    romm_settings.reset_for_tests()
    if _db.engine is not None:
        _db.engine.dispose()
    _db.engine = None
    _db.SessionLocal = None


@pytest_asyncio.fixture(name="romm_env")
async def _romm_env(romm_db, fake):
    """The app pointed at the fake RomM, with its library inside the Volume."""
    from services.romm import settings as romm_settings

    await romm_settings.save({"url": fake.url, "library_root": str(romm_db)})
    return fake


# ----------------------------------------------------------------------
# the client
# ----------------------------------------------------------------------


def test_client_reads_the_converter_from_heartbeat_and_config(fake) -> None:
    """On/off from the heartbeat, the per-Platform targets from /api/config."""
    client = RommClient(base_url=fake.url, token="rmm_test")

    converter = client.converter_config()

    assert converter["enabled"] is True
    assert converter["platform_formats"] == FORMATS
    assert converter["library_targets"]["ngc"] == ["iso", "rvz"]
    assert "gba" not in converter["library_targets"]
    assert fake.paths() == ["/api/heartbeat", "/api/config"]


def test_client_reads_no_converter_from_a_romm_without_one() -> None:
    """A RomM from before the converter has no CONVERTO in its heartbeat."""
    fake = FakeRomm(converter=False).start()
    try:
        client = RommClient(base_url=fake.url, token="rmm_test")
        assert client.converter_config() is None
        # Nothing else is asked of a RomM that has no converter.
        assert fake.paths() == ["/api/heartbeat"]
    finally:
        fake.stop()


def test_client_reads_a_switched_off_converter_without_its_targets() -> None:
    """Off means no banner and no flags, so the targets are not read."""
    fake = FakeRomm(enabled=False, platform_formats=FORMATS).start()
    try:
        client = RommClient(base_url=fake.url, token="rmm_test")
        converter = client.converter_config()
        assert converter == {
            "enabled": False, "platform_formats": {}, "library_targets": None,
        }
        assert fake.paths() == ["/api/heartbeat"]
    finally:
        fake.stop()


def test_client_reuses_a_heartbeat_it_is_handed(fake) -> None:
    """The status route already has the heartbeat; it is not fetched twice."""
    client = RommClient(base_url=fake.url, token="rmm_test")

    client.converter_config(fake.heartbeat())

    assert fake.paths() == ["/api/config"]


# ----------------------------------------------------------------------
# the mode a RomM target maps onto
# ----------------------------------------------------------------------


def test_each_mode_says_which_extensions_it_writes() -> None:
    """Asked of the tool, so input-mapped modes (Z3DS, NSZ) answer too."""
    assert registry.mode_output_extensions("dolphin_rvz") == frozenset({".rvz"})
    assert registry.mode_output_extensions("createcd") == frozenset({".chd"})
    assert {".nsz", ".xcz"} <= registry.mode_output_extensions("nsz_compress")
    assert ".zcia" in registry.mode_output_extensions("z3ds_compress")
    assert ".iso" not in registry.mode_output_extensions("xiso_rewrite")


def test_romm_targets_map_onto_this_apps_modes() -> None:
    from services.romm import converter

    assert converter.modes_for_target("ngc", "rvz")[0] == "dolphin_rvz"
    assert converter.modes_for_target("psx", "chd") == ["createcd"]
    assert converter.modes_for_target("ps2", "chd")[0] == "createdvd"
    assert converter.modes_for_target("ps2", "zso") == ["zso_compress"]
    assert converter.modes_for_target("switch", "nsz") == ["nsz_compress"]
    assert converter.modes_for_target("3ds", "z3ds") == ["z3ds_compress"]
    # rom-converto writes Xbox 360 ZAR here too (#422). Nothing writes a WBFS.
    assert converter.modes_for_target("xbox360", "zar") == ["zar_compress"]
    assert converter.modes_for_target("ps2", "iso") == []
    assert converter.modes_for_target("wii", "wbfs") == []
    # A mode that cannot run unattended is never offered (CHDMAN copy).
    assert "copy" not in converter.modes_for_target("psx", "chd")


# ----------------------------------------------------------------------
# GET /api/romm/status
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_reports_romms_converter_when_it_is_on(romm_env) -> None:
    with patch.object(romm_repin, "count_pending", return_value=0):
        status = await romm_routes.romm_status()

    converter = status["converter"]
    assert status["connected"] is True
    assert converter["enabled"] is True
    assert converter["platform_formats"] == FORMATS
    assert converter["library_targets"]["wii"] == ["iso", "rvz", "wbfs"]
    # This app's modes for each RomM target, the import's pick first.
    assert converter["modes"]["ngc"][0] == "dolphin_rvz"
    assert converter["modes"]["psx"] == ["createcd"]
    assert converter["modes"]["xbox360"] == ["zar_compress"]
    assert converter["modes"]["ps2"] == []
    assert converter["error"] is None
    # One heartbeat, shared by the version and the converter.
    assert romm_env.paths().count("/api/heartbeat") == 1


@pytest.mark.asyncio
async def test_status_has_no_converter_on_a_romm_without_one(romm_db) -> None:
    from services.romm import settings as romm_settings

    fake = FakeRomm(converter=False).start()
    try:
        await romm_settings.save({"url": fake.url, "library_root": str(romm_db)})
        with patch.object(romm_repin, "count_pending", return_value=0):
            status = await romm_routes.romm_status()
    finally:
        fake.stop()

    assert status["connected"] is True
    assert status["converter"] is None


@pytest.mark.asyncio
async def test_status_says_the_converter_is_off(romm_db) -> None:
    from services.romm import settings as romm_settings

    fake = FakeRomm(enabled=False, platform_formats=FORMATS).start()
    try:
        await romm_settings.save({"url": fake.url, "library_root": str(romm_db)})
        with patch.object(romm_repin, "count_pending", return_value=0):
            status = await romm_routes.romm_status()
    finally:
        fake.stop()

    assert status["converter"]["enabled"] is False
    assert status["converter"]["platform_formats"] == {}


@pytest.mark.asyncio
async def test_a_failed_config_read_still_reports_the_converter_on(romm_db) -> None:
    """The banner is owed even when the targets cannot be read."""
    from services.romm import settings as romm_settings

    fake = FakeRomm(platform_formats=FORMATS, config_status=500).start()
    try:
        await romm_settings.save({"url": fake.url, "library_root": str(romm_db)})
        with patch.object(romm_repin, "count_pending", return_value=0):
            status = await romm_routes.romm_status()
    finally:
        fake.stop()

    assert status["connected"] is True
    assert status["error"] is None
    assert status["converter"]["enabled"] is True
    assert status["converter"]["platform_formats"] == {}
    assert status["converter"]["library_targets"] is None
    assert status["converter"]["error"]


# ----------------------------------------------------------------------
# import RomM's targets as Rules
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_plan_covers_only_platforms_with_no_rule(romm_env) -> None:
    await lib_routes.put_library_rules("romm", {"rules": {"10": {"mode": "dolphin_wia"}}})

    plan = await romm_routes.romm_converter_import_plan()

    assert plan["enabled"] is True
    assert plan["rules"] == {
        "7": {"mode": "dolphin_rvz", "target": "rvz"},
        "8": {"mode": "createcd", "target": "chd"},
        "9": {"mode": "zar_compress", "target": "zar"},
    }
    # PS2 to ISO is a RomM target nothing here writes: skipped, and said so.
    assert plan["skipped"] == [
        {"platform_id": "12", "name": "PlayStation 2", "slug": "ps2", "target": "iso"},
    ]
    # Wii already has a Rule, so it is left as it is.
    assert plan["existing"] == ["10"]


@pytest.mark.asyncio
async def test_importing_through_the_rules_api_creates_the_planned_rules(romm_env) -> None:
    """The planned Rules saved through PUT /api/libraries/romm/rules, off until turned on."""
    await lib_routes.put_library_rules("romm", {"rules": {"10": {"mode": "dolphin_wia"}}})
    plan = await romm_routes.romm_converter_import_plan()

    current = (await lib_routes.get_library_rules("romm"))["rules"]
    merged = {**current, **{pid: {"mode": e["mode"]} for pid, e in plan["rules"].items()}}
    saved = await lib_routes.put_library_rules("romm", {"rules": merged})

    assert saved["rules"]["7"]["mode"] == "dolphin_rvz"
    assert saved["rules"]["8"]["mode"] == "createcd"
    assert saved["rules"]["7"]["enabled"] is False
    assert saved["rules"]["10"]["mode"] == "dolphin_wia"
    assert saved["rules"]["9"]["mode"] == "zar_compress"
    assert "12" not in saved["rules"]


@pytest.mark.asyncio
async def test_reading_the_converter_changes_no_rule(romm_env) -> None:
    """The warnings alone block and change nothing: no Rule is written."""
    await lib_routes.put_library_rules("romm", {"rules": {"7": {"mode": "dolphin_wia"}}})
    before = await lib_routes.get_library_rules("romm")

    with patch.object(romm_repin, "count_pending", return_value=0):
        await romm_routes.romm_status()
    await romm_routes.romm_converter_import_plan()

    assert await lib_routes.get_library_rules("romm") == before


@pytest.mark.asyncio
async def test_import_plan_is_empty_when_the_converter_is_off(romm_db) -> None:
    from services.romm import settings as romm_settings

    fake = FakeRomm(enabled=False, platform_formats=FORMATS, platforms=PLATFORMS).start()
    try:
        await romm_settings.save({"url": fake.url, "library_root": str(romm_db)})
        plan = await romm_routes.romm_converter_import_plan()
    finally:
        fake.stop()

    assert plan == {"enabled": False, "rules": {}, "skipped": [], "existing": []}
