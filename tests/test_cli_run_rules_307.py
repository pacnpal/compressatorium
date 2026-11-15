"""`compressatorium run-rules` and the one-scheduler-per-data-directory lock (#307).

Each test calls the CLI's ``main()`` against a temp Volume and data directory.
The Libraries are swapped for small folder Libraries, and dolphin-tool is
stubbed at the ``SubprocessRunner`` seam, so a pass drives the real sweep, job
manager, Ledger and Rule state.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import textwrap
from collections import OrderedDict
from pathlib import Path
from typing import ClassVar

import pytest
from services.tools import registry


class _Shelf:
    """A folder Library: one Platform (``gc``), the files in ``<root>/gc``."""

    capabilities = frozenset()
    legacy_preference_keys: ClassVar[dict] = {}

    def __init__(self, library_id: str, root: Path) -> None:
        self.id = library_id
        self.root = root
        # What happened, in order: ("before_pass", dry_run), ("images", id).
        self.calls: list[tuple] = []
        self.unreadable = False  # images() raises LibraryUnreadable
        self.broken = False  # configured() raises: the whole sweep fails
        self.on_images = None  # called inside images(), for a signal mid-sweep

    async def before_pass(self, *, dry_run: bool) -> None:
        self.calls.append(("before_pass", dry_run))

    def configured(self):
        if self.broken:
            raise RuntimeError("database is locked")
        return True

    def canonical_platform_id(self, raw):
        return raw if raw == "gc" else None

    def platform_value(self, platform_id):
        return platform_id

    def platforms(self):
        from services.rules import Platform

        return [Platform(id="gc", slug="ngc", name="GameCube")]

    def images(self, platform_id):
        from services.rules import Image, LibraryUnreadable

        self.calls.append(("images", platform_id))
        if self.on_images is not None:
            self.on_images()
        if self.unreadable:
            raise LibraryUnreadable("RomM is not answering")
        return [
            Image(
                key=p.name, name=p.name, size=p.stat().st_size,
                added_at=p.stat().st_mtime, platform="ngc", path=str(p),
            )
            for p in sorted((self.root / platform_id).iterdir())
            if p.suffix == ".iso"
        ]

    def rule_defaults(self):
        return {}

    def max_per_sweep(self):
        return 25

    def schedule_enabled(self):
        return False  # the in-app switch; run-rules is its own

    def sweep_blocked(self):
        return None

    def summary_fields(self):
        return {}

    async def after_queue(self, rule, spec, queued, summary, result):
        return None

    async def sweep_extras(self):
        return {}


@pytest.fixture(name="env")
def _env(tmp_path, monkeypatch, reset_db_engine):
    from services import library_catalog
    from services.job_manager import job_manager
    from services.romm import settings as romm_settings

    from config import settings

    vol = tmp_path / "vol"
    vol.mkdir()
    config = tmp_path / "config"
    config.mkdir()
    locks = config / "locks"
    monkeypatch.setattr(settings, "chd_volumes", str(vol))
    monkeypatch.setattr(settings, "data_mount_root", str(vol))
    monkeypatch.setattr(settings, "data_dir", str(config))
    monkeypatch.setattr(settings, "db_path", str(config / "compressatorium.db"))
    monkeypatch.setattr(settings, "temp_dir", str(tmp_path / "temp"))
    monkeypatch.setattr(settings, "max_queue_depth", 0)
    monkeypatch.setattr(settings, "concurrency_lock_dir", str(locks))
    # #306's helper: every file the lock managers keep, the queue's holder
    # file included, goes under the test's lock dir.
    from .test_cli_306 import _point_locks_at

    _point_locks_at(monkeypatch, locks)
    # A CLI process starts with an empty queue.
    monkeypatch.setattr(job_manager, "jobs", OrderedDict())
    monkeypatch.setattr(job_manager, "_queue", asyncio.PriorityQueue())
    # And with its own event loop: a module lock a job's outcome waits on
    # binds to the loop of the run, so each run here gets fresh ones.
    from services.rules import store as rules_store
    from services.rules import sweep as rules_sweep

    monkeypatch.setattr(rules_sweep, "_sweep_lock", asyncio.Lock())
    monkeypatch.setattr(rules_store, "_lock", asyncio.Lock())
    romm_settings.reset_for_tests()

    shelves = {
        "shelf": _Shelf("shelf", vol / "shelf"),
        "attic": _Shelf("attic", vol / "attic"),
    }
    for shelf in shelves.values():
        (shelf.root / "gc").mkdir(parents=True)
    monkeypatch.setattr(library_catalog, "all_libraries", lambda: list(shelves.values()))
    yield {"vol": vol, "config": config, "locks": locks, **shelves}
    romm_settings.reset_for_tests()
    reset_db_engine()


@pytest.fixture(name="dolphin")
def _dolphin(monkeypatch):
    service = registry.get("dolphin")._service
    state = {"fail": False, "runs": [], "seconds": 0}

    async def fake_run(cmd, *, input_path, output_path, **kwargs):
        state["runs"].append((input_path, output_path))
        if state["fail"]:
            raise RuntimeError("dolphin-tool failed with return code 1.")
        if state["seconds"]:
            # A slow run that, like the real runner, stops on its cancel event.
            from services.subprocess_runner import ConversionCancelled

            yield {"progress": 1, "message": "Converting"}
            cancel = kwargs.get("cancel_event") or asyncio.Event()
            try:
                await asyncio.wait_for(cancel.wait(), state["seconds"])
            except asyncio.TimeoutError:
                pass
            else:
                raise ConversionCancelled("Conversion cancelled")
        Path(output_path).write_bytes(b"converted output")
        yield {"progress": 100, "message": "Conversion complete"}

    async def fake_verify(cmd, *, path, **kwargs):
        yield {"type": "complete", "valid": True, "message": "Disc image is Intact"}

    monkeypatch.setattr(service._runner, "run", fake_run)
    monkeypatch.setattr(service._runner, "run_verify", fake_verify)
    return state


def _store_rules(env, rules_by_library: dict) -> None:
    """Create and migrate the database, then save each Library's Rules."""
    import cli
    from services.rules import sweep as rules_sweep

    cli._open_db(False)

    async def save():
        for library_id, rules in rules_by_library.items():
            await rules_sweep.set_rules(env[library_id], rules)

    asyncio.run(save())


