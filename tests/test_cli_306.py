"""`compressatorium convert` and `compressatorium verify` (#306).

Each test calls the CLI's ``main()`` against a temp Volume. dolphin-tool is
stubbed at the ``SubprocessRunner`` seam (``run`` writes the output,
``run_verify`` answers the container check, ``run_capture`` answers the
Round-trip digest), so the CLI drives the real planner, job manager, delete
check and Ledger.
"""
from __future__ import annotations

import hashlib
import json
import os
import zipfile
from pathlib import Path

import pytest
from services.tools import registry


@pytest.fixture(name="volume")
def _volume(tmp_path, monkeypatch, reset_db_engine):
    from config import settings

    vol = tmp_path / "vol"
    vol.mkdir()
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setattr(settings, "chd_volumes", str(vol))
    monkeypatch.setattr(settings, "data_mount_root", str(vol))
    monkeypatch.setattr(settings, "data_dir", str(config))
    monkeypatch.setattr(settings, "db_path", str(config / "compressatorium.db"))
    monkeypatch.setattr(settings, "temp_dir", str(tmp_path / "temp"))
    monkeypatch.setattr(settings, "max_queue_depth", 0)
    # Other tests leave jobs queued on the shared job manager; a CLI process
    # starts with an empty one.
    import asyncio
    from collections import OrderedDict

    from services.job_manager import job_manager

    monkeypatch.setattr(job_manager, "jobs", OrderedDict())
    monkeypatch.setattr(job_manager, "_queue", asyncio.PriorityQueue())
    yield vol
    reset_db_engine()


@pytest.fixture(name="dolphin")
def _dolphin(monkeypatch):
    service = registry.get("dolphin")._service
    state = {"fail": False, "valid": True, "digest": None, "captures": [], "runs": []}

    async def fake_run(cmd, *, input_path, output_path, **kwargs):
        state["runs"].append((input_path, output_path))
        if state["fail"]:
            raise RuntimeError("dolphin-tool failed with return code 1.")
        Path(output_path).write_bytes(b"converted output")
        yield {"progress": 100, "message": "Conversion complete"}

    async def fake_verify(cmd, *, path, **kwargs):
        yield {
            "type": "complete",
            "valid": state["valid"],
            "message": "Disc image is Intact" if state["valid"] else "Disc is damaged",
        }

    async def fake_capture(cmd, **kwargs):
        state["captures"].append(cmd)
        return 0, f"{state['digest']}\n".encode(), b""

    monkeypatch.setattr(service._runner, "run", fake_run)
    monkeypatch.setattr(service._runner, "run_verify", fake_verify)
    monkeypatch.setattr(service._runner, "run_capture", fake_capture)
    return state


def _cli(capsys, *argv):
    import cli

    code = cli.main(list(argv))
    out = capsys.readouterr().out
    return code, [json.loads(line) for line in out.splitlines() if line.strip()]


def _iso(volume, rel="gc/Game.iso", payload=b"the disc" * 512):
    path = volume / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _ledger_rows():
    from services import db as _db
    from services.ledger import _recent_sync

    if _db.SessionLocal is None:
        return []
    return _recent_sync(100)


# --------------------------------------------------------------------------- #
# convert
# --------------------------------------------------------------------------- #


def test_convert_recurses_and_writes_one_json_line_per_image(volume, dolphin, capsys):
    iso = _iso(volume, "gc/sub/Game.iso")

    code, lines = _cli(capsys, "convert", str(volume), "--to", "rvz", "--json")

    assert code == 0
    assert len(lines) == 1
    line = lines[0]
    assert line["path"] == str(iso)
    assert line["status"] == "completed"
    assert line["mode"] == "dolphin_rvz"
    assert line["platform"] == "ngc"
    assert line["output"] == str(iso.with_suffix(".rvz"))
    assert line["source_deleted"] is False
    assert iso.with_suffix(".rvz").read_bytes() == b"converted output"
    assert iso.exists()
    rows = _ledger_rows()
    assert [r["source_path"] for r in rows] == [str(iso)]
    assert rows[0]["status"] == "completed"


def test_delete_source_uses_the_round_trip_check(volume, dolphin, capsys):
    payload = b"the disc" * 512
    iso = _iso(volume, payload=payload)
    dolphin["digest"] = hashlib.sha1(payload).hexdigest()

    code, lines = _cli(
        capsys, "convert", str(volume), "--to", "rvz", "--delete-source", "--json",
    )

    assert code == 0, lines
    assert lines[0]["status"] == "completed"
    assert lines[0]["check"] == "round_tripped"
    assert lines[0]["source_deleted"] is True
    assert not iso.exists()
    assert dolphin["captures"], "the output's content digest was read"
    assert _ledger_rows()[0]["check_passed"] == "round_tripped"


def test_container_only_opts_out_of_the_round_trip(volume, dolphin, capsys):
    iso = _iso(volume)

    code, lines = _cli(
        capsys, "convert", str(volume), "--to", "rvz",
        "--delete-source", "--container-only", "--json",
    )

    assert code == 0, lines
    assert lines[0]["check"] == "intact"
    assert not iso.exists()
    assert dolphin["captures"] == []


def test_a_round_trip_mismatch_keeps_the_source_and_exits_1(volume, dolphin, capsys):
    iso = _iso(volume)
    dolphin["digest"] = "0" * 40

    code, lines = _cli(
        capsys, "convert", str(volume), "--to", "rvz", "--delete-source", "--json",
    )

    assert code == 1
    assert lines[0]["status"] == "failed"
    assert "Source kept" in lines[0]["reason"]
    assert iso.exists()


def test_a_failed_conversion_exits_1(volume, dolphin, capsys):
    _iso(volume)
    dolphin["fail"] = True

    code, lines = _cli(capsys, "convert", str(volume), "--to", "rvz", "--json")

    assert code == 1
    assert lines[0]["status"] == "failed"
    assert "dolphin-tool failed" in lines[0]["reason"]
    assert [r["status"] for r in _ledger_rows()] == ["failed"]


def test_dry_run_plans_and_changes_nothing(volume, dolphin, capsys):
    from config import settings

    iso = _iso(volume)

    code, lines = _cli(
        capsys, "convert", str(volume), "--to", "rvz", "--delete-source",
        "--dry-run", "--json",
    )

    assert code == 0
    assert lines == [{
        "path": str(iso), "status": "planned", "platform": "ngc",
        "mode": "dolphin_rvz", "output": str(iso.with_suffix(".rvz")),
        "check": None, "source_deleted": False, "reason": None,
        "delete_source": True, "detection": "folder", "compression": None,
    }]
    assert dolphin["runs"] == []
    assert sorted(p.name for p in iso.parent.iterdir()) == ["Game.iso"]
    assert not Path(settings.db_path).exists()


def test_nothing_to_do_exits_0(volume, dolphin, capsys):
    (volume / "notes.txt").write_text("hello")

    code, lines = _cli(capsys, "convert", str(volume), "--to", "rvz", "--json")

    assert code == 0
    assert lines == []


def test_an_image_already_in_the_format_is_reported_and_left_alone(
    volume, dolphin, capsys,
):
    rvz = volume / "gc" / "Game.rvz"
    rvz.parent.mkdir()
    rvz.write_bytes(b"rvz")

    code, lines = _cli(capsys, "convert", str(volume), "--to", "rvz", "--json")

    assert code == 0
    assert lines[0]["status"] == "skipped"
    assert lines[0]["reason"] == "Already RVZ"
    assert dolphin["runs"] == []


def test_auto_skips_and_reports_unsorted_images(volume, dolphin, capsys):
    gc = _iso(volume, "gc/Game.iso")
    loose = _iso(volume, "misc/Loose.iso")

    code, lines = _cli(capsys, "convert", str(volume), "--auto", "--json")

    assert code == 0, lines
    by_path = {line["path"]: line for line in lines}
    assert by_path[str(gc)]["status"] == "completed"
    assert by_path[str(gc)]["mode"] == "dolphin_rvz"
    assert by_path[str(loose)]["status"] == "skipped"
    assert by_path[str(loose)]["reason"].startswith("Unsorted")
    assert not loose.with_suffix(".rvz").exists()


def test_converts_from_inside_an_archive(volume, dolphin, capsys):
    gc = volume / "gc"
    gc.mkdir()
    with zipfile.ZipFile(gc / "pack.zip", "w") as zf:
        zf.writestr("Game.iso", b"the disc" * 512)

    code, lines = _cli(capsys, "convert", str(volume), "--to", "rvz", "--json")

    assert code == 0, lines
    assert lines[0]["path"] == f"{gc / 'pack.zip'}::Game.iso"
    assert lines[0]["status"] == "completed"
    assert (gc / "Game.rvz").exists()
    # The job's extracted copy is cleaned up before the CLI exits.
    from config import settings

    temp = Path(settings.temp_dir)
    assert not temp.exists() or list(temp.iterdir()) == []


def test_an_output_another_process_is_writing_is_left_alone(volume, dolphin, capsys):
    """The server's output lock is a file lock in the shared lock directory;
    a second LockManager stands in for the server process."""
    from services.lock_manager import LockManager

    iso = _iso(volume)
    server = LockManager()
    assert server.acquire_lock(str(iso.with_suffix(".rvz")))
    try:
        code, lines = _cli(capsys, "convert", str(volume), "--to", "rvz", "--json")
    finally:
        server.release_lock(str(iso.with_suffix(".rvz")))

    assert code == 0
    assert lines[0]["status"] == "skipped"
    assert lines[0]["reason"] == "Output file is currently being converted"
    assert dolphin["runs"] == []


@pytest.mark.parametrize("argv", [
    ("convert",),
    ("convert", "{vol}"),
    ("convert", "{vol}", "--to", "rvz", "--auto"),
    ("convert", "{vol}", "--to", "bogus"),
    ("convert", "{vol}", "--to", "rvz", "--container-only"),
    ("convert", "{vol}/missing.iso", "--to", "rvz"),
    ("convert", "/", "--to", "rvz"),
    ("convert", "{vol}", "--to", "rvz", "--output-dir", "/"),
    ("convert", "{vol}", "--to", "rvz", "--compression", "not a codec!"),
    ("verify", "{vol}", "--since", "soon"),
    ("frobnicate",),
])
def test_bad_arguments_exit_2(volume, dolphin, capsys, argv):
    import cli

    code = cli.main([a.replace("{vol}", str(volume)) for a in argv])

    assert code == 2
    assert capsys.readouterr().out == ""


