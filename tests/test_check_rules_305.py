"""Check Rules (#305): a Rule with no target format checks Images instead.

A check Rule picks the stalest Images of a Platform (never checked first, then
the oldest last check) within a nightly byte budget, and checks them through the
tool's own Verify. A failure becomes a "not Intact" Finding, a later pass
clears it, and a sweep with failures sends one "check failed" notification.

Only the verifier is stubbed: it is the one external binary a check runs.
"""

from __future__ import annotations

import asyncio
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
    from services.rules.rules import CHECK, is_check_rule, normalize_rule

    rule = normalize_rule({
        "enabled": True, "check_budget_gb": 50,
        "window_start": "01:00", "window_end": "05:00", "days": [0, 2],
    }, kind=CHECK)

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
    from services.rules.rules import CHECK, normalize_rule

    assert normalize_rule({"check_budget_gb": 0}, kind=CHECK)["check_budget_gb"] == 1
    assert normalize_rule({"check_budget_gb": "lots"}, kind=CHECK)["check_budget_gb"] == 50
    assert normalize_rule({}, kind=CHECK)["check_budget_gb"] == 50


def test_a_conversion_rule_is_not_a_check_rule() -> None:
    from services.rules.rules import is_check_rule, normalize_rule

    rule = normalize_rule({"mode": "createcd", "enabled": True})
    assert not is_check_rule(rule)


def test_a_check_rule_keeps_no_conversion_filters() -> None:
    """The check sweep reads none of them, so none is stored to mislead."""
    from services.rules.rules import CHECK, default_rule, normalize_rule

    rule = normalize_rule({
        "include_pattern": "USA", "exclude_pattern": "Beta", "min_size_mb": 5,
        "max_size_mb": 9, "order": "size_desc", "max_per_run": 3,
        "only_matched": True, "duplicate_action": "overwrite",
    }, kind=CHECK)
    defaults = default_rule()
    for field in (
        "include_pattern", "exclude_pattern", "min_size_mb", "max_size_mb", "order",
        "max_per_run", "only_matched", "duplicate_action",
    ):
        assert rule[field] == defaults[field], field


# ----------------------------------------------------------------------
# the nightly schedule: once per window, never drifting
# ----------------------------------------------------------------------


def _at(day: int, hhmm: str) -> datetime:
    """2026-11-<day> at hh:mm UTC. The 16th is a Monday."""
    hour, minute = (int(x) for x in hhmm.split(":"))
    return datetime(2026, 11, day, hour, minute, tzinfo=timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _nightly(**extra) -> dict:
    from services.rules.rules import CHECK, normalize_rule

    return normalize_rule({
        "enabled": True, "window_start": "22:00", "window_end": "02:00",
        "timezone": "UTC", **extra,
    }, kind=CHECK)


def test_a_late_run_does_not_push_the_next_night_later() -> None:
    from services.rules.schedule import _is_due

    rule = _nightly()
    ran = {"last_run_at": _stamp(_at(16, "23:50"))}

    # The next night opens at 22:00, ten minutes short of 24 hours later.
    assert _is_due(rule, ran, _at(17, "22:00"))
    assert not _is_due(rule, ran, _at(17, "21:59"))


def test_a_run_that_crossed_midnight_counts_for_the_window_it_started_in() -> None:
    from services.rules.schedule import _is_due

    rule = _nightly()
    ran = {"last_run_at": _stamp(_at(16, "23:50"))}

    # Still Monday night's window after midnight: already run.
    assert not _is_due(rule, ran, _at(17, "00:30"))
    assert not _is_due(rule, ran, _at(17, "01:59"))
    # A run that started after midnight belongs to the night before, too.
    after_midnight = {"last_run_at": _stamp(_at(17, "00:10"))}
    assert not _is_due(rule, after_midnight, _at(17, "01:00"))
    assert _is_due(rule, after_midnight, _at(17, "22:00"))


def test_a_nightly_run_honours_its_weekdays() -> None:
    from services.rules.schedule import _is_due

    rule = _nightly(days=[0])   # Monday nights only
    never: dict = {}

    assert _is_due(rule, never, _at(16, "22:30"))
    # Tuesday 01:00 is the tail of Monday night's window.
    assert _is_due(rule, never, _at(17, "01:00"))
    # Tuesday night is not allowed.
    assert not _is_due(rule, never, _at(17, "22:30"))
    ran = {"last_run_at": _stamp(_at(16, "22:30"))}
    assert not _is_due(rule, ran, _at(23, "21:00"))
    assert _is_due(rule, ran, _at(23, "22:00"))


def test_a_run_now_does_not_count_as_the_nightly_run() -> None:
    from services.rules.schedule import _is_due

    rule = _nightly()

    assert _is_due(rule, {"last_manual_run_at": _stamp(_at(16, "22:10"))}, _at(16, "22:20"))


def test_a_check_rule_with_no_window_runs_once_a_day() -> None:
    from services.rules.rules import CHECK, normalize_rule
    from services.rules.schedule import _is_due

    rule = normalize_rule({"enabled": True, "timezone": "UTC"}, kind=CHECK)
    ran = {"last_run_at": _stamp(_at(16, "10:00"))}

    assert not _is_due(rule, ran, _at(16, "23:59"))
    assert _is_due(rule, ran, _at(17, "00:01"))


def _runs(rule: dict, start: datetime, minutes: int) -> list[datetime]:
    """Tick *rule* once a minute from *start*, as the scheduler does; when it ran."""
    from services.rules.schedule import _is_due

    state: dict = {}
    ran = []
    # In UTC, so a minute is a real minute across a clock change.
    moment = start.astimezone(timezone.utc)
    for _ in range(minutes):
        if _is_due(rule, state, moment):
            ran.append(moment)
            state = {"last_run_at": _stamp(moment + timedelta(seconds=5))}
        moment += timedelta(minutes=1)
    return ran


def test_a_window_that_opens_in_the_spring_forward_gap_opens_once() -> None:
    """02:30 does not exist on 2027-03-14 in New York: the window opens at 03:00 EDT, once."""
    from zoneinfo import ZoneInfo

    new_york = ZoneInfo("America/New_York")
    rule = _nightly(window_start="02:30", window_end="05:00", timezone="America/New_York")
    start = datetime(2027, 3, 13, 23, 0, tzinfo=new_york)

    ran = _runs(rule, start, 8 * 60)

    assert [r.astimezone(new_york).strftime("%H:%M %Z") for r in ran] == ["03:00 EDT"]


def test_a_window_that_opens_in_the_repeated_hour_opens_once() -> None:
    """01:30 happens twice on 2026-11-01 in New York: the window opens at the first."""
    from zoneinfo import ZoneInfo

    new_york = ZoneInfo("America/New_York")
    rule = _nightly(window_start="01:30", window_end="03:00", timezone="America/New_York")
    start = datetime(2026, 10, 31, 23, 0, tzinfo=new_york)

    ran = _runs(rule, start, 6 * 60)

    assert [r.astimezone(new_york).strftime("%H:%M %Z") for r in ran] == ["01:30 EDT"]


def test_an_overnight_window_runs_once_a_night_across_both_changes() -> None:
    from zoneinfo import ZoneInfo

    new_york = ZoneInfo("America/New_York")
    rule = _nightly(timezone="America/New_York")   # 22:00-02:00
    for day in (datetime(2027, 3, 13, 12, 0, tzinfo=new_york),
                datetime(2026, 10, 31, 12, 0, tzinfo=new_york)):
        ran = _runs(rule, day, 40 * 60)
        assert [r.astimezone(new_york).strftime("%a %H:%M") for r in ran] == [
            day.strftime("%a") + " 22:00",
            (day + timedelta(days=1)).strftime("%a") + " 22:00",
        ]


def test_a_window_whose_start_equals_its_end_is_open_all_day() -> None:
    """r2 M1: 03:00-03:00 is the whole day, once per day from local midnight."""
    start = datetime(2026, 11, 16, 0, 0, 17, tzinfo=timezone.utc)   # Monday, ticks at :17

    every_day = _nightly(window_start="03:00", window_end="03:00")
    assert every_day["enabled"] and not every_day["invalid_window"]
    assert [r.strftime("%a %H:%M") for r in _runs(every_day, start, 72 * 60)] == [
        "Mon 00:00", "Tue 00:00", "Wed 00:00",
    ]

    mondays = _nightly(window_start="03:00", window_end="03:00", days=[0])
    assert [r.strftime("%a %H:%M") for r in _runs(mondays, start, 72 * 60)] == ["Mon 00:00"]


def test_a_conversion_rule_refuses_a_window_whose_start_equals_its_end() -> None:
    """r3 I1: for a conversion an equal pair is a slip, so it fails safe: paused."""
    from services.rules.rules import CONVERT, normalize_rule
    from services.rules.schedule import _in_window, _is_due

    rule = normalize_rule({
        "mode": "createcd", "enabled": True, "window_start": "03:00",
        "window_end": "03:00", "timezone": "UTC",
    }, kind=CONVERT)
    noon = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)

    assert rule["invalid_window"] is True
    assert rule["enabled"] is False
    assert not _is_due(rule, {}, noon)
    # Even a hand-edited blob that skips normalize_rule is not open all day.
    assert not _in_window({**rule, "enabled": True}, noon)


def test_each_kind_keeps_to_its_own_shape() -> None:
    """A conversion Rule needs a target; a check Rule never has one."""
    from services.rules.rules import CHECK, CONVERT, normalize_rule

    assert normalize_rule({"enabled": True}, kind=CONVERT) is None
    assert normalize_rule({"mode": "createcd", "enabled": True}, kind=CHECK)["mode"] == ""


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
    async def always(_tool, **_kwargs):
        return True

    monkeypatch.setattr(registry, "tool_is_ready", always)