def _iso(env, library_id="shelf", name="Game.iso"):
    path = env[library_id].root / "gc" / name
    path.write_bytes(b"the disc" * 512)
    return path


def _cli(capsys, *argv):
    import cli

    code = cli.main(list(argv))
    captured = capsys.readouterr()
    lines = [json.loads(line) for line in captured.out.splitlines() if line.strip()]
    return code, lines, captured.err


def _rule_state(env, library_id="shelf"):
    from services.rules import sweep as rules_sweep

    return asyncio.run(rules_sweep.get_state(env[library_id]))


def _ledger_rows():
    from services import db as _db
    from services.ledger import _recent_sync

    if _db.SessionLocal is None:
        return []
    return _recent_sync(100)


RVZ_RULE = {"gc": {"mode": "dolphin_rvz", "enabled": True}}


# --------------------------------------------------------------------------- #
# a pass
# --------------------------------------------------------------------------- #


def test_a_pass_queues_each_rules_images_waits_and_exits_0(env, dolphin, capsys):
    iso = _iso(env)
    _store_rules(env, {"shelf": RVZ_RULE})

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0, lines
    platform = [line for line in lines if line.get("platform_id") == "gc"
                and line["library"] == "shelf"]
    assert platform == [{
        "library": "shelf", "platform_id": "gc", "mode": "dolphin_rvz",
        "status": "queued", "queued": 1, "considered": 1,
        "candidates": ["Game.iso"], "reason": None,
    }]
    jobs = [line for line in lines if line.get("path")]
    assert len(jobs) == 1
    assert jobs[0]["library"] == "shelf"
    assert jobs[0]["path"] == str(iso)
    assert jobs[0]["status"] == "completed"
    assert jobs[0]["output"] == str(iso.with_suffix(".rvz"))
    # The job ran to its end before the command returned.
    assert iso.with_suffix(".rvz").read_bytes() == b"converted output"
    # Ledger rows and Rule state are written as for a server sweep.
    assert [r["source_path"] for r in _ledger_rows()] == [str(iso)]
    assert _rule_state(env)["gc"]["last_queued"] == 1


def test_a_library_with_no_rules_is_reported_and_left_alone(env, dolphin, capsys):
    _iso(env)
    _store_rules(env, {"shelf": RVZ_RULE})

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0
    assert {"library": "attic", "platform_id": None, "mode": None, "status": "skipped",
            "queued": 0, "considered": 0, "candidates": [], "reason": "no_rules"} in lines


