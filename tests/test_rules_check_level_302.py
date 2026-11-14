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
