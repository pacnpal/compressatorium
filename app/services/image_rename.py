"""The shared Image rename (#425): every file of an Image to a new name, with
its Referrers following, or nothing at all.

:func:`rename_image` takes an Image and its renames map (``{old: new}``, one
entry per file), so a multi-file Image (a sheet and its tracks, #426) is the
same call with a longer map. It goes through the Referrer hook a conversion
uses (``referrers.update_before_source_removal``), with a
:class:`~services.referrers.StagedPlacement` whose ``rename`` is set: each new
name's "staged" file is the Image's own file under its old name, so a
playlist or gamelist is pointed at the new name while the file is still under
the old one, as for a conversion's staged output. A Referrer that renames the
file itself does so (RomM: its ROM rename moves the file, and the ROM keeps
its id); whatever is still under its old name afterwards is renamed here.

**A sheet's Image** (#426). When the Image is a ``.cue`` or ``.gdi`` and the
map holds its tracks, the placing step first rewrites the sheet's track
references to the new names (``sheet_rewrite``: only those tokens change,
every other byte is kept), atomically through its folder (a whole new sheet
renamed over it), and records that write in the same ``WriteJournal`` as the
Referrers' files, under the sheet's old path. Then each file is renamed, and
last a :class:`Folder` that holds only the Image and carries its name. A step
that fails puts back the files already renamed and raises, so the files are
found under their old names and the journal puts the sheet's bytes and every
Referrer back exactly.

**Bound to the folders it checked** (Codex 4602569670). Each file's folder
is resolved, confirmed inside a Volume and opened beneath the Volume root
with no symlink on the way (``open_dir_beneath``); every look at the files
and every move then goes through those descriptors (``*at`` calls,
``place_without_clobbering`` with ``src_dir_fd``/``dst_dir_fd``), and a file
is moved only while it is still the one fingerprinted. A folder swapped for a
link afterwards can't redirect a move, and nothing is ever moved over
another file.

**One rule for every ending** (Codex 4602538922, CodeRabbit 4602644829 to
4602644910): the rename runs as a task of its own, shielded from its caller's
cancel (a Cancel reaches it through the hook's *cancel_event*), so it always
gets to: settle (every RomM request handed off by the hook, and every
Referrer worker it stopped waiting for, ends first); then inspect where the
files are now; then carry or compensate; then let go of its locks.

* Every file under its new name: renamed. What the app knows about each file
  follows it (its check record, cached ``chdman info`` and DAT match, under
  the listed and the real path, Codex 4602538941), whatever the hook said.
* Every file under its old name: not renamed. Each Referrer file the hook
  wrote is put back to exactly the bytes it held (``WriteJournal``,
  ``referrers.restore_written``): no re-plan, so an entry the rename never
  changed is left as it is (Codex 4602560232), and no file is moved, so a
  destination someone else took meanwhile is never touched (CodeRabbit
  4602644886). A Referrer file changed since is left alone and reported.
* Anything else (a RomM that renamed and then stopped answering): never
  guessed at; the reason says so.
"""

from __future__ import annotations

import asyncio
import errno
import os
import stat
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from fastapi.concurrency import run_in_threadpool
from logging_setup import get_logger
from services import referrers
from services.lock_manager import LockFailed, lock_manager
from services.referrers import (
    HandedOff,
    ReferrerOutcome,
    StagedPlacement,
    WriteJournal,
)
from services.subprocess_runner import (
    DeadlineExpired,
    run_detached_bounded,
    run_detached_to_end,
    run_detached_undoable,
    start_detached_or_own_thread,
)
from utils.path_utils import (
    AnchoredFolders,
    PlacementCommitted,
    file_snapshot,
    move_anchored,
    place_without_clobbering,
    short_temp_name,
    stat_at,
    volume_containment,
)
from utils.sheet_text import (
    MAX_SHEET_BYTES,
    cue_tracks,
    gdi_track_names,
    rewrite_track_names,
    sheet_spellings_of,
)

logger = get_logger("image_rename")

# The sheet rewrite's writes and sync (#426), named so a test can fail them.
_write = os.write
_fsync = os.fsync

