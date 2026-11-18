"""Output locks hold across processes, so the CLI is safe beside a server (#306).

Each ``LockManager`` here stands in for one process: ``flock`` locks belong to
an open file, so two managers sharing a lock directory contend exactly as the
server and a CLI run do.
"""
from __future__ import annotations

import fcntl
import os

import pytest
from services import lock_manager as lock_module
from services.lock_manager import LockManager
from utils.path_utils import queued_source_identity


def _manager(lock_dir) -> LockManager:
    manager = LockManager()
    manager._lock_dir = str(lock_dir)
    return manager


def test_a_second_process_cannot_take_a_held_output(tmp_path):
    server, cli = _manager(tmp_path), _manager(tmp_path)
    out = str(tmp_path / "Game.rvz")

    assert server.acquire_lock(out)
    assert not cli.acquire_lock(out)
    assert cli.check_file_status(out) == (False, True)
    server.release_lock(out)
    assert cli.acquire_lock(out)
    cli.release_lock(out)


def test_a_lock_file_unlinked_while_waiting_is_not_trusted(tmp_path, monkeypatch):
    """The holder releases and unlinks the lock file between our open() and our
    flock(): the lock we then get is on a file nobody else can see, so another
    process creating the file afresh would take the same output too."""
    first, second = _manager(tmp_path), _manager(tmp_path)
    out = str(tmp_path / "Game.rvz")
    lock_file = first._lock_file_path(os.path.normpath(out))
    real_flock = fcntl.flock
    raced = []

    def racing_flock(fd, operation):
        if operation & fcntl.LOCK_EX and not raced:
            raced.append(True)
            os.remove(lock_file)
        return real_flock(fd, operation)

    monkeypatch.setattr(lock_module.fcntl, "flock", racing_flock)
    assert first.acquire_lock(out)

    assert not second.acquire_lock(out)
    first.release_lock(out)


def _race_inside_remove(monkeypatch, out, second, third):
    """Run ``second`` then ``third``'s acquire around the first unlink of a lock
    file: the moment another process can slip in. Returns their results."""
    real_remove = os.remove
    results = []
    raced = []

    def racing_remove(path, *args, **kwargs):
        if not raced and os.path.basename(path).startswith("filelock-"):
            raced.append(path)
            results.append(second.acquire_lock(out))
            real_remove(path, *args, **kwargs)
            results.append(third.acquire_lock(out))
            return None
        return real_remove(path, *args, **kwargs)

    monkeypatch.setattr(lock_module.os, "remove", racing_remove)
    return results


def test_release_never_lets_two_processes_hold_one_output(tmp_path, monkeypatch):
    first, second, third = (_manager(tmp_path) for _ in range(3))
    out = str(tmp_path / "Game.rvz")
    assert first.acquire_lock(out)

    results = _race_inside_remove(monkeypatch, out, second, third)
    first.release_lock(out)

    assert results, "release_lock unlinks its lock file"
    assert results.count(True) <= 1, results


def test_a_stale_probe_never_lets_two_processes_hold_one_output(tmp_path, monkeypatch):
    """check_file_status and the stale sweep unlink lock files nobody holds."""
    prober, second, third = (_manager(tmp_path) for _ in range(3))
    out = str(tmp_path / "Game.rvz")
    # A lock file left behind by a crashed process: present, unlocked.
    with open(prober._lock_file_path(os.path.normpath(out)), "ab"):
        pass

    results = _race_inside_remove(monkeypatch, out, second, third)
    prober.check_file_status(out)

    assert results, "the probe removes a stale lock file"
    assert results.count(True) <= 1, results


def test_the_stale_sweep_never_lets_two_processes_hold_one_output(tmp_path, monkeypatch):
    sweeper, second, third = (_manager(tmp_path) for _ in range(3))
    out = str(tmp_path / "Game.rvz")
    with open(sweeper._lock_file_path(os.path.normpath(out)), "ab"):
        pass

    results = _race_inside_remove(monkeypatch, out, second, third)
    sweeper.cleanup_stale_locks_periodic()

    assert results, "the sweep removes a stale lock file"
    assert results.count(True) <= 1, results


def test_dir_lock_release_never_lets_two_processes_hold_one_folder(tmp_path, monkeypatch):
    first, second, third = (_manager(tmp_path) for _ in range(3))
    folder = tmp_path / "GAME"
    folder.mkdir()
    real_remove = os.remove
    results = []
    raced = []

    def racing_remove(path, *args, **kwargs):
        if not raced and os.path.basename(path).startswith("filelock-"):
            raced.append(path)
            results.append(second.acquire_dir_lock(str(folder)))
            real_remove(path, *args, **kwargs)
            results.append(third.acquire_dir_lock(str(folder)))
            return None
        return real_remove(path, *args, **kwargs)

    assert first.acquire_dir_lock(str(folder))
    monkeypatch.setattr(lock_module.os, "remove", racing_remove)
    first.release_dir_lock(str(folder))

    assert results.count(True) <= 1, results



