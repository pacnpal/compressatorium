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
        "delete_source": True,
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
    ("verify", "{vol}", "--since", "soon"),
    ("frobnicate",),
])
def test_bad_arguments_exit_2(volume, dolphin, capsys, argv):
    import cli

    code = cli.main([a.replace("{vol}", str(volume)) for a in argv])

    assert code == 2
    assert capsys.readouterr().out == ""


def test_chd_mode_cli_alias_runs_the_convert_command():
    entrypoint = (Path(__file__).resolve().parents[1] / "entrypoint.sh").read_text()
    cli_branch = entrypoint.split('if [[ "$MODE" == "cli" ]]; then', 1)[1].split("\nelse\n", 1)[0]
    assert 'compressatorium convert "${existing[@]}" --to "$CHDMAN_MODE"' in cli_branch
    assert "chdman createcd" not in cli_branch


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