# How long a stat of a file on a Volume may take (the job's delete probes take
# the same).
PROBE_SECONDS = 20.0
# How long opening the Image's folders may take. One that answers later is
# closed then (``run_detached_undoable``), never leaked (Codex 4603430442).
OPEN_SECONDS = PROBE_SECONDS
# Recovery waits, with no bound, for every RomM request the hook handed off
# and every Referrer worker it stopped waiting for (CodeRabbit 4602980637):
# these are how often it says what it is still waiting for.
SETTLE_SECONDS = 30.0
HANDED_OFF_SECONDS = 30.0

# Renames running now, kept referenced until they end (the loop holds only
# weak references to tasks).
_running: set[asyncio.Task] = set()


@dataclass
class RenameOutcome:
    """What :func:`rename_image` did. ``records`` are the Referrer hook's
    (``ReferrerOutcome.records``), for the job's report."""

    renamed: bool
    reason: str | None = None
    records: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Folder:
    """A folder an Image rename renames too (#426): the folder holding only
    that Image's files, *old* to *new* beside it."""

    old: str
    new: str


def inner_renames(renames: Mapping[str, str], folder: Folder | None) -> dict[str, str]:
    """Each file's rename inside its own folder: *renames* (``{old: final
    new}``) with a renamed *folder* still under its old name. The files are
    renamed there first and the folder last (``_Files.move_blocking``)."""
    if folder is None:
        return dict(renames)
    return {
        old: os.path.join(folder.old, os.path.relpath(new, folder.new))
        for old, new in renames.items()
    }


def referrer_renames(renames: Mapping[str, str], folder: Folder | None) -> dict[str, str]:
    """What the Referrers are told moves: every file, and a renamed folder
    too, so an entry that names the folder itself (an ES-DE ``<folder>``
    entry, Codex 4604233211) follows it as an entry naming a file does. The
    preview plans with it and the hook runs with it."""
    moved = dict(renames)
    if folder is not None:
        moved[folder.old] = folder.new
    return moved


def _problem_blocking(renames: Mapping[str, str], folder: Folder | None) -> str | None:
    """Why *renames* can't start, or None. Blocking. Fail closed: a path not
    confirmed inside the Volumes refuses it."""
    inner = inner_renames(renames, folder)
    for old, new in renames.items():
        for path in (old, new):
            if volume_containment(path, treat_archives=False) is not True:
                return f"{path} isn't confirmed inside the configured Volumes"
        if os.path.islink(old):
            return f"{os.path.basename(old)} is a symlink"
        if not os.path.isfile(old):
            return f"{os.path.basename(old)} is gone"
        if inner[old] == old:
            continue
        if not os.path.isdir(os.path.dirname(inner[old])):
            return f"{os.path.dirname(inner[old])} isn't a folder"
        if os.path.lexists(inner[old]):
            return f"{os.path.basename(inner[old])} already exists"
    if folder is not None:
        if os.path.islink(folder.old) or not os.path.isdir(folder.old):
            return f"{os.path.basename(folder.old)} isn't a folder"
        if os.path.lexists(folder.new):
            return f"{os.path.basename(folder.new)} already exists"
    return None


def _lock_blocking(
    renames: Mapping[str, str], held: list[str], folder: Folder | None = None,
) -> str | None:
    """Lock every old path (as it is) and every new one (which must be free),
    or none (``LockManager.acquire_all``), adding what it locked to *held*.
    With a *folder*, each file's name inside the old folder and the new
    folder are locked too. Returns why when they couldn't be locked (then
    nothing is held)."""
    inner = inner_renames(renames, folder)
    news = [
        *(new for new in renames.values() if new not in renames),
        *(path for old, path in inner.items() if path not in (old, renames[old])),
        *((folder.new,) if folder is not None else ()),
    ]
    wanted = [*((old, True) for old in renames), *((new, False) for new in dict.fromkeys(news))]
    try:
        refused = lock_manager.acquire_all(wanted)
    except LockFailed as exc:
        return str(exc)
    if refused is None:
        held.extend(path for path, _ in wanted)
        return None
    what = "is in use by a job" if refused in renames else "is taken, or in use by a job"
    return f"{os.path.basename(refused)} {what}"


def _release_blocking(paths: list[str]) -> None:
    for path in paths:
        try:
            lock_manager.release_lock(path)
        except Exception:
            # Releasing is cleanup: a lock left behind is cleared as stale.
            logger.warning("image_rename: could not release the lock on %s", path, exc_info=True)