async def _folder_library(tree: Path, check: dict, convert: dict | None = None) -> str:
    """A folder Library on *tree* with a PS2 check Rule (and a conversion Rule)."""
    from routes import libraries as routes

    created = await routes.register_library(routes.RegisterLibraryRequest(path=str(tree)))
    await routes.put_library_rules(created["id"], {
        "rules": {"ps2": convert} if convert else {},
        "check_rules": {"ps2": check},
    })
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

    monkeypatch.setitem(vars(tool), "verify", verify)
    monkeypatch.setitem(vars(tool), "verify_timeout", bound)
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
    # Raised by the check: only #306's allowlist (the check could not run)
    # is no verdict.
    OSError("the file could not be opened"),
    TimeoutError(),
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

        monkeypatch.setitem(vars(registry.get("chdman")), "verify", broken)
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
async def test_a_check_that_rejects_the_file_by_raising_is_a_verdict(
    tree: Path, ready, verifier, sent, monkeypatch,
) -> None:
    """Outside #306's allowlist, an exception is the tool rejecting the data."""
    async def rejects(path, *, cancel_event=None):
        raise ValueError("bad CHD header")

    monkeypatch.setitem(vars(registry.get("chdman")), "verify", rejects)
    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 3})
    _seed(lib, {"ps2/Aria.chd": 2, "ps2/Bolt.chd": 3, "ps2/Dune.chd": 1})

    await _run(lib)

    (finding,) = _not_intact()
    assert finding["image_key"] == "ps2/Cove.chd"
    assert "bad CHD header" in finding["reason"]


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
async def test_only_a_mode_that_says_so_stays_out_of_the_ledger(db: Path) -> None:
    """The check mode declares it; a metadata scan still writes its row."""
    from models import ConversionMode
    from services import ledger
    from services.job_manager import JobManager

    manager = JobManager()
    scan = manager.create_external_job("Scan", ConversionMode.METADATA_SCAN)
    await manager.finish_external_job(scan.id, success=True)
    check = manager.create_external_job("Check", ConversionMode.CHECK)
    await manager.finish_external_job(check.id, success=True)

    await ledger.record(scan)
    await ledger.record(check)

    assert [row["job_id"] for row in await ledger.recent(10)] == [scan.id]


@pytest.mark.asyncio
async def test_a_failed_check_run_is_no_job_you_started(db: Path, sent) -> None:
    """The scheduler starts a check run: its failure is no "A job you started failed"."""
    from models import ConversionMode
    from services import notifications
    from services.job_manager import JobManager

    manager = JobManager()
    scan = manager.create_external_job("Scan", ConversionMode.METADATA_SCAN)
    await manager.finish_external_job(scan.id, success=False, error_message="boom")
    check = manager.create_external_job("Check", ConversionMode.CHECK)
    await manager.finish_external_job(check.id, success=False, error_message="boom")

    await notifications.on_job_finished(scan)
    await notifications.on_job_finished(check)

    assert [e.data["job_id"] for e in sent if e.kind == "job_failed"] == [scan.id]


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
    await romm_routes.put_romm_rules({
        "rules": {}, "check_rules": {"7": {"enabled": True, "check_budget_gb": 6}},
    })

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


@pytest.mark.asyncio
async def test_a_rom_with_no_size_still_costs_its_file_size(db: Path, ready) -> None:
    """RomM may leave out fs_size_bytes; the budget then reads the file."""
    from routes import romm as romm_routes
    from services.romm import settings as romm_settings

    for name in ("Aria", "Bolt"):
        _sparse(db / "library" / "roms" / "ps2" / f"{name}.chd", 3 * GB)
    await romm_settings.save({"url": "http://romm:8080", "library_root": str(db / "library")})
    roms = [
        {
            "id": rom_id, "name": name, "full_path": f"roms/ps2/{name}.chd",
            "fs_name": f"{name}.chd", "platform_slug": "ps2",
        }
        for rom_id, name in ((1, "Aria"), (2, "Bolt"))
    ]
    await romm_routes.put_romm_rules({
        "rules": {}, "check_rules": {"7": {"enabled": True, "check_budget_gb": 3}},
    })

    with patch.object(romm_routes.romm_client, "roms", return_value=roms):
        result = await romm_routes.preview_auto_convert(
            romm_routes.SweepRequest(platform_ids=[7]),
        )

    (summary,) = result["platforms"]
    assert summary["candidates"] == ["Aria.chd"]
    assert summary["check_bytes"] == 3 * GB


# ----------------------------------------------------------------------
# #306's verdict rules
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_verifier_killed_by_a_signal_leaves_no_finding_and_no_check_time(
    tree: Path, ready, sent, monkeypatch,
) -> None:
    """The real chdman Verify path, its binary replaced by one that is killed."""
    from services import image_checks

    killed = tree.parent / "bin" / "chdman"
    killed.parent.mkdir()
    killed.write_text("#!/bin/sh\nkill -KILL $$\n")
    killed.chmod(0o755)
    monkeypatch.setattr(registry.get("chdman")._service, "chdman_path", str(killed))
    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 3})
    _seed(lib, {"ps2/Aria.chd": 2, "ps2/Bolt.chd": 3, "ps2/Dune.chd": 1})

    await _run(lib)

    assert "ps2/Cove.chd" not in image_checks.last_checked(lib)
    assert _not_intact() == []
    assert [e for e in sent if e.kind == "check_failed"] == []


@pytest.mark.asyncio
async def test_an_archive_too_damaged_to_read_is_checked_and_not_intact(
    db: Path, ready, sent,
) -> None:
    """romz claims a ``.zip`` it cannot open (``claims_check``), so it fails the check.

    ``verifies_path`` alone would drop it as uncheckable, and the damage would
    never show up.
    """
    roms = db / "roms"
    broken = roms / "gba" / "Broken.zip"
    broken.parent.mkdir(parents=True)
    broken.write_bytes(b"PK\x03\x04" + b"\x00" * 60)
    from routes import libraries as routes

    created = await routes.register_library(routes.RegisterLibraryRequest(path=str(roms)))
    await routes.put_library_rules(
        created["id"], {"check_rules": {"gba": {"enabled": True, "check_budget_gb": 1}}},
    )

    await _run(created["id"])

    (finding,) = _not_intact()
    assert finding["image_key"] == "gba/Broken.zip"


# ----------------------------------------------------------------------
# a conversion Rule and a check Rule on one Platform
# ----------------------------------------------------------------------


@pytest.fixture(name="queue")
def _queue(monkeypatch):
    """The conversion queue stubbed: records what a run queued."""
    from types import SimpleNamespace

    from services.job_manager import job_manager

    queued: list[str] = []

    async def fake_batch(paths, mode, **_kwargs):
        jobs = []
        for path in paths:
            queued.append(Path(path).name)
            jobs.append(SimpleNamespace(file_path=path, id=f"job-{len(queued)}"))
        return jobs

    monkeypatch.setattr(job_manager, "create_batch_jobs", fake_batch)
    monkeypatch.setattr(job_manager, "get_active_job_candidates", list)
    return queued


_CONVERT = {"mode": "createdvd", "enabled": True}
_CHECK = {"enabled": True, "check_budget_gb": 3}


@pytest.mark.asyncio
async def test_a_platform_has_a_conversion_rule_and_a_check_rule(
    tree: Path, ready, verifier, sent, queue,
) -> None:
    """Convert PS2 discs to CHD and check the PS2 CHDs nightly, together."""
    from routes import libraries as routes

    from .test_platform_detect import _ps2

    _ps2(tree / "ps2" / "Able.iso")
    lib = await _folder_library(tree, _CHECK, _CONVERT)

    stored = await routes.get_library_rules(lib)
    assert stored["rules"]["ps2"]["mode"] == "createdvd"
    assert stored["check_rules"]["ps2"]["check_budget_gb"] == 3

    _verdicts, calls = verifier
    result = await _run(lib)

    # The conversion Rule queued the ISO; the check Rule checked a CHD.
    assert queue == ["Able.iso"]
    assert calls == ["Aria.chd"]
    assert result["queued"] == 1
    assert result["checks"] == 1
    kinds = sorted((p["platform_id"], bool(p.get("check"))) for p in result["platforms"])
    assert kinds == [("ps2", False), ("ps2", True)]


@pytest.mark.asyncio
async def test_each_rule_runs_on_its_own_schedule(
    tree: Path, ready, verifier, sent, queue,
) -> None:
    """A scheduled sweep runs only the kind that is due, and stamps only its clock."""
    from routes import libraries as routes
    from services import libraries
    from services.rules import check as checks
    from services.rules import store
    from services.rules import sweep as rules_sweep

    from .test_platform_detect import _ps2

    _ps2(tree / "ps2" / "Able.iso")
    lib = await _folder_library(tree, _CHECK, _CONVERT)
    library = libraries.get(lib)
    # The conversion Rule ran a moment ago, so it is not due; the check never ran.
    stamp = _ago(0)
    await store.write(store.STATE_KEY, library, {"ps2": {"last_run_at": stamp}})

    _verdicts, calls = verifier
    result = await rules_sweep.sweep(library)
    await checks.drain()

    assert queue == []
    assert calls == ["Aria.chd"]
    assert [bool(p.get("check")) for p in result["platforms"]] == [True]
    state = await routes.get_library_rules(lib)
    # The conversion Rule's clock did not move.
    assert state["state"]["ps2"] == {"last_run_at": stamp}
    assert state["check_state"]["ps2"]["last_checks"] == 1

    # Now the check Rule is the one that is not due: the conversion Rule runs.
    await store.write(store.STATE_KEY, library, {})
    result = await rules_sweep.sweep(library)
    await checks.drain()

    assert queue == ["Able.iso"]
    assert calls == ["Aria.chd"]
    assert [bool(p.get("check")) for p in result["platforms"]] == [False]


