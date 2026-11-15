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
    assert not _rule_state(env)["gc"].get("kept_sources")


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


# --------------------------------------------------------------------------- #
# The decision that kept it must still be in force (Codex review of #343)
# --------------------------------------------------------------------------- #

KEPT_RULE = {
    "mode": "dolphin_rvz", "enabled": True, "delete_on_verify": True,
    "check_level": "container_only",
}


def _rule(env, **changes):
    return {"gc": {**KEPT_RULE, "output_dir": str(env["vol"] / "out"), **changes}}


def test_a_disabled_rule_keeps_the_source_and_the_entry(env, dolphin, capsys, kept):
    _store_rules(env, {"shelf": _rule(env, enabled=False)})

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0, lines
    assert kept["iso"].exists()
    assert _rule_state(env)["gc"]["kept_sources"]["Game.iso"]["attempts"] == 0


def test_a_validation_paused_rule_keeps_it_even_when_named(env, dolphin, capsys, kept):
    from services.rules import sweep as rules_sweep

    _store_rules(env, {"shelf": _rule(env, invalid_pattern=True)})

    result = asyncio.run(rules_sweep.sweep(env["shelf"], platform_ids=["gc"]))

    assert {"platform_id": "gc", "error": "rule_invalid"} in result["errors"]
    assert kept["iso"].exists()
    assert _rule_state(env)["gc"]["kept_sources"]["Game.iso"]["attempts"] == 0


def test_a_retargeted_rule_drops_the_entry_without_deleting(env, dolphin, capsys, kept):
    elsewhere = env["vol"] / "elsewhere"
    elsewhere.mkdir()
    _store_rules(env, {"shelf": _rule(env, output_dir=str(elsewhere))})

    assert not _rule_state(env)["gc"].get("kept_sources")
    dolphin["fail"] = True  # so the retargeted Rule's own conversion keeps it too
    _cli(capsys, "run-rules", "--json")

    assert kept["iso"].exists()


def test_an_entry_from_a_former_rule_identity_is_dropped_at_retry(
    env, dolphin, capsys, kept,
):
    from services.rules import sweep as rules_sweep

    async def tamper():
        state = await rules_sweep.get_state(env["shelf"])
        state["gc"]["kept_sources"]["Game.iso"]["rule_identity"] = ["dolphin_wia"]
        await rules_sweep.store.write(rules_sweep.store.STATE_KEY, env["shelf"], state)

    asyncio.run(tamper())

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0, lines
    assert kept["iso"].exists()
    assert not _rule_state(env)["gc"].get("kept_sources")


def test_a_failed_unlink_counts_an_attempt_and_the_sweep_goes_on(
    env, dolphin, capsys, kept, monkeypatch,
):
    import os

    iso = kept["iso"]
    real_remove = os.remove

    def refuse(path, *args, **kwargs):
        if os.path.realpath(path) == os.path.realpath(iso):
            raise PermissionError(13, "Permission denied", str(path))
        return real_remove(path, *args, **kwargs)

    monkeypatch.setattr(os, "remove", refuse)
    other = _iso(env, "attic")
    _store_rules(env, {
        "shelf": _rule(env), "attic": {"gc": {"mode": "dolphin_rvz", "enabled": True}},
    })

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0, lines
    assert iso.exists()
    assert other.with_suffix(".rvz").exists(), "the other Library still swept"
    pending = _rule_state(env)["gc"]["kept_sources"]["Game.iso"]
    assert pending["attempts"] == 1
    assert "Permission denied" in pending["last_reason"]


def test_a_lock_that_answers_late_is_bounded_and_released(
    env, dolphin, capsys, kept, monkeypatch,
):
    import time

    from services import job_manager as job_manager_module
    from services.job_manager import KEPT_KEPT, job_manager
    from services.lock_manager import lock_manager

    real_acquire = lock_manager.acquire_lock
    output = str(kept["output"])

    def slow(path, *args, **kwargs):
        if path == output:
            time.sleep(1.0)
        return real_acquire(path, *args, **kwargs)

    monkeypatch.setattr(lock_manager, "acquire_lock", slow)
    monkeypatch.setattr(job_manager_module, "KEPT_DELETE_PROBE_SECONDS", 0.2)

    started = time.monotonic()
    outcome = asyncio.run(job_manager.retry_kept_delete(
        source_path=str(kept["iso"]), output_path=output,
    ))

    assert time.monotonic() - started < 0.9
    assert outcome.status == KEPT_KEPT
    time.sleep(1.5)
    assert lock_manager.is_locked(output) is False, "the late lock was let go"
    assert kept["iso"].exists()


def test_a_stalled_path_resolve_in_the_delete_step_is_off_the_loop_and_bounded(
    env, dolphin, capsys, kept, monkeypatch,
):
    """The shared delete step resolves and stats its paths on a detached
    thread under a bound (CodeRabbit 4579539704), for the job and the retry
    alike: a volume that stops answering neither stalls the event loop nor
    hangs the step, and nothing is removed."""
    import os
    import time

    from services import job_manager as job_manager_module
    from services.job_manager import job_manager

    monkeypatch.setattr(job_manager_module, "_CANONICAL_PROBE_SECONDS", 0.3)
    output = str(kept["output"])
    real_realpath = os.path.realpath

    def stalled(path, *args, **kwargs):
        if str(path) == output:
            time.sleep(1.0)
        return real_realpath(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", stalled)

    async def run():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.05)

        task = asyncio.create_task(ticker())
        started = time.monotonic()
        with pytest.raises(RuntimeError):
            await job_manager._delete_source_set(
                "stall-test", file_path=str(kept["iso"]), output_path=output,
                expected_paths=[str(kept["iso"])], fingerprints={},
                check_label="Intact", cancel_event=asyncio.Event(),
                cancelled=lambda: False, deleted=[],
            )
        elapsed = time.monotonic() - started
        task.cancel()
        return ticks, elapsed

    ticks, elapsed = asyncio.run(run())

    assert elapsed < 0.9
    assert ticks >= 3, "the event loop kept running"
    assert kept["iso"].exists()
