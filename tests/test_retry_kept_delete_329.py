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


_DECISION_INPUTS = [
    ("rule", "mode"), ("rule", "output_dir"), ("rule", "compression"),
    ("rule", "compression_level"), ("rule", "split"), ("rule", "duplicate_action"),
    ("rule", "delete_on_verify"), ("rule", "check_level"), ("check", None),
    ("library", None),
]


@pytest.mark.parametrize(("part", "field"), _DECISION_INPUTS)
def test_an_entry_decided_under_any_other_input_is_dropped_at_retry(
    env, dolphin, capsys, kept, part, field,
):
    """Each input to the delete decision, toggled on its own in the stored
    entry: the retry recomputes the decision from the current Rule and
    Library, finds it differs, and drops the entry without deleting."""
    from services.rules import sweep as rules_sweep

    async def tamper():
        state = await rules_sweep.get_state(env["shelf"])
        decision = state["gc"]["kept_sources"]["Game.iso"]["decision"]
        if field is None:
            decision[part] = "something else"
        else:
            decision[part][field] = "something else"
        await rules_sweep.store.write(rules_sweep.store.STATE_KEY, env["shelf"], state)

    asyncio.run(tamper())

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0, lines
    assert kept["iso"].exists()
    assert not _rule_state(env)["gc"].get("kept_sources")


def test_the_decision_identity_covers_every_listed_input():
    from services.rules import sweep as rules_sweep

    assert {f for part, f in _DECISION_INPUTS if part == "rule"} == set(
        rules_sweep.DELETE_DECISION_FIELDS,
    )


def test_a_new_check_level_keeps_the_source_and_drops_the_entry(
    env, dolphin, capsys, kept,
):
    """Kept under the container check; the Rule now asks for the Round-trip,
    which the Intact record never passed (codex 4579636593)."""
    _store_rules(env, {"shelf": _rule(env, check_level="round_trip")})

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0, lines
    assert kept["iso"].exists()
    assert not _rule_state(env)["gc"].get("kept_sources")


def test_a_round_trip_rule_takes_no_intact_only_record(env, dolphin, capsys, kept):
    """The retry's own check, apart from the identity: a Rule that asks for
    the Round-trip refuses an Intact record."""
    from services.job_manager import KEPT_STALE, job_manager

    outcome = asyncio.run(job_manager.retry_kept_delete(
        source_path=str(kept["iso"]), output_path=str(kept["output"]),
        required_check="round_trip",
    ))

    assert outcome.status == KEPT_STALE
    assert kept["iso"].exists()