def test_a_source_kept_while_another_job_reads_it_is_a_success(
    env, dolphin, capsys, monkeypatch,
):
    """The shared delete step (#306): the output is done and checked, but
    another process still reads the Source (here, locks the folder above it),
    so the job completes and keeps the Source with a warning. run-rules shows
    the warning and exits 0."""
    from services import job_manager as job_manager_module
    from services.job_manager import SOURCE_KEPT_FOR_READERS, JobManager
    from services.lock_manager import LockManager, lock_manager

    monkeypatch.setattr(job_manager_module, "DELETE_READER_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(job_manager_module, "DELETE_READER_POLL_SECONDS", 0.05)
    iso = _iso(env)
    folder = iso.parent
    # The output goes elsewhere, so a lock on the Source's folder is the
    # other reader's alone.
    out_dir = env["vol"] / "out"
    out_dir.mkdir()
    _store_rules(env, {"shelf": {"gc": {
        "mode": "dolphin_rvz", "enabled": True, "delete_on_verify": True,
        "check_level": "container_only", "output_dir": str(out_dir),
    }}})
    reader = LockManager()
    reader._lock_dir = lock_manager._lock_dir
    original = JobManager._release_inputs
    taken = []

    def release_then_read(self, job_id):
        original(self, job_id)
        if not taken:
            taken.append(reader.acquire_dir_lock(str(folder)))

    monkeypatch.setattr(JobManager, "_release_inputs", release_then_read)
    try:
        code, lines, _err = _cli(capsys, "run-rules", "--json")
    finally:
        reader.release_dir_lock(str(folder))

    assert taken == [True]
    assert code == 0, lines
    jobs = [line for line in lines if line.get("path")]
    assert [(j["status"], j["source_deleted"], j["reason"]) for j in jobs] == [
        ("completed", False, SOURCE_KEPT_FOR_READERS),
    ]
    assert iso.exists()
    assert (out_dir / "Game.rvz").exists()


def test_an_output_another_process_is_writing_is_skipped_not_failed(env, dolphin, capsys):
    """run-rules' counterpart of verify's "Busy: another job is writing it":
    the sweep skips an Image whose output is locked elsewhere as active, which
    is not an error, so the pass exits 0 and queues nothing."""
    from services.lock_manager import LockManager, lock_manager

    iso = _iso(env)
    _store_rules(env, {"shelf": RVZ_RULE})
    writer = LockManager()
    writer._lock_dir = lock_manager._lock_dir
    output = str(iso.with_suffix(".rvz"))
    assert writer.acquire_lock(output)
    try:
        code, lines, _err = _cli(capsys, "run-rules", "--json")
    finally:
        writer.release_lock(output)

    assert code == 0, lines
    platform = [line for line in lines if line.get("platform_id") == "gc"]
    assert [(p["status"], p["queued"], p["reason"]) for p in platform] == [("queued", 0, None)]
    assert [line for line in lines if line.get("path")] == []
    assert dolphin["runs"] == []


def test_a_failed_job_exits_1(env, dolphin, capsys):
    _iso(env)
    _store_rules(env, {"shelf": RVZ_RULE})
    dolphin["fail"] = True

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 1
    jobs = [line for line in lines if line.get("path")]
    assert [j["status"] for j in jobs] == ["failed"]
    assert "dolphin-tool failed" in jobs[0]["reason"]


def test_library_limits_the_pass_to_one_library(env, dolphin, capsys):
    shelf_iso = _iso(env, "shelf")
    attic_iso = _iso(env, "attic", "Other.iso")
    _store_rules(env, {"shelf": RVZ_RULE, "attic": RVZ_RULE})

    code, lines, _err = _cli(capsys, "run-rules", "--library", "attic", "--json")

    assert code == 0, lines
    assert {line["library"] for line in lines} == {"attic"}
    assert attic_iso.with_suffix(".rvz").exists()
    assert not shelf_iso.with_suffix(".rvz").exists()


def test_an_unknown_library_exits_2(env, dolphin, capsys):
    code, _lines, err = _cli(capsys, "run-rules", "--library", "nowhere")

    assert code == 2
    assert "nowhere" in err
    assert "shelf" in err


# --------------------------------------------------------------------------- #
# --dry-run
# --------------------------------------------------------------------------- #


def test_dry_run_shows_what_each_rule_would_queue_and_writes_nothing(env, dolphin, capsys):
    from services.preferences_store import preferences_store

    iso = _iso(env)
    _store_rules(env, {"shelf": RVZ_RULE})
    before = asyncio.run(preferences_store.get("rules.state"))

    code, lines, _err = _cli(capsys, "run-rules", "--dry-run", "--json")

    assert code == 0, lines
    assert {
        "library": "shelf", "platform_id": "gc", "mode": "dolphin_rvz",
        "status": "planned", "queued": 1, "considered": 1,
        "candidates": ["Game.iso"], "reason": None,
    } in lines
    assert dolphin["runs"] == []
    assert sorted(p.name for p in iso.parent.iterdir()) == ["Game.iso"]
    assert asyncio.run(preferences_store.get("rules.state")) == before
    assert not env["locks"].exists(), "a dry run creates no lock files"


def test_dry_run_moves_no_stored_rows(env, dolphin, capsys):
    """The one-time copies the Rule store makes on first read are not written."""
    from services.preferences_store import preferences_store

    _iso(env)
    _store_rules(env, {})
    asyncio.run(preferences_store.put("rules", {"shelf:gc": RVZ_RULE["gc"]}))
    asyncio.run(preferences_store.put("rules.check_level_upgrade", None))
    rows = {
        key: asyncio.run(preferences_store.get(key))
        for key in ("rules", "rules.state", "rules.migrated", "rules.check_level_upgrade")
    }

    code, lines, _err = _cli(capsys, "run-rules", "--dry-run", "--json")

    assert code == 0, lines
    assert any(line["status"] == "planned" for line in lines)
    for key, value in rows.items():
        assert asyncio.run(preferences_store.get(key)) == value, key


# --------------------------------------------------------------------------- #
# one scheduler per data directory
# --------------------------------------------------------------------------- #


def test_while_a_server_holds_the_lock_run_rules_exits_3_and_changes_nothing(
    env, dolphin, capsys,
):
    from services import scheduler_lock

    iso = _iso(env)
    _store_rules(env, {"shelf": RVZ_RULE})
    before = _rule_state(env)
    server = scheduler_lock.acquire("server")
    try:
        code, lines, err = _cli(capsys, "run-rules", "--json")
        dry_code, dry_lines, dry_err = _cli(capsys, "run-rules", "--dry-run", "--json")
    finally:
        server.release()

    assert code == 3
    assert lines == []
    assert "server" in err
    assert str(env["config"]) in err
    assert dry_code == 3
    assert dry_lines == []
    assert "server" in dry_err
    assert dolphin["runs"] == []
    assert sorted(p.name for p in iso.parent.iterdir()) == ["Game.iso"]
    assert _rule_state(env) == before
    assert _ledger_rows() == []


def test_a_lock_held_by_another_process_is_seen(env, dolphin, capsys):
    """The lock is an flock on a file in the shared lock dir, so it holds
    across processes (and containers that mount the same /config)."""
    from services import scheduler_lock

    _iso(env)
    _store_rules(env, {"shelf": RVZ_RULE})
    env["locks"].mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(f"""
            import fcntl, sys, time
            handle = open({scheduler_lock.lock_path()!r}, "a")
            fcntl.flock(handle, fcntl.LOCK_EX)
            print("held", flush=True)
            time.sleep(60)
        """)],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        code, lines, err = _cli(capsys, "run-rules", "--json")
    finally:
        holder.kill()
        holder.wait()

    assert code == 3
    assert lines == []
    assert "in use" in err
    assert dolphin["runs"] == []

    # Once that process is gone, the next pass runs.
    code, lines, _err = _cli(capsys, "run-rules", "--json")
    assert code == 0, lines
    assert dolphin["runs"]


