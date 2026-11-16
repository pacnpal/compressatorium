"""A check run counts only if its listing is still current when it is recorded (#360).

#353 compared the Library's walk generation when a check Rule's pick ended.
But a check run is recorded later, when its check job ends
(``check_run_ended`` -> ``record_check_runs`` -> ``_record_run``). One tick
can queue conversions and start a check job; the conversions finish while the
job runs, after the pick. The run was still recorded, so the new CHDs waited a
day for their first check.

Now the walk generation the pick listed at travels with the run to the moment
it is recorded, and ``_record_run`` compares it there, for both the tick-end
and the job-end paths. A check job's own end changes no Library file, so a
quiet Library still records its run.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from services.job_manager import job_manager

from . import test_check_clock_stale_walk_353 as stale_walk_353
from . import test_check_rules_305 as check_rules_305
from .test_check_clock_stale_walk_353 import _check_state, _converted
from .test_check_rules_305 import GB, _folder_library, _sparse

# #305's and #353's fixtures, shared here: pytest registers a fixture found
# among this module's attributes.
_db_fixture = check_rules_305._db_fixture
_tree = check_rules_305._tree
_ready = check_rules_305._ready
_verifier = check_rules_305._verifier
_sent = check_rules_305._sent
_attached = stale_walk_353._attached

_OLD = ("Aria", "Bolt", "Cove", "Dune")   # the `tree` fixture, 3 GB each
_NEW = ("Echo", "Fern", "Gale")


@pytest.mark.asyncio
async def test_conversions_finishing_during_the_check_job_leave_the_rule_due(
    tree: Path, ready, verifier, sent, attached, monkeypatch,
) -> None:
    """The issue: the conversions land after the pick, while the job checks.

    The run must not be recorded, and the next tick checks the new CHDs.
    """
    from services import libraries
    from services.rules import check as checks
    from services.rules import sweep as rules_sweep

    _verdicts, calls = verifier
    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 12})
    library = libraries.get(lib)

    tool = check_rules_305.registry.get("chdman")
    real_verify = vars(tool)["verify"]
    landed: list[str] = []

    async def verify_while_converting(path, *, cancel_event=None):
        if not landed:
            # The pick is over; the check job is running. Three conversions
            # queued by the same tick finish now.
            for name in _NEW:
                chd = _sparse(tree / "ps2" / f"{name}.chd", 3 * GB)
                _converted(tree / "ps2" / f"{name}.iso", chd)
                landed.append(chd.name)
        return await real_verify(path, cancel_event=cancel_event)

    monkeypatch.setitem(vars(tool), "verify", verify_while_converting)

    await rules_sweep.sweep(library)
    await checks.drain()

    assert sorted(calls) == sorted(f"{n}.chd" for n in _OLD)
    assert "last_run_at" not in await _check_state(lib)   # still due

    await rules_sweep.sweep(library)                       # the next tick
    await checks.drain()

    # Never checked, so the stalest: all three are in the next pick (the
    # budget's last 3 GB goes to the stalest old Image).
    assert set(landed) <= set(calls[len(_OLD):])
    assert "last_run_at" in await _check_state(lib)


@pytest.mark.asyncio
async def test_a_quiet_library_records_its_run_when_the_check_job_ends(
    tree: Path, ready, verifier, sent, attached,
) -> None:
    """No file changed during the job: the run is recorded as before."""
    from services import libraries
    from services.rules import check as checks
    from services.rules import sweep as rules_sweep

    _verdicts, calls = verifier
    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 12})
    library = libraries.get(lib)
    before = library.walk_generation()

    await rules_sweep.sweep(library)
    await checks.drain()

    assert sorted(calls) == sorted(f"{n}.chd" for n in _OLD)
    assert "last_run_at" in await _check_state(lib)
    # The check job's own end (through the job manager) moved no walk.
    assert library.walk_generation() == before


def test_a_check_job_ending_drops_no_walk(db: Path, attached) -> None:
    """A check job writes no Library file, whatever path it carries.

    External jobs (a check run, a metadata scan, a DAT match) carry a
    sentinel path today; the listener must not depend on that to leave the
    walk alone.
    """
    import asyncio

    from models import ConversionMode, JobStatus
    from services import libraries

    roms = db / "roms"
    (roms / "ps2").mkdir(parents=True)
    asyncio.run(_folder_library(roms, {"enabled": True, "check_budget_gb": 3}))
    (library,) = libraries.folder_libraries()
    before = library.walk_generation()

    job_manager._notify_terminal(SimpleNamespace(
        id="job-360-check", status=JobStatus.COMPLETED, mode=ConversionMode.CHECK,
        file_path=str(roms / "ps2" / "Aria.chd"), output_path=None,
    ))

    assert library.walk_generation() == before
