"""A re-pin plan never overwrites a snapshot it does not own (#337).

``POST /romm/repin/plan`` checks each destination for a live job, then awaits
several bounded probes, then records under ``_settle_lock``. ``record()``
supersedes whatever pending row the destination holds. Two reviews found the
same hole from two sides:

* CodeRabbit (#320): a conversion is queued to the destination while the plan
  sits in its probes. The plan's write supersedes that job's row, the plan's
  own submit is refused (the destination is taken), and its cleanup retires
  the row it just wrote. The queued conversion finishes with no metadata.
* Codex (#324): an aborted Auto run's snapshot is still in its probes when a
  newer run records the same destination. The abandoned request resumes, its
  write supersedes the newer row, and ``retireLateSnapshot()`` then cancels
  the replacement.

The fix is ownership: every plan carries a generation, and ``record()``
refuses to supersede a row that a newer plan or a live job owns.
"""
from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from routes import romm as romm_routes
from services import db as _db
from services.romm import RommClient
from services.romm import repin as romm_repin


@pytest.fixture(name="repin_db")
def _repin_db(tmp_path: Path):
    """A real SQLite database wired into the module-level session factory."""
    if _db.engine is not None:
        _db.engine.dispose()
    _db.init_engine(str(tmp_path / "compressatorium.db"), create_schema=True)
    yield
    if _db.engine is not None:
        _db.engine.dispose()
    _db.engine = None
    _db.SessionLocal = None


def _library(tmp_path: Path) -> tuple[str, str, str]:
    """Two sources whose conversions land on one destination."""
    for name in ("a", "b"):
        (tmp_path / "roms" / name).mkdir(parents=True)
        (tmp_path / "roms" / name / "Game.iso").write_bytes(b"\0" * 32)
    out = tmp_path / "out"
    out.mkdir()
    return (
        str(tmp_path / "roms" / "a" / "Game.iso"),
        str(tmp_path / "roms" / "b" / "Game.iso"),
        str(out),
    )


_ROM_A = {"id": 1, "name": "Game A", "igdb_id": 11}
_ROM_B = {"id": 2, "name": "Game B", "igdb_id": 22}


def _request(source: str, out: str, **extra) -> romm_routes.RepinPlanRequest:
    return romm_routes.RepinPlanRequest(
        paths=[source], mode="dolphin_rvz", output_dir=out,
        duplicate_action="overwrite", **extra,
    )


def _patches(tmp_path: Path, a: str, b: str, live: list, fingerprint):
    roms = {os.path.realpath(a): _ROM_A, os.path.realpath(b): _ROM_B}
    return (
        patch.object(RommClient, "base_url", "http://romm:8080"),
        patch.object(RommClient, "library_root", str(tmp_path)),
        patch.object(
            romm_routes.romm_repin, "roms_by_local_path",
            lambda paths, _pid=None: {
                os.path.realpath(p): roms[os.path.realpath(p)] for p in paths
            },
        ),
        patch.object(romm_routes, "is_within_configured_volumes", lambda _p: True),
        patch.object(
            romm_routes.job_manager, "get_active_job_candidates", lambda: list(live),
        ),
        patch.object(romm_repin, "path_fingerprint", fingerprint),
    )


@pytest.mark.asyncio
async def test_a_job_queued_while_the_plan_probes_keeps_its_snapshot(
    repin_db, tmp_path: Path,
) -> None:
    """CodeRabbit's repro: the write phase must recheck who owns the destination."""
    a, b, out = _library(tmp_path)
    live: list = []
    theirs: dict = {}
    real_fingerprint = romm_repin.path_fingerprint

    def _fingerprint(path):
        # While this plan sits in its probe, another run records the same
        # destination and its conversion is queued.
        if not theirs:
            theirs["id"] = romm_repin.record(
                _ROM_B, path, {"igdb_id": 22}, "dolphin_rvz", "", b,
            )
            live.append(("job-b", [b, path]))
        return real_fingerprint(path)

    p = _patches(tmp_path, a, b, live, _fingerprint)
    with p[0], p[1], p[2], p[3], p[4], p[5]:
        mine = await romm_routes.romm_repin_plan(_request(a, out))
        # This run's submit is refused (the queue holds the destination), so
        # it retires what it recorded, exactly as the store does.
        await romm_routes.romm_repin_cancel(
            romm_routes.RepinCancelRequest(ids=list(mine["recorded_ids"].values())),
        )

    rows = romm_repin.pending_rows(10)
    assert [row[5] for row in rows] == [theirs["id"]], rows
    assert rows[0][3] == {"igdb_id": 22}, rows
    assert mine["recorded"] == 0, mine