# The anchored-move helpers this rename and the Inbox (#431) share live in
# `utils.path_utils`; these names are the ones this module has always used.
_stat_at = stat_at
_snapshot = file_snapshot


@dataclass
class _Move:
    old: str
    new: str
    src: int  # the old name's folder, opened beneath its Volume root
    dst: int  # the new name's folder, likewise
    src_real: str  # those folders' real paths when they were opened
    dst_real: str

    @property
    def old_name(self) -> str:
        return os.path.basename(self.old)

    @property
    def new_name(self) -> str:
        return os.path.basename(self.new)


@dataclass
class _FolderMove:
    parent: int  # the folder's parent, opened beneath its Volume root
    old_name: str
    new_name: str
    identity: tuple[int, int]  # the folder's (st_dev, st_ino) when it was opened
    real: str  # the folder's and its parent's real paths when they were opened
    parent_real: str

    def at(self, name: str) -> bool:
        """Whether the folder that was opened is under *name* now."""
        st = _stat_at(name, self.parent)
        return st is not None and stat.S_ISDIR(st.st_mode) and (
            (st.st_dev, st.st_ino) == self.identity
        )


class _Files:
    """The Image's files, bound to the folders that were checked."""

    def __init__(self) -> None:
        self.moves: list[_Move] = []
        self.fingerprints: dict[str, dict[str, int]] = {}
        self._folders = AnchoredFolders()
        # The final ``{old: new}`` the Referrers see (a renamed folder's new
        # name included), the folder renamed with the Image, and the Image's
        # sheet, whose track references follow the tracks (#426).
        self.renames: dict[str, str] = {}
        self.folder: _FolderMove | None = None
        self.sheet: str | None = None
        self.journal: WriteJournal | None = None
        # Files a put-back left under their new name because a writer took
        # the old one meanwhile: what the not-renamed result says, both
        # paths each (CodeRabbit 4607070950).
        self.kept_apart: list[str] = []

    @classmethod
    def open_blocking(
        cls, renames: Mapping[str, str], folder: Folder | None = None,
        sheet: str | None = None, journal: WriteJournal | None = None,
    ) -> _Files:
        """Open each folder beneath its Volume root and fingerprint each
        file through it. Raises OSError or ValueError when one can't be."""
        files = cls()
        # A sheet kept under its own name (``_every_file``) is no rename; a
        # renamed folder is one (``referrer_renames``).
        files.renames = referrer_renames(
            {old: new for old, new in renames.items() if old != new}, folder,
        )
        files.sheet = sheet if sheet in renames else None
        files.journal = journal
        try:
            for old, new in inner_renames(renames, folder).items():
                (src, src_real), (dst, dst_real) = files._folder(old), files._folder(new)
                move = _Move(old, new, src, dst, src_real, dst_real)
                st = _stat_at(move.old_name, move.src)
                if st is None or not stat.S_ISREG(st.st_mode):
                    raise OSError(f"{move.old_name} isn't a file in its folder")
                files.fingerprints[old] = _snapshot(st)
                files.moves.append(move)
            if folder is not None:
                files.folder = files._open_folder_move(folder)
        except BaseException:
            files.close()
            raise
        return files

    def _open_folder_move(self, folder: Folder) -> _FolderMove:
        here, real = self._folder(os.path.join(folder.old, "."))
        parent, parent_real = self._folder(folder.old)
        if os.path.dirname(real) != parent_real or any(
            m.src != here or m.dst != here for m in self.moves
        ):
            raise OSError(f"{os.path.basename(folder.old)} doesn't hold the Image's files")
        own = os.fstat(here)
        move = _FolderMove(
            parent, os.path.basename(folder.old), os.path.basename(folder.new),
            (own.st_dev, own.st_ino), real, parent_real,
        )
        if not move.at(move.old_name):
            raise OSError(f"{move.old_name} isn't the folder that was checked")
        return move

    @property
    def _fds(self) -> dict[str, int]:
        """The folders open now (real path -> descriptor)."""
        return self._folders.descriptors

    def _folder(self, path: str) -> tuple[int, str]:
        return self._folders.open(os.path.dirname(path))

    def close(self) -> None:
        self._folders.close()

    def _is_ours(self, move: _Move, st: os.stat_result | None) -> bool:
        return st is not None and _snapshot(st) == self.fingerprints[move.old]

    def move_blocking(self) -> None:
        """Rewrite the sheet's track references, rename each file still
        under its old name, then the folder: through the checked folders,
        never over another file, and each file only while it is still the one
        fingerprinted. One a Referrer already renamed is left alone. A step
        that fails puts back the files already renamed (#426) and raises;
        the sheet's bytes go back with the Referrers' (``WriteJournal``)."""
        done: list[_Move] = []
        try:
            self._rewrite_sheet_blocking()
            for move in self.moves:
                if _stays(move):
                    continue
                here = _stat_at(move.old_name, move.src)
                if here is None and self._is_ours(move, _stat_at(move.new_name, move.dst)):
                    continue
                if not self._is_ours(move, here):
                    raise OSError(f"{move.old_name} changed since the rename began")
                # The shared anchored move (#431): only while it is the file
                # fingerprinted, never over anything, the entry that landed
                # checked again, and no delete race on a share without an
                # atomic no-replace rename.
                try:
                    move_anchored(
                        move.old_name, move.src, move.dst, self.fingerprints[move.old],
                        new_name=move.new_name,
                    )
                except PlacementCommitted:
                    # It landed, and its old name is gone: put back with
                    # the rest (CodeRabbit 4606148011).
                    done.append(move)
                    raise
                done.append(move)
            self._move_folder_blocking()
        except BaseException:
            self._put_back_blocking(done)
            raise

    def _move_folder_blocking(self) -> None:
        folder = self.folder
        if folder is None or folder.at(folder.new_name):
            return
        # Only the Image's files, under their new names: anything that
        # appeared in the folder meanwhile keeps it where it is.
        if set(os.listdir(self.moves[0].dst)) != {m.new_name for m in self.moves}:
            raise OSError(f"{folder.old_name} holds more than the Image now")
        if not folder.at(folder.old_name):
            raise OSError(f"{folder.old_name} changed since the rename began")
        place_without_clobbering(
            folder.old_name, folder.new_name,
            src_dir_fd=folder.parent, dst_dir_fd=folder.parent,
        )

    def _put_back_blocking(self, done: list[_Move]) -> None:
        """Rename back each file in *done*, newest first, while it is still
        the file that was moved; never over another file. One that can't be
        is logged, and the rename then finds the Image partly renamed and
        says so."""
        for move in reversed(done):
            try:
                at_old = _stat_at(move.old_name, move.src)
                at_new = _stat_at(move.new_name, move.dst)
                if at_old is not None and at_new is not None and (
                    at_old.st_dev, at_old.st_ino,
                ) == (at_new.st_dev, at_new.st_ino):
                    # Both names of one file (a move that never let go of
                    # its old name): the new name goes, the old one stays.
                    os.unlink(move.new_name, dir_fd=move.dst)
                    continue
                if at_old is not None:
                    # Its old name is taken by another file: never moved
                    # back over it. It keeps its new name, its only one.
                    logger.error(
                        "image_rename: %s keeps its new name: %s is taken by another file",
                        move.new, move.old,
                    )
                    self.kept_apart.append(
                        f"{move.new} keeps its new name ({move.old} is taken by another "
                        "file, which was left alone)"
                    )
                    continue
                if not self._is_ours(move, _stat_at(move.new_name, move.dst)):
                    raise OSError(f"{move.new_name} changed since it was renamed")
                move_anchored(
                    move.new_name, move.dst, move.src, self.fingerprints[move.old],
                    new_name=move.old_name,
                )
            except OSError:
                logger.warning(
                    "image_rename: couldn't put %s back as %s", move.new_name, move.old_name,
                    exc_info=True,
                )

    def _rewrite_sheet_blocking(self) -> None:
        """Point the sheet's track references at the tracks' new names,
        every other byte kept (``sheet_rewrite``), through its checked folder
        and only while it is the file fingerprinted (#426).

        Atomic (Codex 4604210916, 4604210926): the new sheet is written
        whole to a hidden file beside it (every byte, a short write written
        on), synced, given the sheet's mode and group, and only then renamed
        over the sheet, which is checked once more just before. Any failure
        before that rename leaves the sheet exactly as it was and removes the
        hidden file; the rename itself is all or nothing. Once it landed, the
        write is journaled under the sheet's old path (``WriteJournal``) and
        the new file is fingerprinted, so a rename that doesn't go ahead puts
        the sheet's exact bytes back, as it does a Referrer's file."""
        if self.sheet is None:
            return
        move = next(m for m in self.moves if m.old == self.sheet)
        before, original = self._read_own_sheet(move)
        after = sheet_rewrite(self.sheet, before, {m.old: m.new for m in self.moves})
        if after is None:
            raise OSError(f"{move.old_name}'s track references can't be rewritten exactly")
        if after == before:
            return
        temp = short_temp_name()
        fd = os.open(
            temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0), 0o600, dir_fd=move.src,
        )
        placed = False
        try:
            _write_all(fd, after)
            _keep_mode_and_group(fd, original)
            _fsync(fd)
            written = _snapshot(os.fstat(fd))
            if not self._is_ours(move, _stat_at(move.old_name, move.src)):
                raise OSError(f"{move.old_name} changed since the rename began")
            os.replace(temp, move.old_name, src_dir_fd=move.src, dst_dir_fd=move.src)
            placed = True
            if self.journal is not None:
                self.journal.record(self.sheet, before, after)
            self.fingerprints[move.old] = written
        finally:
            os.close(fd)
            if not placed:
                _remove_quietly(temp, move.src)

    def _read_own_sheet(self, move: _Move) -> tuple[bytes, os.stat_result]:
        fd = os.open(
            move.old_name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            dir_fd=move.src,
        )
        try:
            st = os.fstat(fd)
            if not self._is_ours(move, st):
                raise OSError(f"{move.old_name} changed since the rename began")
            return _read_sheet(fd), st
        finally:
            os.close(fd)

    def where_blocking(self) -> str:
        """``old`` when every file (and the folder) is under its old name,
        ``new`` when every one is under its new name, else ``mixed``.
        Through the folders."""
        old = new = True
        for move in self.moves:
            at_old = self._is_ours(move, _stat_at(move.old_name, move.src))
            at_new = at_old if _stays(move) else self._is_ours(
                move, _stat_at(move.new_name, move.dst),
            )
            if not _stays(move):
                at_old, at_new = at_old and not at_new, at_new and not at_old
            old, new = old and at_old, new and at_new
        if self.folder is not None:
            at_old = self.folder.at(self.folder.old_name)
            at_new = self.folder.at(self.folder.new_name)
            old, new = old and at_old and not at_new, new and at_new and not at_old
        return "old" if old else "new" if new else "mixed"

    def real_renames(self) -> dict[str, str]:
        """Each file's old and new path along the folders as they were
        opened, for records cached under the real path."""
        folder = self.folder

        def new_real(move: _Move) -> str:
            if folder is not None and move.dst_real == folder.real:
                return os.path.join(folder.parent_real, folder.new_name, move.new_name)
            return os.path.join(move.dst_real, move.new_name)

        pairs = {os.path.join(m.src_real, m.old_name): new_real(m) for m in self.moves}
        return {old: new for old, new in pairs.items() if old != new}

    def placement(self) -> StagedPlacement:
        return placement_for(
            self.renames, self.fingerprints,
            place_blocking=self.move_blocking,
            is_placed=lambda: self.where_blocking() == "new",
        )