# --------------------------------------------------------------------------- #
# The web mutation guard sees another process's folder lock (codex 4576670752)
# --------------------------------------------------------------------------- #


@pytest.fixture(name="guarded_volume")
def _guarded_volume(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from routes import files as files_routes
    from services.lock_manager import lock_manager
    from starlette.datastructures import Headers

    vol = tmp_path / "vol"
    folder = vol / "ps3" / "GAME"
    (folder / "PS3_GAME").mkdir(parents=True)
    inner = folder / "PS3_GAME" / "PARAM.SFO"
    inner.write_bytes(b"sfo")
    lock_dir = tmp_path / "locks"
    monkeypatch.setattr(files_routes.settings, "chd_volumes", str(vol))
    monkeypatch.setattr(files_routes.settings, "data_mount_root", str(vol))
    monkeypatch.setattr(lock_manager, "_lock_dir", str(lock_dir))
    monkeypatch.setattr(lock_manager, "_ready", False)
    cli = _manager(lock_dir)  # the CLI process packing the folder

    def request(action):
        return SimpleNamespace(headers=Headers({"x-chd-action-confirm": action}))

    return SimpleNamespace(
        routes=files_routes, folder=folder, inner=inner, cli=cli, request=request,
    )


@pytest.mark.asyncio
async def test_the_server_refuses_to_delete_inside_a_folder_another_process_packs(
    guarded_volume,
):
    from fastapi import HTTPException

    g = guarded_volume
    assert g.cli.acquire_dir_lock(str(g.folder))
    try:
        with pytest.raises(HTTPException) as exc:
            await g.routes.delete_file(
                g.request("delete-file"), path=str(g.inner), recursive=False,
            )
        assert exc.value.status_code == 409
        assert g.inner.exists()
        with pytest.raises(HTTPException) as exc:
            await g.routes.rename_file(
                g.request("rename-file"), path=str(g.inner), new_name="X.SFO",
            )
        assert exc.value.status_code == 409
        assert g.inner.exists()
    finally:
        g.cli.release_dir_lock(str(g.folder))

    # Released: the same delete goes through.
    result = await g.routes.delete_file(
        g.request("delete-file"), path=str(g.inner), recursive=False,
    )
    assert result["success"] is True


@pytest.mark.asyncio
async def test_the_exact_path_check_still_refuses_a_locked_output(guarded_volume):
    from fastapi import HTTPException

    g = guarded_volume
    output = g.folder.parent / "GAME.iso"
    output.write_bytes(b"partial")
    assert g.cli.acquire_lock(str(output), allow_existing=True)
    try:
        with pytest.raises(HTTPException) as exc:
            await g.routes.delete_file(
                g.request("delete-file"), path=str(output), recursive=False,
            )
        assert exc.value.status_code == 409
    finally:
        g.cli.release_lock(str(output))


# --------------------------------------------------------------------------- #
# Input locks: a job's Source is visible to other processes (codex 4576728590)
# --------------------------------------------------------------------------- #


def test_readers_share_an_input_lock(tmp_path):
    first, second = _manager(tmp_path), _manager(tmp_path)
    source = str(tmp_path / "Game.wia")

    assert first.acquire_input_lock(source)
    assert second.acquire_input_lock(source)  # another job reading it is fine
    assert _manager(tmp_path).check_file_status(source)[1] is True
    first.release_input_lock(source)
    assert _manager(tmp_path).check_file_status(source)[1] is True
    second.release_input_lock(source)
    assert _manager(tmp_path).check_file_status(source)[1] is False


@pytest.mark.asyncio
async def test_the_server_refuses_to_touch_an_input_another_process_reads(
    guarded_volume, db_path, reset_db_engine,
):
    from fastapi import HTTPException
    from services import db as _db

    g = guarded_volume
    _db.init_engine(db_path)  # a rename moves the file's stored records
    wia = g.folder.parent / "Game.wia"
    wia.write_bytes(b"wia")
    assert g.cli.acquire_input_lock(str(wia))
    try:
        with pytest.raises(HTTPException) as exc:
            await g.routes.delete_file(g.request("delete-file"), path=str(wia), recursive=False)
        assert exc.value.status_code == 409
        with pytest.raises(HTTPException) as exc:
            await g.routes.rename_file(g.request("rename-file"), path=str(wia), new_name="B.wia")
        assert exc.value.status_code == 409
        assert wia.exists()
    finally:
        g.cli.release_input_lock(str(wia))

    try:
        result = await g.routes.rename_file(
            g.request("rename-file"), path=str(wia), new_name="B.wia",
        )
        assert result["success"] is True
        result = await g.routes.delete_file(
            g.request("delete-file"), path=str(wia.parent / "B.wia"), recursive=False,
        )
        assert result["success"] is True
    finally:
        reset_db_engine()


# --------------------------------------------------------------------------- #
# Locks below a folder (review r8 M1)
# --------------------------------------------------------------------------- #


def test_a_lock_inside_a_folder_is_found_from_the_folder(tmp_path):
    holder, probe = _manager(tmp_path / "locks"), _manager(tmp_path / "locks")
    folder = tmp_path / "vol" / "ps3"
    (folder / "GAME").mkdir(parents=True)
    wia = folder / "Game.wia"
    wia.write_bytes(b"x")
    out = tmp_path / "vol" / "gc" / "Game.rvz"

    assert probe.locked_path_below(str(folder)) is None
    assert holder.acquire_dir_lock(str(folder / "GAME"))
    assert probe.locked_path_below(str(folder)) == os.path.realpath(folder / "GAME")
    holder.release_dir_lock(str(folder / "GAME"))
    assert holder.acquire_input_lock(str(wia))
    assert probe.locked_path_below(str(folder)) == os.path.normpath(wia)
    holder.release_input_lock(str(wia))
    assert holder.acquire_lock(str(out))
    assert probe.locked_path_below(str(folder)) is None  # elsewhere
    assert probe.locked_path_below(str(tmp_path / "vol")) == os.path.normpath(out)
    holder.release_lock(str(out))
    assert probe.locked_path_below(str(tmp_path / "vol")) is None
    # A sibling whose name only starts the same is not inside.
    assert holder.acquire_input_lock(str(tmp_path / "vol" / "ps3x" / "A.iso"))
    assert probe.locked_path_below(str(folder)) is None


@pytest.mark.asyncio
async def test_the_server_refuses_to_delete_or_rename_a_folder_holding_a_lock(
    guarded_volume,
):
    from fastapi import HTTPException

    g = guarded_volume
    ps3 = g.folder.parent  # vol/ps3, holding the GAME folder being packed
    assert g.cli.acquire_dir_lock(str(g.folder))
    try:
        with pytest.raises(HTTPException) as exc:
            await g.routes.delete_file(
                g.request("recursive-delete"), path=str(ps3), recursive=True,
            )
        assert exc.value.status_code == 409
        with pytest.raises(HTTPException) as exc:
            await g.routes.rename_file(g.request("rename-file"), path=str(ps3), new_name="PS3")
        assert exc.value.status_code == 409
        assert g.inner.exists()
    finally:
        g.cli.release_dir_lock(str(g.folder))


# --------------------------------------------------------------------------- #
# Subtree exclusion across processes, both orders (codex 4576810647)
# --------------------------------------------------------------------------- #


def test_no_output_inside_a_folder_another_process_packs(tmp_path):
    packer, cli = _manager(tmp_path / "locks"), _manager(tmp_path / "locks")
    game = tmp_path / "vol" / "ps3" / "GAME"
    game.mkdir(parents=True)
    out = str(game / "new.rvz")

    assert packer.acquire_dir_lock(str(game))
    assert not cli.acquire_lock(out)
    assert not cli.acquire_input_lock(str(game / "Game.iso"))
    assert cli.folder_locked_above(out) == os.path.realpath(game)
    packer.release_dir_lock(str(game))
    assert cli.folder_locked_above(out) is None
    assert cli.acquire_lock(out)
    cli.release_lock(out)


def test_no_folder_lock_over_a_path_another_process_holds(tmp_path):
    cli, packer = _manager(tmp_path / "locks"), _manager(tmp_path / "locks")
    game = tmp_path / "vol" / "ps3" / "GAME"
    game.mkdir(parents=True)
    out = str(game / "new.rvz")
    source = str(game / "Game.iso")

    assert cli.acquire_lock(out)
    assert not packer.acquire_dir_lock(str(game))
    cli.release_lock(out)
    assert cli.acquire_input_lock(source)
    assert not packer.acquire_dir_lock(str(game))
    cli.release_input_lock(source)
    assert packer.acquire_dir_lock(str(game))
    packer.release_dir_lock(str(game))


def test_a_folder_lock_does_not_trip_over_its_own_lock_file(tmp_path):
    manager = _manager(tmp_path / "locks")
    game = tmp_path / "vol" / "GAME"
    game.mkdir(parents=True)
    assert manager.acquire_lock(str(tmp_path / "vol" / "GAME.iso"))  # beside it
    assert manager.acquire_dir_lock(str(game))
    manager.release_dir_lock(str(game))
    manager.release_lock(str(tmp_path / "vol" / "GAME.iso"))


# --------------------------------------------------------------------------- #
# Lock identity is the real path (codex 4576966571, review r9 M3)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_an_input_reached_through_a_symlinked_folder_is_seen_on_the_real_path(
    guarded_volume,
):
    from fastapi import HTTPException

    g = guarded_volume
    vol = g.folder.parent.parent
    real = vol / "real"
    real.mkdir()
    wia = real / "Game.wia"
    wia.write_bytes(b"wia")
    (vol / "alias").symlink_to(real, target_is_directory=True)
    alias_path = str(vol / "alias" / "Game.wia")
    probe = _manager(g.cli._lock_dir)

    assert g.cli.acquire_input_lock(alias_path)
    try:
        assert probe.check_file_status(str(wia))[1] is True
        assert probe.locked_path_below(str(real)) == os.path.realpath(wia)
        assert not probe.acquire_dir_lock(str(real))  # packing the real parent
        with pytest.raises(HTTPException) as exc:
            await g.routes.delete_file(g.request("delete-file"), path=str(wia), recursive=False)
        assert exc.value.status_code == 409
        assert wia.exists()
    finally:
        g.cli.release_input_lock(alias_path)

    assert probe.check_file_status(str(wia))[1] is False
    assert probe.acquire_dir_lock(str(real))
    probe.release_dir_lock(str(real))


