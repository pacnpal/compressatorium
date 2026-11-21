"""The Rule engine behind the Library interface (#296).

RomM is the first Library. These tests cover what the move adds: the
interface itself, the Library-prefixed storage keys with their one-time
migration, and a second Library running through the same engine without
touching RomM's Rules.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import pytest
import pytest_asyncio
from routes import romm as romm_routes
from services import db as _db
from services.job_manager import job_manager
from services.preferences_store import preferences_store

from config import settings as app_settings


@pytest.fixture(name="db")
def _db_fixture(tmp_path: Path, monkeypatch):
    from services.romm import settings as romm_settings

    if _db.engine is not None:
        _db.engine.dispose()
    _db.init_engine(str(tmp_path / "compressatorium.db"), create_schema=True)
    romm_settings.reset_for_tests()
    monkeypatch.setattr(app_settings, "chd_volumes", str(tmp_path))
    yield tmp_path
    romm_settings.reset_for_tests()
    if _db.engine is not None:
        _db.engine.dispose()
    _db.engine = None
    _db.SessionLocal = None


@pytest_asyncio.fixture(name="romm_lib")
async def _romm_lib(db: Path):
    from services.romm import settings as romm_settings

    lib = db / "library" / "roms" / "gc"
    lib.mkdir(parents=True)
    (lib / "Game.iso").write_bytes(b"\0" * 32)
    await romm_settings.save({
        "url": "http://romm:8080", "library_root": str(db / "library"),
    })
    return lib


ROMS = [
    {
        "id": 4, "name": "Game", "full_path": "roms/gc/Game.iso",
        "fs_name": "Game.iso", "fs_size_bytes": 32, "platform_slug": "ngc",
        "is_identified": True,
    },
    {
        "id": 9, "name": "Homebrew", "full_path": "roms/gc/Homebrew.iso",
        "fs_name": "Homebrew.iso", "platform_slug": "ngc",
    },
]


# ----------------------------------------------------------------------
# the interface, as RomM implements it
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_romm_lists_the_images_of_a_platform(romm_lib: Path) -> None:
    from services.romm.library import romm_library
    from services.rules import Image

    assert romm_library.id == "romm"
    with patch.object(romm_routes.romm_client, "roms", return_value=ROMS):
        images = romm_library.images("7")

    assert [type(i) for i in images] == [Image, Image]
    game, homebrew = images
    # The Image key is RomM's ROM id.
    assert game.key == 4
    assert game.name == "Game.iso"
    assert game.size == 32
    assert game.platform == "ngc"
    assert game.recognized is True
    assert game.path == str(romm_lib / "Game.iso")
    assert homebrew.size == 0
    assert homebrew.recognized is False
    # RomM numbers ROMs as they are added, so the id is its added order.
    assert homebrew.added_at > game.added_at


def test_romm_reads_its_platform_ids_as_integers() -> None:
    from services.romm.library import romm_library

    assert romm_library.canonical_platform_id("07") == "7"
    assert romm_library.canonical_platform_id("ps2") is None
    assert romm_library.platform_value("7") == 7


def test_romm_lists_its_platforms() -> None:
    from services.romm.library import romm_library
    from services.rules import Platform

    catalog = [{"id": 7, "slug": "ngc", "name": "GameCube"}]
    with patch.object(romm_routes.romm_client, "platforms", return_value=catalog):
        assert romm_library.platforms() == [Platform(id="7", slug="ngc", name="GameCube")]


def test_romm_declares_its_capabilities() -> None:
    from services.romm.library import romm_library
    from services.rules import library as rules_library

    assert rules_library.RECOGNIZED_FILTER in romm_library.capabilities
    assert rules_library.CONVERTED_HISTORY in romm_library.capabilities


# ----------------------------------------------------------------------
# storage: keys prefixed with the Library id
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_old_romm_keys_are_copied_once_and_kept(romm_lib: Path) -> None:
    """The pre-#296 rows stay as they were, so a downgrade can still read them."""
    rule = {"mode": "dolphin_rvz", "enabled": True}
    old_state = {"7": {"last_queued": 2, "converted": {"1": {"path": "/x", "pre": ""}}}}
    await preferences_store.put("romm.rules", {"7": rule})
    await preferences_store.put("romm.auto_state", old_state)

    got = await romm_routes.get_romm_rules()
    assert got["rules"]["7"]["mode"] == "dolphin_rvz"
    assert got["state"] == old_state

    assert await preferences_store.get("rules") == {"romm:7": rule}
    assert await preferences_store.get("rules.state") == {"romm:7": old_state["7"]}
    # Kept, untouched: a copy as of the upgrade.
    assert await preferences_store.get("romm.rules") == {"7": rule}
    assert await preferences_store.get("romm.auto_state") == old_state