def test_run_rules_holds_the_lock_for_its_whole_pass(env, dolphin, capsys, monkeypatch):
    from services import scheduler_lock

    _iso(env)
    _store_rules(env, {"shelf": RVZ_RULE})
    seen = []
    runner = registry.get("dolphin")._service._runner
    stubbed = runner.run  # the dolphin fixture's stub

    async def run_and_look(cmd, **kwargs):
        try:
            scheduler_lock.acquire("server", wait=0).release()
            seen.append("free")
        except scheduler_lock.SchedulerLockHeld as exc:
            seen.append(exc.holder.get("by"))
        async for event in stubbed(cmd, **kwargs):
            yield event

    monkeypatch.setattr(runner, "run", run_and_look)

    code, _lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0
    assert seen == ["run-rules"]
    # Released at the end: a server can start now.
    scheduler_lock.acquire("server", wait=0).release()


def test_lock_held_with_no_database_exits_3_and_creates_none(env, dolphin, capsys):
    from services import scheduler_lock

    server = scheduler_lock.acquire("server")
    try:
        code, lines, _err = _cli(capsys, "run-rules", "--json")
    finally:
        server.release()

    assert (code, lines) == (3, [])
    assert not (env["config"] / "compressatorium.db").exists()


class _ReachedTheDatabase(Exception):
    """Raised where the server's startup opens the database."""