@pytest.mark.asyncio
async def test_saving_one_kind_keeps_the_other(tree: Path, ready) -> None:
    from routes import libraries as routes

    lib = await _folder_library(tree, _CHECK, _CONVERT)

    # An editor that sends only its conversion Rules leaves the check Rules be.
    await routes.put_library_rules(lib, {"rules": {"ps2": {**_CONVERT, "enabled": False}}})

    stored = await routes.get_library_rules(lib)
    assert stored["rules"]["ps2"]["enabled"] is False
    assert stored["check_rules"]["ps2"]["check_budget_gb"] == 3


# ----------------------------------------------------------------------
# review round 1: the Image moves under a running check (C1)
# ----------------------------------------------------------------------


def _fake_chdman(tree: Path) -> Path:
    """chdman verify that passes an existing file and fails a missing one, as chdman does.

    A pass prints chdman's own success line: #300's verify only counts an exit
    0 that said ``Raw SHA1 verification successful!``.
    """
    fake = tree.parent / "bin" / "chdman"
    fake.parent.mkdir(exist_ok=True)
    fake.write_text('#!/bin/sh\nif [ -f "$3" ]; then\n'
                    '  echo "Raw SHA1 verification successful!"; exit 0\nfi\n'
                    'echo "Error opening CHD file: No such file"; exit 1\n')
    fake.chmod(0o755)
    return fake


@pytest.mark.asyncio
async def test_an_image_gone_before_its_turn_gets_no_finding(
    tree: Path, ready, sent, monkeypatch,
) -> None:
    """Reviewer probe: a convert Rule's delete-source (or the user) removes a picked Image."""
    from services import image_checks

    tool = registry.get("chdman")
    monkeypatch.setattr(tool._service, "chdman_path", str(_fake_chdman(tree)))
    real = tool.verify

    async def verify(path, *, cancel_event=None):
        if Path(path).name == "Cove.chd":
            (tree / "ps2" / "Bolt.chd").unlink()   # gone before Bolt's turn
        return await real(path, cancel_event=cancel_event)

    monkeypatch.setitem(vars(tool), "verify", verify)
    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 6})
    _seed(lib, {"ps2/Aria.chd": 2, "ps2/Bolt.chd": 10, "ps2/Dune.chd": 1})
    before = image_checks.last_checked(lib)

    await _run(lib)

    assert _not_intact() == []
    assert [e for e in sent if e.kind == "check_failed"] == []
    # No check time either: it was never checked.
    assert image_checks.last_checked(lib)["ps2/Bolt.chd"] == before["ps2/Bolt.chd"]


@pytest.mark.asyncio
async def test_an_image_changed_before_its_turn_is_skipped(
    tree: Path, ready, verifier, sent, monkeypatch,
) -> None:
    from services import image_checks

    _verdicts, calls = verifier

    async def replace_bolt(path, *, cancel_event=None):
        name = Path(path).name
        calls.append(name)
        if name == "Cove.chd":
            _sparse(tree / "ps2" / "Bolt.chd", 2 * GB)   # a different file now
        return {"valid": True, "message": "Intact"}

    monkeypatch.setitem(vars(registry.get("chdman")), "verify", replace_bolt)
    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 6})
    _seed(lib, {"ps2/Aria.chd": 2, "ps2/Bolt.chd": 10, "ps2/Dune.chd": 1})
    before = image_checks.last_checked(lib)
    await _run(lib)

    assert calls == ["Cove.chd"]
    assert image_checks.last_checked(lib)["ps2/Bolt.chd"] == before["ps2/Bolt.chd"]


@pytest.mark.asyncio
async def test_a_check_holds_the_images_input_lock(
    tree: Path, ready, verifier, sent, monkeypatch,
) -> None:
    """While it is checked, a delete-source job sees the Image as in use (#306's lock)."""
    from services.lock_manager import lock_manager

    held: dict[str, bool] = {}

    async def verify(path, *, cancel_event=None):
        held[Path(path).name] = lock_manager.check_file_status(path)[1]
        return {"valid": True, "message": "Intact"}

    monkeypatch.setitem(vars(registry.get("chdman")), "verify", verify)
    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 3})
    _seed(lib, {"ps2/Aria.chd": 2, "ps2/Bolt.chd": 3, "ps2/Dune.chd": 1})
    await _run(lib)

    assert held == {"Cove.chd": True}
    assert lock_manager.check_file_status(str(tree / "ps2" / "Cove.chd"))[1] is False


@pytest.mark.asyncio
async def test_a_not_intact_finding_for_a_vanished_file_is_cleared_by_the_next_sweep(
    tree: Path, ready, verifier, sent,
) -> None:
    verdicts, _calls = verifier
    verdicts["Cove.chd"] = {"valid": False, "message": "bad hunk"}
    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 3})
    _seed(lib, {"ps2/Aria.chd": 2, "ps2/Bolt.chd": 3, "ps2/Dune.chd": 1})
    await _run(lib)
    assert [f["image_key"] for f in _not_intact()] == ["ps2/Cove.chd"]

    (tree / "ps2" / "Cove.chd").unlink()
    await _run(lib)

    assert _not_intact() == []


@pytest.mark.asyncio
async def test_a_not_intact_finding_for_a_vanished_file_is_cleared_by_the_next_walk(
    tree: Path, ready, verifier, sent,
) -> None:
    from services import libraries

    verdicts, _calls = verifier
    verdicts["Cove.chd"] = {"valid": False, "message": "bad hunk"}
    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 3})
    _seed(lib, {"ps2/Aria.chd": 2, "ps2/Bolt.chd": 3, "ps2/Dune.chd": 1})
    await _run(lib)
    assert [f["image_key"] for f in _not_intact()] == ["ps2/Cove.chd"]

    (tree / "ps2" / "Cove.chd").unlink()
    libraries.get(lib).summary(refresh=True)

    assert _not_intact() == []


# ----------------------------------------------------------------------
# review round 1: rotation, Run now, RomM identity (I1, I2, I3)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_always_inconclusive_image_does_not_starve_its_platform(
    tree: Path, ready, sent, monkeypatch,
) -> None:
    """Reviewer probe: an inconclusive Image goes behind the others until they had a turn."""
    from services import image_checks

    calls: list[str] = []
    tool = registry.get("chdman")

    async def verify(path, *, cancel_event=None):
        calls.append(Path(path).name)
        if Path(path).name == "Aria.chd":
            return {"valid": False, "inconclusive": True, "message": "timed out"}
        return {"valid": True, "message": "ok"}

    async def bound(path, *, cancel_event=None):
        return 0

    monkeypatch.setitem(vars(tool), "verify", verify)
    monkeypatch.setitem(vars(tool), "verify_timeout", bound)
    _sparse(tree / "ps2" / "Aria.chd", 9 * GB)   # bigger than the budget
    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 6})
    for _night in range(3):
        await _run(lib)

    assert calls == ["Aria.chd", "Bolt.chd", "Cove.chd", "Dune.chd"][: len(calls)]
    assert calls[:3] != ["Aria.chd"] * 3
    # An attempt is no check: Aria still has no check time and no Finding.
    assert "ps2/Aria.chd" not in image_checks.last_checked(lib)
    assert _not_intact() == []


@pytest.mark.asyncio
async def test_run_now_leaves_the_nightly_schedule_alone(
    tree: Path, ready, verifier, sent,
) -> None:
    from routes import libraries as routes
    from services import libraries
    from services.rules import check as checks
    from services.rules import sweep as rules_sweep

    _verdicts, calls = verifier
    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 3})

    await _run(lib)   # Run now
    assert calls == ["Aria.chd"]
    state = (await routes.get_library_rules(lib))["check_state"]["ps2"]
    assert "last_run_at" not in state

    # The scheduled run is still due: Run now was an extra budget.
    result = await rules_sweep.sweep(libraries.get(lib))
    await checks.drain()
    assert result["checks"] == 1
    assert calls == ["Aria.chd", "Bolt.chd"]


@pytest.mark.asyncio
async def test_a_new_romm_instance_drops_romms_check_times_and_findings(
    db: Path, ready, verifier, sent,
) -> None:
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
    verdicts, _calls = verifier
    verdicts["Bolt.chd"] = {"valid": False, "message": "bad hunk"}
    await romm_routes.put_romm_rules({
        "rules": {}, "check_rules": {"7": {"enabled": True, "check_budget_gb": 6}},
    })
    with patch.object(romm_routes.romm_client, "roms", return_value=roms):
        await romm_routes.run_auto_convert(romm_routes.SweepRequest(platform_ids=[7]))
        await check.drain()
    assert image_checks.last_checked("romm")
    assert _not_intact()

    await romm_routes.put_romm_settings(
        romm_routes.RommSettingsPatch(url="http://other-romm:8080"),
    )

    # The new instance reuses ROM ids: the old checks say nothing about it.
    assert image_checks.last_checked("romm") == {}
    assert _not_intact() == []


@pytest.mark.asyncio
async def test_two_platforms_failing_in_one_sweep_send_one_notification(
    db: Path, ready, sent, monkeypatch,
) -> None:
    """Reviewer probe: one run, one message, whatever the number of check Rules."""
    from routes import libraries as routes

    tool = registry.get("chdman")

    async def verify(path, *, cancel_event=None):
        return {"valid": False, "message": "bad"}

    async def bound(path, *, cancel_event=None):
        return 0

    monkeypatch.setitem(vars(tool), "verify", verify)
    monkeypatch.setitem(vars(tool), "verify_timeout", bound)
    roms = db / "roms"
    _sparse(roms / "ps2" / "A.chd", GB)
    _sparse(roms / "psx" / "B.chd", GB)
    created = await routes.register_library(routes.RegisterLibraryRequest(path=str(roms)))
    await routes.put_library_rules(created["id"], {"rules": {}, "check_rules": {
        "ps2": {"enabled": True, "check_budget_gb": 6},
        "psx": {"enabled": True, "check_budget_gb": 6},
    }})
    await _run(created["id"])
    assert len([e for e in sent if e.kind == "check_failed"]) == 1


