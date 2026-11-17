"""Staging a conversion's output out of sight until it is checked (#398).

A Library whose own scanner indexes whatever appears beside an Image (RomM:
5.4.1 indexes even dot-prefixed files) must never see a half-written output,
a tool's temp file, or an output that has not passed its checks: it would
index it as a second ROM, and a ghost once the Source goes. Dot-prefixed
*folders* are skipped, so the work happens in a hidden folder beside the
output and the checked output is moved into place by a rename on the same
filesystem.

One helper for every caller: the job pipeline (``JobManager._process_job``)
and the RomM swap (#391), which stages its output the same way.

* :func:`open_slot` makes ``<folder>/.compressatorium-stage/slot-XXXXXXXX/``
  and returns a :class:`StagedOutput`. The folder must be inside a configured
  Volume (``volume_containment(...) is True``); it, the stage folder and the
  slot are opened along their real path with no link followed, and every
  later step works through those descriptors, so a folder swapped for a link
  after the check can't redirect a placement or a cleanup.
* :meth:`StagedOutput.path_for` names where a tool writes: the output's own
  name inside the slot, so a sheet that names its tracks (a ``.cue``) stays
  right once moved.
* :meth:`StagedOutput.place` moves the named entries into the folder, never
  over an existing one, all or none.
* :meth:`StagedOutput.close` removes whatever is still in the slot (an
  unplaced output, a tool's scratch), the slot, its lock and journal record,
  and the stage folder once no other slot is in it.

A crash leaves the slot behind, hidden. ``partial_writes`` journals it and the
startup sweep (``partial_writes.sweep_recorded``) removes it by the same rules
as a ``.partial`` leftover (#331): its lock must be free and it must be older
than ``partial_writes.MIN_AGE_SECONDS``. Opening a slot sweeps its stage folder
first, so a slot whose record was lost goes too.

**Parking slots (#391).** ``open_slot(folder, parking=True)`` makes a
``park-XXXXXXXX/`` slot instead. The RomM swap parks the Source in one while
the output takes its name, so what it holds is a Source, never a leftover: no
sweep ever removes a parking slot (the sweeps match ``slot-`` names only) and
it is not journaled in ``partial_writes``. The swap's own journal
(``services.romm.swap_journal``) records it instead. On a slot:

* :meth:`StagedOutput.take` moves an entry of the folder into the slot,
* :meth:`StagedOutput.restore` moves it back out, under a name of choice,
* :meth:`StagedOutput.remove` deletes a file in the slot,
* :meth:`StagedOutput.keep` lets go of the slot and leaves it, and whatever
  is in it, where it is (a parked Source the swap could not put back).
* :meth:`StagedOutput.set_aside` turns an ordinary slot holding a checked
  output that could not be placed into a parking slot, so it is kept too.

Each move is a no-replace rename on one filesystem, through the slot's
descriptors.

Everything here is blocking: callers run it off the event loop, bounded.
"""
from __future__ import annotations

import errno
import fcntl
import os
import shutil
import threading
from collections.abc import Iterable

from logging_setup import get_logger
from services import partial_writes
from utils.junk import STAGE_DIR_NAME
from utils.path_utils import (
    open_dir_beneath,
    place_without_clobbering,
    short_temp_name,
    volume_containment,
)

logger = get_logger("staging")

_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC
# Read and write, never truncate: an exclusive flock needs a write open on NFS.
_LOCK_FLAGS = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _CLOEXEC
_CLAIM_ATTEMPTS = 100
# A parking slot's name prefix (#391). Not ``partial_writes.SLOT_PREFIX``, so
# no sweep ever matches it.
PARK_PREFIX = "park-"

__all__ = ["PARK_PREFIX", "STAGE_DIR_NAME", "StagedOutput", "open_slot"]


