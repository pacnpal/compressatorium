"""The check level on a Rule, and the upgrade that moves delete-source Rules to Round-trip (#302).

A Rule says which check runs before a sweep's job deletes its Source:
``round_trip`` (the default) or ``container_only``. The field lives on the
shared Rule schema in ``services/rules``, so every Library gets it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
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


# ----------------------------------------------------------------------
# the schema
# ----------------------------------------------------------------------


def test_a_rule_defaults_to_round_trip() -> None:
    from services.rules.rules import default_rule, normalize_rule

    assert default_rule()["check_level"] == "round_trip"
    rule = normalize_rule({"mode": "dolphin_rvz", "delete_on_verify": True})
    assert rule["check_level"] == "round_trip"


def test_a_rule_keeps_container_only() -> None:
    from services.rules.rules import normalize_rule

    rule = normalize_rule({
        "mode": "dolphin_rvz", "delete_on_verify": True, "check_level": "container_only",
    })
    assert rule["check_level"] == "container_only"


def test_an_unknown_check_level_falls_back_to_round_trip() -> None:
    """The stronger check, never the weaker one, for a value nobody can read."""
    from services.rules.rules import normalize_rule

    rule = normalize_rule({
        "mode": "dolphin_rvz", "delete_on_verify": True, "check_level": "bytes",
    })
    assert rule["check_level"] == "round_trip"


@pytest.mark.asyncio
async def test_the_rules_api_saves_and_returns_the_check_level(db: Path) -> None:
    """Through the routes, with the #296 response shape (no new top-level keys)."""
    got = await romm_routes.get_romm_rules()
    assert got["defaults"]["check_level"] == "round_trip"
    saved = await romm_routes.put_romm_rules({"7": {
        "mode": "dolphin_rvz", "delete_on_verify": True, "check_level": "container_only",
    }})
    assert saved["rules"]["7"]["check_level"] == "container_only"
    got = await romm_routes.get_romm_rules()
    assert got["rules"]["7"]["check_level"] == "container_only"


# ----------------------------------------------------------------------
# sweeps
# ----------------------------------------------------------------------

ROMS = [{
    "id": 1, "name": "Game", "full_path": "roms/gc/Game.iso",
    "fs_name": "Game.iso", "platform_slug": "ngc",
}]


async def _configured_romm(db: Path) -> Path:
    from services.romm import settings as romm_settings

    lib = db / "library" / "roms" / "gc"
    lib.mkdir(parents=True)
    (lib / "Game.iso").write_bytes(b"\0" * 32)
    await romm_settings.save({
        "url": "http://romm:8080", "library_root": str(db / "library"),
    })
    return lib


async def _sweep(rule: dict, *, dry_run: bool = False):
    from services.romm import auto as romm_auto
    from services.rules import sweep as rules_sweep

    queued = AsyncMock(return_value=[MagicMock()])
    with patch.object(romm_routes.romm_client, "roms", return_value=ROMS), \
            patch.object(job_manager, "get_active_job_candidates", return_value=[]), \
            patch.object(job_manager, "create_batch_jobs", queued), \
            patch.object(rules_sweep, "is_within_configured_volumes", return_value=True):
        await romm_auto.set_rules({"7": {"mode": "dolphin_rvz", "enabled": True, **rule}})
        result = await romm_auto.sweep(ignore_schedule=True, dry_run=dry_run)
    return queued, result


@pytest.mark.asyncio
@pytest.mark.parametrize(("level", "container_only"), [
    ("round_trip", False),
    ("container_only", True),
])
async def test_a_sweep_passes_the_rule_check_level_to_its_jobs(
    db: Path, level: str, container_only: bool,
) -> None:
    await _configured_romm(db)
    queued, _ = await _sweep({"delete_on_verify": True, "check_level": level})
    assert queued.await_args.kwargs["container_only"] is container_only