# ----------------------------------------------------------------------
# review round 2
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_image_waiting_for_a_verify_slot_is_not_locked(
    tree: Path, ready, verifier, sent, monkeypatch,
) -> None:
    """I1: the lock is taken when the verify starts, never while the check queues."""
    import asyncio

    from routes import libraries as routes
    from services.lock_manager import lock_manager
    from services.rules import check
    from services.workload_limiter import WorkloadLimiter

    # A lane of its own: an asyncio semaphore binds to the first event loop a
    # waiter queues on, and each test has its own loop.
    workload_limiter = WorkloadLimiter(verify_limit=1, metadata_scan_limit=1)
    monkeypatch.setattr(check, "workload_limiter", workload_limiter)

    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 3})
    _seed(lib, {"ps2/Aria.chd": 2, "ps2/Bolt.chd": 3, "ps2/Dune.chd": 1})
    cove = str(tree / "ps2" / "Cove.chd")
    busy = [await workload_limiter.acquire("verify") for _ in range(workload_limiter.limit("verify"))]
    try:
        await routes.run_library(lib)
        await asyncio.sleep(0.3)   # the check is queued for a verify slot
        waiting_locked = lock_manager.check_file_status(cove)[1]
    finally:
        for token in busy:
            token.release()
    await check.drain()

    assert waiting_locked is False
    _verdicts, calls = verifier
    assert calls == ["Cove.chd"]
    assert lock_manager.check_file_status(cove)[1] is False


def _observe_not_intact(library_id: str, key: str, path: Path) -> None:
    from services import findings

    findings.observe(library_id, findings.Observed(
        kind=findings.NOT_INTACT, image_key=key, path=str(path), reason="bad",
        cause={"tool": "chdman", "files": "x"},
    ))


def test_a_not_intact_finding_under_an_empty_mount_point_is_kept(db: Path) -> None:
    """M2: an unmounted share is an empty folder; that is no evidence the Image is gone."""
    from services import findings

    (db / "mnt").mkdir()   # an unmounted mount point: empty
    _observe_not_intact("lib", "A.chd", db / "mnt" / "A.chd")
    _observe_not_intact("lib", "share/ps2/B.chd", db / "share" / "ps2" / "B.chd")

    assert findings.clear_gone("lib", findings.NOT_INTACT) == 0
    assert sorted(f["image_key"] for f in _not_intact()) == ["A.chd", "share/ps2/B.chd"]


def test_a_not_intact_finding_beside_other_files_is_cleared(db: Path) -> None:
    """M2: the folder lists other files, so the share is there and the Image is gone."""
    from services import findings

    _sparse(db / "roms" / "ps2" / "Other.chd", 1)
    _observe_not_intact("lib", "ps2/A.chd", db / "roms" / "ps2" / "A.chd")

    assert findings.clear_gone("lib", findings.NOT_INTACT) == 1
    assert _not_intact() == []


@pytest.mark.asyncio
async def test_a_lock_that_fails_part_way_lets_go_of_the_others(
    tree: Path, ready, verifier, sent, monkeypatch,
) -> None:
    """M3: _hold releases what it took when an acquisition raises, and the
    Image gets no verdict (inconclusive) instead of failing the run."""
    from services import image_checks
    from services.job_manager import job_manager
    from services.lock_manager import lock_manager
    from services.rules import check as checks

    extra = tree / "ps2" / "Cove.extra"
    extra.write_bytes(b"x")
    monkeypatch.setattr(checks, "image_members", lambda path: [str(extra)])
    real = lock_manager.acquire_input_lock

    def acquire(path):
        if path == str(extra):
            raise PermissionError("lock dir not writable")
        return real(path)

    monkeypatch.setitem(vars(lock_manager), "acquire_input_lock", acquire)
    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 3})
    _seed(lib, {"ps2/Aria.chd": 2, "ps2/Bolt.chd": 3, "ps2/Dune.chd": 1})
    before = {j.id for j in job_manager.get_all_jobs()}

    await _run(lib)

    assert lock_manager.check_file_status(str(tree / "ps2" / "Cove.chd"))[1] is False
    # No verdict: inconclusive, so only an attempt time; the run carries on and
    # ends cleanly rather than failing on the lock error.
    _verdicts, calls = verifier
    assert calls == []
    assert "ps2/Cove.chd" not in image_checks.last_checked(lib)
    assert "ps2/Cove.chd" in image_checks.order_stamps(lib)
    assert _not_intact() == []
    (job,) = [j for j in job_manager.get_all_jobs() if j.id not in before]
    assert job.status.value == "completed"
    assert "1 could not be checked" in job.message


@pytest.mark.asyncio
async def test_a_check_cancelled_while_it_takes_its_lock_lets_go_of_it(
    tree: Path, ready, verifier, sent, monkeypatch,
) -> None:
    """r3 minor (reviewer probe): a task cancel during _hold leaves nothing locked."""
    import asyncio
    import threading

    from services.lock_manager import lock_manager
    from services.rules import check as checks

    gate = threading.Event()
    real = lock_manager.acquire_input_lock

    def slow(path):
        gate.wait(1)
        return real(path)

    monkeypatch.setitem(vars(lock_manager), "acquire_input_lock", slow)
    cove = tree / "ps2" / "Cove.chd"
    item = checks.Check(
        key="ps2/Cove.chd", name="Cove.chd", path=str(cove), size=GB,
        tool_id="chdman", fingerprint="",
    )
    task = asyncio.create_task(checks._check_one(item, None))
    await asyncio.sleep(0.2)
    task.cancel()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.3)

    assert lock_manager.check_file_status(str(cove))[1] is False


@pytest.mark.asyncio
async def test_a_lock_that_does_not_come_in_time_skips_the_image(
    tree: Path, ready, verifier, sent, monkeypatch,
) -> None:
    """r3 minor: _hold is bounded; a stuck acquisition skips the Image and leaks nothing."""
    import threading

    from services.lock_manager import lock_manager
    from services.rules import check as checks

    gate = threading.Event()
    real = lock_manager.acquire_input_lock

    def stuck(path):
        gate.wait(2)
        return real(path)

    monkeypatch.setitem(vars(lock_manager), "acquire_input_lock", stuck)
    monkeypatch.setattr(checks, "_RECHECK_SECONDS", 0.2)
    cove = tree / "ps2" / "Cove.chd"
    item = checks.Check(
        key="ps2/Cove.chd", name="Cove.chd", path=str(cove), size=GB,
        tool_id="chdman", fingerprint="",
    )

    import asyncio
    import time

    started = time.monotonic()
    verdict = await checks._check_one(item, None)
    waited = time.monotonic() - started
    gate.set()
    await asyncio.sleep(0.3)

    assert waited < 1.0   # gave up at the bound, not when the lock came
    assert verdict[0] == checks.SKIPPED
    _verdicts, calls = verifier
    assert calls == []
    assert lock_manager.check_file_status(str(cove))[1] is False


@pytest.mark.asyncio
async def test_an_image_checked_long_ago_and_now_inconclusive_rotates(
    tree: Path, ready, sent, monkeypatch,
) -> None:
    """M4: the order is the later of the check and attempt times, oldest first."""
    calls: list[str] = []
    tool = registry.get("chdman")

    async def verify(path, *, cancel_event=None):
        calls.append(Path(path).name)
        if Path(path).name == "Aria.chd":
            return {"valid": False, "inconclusive": True, "message": "timed out"}
        return {"valid": True, "message": "ok"}

    async def bound(path, *, cancel_event=None):
        return 0

    monkeypatch.setitem(vars(tool), "verify", verify)
    monkeypatch.setitem(vars(tool), "verify_timeout", bound)
    _sparse(tree / "ps2" / "Aria.chd", 9 * GB)
    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 6})
    _seed(lib, {"ps2/Aria.chd": 60, "ps2/Bolt.chd": 3, "ps2/Cove.chd": 2, "ps2/Dune.chd": 1})
    for _night in range(3):
        await _run(lib)

    assert calls == ["Aria.chd", "Bolt.chd", "Cove.chd", "Dune.chd"]


@pytest.mark.asyncio
async def test_a_run_stopped_by_a_romm_change_reports_nothing_it_did_not_write(
    db: Path, ready, sent, monkeypatch,
) -> None:
    """M5: the identity change lands mid-check; the run ends cancelled, counting nothing."""
    from routes import romm as romm_routes
    from services import image_checks
    from services.job_manager import job_manager
    from services.romm import settings as romm_settings
    from services.romm.library import romm_library
    from services.rules import check
    from services.rules import sweep as rules_sweep

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
    calls: list[str] = []
    tool = registry.get("chdman")

    async def verify(path, *, cancel_event=None):
        calls.append(Path(path).name)
        rules_sweep.forget_checks_locked(romm_library)   # the change lands mid-check
        return {"valid": False, "message": "bad"}

    async def bound(path, *, cancel_event=None):
        return 0

    monkeypatch.setitem(vars(tool), "verify", verify)
    monkeypatch.setitem(vars(tool), "verify_timeout", bound)
    await romm_routes.put_romm_rules({
        "rules": {}, "check_rules": {"7": {"enabled": True, "check_budget_gb": 6}},
    })
    before = {j.id for j in job_manager.get_all_jobs()}
    with patch.object(romm_routes.romm_client, "roms", return_value=roms):
        await romm_routes.run_auto_convert(romm_routes.SweepRequest(platform_ids=[7]))
        await check.drain()

    assert calls == ["Aria.chd"]
    assert image_checks.last_checked("romm") == {}
    assert _not_intact() == []
    assert [e for e in sent if e.kind == "check_failed"] == []
    (job,) = [j for j in job_manager.get_all_jobs() if j.id not in before]
    assert job.status.value == "cancelled"
    assert "not Intact" not in job.message.replace("0 not Intact", "")
    assert job.message == "Stopped: 0 Intact, 0 not Intact"