def test_an_output_lock_through_an_alias_is_one_lock_with_the_real_path(tmp_path):
    first, second = _manager(tmp_path / "locks"), _manager(tmp_path / "locks")
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "alias").symlink_to(real, target_is_directory=True)

    assert first.acquire_lock(str(tmp_path / "alias" / "Game.rvz"))
    assert not second.acquire_lock(str(real / "Game.rvz"))
    assert second.check_file_status(str(real / "Game.rvz"))[1] is True
    first.release_lock(str(tmp_path / "alias" / "Game.rvz"))
    assert second.acquire_lock(str(real / "Game.rvz"))
    second.release_lock(str(real / "Game.rvz"))


@pytest.mark.asyncio
async def test_a_job_cancelled_before_its_run_lets_go_of_its_input_locks(tmp_path, monkeypatch):
    """review r9 M4: a CancelledError between taking the input locks and the
    try/finally that releases them (worker shutdown at the first status
    notice) must not leave the Source shared-locked."""
    import asyncio
    from datetime import datetime, timezone

    from models import ConversionJob, ConversionMode, JobStatus
    from services.concurrency_manager import concurrency_manager
    from services.job_manager import job_manager
    from services.lock_manager import lock_manager

    monkeypatch.setattr(lock_manager, "_lock_dir", str(tmp_path / "locks"))
    monkeypatch.setattr(lock_manager, "_ready", False)
    iso = tmp_path / "Game.iso"
    iso.write_bytes(b"disc")
    out = str(tmp_path / "Game.rvz")
    job = ConversionJob(
        id="m4cancel", file_path=str(iso), filename=iso.name,
        mode=ConversionMode.DOLPHIN_RVZ, status=JobStatus.QUEUED,
        created_at=datetime.now(timezone.utc), output_path=out,
    )
    original = job_manager._notify_subscribers

    async def cancelled_at_start(job_id, event):
        if event.get("type") == "status" and event.get("progress") == 0:
            raise asyncio.CancelledError
        return await original(job_id, event)

    monkeypatch.setattr(job_manager, "_notify_subscribers", cancelled_at_start)
    job_manager.jobs[job.id] = job
    try:
        with pytest.raises(asyncio.CancelledError):
            await job_manager._process_job(job.id)
        assert _manager(tmp_path / "locks").check_file_status(str(iso))[1] is False
        assert job.id not in job_manager._input_locks
    finally:
        lock_manager.release_lock(out)
        concurrency_manager.release(job.id)
        job_manager.jobs.pop(job.id, None)
        job_manager._cancel_events.pop(job.id, None)
        job_manager._release_inputs(job.id)