def _stays(move: _Move) -> bool:
    """Whether *move* keeps its file's name (it moves only with its folder)."""
    return move.old_name == move.new_name and move.src == move.dst


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte of *data* to *fd*, writing on after a short write;
    one that accepts nothing raises."""
    view = memoryview(data)
    while view:
        count = _write(fd, view)
        if not count or count < 0:
            raise OSError(errno.EIO, "the disk stopped accepting the new sheet")
        view = view[count:]


def _keep_mode_and_group(fd: int, original: os.stat_result) -> None:
    """Give the new sheet the old one's mode, and its group (and owner when
    running as root), as a Referrer's atomic write keeps them. One that
    can't be kept stops the rewrite: the sheet stays as it was."""
    os.fchmod(fd, stat.S_IMODE(original.st_mode))
    uid = original.st_uid if os.geteuid() == 0 else -1
    current = os.fstat(fd)
    if current.st_gid != original.st_gid or uid not in (-1, current.st_uid):
        os.fchown(fd, uid, original.st_gid)


def _remove_quietly(name: str, folder: int) -> None:
    try:
        os.unlink(name, dir_fd=folder)
    except OSError:
        logger.warning("image_rename: couldn't remove the unused %s", name, exc_info=True)


def _read_sheet(fd: int) -> bytes:
    """A sheet's bytes from *fd*; one larger than a sheet is refused."""
    chunks: list[bytes] = []
    total = 0
    while chunk := os.pread(fd, 65536, total):
        total += len(chunk)
        if total > MAX_SHEET_BYTES:
            raise OSError("it is larger than a sheet can be")
        chunks.append(chunk)
    return b"".join(chunks)