ALIAS_FLAGS = (
    "--no-recurse", "--no-archives", "--any-platform", "--compression", "default",
)


def _entrypoint_cli_branch() -> str:
    entrypoint = (Path(__file__).resolve().parents[1] / "entrypoint.sh").read_text()
    return entrypoint.split('if [[ "$MODE" == "cli" ]]; then', 1)[1].split("\nelse\n", 1)[0]


def test_chd_mode_cli_alias_runs_the_convert_command_with_todays_scope():
    branch = _entrypoint_cli_branch()
    assert (
        'compressatorium convert "${existing[@]}" --to "$CHDMAN_MODE" '
        + " ".join(ALIAS_FLAGS)
    ) in branch
    assert "chdman createcd" not in branch


GC_HEADER = b"\0" * 0x1C + bytes.fromhex("C2339F3D") + b"\0" * 64


def _alias_tree(volume):
    for rel in ("A.cue", "A.bin", "B.iso", "C.gdi", "sub/D.cue", "gc/F.iso", "Done.rvz"):
        path = volume / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 64)
    (volume / "G.iso").write_bytes(GC_HEADER)  # a GameCube disc at the top level
    with zipfile.ZipFile(volume / "pack.zip", "w") as zf:
        zf.writestr("E.cue", b"x")


@pytest.mark.parametrize("mode", ["createcd", "createdvd"])
def test_chd_mode_cli_alias_keeps_todays_scope(volume, dolphin, capsys, mode):
    """Today's loop: the top level of each Volume, *.gdi *.iso *.cue, every one
    to chdman $CHDMAN_MODE, no archives, no Platform check, chdman's own
    compression. Files the command cannot read at all (createdvd and a .cue)
    are left alone rather than failed."""
    _alias_tree(volume)

    code, lines = _cli(
        capsys, "convert", str(volume), "--to", mode, *ALIAS_FLAGS, "--dry-run", "--json",
    )

    assert code == 0, lines
    planned = sorted(Path(line["path"]).name for line in lines if line["status"] == "planned")
    expected = ["A.cue", "B.iso", "C.gdi", "G.iso"] if mode == "createcd" else ["B.iso", "G.iso"]
    assert planned == expected
    assert {line["mode"] for line in lines if line["status"] == "planned"} == {mode}
    assert all(line["compression"] is None for line in lines)
    assert [line for line in lines if line["status"] != "planned"] == []


def test_compression_default_ignores_the_saved_choice(volume, dolphin, capsys):
    import asyncio

    from routes.preferences import CONVERSION_KEY
    from services.preferences_store import preferences_store

    (volume / "empty").mkdir()
    _cli(capsys, "convert", str(volume / "empty"), "--to", "chd")  # creates the DB
    asyncio.run(preferences_store.put(CONVERSION_KEY, {"chdman": "zstd"}))
    (volume / "ps2").mkdir()
    (volume / "ps2" / "Game.iso").write_bytes(b"x" * 64)

    _, saved = _cli(capsys, "convert", str(volume), "--to", "chd", "--dry-run", "--json")
    _, plain = _cli(
        capsys, "convert", str(volume), "--to", "chd", "--compression", "default",
        "--dry-run", "--json",
    )

    assert saved[0]["compression"] == "zstd"
    assert plain[0]["compression"] is None


def test_to_skips_a_conflicted_image_with_its_reason(volume, dolphin, capsys):
    disc = volume / "ps2" / "Game.iso"
    disc.parent.mkdir()
    disc.write_bytes(GC_HEADER)

    code, lines = _cli(capsys, "convert", str(volume), "--to", "chd", "--json")

    assert code == 0
    assert lines[0]["status"] == "skipped"
    assert lines[0]["detection"] == "conflict"
    assert lines[0]["platform"] is None
    assert lines[0]["reason"] == (
        "Platform conflict: in the ps2 folder, but the header says ngc"
    )


def test_to_reports_the_platform_it_worked_out(volume, dolphin, capsys):
    disc = volume / "misc" / "Game.iso"
    disc.parent.mkdir()
    disc.write_bytes(GC_HEADER)

    code, lines = _cli(capsys, "convert", str(volume), "--to", "rvz", "--json")

    assert code == 0
    assert (lines[0]["platform"], lines[0]["detection"]) == ("ngc", "header")


def test_to_7z_compresses_roms_per_platform(volume, dolphin, capsys):
    for rel in ("gba/A.gba", "snes/B.sfc"):
        path = volume / rel
        path.parent.mkdir(parents=True)
        path.write_bytes(b"rom")

    code, lines = _cli(capsys, "convert", str(volume), "--to", "7z", "--dry-run", "--json")

    assert code == 0
    assert sorted((Path(line["path"]).name, line["mode"]) for line in lines) == [
        ("A.gba", "romz_7z"), ("B.sfc", "romz_7z"),
    ]


def test_one_bad_image_does_not_sink_the_others(volume, dolphin, capsys, monkeypatch):
    from routes import convert as convert_routes

    good, bad = _iso(volume, "gc/Good.iso"), _iso(volume, "gc/Bad.iso")
    dolphin["digest"] = hashlib.sha1(b"the disc" * 512).hexdigest()
    real = convert_routes.build_delete_snapshot

    def snapshot(path):
        if path == str(bad):
            raise ValueError("a companion file is a symlink")
        return real(path)

    monkeypatch.setattr(convert_routes, "build_delete_snapshot", snapshot)

    code, lines = _cli(
        capsys, "convert", str(volume), "--to", "rvz", "--delete-source", "--json",
    )

    by_path = {line["path"]: line for line in lines}
    assert code == 1
    assert by_path[str(bad)]["status"] == "failed"
    assert "symlink" in by_path[str(bad)]["reason"]
    assert by_path[str(good)]["status"] == "completed"
    assert not good.exists() and bad.exists()


def test_jobs_are_fed_below_the_queue_cap(volume, dolphin, capsys, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "max_queue_depth", 1)
    isos = [_iso(volume, f"gc/G{i}.iso") for i in range(3)]

    code, lines = _cli(capsys, "convert", str(volume), "--to", "rvz", "--json")

    assert code == 0, lines
    assert [line["status"] for line in lines] == ["completed"] * 3
    assert all(iso.with_suffix(".rvz").exists() for iso in isos)


def test_the_wrapper_drops_root_through_the_entrypoint():
    root = Path(__file__).resolve().parents[1]
    wrapper = (root / "scripts" / "compressatorium").read_text()
    entrypoint = (root / "entrypoint.sh").read_text()

    assert 'exec /entrypoint.sh compressatorium "$@"' in wrapper
    # The entrypoint runs the command right after its PUID/PGID remap and
    # privilege drop, before anything that prints to stdout.
    drop = entrypoint.index('exec /usr/sbin/gosu converter "$0" "$@"')
    dispatch = entrypoint.index('exec /usr/local/bin/compressatorium "$@"')
    banner = entrypoint.index("WHOOPS")
    assert drop < dispatch < banner


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #


def _rvz(volume, rel="gc/Game.rvz"):
    path = volume / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"rvz")
    return path


def test_verify_checks_each_image_and_stores_the_result(volume, dolphin, capsys):
    rvz = _rvz(volume, "gc/deep/Game.rvz")

    code, lines = _cli(capsys, "verify", str(volume), "--json")

    assert code == 0
    assert lines == [{
        "path": str(rvz), "status": "intact", "tool": "dolphin",
        "reason": "Disc image is Intact",
    }]

    code, lines = _cli(capsys, "verify", str(volume), "--since", "30d", "--json")

    assert code == 0
    assert lines[0]["status"] == "skipped"
    assert lines[0]["reason"].startswith("Checked ")


def test_verify_since_rechecks_images_checked_before_the_window(volume, dolphin, capsys):
    import asyncio

    from services import db as _db
    from services.verification_store import verification_store

    rvz = _rvz(volume)
    _cli(capsys, "verify", str(volume), "--json")
    with _db.SessionLocal() as session:
        row = session.get(_db.Verification, str(rvz.resolve()))
        row.verified_at = "2020-01-01T00:00:00Z"
        session.commit()
    assert asyncio.run(verification_store.get_record(str(rvz)))

    code, lines = _cli(capsys, "verify", str(volume), "--since", "30d", "--json")

    assert code == 0
    assert lines[0]["status"] == "intact"


def test_a_failed_check_exits_1_and_drops_the_stored_result(volume, dolphin, capsys):
    import asyncio

    from services.verification_store import verification_store

    rvz = _rvz(volume)
    _cli(capsys, "verify", str(volume), "--json")
    dolphin["valid"] = False

    code, lines = _cli(capsys, "verify", str(volume), "--json")

    assert code == 1
    assert lines[0]["status"] == "failed"
    assert lines[0]["reason"] == "Disc is damaged"
    assert asyncio.run(verification_store.get_record(str(rvz))) is None


def test_verify_dry_run_checks_nothing(volume, dolphin, capsys):
    rvz = _rvz(volume)
    dolphin["valid"] = False

    code, lines = _cli(capsys, "verify", str(volume), "--dry-run", "--json")

    assert code == 0
    assert lines == [{"path": str(rvz), "status": "planned", "tool": "dolphin",
                      "reason": None}]


@pytest.mark.parametrize("target", [("--to", "rvz"), ("--auto",)])
def test_a_tool_that_cannot_run_here_is_skipped_with_its_reason(
    volume, dolphin, capsys, monkeypatch, target,
):
    tool = registry.get("dolphin")

    async def not_ready(self):
        return False

    # On the class: patching the instance leaves a bound method behind on undo,
    # which would shadow later class-level patches.
    monkeypatch.setattr(type(tool), "is_ready", not_ready)
    _iso(volume)

    code, lines = _cli(capsys, "convert", str(volume), *target, "--json")

    assert code == 0
    assert lines[0]["status"] == "skipped"
    assert lines[0]["reason"].startswith(f"{tool.display_name} isn't ready")
    assert dolphin["runs"] == []