class StagedOutput:
    """One writer's slot in the stage folder beside its output.

    ``folder`` is the output folder's real path; ``path`` the slot's. The slot
    is held (its lock, its journal record) until :meth:`close`. Thread-safe:
    a :meth:`close` waits for a :meth:`place` still running on another thread.
    """

    def __init__(
        self, *, folder: str, folder_fd: int, stage_fd: int, slot_name: str,
        slot_fd: int, lock_fd: int, marker: str | None,
    ) -> None:
        self.folder = folder
        self.path = os.path.join(folder, STAGE_DIR_NAME, slot_name)
        self.placed = False
        self._folder_fd = folder_fd
        self._stage_fd = stage_fd
        self._slot_name = slot_name
        self._slot_fd = slot_fd
        self._lock_fd = lock_fd
        self._marker = marker
        self._closed = False
        self._mutex = threading.Lock()

    def path_for(self, name: str) -> str:
        """Where to write the output named *name*: in the slot, same name."""
        if not name or os.sep in name or name in (".", ".."):
            raise ValueError(f"Not a file name: {name!r}")
        return os.path.join(self.path, name)

    def place(self, names: Iterable[str]) -> list[tuple[str, str]]:
        """Move the entries *names* (files or folders) from the slot into the
        output folder, under the same names: ``(staged path, final path)``
        for each one moved, in order. A name not in the slot is skipped.

        Never over anything: an entry already at a destination raises
        ``FileExistsError`` before anything moves. All or none: if a move
        fails part way, the ones already moved go back into the slot and the
        error propagates. Nothing to move at all raises ``FileNotFoundError``.
        """
        with self._mutex:
            if self._closed:
                raise ValueError("The staging slot is closed")
            present = [name for name in dict.fromkeys(names) if self._in_slot(name)]
            if not present:
                raise FileNotFoundError(
                    errno.ENOENT, "Nothing staged to place", self.path,
                )
            # A fast path only: the move below is what refuses an existing
            # entry, atomically, however late another one appears.
            for name in present:
                if self._exists_in_folder(name):
                    raise FileExistsError(
                        errno.EEXIST, "Already exists; not replaced",
                        os.path.join(self.folder, name),
                    )
            moved: list[str] = []
            try:
                for name in present:
                    place_without_clobbering(
                        name, name, src_dir_fd=self._slot_fd, dst_dir_fd=self._folder_fd,
                    )
                    moved.append(name)
            except OSError as exc:
                if exc.errno == errno.EEXIST:
                    # The entry that failed is the one after those moved.
                    exc.filename = os.path.join(self.folder, present[len(moved)])
                for done in reversed(moved):
                    self._move_back(done)
                raise
            self.placed = True
            return [
                (os.path.join(self.path, name), os.path.join(self.folder, name))
                for name in present
            ]

    def take(self, name: str) -> str:
        """Move the entry *name* of the output folder into the slot, same
        name, never over anything: its path in the slot. Raises ``OSError``
        (``FileNotFoundError`` when it is not there)."""
        with self._mutex:
            self._require_open()
            place_without_clobbering(
                name, name, src_dir_fd=self._folder_fd, dst_dir_fd=self._slot_fd,
            )
            return os.path.join(self.path, name)

    def restore(self, name: str, as_name: str | None = None) -> str:
        """Move the entry *name* out of the slot into the output folder, as
        *as_name* (default: the same name), never over anything: its final
        path. Raises ``OSError`` (``FileExistsError`` when the name is
        taken)."""
        target = as_name or name
        with self._mutex:
            self._require_open()
            place_without_clobbering(
                name, target, src_dir_fd=self._slot_fd, dst_dir_fd=self._folder_fd,
            )
            return os.path.join(self.folder, target)

    def contents(self) -> list[str]:
        """The names in the slot now, read through its descriptor. What a
        caller checks before removing a slot that may hold something it must
        keep (a parked Source). Raises ``OSError``."""
        with self._mutex:
            self._require_open()
            return sorted(os.listdir(self._slot_fd))

    def remove(self, name: str) -> None:
        """Delete the file *name* in the slot, never following a link.
        Raises ``OSError``."""
        with self._mutex:
            self._require_open()
            os.unlink(name, dir_fd=self._slot_fd)

    def keep(self) -> None:
        """Let go of the slot and leave it, and whatever is in it, in place:
        the descriptors close and the lock goes, the folder stays. Only for a
        parking slot, which no sweep removes: a parked Source the swap could
        not put back stays parked for the next run. Idempotent; never
        raises."""
        with self._mutex:
            if self._closed:
                return
            self._closed = True
            os.close(self._slot_fd)
            _unlink_quietly(partial_writes.slot_lock_name(self._slot_name), self._stage_fd)
            os.close(self._lock_fd)
            os.close(self._stage_fd)
            os.close(self._folder_fd)

    def set_aside(self) -> str:
        """Keep the slot, and the checked output in it, out of every sweep,
        and let go of it: the slot is renamed to a ``park-`` slot (never
        swept), its journal record dropped and its lock let go. For an output
        that passed its checks but could not be placed: it is never
        discarded. Returns the slot's new path. Raises ``OSError`` (the slot
        is then still open)."""
        with self._mutex:
            self._require_open()
            old = self._slot_name
            new = PARK_PREFIX + old[len(partial_writes.SLOT_PREFIX):]
            place_without_clobbering(old, new, src_dir_fd=self._stage_fd, dst_dir_fd=self._stage_fd)
            self._slot_name = new
            self.path = os.path.join(self.folder, STAGE_DIR_NAME, new)
            _unlink_quietly(partial_writes.slot_lock_name(old), self._stage_fd)
            partial_writes.forget(self._marker)
            self._marker = None
        self.keep()
        return self.path

    def _require_open(self) -> None:
        if self._closed:
            raise ValueError("The staging slot is closed")

    def close(self) -> None:
        """Remove what is left in the slot, the slot and its lock, then the
        stage folder if no other slot is in it. Idempotent; never raises. A
        slot that can't be removed keeps its journal record, so the startup
        sweep takes it later."""
        with self._mutex:
            if self._closed:
                return
            self._closed = True
            os.close(self._slot_fd)
            try:
                shutil.rmtree(self._slot_name, dir_fd=self._stage_fd)
                gone = True
            except FileNotFoundError:
                gone = True
            except OSError:
                logger.warning("Could not remove the staging slot %s", self.path, exc_info=True)
                gone = False
            # The lock file goes while its lock is still held, so no sweep
            # can lock a lock file that is about to vanish; then the record.
            if gone:
                _unlink_quietly(partial_writes.slot_lock_name(self._slot_name), self._stage_fd)
            os.close(self._lock_fd)
            if gone:
                partial_writes.forget(self._marker)
            os.close(self._stage_fd)
            if gone:
                partial_writes.remove_stage_folder_if_empty(self._folder_fd)
            os.close(self._folder_fd)

    def _move_back(self, name: str) -> None:
        """Undo one placing: the entry goes back into the slot, never over
        anything. Logged when it can't."""
        try:
            place_without_clobbering(
                name, name, src_dir_fd=self._folder_fd, dst_dir_fd=self._slot_fd,
            )
        except OSError:
            logger.exception(
                "A staged output was placed but could not be moved back: %s",
                os.path.join(self.folder, name),
            )

    def _in_slot(self, name: str) -> bool:
        try:
            os.stat(name, dir_fd=self._slot_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    def _exists_in_folder(self, name: str) -> bool:
        try:
            os.stat(name, dir_fd=self._folder_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True  # anything else (a stat that failed) counts as there


def _unlink_quietly(name: str, dir_fd: int) -> None:
    try:
        os.unlink(name, dir_fd=dir_fd)
    except OSError:
        pass


def _open_folder(folder: str, *, make: bool) -> tuple[str, int]:
    """The output folder's real path and a descriptor of it, only inside a
    configured Volume and with no link followed; made first when *make* and
    missing (only once it is known to be inside a Volume). Raises
    ``OSError``."""
    real = os.path.realpath(folder)
    if volume_containment(real, treat_archives=False) is not True:
        raise PermissionError(
            errno.EACCES, "Not inside a configured Volume (or can't tell)", folder,
        )
    if make:
        os.makedirs(real, exist_ok=True)
    try:
        return real, open_dir_beneath(real)
    except ValueError as exc:
        raise OSError(errno.EINVAL, str(exc), folder) from exc


def _claim(
    folder: str, folder_fd: int, stage_fd: int, *, parking: bool = False,
) -> StagedOutput | None:
    """One attempt at a fresh slot in the open stage folder; None when the
    name was taken (try another). A parking slot is not journaled."""
    name = short_temp_name(PARK_PREFIX if parking else partial_writes.SLOT_PREFIX)
    lock_name = partial_writes.slot_lock_name(name)
    try:
        lock_fd = os.open(lock_name, _LOCK_FLAGS, 0o600, dir_fd=stage_fd)
    except FileExistsError:
        return None
    slot_fd = -1
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            os.mkdir(name, 0o700, dir_fd=stage_fd)
        except FileExistsError:
            _unlink_quietly(lock_name, stage_fd)
            os.close(lock_fd)
            return None
        slot_fd = os.open(name, _DIR_FLAGS, dir_fd=stage_fd)
        marker = None if parking else partial_writes.record(
            os.path.join(folder, STAGE_DIR_NAME, name),
        )
        return StagedOutput(
            folder=folder, folder_fd=folder_fd, stage_fd=stage_fd, slot_name=name,
            slot_fd=slot_fd, lock_fd=lock_fd, marker=marker,
        )
    except BaseException:
        if slot_fd >= 0:
            os.close(slot_fd)
        try:
            os.rmdir(name, dir_fd=stage_fd)
        except OSError:
            pass
        _unlink_quietly(lock_name, stage_fd)
        os.close(lock_fd)
        raise


def open_slot(
    folder: str, *, make_folder: bool = False, parking: bool = False,
) -> StagedOutput:
    """A fresh staging slot beside *folder*'s files: the hidden stage folder
    is made if need be, swept of stale slots, then the slot is made, locked
    and journaled. With *make_folder*, a missing *folder* is made first, as a
    tool's runner would make its output folder (a conversion into a new
    subfolder). With *parking*, a parking slot (see the module docstring):
    never swept, never journaled here. Raises ``OSError`` (``PermissionError``
    outside the Volumes) and leaves no slot behind when it does."""
    real, folder_fd = _open_folder(folder, make=make_folder)
    try:
        for _ in range(_CLAIM_ATTEMPTS):
            stage_fd = partial_writes.open_stage_folder(folder_fd, create=True)
            try:
                partial_writes.sweep_stage(
                    stage_fd, where=os.path.join(real, STAGE_DIR_NAME),
                )
                slot = _claim(real, folder_fd, stage_fd, parking=parking)
            except FileNotFoundError:
                # A sweep elsewhere removed the empty stage folder between our
                # open and our claim: open it again.
                os.close(stage_fd)
                continue
            except BaseException:
                os.close(stage_fd)
                raise
            if slot is not None:
                return slot
            os.close(stage_fd)
        raise FileExistsError(errno.EEXIST, "No free staging slot name", real)
    except BaseException:
        partial_writes.remove_stage_folder_if_empty(folder_fd)
        os.close(folder_fd)
        raise