def sheet_reference_changes(
    sheet: str, raw: bytes, renames: Mapping[str, str],
) -> dict[str, str] | None:
    """``{reference as written: new reference}`` for each track the sheet at
    *sheet* (its bytes *raw*) names that *renames* (``{old: new}``, each in
    its own folder) renames: the reference's folder part kept, its file name
    changed (#426). None when that can't be told exactly.

    A reference is resolved the way the tracks were found
    (``sheet_track_list``, ``pick_existing``): each spelling of it the sheet
    has, in order (Codex 4604233197: a Windows-1252 ``café`` whose file is
    spelled in UTF-8). The written reference is the native one, which is
    what the rewrite changes, and its folder part must be the resolved
    spelling's. Every file *renames* renames in the sheet's folders must be
    reached by a reference, or None: the sheet would be left naming a file
    that is gone."""
    suffix = os.path.splitext(sheet)[1]
    parse = {".cue": cue_tracks, ".gdi": gdi_track_names}.get(suffix.lower())
    if parse is None:
        return {}
    spellings = sheet_spellings_of(raw, suffix)
    if spellings is None:
        return None
    readings, native = spellings
    written = parse(native)
    spelled = [parse(text) for text in readings]
    folder = os.path.dirname(sheet)
    by_path = {os.path.normpath(old): new for old, new in renames.items()}
    reached: set[str] = set()
    changes: dict[str, str] = {}
    for index, ref in enumerate(written):
        for spelling in [names[index] for names in spelled if index < len(names)] + [ref]:
            track = os.path.normpath(os.path.join(folder, spelling))
            if track in by_path:
                break
        else:
            continue
        reached.add(track)
        head, sep, name = spelling.rpartition("/")
        if name != os.path.basename(track) or ref.rpartition("/")[0] != head:
            return None
        new_name = os.path.basename(by_path[track])
        if new_name != name:
            changes[ref] = head + sep + new_name
    own = os.path.normpath(sheet)
    renamed = {
        old for old, new in by_path.items()
        if old != own and os.path.basename(old) != os.path.basename(new)
    }
    return changes if renamed <= reached else None


