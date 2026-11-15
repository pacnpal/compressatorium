"""Lock file names stay short, and a lock that can't be made is an error
(codex 4577893015).

A cue track with a 184-byte name gave a lock file name over 255 bytes; making
it raised ENAMETOOLONG, which read as "busy", and the job was requeued
forever. Every lock file name now has a bounded readable part, and an
``OSError`` that isn't contention raises ``LockFailed``, which fails the job
(or the CLI line) with the reason. Only real contention requeues.
"""
from __future__ import annotations

import errno
import os

import pytest
from services import lock_manager as lock_module
from services.lock_manager import LockFailed, LockManager

LONG = "A" * 180 + ".bin"  # a 184-byte basename


def _manager(lock_dir) -> LockManager:
    manager = LockManager()
    manager._lock_dir = str(lock_dir)
    return manager


def test_a_184_byte_name_locks_every_kind(tmp_path):
    manager = _manager(tmp_path / "locks")
    output = tmp_path / LONG
    source = tmp_path / ("B" * 180 + ".cue")
    source.write_bytes(b"cue")
    folder = tmp_path / ("C" * 184)
    folder.mkdir()

    assert manager.acquire_lock(str(output))
    assert manager.acquire_input_lock(str(source))
    assert manager.acquire_dir_lock(str(folder))
    with manager.referrer_lock(str(tmp_path / ("D" * 180 + ".m3u")), wait=1):
        names = os.listdir(tmp_path / "locks")
        assert len(names) == 4
        assert all(len(os.fsencode(name)) <= 160 for name in names), names
    # Still found by the probes that read the lock dir.
    other = _manager(tmp_path / "locks")
    assert other.check_file_status(str(output))[1] is True
    assert other.locked_path_below(str(tmp_path)) is not None
    manager.release_lock(str(output))
    manager.release_input_lock(str(source))
    manager.release_dir_lock(str(folder))


def test_a_long_multibyte_name_is_cut_on_a_character_boundary(tmp_path):
    manager = _manager(tmp_path / "locks")
    name = os.path.basename(manager._lock_file_path(str(tmp_path / ("é" * 120))))
    assert len(os.fsencode(name)) <= 160
    name.encode("utf-8")  # no half character


def test_two_long_names_sharing_a_prefix_are_two_locks(tmp_path):
    manager = _manager(tmp_path / "locks")
    first, second = tmp_path / (LONG + "1"), tmp_path / (LONG + "2")
    assert manager._lock_file_path(str(first)) != manager._lock_file_path(str(second))
    assert manager.acquire_lock(str(first))
    assert manager.acquire_lock(str(second))
    manager.release_lock(str(first))
    manager.release_lock(str(second))


@pytest.mark.parametrize("code", [errno.ENAMETOOLONG, errno.EACCES])
def test_a_lock_that_cannot_be_made_raises_not_busy(tmp_path, monkeypatch, code):
    manager = _manager(tmp_path / "locks")

    def refuse(path):
        raise OSError(code, os.strerror(code), path)

    monkeypatch.setattr(lock_module, "open_lock_handle", refuse)
    source = tmp_path / "Game.iso"
    source.write_bytes(b"disc")
    folder = tmp_path / "GAME"
    folder.mkdir()
    for take in (
        lambda: manager.acquire_lock(str(tmp_path / "Game.rvz")),
        lambda: manager.acquire_input_lock(str(source)),
        lambda: manager.acquire_dir_lock(str(folder)),
    ):
        with pytest.raises(LockFailed, match=os.strerror(code)):
            take()
    assert manager._locks == set()
    assert manager._dir_locks == set()


def test_contention_still_reads_as_busy(tmp_path):
    holder, other = _manager(tmp_path / "locks"), _manager(tmp_path / "locks")
    out = str(tmp_path / "Game.rvz")
    assert holder.acquire_lock(out)
    assert other.acquire_lock(out) is False
    holder.release_lock(out)