@pytest.fixture(name="server_start")
def _server_start(env, monkeypatch):
    """Start ``main.lifespan`` up to the database, with short lock intervals."""
    import main
    from services import db as _db
    from services import scheduler_lock

    # The app's log handlers would outlive the test.
    monkeypatch.setattr(main, "configure_logging", lambda: None)
    monkeypatch.setattr(scheduler_lock, "_WAIT_POLL", 0.02)
    monkeypatch.setattr(scheduler_lock, "_WAIT_LOG_EVERY", 0.1)

    def reached(*args, **kwargs):
        raise _ReachedTheDatabase

    monkeypatch.setattr(_db, "resolve_db_path", reached)

    async def start():
        async with main.lifespan(main.app):
            pass

    return start


@pytest.mark.parametrize("holder", ["run-rules", "server"])
def test_a_server_waits_for_the_lock_then_starts(env, server_start, caplog, holder):
    """A server started during a run-rules pass (or beside another server)
    waits, saying who it waits for, and starts once the lock is free."""
    import logging

    from services import scheduler_lock

    caplog.set_level(logging.INFO)
    first = scheduler_lock.acquire(holder)

    async def scenario():
        task = asyncio.create_task(server_start())
        await asyncio.sleep(0.35)
        waiting = not task.done()
        first.release()
        with pytest.raises(_ReachedTheDatabase):
            await asyncio.wait_for(task, 5)
        return waiting

    try:
        assert asyncio.run(scenario()), "the server waited for the lock"
    finally:
        first.release()

    assert f"by {'the server' if holder == 'server' else 'another run-rules'}" in caplog.text
    assert caplog.text.count("Waiting for the Rule scheduler lock") >= 2, "logged again"
    # Released when startup failed, so the next start does not wait.
    scheduler_lock.acquire("server", wait=0).release()


def test_a_waiting_server_stops_on_sigterm(env, server_start):
    import os
    import signal

    from services import scheduler_lock

    told = []
    previous = signal.signal(signal.SIGTERM, lambda *a: told.append("uvicorn"))
    first = scheduler_lock.acquire("run-rules")
    fds_before = len(os.listdir("/dev/fd"))

    async def scenario():
        task = asyncio.create_task(server_start())
        await asyncio.sleep(0.1)
        os.kill(os.getpid(), signal.SIGTERM)
        with pytest.raises(RuntimeError, match="Stopped"):
            await asyncio.wait_for(task, 5)

    try:
        asyncio.run(scenario())
        handler_after = signal.getsignal(signal.SIGTERM)
    finally:
        first.release()
        signal.signal(signal.SIGTERM, previous)

    assert told == ["uvicorn"], "the server's own handler still heard it"
    assert handler_after is not None and handler_after.__name__ == "<lambda>"
    assert len(os.listdir("/dev/fd")) <= fds_before, "the lock file was closed"
    assert not (env["config"] / "compressatorium.db").exists()


# --------------------------------------------------------------------------- #
# errors, stops and the Library hook
# --------------------------------------------------------------------------- #


def test_an_unreadable_library_exits_1_and_the_rest_still_run(env, dolphin, capsys):
    env["shelf"].unreadable = True
    attic_iso = _iso(env, "attic")
    _store_rules(env, {"shelf": RVZ_RULE, "attic": RVZ_RULE})

    code, lines, _err = _cli(capsys, "run-rules", "--json")
    dry_code, dry_lines, _err = _cli(capsys, "run-rules", "--dry-run", "--json")

    assert code == 1
    assert {"library": "shelf", "platform_id": "gc", "mode": None, "status": "error",
            "queued": 0, "considered": 0, "candidates": [], "reason": "unreadable"} in lines
    assert attic_iso.with_suffix(".rvz").exists()
    assert dry_code == 1
    assert any(line.get("reason") == "unreadable" for line in dry_lines)


def test_a_sweep_that_raises_is_reported_and_the_next_library_runs(env, dolphin, capsys):
    env["shelf"].broken = True
    attic_iso = _iso(env, "attic")
    _store_rules(env, {"shelf": RVZ_RULE, "attic": RVZ_RULE})

    code, lines, err = _cli(capsys, "run-rules", "--json")

    assert code == 1
    shelf = [line for line in lines if line["library"] == "shelf"]
    assert [(s["status"], s["platform_id"]) for s in shelf] == [("error", None)]
    assert "database is locked" in shelf[0]["reason"]
    assert attic_iso.with_suffix(".rvz").exists()
    assert "database is locked" in err