def sheet_rewrite(sheet: str, raw: bytes, renames: Mapping[str, str]) -> bytes | None:
    """*raw* (the sheet at *sheet*) with its references to the files
    *renames* renames pointing at their new names
    (:func:`sheet_reference_changes`, ``rewrite_track_names``), every other
    byte kept; None when that can't be done exactly."""
    changes = sheet_reference_changes(sheet, raw, renames)
    if changes is None:
        return None
    if not changes:
        return raw
    return rewrite_track_names(raw, os.path.splitext(sheet)[1], changes)


def placement_for(
    renames: Mapping[str, str], fingerprints: Mapping[str, Mapping[str, int]] | None = None,
    *, place_blocking=None, is_placed=None,
) -> StagedPlacement:
    """The :class:`StagedPlacement` an Image rename hands the Referrer hook:
    each new name is "staged" at the file's old name, and placing it is the
    rename (``rename`` set). The preview plans with it too (it never places)."""
    moves = dict(renames)

    def refuse() -> None:
        raise OSError("this placement only plans")

    blocking = place_blocking or refuse

    async def place() -> None:
        await run_detached_to_end(blocking)

    return StagedPlacement(
        staged={new: old for old, new in moves.items()},
        place=place,
        is_placed=is_placed or (lambda: False),
        place_blocking=blocking,
        source_fingerprints=dict(fingerprints or {}),
        rename=True,
    )


def _failure(outcome: ReferrerOutcome) -> str:
    """Why the hook stopped, as the job says it."""
    if outcome.kept:
        return f"{outcome.kept} ({outcome.reason})"
    return f"couldn't update {os.path.basename(outcome.failed or '')} ({outcome.reason})"