@pytest.mark.asyncio
async def test_locks_a_cancelled_job_takes_after_the_cancel_are_released(
    tmp_path, monkeypatch,
):
    """The input-lock thread can still be running when the job is cancelled:
    whatever it acquires after that is released as soon as it returns."""
    import asyncio
    import threading
    import time as time_module
    from datetime import datetime, timezone

    from models import ConversionJob, ConversionMode, JobStatus
    from services.concurrency_manager import concurrency_manager
    from services.job_manager import job_manager
    from services.lock_manager import lock_manager

    monkeypatch.setattr(lock_manager, "_lock_dir", str(tmp_path / "locks"))
    monkeypatch.setattr(lock_manager, "_ready", False)
    iso = tmp_path / "Game.iso"
    iso.write_bytes(b"disc")
    out = str(tmp_path / "Game.rvz")
    entered, go, finished = threading.Event(), threading.Event(), threading.Event()
    original = lock_manager.acquire_input_lock

    def paused_acquire(path):
        entered.set()
        go.wait(10)
        try:
            return original(path)
        finally:
            finished.set()

    monkeypatch.setattr(lock_manager, "acquire_input_lock", paused_acquire)
    job = ConversionJob(
        id="raceinput", file_path=str(iso), filename=iso.name,
        mode=ConversionMode.DOLPHIN_RVZ, status=JobStatus.QUEUED,
        created_at=datetime.now(timezone.utc), output_path=out,
    )
    job_manager.jobs[job.id] = job
    task = asyncio.create_task(job_manager._process_job(job.id))
    try:
        while not entered.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        go.set()  # the thread now takes its lock, after the cancel
        assert finished.wait(5)
        deadline = time_module.monotonic() + 2
        while lock_manager._input_handles and time_module.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert lock_manager._input_handles == {}
        assert _manager(tmp_path / "locks").check_file_status(str(iso))[1] is False
        assert job.id not in job_manager._input_locks
    finally:
        go.set()
        lock_manager.release_lock(out)
        concurrency_manager.release(job.id)
        job_manager.jobs.pop(job.id, None)
        job_manager._cancel_events.pop(job.id, None)
        job_manager._release_inputs(job.id)


