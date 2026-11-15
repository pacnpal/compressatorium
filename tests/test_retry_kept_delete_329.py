"""A Source kept because a reader held it is deleted on a later sweep (#329).

When a Rule's delete step finds another process still reading the Source, the
job completes and keeps it (#306). The sweep remembers it in the Rule state
(``kept_sources``) and, on each later sweep, retries only the delete step,
under the same locks, reader check and ``mutation_guard``, after
re-establishing that the output is still the verified one and the Source is
still inside the Volumes. A Source a reader holds again is retried on the
next sweep, up to ``KEPT_DELETE_MAX_ATTEMPTS``; then the sweep gives up and
files a ``source_kept`` Finding.

Driven through ``compressatorium run-rules`` (the web sweep runs the same
``sweep``), with #307's folder Library and dolphin stub.
"""
from __future__ import annotations

import asyncio

import pytest

from .test_cli_run_rules_307 import (  # the fixtures, registered by import
    _cli,
    _dolphin,
    _env,
    _iso,
    _rule_state,
    _store_rules,
)

__all__ = ["_dolphin", "_env"]


@pytest.fixture(name="kept")
def _kept(env, dolphin, capsys, monkeypatch):
    """A first pass that converts Game.iso and keeps it for a reader."""
    from services import job_manager as job_manager_module
    from services.job_manager import SOURCE_KEPT_FOR_READERS, JobManager
    from services.lock_manager import LockManager, lock_manager

    monkeypatch.setattr(job_manager_module, "DELETE_READER_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(job_manager_module, "DELETE_READER_POLL_SECONDS", 0.05)
    iso = _iso(env)
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
            taken.append(reader.acquire_dir_lock(str(iso.parent)))

    with monkeypatch.context() as patch:
        patch.setattr(JobManager, "_release_inputs", release_then_read)
        try:
            code, lines, _err = _cli(capsys, "run-rules", "--json")
        finally:
            reader.release_dir_lock(str(iso.parent))

    assert taken == [True]
    assert code == 0, lines
    jobs = [line for line in lines if line.get("path")]
    assert [(j["status"], j["source_deleted"], j["reason"]) for j in jobs] == [
        ("completed", False, SOURCE_KEPT_FOR_READERS),
    ]
    assert iso.exists()
    pending = _rule_state(env)["gc"]["kept_sources"]
    assert list(pending) == ["Game.iso"]
    assert pending["Game.iso"]["source"] == str(iso)
    assert pending["Game.iso"]["output"] == str(out_dir / "Game.rvz")
    return {"iso": iso, "output": out_dir / "Game.rvz", "reader": reader}


def test_a_kept_source_is_deleted_on_the_next_sweep(env, dolphin, capsys, kept):
    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0, lines
    assert not kept["iso"].exists()
    assert kept["output"].read_bytes() == b"converted output"
    assert dolphin["runs"] == [(str(kept["iso"]), str(kept["output"]))]  # not re-converted
    assert "kept_sources" not in _rule_state(env)["gc"] or not _rule_state(env)["gc"]["kept_sources"]


def test_an_output_changed_since_its_check_keeps_the_source(env, dolphin, capsys, kept):
    kept["output"].write_bytes(b"something else entirely")

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0, lines
    assert kept["iso"].exists()
    assert not _rule_state(env)["gc"].get("kept_sources")


def test_an_output_gone_keeps_the_source(env, dolphin, capsys, kept):
    from services.job_manager import job_manager

    kept["output"].unlink()

    outcome = asyncio.run(job_manager.retry_kept_delete(
        source_path=str(kept["iso"]), output_path=str(kept["output"]),
    ))

    assert outcome.status == "stale"
    assert kept["iso"].exists()


def test_a_source_outside_the_volumes_is_not_deleted(env, dolphin, capsys, kept, monkeypatch):
    from config import settings
    from services.job_manager import job_manager

    monkeypatch.setattr(settings, "chd_volumes", str(env["vol"] / "out"))
    monkeypatch.setattr(settings, "data_mount_root", str(env["vol"] / "out"))

    outcome = asyncio.run(job_manager.retry_kept_delete(
        source_path=str(kept["iso"]), output_path=str(kept["output"]),
    ))

    assert outcome.status == "stale"
    assert kept["iso"].exists()


def test_a_reader_again_keeps_it_until_the_cap_then_files_a_finding(
    env, dolphin, capsys, kept, monkeypatch,
):
    from services import findings
    from services.rules import sweep as rules_sweep

    monkeypatch.setattr(rules_sweep, "KEPT_DELETE_MAX_ATTEMPTS", 2)
    reader, iso = kept["reader"], kept["iso"]
    assert reader.acquire_dir_lock(str(iso.parent))
    try:
        code, lines, _err = _cli(capsys, "run-rules", "--json")
        assert code == 0, lines
        assert iso.exists()
        assert _rule_state(env)["gc"]["kept_sources"]["Game.iso"]["attempts"] == 1
        assert not [f for f in findings.list_findings() if f["kind"] == findings.SOURCE_KEPT]

        code, lines, _err = _cli(capsys, "run-rules", "--json")
        assert code == 0, lines
    finally:
        reader.release_dir_lock(str(iso.parent))

    assert iso.exists()
    assert not _rule_state(env)["gc"].get("kept_sources")
    kept_findings = [f for f in findings.list_findings() if f["kind"] == findings.SOURCE_KEPT]
    assert [(f["image_key"], f["path"]) for f in kept_findings] == [("Game.iso", str(iso))]


def test_the_findings_view_names_a_kept_source_and_offers_no_set_platform():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src" / "lib" / "components" / "views"
           / "FindingsView.svelte").read_text(encoding="utf-8")
    assert "source_kept: 'Source kept'" in src
    assert "row.kind !== 'source_kept'" in src
