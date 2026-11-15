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

    def configured(self):
        return True

    def canonical_platform_id(self, raw):
        return raw if raw == "gc" else None

    def platform_value(self, platform_id):
        return platform_id

    def platforms(self):
        from services.rules import Platform

        return [Platform(id="gc", slug="ngc", name="GameCube")]

    def images(self, platform_id):
        from services.rules import Image

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
    from services import libraries
    from services.concurrency_manager import concurrency_manager
    from services.job_manager import job_manager
    from services.lock_manager import lock_manager
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
    monkeypatch.setattr(lock_manager, "_lock_dir", str(locks))
    monkeypatch.setattr(lock_manager, "_ready", False)
    monkeypatch.setattr(concurrency_manager, "lock_dir", str(locks))
    monkeypatch.setattr(
        concurrency_manager, "_slot_paths", [str(locks / "convert_slot_0.lock")],
    )
    monkeypatch.setattr(concurrency_manager, "_ticket_counter_path", str(locks / "queue_counter"))
    monkeypatch.setattr(concurrency_manager, "_ready", False)
    # A CLI process starts with an empty queue.
    monkeypatch.setattr(job_manager, "jobs", OrderedDict())
    monkeypatch.setattr(job_manager, "_queue", asyncio.PriorityQueue())
    romm_settings.reset_for_tests()

    shelves = {
        "shelf": _Shelf("shelf", vol / "shelf"),
        "attic": _Shelf("attic", vol / "attic"),
    }
    for shelf in shelves.values():
        (shelf.root / "gc").mkdir(parents=True)
    monkeypatch.setattr(libraries, "all_libraries", lambda: list(shelves.values()))
    yield {"vol": vol, "config": config, "locks": locks, **shelves}
    romm_settings.reset_for_tests()
    reset_db_engine()


@pytest.fixture(name="dolphin")
def _dolphin(monkeypatch):
    service = registry.get("dolphin")._service
    state = {"fail": False, "runs": []}

    async def fake_run(cmd, *, input_path, output_path, **kwargs):
        state["runs"].append((input_path, output_path))
        if state["fail"]:
            raise RuntimeError("dolphin-tool failed with return code 1.")
        Path(output_path).write_bytes(b"converted output")
        yield {"progress": 100, "message": "Conversion complete"}

    monkeypatch.setattr(service._runner, "run", fake_run)
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


def test_a_second_server_on_the_same_data_dir_refuses_to_start(env, monkeypatch):
    import main
    from services import scheduler_lock

    # The app's log handlers would outlive the test.
    monkeypatch.setattr(main, "configure_logging", lambda: None)
    first = scheduler_lock.acquire("server")

    async def start():
        async with main.lifespan(main.app):
            pass

    try:
        with pytest.raises(RuntimeError) as refused:
            asyncio.run(start())
    finally:
        first.release()

    assert "in use by the server" in str(refused.value)
    # Refused before it touched anything: no database was created.
    assert not (env["config"] / "compressatorium.db").exists()