# --------------------------------------------------------------------------- #
# Review r9 M6: a cue track another process reads; a dead process's lock
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_server_refuses_to_touch_a_cue_track_another_process_reads(
    guarded_volume, db_path, reset_db_engine,
):
    """The job's input set includes the tracks its sheet names
    (``_track_candidate_paths``); a track another process is converting from
    can't be renamed or deleted from the web."""
    from datetime import datetime, timezone

    from fastapi import HTTPException
    from models import ConversionJob, ConversionMode, JobStatus
    from services import db as _db
    from services.job_manager import job_manager

    g = guarded_volume
    _db.init_engine(db_path)
    disc = g.folder.parent
    cue = disc / "Game.cue"
    track = disc / "Game.bin"
    cue.write_text('FILE "Game.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n')
    track.write_bytes(b"\0" * 2352)
    job = ConversionJob(
        id="cuetrack", file_path=str(cue), filename=cue.name,
        mode=ConversionMode.CREATECD, status=JobStatus.QUEUED,
        created_at=datetime.now(timezone.utc), output_path=str(disc / "Game.chd"),
    )
    job._source_identity = queued_source_identity(
        job.file_path, job_manager._track_candidate_paths(job.file_path),
    )
    inputs = job_manager._input_paths(job)
    assert os.path.realpath(track) in {os.path.realpath(p) for p in inputs}
    for path in inputs:  # what the other process's job holds
        assert g.cli.acquire_input_lock(path)
    try:
        with pytest.raises(HTTPException) as exc:
            await g.routes.delete_file(g.request("delete-file"), path=str(track), recursive=False)
        assert exc.value.status_code == 409
        with pytest.raises(HTTPException) as exc:
            await g.routes.rename_file(g.request("rename-file"), path=str(track), new_name="B.bin")
        assert exc.value.status_code == 409
        assert track.exists()
    finally:
        for path in inputs:
            g.cli.release_input_lock(path)
        reset_db_engine()


_DEAD_HOLDER = """
import sys, time
from services.lock_manager import LockManager
manager = LockManager()
manager._lock_dir = sys.argv[1]
assert manager.acquire_input_lock(sys.argv[2])
print("ready", flush=True)
time.sleep(120)
"""


