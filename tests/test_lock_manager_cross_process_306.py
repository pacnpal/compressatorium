"""Output locks hold across processes, so the CLI is safe beside a server (#306).

Each ``LockManager`` here stands in for one process: ``flock`` locks belong to
an open file, so two managers sharing a lock directory contend exactly as the
server and a CLI run do.
"""
from __future__ import annotations

import fcntl
import os

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
