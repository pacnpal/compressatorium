"""Check Rules (#305): a Rule with no target format checks Images instead.

A check Rule picks the stalest Images of a Platform (never checked first, then
the oldest last check) within a nightly byte budget, and checks them through the
tool's own Verify. A failure becomes a "not Intact" Finding, a later pass
clears it, and a sweep with failures sends one "check failed" notification.

Only the verifier is stubbed: it is the one external binary a check runs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from services import db as _db
from services import platform_detect
from services.tools import registry

from config import settings as app_settings

GB = 1024 ** 3


# ----------------------------------------------------------------------
# the schema
# ----------------------------------------------------------------------


def test_a_rule_with_no_target_format_is_a_check_rule() -> None:
    from services.rules.rules import is_check_rule, normalize_rule

    rule = normalize_rule({
        "enabled": True, "check_budget_gb": 50,
        "window_start": "01:00", "window_end": "05:00", "days": [0, 2],
    })

    assert rule is not None
    assert is_check_rule(rule)
    assert rule["mode"] == ""
    assert rule["check_budget_gb"] == 50
    assert (rule["window_start"], rule["window_end"], rule["days"]) == ("01:00", "05:00", [0, 2])
    # Nightly: a check Rule runs at most once a day, inside its window.
    assert rule["interval_minutes"] == 24 * 60
    # It never converts, so it never deletes.
    assert rule["delete_on_verify"] is False


def test_a_check_rule_budget_is_bounded() -> None:
    from services.rules.rules import normalize_rule

    assert normalize_rule({"check_budget_gb": 0})["check_budget_gb"] == 1
    assert normalize_rule({"check_budget_gb": "lots"})["check_budget_gb"] == 50
    assert normalize_rule({})["check_budget_gb"] == 50


def test_a_conversion_rule_is_not_a_check_rule() -> None:
    from services.rules.rules import is_check_rule, normalize_rule

    rule = normalize_rule({"mode": "createcd", "enabled": True})
    assert not is_check_rule(rule)


# ----------------------------------------------------------------------
# a folder Library: which Images a sweep picks
# ----------------------------------------------------------------------


@pytest.fixture(name="db")
def _db_fixture(tmp_path: Path, monkeypatch):
    from services import libraries, notifications
    from services.romm import settings as romm_settings

    if _db.engine is not None:
        _db.engine.dispose()
    _db.init_engine(str(tmp_path / "compressatorium.db"), create_schema=True)
    romm_settings.reset_for_tests()
    libraries.reset_for_tests()
    notifications._reset_for_tests()
    platform_detect.clear_cache()
    volume = tmp_path / "vol"
    volume.mkdir()
    monkeypatch.setattr(app_settings, "chd_volumes", str(volume))
    yield volume
    platform_detect.clear_cache()
    libraries.reset_for_tests()
    romm_settings.reset_for_tests()
    if _db.engine is not None:
        _db.engine.dispose()
    _db.engine = None
    _db.SessionLocal = None


def _sparse(path: Path, size: int) -> Path:
    """A file of *size* bytes that takes no space on disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.truncate(size)
    return path


@pytest.fixture(name="tree")
def _tree(db: Path) -> Path:
    """``roms/ps2`` with four 3 GB CHDs."""
    roms = db / "roms"
    for name in ("Aria", "Bolt", "Cove", "Dune"):
        _sparse(roms / "ps2" / f"{name}.chd", 3 * GB)
    return roms


@pytest.fixture(name="ready")
def _ready(monkeypatch):
    async def always(_tool):
        return True

    monkeypatch.setattr(registry, "tool_is_ready", always)


async def _folder_library(tree: Path, rule: dict) -> str:
    from routes import libraries as routes

    created = await routes.register_library(routes.RegisterLibraryRequest(path=str(tree)))
    await routes.put_library_rules(created["id"], {"rules": {"ps2": rule}})
    return created["id"]


def _ago(days: float) -> str:
    stamp = datetime.now(timezone.utc) - timedelta(days=days)
    return stamp.isoformat().replace("+00:00", "Z")


def _seed(library_id: str, checked: dict[str, float]) -> None:
    from services import image_checks

    for key, days in checked.items():
        image_checks.record(library_id, key, "intact", at=_ago(days))


@pytest.mark.asyncio
async def test_a_preview_picks_the_stalest_images_within_the_budget(tree: Path, ready) -> None:
    from routes import libraries as routes

    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 6})
    # Cove was never checked; Bolt longest ago, then Aria, then Dune.
    _seed(lib, {"ps2/Aria.chd": 2, "ps2/Bolt.chd": 10, "ps2/Dune.chd": 1})

    result = await routes.preview_library(lib)

    (summary,) = result["platforms"]
    assert summary["check"] is True
    # 3 GB each: Cove and Bolt fill the 6 GB budget; Aria would not fit.
    assert summary["candidates"] == ["Cove.chd", "Bolt.chd"]
    assert summary["checks"] == 2
    assert summary["check_bytes"] == 6 * GB
    assert result["checks"] == 2
    # A check queues no conversion.
    assert result["queued"] == 0