def test_a_stop_during_a_sweep_queues_nothing_more(env, dolphin, capsys):
    import os
    import signal

    iso = _iso(env)
    _store_rules(env, {"shelf": RVZ_RULE})
    env["shelf"].on_images = lambda: os.kill(os.getpid(), signal.SIGTERM)

    code, _lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 1
    assert dolphin["runs"] == []
    assert not iso.with_suffix(".rvz").exists()


def test_a_job_queued_after_the_stop_is_cancelled(env, dolphin, capsys, monkeypatch):
    """A stop that lands while the batch is being queued: the job it let in
    is cancelled, not waited for."""
    import os
    import signal
    import time

    from services.job_manager import job_manager

    _iso(env)
    _store_rules(env, {"shelf": RVZ_RULE})
    dolphin["seconds"] = 30
    real = job_manager.create_batch_jobs

    async def stop_then_queue(*args, **kwargs):
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.sleep(0.1)  # the stop's own cancel finds nothing queued
        return await real(*args, **kwargs)

    monkeypatch.setattr(job_manager, "create_batch_jobs", stop_then_queue)
    started = time.monotonic()

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 1
    assert time.monotonic() - started < 15, "the job was cancelled, not waited for"
    assert [line["status"] for line in lines if line.get("path")] == ["cancelled"]


def test_each_library_runs_its_before_pass_hook_first(env, dolphin, capsys):
    _iso(env)
    _store_rules(env, {"shelf": RVZ_RULE})

    _cli(capsys, "run-rules", "--json")
    _cli(capsys, "run-rules", "--dry-run", "--json")

    # The dry pass lists no Images: the Rule ran a moment ago, so it is not due.
    assert env["shelf"].calls == [
        ("before_pass", False), ("images", "gc"), ("before_pass", True),
    ]
    assert env["attic"].calls == [("before_pass", False), ("before_pass", True)]


def test_libraries_are_listed_once_the_database_is_open(env, dolphin, capsys, monkeypatch):
    """A Library list read from the database (#303's folder Libraries) works."""
    from services import db as _db
    from services import library_catalog

    shelves = [env["shelf"], env["attic"]]

    def from_the_database():
        assert _db.SessionLocal is not None, "listed before the database was opened"
        return shelves

    monkeypatch.setattr(library_catalog, "all_libraries", from_the_database)
    iso = _iso(env)
    _store_rules(env, {"shelf": RVZ_RULE})
    _db.SessionLocal = None
    _db.engine = None

    code, lines, _err = _cli(capsys, "run-rules", "--library", "shelf", "--json")

    assert code == 0, lines
    assert iso.with_suffix(".rvz").exists()


def test_dry_run_keeps_a_legacy_copy_in_memory(env, dolphin, capsys):
    """A Library's first read copies its pre-#296 rows; a dry run does not."""
    from services.preferences_store import preferences_store

    _iso(env)
    env["shelf"].legacy_preference_keys = {"rules": "shelf.old_rules"}
    _store_rules(env, {})
    asyncio.run(preferences_store.put("shelf.old_rules", {"gc": RVZ_RULE["gc"]}))
    keys = ("rules", "rules.migrated", "shelf.old_rules")
    rows = {key: asyncio.run(preferences_store.get(key)) for key in keys}

    code, lines, _err = _cli(capsys, "run-rules", "--dry-run", "--json")

    assert code == 0, lines
    assert any(line["status"] == "planned" and line["library"] == "shelf" for line in lines)
    for key, value in rows.items():
        assert asyncio.run(preferences_store.get(key)) == value, key


def test_text_job_lines_name_the_library(env, dolphin, capsys):
    import cli

    iso = _iso(env)
    _store_rules(env, {"shelf": RVZ_RULE})

    assert cli.main(["run-rules"]) == 0
    out = capsys.readouterr().out.splitlines()

    assert f"completed  shelf: {iso} -> {iso.with_suffix('.rvz')}" in out


def test_romm_before_pass_does_what_the_server_does_at_startup(env, monkeypatch):
    """Load the settings; on a real pass also finish an owed identity cleanup
    and settle the re-pins RomM has matched since, once."""
    from routes import romm as romm_routes
    from services.romm import settings as romm_settings
    from services.romm.library import romm_library

    calls = []

    async def load(**kwargs):
        calls.append("load")
        return {}

    async def replay():
        calls.append("replay_identity_cleanup")

    async def settle():
        calls.append("settle_once")

    monkeypatch.setattr(romm_settings, "load", load)
    monkeypatch.setattr(romm_routes, "replay_identity_cleanup", replay)
    monkeypatch.setattr(romm_routes, "settle_once", settle)

    asyncio.run(romm_library.before_pass(dry_run=True))
    assert calls == ["load"]
    calls.clear()
    asyncio.run(romm_library.before_pass(dry_run=False))
    assert calls == ["load", "replay_identity_cleanup", "settle_once"]