@pytest.mark.asyncio
@pytest.mark.parametrize("queued_before_resume", [False, True])
async def test_an_abandoned_snapshot_cannot_supersede_a_newer_run(
    repin_db, tmp_path: Path, queued_before_resume: bool,
) -> None:
    """Codex's repro: a late write from an aborted run loses to the newer run.

    Both orders of the newer run's queueing and the old request's resume are
    covered: the outcome must not depend on which one lands first.
    """
    a, b, out = _library(tmp_path)
    live: list = []
    in_probe = threading.Event()
    release = threading.Event()
    real_fingerprint = romm_repin.path_fingerprint
    calls = {"n": 0}

    def _fingerprint(path):
        calls["n"] += 1
        if calls["n"] == 1:
            # The old run's request: stalled in its probe after it passed the
            # pending-job check.
            in_probe.set()
            release.wait(5)
        return real_fingerprint(path)

    p = _patches(tmp_path, a, b, live, _fingerprint)
    with p[0], p[1], p[2], p[3], p[4], p[5]:
        old = asyncio.create_task(romm_routes.romm_repin_plan(_request(a, out)))
        for _ in range(500):
            if in_probe.is_set():
                break
            await asyncio.sleep(0.01)
        assert in_probe.is_set()

        # The user aborted; Start is free, and a newer run records the same
        # destination.
        newer = await romm_routes.romm_repin_plan(_request(b, out))
        destination = newer["recorded_paths"][b]
        if queued_before_resume:
            live.append(("job-b", [b, destination]))

        release.set()
        late = await old
        live[:] = [("job-b", [b, destination])]
        # retireLateSnapshot(): the abandoned run retires whatever it wrote.
        await romm_routes.romm_repin_cancel(
            romm_routes.RepinCancelRequest(ids=list(late["recorded_ids"].values())),
        )

    rows = romm_repin.pending_rows(10)
    assert [row[5] for row in rows] == [newer["recorded_ids"][b]], rows
    assert rows[0][3] == {"igdb_id": 22}, rows
    assert late["recorded"] == 0, late


def test_record_refuses_to_supersede_a_newer_plan_s_row(repin_db) -> None:
    """The generation is the plan's age, not the time its write lands."""
    older = romm_repin.plan_generation(None)
    newer = romm_repin.plan_generation(None)
    assert newer > older

    kept = romm_repin.record(
        _ROM_B, "/vol/Game.rvz", {"igdb_id": 22}, generation=newer,
    )
    assert romm_repin.record(
        _ROM_A, "/vol/Game.rvz", {"igdb_id": 11}, generation=older,
    ) == 0
    rows = romm_repin.pending_rows(10)
    assert [row[5] for row in rows] == [kept]
    assert rows[0][3] == {"igdb_id": 22}

    # A newer plan still supersedes, which is what re-submitting relies on.
    replaced = romm_repin.record(
        _ROM_A, "/vol/Game.rvz", {"igdb_id": 11},
        generation=romm_repin.plan_generation(None),
    )
    assert [row[5] for row in romm_repin.pending_rows(10)] == [replaced]


def test_record_refuses_to_supersede_a_row_a_live_job_owns(repin_db) -> None:
    """Whatever its age, a row whose conversion is queued stays."""
    kept = romm_repin.record(
        _ROM_B, "/vol/Game.rvz", {"igdb_id": 22}, source_path="/lib/b/Game.iso",
    )
    seen = []

    def _owned(output_path, source_path):
        seen.append((output_path, source_path))
        return True

    assert romm_repin.record(
        _ROM_A, "/vol/Game.rvz", {"igdb_id": 11}, source_path="/lib/a/Game.iso",
        owned_by_live_job=_owned,
    ) == 0
    assert seen == [("/vol/Game.rvz", "/lib/b/Game.iso")]
    assert [row[5] for row in romm_repin.pending_rows(10)] == [kept]


def test_a_claimed_generation_never_runs_ahead_of_the_issued_ones(repin_db) -> None:
    """A client can carry its run's generation, never invent a newer one."""
    issued = romm_repin.plan_generation(None)
    assert romm_repin.plan_generation(issued) == issued
    assert romm_repin.plan_generation(issued + 1000) == issued
