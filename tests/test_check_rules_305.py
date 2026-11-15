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


# ----------------------------------------------------------------------
# a run: Verify, last check times, Findings, the notification
# ----------------------------------------------------------------------


@pytest.fixture(name="verifier")
def _verifier(monkeypatch):
    """chdman's Verify stubbed: ``verdicts[name]`` is the result for that file.

    The binary is the only thing stubbed; the run goes through the tool's own
    ``verify`` call. A file with no verdict passes.
    """
    verdicts: dict[str, dict] = {}
    calls: list[str] = []
    tool = registry.get("chdman")

    async def verify(path, *, cancel_event=None):
        name = Path(path).name
        calls.append(name)
        return verdicts.get(name, {"valid": True, "message": "Intact"})

    async def bound(path, *, cancel_event=None):
        return 0

    monkeypatch.setattr(tool, "verify", verify)
    monkeypatch.setattr(tool, "verify_timeout", bound)
    return verdicts, calls


@pytest.fixture(name="sent")
def _sent(monkeypatch):
    """Every notification raised, by kind. Nothing leaves the process."""
    from services import notifications

    events: list = []
    monkeypatch.setattr(notifications, "notify", events.append)
    return events


def _not_intact() -> list[dict]:
    from services import findings

    return [f for f in findings.list_findings(include_dismissed=True) if f["kind"] == "not_intact"]


async def _run(library_id: str) -> dict:
    from routes import libraries as routes
    from services.rules import check

    result = await routes.run_library(library_id)
    await check.drain()
    return result


@pytest.mark.asyncio
async def test_a_run_checks_the_picked_images_and_one_failure_makes_one_finding(
    tree: Path, ready, verifier, sent,
) -> None:
    from services import image_checks

    verdicts, calls = verifier
    verdicts["Bolt.chd"] = {"valid": False, "message": "CHD hunk 12 CRC mismatch"}
    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 6})
    _seed(lib, {"ps2/Aria.chd": 2, "ps2/Bolt.chd": 10, "ps2/Dune.chd": 1})
    before = image_checks.last_checked(lib)

    await _run(lib)

    # The stalest two, through the tool's own Verify.
    assert calls == ["Cove.chd", "Bolt.chd"]
    after = image_checks.last_checked(lib)
    # A pass and a fail both move the last check time; the rest stay.
    assert after["ps2/Cove.chd"] > before["ps2/Bolt.chd"]
    assert after["ps2/Bolt.chd"] > before["ps2/Bolt.chd"]
    assert after["ps2/Aria.chd"] == before["ps2/Aria.chd"]
    assert after["ps2/Dune.chd"] == before["ps2/Dune.chd"]
    # One failure, one Finding.
    (finding,) = _not_intact()
    assert finding["image_key"] == "ps2/Bolt.chd"
    assert finding["library_id"] == lib
    assert "CRC mismatch" in finding["reason"]
    # One "check failed" notification for the sweep, naming the Image.
    (event,) = [e for e in sent if e.kind == "check_failed"]
    assert "Bolt.chd" in event.body
    assert [f["image"] for f in event.data["failed"]] == ["ps2/Bolt.chd"]


@pytest.mark.asyncio
async def test_passing_a_later_check_clears_the_finding(tree: Path, ready, verifier, sent) -> None:
    verdicts, _calls = verifier
    verdicts["Cove.chd"] = {"valid": False, "message": "bad hunk"}
    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 3})
    _seed(lib, {"ps2/Aria.chd": 2, "ps2/Bolt.chd": 3, "ps2/Dune.chd": 1})
    await _run(lib)
    assert [f["image_key"] for f in _not_intact()] == ["ps2/Cove.chd"]

    # Restored from a backup: the next check of it passes.
    verdicts.pop("Cove.chd")
    _seed(lib, {"ps2/Aria.chd": 20, "ps2/Bolt.chd": 30, "ps2/Dune.chd": 10, "ps2/Cove.chd": 40})
    await _run(lib)

    assert _not_intact() == []


@pytest.mark.parametrize("verdict", [
    {"valid": False, "inconclusive": True, "message": "chdman is not installed"},
    {"valid": False, "cancelled": True, "message": "Cancelled"},
    RuntimeError("the verifier crashed"),
])
@pytest.mark.asyncio
async def test_an_inconclusive_check_changes_nothing(
    tree: Path, ready, verifier, sent, monkeypatch, verdict,
) -> None:
    from services import image_checks

    verdicts, _calls = verifier
    if isinstance(verdict, Exception):
        async def broken(path, *, cancel_event=None):
            raise verdict

        monkeypatch.setattr(registry.get("chdman"), "verify", broken)
    else:
        verdicts["Cove.chd"] = verdict
    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 3})
    _seed(lib, {"ps2/Aria.chd": 2, "ps2/Bolt.chd": 3, "ps2/Dune.chd": 1})

    await _run(lib)

    # Cove keeps no check time, so the next run picks it first again.
    assert "ps2/Cove.chd" not in image_checks.last_checked(lib)
    assert _not_intact() == []
    assert [e for e in sent if e.kind == "check_failed"] == []