def test_a_check_that_errors_keeps_the_stored_result(volume, dolphin, capsys, monkeypatch):
    import asyncio

    from services.verification_store import verification_store

    rvz = _rvz(volume)
    _cli(capsys, "verify", str(volume), "--json")
    service = registry.get("dolphin")._service

    async def broken(cmd, *, path, **kwargs):
        raise OSError("dolphin-tool: No such file or directory")
        yield  # pragma: no cover

    monkeypatch.setattr(service._runner, "run_verify", broken)

    code, lines = _cli(capsys, "verify", str(volume), "--json")

    assert code == 1
    assert lines[0]["status"] == "failed"
    assert "No such file" in lines[0]["reason"]
    assert asyncio.run(verification_store.get_record(str(rvz))) is not None


def test_a_stalled_check_keeps_the_stored_result(volume, dolphin, capsys, monkeypatch):
    """The runner's own stall bound is no verdict on the file."""
    import asyncio

    from services.subprocess_runner import SubprocessRunner
    from services.verification_store import verification_store

    rvz = _rvz(volume)
    _cli(capsys, "verify", str(volume), "--json")
    service = registry.get("dolphin")._service

    async def stalls(cmd, *, path, **kwargs):
        yield SubprocessRunner._verify_error(
            "Verification stalled: no output for 300s", inconclusive=True,
        )

    monkeypatch.setattr(service._runner, "run_verify", stalls)

    code, lines = _cli(capsys, "verify", str(volume), "--json")

    assert code == 1
    assert lines[0]["status"] == "failed"
    assert "stalled" in lines[0]["reason"]
    assert asyncio.run(verification_store.get_record(str(rvz))) is not None


