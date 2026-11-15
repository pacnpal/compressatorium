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