@pytest.mark.asyncio
async def test_an_image_no_tool_can_check_is_skipped_and_costs_nothing(
    tree: Path, ready,
) -> None:
    from routes import libraries as routes

    from .test_platform_detect import _ps2

    # A PS2 ISO: chdman checks only CHDs, and dolphin-tool is not a PS2 tool.
    _ps2(tree / "ps2" / "Able.iso")
    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 3})
    _seed(lib, {"ps2/Aria.chd": 2, "ps2/Bolt.chd": 3, "ps2/Dune.chd": 1})

    result = await routes.preview_library(lib)

    (summary,) = result["platforms"]
    # Able was never checked and sorts first, yet Cove gets the whole budget.
    assert summary["candidates"] == ["Cove.chd"]
    assert summary["check_bytes"] == 3 * GB
    assert summary["skipped_uncheckable"] == 1


@pytest.mark.asyncio
async def test_an_image_larger_than_the_budget_is_still_checked(tree: Path, ready) -> None:
    from routes import libraries as routes

    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 1})

    result = await routes.preview_library(lib)

    # Never stuck behind an Image bigger than the whole budget: it goes alone.
    assert result["platforms"][0]["candidates"] == ["Aria.chd"]


@pytest.mark.asyncio
async def test_a_dismissed_finding_stays_dismissed_while_the_file_is_unchanged(
    tree: Path, ready, verifier, sent,
) -> None:
    from services import findings

    verdicts, _calls = verifier
    verdicts["Cove.chd"] = {"valid": False, "message": "bad hunk"}
    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 3})
    _seed(lib, {"ps2/Aria.chd": 2, "ps2/Bolt.chd": 3, "ps2/Dune.chd": 1})
    await _run(lib)
    (finding,) = _not_intact()
    findings.dismiss(finding["id"])

    # Checked again, failing again, the file untouched: still dismissed.
    _seed(lib, {"ps2/Aria.chd": 20, "ps2/Bolt.chd": 30, "ps2/Dune.chd": 10, "ps2/Cove.chd": 40})
    await _run(lib)
    assert [f["dismissed"] for f in _not_intact()] == [True]

    # The file changed and still fails: the person decides again.
    _sparse(tree / "ps2" / "Cove.chd", 3 * GB - 512)
    _seed(lib, {"ps2/Aria.chd": 20, "ps2/Bolt.chd": 30, "ps2/Dune.chd": 10, "ps2/Cove.chd": 40})
    await _run(lib)
    assert [f["dismissed"] for f in _not_intact()] == [False]


@pytest.mark.asyncio
async def test_a_run_is_one_job_in_the_queue_and_no_ledger_row(
    tree: Path, ready, verifier, sent,
) -> None:
    from models import ConversionMode, JobStatus
    from services import ledger
    from services.job_manager import job_manager

    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 6})
    before = {j.id for j in job_manager.get_all_jobs()}

    await _run(lib)

    (job,) = [j for j in job_manager.get_all_jobs() if j.id not in before]
    assert job.mode == ConversionMode.CHECK
    assert job.status == JobStatus.COMPLETED
    assert job.message == "2 Intact, 0 not Intact"
    await ledger.record(job)
    assert await ledger.recent(10) == []


@pytest.mark.asyncio
async def test_a_preview_checks_nothing(tree: Path, ready, verifier) -> None:
    from routes import libraries as routes
    from services import image_checks

    _verdicts, calls = verifier
    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 6})

    await routes.preview_library(lib)

    assert calls == []
    assert image_checks.last_checked(lib) == {}


@pytest.mark.asyncio
async def test_removing_the_library_drops_its_check_times(tree: Path, ready, verifier) -> None:
    from routes import libraries as routes
    from services import image_checks

    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 6})
    await _run(lib)
    assert image_checks.last_checked(lib)

    await routes.remove_library(lib)

    assert image_checks.last_checked(lib) == {}


# ----------------------------------------------------------------------
# RomM: the same check, through the Library interface
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_rules_work_on_a_romm_library(db: Path, ready, verifier, sent) -> None:
    from routes import romm as romm_routes
    from services import image_checks
    from services.romm import settings as romm_settings
    from services.rules import check

    for name in ("Aria", "Bolt"):
        _sparse(db / "library" / "roms" / "ps2" / f"{name}.chd", 3 * GB)
    await romm_settings.save({"url": "http://romm:8080", "library_root": str(db / "library")})
    roms = [
        {
            "id": rom_id, "name": name, "full_path": f"roms/ps2/{name}.chd",
            "fs_name": f"{name}.chd", "fs_size_bytes": 3 * GB, "platform_slug": "ps2",
        }
        for rom_id, name in ((1, "Aria"), (2, "Bolt"))
    ]
    verdicts, calls = verifier
    verdicts["Bolt.chd"] = {"valid": False, "message": "bad hunk"}
    await romm_routes.put_romm_rules({"7": {"enabled": True, "check_budget_gb": 6}})

    with patch.object(romm_routes.romm_client, "roms", return_value=roms):
        result = await romm_routes.run_auto_convert(romm_routes.SweepRequest(platform_ids=[7]))
        await check.drain()

    assert result["checks"] == 2
    assert calls == ["Aria.chd", "Bolt.chd"]
    # RomM's Image key is the ROM id.
    assert set(image_checks.last_checked("romm")) == {"1", "2"}
    (finding,) = _not_intact()
    assert (finding["library_id"], finding["image_key"]) == ("romm", "2")
    (event,) = [e for e in sent if e.kind == "check_failed"]
    assert "Bolt.chd" in event.body