def test_a_dead_process_lock_is_cleared_by_the_next_probe(tmp_path):
    """A CLI killed while holding an input lock leaves its lock file behind;
    the flock died with it, so the next probe reports the file free and
    removes the leftover."""
    import signal
    import subprocess
    import sys
    from pathlib import Path

    lock_dir = tmp_path / "locks"
    source = tmp_path / "Game.iso"
    source.write_bytes(b"disc")
    app = str(Path(__file__).resolve().parents[1] / "app")
    proc = subprocess.Popen(
        [sys.executable, "-c", _DEAD_HOLDER, str(lock_dir), str(source)],
        env={**os.environ, "PYTHONPATH": app}, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "ready"
        probe = _manager(lock_dir)
        assert probe.check_file_status(str(source))[1] is True
        leftover = probe._lock_file_path(os.path.realpath(source))
        proc.send_signal(signal.SIGKILL)
        proc.wait(10)
        assert os.path.exists(leftover)  # nobody cleaned up
        assert probe.check_file_status(str(source))[1] is False
        assert not os.path.exists(leftover)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.stdout.close()


# --------------------------------------------------------------------------- #
# Native (non-UTF-8) names in lock identities (codex 4577157688)
# --------------------------------------------------------------------------- #


def test_a_native_name_locks_and_is_seen_by_every_probe(tmp_path):
    """A sheet's native track name carries surrogate escapes; its lock key,
    lock file name and recorded path are filesystem bytes, so acquire, the
    exact probe and the scan below a folder agree, and nothing raises."""
    holder, probe = _manager(tmp_path / "locks"), _manager(tmp_path / "locks")
    folder = tmp_path / "games"
    folder.mkdir()
    native = os.path.join(str(folder), "Caf\udce9.bin")

    assert holder.acquire_input_lock(native)
    try:
        assert probe.check_file_status(native)[1] is True
        assert probe.locked_path_below(str(folder)) == os.path.realpath(native)
        assert not probe.acquire_dir_lock(str(folder))
    finally:
        holder.release_input_lock(native)
    assert probe.check_file_status(native)[1] is False
    assert holder.acquire_lock(native)
    assert probe.check_file_status(native)[1] is True
    holder.release_lock(native)


def test_a_cue_naming_a_native_track_converts(tmp_path, monkeypatch, db_path, reset_db_engine):
    """An ASCII-named CUE whose track is ``Caf\\xe9.bin`` (Latin-1 bytes) on a
    filesystem that allows such names: the job takes its locks and runs."""
    import asyncio
    from datetime import datetime, timezone

    from models import ConversionJob, ConversionMode, JobStatus
    from services import db as _db
    from services import job_manager as job_manager_module
    from services.job_manager import job_manager
    from services.lock_manager import lock_manager

    folder = tmp_path / "games"
    folder.mkdir()
    raw = os.path.join(os.fsencode(folder), b"Caf\xe9.bin")
    try:
        with open(raw, "wb") as fh:
            fh.write(b"\0" * 2352)
    except OSError:
        pytest.skip("this filesystem refuses non-UTF-8 file names (macOS); runs on Linux")
    cue = folder / "Game.cue"
    cue.write_bytes(b'FILE "Caf\xe9.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n')
    _db.init_engine(db_path)
    monkeypatch.setattr(lock_manager, "_lock_dir", str(tmp_path / "locks"))
    monkeypatch.setattr(lock_manager, "_ready", False)
    for attr in ("chd_volumes", "data_mount_root"):
        monkeypatch.setattr(
            job_manager_module.source_companions_are_safe.__globals__["settings"],
            attr, str(tmp_path),
        )
    out = folder / "Game.chd"

    async def fake_convert(*_args, **_kwargs):
        out.write_bytes(b"chd")
        yield {"progress": 100, "message": "done"}

    monkeypatch.setattr(
        job_manager_module.registry.for_mode("createcd"), "convert", fake_convert,
    )
    job = ConversionJob(
        id="nativecue", file_path=str(cue), filename=cue.name,
        mode=ConversionMode.CREATECD, status=JobStatus.QUEUED,
        created_at=datetime.now(timezone.utc), output_path=str(out),
    )
    job_manager.jobs[job.id] = job
    job._source_identity = queued_source_identity(
        job.file_path, job_manager._track_candidate_paths(job.file_path),
    )
    try:
        asyncio.run(job_manager._process_job(job.id))
        assert job.status == JobStatus.COMPLETED, job.error_message
    finally:
        job_manager.jobs.pop(job.id, None)
        reset_db_engine()


# --------------------------------------------------------------------------- #
# A job's companion outputs are locked across processes (codex 4577208360)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_companion_output_is_held_against_other_processes(
    guarded_volume, monkeypatch, db_path, reset_db_engine,
):
    """During extractcd the BIN beside the CUE is being written: another
    process can't take it for writing, and the web refuses to rename or
    delete it, exactly as for the primary output."""
    from datetime import datetime, timezone

    from fastapi import HTTPException
    from models import ConversionJob, ConversionMode, JobStatus
    from services import db as _db
    from services import job_manager as job_manager_module
    from services.job_manager import job_manager

    g = guarded_volume
    _db.init_engine(db_path)
    disc = g.folder.parent
    chd = disc / "Game.chd"
    chd.write_bytes(b"MComprHD" + b"\0" * 120)
    cue = disc / "Game.cue"
    bin_path = disc / "Game.bin"
    probe = _manager(g.cli._lock_dir)
    for attr in ("chd_volumes", "data_mount_root"):
        monkeypatch.setattr(
            job_manager_module.source_companions_are_safe.__globals__["settings"],
            attr, str(disc.parent),
        )
    seen = {}

    async def fake_convert(*_args, **_kwargs):
        bin_path.write_bytes(b"\0" * 2352)
        seen["overwrite"] = probe.acquire_lock(str(bin_path), allow_existing=True)
        seen["probe"] = probe.check_file_status(str(bin_path))[1]
        for action, call in (
            ("delete", lambda: g.routes.delete_file(
                g.request("delete-file"), path=str(bin_path), recursive=False)),
            ("rename", lambda: g.routes.rename_file(
                g.request("rename-file"), path=str(bin_path), new_name="Other.bin")),
        ):
            try:
                await call()
                seen[action] = "allowed"
            except HTTPException as exc:
                seen[action] = exc.status_code
        cue.write_text('FILE "Game.bin" BINARY\n  TRACK 01 MODE1/2352\n')
        yield {"progress": 100, "message": "done"}

    monkeypatch.setattr(
        job_manager_module.registry.for_mode("extractcd"), "convert", fake_convert,
    )
    job = ConversionJob(
        id="companionlock", file_path=str(chd), filename=chd.name,
        mode=ConversionMode.EXTRACTCD, status=JobStatus.QUEUED,
        created_at=datetime.now(timezone.utc), output_path=str(cue),
    )
    job_manager.jobs[job.id] = job
    job._source_identity = queued_source_identity(
        job.file_path, job_manager._track_candidate_paths(job.file_path),
    )
    try:
        await job_manager._process_job(job.id)
    finally:
        job_manager.jobs.pop(job.id, None)
        reset_db_engine()

    assert seen == {"overwrite": False, "probe": True, "delete": 409, "rename": 409}, seen
    assert bin_path.exists()
    # Released once the job ended.
    assert probe.acquire_lock(str(bin_path), allow_existing=True)
    probe.release_lock(str(bin_path))