def test_forget_history_while_a_completion_waits_records_nothing(
    env, dolphin, capsys, monkeypatch,
):
    """Forget history runs while a finished job's listener waits for the
    sweep lock: the entry it was about to write is not written after the
    clear (codex 4579636594)."""
    from services import job_manager as job_manager_module
    from services.job_manager import JobManager
    from services.lock_manager import LockManager, lock_manager
    from services.rules import sweep as rules_sweep

    monkeypatch.setattr(job_manager_module, "DELETE_READER_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(job_manager_module, "DELETE_READER_POLL_SECONDS", 0.05)
    iso = _iso(env)
    (env["vol"] / "out").mkdir()
    _store_rules(env, {"shelf": _rule(env)})
    reader = LockManager()
    reader._lock_dir = lock_manager._lock_dir
    original_release = JobManager._release_inputs
    taken = []

    def release_then_read(self, job_id):
        original_release(self, job_id)
        if not taken:
            taken.append(reader.acquire_dir_lock(str(iso.parent)))

    original_remember = rules_sweep._remember_kept_source

    async def forget_first(library, *args, **kwargs):
        # What the operator's Forget history does while this listener waits.
        await rules_sweep.forget_converted(library)
        return await original_remember(library, *args, **kwargs)

    monkeypatch.setattr(JobManager, "_release_inputs", release_then_read)
    monkeypatch.setattr(rules_sweep, "_remember_kept_source", forget_first)
    try:
        _cli(capsys, "run-rules", "--json")
    finally:
        reader.release_dir_lock(str(iso.parent))

    assert taken == [True]
    assert iso.exists()
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


# --------------------------------------------------------------------------- #
# The Library is part of the decision (Codex 4579589757)
# --------------------------------------------------------------------------- #


def _seed_kept(library, source="/v/Game.iso", output="/v/out/Game.rvz", pid="gc"):
    from services.rules import sweep as rules_sweep

    async def seed():
        state = await rules_sweep.get_state(library)
        entry = dict(state.get(pid) or {})
        entry[rules_sweep.KEPT_SOURCES] = {"Game.iso": {
            "source": source, "output": output, "attempts": 0,
            "decision": {}, "decision_generation": 0,
        }}
        state[pid] = entry
        await rules_sweep.store.write(rules_sweep.store.STATE_KEY, library, state)

    asyncio.run(seed())


def test_a_folder_library_root_change_drops_the_entry_without_deleting(
    env, dolphin, capsys, kept,
):
    """The Library now names another folder: the decision that kept the
    Source was made for the old one."""
    moved = env["vol"] / "moved"
    (moved / "gc").mkdir(parents=True)
    env["shelf"].root = moved

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0, lines
    assert kept["iso"].exists()
    assert not _rule_state(env)["gc"].get("kept_sources")


def test_a_source_outside_the_librarys_root_is_not_deleted(
    env, dolphin, capsys, kept, monkeypatch,
):
    """Checked fresh at retry: the Source must still be inside the Library as
    it is now, even when the recorded identity matches. The containment check
    alone keeps it: the delete step is never reached."""
    from services.job_manager import job_manager
    from services.rules import sweep as rules_sweep

    reached = []

    async def must_not_run(**kwargs):
        reached.append(kwargs)
        raise AssertionError("retry_kept_delete reached past the containment check")

    monkeypatch.setattr(job_manager, "retry_kept_delete", must_not_run)

    async def retarget_entry():
        state = await rules_sweep.get_state(env["shelf"])
        pending = state["gc"]["kept_sources"]["Game.iso"]
        elsewhere = env["vol"] / "attic" / "gc" / "Game.iso"
        elsewhere.write_bytes(kept["iso"].read_bytes())
        pending["source"] = str(elsewhere)
        await rules_sweep.store.write(rules_sweep.store.STATE_KEY, env["shelf"], state)
        return elsewhere

    elsewhere = asyncio.run(retarget_entry())

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0, lines
    assert elsewhere.exists()
    assert not _rule_state(env)["gc"].get("kept_sources")
    assert reached == []


def test_a_late_completion_from_a_former_library_identity_records_nothing(
    env, dolphin, capsys, monkeypatch,
):
    """The job was queued while the Library named one folder and ends after it
    names another: nothing is remembered to delete."""
    from services import job_manager as job_manager_module
    from services.job_manager import JobManager
    from services.lock_manager import LockManager, lock_manager

    monkeypatch.setattr(job_manager_module, "DELETE_READER_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(job_manager_module, "DELETE_READER_POLL_SECONDS", 0.05)
    iso = _iso(env)
    out_dir = env["vol"] / "out"
    out_dir.mkdir()
    _store_rules(env, {"shelf": _rule(env)})
    reader = LockManager()
    reader._lock_dir = lock_manager._lock_dir
    original = JobManager._release_inputs
    moved = env["vol"] / "moved"
    (moved / "gc").mkdir(parents=True)

    def release_then_read(self, job_id):
        original(self, job_id)
        reader.acquire_dir_lock(str(iso.parent))
        env["shelf"].root = moved  # the Library moves while the job runs

    monkeypatch.setattr(JobManager, "_release_inputs", release_then_read)
    try:
        _cli(capsys, "run-rules", "--json")
    finally:
        reader.release_dir_lock(str(iso.parent))

    assert iso.exists()
    assert not _rule_state(env)["gc"].get("kept_sources")


def test_every_history_invalidation_drops_pending_deletes(env, dolphin, capsys):
    """Every caller of ``forget_converted_locked`` (Forget history, a Rule's
    retarget through ``set_rules``, RomM's identity cleanup) leaves no
    ``kept_sources`` behind: one invalidation point."""
    from routes import romm as romm_routes
    from services.romm.library import romm_library
    from services.rules import sweep as rules_sweep

    _store_rules(env, {"shelf": _rule(env)})

    _seed_kept(env["shelf"])
    asyncio.run(rules_sweep.forget_converted(env["shelf"]))
    assert not _rule_state(env)["gc"].get("kept_sources"), "Forget history"

    _seed_kept(env["shelf"])
    elsewhere = env["vol"] / "elsewhere"
    elsewhere.mkdir()
    _store_rules(env, {"shelf": _rule(env, output_dir=str(elsewhere))})
    assert not _rule_state(env)["gc"].get("kept_sources"), "a retarget"

    _seed_kept(romm_library, pid="7")
    asyncio.run(romm_routes._run_identity_cleanup())
    state = asyncio.run(rules_sweep.get_state(romm_library))
    assert not (state.get("7") or {}).get("kept_sources"), "RomM's identity cleanup"


# --------------------------------------------------------------------------- #
# Every stage under the sweep lock is bounded (Codex 4579683118)
# --------------------------------------------------------------------------- #


def _stall_record_lookup(kept, monkeypatch, stall):
    from services.verification_store import VerificationStore

    real = VerificationStore._normalize
    monkeypatch.setattr(VerificationStore, "_normalize", staticmethod(
        lambda path: stall(path, str(kept["output"])) or real(path),
    ))


def _stall_module(name):
    def patch(kept, monkeypatch, stall):
        from services import job_manager as job_manager_module

        real = getattr(job_manager_module, name)
        monkeypatch.setattr(
            job_manager_module, name,
            lambda path, *a, **k: stall(path, str(kept["iso"])) or real(path, *a, **k),
        )
    return patch


def _stall_path_checks(kept, monkeypatch, stall):
    from services.job_manager import JobManager

    real = JobManager._delete_path_problem

    def stalled(self, job_id, paths, *args, **kwargs):
        stall(paths[0], str(kept["iso"]))
        return real(self, job_id, paths, *args, **kwargs)

    monkeypatch.setattr(JobManager, "_delete_path_problem", stalled)


def _stall_referrer_hook(kept, monkeypatch, stall):
    import services.referrers as hook_module

    real = hook_module._update_sync
    monkeypatch.setattr(
        hook_module, "_update_sync",
        lambda image, *a, **k: stall(image, str(kept["iso"])) or real(image, *a, **k),
    )


def _stall_unlink(kept, monkeypatch, stall):
    import os

    real = os.remove
    monkeypatch.setattr(
        os, "remove",
        lambda path, *a, **k: stall(path, str(kept["iso"])) or real(path, *a, **k),
    )


def _stall_store_clear(kept, monkeypatch, stall):
    from services.verification_store import VerificationStore

    real = VerificationStore._clear_sync
    monkeypatch.setattr(
        VerificationStore, "_clear_sync",
        lambda self, path: stall(path, str(kept["iso"])) or real(self, path),
    )


# (stage, how to stall it, the outcome once it gives up)
_STAGES = [
    ("record lookup", _stall_record_lookup, "kept"),
    ("Source snapshot", _stall_module("build_delete_snapshot"), "kept"),
    ("delete plan", _stall_module("build_delete_plan"), "kept"),
    ("path checks", _stall_path_checks, "kept"),
    ("Referrer hook", _stall_referrer_hook, "kept"),
    # An unlink is never abandoned with its locks: its outcome is unknown.
    ("unlink", _stall_unlink, "unknown"),
    # The Source is gone by then: the bookkeeping after it never undoes that.
    ("store clear", _stall_store_clear, "deleted"),
]


@pytest.mark.parametrize(("stage", "stall_it", "expected"), _STAGES,
                         ids=[s[0] for s in _STAGES])
def test_a_stage_that_stalls_is_given_up_within_the_bound(
    env, dolphin, capsys, kept, monkeypatch, stage, stall_it, expected,
):
    import os
    import time

    from services import job_manager as job_manager_module
    from services.job_manager import job_manager

    monkeypatch.setattr(job_manager_module, "KEPT_DELETE_PROBE_SECONDS", 0.3)

    def stall(path, target):
        if os.path.realpath(str(path)) == os.path.realpath(target):
            time.sleep(1.5)

    stall_it(kept, monkeypatch, stall)
    started = time.monotonic()
    outcome = asyncio.run(job_manager.retry_kept_delete(
        source_path=str(kept["iso"]), output_path=str(kept["output"]),
        required_check="container_only",
    ))

    assert time.monotonic() - started < 1.2, stage
    assert outcome.status == expected, (stage, outcome)
    time.sleep(1.5)  # let the abandoned thread finish before teardown


# --------------------------------------------------------------------------- #
# The same Source, however its path is spelled (Codex 4579683123)
# --------------------------------------------------------------------------- #


@pytest.fixture(name="symlinked_root")
def _symlinked_root(env):
    """The Library's root is a symlink to where its files really are, from
    before the job ran: the check record names the real path, the job the
    linked one."""
    real = env["vol"] / "shelf_real"
    (env["vol"] / "shelf").rename(real)
    (env["vol"] / "shelf").symlink_to(real, target_is_directory=True)
    return real


def test_a_source_under_a_symlinked_root_is_still_deleted(
    env, dolphin, capsys, symlinked_root, kept,
):
    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0, lines
    assert not (symlinked_root / "gc" / "Game.iso").exists()
    assert kept["output"].read_bytes() == b"converted output"
    assert not _rule_state(env)["gc"].get("kept_sources")


# --------------------------------------------------------------------------- #
# A mutation is never abandoned with its locks (Codex 4579725632)
# --------------------------------------------------------------------------- #


def _slow_unlink(monkeypatch, target, *, then_fail=False, seconds=1.0):
    """``os.remove`` on *target* waits, then removes it (or refuses)."""
    import os
    import time

    real = os.remove

    def slow(path, *args, **kwargs):
        if os.path.realpath(str(path)) == os.path.realpath(str(target)):
            time.sleep(seconds)
            if then_fail:
                raise PermissionError(13, "Permission denied", str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "remove", slow)


def _wait_for(condition, seconds=5.0):
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


def test_a_timed_out_unlink_keeps_its_locks_until_it_finishes(
    env, dolphin, capsys, kept, monkeypatch,
):
    """Codex's repro: the retry stops waiting on a slow unlink. Another job's
    read lock on the Source is refused until the unlink has finished, so the
    Source never disappears under a granted lock, and then the worker lets go
    of both locks."""
    import time

    from services import job_manager as job_manager_module
    from services.job_manager import KEPT_UNKNOWN, job_manager
    from services.lock_manager import LockManager, lock_manager

    monkeypatch.setattr(job_manager_module, "KEPT_DELETE_PROBE_SECONDS", 0.3)
    iso, output = kept["iso"], kept["output"]
    # Long enough that a reader's own brief retries (about half a second) end
    # well before the unlink does.
    _slow_unlink(monkeypatch, iso, seconds=3.0)
    reader = LockManager()
    reader._lock_dir = lock_manager._lock_dir

    started = time.monotonic()
    outcome = asyncio.run(job_manager.retry_kept_delete(
        source_path=str(iso), output_path=str(output), required_check="container_only",
    ))

    assert time.monotonic() - started < 0.9
    assert outcome.status == KEPT_UNKNOWN
    # The unlink is still running: no read lock is granted on the Source.
    assert iso.exists()
    granted = reader.acquire_input_lock(str(iso))
    assert granted is False or not iso.exists(), "a read lock on a Source still being removed"
    assert granted is False
    # Once it has finished, the worker let go of the Source and output locks.
    assert _wait_for(lambda: not iso.exists())
    assert _wait_for(lambda: not lock_manager.is_locked(str(output)))
    assert _wait_for(lambda: not reader.check_file_status(str(iso))[1])


def test_an_unknown_outcome_is_not_an_attempt_and_the_next_sweep_settles_it(
    env, dolphin, capsys, kept, monkeypatch,
):
    """The unlink outlives the sweep's wait: the entry is neither counted nor
    dropped. Here the unlink then fails, so the Source is still there, and the
    next sweep deletes it."""
    from services import job_manager as job_manager_module

    iso = kept["iso"]
    with monkeypatch.context() as patch:
        patch.setattr(job_manager_module, "KEPT_DELETE_PROBE_SECONDS", 0.3)
        _slow_unlink(patch, iso, then_fail=True)
        code, lines, _err = _cli(capsys, "run-rules", "--json")
        assert code == 0, lines
        pending = _rule_state(env)["gc"]["kept_sources"]["Game.iso"]
        assert pending["attempts"] == 0
        assert _wait_for(lambda: not _source_locked(iso))

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0, lines
    assert not iso.exists()
    assert not _rule_state(env)["gc"].get("kept_sources")


def test_an_unknown_outcome_whose_unlink_finished_is_settled_as_gone(
    env, dolphin, capsys, kept, monkeypatch,
):
    from services import job_manager as job_manager_module

    iso = kept["iso"]
    with monkeypatch.context() as patch:
        patch.setattr(job_manager_module, "KEPT_DELETE_PROBE_SECONDS", 0.3)
        _slow_unlink(patch, iso)
        _cli(capsys, "run-rules", "--json")
        assert _wait_for(lambda: not iso.exists())
        assert _wait_for(lambda: not _source_locked(iso))

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0, lines
    assert not iso.exists()
    assert not _rule_state(env)["gc"].get("kept_sources")


def _source_locked(path) -> bool:
    from services.lock_manager import lock_manager

    return lock_manager.check_file_status(str(path))[1]


# --------------------------------------------------------------------------- #
# A mutation that never started keeps its locks with the caller (Codex 4579801887)
# --------------------------------------------------------------------------- #


def _exhausted_probe_slots():
    """Every detached-probe slot taken, as when that many probes are stuck."""
    import contextlib

    from services import subprocess_runner

    @contextlib.contextmanager
    def held():
        taken = 0
        while subprocess_runner._probe_slots.acquire(blocking=False):
            taken += 1
        try:
            yield
        finally:
            for _ in range(taken):
                subprocess_runner._probe_slots.release()

    return held()


def test_a_mutation_that_cannot_start_hands_nothing_off():
    from services.subprocess_runner import MutationNotStarted, run_detached_mutation

    ran, handed = [], []

    async def attempt():
        with _exhausted_probe_slots():
            await run_detached_mutation(
                ran.append, "unlink", timeout=5,
                hand_off=lambda: handed.append(True) or (lambda: None),
            )

    with pytest.raises(MutationNotStarted):
        asyncio.run(attempt())
    assert ran == [] and handed == []


def test_an_undoable_call_that_cannot_start_undoes_nothing():
    from services.subprocess_runner import run_detached_undoable

    ran, undone = [], []

    async def attempt():
        with _exhausted_probe_slots():
            await run_detached_undoable(
                ran.append, "lock", timeout=5, undo=undone.append,
            )

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(attempt())
    assert ran == [] and undone == []


def test_an_unlink_that_cannot_start_is_a_counted_kept_attempt(
    env, dolphin, capsys, kept, monkeypatch,
):
    """The retry keeps its locks and lets go of them itself; nothing is
    deleted, and it is an attempt like any other, never "unknown"."""
    from services import job_manager as job_manager_module
    from services.lock_manager import lock_manager

    real = job_manager_module.run_detached_mutation

    async def starved(*args, **kwargs):
        with _exhausted_probe_slots():
            return await real(*args, **kwargs)

    monkeypatch.setattr(job_manager_module, "run_detached_mutation", starved)

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0, lines
    assert kept["iso"].exists()
    pending = _rule_state(env)["gc"]["kept_sources"]["Game.iso"]
    assert pending["attempts"] == 1
    assert not lock_manager.check_file_status(str(kept["iso"]))[1]
    assert not lock_manager.is_locked(str(kept["output"]))


def test_a_job_cancelled_during_its_unlink_keeps_its_output_locks_until_it_ends(
    env, dolphin, monkeypatch,
):
    """The job path's hand-off carries the output lock too (CodeRabbit
    4579839955): cancelled while its Source's unlink is still running, the job
    does not let go of its output lock in cleanup; the unlink's worker does,
    once the unlink has finished. Until then no other job may take it."""
    import os
    import threading
    import time

    from models import ConversionMode, DuplicateAction, JobCreateRequest
    from routes import convert as convert_routes
    from services.lock_manager import LockManager, lock_manager

    import cli

    cli._open_db(False)  # the verification store the job writes to
    iso = _iso(env)
    output = iso.with_suffix(".rvz")
    began = threading.Event()
    real_remove = os.remove

    def slow_remove(path, *args, **kwargs):
        if os.path.realpath(str(path)) == os.path.realpath(str(iso)):
            began.set()
            time.sleep(2.0)
        return real_remove(path, *args, **kwargs)

    monkeypatch.setattr(os, "remove", slow_remove)
    other = LockManager()
    other._lock_dir = lock_manager._lock_dir

    async def run():
        job = await convert_routes.create_job(JobCreateRequest(
            file_path=str(iso), mode=ConversionMode.DOLPHIN_RVZ,
            duplicate_action=DuplicateAction.SKIP, delete_on_verify=True,
            container_only=True,
        ))
        task = asyncio.create_task(convert_routes.job_manager._process_job(job.id))
        deadline = time.monotonic() + 20
        while not began.is_set():
            assert not task.done(), (job.status, job.error_message, job.message)
            assert time.monotonic() < deadline, (job.status, job.message)
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())

    # The unlink is still running: the output stays locked against others.
    assert iso.exists()
    assert other.acquire_lock(str(output), allow_existing=True) is False
    assert _wait_for(lambda: not iso.exists())
    assert _wait_for(lambda: not lock_manager.is_locked(str(output)))
    assert other.acquire_lock(str(output), allow_existing=True)
    other.release_lock(str(output))


def test_a_stalled_probe_in_lock_acquisition_never_blocks_the_event_loop(
    env, dolphin, capsys, kept, monkeypatch,
):
    """Codex 4579852997's repro: the Source's ``isfile`` stalls inside lock
    acquisition. The retry returns within its bound, and its cleanup (letting
    go of the output lock it already holds) never waits on the lock manager's
    mutex on the event loop: a heartbeat keeps ticking throughout."""
    import os
    import sys
    import time

    from services import job_manager as job_manager_module
    from services.job_manager import KEPT_KEPT, job_manager

    monkeypatch.setattr(job_manager_module, "KEPT_DELETE_PROBE_SECONDS", 0.1)
    iso = str(kept["iso"])
    real_isfile = os.path.isfile

    def stalled(path, *args, **kwargs):
        if (
            os.path.realpath(str(path)) == os.path.realpath(iso)
            and sys._getframe(1).f_code.co_name == "_try_file_lock"
        ):
            time.sleep(1.5)
        return real_isfile(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "isfile", stalled)

    async def run():
        gaps = []

        async def heartbeat():
            last = time.monotonic()
            while True:
                await asyncio.sleep(0)
                now = time.monotonic()
                gaps.append(now - last)
                last = now
                await asyncio.sleep(0.01)

        beat = asyncio.create_task(heartbeat())
        started = time.monotonic()
        outcome = await job_manager.retry_kept_delete(
            source_path=iso, output_path=str(kept["output"]),
            required_check="container_only",
        )
        elapsed = time.monotonic() - started
        beat.cancel()
        return outcome, elapsed, max(gaps or [0])

    outcome, elapsed, worst_gap = asyncio.run(run())

    assert outcome.status == KEPT_KEPT
    assert elapsed < 0.6, elapsed
    assert worst_gap < 0.5, worst_gap
    time.sleep(1.6)  # let the stalled probe finish before teardown
