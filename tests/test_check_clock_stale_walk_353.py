"""A check Rule never counts a run that looked at an out-of-date walk (#353).

A folder Library keeps its walk for ``SCAN_TTL_SECONDS``. A conversion that
added an Image just before a scheduled check ran left that walk out of date:
the check listed the old Images, picked nothing new, and still moved its 24 h
clock, so the new Image waited a day for its first check.

Now a finished job tells the Libraries which paths it changed
(``libraries.note_job_finished``, attached like the notifications listener),
and a folder Library whose root holds one drops its walk. Separately, a run is
recorded only when the Library's walk did not move between its listing and
the decision to record: a run that saw a stale walk stays due.
"""
from __future__ import annotations

import itertools
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.job_manager import job_manager

from . import test_check_rules_305 as check_rules_305
from .test_check_rules_305 import GB, _folder_library, _seed, _sparse

# #305's fixtures (`db`, `tree`, `ready`, `verifier`, `sent`), shared here:
# pytest registers a fixture found among this module's attributes.
_db_fixture = check_rules_305._db_fixture
_tree = check_rules_305._tree
_ready = check_rules_305._ready
_verifier = check_rules_305._verifier
_sent = check_rules_305._sent

_ids = itertools.count()


@pytest.fixture(name="attached")
def _attached():
    """The Libraries' job listener on the job manager, as the server attaches it."""
    from services import libraries

    libraries.attach(job_manager)
    yield
    job_manager.remove_terminal_listener(libraries.note_job_finished)


def _converted(source: Path, output: Path):
    """A finished conversion, announced as the job manager announces one."""
    from models import JobStatus

    job = SimpleNamespace(
        id=f"job-353-{next(_ids)}", status=JobStatus.COMPLETED,
        file_path=str(source), output_path=str(output),
    )
    job_manager._notify_terminal(job)
    return job


async def _check_state(lib: str) -> dict:
    from routes import libraries as routes

    return (await routes.get_library_rules(lib))["check_state"].get("ps2", {})


@pytest.mark.asyncio
async def test_a_check_right_after_a_conversion_checks_the_new_image(
    tree: Path, ready, verifier, sent, attached,
) -> None:
    """The issue: convert, then the scheduled check runs within 30 s.

    The walk the Library holds was taken before the conversion. The new CHD
    is the stalest Image (never checked), so the check picks it.
    """
    from services import libraries
    from services.rules import check as checks
    from services.rules import sweep as rules_sweep

    _verdicts, calls = verifier
    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 3})
    _seed(lib, {f"ps2/{n}.chd": 1 for n in ("Aria", "Bolt", "Cove", "Dune")})
    library = libraries.get(lib)
    library.scan(refresh=True)                      # the walk, taken before

    echo = _sparse(tree / "ps2" / "Echo.chd", 3 * GB)
    _converted(tree / "ps2" / "Echo.iso", echo)     # a conversion just finished

    await rules_sweep.sweep(library)                # the scheduled tick, within 30 s
    await checks.drain()

    assert calls == ["Echo.chd"]
    assert "last_run_at" in await _check_state(lib)


@pytest.mark.asyncio
async def test_a_conversion_finishing_during_the_pick_leaves_the_rule_due(
    tree: Path, ready, verifier, sent, attached, monkeypatch,
) -> None:
    """A run whose listing went stale before it was recorded does not count.

    The conversion lands while the check is picking from a listing taken just
    before it: that run must not move the clock, and the next tick checks the
    new Image.
    """
    from services import libraries
    from services.rules import check as checks
    from services.rules import sweep as rules_sweep

    _verdicts, calls = verifier
    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 3})
    _seed(lib, {f"ps2/{n}.chd": 1 for n in ("Aria", "Bolt", "Cove", "Dune")})
    library = libraries.get(lib)

    real_inspect = checks.inspect
    landed: list[Path] = []

    def inspect_while_converting(image):
        if not landed:
            echo = _sparse(tree / "ps2" / "Echo.chd", 3 * GB)
            landed.append(echo)
            _converted(tree / "ps2" / "Echo.iso", echo)
        return real_inspect(image)

    monkeypatch.setattr(checks, "inspect", inspect_while_converting)
    await rules_sweep.sweep(library)
    await checks.drain()

    assert "Echo.chd" not in calls
    assert "last_run_at" not in await _check_state(lib)   # still due

    await rules_sweep.sweep(library)                       # the next tick
    await checks.drain()
    assert calls[-1] == "Echo.chd"
    assert "last_run_at" in await _check_state(lib)


@pytest.mark.asyncio
async def test_nothing_to_check_on_a_current_walk_still_moves_the_clock(
    db: Path, ready, verifier, sent, attached,
) -> None:
    """A run that looked at the current walk and found no Image is a real run."""
    from services import libraries
    from services.rules import check as checks
    from services.rules import sweep as rules_sweep

    _verdicts, calls = verifier
    roms = db / "roms"
    (roms / "ps2").mkdir(parents=True)
    lib = await _folder_library(roms, {"enabled": True, "check_budget_gb": 3})
    library = libraries.get(lib)

    await rules_sweep.sweep(library)
    await checks.drain()

    assert calls == []
    assert "last_run_at" in await _check_state(lib)


def test_a_job_outside_every_library_drops_no_walk(db: Path, attached) -> None:
    """Only a Library whose root holds a changed path walks again."""
    import asyncio

    from services import libraries

    roms = db / "roms"
    (roms / "ps2").mkdir(parents=True)
    other = db / "elsewhere"
    other.mkdir()
    asyncio.run(_folder_library(roms, {"enabled": True, "check_budget_gb": 3}))
    (library,) = libraries.folder_libraries()
    before = library.walk_generation()

    _converted(other / "a.iso", other / "a.chd")
    assert library.walk_generation() == before

    _converted(other / "b.iso", roms / "ps2" / "b.chd")
    assert library.walk_generation() == before + 1


@pytest.mark.asyncio
async def test_a_job_finishing_during_a_walk_does_not_fail_the_listing(
    tree: Path, attached, monkeypatch,
) -> None:
    """A busy night of conversions must not make Rules listings fail.

    Unlike a mapping change, a finished job only makes the walk out of date
    for the next listing; the walk it overtook is still served, never refused
    as ``listing_in_progress``.
    """
    from services import libraries
    from services.libraries import folder as folder_mod

    lib = await _folder_library(tree, {"enabled": True, "check_budget_gb": 3})
    library = libraries.get(lib)
    real = folder_mod.platform_detect.detect_uncached
    fired: list[int] = []

    def detect_while_a_job_ends(path, **kwargs):
        if not fired:
            fired.append(1)
            _converted(tree / "ps2" / "Echo.iso", tree / "ps2" / "Echo.chd")
        return real(path, **kwargs)

    monkeypatch.setattr(folder_mod.platform_detect, "detect_uncached", detect_while_a_job_ends)

    images = library.images("ps2")

    assert fired == [1]
    assert len(images) == 4


def test_the_server_and_the_cli_attach_the_libraries_listener() -> None:
    import re

    app = Path(__file__).resolve().parents[1] / "app"
    attach = re.compile(r"\blibraries(?:_service)?\.attach\(job_manager\)")
    assert attach.search((app / "main.py").read_text(encoding="utf-8"))
    assert attach.search((app / "cli.py").read_text(encoding="utf-8"))