@pytest.mark.asyncio
async def test_a_conversion_that_finishes_during_a_check_keeps_its_source(
    db: Path, ready, sent, monkeypatch,
) -> None:
    """While a check reads an Image, a delete-source conversion of it keeps the Source.

    The real job pipeline (dolphin-tool's runner stubbed, as the other job
    tests do): the conversion finishes while the check's Verify holds #306's
    input lock, waits its short bound, completes with "Source kept", and the
    check still records its result.
    """
    import asyncio
    from collections import OrderedDict

    from models import ConversionMode, JobStatus
    from routes import libraries as routes
    from services import image_checks
    from services import job_manager as job_manager_module
    from services.concurrency_manager import concurrency_manager
    from services.job_manager import SOURCE_KEPT_FOR_READERS, job_manager
    from services.rules import check
    from utils.delete_plan import build_delete_snapshot

    from .test_platform_detect import _gamecube

    monkeypatch.setattr(job_manager, "jobs", OrderedDict())
    monkeypatch.setattr(job_manager, "_queue", asyncio.PriorityQueue())
    monkeypatch.setattr(job_manager_module, "DELETE_READER_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(job_manager_module, "DELETE_READER_POLL_SECONDS", 0.05)
    roms = db / "roms"
    iso = Path(_gamecube(roms / "gc" / "Game.iso"))
    rvz = roms / "gc" / "Game.rvz"
    tool = registry.get("dolphin")
    runner = tool._service._runner

    async def fake_run(cmd, *, input_path, output_path, **kwargs):
        Path(output_path).write_bytes(b"converted output")
        yield {"progress": 100, "message": "Conversion complete"}

    async def fake_verify(cmd, *, path, **kwargs):
        yield {"type": "complete", "valid": True, "message": "Disc image is Intact"}

    monkeypatch.setitem(vars(runner), "run", fake_run)
    monkeypatch.setitem(vars(runner), "run_verify", fake_verify)
    real_verify = tool.verify
    conversion: dict = {}

    async def verify(path, *, cancel_event=None):
        if Path(path) == iso:
            # The check is reading Game.iso: the conversion finishes now.
            job = await job_manager.create_job(
                str(iso), ConversionMode.DOLPHIN_RVZ, output_path=str(rvz),
                delete_on_verify=True, container_only=True,
                delete_snapshot=build_delete_snapshot(str(iso)),
            )
            try:
                await job_manager._process_job(job.id)
            finally:
                concurrency_manager.release(job.id)
            conversion["job"] = job
        return await real_verify(path, cancel_event=cancel_event)

    monkeypatch.setitem(vars(tool), "verify", verify)
    created = await routes.register_library(routes.RegisterLibraryRequest(path=str(roms)))
    await routes.put_library_rules(
        created["id"], {"check_rules": {"gc": {"enabled": True, "check_budget_gb": 1}}},
    )

    await routes.run_library(created["id"])
    await check.drain()

    job = conversion["job"]
    assert job.status == JobStatus.COMPLETED, job.error_message
    assert job.warning == SOURCE_KEPT_FOR_READERS
    assert "Source kept: it was being read by another job" in job.message
    assert iso.exists()
    assert rvz.exists()
    # The check still records its result.
    assert "gc/Game.iso" in image_checks.last_checked(created["id"])


# ----------------------------------------------------------------------
# PR review (#325, Codex)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_passing_check_keeps_what_made_the_file(
    tree: Path, ready, verifier, sent,
) -> None:
    """4577114954: a check refreshes the Verify record and keeps its provenance."""
    from services.verification_store import verification_store

    from .test_platform_detect import _ps2

    cove = str(tree / "ps2" / "Cove.chd")
    await verification_store.mark_verified(
        cove, source_path=str(tree / "ps2" / "Cove.cue"),
        produced_meta={"mode": "createcd"},
    )
    # A PS3 ISO makeps3iso built: the Library has no PS3 check Rule, so it is
    # not checked and its record (and the makeps3iso mode in it) is left as it is.
    ps3 = Path(_ps2(tree / "ps3" / "Game.iso"))
    await verification_store.mark_verified(
        str(ps3), source_path=str(tree / "ps3" / "Game"),
        produced_meta={"mode": "folder_to_iso"},
    )
    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 3})
    _seed(lib, {"ps2/Aria.chd": 2, "ps2/Bolt.chd": 3, "ps2/Dune.chd": 1})

    await _run(lib)

    record = await verification_store.get_record(cove)
    assert record["produced_meta"] == {"mode": "createcd"}
    assert record["source_path"] == str(tree / "ps2" / "Cove.cue")
    ps3_record = await verification_store.get_record(str(ps3))
    assert ps3_record["produced_meta"] == {"mode": "folder_to_iso"}


@pytest.mark.asyncio
async def test_a_check_asks_whether_the_verifier_is_ready(db: Path, monkeypatch) -> None:
    """4577114957: verify readiness decides, not conversion readiness.

    JWUDTool (conversion) missing still leaves WUX Images checkable by its pure
    Python verifier; zstd missing makes z3ds Images not ready to check.
    """
    from routes import libraries as routes

    asked: list[tuple[str, str]] = []

    async def readiness(tool, *, purpose="convert"):
        asked.append((tool.id, purpose))
        if purpose == "verify":
            return tool.id != "z3ds"
        return tool.id != "jwud"

    monkeypatch.setattr(registry, "tool_is_ready", readiness)
    roms = db / "roms"
    _sparse(roms / "wiiu" / "Game.wux", 1024)
    _sparse(roms / "3ds" / "Game.z3ds", 1024)
    created = await routes.register_library(routes.RegisterLibraryRequest(path=str(roms)))
    await routes.put_library_rules(created["id"], {"check_rules": {
        "wiiu": {"enabled": True, "check_budget_gb": 1},
        "3ds": {"enabled": True, "check_budget_gb": 1},
    }})

    result = await routes.preview_library(created["id"])

    by_platform = {p["platform_id"]: p for p in result["platforms"]}
    assert by_platform["wiiu"]["candidates"] == ["Game.wux"]
    assert by_platform["3ds"]["candidates"] == []
    assert {"platform_id": "3ds", "error": "tool_not_ready"} in result["errors"]
    assert {purpose for _tool, purpose in asked} == {"verify"}


@pytest.mark.asyncio
async def test_an_archive_the_safety_rules_refuse_is_no_verdict(
    db: Path, ready, sent, monkeypatch,
) -> None:
    """4577114961: "uncheckable" is server policy, not damage: no Finding, the
    Intact badge stays, no notification; only an attempt time for ordering."""
    import zipfile

    from routes import libraries as routes
    from services import image_checks
    from services.verification_store import verification_store

    from config import settings

    roms = db / "roms"
    archive = roms / "gba" / "Big Racer.zip"
    archive.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("Big Racer.gba", b"\x01" * 4096)
    monkeypatch.setattr(settings, "archive_max_member_size", 1024)
    await verification_store.mark_verified(str(archive))
    created = await routes.register_library(routes.RegisterLibraryRequest(path=str(roms)))
    await routes.put_library_rules(
        created["id"], {"check_rules": {"gba": {"enabled": True, "check_budget_gb": 1}}},
    )

    await _run(created["id"])

    assert _not_intact() == []
    assert [e for e in sent if e.kind == "check_failed"] == []
    assert await verification_store.get_record(str(archive)) is not None
    assert "gba/Big Racer.zip" not in image_checks.last_checked(created["id"])
    assert "gba/Big Racer.zip" in image_checks.order_stamps(created["id"])


@pytest.mark.asyncio
async def test_a_writer_waits_until_the_verdict_is_recorded(
    tree: Path, ready, verifier, sent, monkeypatch,
) -> None:
    """4577134005: the locks are held until the verdict is written, so an overwrite
    conversion cannot replace the file the recorded verdict describes."""
    from services import image_checks
    from services.lock_manager import LockManager, lock_manager
    from services.verification_store import verification_store

    cove = str(tree / "ps2" / "Cove.chd")
    writer = LockManager()
    writer._lock_dir = lock_manager._lock_dir
    while_recording: list[bool] = []
    real_record = image_checks.record
    real_refresh = verification_store.refresh_check

    def try_to_write() -> None:
        taken = writer.acquire_lock(cove, allow_existing=True)
        while_recording.append(taken)
        if taken:
            writer.release_lock(cove)

    def record(*args, **kwargs):
        try_to_write()
        return real_record(*args, **kwargs)

    async def refresh(path, **kwargs):
        try_to_write()
        return await real_refresh(path, **kwargs)

    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 3})
    _seed(lib, {"ps2/Aria.chd": 2, "ps2/Bolt.chd": 3, "ps2/Dune.chd": 1})
    monkeypatch.setattr(image_checks, "record", record)
    monkeypatch.setitem(vars(verification_store), "refresh_check", refresh)

    await _run(lib)

    # The writer was refused while each part of the verdict was written...
    assert while_recording == [False, False]
    # ...and gets the file once the check is done.
    assert writer.acquire_lock(cove, allow_existing=True)
    writer.release_lock(cove)


