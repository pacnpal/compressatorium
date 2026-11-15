"""Two processes updating one playlist never lose an edit (codex 4577804154).

With the CLI (#306) a CLI conversion and the server, or two CLIs, can update
the same ``.m3u`` for different discs at once. The in-process lock alone let
both read the original and the last writer win, so the playlist kept naming a
disc whose Source was then deleted. ``M3uReferrer.apply`` now also holds a
cross-process Referrer lock (``LockManager.referrer_lock``) for its whole
read, rewrite and replace.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from services.lock_manager import LockManager, lock_manager
from services.referrers import ReferrerError, m3u

_APP = str(Path(__file__).resolve().parents[1] / "app")

# The other process: plans the Disc 2 change, then applies it with a read that
# signals and stalls, so this process's apply runs while it holds the result.
_OTHER = r"""
import sys, time
vol, lock_dir, flag, old, new = sys.argv[1:6]
from config import settings
settings.chd_volumes = vol
settings.data_mount_root = vol
from services.lock_manager import lock_manager
lock_manager._lock_dir = lock_dir
from services.referrers import m3u
referrer = m3u.M3uReferrer()
changes = referrer.plan({old: new})
real_read = m3u._read
def slow_read(path):
    data = real_read(path)
    if path.endswith(".m3u"):
        open(flag, "w").close()
        time.sleep(1.5)
    return data
m3u._read = slow_read
for change in changes:
    referrer.apply(change)
print("done", flush=True)
"""


@pytest.fixture(name="playlist")
def _playlist(tmp_path, monkeypatch):
    from config import settings

    vol = tmp_path / "vol"
    vol.mkdir()
    monkeypatch.setattr(settings, "chd_volumes", str(vol))
    monkeypatch.setattr(settings, "data_mount_root", str(vol))
    monkeypatch.setattr(lock_manager, "_lock_dir", str(tmp_path / "locks"))
    for disc in ("Game (Disc 1)", "Game (Disc 2)"):
        (vol / f"{disc}.cue").write_bytes(b"cue")
        (vol / f"{disc}.chd").write_bytes(b"chd")
    path = vol / "Game.m3u"
    path.write_bytes(b"Game (Disc 1).cue\nGame (Disc 2).cue\n")
    return path


def _apply(vol: Path, disc: str) -> None:
    referrer = m3u.M3uReferrer()
    for change in referrer.plan({str(vol / f"{disc}.cue"): str(vol / f"{disc}.chd")}):
        referrer.apply(change)


def test_two_processes_updating_one_playlist_both_land(playlist, tmp_path):
    vol = playlist.parent
    flag = tmp_path / "other-read"
    other = subprocess.Popen(
        [sys.executable, "-c", _OTHER, str(vol), str(tmp_path / "locks"), str(flag),
         str(vol / "Game (Disc 2).cue"), str(vol / "Game (Disc 2).chd")],
        env={**os.environ, "PYTHONPATH": _APP}, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 20
        while not flag.exists():
            assert other.poll() is None, other.stderr.read()
            assert time.monotonic() < deadline, "the other process never read"
            time.sleep(0.02)
        # The other process has read the playlist and not written yet.
        _apply(vol, "Game (Disc 1)")
        out, err = other.communicate(timeout=20)
        assert other.returncode == 0, err
        assert out.strip() == "done"
    finally:
        if other.poll() is None:
            other.kill()
            other.communicate()

    assert playlist.read_bytes() == b"Game (Disc 1).chd\nGame (Disc 2).chd\n"


def test_a_playlist_held_too_long_elsewhere_keeps_the_source(playlist, tmp_path, monkeypatch):
    """A bounded wait: still held when it runs out, the Referrer reports it
    couldn't update and writes nothing, so the Source is kept."""
    monkeypatch.setattr(m3u, "PLAYLIST_LOCK_WAIT_SECONDS", 0.3)
    other = LockManager()
    other._lock_dir = str(tmp_path / "locks")
    with other.referrer_lock(str(playlist), wait=1):
        with pytest.raises(ReferrerError, match="being updated by another job"):
            _apply(playlist.parent, "Game (Disc 1)")
    assert playlist.read_bytes() == b"Game (Disc 1).cue\nGame (Disc 2).cue\n"


def test_the_referrer_lock_is_not_an_image_lock(playlist, tmp_path):
    """Its own kind of lock file: holding it never makes the playlist path
    read as locked to an output, input or delete check, and vice versa."""
    other = LockManager()
    other._lock_dir = str(tmp_path / "locks")
    with other.referrer_lock(str(playlist), wait=1):
        assert lock_manager.check_file_status(str(playlist))[1] is False
        assert lock_manager.acquire_lock(str(playlist), allow_existing=True)
        lock_manager.release_lock(str(playlist))


def test_a_waiter_gives_up_when_the_hook_abandons_it(playlist, tmp_path):
    """Inside the hook the wait also ends once the job stops waiting."""
    from services.referrers.base import WriteGuard, write_guard

    other = LockManager()
    other._lock_dir = str(tmp_path / "locks")
    abandoned = threading.Event()
    abandoned.set()
    guard = WriteGuard(abandoned=abandoned, outputs={})
    with other.referrer_lock(str(playlist), wait=1), write_guard(guard):
        started = time.monotonic()
        with pytest.raises(ReferrerError):
            _apply(playlist.parent, "Game (Disc 1)")
        assert time.monotonic() - started < 1
