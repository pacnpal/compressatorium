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
    lock_boundary_root,
    open_dir_beneath,
    place_without_clobbering,
    volume_containment,
)

logger = get_logger("image_rename")

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


def _problem_blocking(renames: Mapping[str, str]) -> str | None:
    """Why *renames* can't start, or None. Blocking. Fail closed: a path not
    confirmed inside the Volumes refuses it."""
    for old, new in renames.items():
        for path in (old, new):
            if volume_containment(path, treat_archives=False) is not True:
                return f"{path} isn't confirmed inside the configured Volumes"
        if os.path.islink(old):
            return f"{os.path.basename(old)} is a symlink"
        if not os.path.isfile(old):
            return f"{os.path.basename(old)} is gone"
        if not os.path.isdir(os.path.dirname(new)):
            return f"{os.path.dirname(new)} isn't a folder"
        if os.path.lexists(new):
            return f"{os.path.basename(new)} already exists"
    return None


def _lock_blocking(renames: Mapping[str, str], held: list[str]) -> str | None:
    """Lock every old path (as it is) and every new one (which must be free),
    or none (``LockManager.acquire_all``), adding what it locked to *held*.
    Returns why when they couldn't be locked (then nothing is held)."""
    wanted = [
        *((old, True) for old in renames), *((new, False) for new in renames.values()),
    ]
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


def _stat_at(name: str, folder: int) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=folder, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _snapshot(st: os.stat_result) -> dict[str, int]:
    """A file's fingerprint as a delete snapshot has it."""
    return {
        "size": st.st_size, "mtime_ns": st.st_mtime_ns,
        "inode": st.st_ino, "device": st.st_dev,
    }


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


class _Files:
    """The Image's files, bound to the folders that were checked."""

    def __init__(self) -> None:
        self.moves: list[_Move] = []
        self.fingerprints: dict[str, dict[str, int]] = {}
        self._fds: dict[str, int] = {}

    @classmethod
    def open_blocking(cls, renames: Mapping[str, str]) -> _Files:
        """Open each folder beneath its Volume root and fingerprint each
        file through it. Raises OSError or ValueError when one can't be."""
        files = cls()
        try:
            for old, new in renames.items():
                (src, src_real), (dst, dst_real) = files._folder(old), files._folder(new)
                move = _Move(old, new, src, dst, src_real, dst_real)
                st = _stat_at(move.old_name, move.src)
                if st is None or not stat.S_ISREG(st.st_mode):
                    raise OSError(f"{move.old_name} isn't a file in its folder")
                files.fingerprints[old] = _snapshot(st)
                files.moves.append(move)
        except BaseException:
            files.close()
            raise
        return files

    def _folder(self, path: str) -> tuple[int, str]:
        real = os.path.realpath(os.path.dirname(path))
        if real in self._fds:
            return self._fds[real], real
        root = lock_boundary_root(real)
        if root is None or volume_containment(real, treat_archives=False) is not True:
            raise OSError(f"{real} isn't confirmed inside the configured Volumes")
        relative = os.path.relpath(real, str(root))
        fd = open_dir_beneath(str(root), "" if relative == "." else relative)
        self._fds[real] = fd
        return fd, real

    def close(self) -> None:
        for fd in self._fds.values():
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds.clear()

    def _is_ours(self, move: _Move, st: os.stat_result | None) -> bool:
        return st is not None and _snapshot(st) == self.fingerprints[move.old]

    def move_blocking(self) -> None:
        """Rename each file still under its old name, through the checked
        folders and never over another file, while it is still the file that
        was fingerprinted. One a Referrer already renamed is left alone."""
        for move in self.moves:
            here = _stat_at(move.old_name, move.src)
            if here is None and self._is_ours(move, _stat_at(move.new_name, move.dst)):
                continue
            if not self._is_ours(move, here):
                raise OSError(f"{move.old_name} changed since the rename began")
            place_without_clobbering(
                move.old_name, move.new_name, src_dir_fd=move.src, dst_dir_fd=move.dst,
            )

    def where_blocking(self) -> str:
        """``old`` when every file is under its old name, ``new`` when every
        one is under its new name, else ``mixed``. Through the folders."""
        old = new = True
        for move in self.moves:
            at_old = self._is_ours(move, _stat_at(move.old_name, move.src))
            at_new = self._is_ours(move, _stat_at(move.new_name, move.dst))
            old = old and at_old and not at_new
            new = new and at_new and not at_old
        return "old" if old else "new" if new else "mixed"

    def real_renames(self) -> dict[str, str]:
        """Each file's old and new path along the folders as they were
        opened, for records cached under the real path."""
        return {
            os.path.join(m.src_real, m.old_name): os.path.join(m.dst_real, m.new_name)
            for m in self.moves
        }

    def placement(self) -> StagedPlacement:
        return placement_for(
            {m.old: m.new for m in self.moves}, self.fingerprints,
            place_blocking=self.move_blocking,
            is_placed=lambda: self.where_blocking() == "new",
        )


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
    image_path: str, renames: Mapping[str, str], *, library=None, cancel_event=None,
    on_wait: Waiting | None = None,
) -> RenameOutcome:
    """Rename every file of the Image at *image_path* (*renames*: ``{old:
    new}``) with its Referrers following, or nothing. *library* is the
    Image's Library, as the hook gets it; *cancel_event* stops it (the hook
    returns, and recovery runs). Never raises for a refusal or a failure: the
    outcome says why (see the module docstring).

    The rename is a task of its own: a cancel of the caller (a shutdown)
    leaves it to settle, compensate and let go of its locks itself. Settling
    has no bound: *on_wait* is told what it is still waiting for, every
    ``HANDED_OFF_SECONDS`` or ``SETTLE_SECONDS`` (the job shows it)."""
    work = asyncio.ensure_future(
        _rename(image_path, dict(renames), library, cancel_event, on_wait),
    )
    _running.add(work)
    work.add_done_callback(_running.discard)
    return await asyncio.shield(work)