# --------------------------------------------------------------------------- #
# review r2: a stop during upkeep, the waiting marker, --library with no DB
# --------------------------------------------------------------------------- #


def test_a_stop_during_before_pass_ends_the_run_promptly(env, dolphin, capsys):
    """The upkeep (RomM's settle pass) can take long; a stop cancels it."""
    import os
    import signal
    import time

    _iso(env)
    _store_rules(env, {"shelf": RVZ_RULE, "attic": RVZ_RULE})

    async def slow_upkeep(*, dry_run):
        env["shelf"].calls.append(("before_pass", dry_run))
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.sleep(30)

    env["shelf"].before_pass = slow_upkeep
    started = time.monotonic()

    code, _lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 1
    assert time.monotonic() - started < 10, "the upkeep was cancelled"
    assert dolphin["runs"] == []
    assert env["shelf"].calls == [("before_pass", False)]
    assert env["attic"].calls == [], "no Library after the stop"


def test_dry_run_with_no_database_still_checks_library(env, dolphin, capsys):
    code, lines, err = _cli(capsys, "run-rules", "--dry-run", "--library", "typo", "--json")

    assert (code, lines) == (2, [])
    assert "typo" in err
    assert "shelf" in err
    assert not (env["config"] / "compressatorium.db").exists()


@pytest.mark.parametrize("ending", ["acquired", "stopped", "cancelled"])
def test_a_waiting_server_keeps_a_marker_only_while_it_waits(env, monkeypatch, ending):
    from services import scheduler_lock

    monkeypatch.setattr(scheduler_lock, "_WAIT_POLL", 0.02)
    first = scheduler_lock.acquire("run-rules")
    marker = Path(scheduler_lock.waiting_path())
    stop = []

    async def scenario():
        task = asyncio.create_task(scheduler_lock.acquire_waiting(
            "server", on_wait=lambda held: None, should_stop=lambda: bool(stop),
        ))
        await asyncio.sleep(0.2)
        seen = marker.exists() and "run-rules" in marker.read_text()
        if ending == "acquired":
            first.release()
            (await asyncio.wait_for(task, 5)).release()
        elif ending == "stopped":
            stop.append(True)
            with pytest.raises(scheduler_lock.StoppedWaiting):
                await asyncio.wait_for(task, 5)
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        return seen

    try:
        assert asyncio.run(scenario()), "the marker names the holder while waiting"
    finally:
        first.release()
    assert not marker.exists()


def _healthcheck_script() -> str:
    """The HEALTHCHECK's shell command, from the Dockerfile."""
    import re

    dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text()
    block = re.search(r"HEALTHCHECK [^\n]*\\\n((?:.*\\\n)*.*\n)", dockerfile).group(1)
    command = block.replace("\\\n", "\n").strip()
    assert command.startswith("CMD ")
    return command[len("CMD "):]


def test_a_stop_while_images_are_inspected_ends_the_inspection(
    env, dolphin, capsys, monkeypatch,
):
    """A large Platform whose outputs all exist queues nothing, so only a
    check inside the candidate loop ends it before the catalog does."""
    import os
    import signal

    from services.rules import sweep as rules_sweep

    for n in range(5):
        iso = _iso(env, name=f"Game{n}.iso")
        iso.with_suffix(".rvz").write_bytes(b"done before")
    _store_rules(env, {"shelf": RVZ_RULE})
    inspected = []
    real = rules_sweep._inspect_candidate

    def inspect_and_stop(image, *args):
        inspected.append(image.name)
        if len(inspected) == 1:
            os.kill(os.getpid(), signal.SIGTERM)
            import time

            time.sleep(0.3)  # the signal handler runs on the loop meanwhile
        return real(image, *args)

    monkeypatch.setattr(rules_sweep, "_inspect_candidate", inspect_and_stop)

    code, _lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 1
    assert inspected == ["Game0.iso"]
    assert "last_run_at" not in _rule_state(env).get("gc", {}), "a cut-short pass is not a run"