@pytest.mark.asyncio
async def test_a_check_waiting_for_a_verify_slot_can_be_cancelled(
    tree: Path, ready, verifier, sent, monkeypatch,
) -> None:
    """4577171591: Cancel ends a run that is queued for a verify slot, at once."""
    import asyncio

    from routes import libraries as routes
    from services.job_manager import job_manager
    from services.rules import check
    from services.workload_limiter import WorkloadLimiter

    # A lane of its own: an asyncio semaphore binds to the first event loop a
    # waiter queues on, and each test has its own loop.
    workload_limiter = WorkloadLimiter(verify_limit=1, metadata_scan_limit=1)
    monkeypatch.setattr(check, "workload_limiter", workload_limiter)

    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 3})
    _seed(lib, {"ps2/Aria.chd": 2, "ps2/Bolt.chd": 3, "ps2/Dune.chd": 1})
    before = {j.id for j in job_manager.get_all_jobs()}
    busy = [await workload_limiter.acquire("verify") for _ in range(workload_limiter.limit("verify"))]
    try:
        await routes.run_library(lib)
        await asyncio.sleep(0.2)   # queued for a slot
        (job,) = [j for j in job_manager.get_all_jobs() if j.id not in before]
        await job_manager.cancel_job(job.id)
        await asyncio.wait_for(check.drain(), 2)   # ends without a slot coming free
    finally:
        for token in busy:
            token.release()

    assert job.status.value == "cancelled"
    _verdicts, calls = verifier
    assert calls == []
    assert not check.in_flight(lib, "ps2/Cove.chd")
    assert workload_limiter.in_use("verify") == 0


@pytest.mark.asyncio
async def test_a_scheduled_check_is_stamped_with_the_moment_it_was_due(
    tree: Path, ready, verifier, sent, monkeypatch,
) -> None:
    """4577171597: a pick that runs past midnight still counts for the day it began."""
    from routes import libraries as routes
    from services import libraries
    from services.rules import check as checks
    from services.rules import sweep as rules_sweep

    monday = datetime(2026, 11, 16, 23, 59, 30, tzinfo=timezone.utc)
    tuesday = monday + timedelta(minutes=2)
    clock = {"now": monday}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"].astimezone(tz) if tz else clock["now"]

    real_inspect = checks.inspect

    def slow_inspect(image):
        clock["now"] = tuesday   # picking crosses midnight
        return real_inspect(image)

    monkeypatch.setattr(rules_sweep, "datetime", Clock)
    monkeypatch.setattr(checks, "inspect", slow_inspect)
    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 3, "timezone": "UTC"})
    library = libraries.get(lib)

    await rules_sweep.sweep(library)
    await checks.drain()

    stored = await routes.get_library_rules(lib)
    state = stored["check_state"]["ps2"]
    assert state["last_run_at"].startswith("2026-11-16T23:59")
    # So Tuesday still gets its own run.
    from services.rules.schedule import _is_due

    rule = stored["check_rules"]["ps2"]
    assert _is_due(rule, state, tuesday + timedelta(minutes=3))


@pytest.mark.asyncio
async def test_a_check_run_cancelled_before_it_starts_ends_once(
    tree: Path, ready, verifier, sent, monkeypatch,
) -> None:
    """#306's 09dbda9: a check job cancelled before its first Image is announced
    terminal once, sends nothing, and records nothing."""
    from routes import libraries as routes
    from services import image_checks, notifications
    from services.job_manager import job_manager
    from services.rules import check

    heard: list[tuple[str, str]] = []
    monkeypatch.setattr(job_manager, "_terminal_listeners", [
        lambda job: heard.append((job.id, job.status.value)),
        notifications.on_job_finished,
    ])
    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 3})
    _seed(lib, {"ps2/Aria.chd": 2, "ps2/Bolt.chd": 3, "ps2/Dune.chd": 1})
    before = {j.id for j in job_manager.get_all_jobs()}

    await routes.run_library(lib)
    # The run is queued as a task but has not checked anything yet.
    (job,) = [j for j in job_manager.get_all_jobs() if j.id not in before]
    await job_manager.cancel_job(job.id)
    await check.drain()

    assert job.status.value == "cancelled"
    assert heard == [(job.id, "cancelled")]
    _verdicts, calls = verifier
    assert calls == []
    assert "ps2/Cove.chd" not in image_checks.order_stamps(lib)
    assert [e for e in sent if e.kind in ("check_failed", "job_failed")] == []


@pytest.mark.asyncio
async def test_a_check_rule_with_a_corrected_window_runs_again(tree: Path, ready) -> None:
    """4577383258: the server recomputes invalid_window from the window it is sent,
    so a check Rule paused for half a window resumes once the window is whole,
    even if a client still sends the old flag back."""
    from routes import libraries as routes

    created = await routes.register_library(routes.RegisterLibraryRequest(path=str(tree)))
    lib = created["id"]

    half = await routes.put_library_rules(lib, {"check_rules": {"ps2": {
        "enabled": True, "check_budget_gb": 3, "window_start": "01:00",
    }}})
    assert half["check_rules"]["ps2"]["invalid_window"] is True
    assert half["check_rules"]["ps2"]["enabled"] is False

    whole = await routes.put_library_rules(lib, {"check_rules": {"ps2": {
        **half["check_rules"]["ps2"], "window_end": "05:00", "enabled": True,
    }}})
    assert whole["check_rules"]["ps2"]["invalid_window"] is False
    assert whole["check_rules"]["ps2"]["enabled"] is True


def test_a_conversion_rule_with_an_equal_window_stays_refused_on_every_save() -> None:
    """The recompute keeps an equal pair invalid for a conversion Rule, flag sent or not."""
    from services.rules.rules import CONVERT, normalize_rule

    for sent_flag in (True, False):
        rule = normalize_rule({
            "mode": "createcd", "enabled": True, "window_start": "03:00",
            "window_end": "03:00", "invalid_window": sent_flag,
        }, kind=CONVERT)
        assert (rule["invalid_window"], rule["enabled"]) == (True, False)


@pytest.mark.asyncio
async def test_a_converted_images_mode_survives_a_nightly_check(
    tree: Path, ready, verifier, sent, monkeypatch,
) -> None:
    """#306 ff939d5: every passing conversion check records the mode and tool that
    made the file. A nightly check refreshes that record and keeps both, so the
    next manual check still goes to the tool that made it. Nothing is seeded by
    hand: a real createcd job with verify_after writes the record."""
    from models import ConversionJob, ConversionMode, JobStatus
    from services import image_checks
    from services.job_manager import job_manager
    from services.verification_store import verification_store

    _verdicts, calls = verifier
    cue = tree / "ps2" / "Echo.cue"
    cue.write_text('FILE "Echo.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n')
    (tree / "ps2" / "Echo.bin").write_bytes(b"\0" * 2352)
    chd = tree / "ps2" / "Echo.chd"

    async def fake_convert(*_args, **_kwargs):
        chd.write_bytes(b"\0" * 4096)
        yield {"progress": 100, "message": "done"}

    monkeypatch.setitem(vars(registry.for_mode("createcd")), "convert", fake_convert)
    job = ConversionJob(
        id="echoverifyafter", file_path=str(cue), filename=cue.name,
        mode=ConversionMode.CREATECD, status=JobStatus.QUEUED,
        created_at=datetime.now(timezone.utc), output_path=str(chd),
        verify_after=True,
    )
    job_manager.jobs[job.id] = job
    try:
        await job_manager._process_job(job.id)
    finally:
        job_manager.jobs.pop(job.id, None)
    assert job.status == JobStatus.COMPLETED, (job.error_message, job.message)
    made = await verification_store.get_record(str(chd))
    assert made["produced_meta"]["mode"] == "createcd"
    assert made["produced_meta"]["tool"] == "chdman"

    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 3})
    _seed(lib, {"ps2/Aria.chd": 1, "ps2/Bolt.chd": 1, "ps2/Cove.chd": 1, "ps2/Dune.chd": 1})
    calls.clear()

    await _run(lib)

    assert calls == ["Echo.chd"]
    assert "ps2/Echo.chd" in image_checks.last_checked(lib)
    after = await verification_store.get_record(str(chd))
    assert after["produced_meta"] == made["produced_meta"]
    assert after["source_path"] == made["source_path"]
    assert after["check_passed"] == "intact"


@pytest.mark.asyncio
async def test_a_split_ps3_iso_is_one_image_and_a_short_later_part_fails_it(
    db: Path, ready, sent,
) -> None:
    """#306 f360920: a split makeps3iso build (GAME.iso.0, .1, no GAME.iso) is one
    Image to a check Rule. Its stored check names folder_to_iso, so makeps3iso
    checks it (as the CLI routes it). The budget pays the parts' summed size,
    the record under GAME.iso is refreshed and keeps its mode, and the real
    makeps3iso verify, no stub, fails the set when a later part is cut short."""
    from routes import libraries as routes
    from services.verification_store import verification_store

    from .test_cli_306 import _real_split_iso

    roms = db / "roms"
    iso = _real_split_iso(roms / "ps3")
    meta = {"mode": "folder_to_iso", "tool": "makeps3iso"}
    await verification_store.mark_verified(str(iso), produced_meta=meta)
    parts = [roms / "ps3" / "GAME.iso.0", roms / "ps3" / "GAME.iso.1"]
    total = sum(p.stat().st_size for p in parts)
    created = await routes.register_library(routes.RegisterLibraryRequest(path=str(roms)))
    lib = created["id"]
    await routes.put_library_rules(lib, {"check_rules": {"ps3": {
        "enabled": True, "check_budget_gb": 1,
    }}})

    preview = await routes.preview_library(lib)
    (summary,) = preview["platforms"]
    assert summary["considered"] == 1  # one Image, not one per part
    assert summary["candidates"] == ["GAME.iso.0"]
    assert summary["checks"] == 1
    assert summary["check_bytes"] == total

    await _run(lib)
    assert _not_intact() == []
    kept = await verification_store.get_record(str(iso))
    assert kept["produced_meta"] == meta
    assert await verification_store.get_record(str(parts[0])) is None

    parts[1].write_bytes(parts[1].read_bytes()[: 2048 * 3])  # cut short
    await _run(lib)

    (finding,) = _not_intact()
    assert finding["image_key"] == "ps3/GAME.iso.0"
    assert "short" in finding["reason"], finding
    assert await verification_store.get_record(str(iso)) is None