@pytest.mark.asyncio
async def test_a_rule_that_keeps_its_source_takes_no_opt_out(db: Path) -> None:
    """container_only opts out of a delete check. Without a delete there is none."""
    await _configured_romm(db)
    queued, _ = await _sweep({"verify_after": True, "check_level": "container_only"})
    assert queued.await_args.kwargs["delete_on_verify"] is False
    assert queued.await_args.kwargs["container_only"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(("rule", "shown"), [
    ({"delete_on_verify": True}, "round_trip"),
    ({"delete_on_verify": True, "check_level": "container_only"}, "container_only"),
    # Nothing is deleted, so there is no check before a delete to show, and
    # the summary keeps the shape #296 pinned.
    ({"check_level": "container_only"}, None),
])
async def test_preview_shows_the_check_level(db: Path, rule: dict, shown) -> None:
    await _configured_romm(db)
    queued, preview = await _sweep(rule, dry_run=True)
    queued.assert_not_awaited()
    assert preview["dry_run"] is True
    assert preview["platforms"][0].get("check_level") == shown


@pytest.mark.asyncio
async def test_preview_shows_the_check_that_runs_on_a_mode_without_a_digest(db: Path) -> None:
    """A stored round_trip on a mode that cannot Round-trip deletes on Intact.

    Read from the registry, so a mode that gains a digest (#300) shows
    round_trip with no change here.
    """
    from services.tools import registry

    await _configured_romm(db)
    with patch.object(registry, "mode_supports_round_trip", return_value=False):
        _, preview = await _sweep({"delete_on_verify": True}, dry_run=True)
    assert preview["platforms"][0]["check_level"] == "container_only"


@pytest.mark.parametrize(("delete", "container_only", "supported", "runs"), [
    (True, False, True, True),
    (True, True, True, False),
    (True, False, False, False),
    (False, False, True, False),
])
def test_one_helper_decides_whether_a_round_trip_runs(
    delete: bool, container_only: bool, supported: bool, runs: bool,
) -> None:
    from services.round_trip import round_trip_applies
    from services.rules.rules import effective_check_level
    from services.tools import registry

    with patch.object(registry, "mode_supports_round_trip", return_value=supported):
        assert round_trip_applies(
            "dolphin_rvz", delete_on_verify=delete, container_only=container_only,
        ) is runs
        level = effective_check_level({
            "mode": "dolphin_rvz", "delete_on_verify": delete,
            "check_level": "container_only" if container_only else "round_trip",
        })
    assert level == (None if not delete else "round_trip" if runs else "container_only")


@pytest.mark.asyncio
async def test_the_job_path_asks_the_same_helper() -> None:
    """`_delete_check` reads nothing extra when the shared helper says no."""
    from services import job_manager as jm

    job = MagicMock(delete_on_verify=True, container_only=False)
    job.mode.value = "dolphin_rvz"
    tool = MagicMock()
    with patch.object(jm, "round_trip_applies", return_value=False) as applies, \
            patch.object(jm, "round_trip", AsyncMock()) as ran:
        got = await jm.job_manager._delete_check(job, tool, "/s.iso", "/o.rvz", None)
    assert (got.check, got.fail_reason, got.keep_reason) == ("intact", None, None)
    applies.assert_called_once_with(
        "dolphin_rvz", delete_on_verify=True, container_only=False,
    )
    ran.assert_not_awaited()


# ----------------------------------------------------------------------
# the upgrade: stored delete-source Rules move to Round-trip, once
# ----------------------------------------------------------------------

UPGRADE_KEY = "rules.check_level_upgrade"

# Rules as an earlier release stored them: no check_level at all.
DELETING = {"mode": "dolphin_rvz", "enabled": True, "delete_on_verify": True}
KEEPING = {"mode": "dolphin_rvz", "enabled": True}


@pytest.mark.asyncio
async def test_the_upgrade_sets_every_deleting_rule_to_round_trip(db: Path) -> None:
    # A row a hand edit left at container_only is switched too: the upgrade
    # says every stored delete-source Rule moves.
    await preferences_store.put("rules", {
        "romm:7": DELETING,
        "romm:8": KEEPING,
        "romm:9": {**DELETING, "check_level": "container_only"},
        "folder:/a:gc": DELETING,
    })

    await romm_routes.get_romm_rules()

    stored = await preferences_store.get("rules")
    assert stored["romm:7"] == {**DELETING, "check_level": "round_trip"}
    assert stored["romm:9"]["check_level"] == "round_trip"
    # Every Library's Rules, not only the one being read.
    assert stored["folder:/a:gc"]["check_level"] == "round_trip"
    # A Rule that keeps its Source is left exactly as it was.
    assert stored["romm:8"] == KEEPING
    marker = await preferences_store.get(UPGRADE_KEY)
    assert marker["switched"] == ["folder:/a:gc", "romm:7", "romm:9"]
    assert marker["upgraded_at"].endswith("Z")


@pytest.mark.asyncio
async def test_the_upgrade_runs_once(db: Path) -> None:
    await preferences_store.put("rules", {"romm:7": DELETING})
    await romm_routes.get_romm_rules()

    # The operator opts out afterwards. Later reads must not undo it.
    await romm_routes.put_romm_rules({"7": {**DELETING, "check_level": "container_only"}})
    got = await romm_routes.get_romm_rules()
    assert got["rules"]["7"]["check_level"] == "container_only"
    assert (await preferences_store.get("rules"))["romm:7"]["check_level"] == "container_only"
    assert (await preferences_store.get(UPGRADE_KEY))["switched"] == ["romm:7"]


@pytest.mark.asyncio
async def test_a_crash_before_the_marker_repeats_the_upgrade(db: Path) -> None:
    """The marker is written last, so a crash after the Rules leaves it unwritten."""
    await preferences_store.put("rules", {"romm:7": DELETING, "romm:8": KEEPING})
    real_put = preferences_store.put

    async def crash_on_marker(key, value):
        if key == UPGRADE_KEY:
            raise RuntimeError("power cut")
        return await real_put(key, value)

    with patch.object(preferences_store, "put", side_effect=crash_on_marker), \
            pytest.raises(RuntimeError):
        await romm_routes.get_romm_rules()
    assert await preferences_store.get(UPGRADE_KEY) is None

    got = await romm_routes.get_romm_rules()
    assert got["rules"]["7"]["check_level"] == "round_trip"
    assert await preferences_store.get("rules") == {
        "romm:7": {**DELETING, "check_level": "round_trip"}, "romm:8": KEEPING,
    }
    assert (await preferences_store.get(UPGRADE_KEY))["switched"] == ["romm:7"]


@pytest.mark.asyncio
async def test_a_fresh_install_switches_nothing(db: Path) -> None:
    await romm_routes.get_romm_rules()
    assert await preferences_store.get("rules") is None
    assert (await preferences_store.get(UPGRADE_KEY))["switched"] == []


@pytest.mark.asyncio
async def test_pre_296_rules_are_switched_as_they_are_copied(db: Path) -> None:
    """RomM's old rows predate the check level too."""
    await preferences_store.put("romm.rules", {"7": DELETING, "8": KEEPING})

    got = await romm_routes.get_romm_rules()

    assert got["rules"]["7"]["check_level"] == "round_trip"
    assert await preferences_store.get("rules") == {
        "romm:7": {**DELETING, "check_level": "round_trip"}, "romm:8": KEEPING,
    }
    # The old row is still a copy as of the upgrade (#296).
    assert await preferences_store.get("romm.rules") == {"7": DELETING, "8": KEEPING}
    assert (await preferences_store.get(UPGRADE_KEY))["switched"] == ["romm:7"]


@pytest.mark.asyncio
async def test_pre_296_rules_copied_after_the_upgrade_are_switched_too(db: Path) -> None:
    """Another Library can run the upgrade before RomM's copy."""
    from services.rules import store

    from tests.test_rules_library_296 import _FolderLike

    await preferences_store.put("romm.rules", {"7": DELETING})
    await store.read(store.RULES_KEY, _FolderLike(db))
    assert (await preferences_store.get(UPGRADE_KEY))["switched"] == []

    got = await romm_routes.get_romm_rules()

    assert got["rules"]["7"]["check_level"] == "round_trip"
    assert (await preferences_store.get(UPGRADE_KEY))["switched"] == ["romm:7"]


@pytest.mark.asyncio
async def test_the_first_notice_read_sees_pre_296_rules(db: Path) -> None:
    """The editor may ask for the notice before anything has read RomM's Rules."""
    from routes import rules as rules_routes

    await preferences_store.put("romm.rules", {"7": DELETING})
    got = await rules_routes.get_rule_notices()
    assert got["check_level"]["show"] is True
    assert got["check_level"]["switched"] == 1


# ----------------------------------------------------------------------
# the one-time notice
# ----------------------------------------------------------------------


@pytest.fixture
def no_digest_for_createhd(monkeypatch):
    """Since #301 every delete-source mode Round-trips, so a mode without a
    digest is simulated: createhd stands in for one."""
    from services.tools import registry

    real = registry.mode_supports_round_trip
    monkeypatch.setattr(
        registry, "mode_supports_round_trip",
        lambda mode: mode != "createhd" and real(mode),
    )


@pytest.mark.asyncio
async def test_the_notice_shows_after_an_upgrade_that_switched_rules(db: Path) -> None:
    from routes import rules as rules_routes

    await preferences_store.put("rules", {"romm:7": DELETING, "romm:8": KEEPING})
    got = await rules_routes.get_rule_notices()
    assert got == {"check_level": {"show": True, "switched": 1, "container_check": 0}}


@pytest.mark.asyncio
async def test_the_notice_counts_only_rules_that_now_round_trip(
    db: Path, no_digest_for_createhd,
) -> None:
    """A delete-source Rule on a mode with no digest keeps the container check."""
    from routes import rules as rules_routes

    await preferences_store.put("rules", {
        "romm:7": DELETING, "romm:9": {**DELETING, "mode": "createhd"},
    })
    got = await rules_routes.get_rule_notices()
    assert got == {"check_level": {"show": True, "switched": 1, "container_check": 1}}


@pytest.mark.asyncio
async def test_no_notice_when_no_switched_rule_can_round_trip(
    db: Path, no_digest_for_createhd,
) -> None:
    from routes import rules as rules_routes

    await preferences_store.put("rules", {"romm:9": {**DELETING, "mode": "createhd"}})
    got = await rules_routes.get_rule_notices()
    assert got == {"check_level": {"show": False, "switched": 0, "container_check": 1}}


@pytest.mark.asyncio
async def test_dismissing_the_notice_is_remembered(db: Path) -> None:
    from routes import rules as rules_routes

    await preferences_store.put("rules", {"romm:7": DELETING})
    await rules_routes.get_rule_notices()

    got = await rules_routes.dismiss_check_level_notice()
    assert got == {"check_level": {"show": False, "switched": 1, "container_check": 0}}
    assert (await rules_routes.get_rule_notices())["check_level"]["show"] is False
    assert (await preferences_store.get("rules.notices"))["check_level"].endswith("Z")


def test_the_notice_routes_are_served() -> None:
    """Registered on the app. Their behaviour is tested by calling them above."""
    import main

    assert main.app.url_path_for("get_rule_notices") == "/api/rules/notices"
    assert main.app.url_path_for("dismiss_check_level_notice") == (
        "/api/rules/notices/check-level/dismiss"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [None, {"romm:8": KEEPING}])
async def test_no_notice_when_nothing_was_switched(db: Path, stored) -> None:
    """A fresh install, or Rules that keep their Source, changed nothing to explain."""
    from routes import rules as rules_routes

    if stored is not None:
        await preferences_store.put("rules", stored)
    got = await rules_routes.get_rule_notices()
    assert got == {"check_level": {"show": False, "switched": 0, "container_check": 0}}
