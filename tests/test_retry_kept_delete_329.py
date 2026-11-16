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
import os

import pytest
from services.tools import registry

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
    return _keep(env, capsys, monkeypatch)


def _keep(env, capsys, monkeypatch, output_name="Game.rvz", **rule_changes):
    """Convert Game.iso under the delete Rule (with *rule_changes*) while
    another job reads its folder, so the Source is kept; return the paths."""
    from services import job_manager as job_manager_module
    from services.job_manager import SOURCE_KEPT_FOR_READERS, JobManager
    from services.lock_manager import LockManager, lock_manager

    monkeypatch.setattr(job_manager_module, "DELETE_READER_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(job_manager_module, "DELETE_READER_POLL_SECONDS", 0.05)
    iso = _iso(env)
    out_dir = env["vol"] / "out"
    out_dir.mkdir(exist_ok=True)
    _store_rules(env, {"shelf": {"gc": {
        "mode": "dolphin_rvz", "enabled": True, "delete_on_verify": True,
        "check_level": "container_only", "output_dir": str(out_dir), **rule_changes,
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
    assert pending["Game.iso"]["output"] == str(out_dir / output_name)
    return {"iso": iso, "output": out_dir / output_name, "reader": reader}


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


def _content_checking_verify(monkeypatch, seen=None, stall=0.0):
    """dolphin-tool's verify, reading the output as a real check would: only
    the bytes the conversion wrote pass. *seen* collects each verify, with
    whether the sweep lock was held; *stall* makes it outlast any bound."""
    from services.rules import sweep as rules_sweep

    service = registry.get("dolphin")._service

    async def verify(cmd, *, path, **kwargs):
        if seen is not None:
            seen.append((path, rules_sweep._sweep_lock.locked()))
        if stall:
            await asyncio.sleep(stall)
        with open(path, "rb") as handle:
            valid = handle.read() == b"converted output"
        yield {
            "type": "complete", "valid": valid,
            "message": "Disc image is Intact" if valid else "Disc is damaged",
        }

    monkeypatch.setattr(service._runner, "run_verify", verify)


def test_an_output_rewritten_in_place_keeps_the_source(env, dolphin, capsys, kept, monkeypatch):
    """Codex 4580346573: every output byte rewritten, same length, mtime put
    back, so size, mtime, inode and device all match the record. The retry
    re-runs the output's check, which fails: the Source is kept."""
    output = kept["output"]
    before = output.stat()
    with open(output, "r+b") as handle:
        handle.write(b"X" * before.st_size)
    os.utime(output, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert output.stat().st_ino == before.st_ino
    _content_checking_verify(monkeypatch)

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0, lines
    assert kept["iso"].exists()
    assert not _rule_state(env)["gc"].get("kept_sources")


def test_a_retry_re_verifies_the_output_outside_the_sweep_lock(
    env, dolphin, capsys, kept, monkeypatch,
):
    """An unchanged output passes its check again, run off the sweep lock,
    and the Source is deleted."""
    seen: list = []
    _content_checking_verify(monkeypatch, seen)

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0, lines
    assert not kept["iso"].exists()
    assert seen == [(str(kept["output"]), False)]


def test_a_re_verify_that_times_out_is_a_counted_attempt(
    env, dolphin, capsys, kept, monkeypatch,
):
    """The re-check runs under the tool's own verify bound; outlasting it is
    an attempt, and nothing is deleted."""
    tool = registry.for_mode("dolphin_rvz")

    async def short(path, *, cancel_event=None):
        return 0.2

    monkeypatch.setattr(tool, "verify_timeout", short)
    _content_checking_verify(monkeypatch, stall=5)

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0, lines
    assert kept["iso"].exists()
    pending = _rule_state(env)["gc"]["kept_sources"]["Game.iso"]
    assert pending["attempts"] == 1


def _checked_outputs(output) -> tuple[list[str], list[str]]:
    """Whether ``/api/verified`` and ``/api/verified/lookup`` list *output*
    as checked: the paths each answer names."""
    from routes import info as info_routes

    async def ask():
        listed = await info_routes.list_verified()
        looked_up = await info_routes.lookup_verified(
            info_routes.VerifiedLookup(paths=[str(output)]),
        )
        return listed["verified"], looked_up["verified"]

    return asyncio.run(ask())


def test_a_re_verify_that_finds_the_output_bad_records_it(
    env, dolphin, capsys, kept, monkeypatch,
):
    """#351: the retry re-checks an output corrupted in place and finds it
    bad. The Source is kept, and the failure is recorded where every badge,
    lookup and delete warning reads it (``mark_failed``, as a check Rule
    records one): the output is no longer listed as checked."""
    output = kept["output"]
    assert _checked_outputs(output) == ([str(output)], [str(output)])
    before = output.stat()
    with open(output, "r+b") as handle:
        handle.write(b"X" * before.st_size)
    os.utime(output, ns=(before.st_atime_ns, before.st_mtime_ns))
    _content_checking_verify(monkeypatch)

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0, lines
    assert kept["iso"].exists()
    assert _checked_outputs(output) == ([], [])


def test_a_re_verify_that_times_out_leaves_the_record_as_it_was(
    env, dolphin, capsys, kept, monkeypatch,
):
    """No verdict is not a bad result: the output stays listed as checked."""
    tool = registry.for_mode("dolphin_rvz")

    async def short(path, *, cancel_event=None):
        return 0.2

    monkeypatch.setattr(tool, "verify_timeout", short)
    _content_checking_verify(monkeypatch, stall=5)

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0, lines
    assert kept["iso"].exists()
    output = str(kept["output"])
    assert _checked_outputs(kept["output"]) == ([output], [output])


def test_a_manual_verify_that_finds_the_output_bad_records_it(
    env, dolphin, capsys, kept, monkeypatch,
):
    """#351, the same rule for every re-check: the verify route's bad verdict
    marks the output failed, as the CLI and a check Rule do."""
    from routes import info as info_routes

    output = kept["output"]
    dolphin["valid"] = False

    result = asyncio.run(info_routes.verify_dolphin(path=str(output)))

    assert result["valid"] is False
    assert _checked_outputs(output) == ([], [])


def test_a_manual_verify_that_times_out_leaves_the_record_as_it_was(
    env, dolphin, capsys, kept, monkeypatch,
):
    from routes import info as info_routes

    tool = registry.get("dolphin")

    async def short(path, *, cancel_event=None):
        return 0.2

    monkeypatch.setattr(tool, "verify_timeout", short)
    _content_checking_verify(monkeypatch, stall=5)
    output = str(kept["output"])

    result = asyncio.run(info_routes.verify_dolphin(path=output))

    assert result.get("timed_out") is True
    assert _checked_outputs(kept["output"]) == ([output], [output])


def test_a_round_trip_rule_runs_the_round_trip_again(env, dolphin, capsys, monkeypatch):
    """A Rule whose check is the Round-trip has it re-run on the retry, by the
    same function the job uses."""
    from services import job_manager as job_manager_module
    from services.round_trip import RoundTripOutcome

    compared: list = []

    async def round_trip(tool, source, output, **kwargs):
        compared.append((source, output))
        return RoundTripOutcome(passed=True)

    monkeypatch.setattr(job_manager_module, "round_trip", round_trip)
    kept = _keep(env, capsys, monkeypatch, check_level="round_trip")
    assert len(compared) == 1  # the job's own

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0, lines
    assert compared[1:] == [(str(kept["iso"]), str(kept["output"]))]
    assert not kept["iso"].exists()


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


def _seed_deleting(env, library, pid="gc"):
    """A pending delete whose unlinks began: one file removed, one left."""
    from services.rules import sweep as rules_sweep

    left = env["shelf"].root / "gc" / "Game.iso"
    left.write_bytes(b"the disc")
    source, output = str(left), str(env["vol"] / "out" / "Game.rvz")

    async def seed():
        state = await rules_sweep.get_state(library)
        entry = dict(state.get(pid) or {})
        entry[rules_sweep.KEPT_SOURCES] = {"Game.iso": {
            "source": source, "output": output, "attempts": 0,
            "decision": {}, "decision_generation": 0,
            "state": rules_sweep.KEPT_DELETING, "planned": [source],
        }}
        state[pid] = entry
        await rules_sweep.store.write(rules_sweep.store.STATE_KEY, library, state)

    asyncio.run(seed())
    return source


def _started_delete_findings(library_id: str) -> list[dict]:
    from services import findings

    return [
        f for f in findings.list_findings()
        if f["kind"] == findings.SOURCE_KEPT and f["library_id"] == library_id
    ]


def test_a_rule_edit_reports_a_started_delete_it_drops(env, dolphin, capsys):
    """Codex 4580328581: saving the Rule with its delete off while a Source is
    half deleted drops the entry, and says which files are left."""
    from services import findings

    _store_rules(env, {"shelf": _rule(env)})
    left = _seed_deleting(env, env["shelf"])

    _store_rules(env, {"shelf": _rule(env, delete_on_verify=False)})

    assert not _rule_state(env)["gc"].get("kept_sources")
    reported = _started_delete_findings("shelf")
    assert len(reported) == 1
    assert reported[0]["cause"][findings.REMAINING] == [left]
    assert "part deleted" in reported[0]["reason"]


def test_forget_history_reports_a_started_delete_it_drops(env, dolphin, capsys):
    from services import findings
    from services.rules import sweep as rules_sweep

    _store_rules(env, {"shelf": _rule(env)})
    left = _seed_deleting(env, env["shelf"])

    asyncio.run(rules_sweep.forget_converted(env["shelf"]))

    assert not _rule_state(env)["gc"].get("kept_sources")
    reported = _started_delete_findings("shelf")
    assert [f["cause"][findings.REMAINING] for f in reported] == [[left]]


def test_removing_a_library_reports_its_started_deletes_under_no_library(
    env, dolphin, capsys,
):
    """The removed Library's Findings go with it, so a started delete it held
    is reported under no Library, and outlives the removal."""
    from services import findings
    from services.rules import sweep as rules_sweep

    _store_rules(env, {"shelf": _rule(env)})
    left = _seed_deleting(env, env["shelf"])

    # What `libraries.remove` does: the Rule state, then the Library's Findings.
    asyncio.run(rules_sweep.forget_library(env["shelf"]))
    findings.forget_library("shelf")

    assert not _rule_state(env).get("gc")
    reported = _started_delete_findings(findings.NO_LIBRARY)
    assert [f["cause"][findings.REMAINING] for f in reported] == [[left]]


def test_romm_identity_cleanup_reports_a_started_delete_under_no_library(
    env, dolphin, capsys,
):
    """RomM's identity cleanup drops its own Source-kept Findings (their ROM
    ids name other games now), so a started delete goes under no Library."""
    from routes import romm as romm_routes
    from services import findings
    from services.romm.library import romm_library

    _store_rules(env, {"shelf": _rule(env)})
    left = _seed_deleting(env, romm_library, pid="7")

    asyncio.run(romm_routes._run_identity_cleanup())

    reported = _started_delete_findings(findings.NO_LIBRARY)
    assert [f["cause"][findings.REMAINING] for f in reported] == [[left]]


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


def test_an_unlink_that_raises_its_own_timeout_error_is_a_counted_attempt(
    env, dolphin, capsys, kept, monkeypatch,
):
    """A network filesystem's ETIMEDOUT from the unlink is the unlink's own
    failure, not the retry's deadline (codex 4579904147): a counted kept
    attempt, never "unknown", so the cap and the Finding stay reachable."""
    import errno
    import os

    iso = kept["iso"]
    real = os.remove

    def etimedout(path, *args, **kwargs):
        if os.path.realpath(str(path)) == os.path.realpath(str(iso)):
            raise TimeoutError(errno.ETIMEDOUT, "Operation timed out", str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "remove", etimedout)

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0, lines
    assert iso.exists()
    pending = _rule_state(env)["gc"]["kept_sources"]["Game.iso"]
    assert pending["attempts"] == 1
    assert "timed out" in pending["last_reason"]


# --------------------------------------------------------------------------- #
# Letting go of locks never stalls the sweep (Codex 4579986280)
# --------------------------------------------------------------------------- #


def test_a_stalled_lock_release_neither_holds_the_sweep_nor_the_pool(
    env, dolphin, capsys, kept, monkeypatch,
):
    """The lock directory stops answering while the retry lets go of its
    locks: the retry returns within its bound, the event loop keeps ticking,
    the shared threadpool is never asked, and the background worker still
    closes every handle once the directory answers."""
    import time

    from services import job_manager as job_manager_module
    from services.job_manager import KEPT_DELETED, job_manager
    from services.lock_manager import LockManager, lock_manager

    monkeypatch.setattr(job_manager_module, "KEPT_DELETE_PROBE_SECONDS", 0.2)
    real_unlink_then_close = LockManager._unlink_then_close

    def stalled(self, handle, *args, **kwargs):
        time.sleep(1.0)
        return real_unlink_then_close(self, handle, *args, **kwargs)

    monkeypatch.setattr(LockManager, "_unlink_then_close", stalled)
    pooled = []
    real_pool = job_manager_module.run_in_threadpool

    async def watched_pool(func, *args, **kwargs):
        pooled.append(getattr(func, "__name__", repr(func)))
        return await real_pool(func, *args, **kwargs)

    monkeypatch.setattr(job_manager_module, "run_in_threadpool", watched_pool)

    async def run():
        gaps = []

        async def heartbeat():
            last = time.monotonic()
            while True:
                await asyncio.sleep(0.01)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        beat = asyncio.create_task(heartbeat())
        started = time.monotonic()
        outcome = await job_manager.retry_kept_delete(
            source_path=str(kept["iso"]), output_path=str(kept["output"]),
            required_check="container_only",
        )
        elapsed = time.monotonic() - started
        beat.cancel()
        return outcome, elapsed, max(gaps or [0])

    outcome, elapsed, worst_gap = asyncio.run(run())

    assert outcome.status == KEPT_DELETED
    assert not kept["iso"].exists()
    # Two releases (the Source's hold, the output lock), each bounded.
    assert elapsed < 0.9, elapsed
    assert worst_gap < 0.5, worst_gap
    assert "_let_go" not in pooled
    # The worker finishes on its own: every lock is let go in the end.
    assert _wait_for(lambda: not lock_manager.is_locked(str(kept["output"])))
    assert _wait_for(lambda: not lock_manager.check_file_status(str(kept["iso"]))[1])


# --------------------------------------------------------------------------- #
# The retry asks the sweep's own plan (Codex security review 4580012814)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("narrowing", [
    {"exclude_pattern": r"Game\.iso$"},
    {"include_pattern": r"^Other"},
    {"min_size_mb": 1},
    {"only_matched": True},
], ids=["exclude", "include", "size", "match"])
def test_a_rule_that_no_longer_selects_the_source_keeps_it(
    env, dolphin, capsys, kept, narrowing,
):
    """The Rule now leaves this Source out: the retry re-runs the sweep's own
    selection and plan for it, finds it no longer converted, and drops the
    entry without deleting."""
    _store_rules(env, {"shelf": _rule(env, **narrowing)})

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0, lines
    assert kept["iso"].exists()
    assert not _rule_state(env)["gc"].get("kept_sources")


# --------------------------------------------------------------------------- #
# A renamed output is still this Rule's output (Codex 4580078019)
# --------------------------------------------------------------------------- #


def _renaming_shelf(env):
    """The shelf may use the rename policy (it keeps converted history), and
    the base output name is already taken by something else."""
    from services.rules.library import CONVERTED_HISTORY

    env["shelf"].capabilities = frozenset({CONVERTED_HISTORY})
    out_dir = env["vol"] / "out"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "Game.rvz").write_bytes(b"someone else's file")


def test_a_renamed_output_is_retried_and_deleted(env, dolphin, capsys, monkeypatch):
    _renaming_shelf(env)
    kept = _keep(
        env, capsys, monkeypatch, output_name="Game_1.rvz", duplicate_action="rename",
    )

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0, lines
    assert not kept["iso"].exists()
    assert kept["output"].read_bytes() == b"converted output"
    assert not _rule_state(env)["gc"].get("kept_sources")


def test_an_unrelated_numbered_file_is_not_this_rules_output(
    env, dolphin, capsys, monkeypatch,
):
    """Only the numbered sibling of the planned output passes as renamed."""
    from services.job_manager import job_manager
    from services.rules import sweep as rules_sweep

    _renaming_shelf(env)
    kept = _keep(
        env, capsys, monkeypatch, output_name="Game_1.rvz", duplicate_action="rename",
    )
    reached = []

    async def must_not_run(**kwargs):
        reached.append(kwargs)
        raise AssertionError("retry_kept_delete reached for an unrelated output")

    async def point_elsewhere():
        state = await rules_sweep.get_state(env["shelf"])
        state["gc"]["kept_sources"]["Game.iso"]["output"] = str(
            env["vol"] / "out" / "Other_1.rvz",
        )
        await rules_sweep.store.write(rules_sweep.store.STATE_KEY, env["shelf"], state)

    asyncio.run(point_elsewhere())
    monkeypatch.setattr(job_manager, "retry_kept_delete", must_not_run)

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0, lines
    assert kept["iso"].exists()
    assert reached == []
    assert not _rule_state(env)["gc"].get("kept_sources")


def test_a_policy_changed_away_from_rename_drops_the_entry(
    env, dolphin, capsys, monkeypatch,
):
    _renaming_shelf(env)
    kept = _keep(
        env, capsys, monkeypatch, output_name="Game_1.rvz", duplicate_action="rename",
    )
    _store_rules(env, {"shelf": _rule(env, duplicate_action="skip")})

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0, lines
    assert kept["iso"].exists()
    assert not _rule_state(env)["gc"].get("kept_sources")


# --------------------------------------------------------------------------- #
# A late Finding write is bound to its Library (Codex 4580078026)
# --------------------------------------------------------------------------- #


def test_a_late_finding_write_after_the_library_is_forgotten_writes_nothing(
    env, dolphin, capsys, kept, monkeypatch,
):
    """The ``source_kept`` Finding's write outlives the sweep's wait; the
    Library is forgotten meanwhile: when the write finally runs, nothing
    appears."""
    import threading
    import time

    from services import findings, subprocess_runner
    from services.rules import sweep as rules_sweep

    monkeypatch.setattr(rules_sweep, "KEPT_DELETE_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(subprocess_runner, "_STAT_TIMEOUT", 0.2)
    real_observe = findings.observe
    release = threading.Event()
    finished = threading.Event()

    def late_observe(*args, **kwargs):
        release.wait(10)
        try:
            return real_observe(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(findings, "observe", late_observe)
    reader, iso = kept["reader"], kept["iso"]
    assert reader.acquire_dir_lock(str(iso.parent))
    try:
        _cli(capsys, "run-rules", "--json")
    finally:
        reader.release_dir_lock(str(iso.parent))

    asyncio.run(rules_sweep.forget_library(env["shelf"]))
    release.set()
    assert finished.wait(10)
    time.sleep(0.1)

    assert not [f for f in findings.list_findings() if f["kind"] == findings.SOURCE_KEPT]


# --------------------------------------------------------------------------- #
# Companion outputs are part of the output (CodeRabbit 4580102953)
# --------------------------------------------------------------------------- #


@pytest.fixture(name="with_companion")
def _with_companion(dolphin, monkeypatch):
    """The dolphin mode writes a ``.bin`` companion beside its output, like
    extractcd's ``.cue`` + ``.bin``."""
    from pathlib import Path

    tool = registry.get("dolphin")
    service = tool._service
    real_run = service._runner.run

    async def run_with_companion(cmd, *, input_path, output_path, **kwargs):
        async for update in real_run(
            cmd, input_path=input_path, output_path=output_path, **kwargs,
        ):
            yield update
        Path(output_path).with_suffix(".bin").write_bytes(b"companion data")

    def companions(output_path, mode):
        bin_path = str(Path(output_path).with_suffix(".bin"))
        return [bin_path] if os.path.exists(bin_path) else []

    monkeypatch.setattr(service._runner, "run", run_with_companion)
    monkeypatch.setitem(vars(tool), "companion_outputs", companions)


@pytest.mark.parametrize("change", ["changed", "removed"])
def test_a_companion_output_changed_since_its_check_keeps_the_source(
    env, dolphin, with_companion, capsys, monkeypatch, change,
):
    kept = _keep(env, capsys, monkeypatch)
    companion = kept["output"].with_suffix(".bin")
    assert companion.exists()
    if change == "changed":
        companion.write_bytes(b"something else entirely")
    else:
        companion.unlink()

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0, lines
    assert kept["iso"].exists()
    assert not _rule_state(env)["gc"].get("kept_sources")


def test_an_untouched_companion_output_is_deleted_with_its_source(
    env, dolphin, with_companion, capsys, monkeypatch,
):
    kept = _keep(env, capsys, monkeypatch)

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0, lines
    assert not kept["iso"].exists()
    assert kept["output"].with_suffix(".bin").exists()


# --------------------------------------------------------------------------- #
# A started delete is one unit: it finishes or is reported (Codex 4580176201)
# --------------------------------------------------------------------------- #


@pytest.fixture(name="two_file_source")
def _two_file_source(monkeypatch):
    """The dolphin Source is two files, like a split dump: ``Game.iso`` and
    ``Game.iso.part2`` beside it (the registry's ``source_companions``)."""
    tool = registry.get("dolphin")

    def companions(path):
        part = path + ".part2"
        return [part] if os.path.exists(part) else []

    monkeypatch.setitem(vars(tool), "source_companions", companions)


def _part2(env):
    part = env["shelf"].root / "gc" / "Game.iso.part2"
    part.write_bytes(b"the second part")
    return part


def test_an_unlink_set_that_outlives_the_deadline_is_finished(
    env, dolphin, two_file_source, capsys, monkeypatch,
):
    """Codex's repro: the first file's unlink outlives the retry's wait. The
    whole remaining set goes to the worker, which removes every planned file;
    the next sweep finds the delete finished and clears the entry."""
    from services import job_manager as job_manager_module

    part = _part2(env)
    kept = _keep(env, capsys, monkeypatch)
    with monkeypatch.context() as patch:
        patch.setattr(job_manager_module, "KEPT_DELETE_PROBE_SECONDS", 0.3)
        _slow_unlink(patch, part)
        _cli(capsys, "run-rules", "--json")
        pending = _rule_state(env)["gc"]["kept_sources"]["Game.iso"]
        assert pending["state"] == "deleting"
        assert _wait_for(lambda: not part.exists() and not kept["iso"].exists())
        assert _wait_for(lambda: not _source_locked(kept["iso"]))

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0, lines
    assert not part.exists() and not kept["iso"].exists()
    assert not _rule_state(env)["gc"].get("kept_sources")


def test_a_part_deleted_source_whose_output_changed_is_kept_and_reported(
    env, dolphin, two_file_source, capsys, monkeypatch,
):
    """The worker stops part way (the slow unlink then fails), and the output
    changes before the next sweep: the files left are kept, the entry dropped,
    and a Finding says the Source is part deleted."""
    from services import findings
    from services import job_manager as job_manager_module

    part = _part2(env)
    kept = _keep(env, capsys, monkeypatch)
    with monkeypatch.context() as patch:
        patch.setattr(job_manager_module, "KEPT_DELETE_PROBE_SECONDS", 0.3)
        _slow_unlink(patch, part, then_fail=True)
        _cli(capsys, "run-rules", "--json")
        assert _wait_for(lambda: not _source_locked(kept["iso"]))
    kept["output"].write_bytes(b"something else entirely")

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0, lines
    assert part.exists() and kept["iso"].exists()
    assert not _rule_state(env)["gc"].get("kept_sources")
    reported = [f for f in findings.list_findings() if f["kind"] == findings.SOURCE_KEPT]
    assert len(reported) == 1
    assert "part" in reported[0]["reason"]


def test_a_job_cancelled_during_its_unlinks_still_removes_every_planned_file(
    env, dolphin, two_file_source, monkeypatch,
):
    import threading
    import time

    import cli
    from models import ConversionMode, DuplicateAction, JobCreateRequest
    from routes import convert as convert_routes

    cli._open_db(False)
    iso = _iso(env)
    part = _part2(env)
    began = threading.Event()
    real_remove = os.remove

    def slow_remove(path, *args, **kwargs):
        if os.path.realpath(str(path)) == os.path.realpath(str(part)):
            began.set()
            time.sleep(1.0)
        return real_remove(path, *args, **kwargs)

    monkeypatch.setattr(os, "remove", slow_remove)

    async def run():
        job = await convert_routes.create_job(JobCreateRequest(
            file_path=str(iso), mode=ConversionMode.DOLPHIN_RVZ,
            duplicate_action=DuplicateAction.SKIP, delete_on_verify=True,
            container_only=True,
        ))
        task = asyncio.create_task(convert_routes.job_manager._process_job(job.id))
        deadline = time.monotonic() + 20
        while not began.is_set():
            assert not task.done(), (job.status, job.error_message)
            assert time.monotonic() < deadline
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())

    assert _wait_for(lambda: not part.exists() and not iso.exists())


# --------------------------------------------------------------------------- #
# Once a file is removed, the delete has started (Codex 4580227623)
# --------------------------------------------------------------------------- #


def _refuse_unlink(monkeypatch, target):
    """``os.remove`` refuses *target* at once (a read-only primary)."""
    real = os.remove

    def refuse(path, *args, **kwargs):
        if os.path.realpath(str(path)) == os.path.realpath(str(target)):
            raise PermissionError(13, "Permission denied", str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "remove", refuse)


def test_a_delete_that_fails_after_a_file_is_removed_is_finished_later(
    env, dolphin, two_file_source, capsys, monkeypatch,
):
    part = _part2(env)
    kept = _keep(env, capsys, monkeypatch)
    with monkeypatch.context() as patch:
        _refuse_unlink(patch, kept["iso"])
        _cli(capsys, "run-rules", "--json")
        assert not part.exists() and kept["iso"].exists()
        pending = _rule_state(env)["gc"]["kept_sources"]["Game.iso"]
        assert pending["state"] == "deleting"
        assert pending["planned"] == [str(kept["iso"])]

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0, lines
    assert not kept["iso"].exists()
    assert not _rule_state(env)["gc"].get("kept_sources")


def test_a_started_delete_whose_output_then_changed_is_reported(
    env, dolphin, two_file_source, capsys, monkeypatch,
):
    from services import findings

    part = _part2(env)
    kept = _keep(env, capsys, monkeypatch)
    with monkeypatch.context() as patch:
        _refuse_unlink(patch, kept["iso"])
        _cli(capsys, "run-rules", "--json")
    kept["output"].write_bytes(b"something else entirely")

    _cli(capsys, "run-rules", "--json")

    assert not part.exists() and kept["iso"].exists()
    assert not _rule_state(env)["gc"].get("kept_sources")
    reported = [f for f in findings.list_findings() if f["kind"] == findings.SOURCE_KEPT]
    assert len(reported) == 1 and "part" in reported[0]["reason"]
    # The Source is its path, and every file left is named (CodeRabbit
    # 4580360093), as the hand-started reporter does.
    assert reported[0]["path"] == str(kept["iso"])
    assert reported[0]["cause"][findings.REMAINING] == [str(kept["iso"])]


def _hand_started_part_delete(env, monkeypatch):
    """Run a hand-started job whose delete removes one file, then fails.
    Returns the job, the Source's kept file and its removed part."""
    import cli
    from models import ConversionMode, DuplicateAction, JobCreateRequest
    from routes import convert as convert_routes

    cli._open_db(False)
    iso, part = _iso(env), _part2(env)
    _refuse_unlink(monkeypatch, iso)

    async def run():
        job = await convert_routes.create_job(JobCreateRequest(
            file_path=str(iso), mode=ConversionMode.DOLPHIN_RVZ,
            duplicate_action=DuplicateAction.SKIP, delete_on_verify=True,
            container_only=True,
        ))
        await convert_routes.job_manager._process_job(job.id)
        return job

    return asyncio.run(run()), iso, part


def test_a_job_whose_delete_fails_part_way_is_reported(
    env, dolphin, two_file_source, monkeypatch,
):
    """A hand-started job (no Rule to keep a pending delete) that removed a
    file and then failed files a Finding naming the part-deleted Source."""
    from models import JobStatus
    from services import findings

    job, iso, part = _hand_started_part_delete(env, monkeypatch)

    assert job.status == JobStatus.FAILED
    assert not part.exists() and iso.exists()
    assert "part deleted" in (job.error_message or "")
    reported = [f for f in findings.list_findings() if f["kind"] == findings.SOURCE_KEPT]
    assert [f["path"] for f in reported] == [str(iso)]


def test_the_findings_view_handles_a_finding_with_no_library(
    env, dolphin, two_file_source, monkeypatch,
):
    """A hand-started job's Finding belongs to no Library. The Findings view
    lists it as started by hand, can dismiss it and show it again, and
    removing a Library or a RomM identity leaves it where it is."""
    from routes import findings as routes
    from services import findings

    _, iso, _ = _hand_started_part_delete(env, monkeypatch)

    async def view():
        listed = await routes.list_findings()
        row = next(r for r in listed["findings"] if r["kind"] == findings.SOURCE_KEPT)
        assert row["library_name"] == "Started by hand"
        assert row["folder"] == str(iso.parent)
        ref = routes.FindingRef(fingerprint=row["fingerprint"])
        await routes.dismiss_finding(row["id"], ref)
        assert (await routes.list_findings())["dismissed"] == 1
        await routes.undismiss_finding(row["id"], ref)
        assert (await routes.list_findings())["dismissed"] == 0
        return row

    row = asyncio.run(view())
    findings.forget_library("folder:1")
    findings.forget_kind("romm", findings.SOURCE_KEPT)

    assert [f["id"] for f in findings.list_findings()] == [row["id"]]


def _view_kept(routes, findings) -> list[dict]:
    listed = asyncio.run(routes.list_findings())["findings"]
    return [r for r in listed if r["kind"] == findings.SOURCE_KEPT]


def test_a_hand_started_finding_clears_once_its_remaining_files_are_gone(
    env, dolphin, two_file_source, monkeypatch,
):
    """The Findings view settles a hand-started Finding as it loads: kept
    while any file the delete left is there, gone once none is."""
    from routes import findings as routes
    from services import findings

    _, iso, _ = _hand_started_part_delete(env, monkeypatch)
    (iso.parent / "Other.iso").write_bytes(b"x")  # the folder is still listed

    assert len(_view_kept(routes, findings)) == 1
    iso.unlink()
    assert _view_kept(routes, findings) == []
    assert findings.list_findings() == []


def test_a_hand_started_finding_clears_once_its_source_is_gone(
    env, dolphin, two_file_source, monkeypatch,
):
    """A Finding whose Source no longer exists is settled, even if a file the
    delete planned is somehow still there."""
    from routes import findings as routes
    from services import findings

    _, iso, part = _hand_started_part_delete(env, monkeypatch)
    part.write_bytes(b"put back")
    iso.unlink()

    assert _view_kept(routes, findings) == []


def test_settling_hand_started_findings_is_bounded(
    env, dolphin, two_file_source, monkeypatch,
):
    """A volume that does not answer cannot hold the Findings view: the
    settle gives up at its bound and the Finding is listed as it was."""
    import threading
    import time

    from routes import findings as routes
    from services import findings

    _, iso, _ = _hand_started_part_delete(env, monkeypatch)
    release = threading.Event()
    real = os.path.lexists

    def hung(path):
        if str(path) == str(iso):
            release.wait(10)
        return real(path)

    monkeypatch.setattr(routes, "_SETTLE_SECONDS", 0.2)
    monkeypatch.setattr(findings.os.path, "lexists", hung)
    started = time.monotonic()
    try:
        assert len(_view_kept(routes, findings)) == 1
        assert time.monotonic() - started < 5
    finally:
        release.set()


# --------------------------------------------------------------------------- #
# A cancel during a started delete (CodeRabbit 4580360083, 4580360089, and the
# outside-diff note on job_manager.py:3362)
# --------------------------------------------------------------------------- #


def _hold_unlink(monkeypatch, target):
    """``os.remove`` of *target* waits for ``go``, then refuses it. Returns
    the ``reached`` and ``go`` events."""
    import threading

    reached, go = threading.Event(), threading.Event()
    real = os.remove

    def held(path, *args, **kwargs):
        if os.path.realpath(str(path)) == os.path.realpath(str(target)):
            reached.set()
            go.wait(10)
            raise PermissionError(13, "Permission denied", str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "remove", held)
    return reached, go


async def _cancel_once(task, reached) -> None:
    while not reached.is_set():
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_letting_go_of_locks_survives_an_immediate_cancel(monkeypatch):
    """The release worker starts before the first await: a cancel that lands
    at once still has every lock let go."""
    import threading

    from services import job_manager as job_manager_module

    released, seen = threading.Event(), []

    def let_go(paths):
        seen.extend(paths)
        released.set()

    monkeypatch.setattr(job_manager_module, "_let_go", let_go)

    async def body():
        asyncio.current_task().cancel()
        await job_manager_module.JobManager._let_go_in_background(["/v/out/Game.rvz"])

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(body())
    assert released.wait(2)
    assert seen == ["/v/out/Game.rvz"]


def test_a_job_cancelled_mid_delete_records_it_and_ends_cancelled(
    env, dolphin, two_file_source, monkeypatch,
):
    import cli
    from models import ConversionMode, DuplicateAction, JobCreateRequest, JobStatus
    from routes import convert as convert_routes
    from services import findings

    cli._open_db(False)
    iso, part = _iso(env), _part2(env)
    reached, go = _hold_unlink(monkeypatch, iso)

    async def run():
        job = await convert_routes.create_job(JobCreateRequest(
            file_path=str(iso), mode=ConversionMode.DOLPHIN_RVZ,
            duplicate_action=DuplicateAction.SKIP, delete_on_verify=True,
            container_only=True,
        ))
        task = asyncio.ensure_future(convert_routes.job_manager._process_job(job.id))
        try:
            await _cancel_once(task, reached)
        finally:
            go.set()
        return job

    job = asyncio.run(run())

    assert not part.exists() and iso.exists()
    assert job.status == JobStatus.CANCELLED
    assert job.id in convert_routes.job_manager._terminal_announced
    reported = [f for f in findings.list_findings() if f["kind"] == findings.SOURCE_KEPT]
    assert [f["library_id"] for f in reported] == [findings.NO_LIBRARY]
    assert reported[0]["cause"][findings.REMAINING] == [str(iso)]


def test_a_retry_cancelled_mid_delete_is_recorded_as_deleting(
    env, dolphin, two_file_source, capsys, monkeypatch,
):
    from services.rules import sweep as rules_sweep

    part = _part2(env)
    kept = _keep(env, capsys, monkeypatch)
    reached, go = _hold_unlink(monkeypatch, kept["iso"])

    async def run():
        task = asyncio.ensure_future(rules_sweep.sweep(env["shelf"]))
        try:
            await _cancel_once(task, reached)
        finally:
            go.set()

    asyncio.run(run())

    assert not part.exists() and kept["iso"].exists()
    pending = _rule_state(env)["gc"]["kept_sources"]["Game.iso"]
    assert pending["state"] == "deleting"
    assert str(kept["iso"]) in pending["planned"]


# --------------------------------------------------------------------------- #
# Settling Findings (CodeRabbit 4580360066, 4580360075)
# --------------------------------------------------------------------------- #


def test_findings_loads_share_one_stuck_settle(env, dolphin, two_file_source, monkeypatch):
    """Ten loads while the settle is stuck on a dead path take one detached
    slot between them, not ten."""
    import threading

    from routes import findings as routes
    from services import findings

    _, iso, _ = _hand_started_part_delete(env, monkeypatch)
    release, stuck = threading.Event(), []
    real = os.path.lexists

    def hung(path):
        if str(path) == str(iso):
            stuck.append(path)
            release.wait(10)
        return real(path)

    monkeypatch.setattr(routes, "_SETTLE_SECONDS", 0.05)
    monkeypatch.setattr(findings.os.path, "lexists", hung)

    async def ten_loads():
        for _ in range(10):
            await routes.list_findings()

    try:
        asyncio.run(ten_loads())
        assert len(stuck) == 1
    finally:
        release.set()


def test_clear_gone_keeps_a_finding_updated_while_it_looked(env, dolphin, monkeypatch):
    """The files are looked at outside the write lock; a Finding a producer
    updated meanwhile is not the one that was seen gone, and stays."""
    import cli
    from services import findings

    cli._open_db(False)
    folder = env["shelf"].root / "gc"
    (folder / "Other.iso").write_bytes(b"x")  # the folder is listed
    gone = str(folder / "Gone.iso")

    def kept(output):
        return findings.Observed(
            kind=findings.SOURCE_KEPT, image_key=gone, path=gone, reason="kept",
            cause={"source": gone, "output": output},
        )

    findings.observe("shelf", kept(None))
    real = os.path.lexists

    def updated_meanwhile(path):
        if str(path) == gone:
            findings.observe("shelf", kept("/v/out/Gone.rvz"))
        return real(path)

    monkeypatch.setattr(findings.os.path, "lexists", updated_meanwhile)

    assert findings.clear_gone("shelf", findings.SOURCE_KEPT) == 0
    rows = findings.list_findings()
    assert [r["cause"]["output"] for r in rows] == ["/v/out/Gone.rvz"]


def _hold_first_refuse_second(monkeypatch, first, second):
    """``os.remove`` of *first* waits for ``go`` and then goes through; of
    *second*, it is refused. Returns the ``reached`` and ``go`` events."""
    import threading

    reached, go = threading.Event(), threading.Event()
    real = os.remove

    def unlink(path, *args, **kwargs):
        real_path = os.path.realpath(str(path))
        if real_path == os.path.realpath(str(first)):
            reached.set()
            go.wait(10)
        elif real_path == os.path.realpath(str(second)):
            raise PermissionError(13, "Permission denied", str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "remove", unlink)
    return reached, go


def test_a_job_cancelled_during_its_first_unlink_is_reported(
    env, dolphin, two_file_source, monkeypatch,
):
    """CodeRabbit 4580360083: the cancel lands while the first unlink still
    runs, so nothing is seen removed yet; the worker then removes it and is
    refused the next. The handed-off delete counts as started: the Finding
    names every planned file not seen removed, the one left among them."""
    import cli
    from models import ConversionMode, DuplicateAction, JobCreateRequest, JobStatus
    from routes import convert as convert_routes
    from services import findings

    cli._open_db(False)
    iso, part = _iso(env), _part2(env)
    reached, go = _hold_first_refuse_second(monkeypatch, part, iso)

    async def run():
        job = await convert_routes.create_job(JobCreateRequest(
            file_path=str(iso), mode=ConversionMode.DOLPHIN_RVZ,
            duplicate_action=DuplicateAction.SKIP, delete_on_verify=True,
            container_only=True,
        ))
        task = asyncio.ensure_future(convert_routes.job_manager._process_job(job.id))
        try:
            await _cancel_once(task, reached)
        finally:
            go.set()
        return job

    job = asyncio.run(run())

    assert _wait_for(lambda: not part.exists())
    assert iso.exists()
    assert job.status == JobStatus.CANCELLED
    reported = [f for f in findings.list_findings() if f["kind"] == findings.SOURCE_KEPT]
    assert [f["library_id"] for f in reported] == [findings.NO_LIBRARY]
    assert str(iso) in reported[0]["cause"][findings.REMAINING]


def test_a_retry_cancelled_during_its_first_unlink_is_finished_later(
    env, dolphin, two_file_source, capsys, monkeypatch,
):
    """The same for a retry: a ``deleting`` entry with the whole plan, which
    the next sweep finishes against what actually remains."""
    from services.rules import sweep as rules_sweep

    part = _part2(env)
    kept = _keep(env, capsys, monkeypatch)
    with monkeypatch.context() as patch:
        reached, go = _hold_first_refuse_second(patch, part, kept["iso"])

        async def run():
            task = asyncio.ensure_future(rules_sweep.sweep(env["shelf"]))
            try:
                await _cancel_once(task, reached)
            finally:
                go.set()

        asyncio.run(run())
        assert _wait_for(lambda: not part.exists())
        assert _wait_for(lambda: not _source_locked(kept["iso"]))
        pending = _rule_state(env)["gc"]["kept_sources"]["Game.iso"]
        assert pending["state"] == "deleting"
        assert str(kept["iso"]) in pending["planned"]
        assert kept["iso"].exists()

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0, lines
    assert not kept["iso"].exists()
    assert not _rule_state(env)["gc"].get("kept_sources")


# --------------------------------------------------------------------------- #
# Codex on ee7142ac: a Rule change, a removed Library, a stop and a cancel
# during the off-lock part of a delete (4580441297, 4580441301, 4580441303,
# 4580441305)
# --------------------------------------------------------------------------- #


def _during_recheck(monkeypatch, action):
    """Run *action* (a coroutine function) while the retry re-checks the
    output, off the sweep lock. Returns the list of rechecks seen."""
    from services.job_manager import JobManager

    real = JobManager._recheck_output
    seen: list = []

    async def recheck(self, *args, **kwargs):
        seen.append(args)
        await action()
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(JobManager, "_recheck_output", recheck)
    return seen


@pytest.mark.parametrize("change", [
    {"exclude_pattern": "Game"},  # Codex's repro: the Source is deselected
    {"priority": 7},  # a field no delete decision reads: still re-planned
])
def test_a_rule_saved_during_the_recheck_keeps_the_source(
    env, dolphin, capsys, kept, monkeypatch, change,
):
    from services.rules import sweep as rules_sweep

    async def save():
        await rules_sweep.set_rules(env["shelf"], _rule(env, **change))

    _during_recheck(monkeypatch, save)

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0, lines
    assert kept["iso"].exists()


def test_a_removed_librarys_job_reports_its_partial_delete(
    env, dolphin, two_file_source, monkeypatch,
):
    """Codex's repro: the owned job's Library is removed before the job runs,
    and its delete stops part way. No owner takes the plan, so it is
    reported under no Library."""
    import cli
    from models import ConversionMode, DuplicateAction, JobCreateRequest
    from routes import convert as convert_routes
    from services import findings
    from services.rules import sweep as rules_sweep

    cli._open_db(False)
    iso, part = _iso(env), _part2(env)
    _refuse_unlink(monkeypatch, iso)

    async def run():
        job = await convert_routes.create_job(JobCreateRequest(
            file_path=str(iso), mode=ConversionMode.DOLPHIN_RVZ,
            duplicate_action=DuplicateAction.SKIP, delete_on_verify=True,
            container_only=True,
        ))
        job._sweep_owner = "shelf"
        rules_sweep._job_owners[job.id] = (env["shelf"], "gc", "Game.iso", None, None)
        await rules_sweep.forget_library(env["shelf"])
        await convert_routes.job_manager._process_job(job.id)

    asyncio.run(run())

    assert not part.exists() and iso.exists()
    reported = [f for f in findings.list_findings() if f["kind"] == findings.SOURCE_KEPT]
    assert [f["library_id"] for f in reported] == [findings.NO_LIBRARY]
    assert reported[0]["cause"][findings.REMAINING] == [str(iso)]


def test_a_stop_during_the_recheck_deletes_nothing(env, dolphin, capsys, kept, monkeypatch):
    """Codex's repro: ``run-rules`` is told to stop while the retry re-checks
    the output. Nothing is unlinked, no attempt is counted, and no further
    retry starts."""
    from services.rules import sweep as rules_sweep

    stop = []

    async def stop_now():
        stop.append(True)

    seen = _during_recheck(monkeypatch, stop_now)

    result = asyncio.run(rules_sweep.sweep(env["shelf"], should_stop=lambda: bool(stop)))

    assert kept["iso"].exists()
    assert len(seen) == 1
    assert result["stopped_reason"] == "stopped"
    pending = _rule_state(env)["gc"]["kept_sources"]["Game.iso"]
    assert pending["attempts"] == 0


def test_a_cancel_while_letting_go_of_output_locks_still_frees_the_slot(
    env, dolphin, monkeypatch,
):
    """Codex's repro: the job is cancelled while its cleanup waits for the
    output locks to be let go. The concurrency slot, the input and folder
    locks are released all the same."""
    import threading

    import cli
    from models import ConversionMode, DuplicateAction, JobCreateRequest
    from routes import convert as convert_routes
    from services import job_manager as job_manager_module
    from services.concurrency_manager import concurrency_manager

    cli._open_db(False)
    iso = _iso(env)
    reached, go = threading.Event(), threading.Event()
    real = job_manager_module._let_go

    def slow_let_go(paths):
        if paths:
            reached.set()
            go.wait(10)
        return real(paths)

    monkeypatch.setattr(job_manager_module, "_let_go", slow_let_go)

    async def run():
        job = await convert_routes.create_job(JobCreateRequest(
            file_path=str(iso), mode=ConversionMode.DOLPHIN_RVZ,
            duplicate_action=DuplicateAction.SKIP,
        ))
        task = asyncio.ensure_future(convert_routes.job_manager._process_job(job.id))
        try:
            await _cancel_once(task, reached)
            return job.id, job.id in concurrency_manager._lock_handles
        finally:
            go.set()

    job_id, slot_held = asyncio.run(run())

    assert not slot_held, job_id



# --------------------------------------------------------------------------- #
# #354 review: a Round-trip mismatch, a stalled stream reader, a verifier
# with no verdict (CodeRabbit 5915313566, 4582189521, 4582189529)
# --------------------------------------------------------------------------- #


def test_a_round_trip_mismatch_on_the_retry_marks_the_output_failed(
    env, dolphin, capsys, monkeypatch,
):
    """The container check passes, but the Round-trip finds other content:
    a real verdict on the output. The Source stays, and the output no longer
    shows as Round-tripped."""
    from services.tools.base import ContentDigest

    tool = registry.for_mode("dolphin_rvz")
    retry = []

    async def digest(path, *, cancel_event=None, within=None):
        changed = retry and str(path).endswith(".rvz")
        return ContentDigest(kind="disc_sha1", value=("b" if changed else "a") * 40)

    async def no_problem(path, *, cancel_event=None, within=None):
        return None

    monkeypatch.setattr(tool, "content_digest", digest)
    monkeypatch.setattr(tool, "source_problem", no_problem)
    kept = _keep(env, capsys, monkeypatch, check_level="round_trip")
    output = kept["output"]
    assert _checked_outputs(output) == ([str(output)], [str(output)])
    retry.append(True)

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0, lines
    assert kept["iso"].exists()
    assert _checked_outputs(output) == ([], [])


def test_a_verifier_with_no_verdict_is_a_counted_attempt(
    env, dolphin, capsys, kept, monkeypatch,
):
    """The verifier's own bound ran out (an ``inconclusive`` result): the
    same as the outer bound, an attempt that is tried again, never stale."""
    from services.subprocess_runner import SubprocessRunner

    service = registry.get("dolphin")._service

    async def stalled(cmd, *, path, **kwargs):
        yield SubprocessRunner._verify_error("Verification stalled", inconclusive=True)

    monkeypatch.setattr(service._runner, "run_verify", stalled)

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0, lines
    assert kept["iso"].exists()
    assert _rule_state(env)["gc"]["kept_sources"]["Game.iso"]["attempts"] == 1
    output = str(kept["output"])
    assert _checked_outputs(kept["output"]) == ([output], [output])


def test_a_stalled_stream_reader_cannot_overwrite_a_newer_check(
    env, dolphin, capsys, kept, monkeypatch,
):
    """A verify stream's reader stops before it reads the bad verdict; a
    newer check of the same file passes meanwhile. When the stalled reader
    resumes, the newer pass stands: the producer recorded the bad verdict
    when it finished, before the lane was let go."""
    from routes import info as info_routes

    service = registry.get("dolphin")._service
    calls = []

    def verify_stream(path, *, cancel_event=None):
        calls.append(path)
        first = len(calls) == 1

        async def events():
            yield {"type": "progress", "progress": 10, "message": "Checking"}
            if first:
                yield {"type": "complete", "valid": False, "message": "Disc is damaged"}
            else:
                yield {"type": "complete", "valid": True, "message": "Disc image is Intact"}

        return events()

    monkeypatch.setattr(service, "verify_stream", verify_stream)
    output = str(kept["output"])
    cfg = info_routes._VERIFY_CONFIG["dolphin"]

    async def run():
        stalled = info_routes._sse_from_verify_stream(
            registry.get("dolphin"), cfg, output,
        ).body_iterator
        first = await stalled.__anext__()  # then the reader stops reading
        for _ in range(100):  # until the stalled check has let go of the lane
            token = await info_routes.workload_limiter.try_acquire("verify")
            if token is not None:
                token.release()
                break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("the stalled check never let go of the lane")
        newer = await info_routes.verify_dolphin(path=output)
        rest = [event async for event in stalled]
        return first, newer, rest

    first, newer, rest = asyncio.run(run())

    assert first["event"] == "verify_progress"
    assert newer["valid"] is True
    assert [event["event"] for event in rest][-1] == "verify_complete"
    assert _checked_outputs(kept["output"]) == ([output], [output])