@pytest.mark.asyncio
async def test_a_split_set_counts_as_one_image_for_a_conversion_rule(db: Path, ready) -> None:
    """Supervisor ruling 3: a split set is exactly one Image in a conversion
    Rule's ``considered`` and ``skipped_unconvertible``, the same as an unsplit
    GAME.iso. The ``.1`` part is never counted on its own."""
    from routes import libraries as routes

    from .test_cli_306 import _real_split_iso

    split_root, whole_root = db / "split", db / "whole"
    _real_split_iso(split_root / "ps3")
    (whole_root / "ps3").mkdir(parents=True)
    (whole_root / "ps3" / "GAME.iso").write_bytes(
        (split_root / "ps3" / "GAME.iso.0").read_bytes()
        + (split_root / "ps3" / "GAME.iso.1").read_bytes()
    )

    async def counts(root: Path) -> tuple:
        created = await routes.register_library(routes.RegisterLibraryRequest(path=str(root)))
        await routes.put_library_rules(created["id"], {"rules": {"ps3": {
            "mode": "folder_to_iso", "enabled": True,
        }}})
        result = await routes.preview_library(created["id"])
        (summary,) = result["platforms"]
        return (
            summary["considered"], result["considered"], result["skipped_unconvertible"],
            result["queued"],
        )

    split, whole = await counts(split_root), await counts(whole_root)

    assert split == whole
    assert split[:3] == (1, 1, 1)


@pytest.mark.asyncio
async def test_a_split_iso_being_rebuilt_is_skipped_as_busy(db: Path, ready, sent) -> None:
    """Codex 4577570988: a directory-input rebuild locks only the logical
    GAME.iso. The check holds that path too, so while another process has it
    the Image is skipped as busy: no Finding, no check time, record untouched,
    even though the parts on disk look damaged mid-build."""
    from routes import libraries as routes
    from services import image_checks
    from services.lock_manager import LockManager, lock_manager
    from services.verification_store import verification_store

    from .test_cli_306 import _real_split_iso

    roms = db / "roms"
    iso = _real_split_iso(roms / "ps3")
    meta = {"mode": "folder_to_iso", "tool": "makeps3iso"}
    await verification_store.mark_verified(str(iso), produced_meta=meta)
    before = await verification_store.get_record(str(iso))
    part1 = roms / "ps3" / "GAME.iso.1"
    part1.write_bytes(part1.read_bytes()[: 2048 * 3])  # half-written by the rebuild
    created = await routes.register_library(routes.RegisterLibraryRequest(path=str(roms)))
    lib = created["id"]
    await routes.put_library_rules(lib, {"check_rules": {"ps3": {
        "enabled": True, "check_budget_gb": 1,
    }}})
    builder = LockManager()
    builder._lock_dir = lock_manager._lock_dir
    assert builder.acquire_lock(str(iso), allow_existing=True)
    try:
        await _run(lib)
    finally:
        builder.release_lock(str(iso))

    assert _not_intact() == []
    assert "ps3/GAME.iso.0" not in image_checks.order_stamps(lib)
    assert await verification_store.get_record(str(iso)) == before
    assert [e for e in sent if e.kind == "check_failed"] == []


@pytest.mark.asyncio
async def test_an_image_with_no_library_size_is_charged_for_every_file(db: Path) -> None:
    """Codex 4577598584: with no size from the Library (a RomM ROM without
    fs_size_bytes) the budget pays for the whole Image, every split part, not
    only the first."""
    from services.rules import check
    from services.rules.library import Image
    from services.verification_store import verification_store

    from .test_cli_306 import _real_split_iso

    folder = db / "roms" / "ps3"
    iso = _real_split_iso(folder)
    await verification_store.mark_verified(
        str(iso), produced_meta={"mode": "folder_to_iso", "tool": "makeps3iso"},
    )
    parts = [folder / "GAME.iso.0", folder / "GAME.iso.1"]
    image = Image(
        key="7", name="GAME.iso.0", platform="ps3", path=str(parts[0]), size=0,
    )

    seen = check.inspect(image)

    assert seen["check"].size == sum(p.stat().st_size for p in parts)
    assert seen["check"].size > parts[0].stat().st_size


@pytest.mark.asyncio
async def test_a_failed_ps3_iso_is_checked_again_once_repaired(db: Path, ready, sent) -> None:
    """Codex 4577598580: a failure takes away the pass but keeps what made the
    file, so the next sweep still routes the ISO to makeps3iso, and a pass after
    the repair clears its Finding."""
    from routes import libraries as routes
    from services import image_checks
    from services.verification_store import verification_store

    from .test_cli_306 import _real_split_iso

    roms = db / "roms"
    iso = _real_split_iso(roms / "ps3")
    meta = {"mode": "folder_to_iso", "tool": "makeps3iso"}
    await verification_store.mark_verified(str(iso), produced_meta=meta)
    part1 = roms / "ps3" / "GAME.iso.1"
    good = part1.read_bytes()
    created = await routes.register_library(routes.RegisterLibraryRequest(path=str(roms)))
    lib = created["id"]
    await routes.put_library_rules(lib, {"check_rules": {"ps3": {
        "enabled": True, "check_budget_gb": 1,
    }}})

    part1.write_bytes(good[: 2048 * 3])  # damaged
    await _run(lib)
    (finding,) = _not_intact()
    assert finding["image_key"] == "ps3/GAME.iso.0"
    # No longer passing: no Intact badge, no re-run fast path...
    assert await verification_store.get_record(str(iso)) is None
    assert not await verification_store.is_verified(str(iso))
    # ...but the producing mode is kept.
    assert verification_store.produced_meta_blocking(str(iso)) == meta

    part1.write_bytes(good)  # repaired
    image_checks.record(lib, "ps3/GAME.iso.0", "not_intact", at=_ago(2))  # next night
    await _run(lib)

    assert _not_intact() == []
    record = await verification_store.get_record(str(iso))
    assert record["produced_meta"] == meta
    assert record["check_passed"] == "intact"


@pytest.mark.asyncio
async def test_a_stop_while_images_are_picked_starts_no_check(
    tree: Path, ready, verifier, sent, monkeypatch,
) -> None:
    """Integration (#307 x #305): a stop that arrives while a check Rule picks
    its Images starts nothing new. No check job is created, nothing is checked,
    and the Rule's run is not recorded, so the next sweep picks them again."""
    from models import ConversionMode
    from routes import libraries as routes
    from services import image_checks
    from services.job_manager import job_manager
    from services.rules import check as checks
    from services.rules import sweep
    from services.rules.rules import CHECK

    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 100})
    stop: list[bool] = []
    real_inspect = checks.inspect
    inspected: list[str] = []

    def inspect_then_stop(image):
        inspected.append(str(image.key))
        if len(inspected) == 4:
            stop.append(True)  # SIGTERM lands while the last Image is looked at
        return real_inspect(image)

    monkeypatch.setattr(checks, "inspect", inspect_then_stop)
    jobs_before = set(job_manager.jobs)

    result = await sweep.sweep(
        routes._library(lib),
        ignore_schedule=True, should_stop=lambda: bool(stop),
    )
    await checks.drain()

    assert result["stopped_reason"] == "stopped"
    new = [job_manager.jobs[j] for j in set(job_manager.jobs) - jobs_before]
    assert [j for j in new if j.mode == ConversionMode.CHECK] == []
    _verdicts, calls = verifier
    assert calls == []
    assert image_checks.order_stamps(lib) == {}
    state = await sweep.get_state(routes._library(lib), CHECK)
    assert not (state.get("ps2") or {}).get("last_run_at")
    assert not (state.get("ps2") or {}).get("last_manual_run_at")


@pytest.mark.asyncio
async def test_a_scheduled_check_run_holds_its_window_until_it_ends(
    tree: Path, ready, sent, monkeypatch,
) -> None:
    """Codex 4578034601: while a scheduled check run is still checking, the next
    tick picks nothing more for that Rule, so the budget applies once per
    window. A run that is cancelled leaves the Rule due, and the next tick
    picks again; one that finishes records the run."""
    from models import ConversionMode
    from routes import libraries as routes
    from services.job_manager import job_manager
    from services.rules import check as checks
    from services.rules import sweep
    from services.rules.rules import CHECK

    tool = registry.get("chdman")
    gate = asyncio.Event()
    calls: list[str] = []

    async def verify(path, *, cancel_event=None):
        calls.append(Path(path).name)
        waits = {asyncio.ensure_future(gate.wait())}
        if cancel_event is not None:
            waits.add(asyncio.ensure_future(cancel_event.wait()))
        _done, pending = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        if cancel_event is not None and cancel_event.is_set():
            return {"valid": False, "cancelled": True, "message": "Verification cancelled"}
        return {"valid": True, "message": "Intact"}

    async def bound(path, *, cancel_event=None):
        return 0

    monkeypatch.setitem(vars(tool), "verify", verify)
    monkeypatch.setitem(vars(tool), "verify_timeout", bound)
    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 3})
    library = routes._library(lib)

    async def tick() -> dict:
        return await sweep.sweep(library)  # the scheduler's tick, not Run now

    async def until(predicate) -> None:
        for _ in range(200):
            if predicate():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("timed out")

    first = await tick()
    assert first["checks"] == 1
    await until(lambda: len(calls) == 1)

    second = await tick()
    assert second.get("checks", 0) == 0  # held open: nothing more this window
    assert len(calls) == 1
    assert not ((await sweep.get_state(library, CHECK)).get("ps2") or {}).get("last_run_at")

    (running,) = [
        j for j in job_manager.jobs.values()
        if j.mode == ConversionMode.CHECK and j.status.value == "processing"
    ]
    await job_manager.cancel_job(running.id)
    await checks.drain()
    assert not ((await sweep.get_state(library, CHECK)).get("ps2") or {}).get("last_run_at")

    third = await tick()  # still due after the cancel
    assert third["checks"] == 1
    await until(lambda: len(calls) == 2)
    gate.set()
    await checks.drain()
    assert ((await sweep.get_state(library, CHECK)).get("ps2") or {}).get("last_run_at")
    fourth = await tick()  # recorded: not due again this window
    assert fourth.get("checks", 0) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("held_by", ["conversion", "lock"])
