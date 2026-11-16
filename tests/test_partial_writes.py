"""services.partial_writes: sweeping what a hard-killed rewrite left behind (#331).

``archive_edit.rewrite_zip`` writes to a hidden ``.partial-*.tmp`` file and
places it only once it is checked. A repack killed hard (SIGKILL, power loss)
leaves that file behind. The sweep removes exactly those, only inside the
Volumes, and only when nothing holds them.
"""
from __future__ import annotations

import errno
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


def _hold_sidecar(temp):
    """What a live writer holds: an exclusive lock on the temp file's sidecar."""
    fd = os.open(partial_writes.sidecar_path(str(temp)), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def test_a_leftover_whose_writer_holds_its_sidecar_is_kept(volume):
    held = _leftover(volume / "dos", ".partial-abcd1234.tmp")
    fd = _hold_sidecar(held)
    try:
        assert partial_writes.sweep_folder(str(volume / "dos")) == 0
    finally:
        os.close(fd)
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
    fd = _hold_sidecar(held)
    try:
        assert partial_writes.sweep_recorded() == 0
    finally:
        os.close(fd)
    assert held.exists() and os.path.exists(marker)


def test_startup_drops_a_record_whose_file_is_already_gone(volume):
    marker = partial_writes.record(str(volume / "dos" / ".partial-abcd1234.tmp"))

    assert partial_writes.sweep_recorded() == 0

    assert not os.path.exists(marker)


@pytest.mark.parametrize("while_away", ["missing", "empty mount point"])
def test_startup_keeps_the_record_while_the_volume_is_away(volume, tmp_path, while_away):
    """A missing file is no proof it is gone: an unmounted Volume hides it too.
    The record stays until the Volume is back and the sweep removes the file."""
    stray = _leftover(volume / "dos", ".partial-abcd1234.tmp")
    marker = partial_writes.record(str(stray))
    away = tmp_path / "away"
    volume.rename(away)
    if while_away == "empty mount point":
        (volume / "dos").mkdir(parents=True)  # same path, not the same folder

    assert partial_writes.sweep_recorded() == 0
    assert os.path.exists(marker)

    if while_away == "empty mount point":
        (volume / "dos").rmdir()
        volume.rmdir()
    away.rename(volume)

    assert partial_writes.sweep_recorded() == 1
    assert not stray.exists() and not os.path.exists(marker)


# --- what rewrite_zip does about it --------------------------------------------


def test_a_rewrite_sweeps_its_destination_folder_first(volume, tmp_path):
    stray = _leftover(volume / "dos", ".partial-abcd1234.tmp")
    src = _zip(tmp_path / "in.zip")

    rewrite_zip(src, str(volume / "dos" / "out.dosz"))

    assert not stray.exists()
    assert sorted(os.listdir(volume / "dos")) == ["out.dosz"]


def test_a_rewrite_locks_a_sidecar_and_journals_its_temp_file_while_it_writes(volume, tmp_path):
    src = _zip(tmp_path / "in.zip")
    seen = {}

    def watch(_done, _total):
        temps = [n for n in os.listdir(volume / "dos") if partial_writes.is_leftover_name(n)]
        if not temps or "sidecar_locked" in seen:
            return
        temp = volume / "dos" / temps[0]
        fd = os.open(partial_writes.sidecar_path(str(temp)), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            seen["sidecar_locked"] = False
        except BlockingIOError:
            seen["sidecar_locked"] = True
        finally:
            os.close(fd)
        seen["journaled"] = str(temp) in partial_writes.recorded_paths()

    rewrite_zip(src, str(volume / "dos" / "out.dosz"), on_progress=watch)

    assert seen == {"sidecar_locked": True, "journaled": True}
    assert partial_writes.recorded_paths() == []
    assert sorted(os.listdir(volume / "dos")) == ["out.dosz"]  # sidecar removed too


def test_the_product_check_reads_a_temp_file_nobody_locks(volume, tmp_path):
    """SMB enforces locks: the writer must not lock the archive it checks."""
    src = _zip(tmp_path / "in.zip")
    seen = []

    def check(path):
        with open(path, "rb") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                seen.append("unlocked")
            except BlockingIOError:
                seen.append("locked")
        zipfile.ZipFile(path).testzip()

    rewrite_zip(src, str(volume / "dos" / "out.dosz"), check=check)

    assert seen == ["unlocked"]


def test_the_sweep_takes_the_sidecar_lock_through_a_write_open(volume, monkeypatch):
    """On NFS an exclusive flock needs a descriptor open for writing."""
    real = fcntl.flock

    def nfs_flock(fd, operation):
        read_only = fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
        if operation & fcntl.LOCK_EX and read_only:
            raise OSError(errno.EBADF, "Bad file descriptor")
        return real(fd, operation)

    monkeypatch.setattr(partial_writes.fcntl, "flock", nfs_flock)
    _leftover(volume / "dos", ".partial-abcd1234.tmp")

    assert partial_writes.sweep_folder(str(volume / "dos")) == 1

    assert sorted(os.listdir(volume / "dos")) == []


def test_an_orphaned_sidecar_is_swept_too(volume):
    sidecar = _leftover(volume / "dos", partial_writes.sidecar_name(".partial-abcd1234.tmp"))

    partial_writes.sweep_folder(str(volume / "dos"))

    assert not sidecar.exists()


def test_a_cleanup_that_cannot_remove_the_temp_file_keeps_its_record(
    volume, tmp_path, monkeypatch
):
    """Only a temp file known to be gone loses its journal entry."""
    src = _zip(tmp_path / "in.zip")
    real_unlink = os.unlink

    def stuck(path, *args, **kwargs):
        if str(path).endswith(partial_writes.TEMP_SUFFIX):
            raise PermissionError(errno.EACCES, "Permission denied", path)
        return real_unlink(path, *args, **kwargs)

    def bad_product(_path):
        raise RuntimeError("bad product")

    monkeypatch.setattr(os, "unlink", stuck)

    with pytest.raises(RuntimeError, match="bad product"):
        rewrite_zip(src, str(volume / "dos" / "out.dosz"), check=bad_product)

    temps = [n for n in os.listdir(volume / "dos") if partial_writes.is_leftover_name(n)]
    assert len(temps) == 1
    assert partial_writes.recorded_paths() == [str(volume / "dos" / temps[0])]


@pytest.mark.parametrize("sweep", ["folder", "recorded"])
def test_a_folder_link_swapped_after_the_check_never_reaches_outside(
    volume, tmp_path, monkeypatch, sweep
):
    """The sweep works in the directory that passed containment, not in
    whatever its path names a moment later."""
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = _leftover(outside, ".partial-abcd1234.tmp")
    _leftover(volume / "dos", ".partial-abcd1234.tmp")
    link = volume / "link"
    link.symlink_to(volume / "dos")
    if sweep == "recorded":
        partial_writes.record(str(link / ".partial-abcd1234.tmp"))
    swapped = []
    for name in ("volume_containment", "is_within_configured_volumes"):
        real = getattr(partial_writes, name, None)
        if real is None:
            continue

        def checked(path, *args, _real=real, **kwargs):
            answer = _real(path, *args, **kwargs)
            if not swapped:
                link.unlink()
                link.symlink_to(outside)
                swapped.append(path)
            return answer

        monkeypatch.setattr(partial_writes, name, checked)

    if sweep == "folder":
        partial_writes.sweep_folder(str(link))
    else:
        partial_writes.sweep_recorded()

    assert swapped
    assert victim.exists()


def test_a_lock_failure_leaks_no_descriptor_and_leaves_nothing_behind(
    volume, tmp_path, monkeypatch
):
    src = _zip(tmp_path / "in.zip")

    def no_locks(_fd, _operation):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(partial_writes.fcntl, "flock", no_locks)
    before = len(os.listdir("/dev/fd"))

    with pytest.raises(OSError):
        rewrite_zip(src, str(volume / "dos" / "out.dosz"))

    assert len(os.listdir("/dev/fd")) == before
    assert os.listdir(volume / "dos") == []
    assert partial_writes.recorded_paths() == []


def test_every_sidecar_name_is_short_and_fixed_length():
    for temp in (".partial-abcd1234.tmp", "." + "G" * 232 + ".partial-abcd1234.tmp"):
        name = partial_writes.sidecar_name(temp)
        assert len(name) == len(".partial-") + 16 + len(".lock")
        assert partial_writes.is_sidecar_name(name)
        assert not partial_writes.is_leftover_name(name)


def test_a_legacy_leftover_at_the_name_limit_is_swept(volume):
    """``.<target>.partial-XXXXXXXX.tmp`` can be 254 bytes: its sidecar's name
    must not grow with it."""
    name = "." + "G" * 232 + ".partial-abcd1234.tmp"
    assert len(name) == 254
    _leftover(volume / "dos", name)

    assert partial_writes.sweep_folder(str(volume / "dos")) == 1

    assert os.listdir(volume / "dos") == []


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