async def _rename(
    image_path: str, renames: dict[str, str], library, cancel_event,
    on_wait: Waiting | None,
) -> RenameOutcome:
    try:
        problem = await _probe(_problem_blocking, renames)
    except (DeadlineExpired, OSError) as exc:
        problem = f"its Volume didn't answer ({exc})"
    if problem:
        return RenameOutcome(False, problem)
    held: list[str] = []
    try:
        problem = await run_detached_to_end(_lock_blocking, renames, held)
        if problem:
            return RenameOutcome(False, problem)
        return await _rename_locked(image_path, renames, library, cancel_event, on_wait)
    finally:
        # Only once everything the rename started has ended (``_settle``).
        _releaser(held)()


def _releaser(held: list[str]):
    def release() -> None:
        if held:
            start_detached_or_own_thread(_release_blocking, list(held))

    return release


async def _rename_locked(
    image_path: str, renames: dict[str, str], library, cancel_event,
    on_wait: Waiting | None,
) -> RenameOutcome:
    """:func:`rename_image` under the locks: the hook, then settle, inspect,
    carry or compensate."""
    try:
        files = await run_detached_undoable(
            _Files.open_blocking, renames, timeout=OPEN_SECONDS, undo=_Files.close,
        )
    except (OSError, ValueError) as exc:  # DeadlineExpired is a TimeoutError
        return RenameOutcome(False, f"couldn't open its folder ({exc})")
    try:
        journal = WriteJournal()
        handed: list[HandedOff] = []
        hook = await referrers.update_before_source_removal(
            image_path, renames, library=library, cancel_event=cancel_event,
            placement=files.placement(), unawaited=handed, journal=journal,
        )
        await _settle(hook, handed, journal, on_wait)
        reason = None if hook.ok else _failure(hook)
        if hook.ok:
            try:
                await run_detached_to_end(files.move_blocking)
            except OSError as exc:
                reason = f"couldn't rename the file ({exc.strerror or exc})"
        try:
            where = await _probe(files.where_blocking)
        except OSError:
            where = "unknown"
        return await _finish(renames, files.real_renames(), hook, journal, where, reason)
    finally:
        files.close()


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