async def test_a_tick_that_finds_its_image_held_by_a_conversion_stays_due(
    tree: Path, ready, verifier, sent, monkeypatch, held_by,
) -> None:
    """Codex 4578154650: an Image a conversion holds (queued or converting
    here, or locked for writing by another process) is held elsewhere, like
    one a check run holds. A pick that comes up empty only for that records
    nothing; once the conversion ends the next tick checks it."""
    from routes import libraries as routes
    from services.job_manager import job_manager
    from services.lock_manager import LockManager, lock_manager
    from services.rules import check as checks
    from services.rules import sweep
    from services.rules.rules import CHECK

    _verdicts, calls = verifier
    for name in ("Aria", "Bolt", "Dune"):
        (tree / "ps2" / f"{name}.chd").unlink()
    cove = str(tree / "ps2" / "Cove.chd")
    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 6})
    library = routes._library(lib)
    writer = LockManager()
    writer._lock_dir = lock_manager._lock_dir
    if held_by == "conversion":
        monkeypatch.setattr(
            job_manager, "get_active_job_candidates", lambda: [("convert1", [cove])],
        )
    else:
        assert writer.acquire_lock(cove, allow_existing=True)

    tick = await sweep.sweep(library)
    await checks.drain()
    assert tick.get("checks", 0) == 0
    assert calls == []
    assert not ((await sweep.get_state(library, CHECK)).get("ps2") or {}).get("last_run_at")

    if held_by == "conversion":
        monkeypatch.setattr(job_manager, "get_active_job_candidates", lambda: [])
    else:
        writer.release_lock(cove)
    again = await sweep.sweep(library)
    await checks.drain()
    assert again["checks"] == 1
    assert calls == ["Cove.chd"]
    assert ((await sweep.get_state(library, CHECK)).get("ps2") or {}).get("last_run_at")


@pytest.mark.asyncio
async def test_a_check_runs_verdicts_ride_its_terminal_event(
    tree: Path, ready, verifier, sent, monkeypatch,
) -> None:
    """Codex 4578116084: an open browser hears each Image's new check state on
    the run's terminal event (the one the jobs stream already forwards), so
    an Intact badge a failed check took away goes away there too."""
    import os

    from services.job_manager import job_manager
    from services.verification_store import verification_store

    verdicts, _calls = verifier
    verdicts["Bolt.chd"] = {"valid": False, "message": "CHD hunk 12 CRC mismatch"}
    bolt = str(tree / "ps2" / "Bolt.chd")
    cove = str(tree / "ps2" / "Cove.chd")
    await verification_store.mark_verified(bolt)
    events: list[dict] = []
    real = job_manager._notify_subscribers

    async def capture(job_id, data):
        events.append(data)
        await real(job_id, data)

    monkeypatch.setitem(vars(job_manager), "_notify_subscribers", capture)
    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 6})
    _seed(lib, {"ps2/Aria.chd": 2, "ps2/Bolt.chd": 10, "ps2/Dune.chd": 1})

    await _run(lib)

    (end,) = [e for e in events if e["type"] in ("complete", "error", "cancelled")]
    assert end["type"] == "complete"
    assert end["verification_changes"] == {
        os.path.realpath(cove): "intact",
        os.path.realpath(bolt): None,  # no longer Intact
    }


@pytest.mark.asyncio
async def test_a_tick_that_finds_every_image_held_by_run_now_stays_due(
    tree: Path, ready, sent, monkeypatch,
) -> None:
    """Codex 4578116078: while Run now is checking every Image of a Platform,
    a scheduler tick selects none of them. That empty pick is not the night's
    run: nothing is recorded, and once Run now is cancelled the next tick picks."""
    from models import ConversionMode
    from routes import libraries as routes
    from services.job_manager import job_manager
    from services.rules import check as checks
    from services.rules import sweep
    from services.rules.rules import CHECK

    tool = registry.get("chdman")
    gate = asyncio.Event()

    async def verify(path, *, cancel_event=None):
        waits = {asyncio.ensure_future(gate.wait())}
        if cancel_event is not None:
            waits.add(asyncio.ensure_future(cancel_event.wait()))
        _done, pending = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        if cancel_event is not None and cancel_event.is_set():
            return {"valid": False, "cancelled": True, "message": "Verification cancelled"}
        return {"valid": True, "message": "Intact"}

    async def bound(path, *, cancel_event=None):
        return 0

    monkeypatch.setitem(vars(tool), "verify", verify)
    monkeypatch.setitem(vars(tool), "verify_timeout", bound)
    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 100})
    library = routes._library(lib)

    manual = await sweep.sweep(library, ignore_schedule=True)  # Run now: all four
    assert manual["checks"] == 4

    tick = await sweep.sweep(library)
    assert tick.get("checks", 0) == 0
    assert not ((await sweep.get_state(library, CHECK)).get("ps2") or {}).get("last_run_at")

    (running,) = [
        j for j in job_manager.jobs.values()
        if j.mode == ConversionMode.CHECK and j.status.value == "processing"
    ]
    await job_manager.cancel_job(running.id)
    await checks.drain()

    again = await sweep.sweep(library)
    assert again["checks"] == 4
    gate.set()
    await checks.drain()


@pytest.mark.asyncio
async def test_a_run_stopped_by_an_abandoned_verifier_leaves_its_rule_due(
    tree: Path, ready, verifier, sent,
) -> None:
    """Codex 4578054443: two picked Images, the first verifier abandoned on
    stalled storage. The run stops before the second, so the Rule is not
    recorded as run, and the next tick picks again."""
    from routes import libraries as routes
    from services.rules import check as checks
    from services.rules import sweep
    from services.rules.rules import CHECK

    verdicts, calls = verifier
    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 6})
    _seed(lib, {"ps2/Aria.chd": 2, "ps2/Bolt.chd": 10, "ps2/Dune.chd": 1})
    # Cove (never checked) goes first, then Bolt.
    verdicts["Cove.chd"] = {"valid": False, "abandoned": True,
                            "message": "the verifier did not exit"}
    library = routes._library(lib)

    first = await sweep.sweep(library)
    await checks.drain()

    assert first["checks"] == 2
    assert calls == ["Cove.chd"]  # stopped before Bolt
    state = (await sweep.get_state(library, CHECK)).get("ps2") or {}
    assert not state.get("last_run_at")

    verdicts.pop("Cove.chd")
    second = await sweep.sweep(library)  # still due
    await checks.drain()
    assert second["checks"] == 2
    assert ((await sweep.get_state(library, CHECK)).get("ps2") or {}).get("last_run_at")


def test_a_numbered_file_is_a_split_part_only_beside_a_modes_output_name(db: Path) -> None:
    """#306 ab2d818: ``save.0`` is no split part, so a folder Library never lists
    it; ``Game.iso.0`` with its ``.1`` is."""
    from .test_cli_306 import _real_split_iso

    roms = db / "roms" / "ps3"
    iso = _real_split_iso(roms)
    (roms / "save.0").write_bytes(b"\0" * 4096)
    (roms / "save.1").write_bytes(b"\0" * 4096)

    assert registry.split_owner(str(roms / "save.0")) is None
    assert registry.split_members(str(roms / "save.0")) == []
    assert registry.split_owner(str(roms / "GAME.iso.1")) == (str(iso), "folder_to_iso")


@pytest.mark.asyncio
async def test_a_split_part_outside_the_volumes_skips_the_whole_image(
    db: Path, ready, sent, tmp_path_factory,
) -> None:
    """#306 ab2d818: every file a check reads is inside the Volumes, or the Image
    is not checked. A ``.1`` symlinked outside skips GAME.iso.0."""
    from routes import libraries as routes
    from services.verification_store import verification_store

    from .test_cli_306 import _real_split_iso

    roms = db / "roms"
    iso = _real_split_iso(roms / "ps3")
    await verification_store.mark_verified(
        str(iso), produced_meta={"mode": "folder_to_iso", "tool": "makeps3iso"},
    )
    outside = tmp_path_factory.mktemp("elsewhere") / "GAME.iso.1"
    part1 = roms / "ps3" / "GAME.iso.1"
    outside.write_bytes(part1.read_bytes())
    part1.unlink()
    part1.symlink_to(outside)
    created = await routes.register_library(routes.RegisterLibraryRequest(path=str(roms)))
    await routes.put_library_rules(created["id"], {"check_rules": {"ps3": {
        "enabled": True, "check_budget_gb": 1,
    }}})

    preview = await routes.preview_library(created["id"])

    (summary,) = preview["platforms"]
    assert summary["candidates"] == []
    assert summary["checks"] == 0