def _with(reason: str | None, more: str | None) -> str | None:
    if reason and more:
        return f"{reason}; {more}"
    return reason or more


async def _probe(func, *args):
    return await run_detached_bounded(func, *args, timeout=PROBE_SECONDS)


Waiting = Callable[[str], Awaitable[None]]


async def rename_image(
    image_path: str, renames: Mapping[str, str], *, folder: Folder | None = None,
    library=None, cancel_event=None, on_wait: Waiting | None = None,
) -> RenameOutcome:
    """Rename every file of the Image at *image_path* (*renames*: ``{old:
    new}``, one entry per file) with its Referrers following, or nothing.
    *library* is the Image's Library, as the hook gets it; *cancel_event*
    stops it (the hook returns, and recovery runs). Never raises for a
    refusal or a failure: the outcome says why (see the module docstring).

    A sheet's Image (#426): *image_path* is the ``.cue`` or ``.gdi`` and
    *renames* holds its tracks too; the sheet's references to them are
    rewritten to their new names, every other byte kept. *folder* is the
    folder that holds only this Image, renamed with it: *renames* then names
    each file under the folder's new name, and the folder is renamed last.

    The rename is a task of its own: a cancel of the caller (a shutdown)
    leaves it to settle, compensate and let go of its locks itself. Settling
    has no bound: *on_wait* is told what it is still waiting for, every
    ``HANDED_OFF_SECONDS`` or ``SETTLE_SECONDS`` (the job shows it)."""
    work = asyncio.ensure_future(
        _rename(image_path, dict(renames), folder, library, cancel_event, on_wait),
    )
    _running.add(work)
    work.add_done_callback(_running.discard)
    return await asyncio.shield(work)


async def _rename(
    image_path: str, renames: dict[str, str], folder: Folder | None, library, cancel_event,
    on_wait: Waiting | None,
) -> RenameOutcome:
    every = _every_file(image_path, renames)
    try:
        problem = await _probe(_problem_blocking, every, folder)
    except (DeadlineExpired, OSError) as exc:
        problem = f"its Volume didn't answer ({exc})"
    if problem:
        return RenameOutcome(False, problem)
    held: list[str] = []
    try:
        problem = await run_detached_to_end(_lock_blocking, every, held, folder)
        if problem:
            return RenameOutcome(False, problem)
        return await _rename_locked(
            image_path, renames, folder, library, cancel_event, on_wait,
        )
    finally:
        # Only once everything the rename started has ended (``_settle``).
        _releaser(held)()


def _releaser(held: list[str]):
    def release() -> None:
        if held:
            start_detached_or_own_thread(_release_blocking, list(held))

    return release


async def _rename_locked(
    image_path: str, renames: dict[str, str], folder: Folder | None, library, cancel_event,
    on_wait: Waiting | None,
) -> RenameOutcome:
    """:func:`rename_image` under the locks: the hook, then settle, inspect,
    carry or compensate."""
    journal = WriteJournal()
    try:
        files = await run_detached_undoable(
            _Files.open_blocking, _every_file(image_path, renames),
            timeout=OPEN_SECONDS, undo=_Files.close,
            folder=folder, sheet=image_path if is_sheet(image_path) else None,
            journal=journal,
        )
    except (OSError, ValueError) as exc:  # DeadlineExpired is a TimeoutError
        return RenameOutcome(False, f"couldn't open its folder ({exc})")
    try:
        handed: list[HandedOff] = []
        hook = await referrers.update_before_source_removal(
            image_path, files.renames, library=library, cancel_event=cancel_event,
            placement=files.placement(), unawaited=handed, journal=journal,
        )
        await _settle(hook, handed, journal, on_wait)
        reason = None if hook.ok else _failure(hook)
        if hook.ok:
            try:
                await run_detached_to_end(files.move_blocking)
            except OSError as exc:
                what = "the file" if len(renames) == 1 else "its files"
                reason = f"couldn't rename {what} ({exc.strerror or exc})"
                if files.kept_apart:
                    reason = f"{reason}; {'; '.join(files.kept_apart)}"
        try:
            where = await _probe(files.where_blocking)
        except OSError:
            where = "unknown"
        return await _finish(renames, files.real_renames(), hook, journal, where, reason)
    finally:
        files.close()