def test_entrypoint_and_wrapper_diagnostics_go_to_stderr():
    """stdout carries --json lines once the wrapper goes through the entrypoint
    (PGID=100 on Debian prints "GID ... already exists" on every one-off run)."""
    import re

    root = Path(__file__).resolve().parents[1]
    for name in ("entrypoint.sh", "scripts/compressatorium"):
        text = (root / name).read_text()
        body = re.sub(r"discover_volumes\(\) \{.*?\n\}\n", "", text, flags=re.S)
        for number, line in enumerate(body.splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.search(r"(^|[;&|]\s*|\s)(echo|printf)\s", code) and ">&2" not in code:
                if re.search(r'echo "\$mount_opts" \|', code):
                    continue  # piped into grep, not printed
                raise AssertionError(f"{name}:{number} prints to stdout: {line.strip()}")


def _point_locks_at(monkeypatch, lock_dir):
    """Aim the process-wide lock managers at ``lock_dir``, not yet set up."""
    from services.concurrency_manager import concurrency_manager
    from services.lock_manager import lock_manager

    monkeypatch.setattr(lock_manager, "_lock_dir", str(lock_dir))
    monkeypatch.setattr(lock_manager, "_ready", False)
    monkeypatch.setattr(concurrency_manager, "lock_dir", str(lock_dir))
    monkeypatch.setattr(
        concurrency_manager, "_slot_paths", [str(lock_dir / "convert_slot_0.lock")],
    )
    monkeypatch.setattr(
        concurrency_manager, "_ticket_counter_path", str(lock_dir / "queue_counter"),
    )
    monkeypatch.setattr(concurrency_manager, "_ready", False)
    monkeypatch.setattr(
        concurrency_manager, "_holder_path",
        str(lock_dir / f"holder_{concurrency_manager._holder_id}.lock"),
    )
    monkeypatch.setattr(concurrency_manager, "_holder_handle", None)


def test_dry_run_creates_no_lock_dir(volume, dolphin, capsys, monkeypatch, tmp_path):
    lock_dir = tmp_path / "config" / "locks"
    _point_locks_at(monkeypatch, lock_dir)
    _iso(volume)

    code, lines = _cli(capsys, "convert", str(volume), "--to", "rvz", "--dry-run", "--json")
    assert (code, lines[0]["status"]) == (0, "planned")
    code, _ = _cli(capsys, "verify", str(volume), "--dry-run", "--json")

    assert code == 0
    assert not lock_dir.exists()


def test_dry_run_leaves_a_running_servers_lock_files_alone(
    volume, dolphin, capsys, monkeypatch, tmp_path,
):
    """No ticket pruned, no stale lock file removed, no lock probed or taken."""
    from services.lock_manager import LockManager

    lock_dir = tmp_path / "shared-locks"
    lock_dir.mkdir()
    _point_locks_at(monkeypatch, lock_dir)
    iso = _iso(volume)
    # Left by others: a ticket nobody holds, and a stale output lock file.
    (lock_dir / "queue_3_1_serverjob.ticket").write_text("{}")
    stale = LockManager()
    stale._lock_dir = str(lock_dir)
    (lock_dir / Path(stale._lock_file_path(str(iso.with_suffix(".rvz")))).name).write_bytes(b"")
    before = sorted(p.name for p in lock_dir.iterdir())

    code, lines = _cli(capsys, "convert", str(volume), "--to", "rvz", "--dry-run", "--json")

    assert code == 0
    assert sorted(p.name for p in lock_dir.iterdir()) == before
    # The lock file is there, so the dry run reports the output as taken.
    assert lines[0]["status"] == "skipped"


@pytest.mark.parametrize("signame", ["SIGHUP", "SIGTERM"])
def test_a_hangup_stops_the_run_like_sigterm(volume, monkeypatch, capsys, signame):
    """Closing the terminal of `docker exec -it ... compressatorium` sends SIGHUP."""
    import asyncio
    import os
    import signal

    import cli

    async def waits_forever(args, printer, state):
        os.kill(os.getpid(), getattr(signal, signame))
        await asyncio.sleep(30)
        return 0

    monkeypatch.setattr(cli, "convert", waits_forever)

    assert cli.main(["convert", str(volume), "--to", "rvz"]) == 1
    assert "stopped" in capsys.readouterr().err


def test_only_a_small_window_of_jobs_is_queued_at_once(volume, dolphin, capsys, monkeypatch):
    """Each queued job holds a ticket in the queue the server shares: a run over
    a whole library must not put every job ahead of the web UI's."""
    from routes import convert as convert_routes
    from services.job_manager import job_manager

    isos = [_iso(volume, f"gc/G{i}.iso") for i in range(5)]
    real = convert_routes.queue_prepared
    depths = []

    async def watching(batch):
        jobs = await real(batch)
        depths.append(job_manager.get_queue_depth())
        return jobs

    monkeypatch.setattr(convert_routes, "queue_prepared", watching)

    code, lines = _cli(capsys, "convert", str(volume), "--to", "rvz", "--json")

    assert code == 0, lines
    assert all(iso.with_suffix(".rvz").exists() for iso in isos)
    assert max(depths) <= 2, depths


def test_never_two_jobs_for_one_output_in_a_run(volume, dolphin, capsys, monkeypatch):
    """After a group refusal each Image is planned alone, so the planner's own
    "Same output as" collapse no longer sees both: Game.gcz and Game.iso both
    write Game.rvz. With --duplicate overwrite and --delete-source both used to
    finish and both Sources were deleted for one surviving output."""
    from routes import convert as convert_routes

    gc = volume / "gc"
    bad = _iso(volume, "gc/Bad.iso")
    for name in ("A.iso", "B.iso", "C.iso", "D.iso"):
        _iso(volume, f"gc/{name}")
    gcz = gc / "Game.gcz"
    _iso(volume, "gc/Game.iso")
    gcz.write_bytes(b"gcz")
    (gc / "Game.rvz").write_bytes(b"old output")
    real = convert_routes.build_delete_snapshot

    def snapshot(path):
        if path == str(bad):
            raise ValueError("a companion file is a symlink")
        return real(path)

    monkeypatch.setattr(convert_routes, "build_delete_snapshot", snapshot)

    code, lines = _cli(
        capsys, "convert", str(volume), "--to", "rvz", "--duplicate", "overwrite",
        "--delete-source", "--container-only", "--json",
    )

    by_name = {Path(line["path"]).name: line for line in lines}
    writers = [n for n in ("Game.gcz", "Game.iso") if by_name[n]["status"] == "completed"]
    assert len(writers) == 1, by_name
    loser = ({"Game.gcz", "Game.iso"} - set(writers)).pop()
    assert by_name[loser]["status"] == "skipped"
    assert by_name[loser]["reason"].startswith("Same output as")
    assert (gc / loser).exists()
    assert [p for p in dolphin["runs"] if p[1].endswith("Game.rvz")] == [
        (str(gc / writers[0]), str(gc / "Game.rvz")),
    ]
    assert code == 1  # Bad.iso


def test_point_locks_at_moves_the_holder_file_too(monkeypatch, tmp_path):
    import os

    from services.concurrency_manager import concurrency_manager

    lock_dir = tmp_path / "locks"
    _point_locks_at(monkeypatch, lock_dir)

    assert os.path.dirname(concurrency_manager._holder_path) == str(lock_dir)
    assert concurrency_manager._holder_handle is None


def test_verify_finds_images_that_only_a_check_reads(volume, dolphin, capsys):
    """A .chd is read by no create mode (chdman leaves it out of its inputs), so
    the convert search skipped it and `verify <dir>` checked nothing."""
    chd = volume / "ps2" / "deep" / "Game.chd"
    chd.parent.mkdir(parents=True)
    chd.write_bytes(b"chd")

    code, lines = _cli(capsys, "verify", str(volume), "--dry-run", "--json")

    assert code == 0
    assert lines == [{"path": str(chd), "status": "planned", "tool": "chdman",
                      "reason": None}]


def test_verify_stops_after_a_verifier_is_abandoned(volume, dolphin, capsys, monkeypatch):
    """A verifier that outlived SIGKILL still holds the storage: opening the
    next file on it would strand one more, so the run stops."""
    from services.subprocess_runner import abandoned_verify_error

    _rvz(volume, "gc/A.rvz")
    _rvz(volume, "gc/B.rvz")
    service = registry.get("dolphin")._service

    async def stuck(cmd, *, path, **kwargs):
        yield abandoned_verify_error(4242)

    monkeypatch.setattr(service._runner, "run_verify", stuck)
    import cli
    from services.subprocess_runner import BATCH_STOPPED_MESSAGE

    code = cli.main(["verify", str(volume), "--json"])
    captured = capsys.readouterr()
    lines = [json.loads(line) for line in captured.out.splitlines() if line.strip()]

    assert code == 1
    assert len(lines) == 1
    assert lines[0]["status"] == "failed"
    assert "could not be killed" in lines[0]["reason"]
    assert BATCH_STOPPED_MESSAGE in captured.err


def test_the_web_batch_verify_uses_the_same_stop_rule():
    from routes import info
    from services import subprocess_runner

    assert info.stops_verify_batch is subprocess_runner.stops_verify_batch
    assert info.BATCH_STOPPED_MESSAGE == subprocess_runner.BATCH_STOPPED_MESSAGE
    assert subprocess_runner.stops_verify_batch({"valid": False, "abandoned": True})
    assert not subprocess_runner.stops_verify_batch({"valid": False, "inconclusive": True})


def test_verify_skips_a_tool_that_cannot_run_here_and_keeps_its_check(
    volume, dolphin, capsys, monkeypatch,
):
    import asyncio

    from services.verification_store import verification_store

    rvz = _rvz(volume)
    _cli(capsys, "verify", str(volume), "--json")
    tool = registry.get("dolphin")

    async def not_ready(self):
        return False

    monkeypatch.setattr(type(tool), "verify_is_ready", not_ready)

    code, lines = _cli(capsys, "verify", str(volume), "--json")

    assert code == 0
    assert lines[0]["status"] == "skipped"
    assert lines[0]["reason"].startswith(f"{tool.display_name} isn't ready")
    assert asyncio.run(verification_store.get_record(str(rvz))) is not None


def test_the_shared_scan_lists_what_a_check_reads_for_verify(volume):
    from routes.files import scan_tree

    chd = volume / "ps2" / "Game.chd"
    chd.parent.mkdir(parents=True)
    chd.write_bytes(b"chd")

    convert_files, _, _ = scan_tree(str(volume), purpose="convert")
    verify_files, _, _ = scan_tree(str(volume), purpose="verify")

    assert str(chd) not in [f["path"] for f in convert_files]
    assert [f["path"] for f in verify_files] == [str(chd)]


def _not_ready(monkeypatch, tool, calls=None):
    async def not_ready(self):
        if calls is not None:
            calls.append(self.id)
        return False

    monkeypatch.setattr(type(tool), "verify_is_ready", not_ready)


def test_verify_probes_readiness_once_per_run(volume, dolphin, capsys, monkeypatch):
    calls = []
    for name in ("A", "B", "C"):
        _rvz(volume, f"gc/{name}.rvz")
    _not_ready(monkeypatch, registry.get("dolphin"), calls)

    _code, lines = _cli(capsys, "verify", str(volume), "--json")

    assert [line["status"] for line in lines] == ["skipped"] * 3
    assert calls.count("dolphin") == 1


def test_verify_dry_run_reports_readiness_too(volume, dolphin, capsys, monkeypatch):
    _rvz(volume)
    _not_ready(monkeypatch, registry.get("dolphin"))

    code, lines = _cli(capsys, "verify", str(volume), "--dry-run", "--json")

    assert code == 0
    assert lines[0]["status"] == "skipped"
    assert "isn't ready" in lines[0]["reason"]


def test_an_unready_tool_named_explicitly_exits_1(volume, dolphin, capsys, monkeypatch):
    rvz = _rvz(volume)
    _not_ready(monkeypatch, registry.get("dolphin"))

    code, lines = _cli(capsys, "verify", str(rvz), "--json")

    assert code == 1
    assert lines[0]["status"] == "skipped"


def test_verify_stops_on_the_abandonment_sink(volume, dolphin, capsys, monkeypatch):
    """A read the check abandoned is reported only to the sink; the run stops
    on it as the web batch does."""
    import json as json_module

    import cli
    from services.subprocess_runner import BATCH_STOPPED_MESSAGE, _note_abandoned

    _rvz(volume, "gc/A.rvz")
    _rvz(volume, "gc/B.rvz")
    service = registry.get("dolphin")._service

    async def leaves_a_read_behind(cmd, *, path, **kwargs):
        _note_abandoned("read of A.rvz")
        yield {"type": "error", "valid": False, "message": "timed out",
               "inconclusive": True}

    monkeypatch.setattr(service._runner, "run_verify", leaves_a_read_behind)

    code = cli.main(["verify", str(volume), "--json"])
    captured = capsys.readouterr()
    lines = [json_module.loads(x) for x in captured.out.splitlines() if x.strip()]

    assert code == 1
    assert len(lines) == 1
    assert BATCH_STOPPED_MESSAGE in captured.err



def test_verify_stops_when_its_own_deadline_abandons_a_read(
    volume, dolphin, capsys, monkeypatch,
):
    """The CLI's outer deadline cancels a check stuck on dead storage; the
    detached read it leaves behind is reported to the abandonment sink, not in
    any result, and the run must stop on it."""
    import asyncio
    import json as json_module

    import cli
    from services.subprocess_runner import BATCH_STOPPED_MESSAGE, _note_abandoned

    _rvz(volume, "gc/A.rvz")
    _rvz(volume, "gc/B.rvz")
    tool = registry.get("dolphin")

    async def stuck(self, path, *, cancel_event=None):
        try:
            await asyncio.sleep(30)
        finally:
            _note_abandoned(f"read of {path}")
        return {"valid": True, "message": "never"}

    async def tiny_bound(self, path, *, cancel_event=None):
        return 0.2

    monkeypatch.setattr(type(tool), "verify", stuck)
    monkeypatch.setattr(type(tool), "verify_timeout", tiny_bound)

    code = cli.main(["verify", str(volume), "--json"])
    captured = capsys.readouterr()
    lines = [json_module.loads(x) for x in captured.out.splitlines() if x.strip()]

    assert code == 1
    assert len(lines) == 1
    assert "timed out" in lines[0]["reason"]
    assert BATCH_STOPPED_MESSAGE in captured.err



def test_a_stop_during_queueing_cancels_the_jobs_it_returned(
    volume, dolphin, capsys, monkeypatch,
):
    """SIGTERM lands while queue_prepared is reserving destinations: the
    one-shot cancel-all ran before the jobs existed, so they must be cancelled
    after the queue call returns, before any runs (Source deletion included)."""
    import asyncio
    import os
    import signal

    from routes import convert as convert_routes

    iso = _iso(volume)
    real = convert_routes.queue_prepared

    async def signalled_mid_queue(batch):
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.sleep(0.05)  # the handler runs and its cancel-all finishes
        return await real(batch)

    monkeypatch.setattr(convert_routes, "queue_prepared", signalled_mid_queue)

    code, lines = _cli(
        capsys, "convert", str(volume), "--to", "rvz", "--delete-source",
        "--container-only", "--json",
    )

    assert code == 1
    assert lines[0]["status"] == "cancelled"
    assert iso.exists()
    assert dolphin["runs"] == []


def _romz_zip(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Game.gba", b"rom" * 64)
    return path


@pytest.mark.parametrize("named", [True, False])
def test_a_damaged_archive_with_a_stored_check_is_checked_and_fails(
    volume, dolphin, capsys, monkeypatch, named,
):
    """Once damaged, the romz content probe no longer claims the file; a stored
    check means it was an Image this app checked, so it is checked again."""
    import asyncio

    from services.verification_store import verification_store

    # 7z's own test of an intact archive, stubbed at the SubprocessRunner seam;
    # the damaged archive is refused before 7z would run.
    async def seven_zip_passes(cmd, *, path, success_message, **kwargs):
        yield {"type": "complete", "valid": True, "message": success_message}

    monkeypatch.setattr(registry.get("romz")._service._runner, "capture_verify",
                        seven_zip_passes)

    archive = _romz_zip(volume / "gba" / "Game.gba.zip")
    code, lines = _cli(capsys, "verify", str(archive), "--json")
    assert (code, lines[0]["status"]) == (0, "intact")

    data = archive.read_bytes()
    archive.write_bytes(data[: len(data) // 2])  # truncate the central directory

    target = archive if named else volume
    code, lines = _cli(capsys, "verify", str(target), "--json")

    assert code == 1
    assert lines[0]["path"] == str(archive)
    assert lines[0]["status"] == "failed"
    assert asyncio.run(verification_store.get_record(str(archive))) is None


def test_a_generic_iso_gets_the_verifier_for_its_platform(volume, dolphin, capsys):
    """A PS2 .iso is not a Dolphin disc: it is skipped as unsupported (not a
    failure), while a GameCube .iso still goes to Dolphin."""
    ps2 = volume / "ps2" / "Game.iso"
    ps2.parent.mkdir()
    ps2.write_bytes(b"x" * 4096)
    gc = _iso(volume, "gc/Game.iso")

    code, lines = _cli(capsys, "verify", str(volume), "--dry-run", "--json")

    by_path = {line["path"]: line for line in lines}
    assert code == 0
    assert by_path[str(gc)]["tool"] == "dolphin"
    assert by_path[str(gc)]["status"] == "planned"
    assert by_path[str(ps2)]["status"] == "skipped"
    assert "ps2" in by_path[str(ps2)]["reason"]



def _truncated_romz_zip(volume):
    archive = _romz_zip(volume / "gba" / "Game.gba.zip")
    data = archive.read_bytes()
    archive.write_bytes(data[: len(data) // 2])
    return archive


def test_a_truncated_rom_archive_named_directly_fails_without_a_stored_check(
    volume, dolphin, capsys,
):
    archive = _truncated_romz_zip(volume)

    code, lines = _cli(capsys, "verify", str(archive), "--json")

    assert code == 1
    assert lines[0]["status"] == "failed"
    assert lines[0]["tool"] == "romz"


def test_a_truncated_rom_archive_in_a_folder_is_counted_failed(volume, dolphin, capsys):
    archive = _truncated_romz_zip(volume)

    code, lines = _cli(capsys, "verify", str(volume), "--json")

    assert code == 1
    assert [(line["path"], line["status"]) for line in lines] == [(str(archive), "failed")]


def test_a_multi_rom_archive_is_still_not_a_rom_archive(volume, dolphin, capsys):
    archive = volume / "gba" / "Pack.zip"
    archive.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("A.gba", b"a" * 64)
        zf.writestr("B.gba", b"b" * 64)

    code, lines = _cli(capsys, "verify", str(archive), "--json")
    assert code == 0
    assert lines == [{"path": str(archive), "status": "skipped", "tool": None,
                      "reason": "No tool checks this file"}]

    code, lines = _cli(capsys, "verify", str(volume), "--json")
    assert (code, lines) == (0, [])



def test_a_z3ds_verifier_killed_by_a_signal_keeps_the_stored_pass(
    volume, dolphin, capsys, monkeypatch,
):
    """z3ds runs zstd itself, outside the shared runner: its signal death goes
    through the same killed_verify_error classification (codex 4576670747)."""
    import asyncio
    import shutil
    import struct

    from services.verification_store import verification_store

    payload = b"payload" * 64
    header = struct.pack("<4s4sBBHIQQ", b"Z3DS", b"NCSD", 1, 0, 0x20, 0, len(payload), 0)
    game = volume / "3ds" / "Game.z3ds"
    game.parent.mkdir(parents=True)
    game.write_bytes(header + payload)
    (volume / "empty").mkdir()
    _cli(capsys, "verify", str(volume / "empty"))  # creates the database
    asyncio.run(verification_store.mark_verified(str(game)))

    zstd = volume.parent / "bin" / "zstd"
    zstd.parent.mkdir()
    zstd.write_text("#!/bin/sh\nkill -KILL $$\n")
    zstd.chmod(0o755)
    real_which = shutil.which
    monkeypatch.setattr(
        shutil, "which", lambda name, *a, **k: str(zstd) if name == "zstd" else real_which(name),
    )

    code, lines = _cli(capsys, "verify", str(game), "--json")

    assert code == 1
    assert lines[0]["status"] == "failed"
    assert "signal" in lines[0]["reason"]
    assert asyncio.run(verification_store.get_record(str(game))) is not None


def test_a_cli_run_imports_the_legacy_verification_store_first(
    volume, dolphin, capsys,
):
    """The first CLI write must not leave verified_chds.json behind forever: the
    CLI runs the same legacy import the web startup does (codex 4576670759)."""
    import asyncio
    import json as json_module

    from services.verification_store import verification_store

    from config import settings

    legacy_path = str((volume / "old" / "Game.chd").resolve())
    legacy = Path(settings.data_dir) / "verified_chds.json"
    legacy.write_text(json_module.dumps({
        legacy_path: {
            "chd_path": legacy_path, "source_path": None,
            "verified_at": "2026-01-01T00:00:00Z",
        },
    }))
    rvz = _rvz(volume)

    code, _ = _cli(capsys, "verify", str(rvz), "--json")

    assert code == 0
    record = asyncio.run(verification_store.get_record(legacy_path))
    assert record is not None and record["verified_at"] == "2026-01-01T00:00:00Z"
    assert not legacy.exists()


def test_a_dry_run_leaves_the_legacy_store_alone(volume, dolphin, capsys):
    import json as json_module

    from config import settings

    legacy = Path(settings.data_dir) / "verified_chds.json"
    legacy.write_text(json_module.dumps({}))
    rvz = _rvz(volume)

    code, _ = _cli(capsys, "verify", str(rvz), "--dry-run", "--json")

    assert code == 0
    assert legacy.exists()
    assert not Path(settings.db_path).exists()



# --------------------------------------------------------------------------- #
# Review r7: what romz claims, stored-check routing, unknown Platforms
# --------------------------------------------------------------------------- #


def _stub_7z_passes(monkeypatch):
    calls = []

    async def seven_zip_passes(cmd, *, path, success_message, **kwargs):
        calls.append(path)
        yield {"type": "complete", "valid": True, "message": success_message}

    monkeypatch.setattr(registry.get("romz")._service._runner, "capture_verify",
                        seven_zip_passes)
    return calls


def test_healthy_archives_that_are_not_rom_archives_are_not_checked(
    volume, dolphin, capsys, monkeypatch,
):
    """r7 I1: a big texture pack, a zip with a symlink, a zip with a `..` member
    are plainly not single-ROM archives: skipped, never failed."""
    _stub_7z_passes(monkeypatch)
    misc = volume / "misc"
    misc.mkdir()
    with zipfile.ZipFile(misc / "Textures.zip", "w") as zf:
        for i in range(5001):
            zf.writestr(f"t/{i}.png", b"x")
    with zipfile.ZipFile(misc / "Tools.zip", "w") as zf:
        zf.writestr("readme.txt", b"x")
        link = zipfile.ZipInfo("link")
        link.external_attr = 0o120777 << 16
        zf.writestr(link, "readme.txt")
        zf.writestr("bin/tool", b"y")
    with zipfile.ZipFile(misc / "Patch.zip", "w") as zf:
        zf.writestr("../escape.txt", b"x")
        zf.writestr("other.txt", b"y")

    code, lines = _cli(capsys, "verify", str(volume), "--json")
    assert (code, lines) == (0, [])
    for name in ("Textures.zip", "Tools.zip", "Patch.zip"):
        code, lines = _cli(capsys, "verify", str(misc / name), "--json")
        assert code == 0, name
        assert lines[0]["status"] == "skipped", name


def test_a_7z_with_an_encrypted_header_is_not_a_rom_archive(
    volume, dolphin, capsys, monkeypatch,
):
    py7zr = pytest.importorskip("py7zr")
    _stub_7z_passes(monkeypatch)
    misc = volume / "misc"
    misc.mkdir()
    for name in ("a.txt", "b.txt"):
        (misc / name).write_bytes(b"x")
    archive = misc / "Private.7z"
    with py7zr.SevenZipFile(archive, "w", password="pw", header_encryption=True) as z:
        z.write(misc / "a.txt", "a.txt")
        z.write(misc / "b.txt", "b.txt")
    (misc / "a.txt").unlink()
    (misc / "b.txt").unlink()

    code, lines = _cli(capsys, "verify", str(volume), "--json")

    assert (code, lines) == (0, [])


def _assert_refused(lines, code, archive, calls, rule):
    """review r8 I1: a safety rule refused it, so it can't be checked here:
    skipped, no failure, the stored pass kept, and 7z never run."""
    assert code == 0, lines
    assert [(Path(line["path"]).name, line["status"]) for line in lines] == [
        (archive.name, "skipped"),
    ]
    reason = lines[0]["reason"]
    assert "can't be checked" in reason and "archive safety rules" in reason
    assert rule in reason
    assert calls == []
    assert _stored(archive) is not None


def test_a_single_rom_archive_with_an_unsafe_junk_member_is_skipped(
    volume, dolphin, capsys, monkeypatch,
):
    calls = _stub_7z_passes(monkeypatch)
    archive = volume / "gba" / "Game.gba.zip"
    archive.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Game.gba", b"rom" * 64)
        zf.writestr("__MACOSX/../../victim.gba", b"x")
    _store(archive)

    code, lines = _cli(capsys, "verify", str(volume), "--json")

    _assert_refused(lines, code, archive, calls, "..")


def test_a_healthy_rom_archive_over_a_size_cap_is_skipped(
    volume, dolphin, capsys, monkeypatch,
):
    """review r8 Q2: an operator's size cap is not damage."""
    from config import settings

    calls = _stub_7z_passes(monkeypatch)
    archive = volume / "nds" / "Game.nds.zip"
    archive.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Game.nds", b"rom" * 4096)
    _store(archive)
    monkeypatch.setattr(settings, "archive_max_member_size", 1024)

    code, lines = _cli(capsys, "verify", str(volume), "--json")

    _assert_refused(lines, code, archive, calls, "CHD_ARCHIVE_MAX_MEMBER_SIZE")


def _encrypt_zip_members(path):
    """Set the encryption bit on every member, as an encrypting zip tool does."""
    data = bytearray(path.read_bytes())
    for signature, flag_at in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        start = 0
        while (at := data.find(signature, start)) != -1:
            data[at + flag_at] |= 0x01
            start = at + 4
    path.write_bytes(bytes(data))


def test_a_rom_archive_with_an_encrypted_member_is_skipped(
    volume, dolphin, capsys, monkeypatch,
):
    calls = _stub_7z_passes(monkeypatch)
    archive = _romz_zip(volume / "gba" / "Game.gba.zip")
    _encrypt_zip_members(archive)
    _store(archive)

    code, lines = _cli(capsys, "verify", str(volume), "--json")

    _assert_refused(lines, code, archive, calls, "encrypted")


def test_a_7z_rom_archive_with_encrypted_content_is_skipped(
    volume, dolphin, capsys, monkeypatch,
):
    py7zr = pytest.importorskip("py7zr")
    calls = _stub_7z_passes(monkeypatch)
    rom = volume / "Game.gba"
    rom.write_bytes(b"rom" * 64)
    archive = volume / "gba" / "Game.gba.7z"
    archive.parent.mkdir()
    with py7zr.SevenZipFile(archive, "w", password="pw") as z:
        z.write(rom, "Game.gba")
    rom.unlink()
    _store(archive)

    code, lines = _cli(capsys, "verify", str(volume), "--json")

    _assert_refused(lines, code, archive, calls, "encrypted")


def test_7z_never_waits_for_a_password(monkeypatch):
    """review r8: every 7z command that reads an archive names a password, so
    an encrypted one fails at once instead of prompting. Compressing names
    none: there the switch would encrypt the new archive."""
    from services.romz import ROMZ_EXTRACT_MODE, romz_service

    def password_switches(cmd):
        return [a for a in cmd[: cmd.index("--")] if a.startswith("-p")]

    for cmd in (
        romz_service._build_command("/i/Game.gba.7z", "/o", ROMZ_EXTRACT_MODE, None),
        romz_service._test_command("/i/Game.gba.7z"),
    ):
        assert [len(a) > 2 for a in password_switches(cmd)] == [True], cmd
    compress = romz_service._build_command("Game.gba", "/o/Game.gba.7z", "romz_7z", None)
    assert password_switches(compress) == []


def test_the_7z_password_switch_is_random_per_call():
    """Codacy B105: no password literal. Each read command gets a fresh
    throwaway value, which only exists so 7z never prompts."""
    from services.romz import romz_service

    def switch(cmd):
        return next(a for a in cmd[: cmd.index("--")] if a.startswith("-p"))

    first = switch(romz_service._test_command("/i/Game.gba.7z"))
    second = switch(romz_service._test_command("/i/Game.gba.7z"))
    assert first != second
    assert len(first) > 2 + 8


@pytest.mark.parametrize("compression", [None, "fast", "default", "max"])
def test_7z_a_never_gets_a_password_switch(compression):
    """On `a` the switch would encrypt the archive being written."""
    from services.romz import ROMZ_COMPRESS_MODES, romz_service

    for mode in sorted(ROMZ_COMPRESS_MODES):
        cmd = romz_service._build_command("Game.gba", "/o/Game.gba.out", mode, compression)
        assert cmd[cmd.index(romz_service.sevenzip_path) + 1] == "a", cmd
        assert not any(a.startswith("-p") for a in cmd[: cmd.index("--")]), cmd


def _store(path, **meta):
    import asyncio

    import cli
    from services.verification_store import verification_store

    cli._open_db(False)
    asyncio.run(verification_store.mark_verified(str(path), **meta))


def _stored(path):
    import asyncio

    from services.verification_store import verification_store

    return asyncio.run(verification_store.get_record(str(path)))


def test_a_stored_check_goes_back_to_the_tool_of_its_recorded_mode(
    volume, dolphin, capsys,
):
    """r7 I2 / P9: a PS3 .iso makeps3iso built and checked is checked by
    makeps3iso again, whatever the Platform narrowing would say."""
    iso = volume / "ps3" / "Game.iso"
    iso.parent.mkdir()
    iso.write_bytes(b"\0" * 4096)  # damaged: no PARAM.SFO any more
    _store(iso, produced_meta={"mode": "folder_to_iso"})

    code, lines = _cli(capsys, "verify", str(volume), "--json")

    assert code == 1
    assert (lines[0]["tool"], lines[0]["status"]) == ("makeps3iso", "failed")
    assert _stored(iso) is None


def test_a_stored_check_skips_platform_narrowing(volume, dolphin, capsys):
    """r7 I2 / P7: a GameCube .iso with a stored Dolphin pass, header gone, in a
    ps2 folder, still reaches Dolphin and fails."""
    iso = volume / "ps2" / "Game.iso"
    iso.parent.mkdir()
    iso.write_bytes(b"\0" * 4096)
    _store(iso)
    dolphin["valid"] = False

    code, lines = _cli(capsys, "verify", str(iso), "--json")

    assert code == 1
    assert (lines[0]["tool"], lines[0]["status"]) == ("dolphin", "failed")
    assert _stored(iso) is None


def test_only_the_stored_check_can_reach_a_replaced_archive(
    volume, dolphin, capsys, monkeypatch,
):
    """r7 M1: a stored-pass .gba.zip replaced by a two-ROM zip is no longer
    romz's by shape; only the stored check routes it, and the check fails."""
    _stub_7z_passes(monkeypatch)
    archive = _romz_zip(volume / "gba" / "Game.gba.zip")
    _store(archive)
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("A.gba", b"a" * 64)
        zf.writestr("B.gba", b"b" * 64)

    code, lines = _cli(capsys, "verify", str(volume), "--json")

    assert code == 1
    assert (lines[0]["path"], lines[0]["status"]) == (str(archive), "failed")


def test_a_recorded_mode_that_cannot_verify_is_reported_not_failed(volume, dolphin, capsys):
    cue = volume / "psx" / "Game.cue"
    cue.parent.mkdir()
    cue.write_text("FILE \"Game.bin\" BINARY\n")
    _store(cue, produced_meta={"mode": "extractcd"})

    code, lines = _cli(capsys, "verify", str(volume), "--json")

    assert code == 0
    assert lines[0]["status"] == "skipped"
    assert "can't be checked" in lines[0]["reason"]
    assert _stored(cue) is not None


def test_a_stored_check_no_tool_reads_is_reported_in_a_folder_run(volume, dolphin, capsys):
    """r7 M2: it used to vanish from a folder run."""
    thing = volume / "misc" / "Thing.bin"
    thing.parent.mkdir()
    thing.write_bytes(b"x" * 10)
    _store(thing)

    code, lines = _cli(capsys, "verify", str(volume), "--json")

    assert code == 0
    assert [(line["path"], line["status"]) for line in lines] == [(str(thing), "skipped")]


def test_an_iso_whose_platform_is_unknown_is_skipped_not_failed(volume, dolphin, capsys):
    """r7 I4 / P1: no stored check and no Platform: never guess Dolphin."""
    iso = volume / "misc" / "Linux.iso"
    iso.parent.mkdir()
    iso.write_bytes(b"\0" * 32768 + b"\x01CD001\x01" + b"\0" * 4096)
    dolphin["valid"] = False

    code, lines = _cli(capsys, "verify", str(volume), "--json")

    assert code == 0
    assert lines[0]["status"] == "skipped"
    assert "Platform" in lines[0]["reason"] and "determined" in lines[0]["reason"]


# --------------------------------------------------------------------------- #
# Codex on 618b0aa
# --------------------------------------------------------------------------- #


def test_an_archive_holding_another_image_is_kept(volume, dolphin, capsys):
    """codex 4576728587: converting only Game.iso out of pack.zip must not
    delete pack.zip, which also holds Other.nsp."""
    pack = volume / "gc" / "pack.zip"
    pack.parent.mkdir()
    with zipfile.ZipFile(pack, "w") as zf:
        zf.writestr("Game.iso", b"the disc" * 512)
        zf.writestr("Other.nsp", b"switch" * 64)

    code, lines = _cli(
        capsys, "convert", str(volume), "--to", "rvz", "--delete-source",
        "--container-only", "--json",
    )

    assert pack.exists()
    with zipfile.ZipFile(pack) as zf:
        assert "Other.nsp" in zf.namelist()
    line = next(x for x in lines if x["path"].endswith("::Game.iso"))
    assert line["status"] == "completed"
    assert line["source_deleted"] is False
    assert "Other.nsp" in line["reason"]
    assert code == 1  # a Source that was asked to go was kept


@pytest.mark.parametrize("extra", ["Game.gba", "Other.bin", "Other.zip", "readme.txt"])
def test_an_archive_with_any_other_member_is_kept(volume, dolphin, capsys, extra):
    """review r9 C1: fail closed. The second member isn't even discovered as
    an Image here, and the archive still stays."""
    pack = volume / "gc" / "pack.zip"
    pack.parent.mkdir()
    with zipfile.ZipFile(pack, "w") as zf:
        zf.writestr("Game.iso", b"the disc" * 512)
        zf.writestr(extra, b"other")

    code, lines = _cli(
        capsys, "convert", str(volume), "--to", "rvz", "--delete-source",
        "--container-only", "--json",
    )

    assert pack.exists()
    line = next(x for x in lines if x["path"].endswith("::Game.iso"))
    assert line["source_deleted"] is False
    assert extra in line["reason"]
    assert code == 1


def test_a_single_image_archive_is_still_deleted(volume, dolphin, capsys):
    pack = volume / "gc" / "pack.zip"
    pack.parent.mkdir()
    with zipfile.ZipFile(pack, "w") as zf:
        zf.writestr("Game.iso", b"the disc" * 512)

    code, lines = _cli(
        capsys, "convert", str(volume), "--to", "rvz", "--delete-source",
        "--container-only", "--json",
    )

    assert code == 0, lines
    assert not pack.exists()


def test_refreshing_a_check_keeps_the_recorded_mode(volume, dolphin, capsys, monkeypatch):
    """codex 4576728584: a passing re-check wrote produced_meta=None, so the
    next run lost the recorded mode and sent the PS3 ISO to Dolphin."""
    from services import ps3

    monkeypatch.setattr(ps3, "ps3_iso_title_id", lambda path, **kw: "BLES00000")
    iso = volume / "misc" / "Game.iso"
    iso.parent.mkdir()
    iso.write_bytes(b"\0" * 4096)
    _store(iso, produced_meta={"mode": "folder_to_iso"})

    for _ in range(2):
        code, lines = _cli(capsys, "verify", str(iso), "--json")
        assert code == 0, lines
        assert (lines[0]["tool"], lines[0]["status"]) == ("makeps3iso", "intact")
    assert _stored(iso)["produced_meta"] == {"mode": "folder_to_iso"}


def test_a_ps3_iso_goes_to_makeps3iso_on_every_run(volume, dolphin, capsys, monkeypatch):
    """review r8 C1 / Q1: a PS3 ISO makeps3iso built, verified twice over the
    whole volume, with Dolphin failing anything it is handed. Both runs go to
    makeps3iso and pass, and the record keeps its mode."""
    from services import ps3

    monkeypatch.setattr(ps3, "ps3_iso_title_id", lambda path, **kw: "BLES00000")
    dolphin["valid"] = False
    iso = volume / "ps3" / "Game.iso"
    iso.parent.mkdir()
    iso.write_bytes(b"\0" * 4096)
    _store(iso, produced_meta={"mode": "folder_to_iso"})

    for _ in range(2):
        code, lines = _cli(capsys, "verify", str(volume), "--json")
        assert code == 0, lines
        assert [(x["tool"], x["status"]) for x in lines] == [("makeps3iso", "intact")]
    assert _stored(iso)["produced_meta"] == {"mode": "folder_to_iso"}


def test_a_cli_job_holds_its_input_for_other_processes(volume, dolphin, capsys, monkeypatch):
    """codex 4576728590: while a job reads Game.wia, another process sees the
    input as taken (the web guard's exact-path probe refuses with 409)."""
    from services.lock_manager import LockManager, lock_manager

    wia = volume / "gc" / "Game.wia"
    wia.parent.mkdir()
    wia.write_bytes(b"wia" * 64)
    server = LockManager()
    server._lock_dir = lock_manager._lock_dir
    seen = []
    service = registry.get("dolphin")._service
    original = service._runner.run

    async def watching_run(cmd, *, input_path, output_path, **kwargs):
        seen.append(server.check_file_status(input_path)[1])
        async for update in original(cmd, input_path=input_path,
                                     output_path=output_path, **kwargs):
            yield update

    monkeypatch.setattr(service._runner, "run", watching_run)

    code, lines = _cli(capsys, "convert", str(volume), "--to", "rvz", "--json")

    assert code == 0, lines
    assert seen == [True]
    assert server.check_file_status(str(wia))[1] is False


def test_delete_on_verify_keeps_a_source_whose_folder_another_process_locked(
    volume, dolphin, capsys, monkeypatch,
):
    """codex 4576810647: in the window after the job lets go of its read locks,
    another process takes the Source's folder (makeps3iso packing it). The
    delete check sees that lock above the Source and keeps it."""
    from services import job_manager as job_manager_module
    from services.job_manager import JobManager
    from services.lock_manager import LockManager, lock_manager

    monkeypatch.setattr(job_manager_module, "DELETE_READER_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(job_manager_module, "DELETE_READER_POLL_SECONDS", 0.05)
    game = volume / "gc" / "GAME"
    game.mkdir(parents=True)
    iso = game / "Game.iso"
    iso.write_bytes(b"the disc" * 512)
    out_dir = volume / "out"
    out_dir.mkdir()
    packer = LockManager()
    packer._lock_dir = lock_manager._lock_dir
    original = JobManager._release_inputs
    taken = []

    def release_then_pack(self, job_id):
        original(self, job_id)
        if not taken:
            taken.append(packer.acquire_dir_lock(str(game)))

    monkeypatch.setattr(JobManager, "_release_inputs", release_then_pack)
    try:
        code, lines = _cli(
            capsys, "convert", str(iso), "--to", "rvz", "--delete-source",
            "--container-only", "--output-dir", str(out_dir), "--json",
        )
    finally:
        packer.release_dir_lock(str(game))

    assert taken == [True]
    assert iso.exists(), lines
    assert lines[0]["source_deleted"] is False
    assert lines[0]["status"] == "completed"


@pytest.mark.parametrize("command", [["verify"], ["convert", "--to", "rvz"]])
def test_a_fifo_named_like_an_image_is_skipped_without_hanging(
    volume, dolphin, capsys, command,
):
    """#294 refuses special files in detect_platform and resolve_track; a CLI
    run over a folder holding a FIFO named game.iso must not block opening it."""
    import os
    import signal

    fifo = volume / "gc" / "game.iso"
    fifo.parent.mkdir()
    os.mkfifo(fifo)

    def timed_out(signum, frame):
        raise TimeoutError("the CLI blocked on a FIFO")

    previous = signal.signal(signal.SIGALRM, timed_out)
    signal.alarm(20)
    try:
        code, lines = _cli(capsys, command[0], str(volume), *command[1:], "--json")
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)

    assert code == 0, lines
    assert all(line["status"] == "skipped" for line in lines), lines
    assert dolphin["runs"] == []


def test_delete_paths_are_held_exclusively_until_unlinked(volume, dolphin, capsys, monkeypatch):
    """review r9 M1: between the delete check and the unlink, no job in any
    process can start reading the Source. Another manager trying to take a
    shared read lock on it at unlink time is refused."""
    from services.lock_manager import LockManager, lock_manager

    iso = volume / "gc" / "Game.iso"
    iso.parent.mkdir()
    iso.write_bytes(b"the disc" * 512)
    reader = LockManager()
    reader._lock_dir = lock_manager._lock_dir
    real_remove = os.remove
    seen = []

    def remove_watching(path, *args, **kwargs):
        if os.path.realpath(path) == os.path.realpath(iso):
            got = reader.acquire_input_lock(str(iso))
            seen.append(got)
            if got:
                reader.release_input_lock(str(iso))
        return real_remove(path, *args, **kwargs)

    monkeypatch.setattr(os, "remove", remove_watching)
    code, lines = _cli(
        capsys, "convert", str(iso), "--to", "rvz", "--delete-source",
        "--container-only", "--json",
    )

    assert code == 0, lines
    assert seen == [False]
    assert not iso.exists()
    assert reader.acquire_input_lock(str(volume / "gc" / "Game.rvz"))


def _reader_on(path):
    from services.lock_manager import LockManager, lock_manager

    reader = LockManager()
    reader._lock_dir = lock_manager._lock_dir
    assert reader.acquire_input_lock(str(path))
    return reader


def test_delete_waits_for_a_reader_that_finishes_within_the_bound(
    volume, dolphin, capsys, monkeypatch,
):
    """#305 review: a Source another process is reading is not a failure; the
    delete waits a short bound for the reader, then deletes."""
    import threading

    from services import job_manager as job_manager_module

    monkeypatch.setattr(job_manager_module, "DELETE_READER_WAIT_SECONDS", 10.0)
    monkeypatch.setattr(job_manager_module, "DELETE_READER_POLL_SECONDS", 0.05)
    iso = volume / "gc" / "Game.iso"
    iso.parent.mkdir()
    iso.write_bytes(b"the disc" * 512)
    reader = _reader_on(iso)
    timer = threading.Timer(1.0, reader.release_input_lock, args=[str(iso)])
    timer.start()
    try:
        code, lines = _cli(
            capsys, "convert", str(iso), "--to", "rvz", "--delete-source",
            "--container-only", "--json",
        )
    finally:
        timer.join()

    assert code == 0, lines
    assert (lines[0]["status"], lines[0]["source_deleted"]) == ("completed", True)
    assert not iso.exists()


def test_a_reader_past_the_bound_keeps_the_source_and_the_job_completes(
    volume, dolphin, capsys, monkeypatch,
):
    from services import job_manager as job_manager_module

    monkeypatch.setattr(job_manager_module, "DELETE_READER_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(job_manager_module, "DELETE_READER_POLL_SECONDS", 0.05)
    iso = volume / "gc" / "Game.iso"
    iso.parent.mkdir()
    iso.write_bytes(b"the disc" * 512)
    reader = _reader_on(iso)
    try:
        code, lines = _cli(
            capsys, "convert", str(iso), "--to", "rvz", "--delete-source",
            "--container-only", "--json",
        )
    finally:
        reader.release_input_lock(str(iso))

    assert lines[0]["status"] == "completed", lines
    assert lines[0]["source_deleted"] is False
    assert "Source kept: it was being read by another job" in lines[0]["reason"]
    assert iso.exists()
    assert (volume / "gc" / "Game.rvz").exists()
    # Transient, and the conversion succeeded: a warning, not a failure (the
    # same rule as #307's run rules). Safety refusals still exit 1.
    assert code == 0


def test_an_xiso_outside_an_xbox_folder_goes_to_xiso(volume):
    """review r8 M2 / r9 M5: ``.xiso.iso`` is xiso's own output name. chdman's
    shorter ``.iso`` match must not make it a shared extension, which skipped
    it as "Platform couldn't be determined" outside an Xbox folder."""
    import cli
    from tests.test_xiso_service import build_image

    image = volume / "games" / "Halo.xiso.iso"
    image.parent.mkdir()
    build_image(image, layout="XISO", optimized=True)

    tool, unsupported = cli._verify_tool(str(image), None)

    assert unsupported is None
    assert tool is not None and tool.id == "xiso"


def test_an_archive_with_an_unsafe_member_is_kept_and_nothing_is_written(
    volume, dolphin, capsys, tmp_path, monkeypatch,
):
    """codex 4577029535: a member named ``../outside-...`` keeps the archive,
    exit 1, and no file is created from its name anywhere."""
    import tempfile
    import uuid

    from config import settings

    app_temp = tmp_path / "app-temp"
    monkeypatch.setattr(settings, "temp_dir", str(app_temp))
    escape = f"../outside-{uuid.uuid4().hex}"
    pack = volume / "gc" / "pack.zip"
    pack.parent.mkdir()
    with zipfile.ZipFile(pack, "w") as zf:
        zf.writestr("Game.iso", b"the disc" * 512)
        zf.writestr(escape, b"payload")

    code, lines = _cli(
        capsys, "convert", str(volume), "--to", "rvz", "--delete-source",
        "--container-only", "--json",
    )

    assert code == 1, lines
    assert pack.exists()
    line = next(x for x in lines if x["path"].endswith("::Game.iso"))
    assert escape in line["reason"]
    assert "unsafe member name" in line["reason"]
    for root in (
        tempfile.gettempdir(), str(volume), str(volume / "gc"), str(tmp_path), str(app_temp),
    ):
        assert not os.path.exists(os.path.join(root, escape[3:])), root


def test_the_extraction_validator_refuses_a_nul_in_a_member_name():
    from services.archive import archive_service

    with pytest.raises(ValueError, match="NUL"):
        archive_service._validate_member("Game\x00.iso")


def test_a_folder_of_chds_is_discovered_for_an_extract_mode(volume, capsys):
    """codex 4577029533: with --to a mode id, a folder walk uses that mode's
    accepted extensions; chdman leaves .chd out of its tool-wide inputs, so
    `convert DIR --to extractdvd` used to find nothing."""
    ps2 = volume / "ps2"
    ps2.mkdir()
    for name in ("A.chd", "B.chd"):
        (ps2 / name).write_bytes(b"MComprHD" + b"\0" * 120)

    code, lines = _cli(
        capsys, "convert", str(volume), "--to", "extractdvd", "--dry-run", "--json",
    )

    assert code == 0, lines
    assert sorted(Path(x["path"]).name for x in lines) == ["A.chd", "B.chd"]
    assert {x["mode"] for x in lines} == {"extractdvd"}
    assert {x["status"] for x in lines} == {"planned"}


def _other_process():
    from services.lock_manager import LockManager, lock_manager

    other = LockManager()
    other._lock_dir = lock_manager._lock_dir
    return other


def test_a_verify_holds_the_file_against_writers_until_it_ends(
    volume, dolphin, capsys, monkeypatch,
):
    """codex 4577087047: while the CLI checks a file, no other process can
    take it to overwrite or replace it; once the check ends, it can."""
    rvz = _rvz(volume, "gc/Game.rvz")
    other = _other_process()
    tool = registry.get("dolphin")
    original = tool.verify
    seen = []

    async def watching_verify(path, *args, **kwargs):
        seen.append(other.acquire_lock(path, allow_existing=True))
        return await original(path, *args, **kwargs)

    monkeypatch.setattr(tool, "verify", watching_verify)

    code, lines = _cli(capsys, "verify", str(rvz), "--json")

    assert code == 0, lines
    assert lines[0]["status"] == "intact"
    assert seen == [False]  # the replacement could not start mid-check
    assert other.acquire_lock(str(rvz), allow_existing=True)
    other.release_lock(str(rvz))


def test_a_file_a_writer_holds_is_skipped_as_busy(volume, dolphin, capsys):
    """A writer in another process holds the file: the check is skipped as
    busy, not failed, and the stored record is left alone."""
    rvz = _rvz(volume, "gc/Game.rvz")
    _store(rvz)
    other = _other_process()
    assert other.acquire_lock(str(rvz), allow_existing=True)
    try:
        code, lines = _cli(capsys, "verify", str(rvz), "--json")
    finally:
        other.release_lock(str(rvz))

    assert code == 0, lines
    assert lines[0]["status"] == "skipped"
    assert "busy" in lines[0]["reason"].lower()
    assert dolphin["runs"] == [] or all("verify" not in str(r) for r in dolphin["runs"])
    assert _stored(rvz) is not None


def test_a_verify_lets_go_of_its_lock_when_the_check_raises(
    volume, dolphin, capsys, monkeypatch,
):
    rvz = _rvz(volume, "gc/Game.rvz")
    tool = registry.get("dolphin")

    async def broken_verify(path, *args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(tool, "verify", broken_verify)

    _cli(capsys, "verify", str(rvz), "--json")

    other = _other_process()
    assert other.acquire_lock(str(rvz), allow_existing=True)
    other.release_lock(str(rvz))


def test_a_to_walk_skips_and_reports_an_unreadable_folder(volume, capsys):
    """review r11 I1: one folder the user can't read must not crash a --to
    run; the readable CHDs are discovered and the folder is reported."""
    ok = volume / "ok"
    ok.mkdir()
    (ok / "A.chd").write_bytes(b"MComprHD" + b"\0" * 120)
    locked = volume / "locked"
    locked.mkdir()
    (locked / "B.chd").write_bytes(b"MComprHD" + b"\0" * 120)
    os.chmod(locked, 0)
    try:
        code, lines = _cli(
            capsys, "convert", str(volume), "--to", "extractdvd", "--dry-run", "--json",
        )
    finally:
        os.chmod(locked, 0o700)

    assert code == 0, lines
    by_name = {Path(x["path"]).name: x for x in lines}
    assert by_name["A.chd"]["status"] == "planned"
    assert by_name["locked"]["status"] == "skipped"
    assert "read" in by_name["locked"]["reason"].lower()
    assert "B.chd" not in by_name


def test_verify_reports_an_unreadable_folder_too(volume, dolphin, capsys):
    _rvz(volume, "gc/Game.rvz")
    locked = volume / "locked"
    locked.mkdir()
    os.chmod(locked, 0)
    try:
        code, lines = _cli(capsys, "verify", str(volume), "--json")
    finally:
        os.chmod(locked, 0o700)

    assert code == 0, lines
    by_name = {Path(x["path"]).name: x for x in lines}
    assert by_name["Game.rvz"]["status"] == "intact"
    assert by_name["locked"]["status"] == "skipped"


def test_a_check_lock_that_raises_part_way_leaves_nothing_held(volume, monkeypatch):
    """If taking the second file's lock raises (an OSError on the lock dir),
    the first file's lock is released before the error goes on."""
    import cli
    from services.lock_manager import lock_manager

    first = volume / "gc" / "game_part1.wud"
    first.parent.mkdir()
    first.write_bytes(b"1")
    second = volume / "gc" / "game_part2.wud"
    second.write_bytes(b"2")
    monkeypatch.setattr(registry, "source_companions", lambda path: [str(second)])
    original = lock_manager.acquire_input_lock

    def failing_on_the_second(path):
        if path == str(second):
            raise OSError("lock dir unavailable")
        return original(path)

    monkeypatch.setattr(lock_manager, "acquire_input_lock", failing_on_the_second)

    with pytest.raises(OSError):
        cli._hold_for_check(str(first))

    other = _other_process()
    assert other.acquire_lock(str(first), allow_existing=True)
    other.release_lock(str(first))


def test_an_input_lock_that_raises_fails_the_job_and_frees_the_slot(
    volume, dolphin, capsys, monkeypatch,
):
    """codex 4577157688: an exception while taking a job's input locks must
    not leave it queued holding its output lock and the slot (which stalls
    every later conversion at concurrency 1). It fails, everything is let go,
    and the next job runs."""
    from services.lock_manager import lock_manager

    bad = volume / "gc" / "A.iso"
    good = volume / "gc" / "B.iso"
    bad.parent.mkdir()
    bad.write_bytes(b"the disc" * 512)
    good.write_bytes(b"the disc" * 512)
    original = lock_manager.acquire_input_lock

    def raising_for_a(path):
        if os.path.basename(path) == "A.iso":
            raise UnicodeEncodeError("utf-8", "x", 0, 1, "surrogates not allowed")
        return original(path)

    monkeypatch.setattr(lock_manager, "acquire_input_lock", raising_for_a)

    code, lines = _cli(capsys, "convert", str(volume), "--to", "rvz", "--json")

    by_name = {Path(x["path"]).name: x for x in lines}
    assert by_name["A.iso"]["status"] == "failed", lines
    assert "lock" in by_name["A.iso"]["reason"].lower()
    assert by_name["B.iso"]["status"] == "completed", lines
    assert code == 1
    other = _other_process()
    assert other.acquire_lock(str(volume / "gc" / "A.rvz"))  # its output lock is gone
    other.release_lock(str(volume / "gc" / "A.rvz"))


def test_a_job_cancelled_while_waiting_for_a_reader_keeps_its_source(
    volume, dolphin, capsys, monkeypatch,
):
    """codex 4577178425: cancelled during the reader wait, and the reader lets
    go before the next poll: the Source must survive and the job end
    CANCELLED, not take its delete locks and unlink."""
    import asyncio
    import threading

    from services import job_manager as job_manager_module
    from services.job_manager import JobManager, job_manager

    monkeypatch.setattr(job_manager_module, "DELETE_READER_WAIT_SECONDS", 10.0)
    monkeypatch.setattr(job_manager_module, "DELETE_READER_POLL_SECONDS", 0.5)
    iso = volume / "gc" / "Game.iso"
    iso.parent.mkdir()
    iso.write_bytes(b"the disc" * 512)
    reader = _reader_on(iso)
    loops = {}
    original_release = JobManager._release_inputs
    original_try = JobManager._try_hold_for_delete

    def recording_release(self, job_id):
        try:
            loops.setdefault("loop", asyncio.get_running_loop())
        except RuntimeError:
            pass
        return original_release(self, job_id)

    def cancel_then_let_go(self, job_id, paths):
        got = original_try(self, job_id, paths)
        if not got and "cancelled" not in loops:
            loops["cancelled"] = True

            def during_the_sleep():
                # Cancel the job, then the reader lets go, both while the
                # wait sleeps before its next poll.
                asyncio.run_coroutine_threadsafe(
                    job_manager.cancel_job(job_id), loops["loop"],
                ).result(5)
                reader.release_input_lock(str(iso))

            threading.Timer(0.05, during_the_sleep).start()
        return got

    monkeypatch.setattr(JobManager, "_release_inputs", recording_release)
    monkeypatch.setattr(JobManager, "_try_hold_for_delete", cancel_then_let_go)
    try:
        code, lines = _cli(
            capsys, "convert", str(iso), "--to", "rvz", "--delete-source",
            "--container-only", "--json",
        )
    finally:
        if "cancelled" not in loops:
            reader.release_input_lock(str(iso))

    assert loops.get("cancelled"), "the wait never started"
    assert iso.exists(), lines
    assert lines[0]["status"] == "cancelled", lines
    assert lines[0]["source_deleted"] is False


@pytest.mark.parametrize("command", [
    ["verify"], ["convert", "--to", "extractdvd"],
])
def test_a_symlink_out_of_the_volumes_is_skipped_never_read(
    volume, dolphin, capsys, tmp_path, command,
):
    """codex 4577208363: a symlink in a Volume pointing outside every Volume is
    not planned, checked or converted; it is skipped as outside the
    configured Volumes."""
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "Secret.chd"
    target.write_bytes(b"MComprHD" + b"\0" * 120)
    (volume / "ps2").mkdir()
    link = volume / "ps2" / "Escape.chd"
    link.symlink_to(target)
    inside = volume / "ps2" / "Inside.chd"
    inside.write_bytes(b"MComprHD" + b"\0" * 120)

    code, lines = _cli(capsys, command[0], str(volume), *command[1:], "--dry-run", "--json")

    by_name = {Path(x["path"]).name: x for x in lines}
    assert by_name["Escape.chd"]["status"] == "skipped", lines
    assert "outside the configured Volumes" in by_name["Escape.chd"]["reason"]
    assert by_name["Inside.chd"]["status"] == "planned", lines
    assert code == 0, lines


def test_a_checked_file_replaced_by_an_escaping_symlink_is_skipped_once(
    volume, dolphin, capsys, tmp_path,
):
    """codex 4577304458: a verified CHD later replaced by a symlink to a file
    outside the Volumes is listed once, as skipped outside, and no record is
    written for it or for the outside target."""
    chd = volume / "ps2" / "Game.chd"
    chd.parent.mkdir()
    chd.write_bytes(b"MComprHD" + b"\0" * 120)
    _store(chd)
    before = _all_records()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "Other.chd"
    target.write_bytes(b"MComprHD" + b"\0" * 120)
    chd.unlink()
    chd.symlink_to(target)

    code, lines = _cli(capsys, "verify", str(volume), "--json")

    named = [x for x in lines if Path(x["path"]).name == "Game.chd"]
    assert len(named) == 1, lines
    assert named[0]["status"] == "skipped"
    assert "outside the configured Volumes" in named[0]["reason"]
    assert _stored(target) is None
    assert _all_records() == before  # the old record is left exactly as it was
    assert code == 0, lines


def _all_records():
    import asyncio

    from services.verification_store import verification_store

    return sorted(
        (r["chd_path"], r["verified_at"]) for r in asyncio.run(verification_store.all_records())
    )