# --------------------------------------------------------------------------- #
# An output lock covers both places a write can land (codex 4577612344)
# --------------------------------------------------------------------------- #


def test_an_output_lock_on_a_symlink_covers_the_destination_and_the_old_target(tmp_path):
    holder, other = _manager(tmp_path / "locks"), _manager(tmp_path / "locks")
    old = tmp_path / "old"
    old.mkdir()
    target = old / "Old.rvz"
    target.write_bytes(b"old output")
    out = tmp_path / "Game.rvz"
    out.symlink_to(target)

    assert holder.acquire_lock(str(out), allow_existing=True)
    try:
        assert not other.acquire_lock(str(target), allow_existing=True)  # the old target
        out.unlink()  # what the overwrite sweep does
        assert not other.acquire_lock(str(out), allow_existing=True)  # the new file's path
        assert other.check_file_status(str(out))[1] is True
    finally:
        holder.release_lock(str(out))
    assert other.acquire_lock(str(out), allow_existing=True)
    other.release_lock(str(out))


@pytest.mark.asyncio
async def test_an_overwrite_job_on_a_symlinked_output_keeps_its_destination_locked(
    guarded_volume, monkeypatch, db_path, reset_db_engine,
):
    """The job's output is a symlink; the overwrite sweep unlinks it and the
    converter writes a new file at the original path. While the job runs,
    another process can take neither that path nor the old target, and the
    web guard reports the path busy."""
    from datetime import datetime, timezone

    from fastapi import HTTPException
    from models import ConversionJob, ConversionMode, JobStatus
    from services import db as _db
    from services import job_manager as job_manager_module
    from services.job_manager import job_manager

    g = guarded_volume
    _db.init_engine(db_path)
    games = g.folder.parent
    source = games / "Game.iso"
    source.write_bytes(b"disc" * 512)
    old = games / "old"
    old.mkdir()
    target = old / "Old.rvz"
    target.write_bytes(b"old output")
    out = games / "Game.rvz"
    out.symlink_to(target)
    probe = _manager(g.cli._lock_dir)
    for attr in ("chd_volumes", "data_mount_root"):
        monkeypatch.setattr(
            job_manager_module.source_companions_are_safe.__globals__["settings"],
            attr, str(games.parent),
        )
    seen = {}

    async def fake_convert(*_args, **_kwargs):
        seen["link_gone"] = not os.path.islink(out)
        seen["dest"] = probe.acquire_lock(str(out), allow_existing=True)
        seen["old"] = probe.acquire_lock(str(target), allow_existing=True)
        seen["busy"] = probe.check_file_status(str(out))[1]
        try:
            await g.routes.delete_file(g.request("delete-file"), path=str(target), recursive=False)
            seen["web_old"] = "allowed"
        except HTTPException as exc:
            seen["web_old"] = exc.status_code
        out.write_bytes(b"new rvz")
        yield {"progress": 100, "message": "done"}

    monkeypatch.setattr(
        job_manager_module.registry.for_mode("dolphin_rvz"), "convert", fake_convert,
    )
    job = ConversionJob(
        id="symlinkout", file_path=str(source), filename=source.name,
        mode=ConversionMode.DOLPHIN_RVZ, status=JobStatus.QUEUED,
        created_at=datetime.now(timezone.utc), output_path=str(out), allow_overwrite=True,
    )
    job_manager.jobs[job.id] = job
    job._source_identity = queued_source_identity(
        job.file_path, job_manager._track_candidate_paths(job.file_path),
    )
    try:
        await job_manager._process_job(job.id)
    finally:
        job_manager.jobs.pop(job.id, None)
        reset_db_engine()

    assert seen == {
        "link_gone": True, "dest": False, "old": False, "busy": True, "web_old": 409,
    }, seen


# --------------------------------------------------------------------------- #
# A symlinked output inside a packed folder (codex 4577847011)
# --------------------------------------------------------------------------- #


def _symlinked_output_in_packed_folder(tmp_path):
    game = tmp_path / "GAME"
    game.mkdir()
    (tmp_path / "outside.rvz").write_bytes(b"old")
    link = game / "new.rvz"
    link.symlink_to("../outside.rvz")
    packer = _manager(tmp_path / "locks")  # the process packing GAME
    assert packer.acquire_dir_lock(str(game))
    return game, link, packer