def is_sheet(path: str) -> bool:
    """Whether *path* is a track sheet (``.cue``, ``.gdi``), whose track
    references an Image rename rewrites (#426)."""
    return os.path.splitext(path)[1].lower() in (".cue", ".gdi")


def _every_file(image_path: str, renames: Mapping[str, str]) -> dict[str, str]:
    """*renames*, with a sheet that keeps its name while its tracks change
    added under that name (#426): it is still checked, locked, fingerprinted
    and rewritten, though it isn't renamed."""
    if renames and is_sheet(image_path) and image_path not in renames:
        return {image_path: image_path, **renames}
    return dict(renames)


async def _settle(
    hook: ReferrerOutcome, handed: list[HandedOff], journal: WriteJournal,
    on_wait: Waiting | None,
) -> None:
    """Wait until whatever the hook left running has ended: a RomM request it
    handed off or abandoned, and a Referrer worker it stopped waiting for
    (whose writes are refused from then on). No bound (CodeRabbit
    4602980637): deciding while one still runs could undo a rename it then
    completes, or restore a playlist it then writes. A RomM request ends at
    its client's timeout; a worker stuck on a Volume that stopped answering
    keeps the rename waiting, its locks held, and *on_wait* says so every
    ``HANDED_OFF_SECONDS`` / ``SETTLE_SECONDS``."""
    pending = {h.task for h in [*handed, *hook.handed_off]} | set(hook.lingering)
    while pending:
        _, pending = await asyncio.wait(pending, timeout=HANDED_OFF_SECONDS)
        if pending:
            await _tell(on_wait, "waiting for a RomM request to end before deciding")
    while True:
        try:
            idle = await run_detached_bounded(
                journal.wait_idle, SETTLE_SECONDS, timeout=SETTLE_SECONDS + PROBE_SECONDS,
            )
        except OSError:
            idle = False
        if idle:
            return
        await _tell(on_wait, "waiting for a playlist or gamelist update to end before deciding")


async def _tell(on_wait: Waiting | None, what: str) -> None:
    logger.warning("image_rename: %s", what)
    if on_wait is not None:
        try:
            await on_wait(what)
        except Exception:
            logger.warning("image_rename: couldn't report the wait", exc_info=True)


async def _finish(
    renames: dict[str, str], real: dict[str, str], hook: ReferrerOutcome,
    journal: WriteJournal, where: str, reason: str | None,
) -> RenameOutcome:
    """Carry or compensate, by where the files are now."""
    if where == "new":
        await run_in_threadpool(_carry_records, renames, real)
        notes = list(hook.notes)
        if reason:
            notes.append(f"renamed all the same: {reason}")
        return RenameOutcome(True, records=hook.records, notes=notes)
    if where == "old":
        problems = await run_detached_to_end(referrers.restore_written, journal)
        undone = f"couldn't put back {'; '.join(problems)}" if problems else None
        return RenameOutcome(
            False, _with(reason or "not renamed", undone), hook.records,
        )
    state = (
        "the Image was left partly renamed" if where == "mixed"
        else "where its files are now couldn't be told"
    )
    return RenameOutcome(
        False, _with(reason, f"{state}; check it and its Referrers by hand"), hook.records,
    )


def _carry_records(renames: Mapping[str, str], real: Mapping[str, str]) -> None:
    """What the app knows about each renamed file follows it. Bookkeeping:
    one that can't be moved is logged, and is found again by the next check
    or match."""
    # Imported here: the stores import the job pipeline's helpers.
    from services.chd_metadata_store import chd_metadata_store
    from services.dat_store import dat_store
    from services.verification_store import verification_store

    # A file reached through a symlinked folder may be cached under its real
    # path (the DAT matcher keys it so): that entry is carried as well (Codex
    # 4602538941), from the folders as they were opened, never re-resolved.
    pairs = {**dict(real), **dict(renames)}
    for old, new in pairs.items():
        for store, move in (
            ("check record", verification_store.move_blocking),
            ("chdman info", chd_metadata_store.move_blocking),
            ("DAT match", dat_store.move_match_blocking),
        ):
            try:
                move(old, new)
            except Exception:
                logger.warning(
                    "image_rename: couldn't carry the %s of %s to %s", store, old, new,
                    exc_info=True,
                )