def test_a_stop_while_delete_snapshots_are_read_queues_nothing(
    env, dolphin, capsys, monkeypatch,
):
    """codex 4577047502: a delete-on-verify batch reads one snapshot per
    Source before it queues; a stop during that must queue none of it."""
    import os
    import signal
    import time

    from services.rules import sweep as rules_sweep

    for n in range(5):
        _iso(env, name=f"Game{n}.iso")
    _store_rules(env, {"shelf": {"gc": {
        "mode": "dolphin_rvz", "enabled": True, "delete_on_verify": True,
        "check_level": "container_only",
    }}})
    read = []
    real = rules_sweep.build_delete_snapshot

    def snapshot_and_stop(path, *args, **kwargs):
        read.append(Path(path).name)
        if len(read) == 1:
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.3)  # the signal handler runs on the loop meanwhile
        return real(path, *args, **kwargs)

    monkeypatch.setattr(rules_sweep, "build_delete_snapshot", snapshot_and_stop)
    started = time.monotonic()

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 1
    assert time.monotonic() - started < 10
    assert read == ["Game0.iso"], "no Source probed after the stop"
    assert [line for line in lines if line.get("path")] == [], "nothing queued"
    assert dolphin["runs"] == []
    assert "last_run_at" not in _rule_state(env).get("gc", {}), "the clock did not move"


def test_the_fixture_keeps_every_lock_file_in_the_test_lock_dir(env):
    """Otherwise the queue's holder file points at the real lock dir, which
    does not exist on a clean machine, and the first job never starts."""
    from services.concurrency_manager import concurrency_manager

    assert concurrency_manager._holder_path.startswith(str(env["locks"]))
    assert concurrency_manager._holder_handle is None


@pytest.mark.parametrize("waiting", [True, False])
def test_the_healthcheck_counts_a_waiting_server_as_healthy(env, waiting):
    """Nothing listens on 8080 while the server waits for the lock; the
    marker keeps Docker from calling it unhealthy."""
    import os

    from services import scheduler_lock

    if waiting:
        env["locks"].mkdir(parents=True, exist_ok=True)
        Path(scheduler_lock.waiting_path()).write_text("{}")
    run_env = {k: v for k, v in os.environ.items() if not k.startswith("CHD_")}
    run_env.update(CHD_DATA_DIR=str(env["config"]), CHD_MODE="webui")

    done = subprocess.run(
        ["sh", "-c", _healthcheck_script()], env=run_env, capture_output=True, timeout=30,
        check=False,
    )

    assert (done.returncode == 0) is waiting, done.stderr


_FIRST_SCHEDULER = """
import sys
from config import settings
from services import db, scheduler_lock

lock = scheduler_lock.acquire("server")
db.init_app_database(settings)  # creates the data dir that did not exist
print("held", scheduler_lock.lock_path(), flush=True)
sys.stdin.readline()
lock.release()
print("released", flush=True)
"""

_SECOND_SCHEDULER = """
import asyncio
from services import scheduler_lock

def waiting(held):
    print("waiting", held.holder.get("by"), flush=True)

async def main():
    lock = await scheduler_lock.acquire_waiting("server", on_wait=waiting)
    print("acquired", scheduler_lock.lock_path(), flush=True)
    lock.release()

asyncio.run(main())
"""


def test_schedulers_before_and_after_db_init_share_one_lock(tmp_path):
    """Fresh install (codex 4576813495): the data dir does not exist when the
    first scheduler starts; its database init creates it. A scheduler started
    after that must contend for the same lock file and wait, not take another."""
    import os

    data_dir = tmp_path / "fresh" / "config"
    (tmp_path / "tmp").mkdir()
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CHD_", "COMPRESSATORIUM_"))}
    env.update(
        CHD_DATA_DIR=str(data_dir), TMPDIR=str(tmp_path / "tmp"),
        COMPRESSATORIUM_VOLUMES=str(tmp_path),
        PYTHONPATH=str(Path(__file__).resolve().parent.parent / "app"),
    )

    def start(script, **kwargs):
        return subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(script)], env=env, text=True,
            stdout=subprocess.PIPE, **kwargs,
        )

    first = start(_FIRST_SCHEDULER, stdin=subprocess.PIPE)
    second = None
    try:
        word, first_lock = first.stdout.readline().split()
        assert word == "held"
        assert data_dir.is_dir(), "database init made the data dir"

        second = start(_SECOND_SCHEDULER)
        assert second.stdout.readline().split() == ["waiting", "server"]
        assert second.poll() is None, "the second scheduler is still waiting"

        first.stdin.write("\n")
        first.stdin.flush()
        assert first.stdout.readline().strip() == "released"
        word, second_lock = second.stdout.readline().split()
        assert word == "acquired"
        assert second.wait(timeout=30) == 0
    finally:
        for proc in (first, second):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait()

    assert first_lock == second_lock == str(data_dir / "locks" / "scheduler.lock")