def test_an_overwrite_through_a_symlink_out_of_a_packed_folder_is_refused(tmp_path):
    """The sweep unlinks ``GAME/new.rvz`` and the converter writes there,
    inside the folder another process packs, so the folder above the
    non-following destination counts, not only the symlink's target's."""
    game, link, packer = _symlinked_output_in_packed_folder(tmp_path)
    other = _manager(tmp_path / "locks")
    try:
        assert other.acquire_lock(str(link), allow_existing=True) is False
        assert other._locks == set()  # all or none
        assert other.output_folder_locked_above(str(link)) == os.path.realpath(game)
        free = tmp_path / "free"
        free.mkdir()
        assert other.output_folder_locked_above(str(free / "Game.rvz")) is None
        assert other.acquire_lock(str(free / "Game.rvz"))
        other.release_lock(str(free / "Game.rvz"))
    finally:
        packer.release_dir_lock(str(game))
    assert other.acquire_lock(str(link), allow_existing=True)
    other.release_lock(str(link))


def test_the_dispatch_probe_sees_a_symlinked_output_in_a_packed_folder(
    tmp_path, monkeypatch,
):
    from datetime import datetime, timezone

    from models import ConversionJob, ConversionMode, InputKind, JobStatus
    from services.job_manager import job_manager

    monkeypatch.setattr(lock_module.lock_manager, "_lock_dir", str(tmp_path / "locks"))
    game, link, packer = _symlinked_output_in_packed_folder(tmp_path)
    (tmp_path / "Game.iso").write_bytes(b"disc")
    job = ConversionJob(
        id="symlinked-out", file_path=str(tmp_path / "Game.iso"), filename="Game.iso",
        mode=ConversionMode.DOLPHIN_RVZ, status=JobStatus.QUEUED,
        created_at=datetime.now(timezone.utc), output_path=str(link),
        input_kind=InputKind.FILE,
    )
    try:
        assert job_manager._blocked_by_dir_lock(job) is True
    finally:
        packer.release_dir_lock(str(game))
    assert job_manager._blocked_by_dir_lock(job) is False


# --------------------------------------------------------------------------- #
# Recorded paths round-trip any pathname (codex 4578027196)
# --------------------------------------------------------------------------- #


def test_a_lock_on_a_path_with_a_newline_is_found_from_its_folder(tmp_path):
    folder = tmp_path / "GAME\nDisc"
    folder.mkdir()
    image = folder / "Game.wia"
    image.write_bytes(b"disc")
    reader, packer = _manager(tmp_path / "locks"), _manager(tmp_path / "locks")
    assert reader.acquire_input_lock(str(image))
    try:
        assert packer.locked_path_below(str(folder)) == os.path.realpath(image)
        assert packer.acquire_dir_lock(str(folder)) is False
    finally:
        reader.release_input_lock(str(image))
    assert packer.acquire_dir_lock(str(folder))
    packer.release_dir_lock(str(folder))


def test_a_recorded_path_round_trips_any_bytes(tmp_path):
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    for name in (b"Caf\xe9\nDisc.bin", b"plain.bin", b"a\x00"[:1] + b"\r\n.iso"):
        path = os.fsdecode(os.path.join(os.fsencode(str(tmp_path)), name))
        lock_file = str(lock_dir / "x.lock")
        with open(lock_file, "wb") as handle:
            LockManager._record_path(handle, path)
        assert LockManager._recorded_path(lock_file) == path
        os.remove(lock_file)


def test_a_lock_recorded_the_old_way_still_counts(tmp_path):
    """A lock file an older process wrote (the plain path and a newline)
    is still read, so a lock taken during an upgrade is not missed."""
    lock_file = tmp_path / "old.lock"
    lock_file.write_bytes(os.fsencode(str(tmp_path / "Caf\udce9.bin")) + b"\n")
    assert LockManager._recorded_path(str(lock_file)) == str(tmp_path / "Caf\udce9.bin")


# --------------------------------------------------------------------------- #
# No filesystem I/O under the mutex (codex 4579852997)
# --------------------------------------------------------------------------- #


def test_no_filesystem_probe_runs_while_the_mutex_is_held(tmp_path, monkeypatch):
    """A probe that stalls on a dead mount must never hold `_lock_mutex`
    against a caller on the event loop, so none runs while it is held."""
    manager = _manager(tmp_path / "locks")
    target = tmp_path / "Game.rvz"
    target.write_bytes(b"old")
    folder = tmp_path / "GAME"
    folder.mkdir()
    real = {name: getattr(os.path, name) for name in ("isfile", "exists", "realpath")}
    held = []

    def watched(name):
        def probe(path, *args, **kwargs):
            if manager._lock_mutex.locked():
                held.append((name, str(path)))
            return real[name](path, *args, **kwargs)
        return probe

    for name in real:
        monkeypatch.setattr(os.path, name, watched(name))

    assert manager.acquire_lock(str(target), allow_existing=True)
    manager.check_file_status(str(target))
    manager.is_within_locked_dir(str(target))
    # With an output lock held, the folder-lock overlap check walks it.
    manager.dir_lock_would_conflict(str(folder))
    assert manager.acquire_dir_lock(str(folder))
    manager.is_within_locked_dir(str(folder / "x"))
    manager.release_dir_lock(str(folder))
    manager.release_lock(str(target))

    assert held == []