@pytest.mark.asyncio
async def test_old_romm_keys_are_not_read_a_second_time(romm_lib: Path) -> None:
    await preferences_store.put("romm.rules", {"7": {"mode": "dolphin_rvz"}})
    await romm_routes.get_romm_rules()

    # Something writes the old row again (a downgraded release, say). The
    # move already happened, so it is not merged in.
    await preferences_store.put("romm.rules", {"8": {"mode": "dolphin_rvz"}})
    await preferences_store.put("romm.auto_state", {"7": {"converted": {"1": {}}}})
    got = await romm_routes.get_romm_rules()
    assert list(got["rules"]) == ["7"]
    assert got["state"] == {}


@pytest.mark.asyncio
async def test_saving_rules_writes_only_the_prefixed_keys(romm_lib: Path) -> None:
    await preferences_store.put("romm.rules", {"7": {"mode": "dolphin_rvz"}})
    await romm_routes.put_romm_rules({"9": {"mode": "dolphin_rvz"}})
    assert list(await preferences_store.get("rules")) == ["romm:9"]
    assert await preferences_store.get("romm.rules") == {"7": {"mode": "dolphin_rvz"}}
    # Nothing to copy on a fresh install, and nothing is ever written to the
    # old keys.
    assert await preferences_store.get("romm.auto_state") is None


@pytest.mark.asyncio
async def test_a_fresh_install_writes_no_old_keys(romm_lib: Path) -> None:
    await romm_routes.put_romm_rules({"7": {"mode": "dolphin_rvz"}})
    assert list(await preferences_store.get("rules")) == ["romm:7"]
    assert await preferences_store.get("romm.rules") is None


@pytest.mark.asyncio
async def test_concurrent_first_reads_copy_the_same_data(romm_lib: Path) -> None:
    import asyncio

    from services.romm import auto as romm_auto

    await preferences_store.put("romm.rules", {"7": {"mode": "dolphin_rvz"}})
    await preferences_store.put("romm.auto_state", {"7": {"last_queued": 1}})
    results = await asyncio.gather(
        *(romm_auto.get_rules() for _ in range(5)),
        *(romm_auto.get_state() for _ in range(5)),
    )
    assert all(list(r) == ["7"] for r in results)
    assert await preferences_store.get("rules.state") == {"romm:7": {"last_queued": 1}}


# ----------------------------------------------------------------------
# a second Library through the same engine
# ----------------------------------------------------------------------


class _FolderLike:
    """The smallest Library: one root, one Platform, the Images in it."""

    id = "folder:/test"
    capabilities = frozenset()
    legacy_preference_keys: ClassVar[dict] = {}

    def __init__(self, root: Path) -> None:
        self.root = root

    def configured(self) -> bool:
        return True

    def canonical_platform_id(self, raw):
        return raw if raw == "gc" else None

    def platform_value(self, platform_id):
        return platform_id

    def platforms(self):
        from services.rules import Platform

        return [Platform(id="gc", slug="ngc", name="gc")]

    def images(self, platform_id):
        from services.rules import Image

        return [
            Image(
                key=p.name, name=p.name, size=p.stat().st_size,
                added_at=p.stat().st_mtime, recognized=None, platform="ngc",
                path=str(p),
            )
            for p in sorted((self.root / platform_id).iterdir())
        ]

    def rule_defaults(self):
        return {}

    def max_per_sweep(self):
        return 25

    def schedule_enabled(self):
        return True

    def sweep_blocked(self):
        return None

    def excluded(self, name):
        return False

    def excluded_extension(self, ext):
        return False

    async def refresh(self):
        return None

    def summary_fields(self):
        return {}

    async def after_queue(self, rule, spec, queued, summary, result):
        return None

    async def sweep_extras(self):
        return {}


@pytest.mark.asyncio
async def test_a_second_library_runs_through_the_same_engine(romm_lib: Path) -> None:
    from services.rules import sweep as rules_sweep

    root = romm_lib.parent.parent.parent / "folder"
    (root / "gc").mkdir(parents=True)
    (root / "gc" / "Disc.iso").write_bytes(b"\0" * 16)
    folder = _FolderLike(root)

    await romm_routes.put_romm_rules({"7": {"mode": "dolphin_rvz", "enabled": True}})
    saved = await rules_sweep.set_rules(folder, {
        "gc": {"mode": "dolphin_rvz", "enabled": True},
        "7": {"mode": "dolphin_rvz"},
    })
    assert list(saved) == ["gc"]

    with patch.object(job_manager, "get_active_job_candidates", return_value=[]):
        preview = await rules_sweep.sweep(folder, ignore_schedule=True, dry_run=True)

    assert preview["queued"] == 1, preview
    assert preview["platforms"][0]["platform_id"] == "gc"
    assert preview["platforms"][0]["candidates"] == ["Disc.iso"]
    assert "pending_repins" not in preview

    # Each Library sees only its own Rules.
    assert list(await rules_sweep.get_rules(folder)) == ["gc"]
    assert list((await romm_routes.get_romm_rules())["rules"]) == ["7"]
    assert set(await preferences_store.get("rules")) == {"romm:7", "folder:/test:gc"}
