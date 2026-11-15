"""services.partial_writes: sweeping what a hard-killed rewrite left behind (#331).

``archive_edit.rewrite_zip`` writes to a hidden ``.partial-*.tmp`` file and
places it only once it is checked. A repack killed hard (SIGKILL, power loss)
leaves that file behind. The sweep removes exactly those, only inside the
Volumes, and only when nothing holds them.
"""
from __future__ import annotations

import fcntl
import os
import time
import zipfile

import pytest
from services import partial_writes
from services.archive_edit import rewrite_zip

HOUR_AGO = time.time() - 3600


@pytest.fixture(name="volume")
def _volume(tmp_path, monkeypatch):
    from config import settings

    vol = tmp_path / "vol"
    (vol / "dos").mkdir(parents=True)
    monkeypatch.setattr(settings, "chd_volumes", str(vol))
    monkeypatch.setattr(settings, "data_mount_root", str(vol))
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "config"))
    return vol


def _leftover(folder, name, *, age=HOUR_AGO):
    path = folder / name
    path.write_bytes(b"half a zip")
    os.utime(path, (age, age))
    return path


def _zip(path, members=None):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in (members or {"A": b"a" * 3000}).items():
            zf.writestr(name, data)
    return str(path)


# --- which names are ours ----------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        ".partial-abcd1234.tmp",                 # the short name rewrite_zip writes
        ".Abaron (1995).dosz.partial-k3_9z0q1.tmp",  # the name #324 shipped with
    ],
)
def test_a_leftover_name_is_recognised(name):
    assert partial_writes.is_leftover_name(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "partial-abcd1234.tmp",          # not hidden
        ".partial-abc.tmp",              # wrong length
        ".partial-ABCD1234.tmp",         # mkstemp never uses capitals
        ".partial-abcd1234.tmp.bak",
        ".partial-abcd1234.TMP",
        ".partial-abcd-234.tmp",
        "Game.dosz",
        ".tmp-abcd1234",                 # a Referrer's temp file, not ours
    ],
)
def test_a_look_alike_is_not_ours(name):
    assert partial_writes.is_leftover_name(name) is False


# --- the folder sweep ----------------------------------------------------------


def test_an_old_unlocked_leftover_is_swept(volume):
    new = _leftover(volume / "dos", ".partial-abcd1234.tmp")
    old = _leftover(volume / "dos", ".Game.dosz.partial-zz99aa00.tmp")

    assert partial_writes.sweep_folder(str(volume / "dos")) == 2

    assert not new.exists() and not old.exists()


def test_a_locked_leftover_is_kept(volume):
    held = _leftover(volume / "dos", ".partial-abcd1234.tmp")
    with open(held, "rb") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        assert partial_writes.sweep_folder(str(volume / "dos")) == 0
    assert held.exists()


def test_a_fresh_leftover_is_kept(volume):
    """A write that started a moment ago may not hold its lock yet."""
    fresh = _leftover(volume / "dos", ".partial-abcd1234.tmp", age=time.time())

    assert partial_writes.sweep_folder(str(volume / "dos")) == 0

    assert fresh.exists()


def test_look_alikes_are_kept(volume):
    names = ["partial-abcd1234.tmp", ".partial-abc.tmp", ".partial-abcd1234.tmp.bak", "Game.dosz"]
    for name in names:
        _leftover(volume / "dos", name)

    assert partial_writes.sweep_folder(str(volume / "dos")) == 0

    assert sorted(os.listdir(volume / "dos")) == sorted(names)


def test_a_symlink_named_like_a_leftover_is_never_followed(volume, tmp_path):
    target = tmp_path / "precious.bin"
    target.write_bytes(b"keep me")
    link = volume / "dos" / ".partial-abcd1234.tmp"
    link.symlink_to(target)
    os.utime(link, (HOUR_AGO, HOUR_AGO), follow_symlinks=False)

    assert partial_writes.sweep_folder(str(volume / "dos")) == 0

    assert link.is_symlink() and target.read_bytes() == b"keep me"


def test_a_folder_outside_the_volumes_is_left_alone(volume, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    stray = _leftover(outside, ".partial-abcd1234.tmp")

    assert partial_writes.sweep_folder(str(outside)) == 0

    assert stray.exists()


# --- the startup sweep, from the journal ---------------------------------------


def test_startup_sweeps_a_journaled_leftover_and_its_record(volume):
    stray = _leftover(volume / "dos", ".partial-abcd1234.tmp")
    marker = partial_writes.record(str(stray))
    assert marker and os.path.exists(marker)

    assert partial_writes.sweep_recorded() == 1

    assert not stray.exists()
    assert not os.path.exists(marker)


def test_startup_keeps_a_journaled_temp_a_live_write_holds(volume):
    held = _leftover(volume / "dos", ".partial-abcd1234.tmp")
    marker = partial_writes.record(str(held))
    with open(held, "rb") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        assert partial_writes.sweep_recorded() == 0
    assert held.exists() and os.path.exists(marker)


def test_startup_drops_a_record_whose_file_is_already_gone(volume):
    marker = partial_writes.record(str(volume / "dos" / ".partial-abcd1234.tmp"))

    assert partial_writes.sweep_recorded() == 0

    assert not os.path.exists(marker)


# --- what rewrite_zip does about it --------------------------------------------


def test_a_rewrite_sweeps_its_destination_folder_first(volume, tmp_path):
    stray = _leftover(volume / "dos", ".partial-abcd1234.tmp")
    src = _zip(tmp_path / "in.zip")

    rewrite_zip(src, str(volume / "dos" / "out.dosz"))

    assert not stray.exists()
    assert sorted(os.listdir(volume / "dos")) == ["out.dosz"]


def test_a_rewrite_locks_and_journals_its_temp_file_while_it_writes(volume, tmp_path):
    src = _zip(tmp_path / "in.zip")
    seen = {}

    def watch(_done, _total):
        temps = [n for n in os.listdir(volume / "dos") if partial_writes.is_leftover_name(n)]
        if not temps or "locked" in seen:
            return
        path = volume / "dos" / temps[0]
        with open(path, "rb") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                seen["locked"] = False
            except BlockingIOError:
                seen["locked"] = True
        seen["journaled"] = str(path) in partial_writes.recorded_paths()

    rewrite_zip(src, str(volume / "dos" / "out.dosz"), on_progress=watch)

    assert seen == {"locked": True, "journaled": True}
    assert partial_writes.recorded_paths() == []


def test_a_target_name_near_the_length_limit_still_writes(volume, tmp_path):
    """The temp name is short and fixed-length, never built from the target."""
    src = _zip(tmp_path / "in.zip")
    long_name = "G" * 245 + ".dosz"  # 250 bytes, under the 255-byte name limit

    rewrite_zip(src, str(volume / "dos" / long_name))

    assert os.listdir(volume / "dos") == [long_name]


def test_startup_resolves_the_volume_roots_then_sweeps_the_journal(monkeypatch):
    """Containment needs the resolved roots (an unknown root keeps the file),
    so the startup sweep runs on the same thread, right after they resolve."""
    import main

    order = []
    monkeypatch.setattr(main, "refresh_volume_roots", lambda: order.append("roots"))
    monkeypatch.setattr(partial_writes, "sweep_recorded", lambda: order.append("sweep") or 0)

    main.startup_volume_work()

    assert order == ["roots", "sweep"]
